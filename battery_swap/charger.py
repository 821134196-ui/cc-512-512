# -*- coding: utf-8 -*-
"""模拟充电桩适配器。

真实场景中充电桩信号来自 PLC / 物联网关；这里用后台线程按固定节拍产生
电压、电流、功率与电池温度、电量变化，并演示两种保护动作：

* 充电中温度越过过热阈值 → 置过热标志、充电中断（释放槽位，电池不入可用池）；
* 电量充满 → 结单、释放槽位、电池进入可用池。

``overheat_when`` 给出需要在充电过程中制造过热样例的电池及其触发电量点。
"""
import threading
import time

from .db import now
from .services import TEMP_HOT, eta_minutes  # noqa: F401 (eta_minutes 供界面复用)

TICK_SECONDS = 3
SOC_STEP = 3          # 每拍涨 3%（约 1%/秒，便于演示）
TEMP_STEP = 0.55      # 正常充电温升；由 overheat_when 显式制造过热样例
TEMP_AMBIENT = 28.0


class ChargerAdapter:
    def __init__(self, db, service, overheat_when=None, tick=TICK_SECONDS):
        self.db = db
        self.service = service
        # {"BAT-07": 96} 表示 BAT-07 充到 96% 时触发过热保护
        self.overheat_when = dict(overheat_when or {})
        self.tick = tick
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, name="charger-adapter", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        # 先为在槽电池补建在线信号
        time.sleep(1.0)
        while not self._stop.wait(self.tick):
            try:
                self.pulse()
            except Exception as exc:  # 模拟信号抖动，不能让线程退出
                print("[charger] pulse error: %r" % exc)

    def pulse(self):
        """一拍信号：更新所有在槽充电电池。"""
        with self.db.transaction() as conn:
            tripped = []  # 本拍刚触发过热保护的电池，当拍不参与降温
            rows = conn.execute(
                """SELECT bo.battery_id, bo.slot_id, b.soc, b.temperature, b.overheating,
                          b.charge_interrupted, s.fault AS slot_fault
                   FROM bonds bo
                   JOIN batteries b ON b.id=bo.battery_id
                   JOIN slots s ON s.id=bo.slot_id
                   WHERE bo.slot_id IS NOT NULL""").fetchall()
            for r in rows:
                if r["slot_fault"]:
                    self._signal(conn, r["slot_id"], online=0)
                    continue
                soc = min(100, r["soc"] + SOC_STEP)
                temp = round(r["temperature"] + TEMP_STEP, 1)
                trigger = self.overheat_when.get(r["battery_id"])
                will_overheat = trigger is not None and r["soc"] < trigger <= soc
                if will_overheat or temp >= TEMP_HOT:
                    # 先写本拍信号与温度，再执行过热保护中断
                    conn.execute("UPDATE batteries SET soc=?,temperature=?,overheating=1,updated_at=? WHERE id=?",
                                 (soc, max(temp, TEMP_HOT + 1.5), now(), r["battery_id"]))
                    self._signal(conn, r["slot_id"], online=1, temp=max(temp, TEMP_HOT + 1.5))
                    self.service._interrupt_charging(conn, r["battery_id"],
                                                     "过热保护 %.1f℃" % max(temp, TEMP_HOT + 1.5))
                    self.overheat_when.pop(r["battery_id"], None)
                    tripped.append(r["battery_id"])
                    continue
                conn.execute("UPDATE batteries SET soc=?,temperature=?,updated_at=? WHERE id=?",
                             (soc, temp, now(), r["battery_id"]))
                self._signal(conn, r["slot_id"], online=1, temp=temp, soc=soc)
                if soc >= 100:
                    self._finish(conn, r["battery_id"], r["slot_id"])
            # 过热离槽电池自然降温，方便维修人员降温后复位（本拍刚触发的保持峰值）
            hot = conn.execute(
                "SELECT id,temperature FROM batteries WHERE overheating=1 AND id NOT IN "
                "(SELECT battery_id FROM bonds WHERE slot_id IS NOT NULL)").fetchall()
            for h in hot:
                if h["id"] in tripped:
                    continue
                # 缓慢自然降温：降到复位阈值约需 40 秒，留出维修处置窗口
                t = max(TEMP_AMBIENT, round(h["temperature"] - 0.5, 1))
                conn.execute("UPDATE batteries SET temperature=?,updated_at=? WHERE id=?",
                             (t, now(), h["id"]))
            # 空闲槽上报离线信号（模拟桩在线心跳只在充电时存在）
            for s in conn.execute("SELECT id FROM slots WHERE fault=0").fetchall():
                busy = conn.execute("SELECT 1 FROM bonds WHERE slot_id=? AND battery_id IS NOT NULL",
                                    (s["id"],)).fetchone()
                if not busy:
                    self._signal(conn, s["id"], online=0)

    @staticmethod
    def _signal(conn, slot_id, online, temp=None, soc=None):
        if online:
            # 充电曲线：电压随电量抬升，电流随电量下降
            pct = (soc if soc is not None else 50) / 100.0
            voltage = round(48.0 + 8.0 * pct, 1)
            current = round(120.0 - 70.0 * pct, 1)
            power = round(voltage * current / 1000.0, 2)
            conn.execute(
                """INSERT INTO charger_signals(slot_id,online,voltage,current,power_kw,updated_at)
                   VALUES(?,1,?,?,?,?)
                   ON CONFLICT(slot_id) DO UPDATE SET online=1,voltage=excluded.voltage,
                     current=excluded.current,power_kw=excluded.power_kw,updated_at=excluded.updated_at""",
                (slot_id, voltage, current, power, now()))
        else:
            conn.execute(
                """INSERT INTO charger_signals(slot_id,online,voltage,current,power_kw,updated_at)
                   VALUES(?,0,NULL,NULL,NULL,?)
                   ON CONFLICT(slot_id) DO UPDATE SET online=0,voltage=NULL,current=NULL,
                     power_kw=NULL,updated_at=excluded.updated_at""",
                (slot_id, now()))

    def _finish(self, conn, battery_id, slot_id):
        log = conn.execute(
            "SELECT * FROM charge_logs WHERE battery_id=? AND status='充电中' ORDER BY id DESC LIMIT 1",
            (battery_id,)).fetchone()
        if log:
            conn.execute(
                """UPDATE charge_logs SET status='已充满',ended_at=?,end_soc=100,
                       energy_kwh=ROUND((100-?)*0.08,2) WHERE id=?""",
                (now(), log["start_soc"], log["id"]))
        conn.execute("DELETE FROM bonds WHERE battery_id=?", (battery_id,))
        conn.execute("UPDATE slots SET status='空闲',updated_at=? WHERE id=?", (now(), slot_id))
        conn.execute(
            "UPDATE batteries SET temperature=ROUND(MAX(?,temperature-2),1),updated_at=? WHERE id=?",
            (TEMP_AMBIENT + 4, now(), battery_id))
        conn.execute(
            "INSERT INTO event_log(kind,message,created_at) VALUES('充满',?,?)",
            (battery_id + " 充电完成，已进入可用池", now()))
