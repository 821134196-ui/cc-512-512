"""SQLite 数据层：绑定互斥事务、可用池准入规则、健康隔离与角色权限。"""
import sqlite3
import time
import os
import json
from contextlib import contextmanager

DB_PATH = os.environ.get("SWAP_DB", os.path.join(os.path.dirname(__file__), "data", "swap.db"))
PHOTO_DIR = os.path.join(os.path.dirname(__file__), "data", "photos")

# ---- 业务常量 ----
MIN_SOC = 60              # 换电可用最低电量（%）
OVERHEAT_TEMP = 55.0      # 过热阈值（℃）
MIN_HEALTH = 80           # 健康度准入阈值
CHARGE_RATE_PER_SEC = 0.25  # 模拟充电速率（%/秒），仅用于充满时间估算

# 阻断进入可用池的原因
BLOCK_OVERHEAT = "overheat"            # 电池过热
BLOCK_SLOT_FAULT = "slot_fault"        # 槽位故障导致的中断
BLOCK_INTERRUPTED = "charge_interrupted"  # 充电中断
BLOCK_LOW_HEALTH = "low_health"        # 健康度过低
BLOCK_WRONG_INSTALL = "wrong_install"  # 发现错装痕迹

ALL_BLOCKS = {BLOCK_OVERHEAT, BLOCK_SLOT_FAULT, BLOCK_INTERRUPTED,
              BLOCK_LOW_HEALTH, BLOCK_WRONG_INSTALL}


class Conflict(Exception):
    """事务冲突或业务规则拒绝（HTTP 409）。"""


class Forbidden(Exception):
    """角色无权操作（HTTP 403）。"""


