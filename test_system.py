"""系统测试：互斥绑定、兼容型号、故障隔离、并发扫码（HTTP 层）。

运行：python3 test_system.py
使用临时 SQLite 库与临时检测照片目录，不影响 data/ 下演示数据。
"""
import os
import tempfile
import threading
import time
import unittest
import urllib.request
import urllib.error
import json
from http.server import ThreadingHTTPServer

# 在导入业务模块前固定适配器参数：快节拍、关闭随机中断，保证用例确定性
os.environ["SWAP_TICK"] = "0.02"
os.environ["SWAP_INTERRUPT_RATE"] = "0"

import db
import seed
import app as app_module
from charger_adapter import ChargerAdapter


def fresh_db():
    tmp = tempfile.mkdtemp(prefix="swap-test-")
    db.DB_PATH = os.path.join(tmp, "test.db")
    db.PHOTO_DIR = os.path.join(tmp, "photos")
    seed.seed(reset=True)
    return db.get_conn()


def post(url, payload):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def get(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read())


class MutexBindingTests(unittest.TestCase):
    def setUp(self):
        self.conn = fresh_db()

    def tearDown(self):
        self.conn.close()

    def test_one_battery_cannot_bind_two_slots(self):
        """同一块电池并发上两个槽位：只允许一个成功（电池侧互斥）。"""
        results = []
        barrier = threading.Barrier(2)

        def charge(slot):
            barrier.wait()
            c = db.get_conn()
            try:
                db.start_charge(c, "BAT-A01", slot, "换电人甲")
                results.append(("ok", slot))
            except db.Conflict as e:
                results.append(("conflict", str(e)))
            finally:
                c.close()

        t1 = threading.Thread(target=charge, args=("SLOT-2",))
        t2 = threading.Thread(target=charge, args=("SLOT-3",))
        t1.start(); t2.start(); t1.join(); t2.join()

        oks = [r for r in results if r[0] == "ok"]
        self.assertEqual(len(oks), 1, f"应只有一个绑定成功，实际：{results}")
        binds = self.conn.execute(
            "SELECT * FROM active_bindings WHERE battery_code='BAT-A01'").fetchall()
        self.assertEqual(len(binds), 1)
        self.assertEqual(binds[0]["kind"], "slot")
        self.assertIn(binds[0]["target_code"], ("SLOT-2", "SLOT-3"))

    def test_one_slot_cannot_hold_two_batteries(self):
        """同一槽位并发放入两块电池：只允许一个成功（槽位侧唯一约束）。"""
        results = []
        barrier = threading.Barrier(2)

        def charge(batt):
            barrier.wait()
            c = db.get_conn()
            try:
                db.start_charge(c, batt, "SLOT-2", "换电人乙")
                results.append("ok")
            except db.Conflict:
                results.append("conflict")
            finally:
                c.close()

        t1 = threading.Thread(target=charge, args=("BAT-A01",))
        t2 = threading.Thread(target=charge, args=("BAT-B06",))
        t1.start(); t2.start(); t1.join(); t2.join()

        self.assertEqual(results.count("ok"), 1)
        n = self.conn.execute(
            "SELECT COUNT(*) c FROM active_bindings WHERE kind='slot' AND target_code='SLOT-2'"
        ).fetchone()["c"]
        self.assertEqual(n, 1)

    def test_battery_bound_to_vehicle_cannot_charge(self):
        """一车一槽互斥：在车上的电池不能再连槽位。"""
        with self.assertRaises(db.Conflict):
            db.start_charge(self.conn, "BAT-B01", "SLOT-2", "换电人")


