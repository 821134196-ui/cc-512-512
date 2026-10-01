# -*- coding: utf-8 -*-
"""业务服务层：换电事务流程、可用池规则、健康隔离与权限。

换电工单状态机：
    已预约 → 换电中 → 待入槽 → 待安装 → 待复核 → 已完成
      （任意未完成状态可取消）

扫码三段：取下旧电池(OLD_REMOVE) → 旧电池入槽(SLOT_IN) → 装新电池(NEW_INSTALL)，
每一步都在 ``BEGIN IMMEDIATE`` 事务内完成校验与绑定，配合 bonds 表的
主键 / UNIQUE / CHECK 约束，保证一块电池同一时刻只连一辆车或一个槽位。
"""
import math
import os
import sqlite3
import uuid

from .db import Database, now

SOH_MIN = 80              # 健康度低于此值不得进入可用池
TEMP_HOT = 55.0           # 过热阈值 ℃
TEMP_RESET = 50.0         # 降温复位阈值
PHOTO_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "data", "photos")

ROLE_NAME = {"dispatcher": "调度员", "maintainer": "维修人员", "operator": "换电员"}


class ServiceError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class _ScanReject(Exception):
    """扫码被业务规则拒绝：事务回滚后，拒绝记录仍须留痕。"""
    def __init__(self, message, detail, battery_id=None, slot_id=None, status=400):
        self.message = message
        self.detail = detail
        self.battery_id = battery_id
        self.slot_id = slot_id
        self.status = status


