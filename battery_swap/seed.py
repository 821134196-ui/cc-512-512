# -*- coding: utf-8 -*-
"""演示数据：用户、叉车、电池、槽位、在充电池与各种不可用样例。

启动后立即可见：
* BAT-07 —— 过热样例（58.2℃ 过热保护、充电中断，等待维修降温复位）；
* BAT-10 —— 健康隔离（鼓包，维修人员李工判定，附检测照片）；
* BAT-11 —— 健康度过低 SOH 65%；
* BAT-12 —— 充电中断未闭环；
* SLOT-05 —— 槽位故障；
* BAT-09 —— 充电过程中将再次触发过热（约半分钟内，由模拟适配器产生）。
"""
import os
import struct
import zlib

from .db import now
from .services import PHOTO_DIR

USERS = [
    ("zhang", "张丽（调度员）", "dispatcher"),
    ("li", "李工（维修）", "maintainer"),
    ("wang", "王强（换电员）", "operator"),
    ("zhao", "赵敏（换电员）", "operator"),
]

# id, 车型, 兼容电池, 当前电池, 状态
FORKLIFTS = [
    ("FK-01", "CP-1T 平衡重", "B-48V", "BAT-01", "作业中"),
    ("FK-02", "CP-2T 平衡重", "B-48V,B-80V", "BAT-02", "等待换电"),
    ("FK-03", "CP-3T 大吨位", "B-80V", "BAT-03", "作业中"),
    ("FK-04", "CP-15 搬运车", "B-24V", "BAT-04", "作业中"),
]

# id, 型号, SOC, 温度, SOH, 过热, 中断, 健康状态, 隔离原因
BATTERIES = [
    ("BAT-01", "B-48V", 90, 29.5, 98, 0, 0, "正常", None),
    ("BAT-02", "B-48V", 38, 41.0, 91, 0, 0, "正常", None),   # FK-02 待换旧电池
    ("BAT-03", "B-80V", 72, 33.2, 96, 0, 0, "正常", None),
    ("BAT-04", "B-24V", 55, 30.1, 89, 0, 0, "正常", None),
    ("BAT-05", "B-48V", 96, 30.4, 95, 0, 0, "正常", None),   # 可用
    ("BAT-06", "B-48V", 88, 29.8, 92, 0, 0, "正常", None),   # 可用
    ("BAT-07", "B-48V", 46, 58.2, 87, 1, 1, "正常", None),   # ★ 过热样例
    ("BAT-08", "B-80V", 63, 36.5, 94, 0, 0, "正常", None),   # 在 SLOT-02 正常充电
    ("BAT-09", "B-24V", 45, 40.0, 90, 0, 0, "正常", None),   # 在 SLOT-03，稍后过热
    ("BAT-10", "B-48V", 77, 31.0, 72, 0, 0, "隔离", "电池外壳鼓包，内阻超标"),
    ("BAT-11", "B-80V", 94, 29.0, 65, 0, 0, "正常", None),   # SOH 过低，不入池
    ("BAT-12", "B-48V", 20, 28.5, 90, 0, 1, "正常", None),   # 充电中断未闭环
    ("BAT-13", "B-24V", 92, 29.2, 97, 0, 0, "正常", None),   # 可用
]

SLOTS = [
    ("SLOT-01", "1 号快充桩", "*", "空闲", 0),
    ("SLOT-02", "2 号快充桩", "*", "充电中", 0),
    ("SLOT-03", "3 号快充桩", "B-24V", "充电中", 0),
    ("SLOT-04", "4 号普通桩", "*", "空闲", 0),
    ("SLOT-05", "5 号普通桩", "*", "故障", 1),             # ★ 槽位故障样例
    ("SLOT-06", "6 号普通桩", "*", "空闲", 0),
]


def _png(path, w, h, draw):
    """无依赖生成一张 PNG：draw(buf, setpx) 逐像素上色。"""
    buf = bytearray(b"\xff" * (w * h * 3))

    def px(x, y, rgb):
        if 0 <= x < w and 0 <= y < h:
            i = (y * w + x) * 3
            buf[i], buf[i + 1], buf[i + 2] = rgb

    def rect(x0, y0, x1, y1, rgb, fill=True):
        for y in range(y0, y1):
            for x in range(x0, x1):
                if fill or x in (x0, x1 - 1) or y in (y0, y1 - 1):
                    px(x, y, rgb)

    draw(w, h, px, rect)
    raw = b"".join(b"\x00" + bytes(buf[y * w * 3:(y + 1) * w * 3]) for y in range(h))

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        f.write(chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)))
        f.write(chunk(b"IDAT", zlib.compress(raw, 9)))
        f.write(chunk(b"IEND", b""))


def _photo_isolation(path):
    def draw(w, h, px, rect):
        rect(0, 0, w, h, (245, 239, 230))           # 工业白纸面
        rect(12, 12, w - 12, h - 12, (90, 90, 90), fill=False)
        rect(70, 35, 130, 115, (60, 60, 60))       # 电池本体
        rect(130, 48, 142, 72, (60, 60, 60))       # 极柱
        # 鼓包裂纹（橙红）
        for i in range(40):
            x = 80 + i
            y = 70 + int(8 * ((i % 7) - 3) / 3)
            rect(x, y, x + 3, y + 3, (214, 92, 32))
        # 告警三角
        for i in range(26):
            for j in range(i + 1):
                px(160 + i, 95 - j, (200, 60, 40))
        rect(169, 80, 175, 90, (255, 255, 255))
        px(172, 93, (255, 255, 255))
    _png(path, 240, 150, draw)


