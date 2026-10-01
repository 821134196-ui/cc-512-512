"""初始化数据库与演示数据（含过热样例、故障槽位、低健康度隔离电池）。"""
import os
import time
import struct
import zlib

import db

PHOTO_DIR = db.PHOTO_DIR


def _png(path, color):
    """生成一张纯色占位 PNG 作为检测照片（无需第三方库）。"""
    w = h = 64
    raw = b""
    row = bytes(color) * w
    for _ in range(h):
        raw += b"\x00" + row

    def chunk(tag, data):
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xffffffff)

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) +
              chunk(b"IEND", b""))


def seed(reset=False):
    if reset and os.path.exists(db.DB_PATH):
        os.remove(db.DB_PATH)
    os.makedirs(os.path.dirname(db.DB_PATH), exist_ok=True)
    os.makedirs(PHOTO_DIR, exist_ok=True)

    # 检测照片占位图
    photos = {
        "HC-B07.png": (198, 40, 40),    # 鼓包-红
        "HC-B04.png": (242, 159, 39),   # 过热复检-橙
        "HC-B01.png": (42, 157, 94),    # 合格-绿
    }
    for name, color in photos.items():
        p = os.path.join(PHOTO_DIR, name)
        if not os.path.exists(p):
            _png(p, color)

    conn = db.get_conn()
    db.init_db(conn)
    if conn.execute("SELECT COUNT(*) c FROM batteries").fetchone()["c"]:
        conn.close()
        return False  # 已有数据，不重复播种

    now = time.time()
    # ---- 电池 ----
    batteries = [
        # code, model, soc, temp, health, status, reasons, isolated
        ("BAT-A01", "P30", 92, 31.0, 96, "loose", "", 0),
        ("BAT-A02", "P30", 47, 33.5, 91, "on_vehicle", "", 0),
        ("BAT-A03", "P30", 88, 52.4, 84, "charging", "", 0),   # ★ 过热样例：上架后迅速爬升
        ("BAT-A04", "P30", 35, 30.2, 72, "isolated", db.BLOCK_LOW_HEALTH, 1),  # 健康度过低
        ("BAT-B01", "P45", 95, 29.8, 98, "on_vehicle", "", 0),
        ("BAT-B02", "P45", 66, 34.0, 90, "loose", db.BLOCK_INTERRUPTED, 0),    # 充电中断
        ("BAT-B03", "P45", 22, 31.5, 89, "on_vehicle", "", 0),
        ("BAT-B04", "P45", 81, 57.2, 78, "isolated",
         f"{db.BLOCK_OVERHEAT},{db.BLOCK_LOW_HEALTH}", 1),                      # 已过热隔离
        ("BAT-B05", "P45", 70, 30.0, 88, "loose", db.BLOCK_WRONG_INSTALL, 0),  # 错装痕迹待核
        ("BAT-B06", "P45", 97, 29.0, 95, "loose", "", 0),
        ("BAT-C01", "P60", 100, 28.5, 99, "loose", "", 0),
        ("BAT-C02", "P60", 58, 32.1, 93, "on_vehicle", db.BLOCK_SLOT_FAULT, 0),  # 槽位故障下撤
    ]
    for code, model, soc, temp, health, status, reasons, iso in batteries:
        conn.execute(
            "INSERT INTO batteries(code,model,soc,temperature,health_score,status,"
            "block_reasons,isolated,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (code, model, soc, temp, health, status, reasons, iso, now))

    # ---- 叉车 ----
    vehicles = [
        ("FL-01", "1.5t平衡重", "P30", "张师傅", "working", "BAT-A02"),
        ("FL-02", "1.5t平衡重", "P30", "李师傅", "idle", None),
        ("FL-03", "2.5t前移式", "P45", "王师傅", "working", "BAT-B01"),
        ("FL-04", "2.5t前移式", "P45", "赵师傅", "awaiting_swap", "BAT-B03"),
        ("FL-05", "3.5t重型", "P60", "陈师傅", "working", "BAT-C02"),
    ]
    for code, model, bmodel, driver, state, batt in vehicles:
        conn.execute(
            "INSERT INTO vehicles(code,model,battery_model,driver,work_state,"
            "installed_battery_code) VALUES(?,?,?,?,?,?)",
            (code, model, bmodel, driver, state, batt))

    # ---- 充电槽位 ----
    slots = [
        ("SLOT-1", "CP-EAST-1", 1, ""),
        ("SLOT-2", "CP-EAST-1", 1, ""),
        ("SLOT-3", "CP-WEST-2", 1, ""),
        ("SLOT-4", "CP-WEST-2", 0, "通信模块故障，等待备件"),
    ]
    for code, cp, ok, reason in slots:
        conn.execute("INSERT INTO slots(code,charger_id,serviceable,fault_reason) VALUES(?,?,?,?)",
                     (code, cp, ok, reason))

    # ---- 当前互斥绑定 ----
    conn.executemany(
        "INSERT INTO active_bindings(battery_code,kind,target_code,since,operator) "
        "VALUES(?,?,?,?,?)", [
            ("BAT-A02", "vehicle", "FL-01", now - 3 * 3600, "张师傅"),
            ("BAT-B01", "vehicle", "FL-03", now - 5 * 3600, "王师傅"),
            ("BAT-B03", "vehicle", "FL-04", now - 2 * 3600, "赵师傅"),
            ("BAT-C02", "vehicle", "FL-05", now - 4 * 3600, "陈师傅"),
            ("BAT-A03", "slot", "SLOT-1", now - 600, "李师傅"),
        ])

    # ---- 充电记录 ----
    eta_a03 = now + (100 - 88) / db.CHARGE_RATE_PER_SEC
    conn.execute(
        "INSERT INTO charge_records(battery_code,slot_code,operator,started_at,start_soc,"
        "eta,status) VALUES(?,?,?,?,?,?,'charging')",
        ("BAT-A03", "SLOT-1", "李师傅", now - 600, 40, eta_a03))
    conn.execute(
        "INSERT INTO charge_records(battery_code,slot_code,operator,started_at,ended_at,"
        "start_soc,end_soc,status,reason) VALUES(?,?,?,?,?,?,?,'interrupted',?)",
        ("BAT-B02", "SLOT-2", "王师傅", now - 7200, now - 7000, 66, 66,
         db.BLOCK_INTERRUPTED))
    conn.execute(
        "INSERT INTO charge_records(battery_code,slot_code,operator,started_at,ended_at,"
        "start_soc,end_soc,status,reason) VALUES(?,?,?,?,?,?,?,'interrupted',?)",
        ("BAT-C02", "SLOT-4", "陈师傅", now - 26 * 3600, now - 25 * 3600, 12, 58,
         db.BLOCK_SLOT_FAULT))

    # ---- 换电队列（FL-04 已预约，作业间隙 10:30-10:50） ----
    conn.execute(
        "INSERT INTO swap_queue(vehicle_code,window,requested_by,requested_at,status) "
        "VALUES(?,?,?,?,'pending')",
        ("FL-04", "10:30-10:50", "赵师傅", now - 900))

    # ---- 历史健康检测结论（不可修改） ----
    conn.execute(
        "INSERT INTO health_checks(battery_code,inspector,result,health_score,reasons,"
        "photo,note,checked_at) VALUES(?,?,?,?,?,?,?,?)",
        ("BAT-B04", "维修-周工", "quarantine", 78,
         f"{db.BLOCK_OVERHEAT},{db.BLOCK_LOW_HEALTH}", "HC-B04.png",
         "充电中外壳烫手，红外测温57℃；电芯一致性差，禁止入池", now - 86400))
    conn.execute(
        "INSERT INTO health_checks(battery_code,inspector,result,health_score,reasons,"
        "photo,note,checked_at) VALUES(?,?,?,?,?,?,?,?)",
        ("BAT-A04", "维修-周工", "quarantine", 72, db.BLOCK_LOW_HEALTH, "HC-B07.png",
         "容量衰减明显，SOH 72%，安排返厂", now - 2 * 86400))
    conn.execute(
        "INSERT INTO health_checks(battery_code,inspector,result,health_score,reasons,"
        "photo,note,checked_at) VALUES(?,?,?,?,?,?,?,?)",
        ("BAT-A01", "维修-周工", "pass", 96, "", "HC-B01.png", "月度巡检合格", now - 7 * 86400))

    conn.execute(
        "INSERT INTO audit_log(ts,actor,role,action,detail) VALUES(?,?,?,?,?)",
        (now, "system", "system", "seed", "演示数据初始化（含过热样例 BAT-A03/BAT-B04）"))
    conn.close()
    return True


if __name__ == "__main__":
    created = seed(reset="--reset" in os.sys.argv)
    print("已重新播种演示数据" if created else "数据库已有数据，跳过（--reset 可重建）")