class SwapService:
    def __init__(self, db: Database):
        self.db = db

    # ---------- 基础工具 ----------
    def _user(self, conn, username: str) -> sqlite3.Row:
        u = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if not u:
            raise ServiceError("未知用户：%s" % username, 401)
        return u

    def require_role(self, username: str, *roles: str) -> sqlite3.Row:
        conn = self.db.connect()
        u = self._user(conn, username)
        if u["role"] not in roles:
            raise ServiceError("权限不足：该操作需要 %s" % "/".join(ROLE_NAME[r] for r in roles), 403)
        return u

    def _log(self, conn, kind: str, message: str):
        conn.execute("INSERT INTO event_log(kind,message,created_at) VALUES(?,?,?)",
                     (kind, message, now()))

    def _scan(self, conn, order_id, battery_id, slot_id, phase, username, ok, detail):
        conn.execute(
            """INSERT INTO scan_events(order_id,battery_id,slot_id,phase,operator,result,detail,created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (order_id, battery_id, slot_id, phase, username, "成功" if ok else "拒绝", detail, now()))

    @staticmethod
    def model_accepted(model: str, accepted_csv: str) -> bool:
        accepted = [x.strip() for x in (accepted_csv or "*").split(",") if x.strip()]
        return "*" in accepted or model in accepted

    # ---------- 查询：池位 / 队列 / 状态 ----------
    def pool(self, forklift_id: str = None):
        """可用池：六不入 —— 过热 / 槽位故障 / 充电中断 / 型号不兼容 / 错装 / 健康度过低。"""
        conn = self.db.connect()
        fk = None
        if forklift_id:
            fk = conn.execute("SELECT * FROM forklifts WHERE id=?", (forklift_id,)).fetchone()
            if not fk:
                raise ServiceError("叉车不存在")
        rows = conn.execute(
            """SELECT b.*, bo.forklift_id AS bound_forklift, bo.slot_id AS bound_slot,
                      s.fault AS slot_fault, s.name AS slot_name,
                      cl.status AS charge_status, cl.id AS charge_log_id
               FROM batteries b
               LEFT JOIN bonds bo ON bo.battery_id=b.id
               LEFT JOIN slots s ON s.id=bo.slot_id
               LEFT JOIN charge_logs cl ON cl.id=(
                     SELECT id FROM charge_logs WHERE battery_id=b.id
                     ORDER BY id DESC LIMIT 1)
               ORDER BY b.id""").fetchall()
        result = []
        for r in rows:
            reasons = self._exclude_reasons(r)
            if fk and not self.model_accepted(r["model"], fk["accepted_models"]):
                reasons.append("型号不兼容（%s 不支持 %s）" % (fk["id"], r["model"]))
            # 错装防护：已装在与型号不匹配的车上，明确标记
            mismatch = False
            if r["bound_forklift"]:
                bf = conn.execute("SELECT * FROM forklifts WHERE id=?",
                                  (r["bound_forklift"],)).fetchone()
                mismatch = not self.model_accepted(r["model"], bf["accepted_models"])
                if mismatch:
                    reasons.append("错装：%s 不兼容 %s" % (r["id"], bf["model"]))
            result.append({
                "id": r["id"], "model": r["model"], "soc": r["soc"],
                "temperature": r["temperature"], "soh": r["soh"],
                "overheating": bool(r["overheating"]),
                "charge_interrupted": bool(r["charge_interrupted"]),
                "health_status": r["health_status"],
                "isolate_reason": r["isolate_reason"],
                "bound_forklift": r["bound_forklift"],
                "bound_slot": r["bound_slot"],
                "slot_name": r["slot_name"],
                "charging": r["charge_status"] == "充电中" and r["bound_slot"],
                "mismatch": mismatch,
                "available": not reasons,
                "exclude_reasons": reasons,
                "eta_minutes": eta_minutes(r["soc"]) if r["charge_status"] == "充电中" else None,
            })
        return result

    @staticmethod
    def _exclude_reasons(r) -> list:
        reasons = []
        if r["health_status"] == "隔离":
            reasons.append("健康隔离" + ("：%s" % r["isolate_reason"] if r["isolate_reason"] else ""))
        if r["health_status"] == "退役":
            reasons.append("已退役")
        if r["soh"] < SOH_MIN:
            reasons.append("健康度过低（SOH %d%%<%d%%）" % (r["soh"], SOH_MIN))
        if r["overheating"]:
            reasons.append("电池过热（%.1f℃）" % r["temperature"])
        if r["charge_interrupted"]:
            reasons.append("充电中断未闭环")
        if r["bound_forklift"]:
            reasons.append("已绑定叉车 %s" % r["bound_forklift"])
        if r["bound_slot"] and (r["slot_fault"] or r["charge_status"] == "充电中"):
            if r["slot_fault"]:
                reasons.append("所在槽位故障")
            else:
                reasons.append("充电中")
        return reasons

    def queue(self):
        conn = self.db.connect()
        rows = conn.execute(
            """SELECT o.*, f.model AS forklift_model,
                      ob.soc AS old_soc, ob.temperature AS old_temp, ob.model AS old_model,
                      nb.soc AS new_soc, nb.model AS new_model
               FROM swap_orders o
               JOIN forklifts f ON f.id=o.forklift_id
               LEFT JOIN batteries ob ON ob.id=o.old_battery_id
               LEFT JOIN batteries nb ON nb.id=o.new_battery_id
               WHERE o.status NOT IN ('已完成','已取消')
               ORDER BY o.scheduled_at""").fetchall()
        return [dict(r) for r in rows]

    def orders_recent(self, limit=30):
        rows = self.db.query(
            """SELECT o.*, f.model AS forklift_model,
                      ob.soc AS old_soc, ob.temperature AS old_temp, ob.model AS old_model,
                      nb.soc AS new_soc, nb.model AS new_model
               FROM swap_orders o
               JOIN forklifts f ON f.id=o.forklift_id
               LEFT JOIN batteries ob ON ob.id=o.old_battery_id
               LEFT JOIN batteries nb ON nb.id=o.new_battery_id
               ORDER BY o.scheduled_at DESC LIMIT ?""", (limit,))
        return [dict(r) for r in rows]

    def forklifts(self):
        return [dict(r) for r in self.db.query(
            """SELECT f.*, b.model AS battery_model, b.soc AS battery_soc,
                      b.temperature AS battery_temp, b.overheating AS battery_hot
               FROM forklifts f LEFT JOIN batteries b ON b.id=f.current_battery_id
               ORDER BY f.id""")]

    def slots(self):
        return [dict(r) for r in self.db.query(
            """SELECT s.*, bo.battery_id, b.soc AS battery_soc, b.model AS battery_model,
                      b.temperature AS battery_temp, sig.voltage, sig.current, sig.updated_at AS signal_at
               FROM slots s
               LEFT JOIN bonds bo ON bo.slot_id=s.id
               LEFT JOIN batteries b ON b.id=bo.battery_id
               LEFT JOIN charger_signals sig ON sig.slot_id=s.id
               ORDER BY s.id""")]

    def charge_logs(self, limit=50):
        return [dict(r) for r in self.db.query(
            "SELECT * FROM charge_logs ORDER BY id DESC LIMIT ?", (limit,))]

    def health_checks(self, limit=50):
        rows = self.db.query(
            "SELECT * FROM health_checks ORDER BY id DESC LIMIT ?", (limit,))
        out = []
        for r in rows:
            d = dict(r)
            d["photo_url"] = ("/photos/" + os.path.basename(d["photo_path"])
                              if d["photo_path"] else None)
            out.append(d)
        return out

    def scan_events(self, limit=50):
        return [dict(r) for r in self.db.query(
            "SELECT * FROM scan_events ORDER BY id DESC LIMIT ?", (limit,))]

    def state(self, forklift_id=None):
        return {
            "now": now(),
            "users": {u["username"]: {"name": u["name"], "role": u["role"],
                                      "role_name": ROLE_NAME[u["role"]]}
                      for u in self.db.query("SELECT * FROM users")},
            "pool": self.pool(forklift_id),
            "queue": self.queue(),
            "orders": self.orders_recent(),
            "forklifts": self.forklifts(),
            "slots": self.slots(),
            "charge_logs": self.charge_logs(),
            "health_checks": self.health_checks(),
            "scan_events": self.scan_events(),
            "thresholds": {"soh_min": SOH_MIN, "temp_hot": TEMP_HOT},
            "role_name": ROLE_NAME,
        }

    # ---------- 换电流程 ----------
    def reserve(self, forklift_id: str, username: str, note: str = ""):
        """班组在作业间隙预约换电。"""
        with self.db.transaction() as conn:
            self._user(conn, username)
            fk = conn.execute("SELECT * FROM forklifts WHERE id=?", (forklift_id,)).fetchone()
            if not fk:
                raise ServiceError("叉车不存在")
            dup = conn.execute(
                "SELECT id FROM swap_orders WHERE forklift_id=? AND status NOT IN ('已完成','已取消')",
                (forklift_id,)).fetchone()
            if dup:
                raise ServiceError("%s 已有进行中的换电单 %s" % (forklift_id, dup["id"]))
            oid = self._new_order_id(conn)
            conn.execute(
                """INSERT INTO swap_orders(id,forklift_id,status,operator,scheduled_at,note)
                   VALUES(?,?,?,?,?,?)""",
                (oid, forklift_id, "已预约", username, now(), note))
            conn.execute("UPDATE forklifts SET status='等待换电',updated_at=? WHERE id=?",
                         (now(), forklift_id))
            self._log(conn, "预约", "%s 为 %s 预约换电（%s）" % (username, forklift_id, oid))
            return oid

    @staticmethod
    def _new_order_id(conn) -> str:
        import datetime
        day = datetime.datetime.now().strftime("%Y%m%d")
        n = conn.execute("SELECT COUNT(*) c FROM swap_orders WHERE id LIKE ?",
                         ("SW-%s-%%" % day,)).fetchone()["c"] + 1
        return "SW-%s-%03d" % (day, n)

    def start(self, order_id: str, username: str):
        with self.db.transaction() as conn:
            self._user(conn, username)
            o = self._get_order(conn, order_id)
            if o["status"] != "已预约":
                raise ServiceError("当前状态为「%s」，不能开始" % o["status"])
            conn.execute("UPDATE swap_orders SET status='换电中',started_at=? WHERE id=?",
                         (now(), order_id))
            conn.execute("UPDATE forklifts SET status='换电中',updated_at=? WHERE id=?",
                         (now(), o["forklift_id"]))
            self._log(conn, "开工", "%s 开始执行 %s" % (username, order_id))

    def _record_scan(self, order_id, battery_id, slot_id, phase, username, ok, detail):
        with self.db.transaction() as conn:
            self._scan(conn, order_id, battery_id, slot_id, phase, username, ok, detail)

    def scan_old_remove(self, order_id: str, code: str, username: str):
        """扫码取下旧电池并交接。"""
        try:
            with self.db.transaction() as conn:
                self._user(conn, username)
                o = self._get_order(conn, order_id)
                if o["status"] != "换电中":
                    raise _ScanReject("当前状态为「%s」，不能取下旧电池" % o["status"],
                                      "状态不是换电中（%s）" % o["status"], code)
                b = conn.execute("SELECT * FROM batteries WHERE id=?", (code,)).fetchone()
                if not b:
                    raise _ScanReject("扫码失败：电池编号 %s 不存在" % code, "电池码无效", code)
                bond = conn.execute("SELECT * FROM bonds WHERE battery_id=?", (code,)).fetchone()
                if not bond or bond["forklift_id"] != o["forklift_id"]:
                    raise _ScanReject("拒绝取下：%s 不是 %s 当前绑定的电池" % (code, o["forklift_id"]),
                                      "旧电池未绑定本叉车", code)
                # 交接：解绑车-电池
                conn.execute("DELETE FROM bonds WHERE battery_id=?", (code,))
                conn.execute(
                    """UPDATE swap_orders SET old_battery_id=?,status='待入槽',old_scan_at=?
                       WHERE id=?""", (code, now(), order_id))
                conn.execute("UPDATE forklifts SET current_battery_id=NULL,updated_at=? WHERE id=?",
                             (now(), o["forklift_id"]))
                self._scan(conn, order_id, code, None, "OLD_REMOVE", username, True,
                           "旧电池取下交接完成")
                self._log(conn, "扫码", "%s 取下旧电池 %s（%s）" % (username, code, order_id))
        except _ScanReject as r:
            self._record_scan(order_id, r.battery_id, None, "OLD_REMOVE", username, False,
                              r.detail)
            raise ServiceError(r.message, r.status)

    def scan_slot_in(self, order_id: str, slot_code: str, username: str):
        """旧电池扫码入槽，开始充电。"""
        try:
            with self.db.transaction() as conn:
                self._user(conn, username)
                o = self._get_order(conn, order_id)
                if o["status"] != "待入槽" or not o["old_battery_id"]:
                    raise _ScanReject("请先扫码取下旧电池", "状态不是待入槽",
                                      o["old_battery_id"], slot_code)
                slot = conn.execute("SELECT * FROM slots WHERE id=?", (slot_code,)).fetchone()
                b = conn.execute("SELECT * FROM batteries WHERE id=?",
                                 (o["old_battery_id"],)).fetchone()
                if not slot:
                    raise _ScanReject("扫码失败：槽位 %s 不存在" % slot_code, "槽位码无效",
                                      b["id"], slot_code)
                if slot["fault"]:
                    raise _ScanReject("拒绝入槽：%s 处于故障状态" % slot_code, "槽位故障",
                                      b["id"], slot_code)
                if not self.model_accepted(b["model"], slot["accepted_models"]):
                    raise _ScanReject(
                        "拒绝入槽：%s 仅接受 %s，电池为 %s"
                        % (slot_code, slot["accepted_models"], b["model"]),
                        "型号不兼容", b["id"], slot_code)
                occupied = conn.execute("SELECT battery_id FROM bonds WHERE slot_id=?",
                                        (slot_code,)).fetchone()
                if occupied:
                    raise _ScanReject("拒绝入槽：%s 已绑定 %s" % (slot_code, occupied["battery_id"]),
                                      "槽位已被占用", b["id"], slot_code, 409)
                try:
                    conn.execute(
                        "INSERT INTO bonds(battery_id,forklift_id,slot_id,bound_at) "
                        "VALUES(?,NULL,?,?)", (b["id"], slot_code, now()))
                except sqlite3.IntegrityError:
                    raise _ScanReject("拒绝：电池或槽位已被占用（并发冲突）",
                                      "电池/槽位已被占用", b["id"], slot_code, 409)
                conn.execute("UPDATE slots SET status='充电中',updated_at=? WHERE id=?",
                             (now(), slot_code))
                conn.execute(
                    """INSERT INTO charge_logs(battery_id,slot_id,started_at,start_soc,status)
                       VALUES(?,?,?,?, '充电中')""", (b["id"], slot_code, now(), b["soc"]))
                # 重新入槽充电视为闭环：清中断标记（过热标记须由维修降温复位清除）
                conn.execute("UPDATE batteries SET charge_interrupted=0,updated_at=? WHERE id=?",
                             (now(), b["id"]))
                conn.execute(
                    "UPDATE swap_orders SET slot_id=?,status='待安装',slot_scan_at=? WHERE id=?",
                    (slot_code, now(), order_id))
                self._scan(conn, order_id, b["id"], slot_code, "SLOT_IN", username, True,
                           "旧电池入槽开始充电")
                self._log(conn, "扫码", "%s 将 %s 放入 %s 充电（%s）"
                          % (username, b["id"], slot_code, order_id))
        except _ScanReject as r:
            self._record_scan(order_id, r.battery_id, r.slot_id, "SLOT_IN", username, False,
                              r.detail)
            raise ServiceError(r.message, r.status)

    def scan_new_install(self, order_id: str, code: str, username: str):
        """扫码安装新电池：可用池 + 型号兼容 + 互斥绑定，全部在一个写事务内。"""
        try:
            with self.db.transaction() as conn:
                self._user(conn, username)
                o = self._get_order(conn, order_id)
                if o["status"] != "待安装":
                    raise _ScanReject("请先将旧电池入槽，再扫码安装新电池",
                                      "状态不是待安装（%s）" % o["status"], code)
                b = conn.execute("SELECT * FROM batteries WHERE id=?", (code,)).fetchone()
                fk = conn.execute("SELECT * FROM forklifts WHERE id=?",
                                  (o["forklift_id"],)).fetchone()
                if not b:
                    raise _ScanReject("扫码失败：电池编号 %s 不存在" % code, "电池码无效", code)
                # 六不入复核（事务内读最新状态）
                reasons = []
                if b["health_status"] != "正常":
                    reasons.append("健康隔离/退役")
                if b["soh"] < SOH_MIN:
                    reasons.append("健康度过低")
                if b["overheating"]:
                    reasons.append("电池过热 %.1f℃" % b["temperature"])
                if b["charge_interrupted"]:
                    reasons.append("充电中断未闭环")
                bond = conn.execute("SELECT * FROM bonds WHERE battery_id=?", (code,)).fetchone()
                if bond:
                    if bond["forklift_id"]:
                        reasons.append("并发冲突：已绑定叉车 %s（可能正被他人扫码）"
                                       % bond["forklift_id"])
                    elif bond["slot_id"]:
                        slot = conn.execute("SELECT * FROM slots WHERE id=?",
                                            (bond["slot_id"],)).fetchone()
                        if slot["fault"]:
                            reasons.append("所在槽位故障")
                        else:
                            reasons.append("仍在槽位 %s 上" % bond["slot_id"])
                if not self.model_accepted(b["model"], fk["accepted_models"]):
                    reasons.append("型号不兼容：%s 需要 %s" % (fk["model"], fk["accepted_models"]))
                if reasons:
                    raise _ScanReject("拒绝安装 %s：%s" % (code, "；".join(reasons)),
                                      "；".join(reasons), code, status=409)
                try:
                    conn.execute(
                        "INSERT INTO bonds(battery_id,forklift_id,slot_id,bound_at) "
                        "VALUES(?,?,NULL,?)", (code, fk["id"], now()))
                except sqlite3.IntegrityError:
                    # 两人并发扫同一块电池：后到者在此被数据库驳回
                    raise _ScanReject(
                        "并发冲突：%s 刚被其他换电员绑定，请改选其他电池" % code,
                        "并发冲突：电池已被绑定", code, status=409)
                conn.execute("UPDATE forklifts SET current_battery_id=?,updated_at=? WHERE id=?",
                             (code, now(), fk["id"]))
                conn.execute(
                    "UPDATE swap_orders SET new_battery_id=?,status='待复核',new_scan_at=? "
                    "WHERE id=?", (code, now(), order_id))
                self._scan(conn, order_id, code, None, "NEW_INSTALL", username, True,
                           "新电池安装完成，待复核")
                self._log(conn, "扫码", "%s 为 %s 安装新电池 %s（%s）"
                          % (username, fk["id"], code, order_id))
        except _ScanReject as r:
            # 事务已回滚，拒绝记录在独立事务中补写，保证扫码流水不丢
            self._record_scan(order_id, r.battery_id, None, "NEW_INSTALL", username, False,
                              r.detail)
            raise ServiceError(r.message, r.status)

    def confirm(self, order_id: str, username: str):
        """装上新电池后复核固定和可用电量。"""
        with self.db.transaction() as conn:
            self._user(conn, username)
            o = self._get_order(conn, order_id)
            if o["status"] != "待复核" or not o["new_battery_id"]:
                raise ServiceError("当前状态为「%s」，无法复核" % o["status"])
            b = conn.execute("SELECT * FROM batteries WHERE id=?",
                             (o["new_battery_id"],)).fetchone()
            # 复核时再次确认固定在位（绑定仍在）且电量可用
            bond = conn.execute("SELECT * FROM bonds WHERE battery_id=?", (b["id"],)).fetchone()
            if not bond or bond["forklift_id"] != o["forklift_id"]:
                raise ServiceError("复核失败：新电池未固定在该叉车上")
            if b["soc"] < 20:
                raise ServiceError("复核失败：可用电量仅 %d%%，不满足出车条件" % b["soc"])
            conn.execute(
                """UPDATE swap_orders SET status='已完成',finished_at=?,check_secured=1,check_soc=?
                   WHERE id=?""",
                (now(), b["soc"], order_id))
            conn.execute("UPDATE forklifts SET status='作业中',updated_at=? WHERE id=?",
                         (now(), o["forklift_id"]))
            self._log(conn, "复核", "%s 复核 %s：固定合格，可用电量 %d%%"
                      % (username, order_id, b["soc"]))

    def cancel(self, order_id: str, username: str):
        with self.db.transaction() as conn:
            self._user(conn, username)
            o = self._get_order(conn, order_id)
            if o["status"] in ("已完成", "已取消"):
                raise ServiceError("工单已结单，不能取消")
            conn.execute("UPDATE swap_orders SET status='已取消',finished_at=? WHERE id=?",
                         (now(), order_id))
            # 预约阶段取消：叉车恢复作业中；已在换电中则保持停用提示
            if o["status"] == "已预约":
                conn.execute("UPDATE forklifts SET status='作业中',updated_at=? WHERE id=?",
                             (now(), o["forklift_id"]))
            else:
                conn.execute("UPDATE forklifts SET status='停用',updated_at=? WHERE id=?",
                             (now(), o["forklift_id"]))
            self._log(conn, "取消", "%s 取消换电单 %s" % (username, order_id))

    @staticmethod
    def _get_order(conn, order_id: str) -> sqlite3.Row:
        o = conn.execute("SELECT * FROM swap_orders WHERE id=?", (order_id,)).fetchone()
        if not o:
            raise ServiceError("换电单不存在")
        return o

    # ---------- 健康隔离（仅维修人员） ----------
    def health_check(self, battery_id: str, username: str, result: str,
                     reason: str = "", soh=None, photo_path=None):
        """登记健康检测并给出隔离/合格结论。仅维修人员可操作。"""
        with self.db.transaction() as conn:
            u = self._user(conn, username)
            if u["role"] != "maintainer":
                raise ServiceError("健康检测结论仅维修人员可修改（%s 无权）" % username, 403)
            b = conn.execute("SELECT * FROM batteries WHERE id=?", (battery_id,)).fetchone()
            if not b:
                raise ServiceError("电池不存在")
            if result not in ("合格", "隔离"):
                raise ServiceError("结论只能是 合格/隔离")
            new_soh = b["soh"] if soh is None else max(0, min(100, int(soh)))
            conn.execute(
                """INSERT INTO health_checks(battery_id,soh,temperature,result,reason,inspector,
                                             photo_path,created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (battery_id, new_soh, b["temperature"], result, reason, username,
                 photo_path, now()))
            if result == "隔离":
                # 槽位上：中断充电并退出槽位；车上：要求先换电卸下
                bond = conn.execute("SELECT * FROM bonds WHERE battery_id=?",
                                    (battery_id,)).fetchone()
                if bond and bond["forklift_id"]:
                    raise ServiceError("电池仍装在 %s 上，请先安排换电卸下后再隔离"
                                       % bond["forklift_id"])
                if bond and bond["slot_id"]:
                    self._interrupt_charging(conn, battery_id, "健康隔离")
                conn.execute(
                    """UPDATE batteries SET health_status='隔离',isolate_reason=?,isolated_by=?,
                                             isolated_at=?,soh=?,updated_at=? WHERE id=?""",
                    (reason or "维修检测不合格", username, now(), new_soh, now(), battery_id))
                self._log(conn, "隔离", "%s 判定 %s 隔离（%s，SOH %d%%）"
                          % (username, battery_id, reason or "检测不合格", new_soh))
            else:
                conn.execute(
                    """UPDATE batteries SET health_status='正常',isolate_reason=NULL,
                                             isolated_by=NULL,isolated_at=NULL,soh=?,updated_at=?
                       WHERE id=?""",
                    (new_soh, now(), battery_id))
                self._log(conn, "复检", "%s 判定 %s 合格（SOH %d%%）"
                          % (username, battery_id, new_soh))

    def reset_overheat(self, battery_id: str, username: str):
        """维修人员确认降温后复位过热/中断标记。"""
        with self.db.transaction() as conn:
            u = self._user(conn, username)
            if u["role"] != "maintainer":
                raise ServiceError("过热复位仅维修人员可操作", 403)
            b = conn.execute("SELECT * FROM batteries WHERE id=?", (battery_id,)).fetchone()
            if not b:
                raise ServiceError("电池不存在")
            if b["temperature"] >= TEMP_RESET:
                raise ServiceError("温度仍为 %.1f℃，降至 %.0f℃ 以下才能复位"
                                   % (b["temperature"], TEMP_RESET))
            conn.execute(
                "UPDATE batteries SET overheating=0,charge_interrupted=0,updated_at=? WHERE id=?",
                (now(), battery_id))
            self._log(conn, "复位", "%s 复位 %s 过热/中断标记" % (username, battery_id))

    def set_slot_fault(self, slot_id: str, username: str, fault: bool, reason=""):
        """维修人员隔离故障槽位 / 修复复用。"""
        with self.db.transaction() as conn:
            u = self._user(conn, username)
            if u["role"] != "maintainer":
                raise ServiceError("槽位故障处置仅维修人员可操作", 403)
            s = conn.execute("SELECT * FROM slots WHERE id=?", (slot_id,)).fetchone()
            if not s:
                raise ServiceError("槽位不存在")
            if fault:
                bond = conn.execute("SELECT * FROM bonds WHERE slot_id=?", (slot_id,)).fetchone()
                if bond:
                    self._interrupt_charging(conn, bond["battery_id"], "槽位故障：" + reason)
                conn.execute("UPDATE slots SET fault=1,status='故障',updated_at=? WHERE id=?",
                             (now(), slot_id))
                self._log(conn, "槽位", "%s 将 %s 置为故障%s"
                          % (username, slot_id, ("：" + reason) if reason else ""))
            else:
                conn.execute("UPDATE slots SET fault=0,status='空闲',updated_at=? WHERE id=?",
                             (now(), slot_id))
                self._log(conn, "槽位", "%s 修复 %s 恢复空闲" % (username, slot_id))

    def _interrupt_charging(self, conn, battery_id: str, reason: str):
        """充电中断：结单充电记录、电池打上中断标记、释放槽位。"""
        log = conn.execute(
            "SELECT * FROM charge_logs WHERE battery_id=? AND status='充电中' ORDER BY id DESC LIMIT 1",
            (battery_id,)).fetchone()
        b = conn.execute("SELECT * FROM batteries WHERE id=?", (battery_id,)).fetchone()
        if log:
            conn.execute(
                """UPDATE charge_logs SET status='已中断',ended_at=?,end_soc=?,interrupt_reason=?,
                       energy_kwh=ROUND((?-?)*0.08,2) WHERE id=?""",
                (now(), b["soc"], reason, b["soc"], log["start_soc"], log["id"]))
        conn.execute("UPDATE batteries SET charge_interrupted=1,updated_at=? WHERE id=?",
                     (now(), battery_id))
        conn.execute("DELETE FROM bonds WHERE battery_id=?", (battery_id,))
        if log:
            conn.execute("UPDATE slots SET status='空闲',updated_at=? WHERE id=?",
                         (now(), log["slot_id"]))
        self._log(conn, "中断", "%s 充电中断（%s，电量 %d%%）" % (battery_id, reason, b["soc"]))


def eta_minutes(soc: int, rate_per_min: float = 2.0) -> int:
    """按模拟适配器约 2%/分钟 的速率估算充满时间。"""
    return max(0, math.ceil((100 - soc) / rate_per_min))