def _photo_ok(path):
    def draw(w, h, px, rect):
        rect(0, 0, w, h, (238, 243, 238))
        rect(12, 12, w - 12, h - 12, (90, 90, 90), fill=False)
        rect(60, 35, 150, 115, (60, 60, 60))
        rect(150, 48, 162, 72, (60, 60, 60))
        rect(70, 45, 140, 68, (46, 156, 92))       # 电量绿格
        # 对勾
        for i in range(14):
            rect(168 + i, 92 - i, 172 + i, 96 - i, (30, 140, 80))
        for i in range(22):
            rect(180 + i, 80 + i // 3, 184 + i, 84 + i // 3, (30, 140, 80))
    _png(path, 240, 150, draw)


def seed(db, force=False):
    os.makedirs(PHOTO_DIR, exist_ok=True)
    conn = db.connect()
    if conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] and not force:
        return False
    conn.executescript("DELETE FROM scan_events;DELETE FROM charge_logs;DELETE FROM health_checks;"
                       "DELETE FROM bonds;DELETE FROM swap_orders;DELETE FROM event_log;"
                       "DELETE FROM charger_signals;DELETE FROM batteries;DELETE FROM slots;"
                       "DELETE FROM forklifts;DELETE FROM users;")
    t = now()
    for u in USERS:
        conn.execute("INSERT INTO users(username,name,role) VALUES(?,?,?)", u)
    for fid, model, acc, bat, st in FORKLIFTS:
        conn.execute(
            "INSERT INTO forklifts(id,model,accepted_models,current_battery_id,status,updated_at) "
            "VALUES(?,?,?,?,?,?)", (fid, model, acc, bat, st, t))
    for bid, model, soc, temp, soh, hot, intr, hs, reason in BATTERIES:
        conn.execute(
            """INSERT INTO batteries(id,model,soc,temperature,soh,overheating,charge_interrupted,
                                     health_status,isolate_reason,isolated_by,isolated_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (bid, model, soc, temp, soh, hot, intr, hs,
             (reason if hs == "隔离" else None),
             ("li" if hs == "隔离" else None),
             (t if hs == "隔离" else None), t))
    for sid, name, acc, st, fault in SLOTS:
        conn.execute("INSERT INTO slots(id,name,accepted_models,status,fault,updated_at) "
                     "VALUES(?,?,?,?,?,?)", (sid, name, acc, st, fault, t))
    # 互斥绑定
    binds = [("BAT-01", "FK-01", None), ("BAT-02", "FK-02", None),
             ("BAT-03", "FK-03", None), ("BAT-04", "FK-04", None),
             ("BAT-08", None, "SLOT-02"), ("BAT-09", None, "SLOT-03")]
    for bid, fk, sl in binds:
        conn.execute("INSERT INTO bonds(battery_id,forklift_id,slot_id,bound_at) VALUES(?,?,?,?)",
                     (bid, fk, sl, t))
    # 一张进行中的预约单
    conn.execute(
        """INSERT INTO swap_orders(id,forklift_id,old_battery_id,status,operator,scheduled_at,note)
           VALUES(?,?,?, '已预约', 'wang', ?,?)""",
        ("SW-DEMO-001", "FK-02", "BAT-02", t, "班中预约，旧电池电量低"))
    # 充电记录：BAT-07 过热中断（样例）、BAT-12 模拟中断、在充两条、历史充满
    logs = [
        ("BAT-07", "SLOT-01", 40, 46, "过热保护 58.2℃", "已中断"),
        ("BAT-12", "SLOT-04", 12, 20, "模拟中断：通讯丢失", "已中断"),
        ("BAT-08", "SLOT-02", 63, None, None, "充电中"),
        ("BAT-09", "SLOT-03", 45, None, None, "充电中"),
        ("BAT-05", "SLOT-06", 12, 100, None, "已充满"),
        ("BAT-06", "SLOT-02", 8, 100, None, "已充满"),
        ("BAT-13", "SLOT-03", 30, 100, None, "已充满"),
    ]
    for bid, sl, ssoc, esoc, reason, stt in logs:
        conn.execute(
            """INSERT INTO charge_logs(battery_id,slot_id,started_at,ended_at,start_soc,end_soc,
                                       energy_kwh,interrupt_reason,status)
               VALUES(?,?,?,?,?,?,CASE WHEN ? IS NULL THEN 0 ELSE ROUND((?-?)*0.08,2) END,?,?)""",
            (bid, sl, t, (t if stt != "充电中" else None), ssoc, esoc,
             esoc, esoc, ssoc, reason, stt))
    # 检测照片
    p10 = os.path.join(PHOTO_DIR, "BAT-10-isolation.png")
    p05 = os.path.join(PHOTO_DIR, "BAT-05-routine.png")
    _photo_isolation(p10)
    _photo_ok(p05)
    conn.execute(
        """INSERT INTO health_checks(battery_id,soh,temperature,result,reason,inspector,
                                     photo_path,created_at)
           VALUES(?,?,?, '隔离', ?, 'li', ?, ?)""",
        ("BAT-10", 72, 31.0, "电池外壳鼓包，内阻超标，禁止入池", p10, t))
    conn.execute(
        """INSERT INTO health_checks(battery_id,soh,temperature,result,reason,inspector,
                                     photo_path,created_at)
           VALUES(?,?,?, '合格', '例检合格', 'li', ?, ?)""",
        ("BAT-05", 95, 30.4, p05, t))
    for kind, msg in [
        ("种子", "演示数据载入完成"),
        ("过热", "BAT-07 在 SLOT-01 充电时过热保护（58.2℃），已中断等待维修处置"),
        ("槽位", "SLOT-05 通讯故障，维修已置为故障隔离"),
    ]:
        conn.execute("INSERT INTO event_log(kind,message,created_at) VALUES(?,?,?)", (kind, msg, t))
    return True