class CompatibleModelTests(unittest.TestCase):
    def setUp(self):
        self.conn = fresh_db()

    def tearDown(self):
        self.conn.close()

    def test_wrong_model_rejected_and_right_model_accepted(self):
        """型号不兼容拒绝装入（错装防线），兼容型号可正常完成换电。"""
        qid = db.enqueue(self.conn, "FL-03", "10:30-10:50", "调度员")
        db.scan_old_battery(self.conn, qid, "换电人丙")  # 取下 BAT-B01(P45)

        # 尝试装 P30 给需要 P45 的 FL-03
        with self.assertRaises(db.Conflict) as cm:
            db.install_new_battery(self.conn, qid, "BAT-A01", "换电人丙")
        self.assertIn("型号不兼容", str(cm.exception))

        # 拒绝后不能留下任何绑定
        v = self.conn.execute("SELECT * FROM vehicles WHERE code='FL-03'").fetchone()
        self.assertIsNone(v["installed_battery_code"])
        n = self.conn.execute(
            "SELECT COUNT(*) c FROM active_bindings WHERE battery_code='BAT-A01'").fetchone()["c"]
        self.assertEqual(n, 0)

        # 装上兼容的 P45 并复核
        db.install_new_battery(self.conn, qid, "BAT-B06", "换电人丙")
        db.complete_swap(self.conn, qid, True, "换电人丙")
        v = self.conn.execute("SELECT * FROM vehicles WHERE code='FL-03'").fetchone()
        self.assertEqual(v["installed_battery_code"], "BAT-B06")
        b = self.conn.execute("SELECT * FROM batteries WHERE code='BAT-B06'").fetchone()
        self.assertEqual(b["status"], "on_vehicle")

    def test_blocked_batteries_not_in_usable_pool(self):
        """过热/中断/低健康/错装电池都不能进入可用池。"""
        pool = {b["code"]: b for b in db.list_batteries(self.conn, usable_only=True)}
        for blocked in ("BAT-A04", "BAT-B02", "BAT-B04", "BAT-B05", "BAT-C02"):
            self.assertNotIn(blocked, pool, f"{blocked} 不应进入可用池")
        for ok_code in ("BAT-A01", "BAT-B06", "BAT-C01"):
            self.assertIn(ok_code, pool)

        b04 = next(b for b in db.list_batteries(self.conn) if b["code"] == "BAT-B04")
        self.assertTrue(any("过热" in t for t in b04["block_labels"]))
        self.assertGreaterEqual(b04["temperature"], db.OVERHEAT_TEMP)

    def test_fixation_and_soc_must_be_confirmed(self):
        """固定未复核不能交车；低电量新电池不能通过复核。"""
        qid = db.enqueue(self.conn, "FL-01", "11:00-11:20", "调度员")
        db.scan_old_battery(self.conn, qid, "换电人")
        db.install_new_battery(self.conn, qid, "BAT-A01", "换电人")
        with self.assertRaises(db.Conflict):
            db.complete_swap(self.conn, qid, False, "换电人")
        db.complete_swap(self.conn, qid, True, "换电人")  # 92% ≥ 60%，通过


