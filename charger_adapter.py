"""模拟充电桩信号适配器。

后台线程按周期产生信号：
- 充电中电池电量上升，充满后自动解绑并离开槽位（清除“充电中断/槽位故障”阻断）；
- 温度漂移：临近过热阈值的电池升温加快，越过阈值立即中断并打上“过热”阻断；
- 以小概率产生随机充电中断事件；
- 槽位被维修人员标记故障时，下一拍信号停止。

真实部署时替换 SignalAdapter 即可，db 层不变。
"""
import os
import time
import random
import threading

import db

TICK = float(os.environ.get("SWAP_TICK", "1.0"))
# 每拍每个充电电池发生随机中断的概率（0 关闭）
INTERRUPT_RATE = float(os.environ.get("SWAP_INTERRUPT_RATE", "0.004"))
# 温度漂移参数
AMBIENT = 28.0
HEAT = 11.0                     # 充电时相对环境的稳态温升
OVERHEAT_RISE = 0.9             # 已逼近阈值时每拍升温


class ChargerAdapter:
    def __init__(self, conn_factory=db.get_conn):
        self.conn_factory = conn_factory
        self._stop = threading.Event()
        self._thread = None
        # 每块电池的温度游走状态（与真实信号解耦的演示用内部量）
        self._temp_phase = {}

    # ---- 生命周期 ----
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="charger-adapter", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    def _run(self):
        while not self._stop.wait(TICK):
            try:
                self.tick()
            except Exception as e:  # 适配器不能拖垮主进程
                print(f"[charger-adapter] tick error: {e}", flush=True)

    # ---- 单拍信号 ----
    def tick(self):
        conn = self.conn_factory()
        try:
            rows = conn.execute(
                "SELECT b.*, ab.target_code AS slot_code FROM batteries b "
                "JOIN active_bindings ab ON ab.battery_code=b.code AND ab.kind='slot'"
            ).fetchall()
            for r in rows:
                self._signal_one(conn, r)
        finally:
            conn.close()

    def _signal_one(self, conn, r):
        slot = conn.execute("SELECT * FROM slots WHERE code=?", (r["slot_code"],)).fetchone()
        if not slot or not slot["serviceable"]:
            return  # 槽位故障：充电桩无信号

        code = r["code"]
        temp = self._next_temperature(code, r["temperature"], r["soc"])

        # 1) 过热：立即中断，永不入池（等待维修复检）
        if temp >= db.OVERHEAT_TEMP:
            with db.transaction(conn):
                now = time.time()
                conn.execute("UPDATE batteries SET temperature=?,updated_at=? WHERE code=?",
                             (temp, now, code))
            db.interrupt_charge(conn, code, db.BLOCK_OVERHEAT)
            # interrupt_charge 默认打“充电中断”，过热需换成过热原因
            with db.transaction(conn):
                b = conn.execute("SELECT * FROM batteries WHERE code=?", (code,)).fetchone()
                reasons = db._reasons(b)
                reasons.discard(db.BLOCK_INTERRUPTED)
                reasons.add(db.BLOCK_OVERHEAT)
                conn.execute("UPDATE batteries SET block_reasons=?,isolated=1,status='isolated',"
                             "updated_at=? WHERE code=?",
                             (",".join(sorted(reasons)), time.time(), code))
            print(f"[charger-adapter] !! {code} 过热 {temp:.1f}℃，已中断并隔离", flush=True)
            return

        # 2) 随机充电中断（模拟信号丢失/保护跳闸）
        if INTERRUPT_RATE > 0 and random.random() < INTERRUPT_RATE:
            with db.transaction(conn):
                conn.execute("UPDATE batteries SET temperature=? WHERE code=?", (temp, code))
            db.interrupt_charge(conn, code, db.BLOCK_INTERRUPTED)
            print(f"[charger-adapter] {code} 充电中断（随机事件）", flush=True)
            return

        # 3) 正常充电信号：电量上升
        soc = min(100, r["soc"] + max(1, round(db.CHARGE_RATE_PER_SEC * TICK)))
        with db.transaction(conn):
            now = time.time()
            conn.execute("UPDATE batteries SET soc=?,temperature=?,updated_at=? WHERE code=?",
                         (soc, round(temp, 1), now, code))
            if soc >= 100:
                rec = conn.execute(
                    "SELECT * FROM charge_records WHERE battery_code=? AND status='charging' "
                    "ORDER BY id DESC LIMIT 1", (code,)).fetchone()
                if rec:
                    conn.execute(
                        "UPDATE charge_records SET status='completed',ended_at=?,end_soc=100 "
                        "WHERE id=?", (now, rec["id"]))
                conn.execute("DELETE FROM active_bindings WHERE battery_code=?", (code,))
                b = conn.execute("SELECT * FROM batteries WHERE code=?", (code,)).fetchone()
                reasons = db._reasons(b)
                # 一次完整且正常的充电消除中断类阻断；健康/过热/错装阻断保留
                reasons.discard(db.BLOCK_INTERRUPTED)
                reasons.discard(db.BLOCK_SLOT_FAULT)
                conn.execute(
                    "UPDATE batteries SET status='loose',block_reasons=?,updated_at=? WHERE code=?",
                    (",".join(sorted(reasons)), now, code))
                print(f"[charger-adapter] {code} 充满，离开 {r['slot_code']} 进入可用池",
                      flush=True)

    def _next_temperature(self, code, cur_temp, soc):
        """温度信号：常规充电在环境温度附近游走；逼近阈值则快速爬升（过热样例）。"""
        if cur_temp >= db.OVERHEAT_TEMP - 4.0:
            return cur_temp + OVERHEAT_RISE
        target = AMBIENT + HEAT * (1.0 - soc / 100.0) * 0.6
        phase = self._temp_phase.setdefault(code, random.random() * 6.28)
        self._temp_phase[code] = phase + 0.35
        wobble = 1.6 * (0.5 + 0.5 * _sin(phase))
        drift = (target + wobble - cur_temp) * 0.08
        return max(AMBIENT, cur_temp + drift + random.uniform(-0.12, 0.12))


import math  # noqa: E402  （小工具，保持文件自包含）
def _sin(x):
    return math.sin(x)