def get_conn(path=None):
    path = path or DB_PATH
    conn = sqlite3.connect(path, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


@contextmanager
def transaction(conn):
    """BEGIN IMMEDIATE：并发扫码/换电时立刻拿写锁，消除重复绑定。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


SCHEMA = """
CREATE TABLE IF NOT EXISTS batteries(
  code TEXT PRIMARY KEY,
  model TEXT NOT NULL,             -- 电池型号
  soc INTEGER NOT NULL DEFAULT 0,  -- 电量百分比
  temperature REAL NOT NULL DEFAULT 25.0,
  health_score INTEGER NOT NULL DEFAULT 100,
  status TEXT NOT NULL DEFAULT 'loose',
    -- loose 待装 | charging 充电中 | on_vehicle 在车上 | isolated 隔离
  block_reasons TEXT NOT NULL DEFAULT '',
  isolated INTEGER NOT NULL DEFAULT 0,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS vehicles(
  code TEXT PRIMARY KEY,
  model TEXT NOT NULL,
  battery_model TEXT NOT NULL,    -- 兼容的电池型号
  driver TEXT NOT NULL DEFAULT '',
  work_state TEXT NOT NULL DEFAULT 'idle',
    -- idle 空闲 | working 作业中 | awaiting_swap 待换电 | swapping 换电中
  installed_battery_code TEXT REFERENCES batteries(code)
);
CREATE TABLE IF NOT EXISTS slots(
  code TEXT PRIMARY KEY,
  charger_id TEXT NOT NULL,
  serviceable INTEGER NOT NULL DEFAULT 1,
  fault_reason TEXT NOT NULL DEFAULT ''
);
-- 互斥绑定核心表：
-- 一个电池一行（同一时刻只能连一辆车或一个槽位）；
-- UNIQUE(kind,target_code) 保证每辆车/每个槽位至多一块电池。
CREATE TABLE IF NOT EXISTS active_bindings(
  battery_code TEXT PRIMARY KEY REFERENCES batteries(code) ON DELETE RESTRICT,
  kind TEXT NOT NULL CHECK(kind IN ('vehicle','slot')),
  target_code TEXT NOT NULL,
  since REAL NOT NULL,
  operator TEXT NOT NULL DEFAULT '',
  UNIQUE(kind, target_code)
);
CREATE TABLE IF NOT EXISTS swap_queue(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  vehicle_code TEXT NOT NULL REFERENCES vehicles(code),
  window TEXT NOT NULL DEFAULT '',        -- 预约的作业间隙
  requested_by TEXT NOT NULL DEFAULT '',
  requested_at REAL NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending'  -- pending|in_progress|done|cancelled
);
CREATE TABLE IF NOT EXISTS swap_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  queue_id INTEGER NOT NULL REFERENCES swap_queue(id),
  vehicle_code TEXT NOT NULL,
  old_battery_code TEXT,
  new_battery_code TEXT,
  operator TEXT NOT NULL DEFAULT '',
  old_scanned_at REAL,                   -- 旧电池扫码交接时间
  new_installed_at REAL,                 -- 新电池装上时间
  fixation_checked INTEGER DEFAULT 0,    -- 固定复核
  fixation_checked_at REAL,
  soc_checked INTEGER,                   -- 可用电量复核值
  soc_checked_at REAL,
  state TEXT NOT NULL DEFAULT 'created', -- created|old_removed|new_installed|completed|cancelled
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS charge_records(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  battery_code TEXT NOT NULL,
  slot_code TEXT NOT NULL,
  operator TEXT NOT NULL DEFAULT '',
  started_at REAL NOT NULL,
  ended_at REAL,
  start_soc INTEGER NOT NULL,
  end_soc INTEGER,
  eta REAL,                              -- 预计充满时间
  status TEXT NOT NULL DEFAULT 'charging', -- charging|completed|interrupted
  reason TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS health_checks(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  battery_code TEXT NOT NULL,
  inspector TEXT NOT NULL,
  result TEXT NOT NULL CHECK(result IN ('pass','quarantine')),
  health_score INTEGER NOT NULL,
  reasons TEXT NOT NULL DEFAULT '',
  photo TEXT NOT NULL DEFAULT '',
  note TEXT NOT NULL DEFAULT '',
  checked_at REAL NOT NULL
);
-- 健康检测结论只增不改：任何 UPDATE/DELETE 都被触发器拒绝
CREATE TRIGGER IF NOT EXISTS trg_health_immutable_update
BEFORE UPDATE ON health_checks
BEGIN SELECT RAISE(ABORT,'健康检测结论不可修改'); END;
CREATE TRIGGER IF NOT EXISTS trg_health_immutable_delete
BEFORE DELETE ON health_checks
BEGIN SELECT RAISE(ABORT,'健康检测结论不可删除'); END;

CREATE TABLE IF NOT EXISTS audit_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL, actor TEXT NOT NULL, role TEXT NOT NULL,
  action TEXT NOT NULL, detail TEXT NOT NULL DEFAULT ''
);
"""


def init_db(conn):
    conn.executescript(SCHEMA)


# ---------- 规则判定 ----------
def _reasons(b):
    return {r for r in (b["block_reasons"] or "").split(",") if r}


def evaluate_battery(row):
    """返回 (是否可进入可用池, 原因列表, 状态供界面展示)。"""
    reasons = set(_reasons(row))
    if row["isolated"]:
        reasons.add("isolated")
    if row["temperature"] >= OVERHEAT_TEMP:
        reasons.add(BLOCK_OVERHEAT)
    if row["health_score"] < MIN_HEALTH:
        reasons.add(BLOCK_LOW_HEALTH)
    if row["soc"] < MIN_SOC:
        reasons.add("low_soc")
    usable = not reasons and row["status"] in ("loose",)
    return usable, sorted(reasons)


REASON_TEXT = {
    BLOCK_OVERHEAT: "电池过热",
    BLOCK_SLOT_FAULT: "槽位故障",
    BLOCK_INTERRUPTED: "充电中断",
    BLOCK_LOW_HEALTH: "健康度过低",
    BLOCK_WRONG_INSTALL: "错装痕迹",
    "isolated": "健康隔离",
    "low_soc": "电量不足",
}


def _battery_view(conn, r):
    usable, reasons = evaluate_battery(r)
    bound = conn.execute(
        "SELECT kind,target_code,since,operator FROM active_bindings WHERE battery_code=?",
        (r["code"],)).fetchone()
    eta = None
    if r["status"] == "charging":
        rec = conn.execute(
            "SELECT eta,status FROM charge_records WHERE battery_code=? "
            "ORDER BY id DESC LIMIT 1", (r["code"],)).fetchone()
        if rec and rec["status"] == "charging":
            eta = rec["eta"]
    return {
        "code": r["code"], "model": r["model"], "soc": r["soc"],
        "temperature": r["temperature"], "health_score": r["health_score"],
        "status": r["status"], "isolated": bool(r["isolated"]),
        "block_reasons": reasons, "block_labels": [REASON_TEXT.get(x, x) for x in reasons],
        "usable": usable,
        "bound_to": ({"kind": bound["kind"], "target": bound["target_code"],
                      "since": bound["since"], "operator": bound["operator"]}
                     if bound else None),
        "eta": eta,
    }


# ---------- 查询 ----------
def list_batteries(conn, model=None, usable_only=False):
    rows = conn.execute("SELECT * FROM batteries ORDER BY code").fetchall()
    out = []
    for r in rows:
        v = _battery_view(conn, r)
        if model and v["model"] != model:
            continue
        if usable_only and not v["usable"]:
            continue
        out.append(v)
    return out


def pool_map(conn):
    """池位图：槽位 + 槽上电池，外加未上架的可用/阻断电池。"""
    slots = []
    for s in conn.execute("SELECT * FROM slots ORDER BY code").fetchall():
        b = conn.execute(
            "SELECT b.* FROM active_bindings ab JOIN batteries b ON b.code=ab.battery_code "
            "WHERE ab.kind='slot' AND ab.target_code=?", (s["code"],)).fetchone()
        slots.append({
            "code": s["code"], "charger_id": s["charger_id"],
            "serviceable": bool(s["serviceable"]), "fault_reason": s["fault_reason"],
            "battery": _battery_view(conn, b) if b else None,
        })
    loose = [_battery_view(conn, r) for r in
             conn.execute("SELECT * FROM batteries WHERE status='loose' ORDER BY code")]
    isolated = [_battery_view(conn, r) for r in
                conn.execute("SELECT * FROM batteries WHERE isolated=1 ORDER BY code")]
    return {"slots": slots, "loose": loose, "isolated": isolated}


def list_vehicles(conn):
    out = []
    for v in conn.execute("SELECT * FROM vehicles ORDER BY code").fetchall():
        b = None
        if v["installed_battery_code"]:
            r = conn.execute("SELECT * FROM batteries WHERE code=?",
                             (v["installed_battery_code"],)).fetchone()
            b = _battery_view(conn, r) if r else None
        d = dict(v)
        d["installed_battery"] = b
        out.append(d)
    return out


def list_queue(conn):
    out = []
    qs = conn.execute(
        "SELECT * FROM swap_queue WHERE status IN ('pending','in_progress') "
        "ORDER BY CASE status WHEN 'in_progress' THEN 0 ELSE 1 END, requested_at").fetchall()
    for q in qs:
        ev = conn.execute("SELECT * FROM swap_events WHERE queue_id=? ORDER BY id DESC LIMIT 1",
                          (q["id"],)).fetchone()
        d = dict(q)
        d["event"] = dict(ev) if ev else None
        out.append(d)
    return out


def list_swap_history(conn, limit=30):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM swap_events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]


def list_charge_records(conn, limit=50):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM charge_records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]


def list_health_checks(conn):
    return [dict(r) for r in
            conn.execute("SELECT * FROM health_checks ORDER BY id DESC").fetchall()]


def _audit(conn, actor, role, action, detail=""):
    conn.execute("INSERT INTO audit_log(ts,actor,role,action,detail) VALUES(?,?,?,?,?)",
                 (time.time(), actor, role, action, detail))


# ---------- 换电队列 ----------
def enqueue(conn, vehicle_code, window, actor):
    with transaction(conn):
        v = conn.execute("SELECT * FROM vehicles WHERE code=?", (vehicle_code,)).fetchone()
        if not v:
            raise Conflict(f"叉车 {vehicle_code} 不存在")
        exists = conn.execute(
            "SELECT 1 FROM swap_queue WHERE vehicle_code=? AND status IN ('pending','in_progress')",
            (vehicle_code,)).fetchone()
        if exists:
            raise Conflict(f"{vehicle_code} 已有进行中的预约")
        cur = conn.execute(
            "INSERT INTO swap_queue(vehicle_code,window,requested_by,requested_at,status) "
            "VALUES(?,?,?,?,'pending')",
            (vehicle_code, window, actor, time.time()))
        conn.execute("UPDATE vehicles SET work_state='awaiting_swap' WHERE code=?",
                     (vehicle_code,))
        _audit(conn, actor, "dispatcher", "enqueue", f"{vehicle_code} @ {window}")
        return cur.lastrowid


def cancel_queue(conn, queue_id, actor):
    with transaction(conn):
        q = conn.execute("SELECT * FROM swap_queue WHERE id=?", (queue_id,)).fetchone()
        if not q or q["status"] not in ("pending",):
            raise Conflict("只有待处理预约可以取消")
        conn.execute("UPDATE swap_queue SET status='cancelled' WHERE id=?", (queue_id,))
        conn.execute("UPDATE vehicles SET work_state='idle' WHERE code=? AND work_state='awaiting_swap'",
                     (q["vehicle_code"],))
        _audit(conn, actor, "dispatcher", "cancel_queue", str(queue_id))


# ---------- 换电流程（扫码交接 → 装入 → 复核） ----------
def _start_event(conn, q, actor):
    ev = conn.execute("SELECT * FROM swap_events WHERE queue_id=? ORDER BY id DESC LIMIT 1",
                      (q["id"],)).fetchone()
    if ev:
        return ev
    cur = conn.execute(
        "INSERT INTO swap_events(queue_id,vehicle_code,operator,state,created_at) "
        "VALUES(?,?,?, 'created', ?)", (q["id"], q["vehicle_code"], actor, time.time()))
    return conn.execute("SELECT * FROM swap_events WHERE id=?", (cur.lastrowid,)).fetchone()


def scan_old_battery(conn, queue_id, actor):
    """取下旧电池时扫码交接：解除 车-电池 绑定。"""
    with transaction(conn):
        q = conn.execute("SELECT * FROM swap_queue WHERE id=?", (queue_id,)).fetchone()
        if not q:
            raise Conflict("预约不存在")
        if q["status"] not in ("pending", "in_progress"):
            raise Conflict("该预约不在可换电状态")
        v = conn.execute("SELECT * FROM vehicles WHERE code=?", (q["vehicle_code"],)).fetchone()
        old_code = v["installed_battery_code"]
        if not old_code:
            raise Conflict(f"{v['code']} 当前没有绑定电池，无需扫码取下")
        old = conn.execute("SELECT * FROM batteries WHERE code=?", (old_code,)).fetchone()

        conn.execute("UPDATE swap_queue SET status='in_progress' WHERE id=?", (queue_id,))
        conn.execute("UPDATE vehicles SET work_state='swapping' WHERE code=?", (v["code"],))
        ev = _start_event(conn, q, actor)
        # 互斥解除
        conn.execute("DELETE FROM active_bindings WHERE battery_code=? AND kind='vehicle'",
                     (old_code,))
        conn.execute("UPDATE batteries SET status='loose',updated_at=? WHERE code=?",
                     (time.time(), old_code))
        conn.execute("UPDATE vehicles SET installed_battery_code=NULL WHERE code=?", (v["code"],))
        conn.execute(
            "UPDATE swap_events SET old_battery_code=?,old_scanned_at=?,operator=?,"
            "state='old_removed' WHERE id=?",
            (old_code, time.time(), actor, ev["id"]))
        _audit(conn, actor, "operator", "scan_old",
               f"queue={queue_id} {v['code']} 取下 {old_code} 电量{old['soc']}%")
        return {"event_id": ev["id"], "old_battery": old_code}


def install_new_battery(conn, queue_id, battery_code, actor):
    """装上新电池：事务内校验 空闲/型号兼容/可入池，再建互斥绑定。"""
    with transaction(conn):
        q = conn.execute("SELECT * FROM swap_queue WHERE id=?", (queue_id,)).fetchone()
        if not q or q["status"] != "in_progress":
            raise Conflict("请先扫码取下旧电池")
        v = conn.execute("SELECT * FROM vehicles WHERE code=?", (q["vehicle_code"],)).fetchone()
        if v["installed_battery_code"]:
            raise Conflict("叉车上仍有电池，请先取下")
        b = conn.execute("SELECT * FROM batteries WHERE code=?", (battery_code,)).fetchone()
        if not b:
            raise Conflict(f"电池 {battery_code} 不存在")

        # 型号兼容（错装防线）
        if b["model"] != v["battery_model"]:
            _audit(conn, actor, "operator", "install_rejected_model",
                   f"queue={queue_id} 电池{battery_code}({b['model']})≠{v['code']}要求({v['battery_model']})")
            raise Conflict(f"型号不兼容：{battery_code} 是 {b['model']}，"
                           f"{v['code']} 需要 {v['battery_model']}，禁止装入")

        usable, reasons = evaluate_battery(b)
        if not usable:
            labels = "、".join(REASON_TEXT.get(x, x) for x in reasons)
            _audit(conn, actor, "operator", "install_rejected_pool",
                   f"queue={queue_id} {battery_code} {labels}")
            raise Conflict(f"{battery_code} 不可进入可用池：{labels}")

        now = time.time()
        # 唯一约束兜底：重复绑定/并发抢装在此处失败
        try:
            conn.execute(
                "INSERT INTO active_bindings(battery_code,kind,target_code,since,operator) "
                "VALUES(?,?,?,?,?)",
                (battery_code, "vehicle", v["code"], now, actor))
        except sqlite3.IntegrityError:
            raise Conflict(f"{battery_code} 已被占用，请刷新池位后重试（互斥绑定）")

        conn.execute("UPDATE batteries SET status='on_vehicle',updated_at=? WHERE code=?",
                     (now, battery_code))
        conn.execute("UPDATE vehicles SET installed_battery_code=?,work_state='swapping' WHERE code=?",
                     (battery_code, v["code"]))
        ev = _start_event(conn, q, actor)
        conn.execute(
            "UPDATE swap_events SET new_battery_code=?,new_installed_at=?,operator=?,"
            "state='new_installed' WHERE id=?",
            (battery_code, now, actor, ev["id"]))
        _audit(conn, actor, "operator", "install_new",
               f"queue={queue_id} {v['code']} 装入 {battery_code}")
        return {"event_id": ev["id"], "new_battery": battery_code}


def complete_swap(conn, queue_id, fixation_ok, actor):
    """装后复核：固定 + 可用电量。"""
    with transaction(conn):
        ev = conn.execute("SELECT * FROM swap_events WHERE queue_id=? ORDER BY id DESC LIMIT 1",
                          (queue_id,)).fetchone()
        if not ev or ev["state"] != "new_installed":
            raise Conflict("请先装上新电池再复核")
        if not fixation_ok:
            raise Conflict("固定复核未通过：请重新锁固后再提交")
        b = conn.execute("SELECT * FROM batteries WHERE code=?",
                         (ev["new_battery_code"],)).fetchone()
        if b["soc"] < MIN_SOC:
            _audit(conn, actor, "operator", "soc_check_failed",
                   f"queue={queue_id} {b['code']} 仅{b['soc']}%")
            raise Conflict(f"可用电量复核未通过：{b['soc']}% < {MIN_SOC}%")
        now = time.time()
        conn.execute(
            "UPDATE swap_events SET fixation_checked=1,fixation_checked_at=?,"
            "soc_checked=?,soc_checked_at=?,state='completed' WHERE id=?",
            (now, b["soc"], now, ev["id"]))
        conn.execute("UPDATE swap_queue SET status='done' WHERE id=?", (queue_id,))
        conn.execute("UPDATE vehicles SET work_state='working' WHERE code=?",
                     (ev["vehicle_code"],))
        _audit(conn, actor, "operator", "swap_complete",
               f"queue={queue_id} {ev['vehicle_code']} {ev['old_battery_code']}→"
               f"{ev['new_battery_code']} 固定OK 电量{b['soc']}%")
        return {"ok": True}


# ---------- 充电 ----------
def start_charge(conn, battery_code, slot_code, actor):
    with transaction(conn):
        b = conn.execute("SELECT * FROM batteries WHERE code=?", (battery_code,)).fetchone()
        s = conn.execute("SELECT * FROM slots WHERE code=?", (slot_code,)).fetchone()
        if not b:
            raise Conflict("电池不存在")
        if not s:
            raise Conflict("槽位不存在")
        if not s["serviceable"]:
            raise Conflict(f"槽位 {slot_code} 故障（{s['fault_reason']}），无法充电")
        if b["status"] != "loose":
            raise Conflict(f"{battery_code} 当前状态 {b['status']}，不能上架充电")
        if b["isolated"]:
            raise Conflict(f"{battery_code} 处于健康隔离，需维修人员解除后才能充电")
        now = time.time()
        try:
            conn.execute(
                "INSERT INTO active_bindings(battery_code,kind,target_code,since,operator) "
                "VALUES(?,?,?,?,?)",
                (battery_code, "slot", slot_code, now, actor))
        except sqlite3.IntegrityError:
            raise Conflict("电池或槽位已被占用（互斥绑定）")
        eta = now + max(0, 100 - b["soc"]) / CHARGE_RATE_PER_SEC
        conn.execute(
            "INSERT INTO charge_records(battery_code,slot_code,operator,started_at,"
            "start_soc,eta,status) VALUES(?,?,?,?,?,?,'charging')",
            (battery_code, slot_code, actor, now, b["soc"], eta))
        conn.execute("UPDATE batteries SET status='charging',updated_at=? WHERE code=?",
                     (now, battery_code))
        _audit(conn, actor, "operator", "charge_start", f"{battery_code}@{slot_code}")
        return {"eta": eta}


def interrupt_charge(conn, battery_code, reason, actor="charger-adapter"):
    """充电中断：解绑并阻断进入可用池（适配器或维修调用）。"""
    with transaction(conn):
        b = conn.execute("SELECT * FROM batteries WHERE code=?", (battery_code,)).fetchone()
        if not b or b["status"] != "charging":
            return False
        bind = conn.execute(
            "SELECT * FROM active_bindings WHERE battery_code=? AND kind='slot'",
            (battery_code,)).fetchone()
        rec = conn.execute(
            "SELECT * FROM charge_records WHERE battery_code=? AND status='charging' "
            "ORDER BY id DESC LIMIT 1", (battery_code,)).fetchone()
        now = time.time()
        if rec:
            conn.execute(
                "UPDATE charge_records SET status='interrupted',ended_at=?,end_soc=?,reason=? "
                "WHERE id=?", (now, b["soc"], reason, rec["id"]))
        conn.execute("DELETE FROM active_bindings WHERE battery_code=?", (battery_code,))
        reasons = _reasons(b)
        reasons.add(BLOCK_INTERRUPTED if reason != BLOCK_SLOT_FAULT else BLOCK_SLOT_FAULT)
        conn.execute(
            "UPDATE batteries SET status='loose',block_reasons=?,updated_at=? WHERE code=?",
            (",".join(sorted(reasons)), now, battery_code))
        _audit(conn, actor, "system", "charge_interrupted", f"{battery_code}:{reason}")
        return True


# ---------- 健康隔离（仅维修人员） ----------
def require_maintenance(role):
    if role != "maintenance":
        raise Forbidden("只有维修人员可以修改健康检测结论与隔离状态")


def health_check(conn, battery_code, result, health_score, reasons, photo, note, actor, role):
    require_maintenance(role)
    with transaction(conn):
        b = conn.execute("SELECT * FROM batteries WHERE code=?", (battery_code,)).fetchone()
        if not b:
            raise Conflict("电池不存在")
        now = time.time()
        conn.execute(
            "INSERT INTO health_checks(battery_code,inspector,result,health_score,reasons,"
            "photo,note,checked_at) VALUES(?,?,?,?,?,?,?,?)",
            (battery_code, actor, result, health_score, reasons, photo, note, now))
        if result == "quarantine":
            # 在槽上的充电立即中断
            if b["status"] == "charging":
                conn.execute("DELETE FROM active_bindings WHERE battery_code=?", (battery_code,))
                conn.execute(
                    "UPDATE charge_records SET status='interrupted',ended_at=?,end_soc=?,"
                    "reason='health_quarantine' WHERE battery_code=? AND status='charging'",
                    (now, b["soc"], battery_code))
            cur = set(_reasons(b))
            for r in reasons.split(","):
                if r in ALL_BLOCKS:
                    cur.add(r)
            if health_score < MIN_HEALTH:
                cur.add(BLOCK_LOW_HEALTH)
            conn.execute(
                "UPDATE batteries SET isolated=1,status='isolated',health_score=?,"
                "block_reasons=?,updated_at=? WHERE code=?",
                (health_score, ",".join(sorted(x for x in cur if x)), now, battery_code))
        else:  # pass：解除隔离，健康类阻断由本次结论覆盖
            cur = set(_reasons(b))
            cur.discard(BLOCK_LOW_HEALTH)
            cur.discard(BLOCK_OVERHEAT)  # 复检温度正常后清除
            cur.discard(BLOCK_WRONG_INSTALL)
            if b["temperature"] >= OVERHEAT_TEMP:
                cur.add(BLOCK_OVERHEAT)
            new_status = "loose" if b["status"] == "isolated" else b["status"]
            conn.execute(
                "UPDATE batteries SET isolated=0,status=?,health_score=?,block_reasons=?,"
                "updated_at=? WHERE code=?",
                (new_status, health_score, ",".join(sorted(cur)), now, battery_code))
        _audit(conn, actor, role, "health_check", f"{battery_code} {result} {health_score}")


def set_slot_state(conn, slot_code, serviceable, reason, actor, role):
    require_maintenance(role)
    with transaction(conn):
        s = conn.execute("SELECT * FROM slots WHERE code=?", (slot_code,)).fetchone()
        if not s:
            raise Conflict("槽位不存在")
        conn.execute("UPDATE slots SET serviceable=?,fault_reason=? WHERE code=?",
                     (1 if serviceable else 0, "" if serviceable else reason, slot_code))
        if not serviceable:
            # 槽位故障：在充电池中断并隔离出可用池
            b = conn.execute(
                "SELECT b.* FROM active_bindings ab JOIN batteries b ON b.code=ab.battery_code "
                "WHERE ab.kind='slot' AND ab.target_code=?", (slot_code,)).fetchone()
            if b:
                conn.execute("DELETE FROM active_bindings WHERE battery_code=?", (b["code"],))
                now = time.time()
                conn.execute(
                    "UPDATE charge_records SET status='interrupted',ended_at=?,end_soc=?,"
                    "reason=? WHERE battery_code=? AND status='charging'",
                    (now, b["soc"], BLOCK_SLOT_FAULT, b["code"]))
                cur = set(_reasons(b)) | {BLOCK_SLOT_FAULT, BLOCK_INTERRUPTED}
                conn.execute(
                    "UPDATE batteries SET status='loose',block_reasons=?,updated_at=? WHERE code=?",
                    (",".join(sorted(cur)), now, b["code"]))
        _audit(conn, actor, role, "slot_state",
               f"{slot_code} {'修复' if serviceable else '故障:'+reason}")


def overview(conn):
    batteries = list_batteries(conn)
    return {
        "generated_at": time.time(),
        "counts": {
            "batteries": len(batteries),
            "usable": sum(1 for b in batteries if b["usable"]),
            "charging": sum(1 for b in batteries if b["status"] == "charging"),
            "on_vehicle": sum(1 for b in batteries if b["status"] == "on_vehicle"),
            "isolated": sum(1 for b in batteries if b["isolated"]),
            "overheat": sum(1 for b in batteries if BLOCK_OVERHEAT in b["block_reasons"]),
        },
        "constants": {
            "min_soc": MIN_SOC, "overheat_temp": OVERHEAT_TEMP,
            "min_health": MIN_HEALTH,
            "charge_rate_per_sec": CHARGE_RATE_PER_SEC,
        },
    }