class FaultIsolationTests(unittest.TestCase):
    def setUp(self):
        self.conn = fresh_db()

    def tearDown(self):
        self.conn.close()

    def test_slot_fault_interrupts_charge_and_blocks_battery(self):
        """槽位故障：在充电池立即中断、解绑并被挡在可用池外。"""
        db.set_slot_state(self.conn, "SLOT-1", False, "通信模块故障",
                          "维修-周工", "maintenance")
        b = self.conn.execute("SELECT * FROM batteries WHERE code='BAT-A03'").fetchone()
        self.assertEqual(b["status"], "loose")
        reasons = b["block_reasons"]
        self.assertIn(db.BLOCK_SLOT_FAULT, reasons)
        self.assertIn(db.BLOCK_INTERRUPTED, reasons)
        bind = self.conn.execute(
            "SELECT 1 FROM active_bindings WHERE battery_code='BAT-A03'").fetchone()
        self.assertIsNone(bind)
        rec = self.conn.execute(
            "SELECT * FROM charge_records WHERE battery_code='BAT-A03' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(rec["status"], "interrupted")
        self.assertEqual(rec["reason"], db.BLOCK_SLOT_FAULT)
        usable = {x["code"] for x in db.list_batteries(self.conn, usable_only=True)}
        self.assertNotIn("BAT-A03", usable)

        # 故障槽位拒绝新充电
        with self.assertRaises(db.Conflict):
            db.start_charge(self.conn, "BAT-A01", "SLOT-4", "换电人")

    def test_only_maintenance_can_isolate_and_conclusion_immutable(self):
        """维修负责隔离；调度员不能修改健康检测结论；结论只增不改。"""
        import sqlite3
        # 调度员无权隔离
        with self.assertRaises(db.Forbidden):
            db.health_check(self.conn, "BAT-B06", "quarantine", 70,
                            db.BLOCK_LOW_HEALTH, "", "调度员尝试", "调度员", "dispatcher")
        # 未带角色同样被拒
        with self.assertRaises(db.Forbidden):
            db.health_check(self.conn, "BAT-B06", "quarantine", 70,
                            db.BLOCK_LOW_HEALTH, "", "", "某人", "")

        # 维修人员隔离
        db.health_check(self.conn, "BAT-B06", "quarantine", 74,
                        db.BLOCK_LOW_HEALTH, "HC-B07.png", "SOH 衰减",
                        "维修-周工", "maintenance")
        b = self.conn.execute("SELECT * FROM batteries WHERE code='BAT-B06'").fetchone()
        self.assertTrue(b["isolated"])
        self.assertEqual(b["status"], "isolated")
        self.assertNotIn("BAT-B06",
                         {x["code"] for x in db.list_batteries(self.conn, usable_only=True)})

        # 数据库触发器：结论不可 UPDATE / DELETE（触发器抛 IntegrityError）
        with self.assertRaises(sqlite3.DatabaseError):
            self.conn.execute("UPDATE health_checks SET result='pass' WHERE battery_code='BAT-B06'")
        with self.assertRaises(sqlite3.DatabaseError):
            self.conn.execute("DELETE FROM health_checks WHERE battery_code='BAT-B04'")

        # 复检合格可解除隔离
        db.health_check(self.conn, "BAT-B06", "pass", 92, "", "HC-B01.png",
                        "复检容量恢复", "维修-周工", "maintenance")
        b = self.conn.execute("SELECT * FROM batteries WHERE code='BAT-B06'").fetchone()
        self.assertFalse(b["isolated"])
        self.assertIn("BAT-B06",
                      {x["code"] for x in db.list_batteries(self.conn, usable_only=True)})
        # 历史两条结论都仍在（只增不改）
        n = self.conn.execute(
            "SELECT COUNT(*) c FROM health_checks WHERE battery_code='BAT-B06'").fetchone()["c"]
        self.assertEqual(n, 2)


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self._tmp_conn = fresh_db()

    def tearDown(self):
        self._tmp_conn.close()

    def test_overheat_sample_gets_isolated(self):
        """模拟适配器：过热样例 BAT-A03 升温越限后自动中断并隔离。"""
        adapter = ChargerAdapter()
        adapter.start()
        try:
            deadline = time.time() + 10
            while time.time() < deadline:
                c = db.get_conn()
                b = c.execute("SELECT * FROM batteries WHERE code='BAT-A03'").fetchone()
                over = b["temperature"] >= db.OVERHEAT_TEMP
                c.close()
                if over and b["isolated"]:
                    break
                time.sleep(0.05)
            else:
                self.fail("BAT-A03 未在预期时间内被适配器判为过热隔离")
            c = db.get_conn()
            b = c.execute("SELECT * FROM batteries WHERE code='BAT-A03'").fetchone()
            self.assertEqual(b["status"], "isolated")
            self.assertIn(db.BLOCK_OVERHEAT, b["block_reasons"])
            self.assertIsNone(c.execute(
                "SELECT 1 FROM active_bindings WHERE battery_code='BAT-A03'").fetchone())
            c.close()
        finally:
            adapter.stop()


class HttpConcurrencyTests(unittest.TestCase):
    """通过真实 HTTP 服务验证两人并发扫码/抢装。"""
    @classmethod
    def setUpClass(cls):
        fresh_db()  # 重定向 db.DB_PATH 到临时库
        cls.adapter = ChargerAdapter()
        cls.adapter.start()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), app_module.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.adapter.stop()

    def test_01_concurrent_scan_old_only_one_succeeds(self):
        """两人同时对同一块在车上的电池扫码取下：恰好一人成功。"""
        # 种子队列 id=1：FL-04 / BAT-B03，pending
        qid = 1
        results = []
        barrier = threading.Barrier(2)

        def scan(actor):
            barrier.wait()
            results.append((actor, *post(f"{self.base}/api/swap/scan-old",
                                         {"queue_id": qid, "actor": actor,
                                          "role": "operator"})))

        t1 = threading.Thread(target=scan, args=("换电人-甲",))
        t2 = threading.Thread(target=scan, args=("换电人-乙",))
        t1.start(); t2.start(); t1.join(); t2.join()

        statuses = sorted(r[1] for r in results)
        self.assertEqual(statuses, [200, 409], f"并发扫码结果应为 200+409：{results}")

        c = db.get_conn()
        v = c.execute("SELECT * FROM vehicles WHERE code='FL-04'").fetchone()
        self.assertIsNone(v["installed_battery_code"])
        n = c.execute(
            "SELECT COUNT(*) c FROM active_bindings WHERE battery_code='BAT-B03'").fetchone()["c"]
        self.assertEqual(n, 0)
        ev = c.execute("SELECT * FROM swap_events WHERE queue_id=1 ORDER BY id DESC LIMIT 1"
                       ).fetchone()
        self.assertEqual(ev["old_battery_code"], "BAT-B03")
        self.assertEqual(ev["state"], "old_removed")
        c.close()

    def test_02_concurrent_install_same_battery_only_one_binds(self):
        """两辆叉车并发抢装同一块新电池：只有一个绑定成立。"""
        # 队列1（FL-04, P45）已在上一用例取下旧电池；再为 FL-03 建队列并取下
        _, r = post(f"{self.base}/api/queue",
                    {"vehicle_code": "FL-03", "window": "10:50-11:10",
                     "actor": "调度员", "role": "dispatcher"})
        q2 = r["queue_id"]
        st, _ = post(f"{self.base}/api/swap/scan-old",
                     {"queue_id": q2, "actor": "换电人-甲", "role": "operator"})
        self.assertEqual(st, 200)

        results = []
        barrier = threading.Barrier(2)

        def install(qid, actor):
            barrier.wait()
            results.append(post(f"{self.base}/api/swap/install",
                                {"queue_id": qid, "battery_code": "BAT-B06",
                                 "actor": actor, "role": "operator"})[0])

        t1 = threading.Thread(target=install, args=(1, "换电人-甲"))
        t2 = threading.Thread(target=install, args=(q2, "换电人-乙"))
        t1.start(); t2.start(); t1.join(); t2.join()

        self.assertEqual(sorted(results), [200, 409])
        c = db.get_conn()
        binds = c.execute(
            "SELECT * FROM active_bindings WHERE battery_code='BAT-B06'").fetchall()
        self.assertEqual(len(binds), 1)
        self.assertEqual(binds[0]["kind"], "vehicle")
        self.assertIn(binds[0]["target_code"], ("FL-03", "FL-04"))
        b = c.execute("SELECT * FROM batteries WHERE code='BAT-B06'").fetchone()
        self.assertEqual(b["status"], "on_vehicle")
        c.close()

    def test_03_dispatcher_cannot_change_health_over_http(self):
        """HTTP 层角色隔离：调度员提交健康结论返回 403。"""
        st, r = post(f"{self.base}/api/health/check",
                     {"battery_code": "BAT-A01", "result": "quarantine",
                      "health_score": 10, "actor": "调度员", "role": "dispatcher"})
        self.assertEqual(st, 403)
        self.assertIn("无权", r["error"])

    def test_04_wrong_model_rejected_over_http(self):
        """HTTP 层错装拦截：给 P60 叉车装 P45 电池返回 409。"""
        # FL-05 需要 P60，当前有电池，先预约+扫码
        _, r = post(f"{self.base}/api/queue",
                    {"vehicle_code": "FL-05", "window": "13:00-13:20",
                     "actor": "调度员", "role": "dispatcher"})
        qid = r["queue_id"]
        st, _ = post(f"{self.base}/api/swap/scan-old",
                     {"queue_id": qid, "actor": "换电人", "role": "operator"})
        self.assertEqual(st, 200)
        st, r = post(f"{self.base}/api/swap/install",
                     {"queue_id": qid, "battery_code": "BAT-B06",
                      "actor": "换电人", "role": "operator"})
        self.assertEqual(st, 409)
        self.assertIn("型号不兼容", r["error"])
        # 正确型号可以装（BAT-C01 P60 可用）
        st, _ = post(f"{self.base}/api/swap/install",
                     {"queue_id": qid, "battery_code": "BAT-C01",
                      "actor": "换电人", "role": "operator"})
        self.assertEqual(st, 200)
        st, _ = post(f"{self.base}/api/swap/complete",
                     {"queue_id": qid, "fixation_ok": True,
                      "actor": "换电人", "role": "operator"})
        self.assertEqual(st, 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)
