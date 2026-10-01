# -*- coding: utf-8 -*-
"""测试：互斥绑定、兼容型号、故障隔离、并发扫码、过热保护。

运行：python3 -m unittest -v   或   python3 tests/test_swap.py
"""
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from battery_swap.charger import ChargerAdapter
from battery_swap.db import Database
from battery_swap.seed import seed
from battery_swap.server import make_handler, App
from battery_swap.services import SwapService, ServiceError


def fresh_service(path=":memory:"):
    db = Database(path)
    db.init_schema()
    seed(db)
    return db, SwapService(db)


class BaseCase(unittest.TestCase):
    def setUp(self):
        self.db, self.svc = fresh_service()


class TestMutexBinding(BaseCase):
    """一块电池同一时刻只能连一辆车或一个槽位。"""

    def test_battery_primary_key(self):
        # BAT-05 空闲，可绑车
        with self.db.transaction() as c:
            c.execute("INSERT INTO bonds(battery_id,forklift_id,slot_id,bound_at) "
                      "VALUES('BAT-05',NULL,'SLOT-04',?)", ("t",))
        # 再绑一次（换别的槽）→ 主键冲突
        with self.assertRaises(sqlite3.IntegrityError):
            with self.db.transaction() as c:
                c.execute("INSERT INTO bonds(battery_id,forklift_id,slot_id,bound_at) "
                          "VALUES('BAT-05',NULL,'SLOT-06',?)", ("t",))

    def test_slot_unique(self):
        # SLOT-02 已绑 BAT-08，再插一块 → UNIQUE(slot_id) 冲突
        with self.assertRaises(sqlite3.IntegrityError):
            with self.db.transaction() as c:
                c.execute("INSERT INTO bonds(battery_id,forklift_id,slot_id,bound_at) "
                          "VALUES('BAT-06',NULL,'SLOT-02',?)", ("t",))

    def test_forklift_unique(self):
        # FK-01 已绑 BAT-01，再装一块 → UNIQUE(forklift_id) 冲突
        with self.assertRaises(sqlite3.IntegrityError):
            with self.db.transaction() as c:
                c.execute("INSERT INTO bonds(battery_id,forklift_id,slot_id,bound_at) "
                          "VALUES('BAT-06','FK-01',NULL,?)", ("t",))

    def test_check_exactly_one_target(self):
        # 既不指向车也不指向槽、或同时指向两者，CHECK 约束都拒绝
        for sql in [
            "INSERT INTO bonds(battery_id,forklift_id,slot_id,bound_at) VALUES('BAT-06',NULL,NULL,'t')",
            "INSERT INTO bonds(battery_id,forklift_id,slot_id,bound_at) VALUES('BAT-06','FK-01','SLOT-04','t')",
        ]:
            with self.assertRaises(sqlite3.IntegrityError):
                with self.db.transaction() as c:
                    c.execute(sql)

    def test_full_swap_flow_rebinds_once(self):
        oid = "SW-DEMO-001"
        self.svc.start(oid, "wang")
        self.svc.scan_old_remove(oid, "BAT-02", "wang")
        self.svc.scan_slot_in(oid, "SLOT-01", "wang")
        self.svc.scan_new_install(oid, "BAT-05", "wang")
        self.svc.confirm(oid, "wang")
        c = self.db.connect()
        # BAT-02 只在槽上，BAT-05 只在车上，FK-02 只绑一块
        self.assertEqual(c.execute(
            "SELECT slot_id FROM bonds WHERE battery_id='BAT-02'").fetchone()["slot_id"],
            "SLOT-01")
        self.assertEqual(c.execute(
            "SELECT forklift_id FROM bonds WHERE battery_id='BAT-05'").fetchone()["forklift_id"],
            "FK-02")
        self.assertIsNone(c.execute(
            "SELECT slot_id FROM bonds WHERE battery_id='BAT-05'").fetchone()["slot_id"])
        self.assertEqual(c.execute(
            "SELECT current_battery_id,status FROM forklifts WHERE id='FK-02'").fetchone()[0],
            "BAT-05")


class TestCompatibility(BaseCase):
    def _to_install_state(self, oid, old_code, slot):
        self.svc.start(oid, "wang")
        self.svc.scan_old_remove(oid, old_code, "wang")
        self.svc.scan_slot_in(oid, slot, "wang")

    def test_incompatible_battery_to_forklift(self):
        # FK-02 兼容 B-48V/B-80V；BAT-13 是 B-24V → 拒绝安装
        self._to_install_state("SW-DEMO-001", "BAT-02", "SLOT-01")
        with self.assertRaisesRegex(ServiceError, "型号不兼容"):
            self.svc.scan_new_install("SW-DEMO-001", "BAT-13", "wang")
        # 被拒电池仍未绑定 FK-02
        row = self.db.query_one(
            "SELECT forklift_id FROM bonds WHERE battery_id='BAT-13'")
        self.assertIsNone(row)

    def test_slot_model_restriction(self):
        # SLOT-03 仅接受 B-24V：BAT-02(B-48V) 扫入应被拒
        self.svc.start("SW-DEMO-001", "wang")
        self.svc.scan_old_remove("SW-DEMO-001", "BAT-02", "wang")
        with self.assertRaisesRegex(ServiceError, "仅接受"):
            self.svc.scan_slot_in("SW-DEMO-001", "SLOT-03", "wang")

    def test_mismatch_flagged_in_pool(self):
        # 人为构造错装：B-24V 的 BAT-13 装到只接受 B-48V 的 FK-01，池位图必须标错装并拒入池
        with self.db.transaction() as c:
            c.execute("DELETE FROM bonds WHERE battery_id='BAT-01'")
            c.execute("UPDATE forklifts SET current_battery_id=NULL WHERE id='FK-01'")
            c.execute("INSERT INTO bonds(battery_id,forklift_id,slot_id,bound_at) "
                      "VALUES('BAT-13','FK-01',NULL,'t')")
            c.execute("UPDATE forklifts SET current_battery_id='BAT-13' WHERE id='FK-01'")
        b = next(x for x in self.svc.pool() if x["id"] == "BAT-13")
        self.assertTrue(b["mismatch"])
        self.assertFalse(b["available"])
        self.assertTrue(any("错装" in r for r in b["exclude_reasons"]))


class TestIsolation(BaseCase):
    def test_dispatcher_cannot_isolate(self):
        # 调度员不能修改健康检测结论
        with self.assertRaisesRegex(ServiceError, "仅维修人员"):
            self.svc.health_check("BAT-06", "zhang", "隔离", "调度员测试隔离")

    def test_maintainer_isolate_removes_from_pool(self):
        self.svc.health_check("BAT-06", "li", "隔离", "内阻异常")
        b = next(x for x in self.svc.pool() if x["id"] == "BAT-06")
        self.assertEqual(b["health_status"], "隔离")
        self.assertFalse(b["available"])
        self.assertTrue(any("健康隔离" in r for r in b["exclude_reasons"]))

    def test_low_soh_excluded(self):
        b = next(x for x in self.svc.pool() if x["id"] == "BAT-11")
        self.assertLess(b["soh"], 80)
        self.assertFalse(b["available"])
        self.assertTrue(any("健康度过低" in r for r in b["exclude_reasons"]))

    def test_overheated_excluded_and_reset_gated(self):
        b = next(x for x in self.svc.pool() if x["id"] == "BAT-07")
        self.assertTrue(b["overheating"])
        self.assertFalse(b["available"])
        # 非维修不能复位；温度未降下来也不能复位
        with self.assertRaisesRegex(ServiceError, "仅维修人员"):
            self.svc.reset_overheat("BAT-07", "zhang")
        with self.assertRaisesRegex(ServiceError, "温度仍为"):
            self.svc.reset_overheat("BAT-07", "li")
        # 降温后维修复位 → 回到可用池（BAT-07 此时未绑定任何对象）
        with self.db.transaction() as c:
            c.execute("UPDATE batteries SET temperature=42.0 WHERE id='BAT-07'")
        self.svc.reset_overheat("BAT-07", "li")
        b = next(x for x in self.svc.pool() if x["id"] == "BAT-07")
        self.assertTrue(b["available"])

    def test_slot_fault_interrupts_charging_and_blocks(self):
        # 调度员不能处置槽位
        with self.assertRaisesRegex(ServiceError, "仅维修人员"):
            self.svc.set_slot_fault("SLOT-01", "zhang", True)
        # SLOT-02 上 BAT-08 正在充电：置故障 → 中断充电、释放槽位
        self.svc.set_slot_fault("SLOT-02", "li", True, "通讯模块损坏")
        b = next(x for x in self.svc.pool() if x["id"] == "BAT-08")
        self.assertTrue(b["charge_interrupted"])
        self.assertFalse(b["available"])
        self.assertIsNone(self.db.query_one(
            "SELECT 1 FROM bonds WHERE battery_id='BAT-08'"))
        log = self.db.query_one(
            "SELECT * FROM charge_logs WHERE battery_id='BAT-08' ORDER BY id DESC LIMIT 1")
        self.assertEqual(log["status"], "已中断")
        self.assertIn("槽位故障", log["interrupt_reason"])
        s = self.db.query_one("SELECT * FROM slots WHERE id='SLOT-02'")
        self.assertEqual(s["status"], "故障")
        # 故障槽不接受旧电池入槽
        oid = "SW-DEMO-001"
        self.svc.start(oid, "wang")
        self.svc.scan_old_remove(oid, "BAT-02", "wang")
        with self.assertRaisesRegex(ServiceError, "故障状态"):
            self.svc.scan_slot_in(oid, "SLOT-02", "wang")

    def test_charge_interrupted_battery_blocked(self):
        b = next(x for x in self.svc.pool() if x["id"] == "BAT-12")
        self.assertTrue(b["charge_interrupted"])
        self.assertFalse(b["available"])

    def test_isolate_on_forklift_requires_removal(self):
        # 还在车上的电池不能直接隔离，必须先换电卸下
        with self.assertRaisesRegex(ServiceError, "仍装在"):
            self.svc.health_check("BAT-01", "li", "隔离", "在车上")


class TestChargerOverheat(unittest.TestCase):
    """模拟适配器产生过热样例：过热保护中断，且不入可用池。"""

    def setUp(self):
        self.db, self.svc = fresh_service()
        self.adapter = ChargerAdapter(self.db, self.svc, overheat_when={"BAT-09": 72})

    def test_overheat_trip_during_charge(self):
        # BAT-09 起 45%，每拍 +3%，第 9~10 拍越过 72% 触发
        for _ in range(12):
            self.adapter.pulse()
            b = self.db.query_one("SELECT * FROM batteries WHERE id='BAT-09'")
            if b["overheating"]:
                break
        b = self.db.query_one("SELECT * FROM batteries WHERE id='BAT-09'")
        self.assertEqual(b["overheating"], 1)
        self.assertEqual(b["charge_interrupted"], 1)
        self.assertGreaterEqual(b["temperature"], 55.0)
        self.assertIsNone(self.db.query_one(
            "SELECT 1 FROM bonds WHERE battery_id='BAT-09'"))
        log = self.db.query_one(
            "SELECT * FROM charge_logs WHERE battery_id='BAT-09' AND status='已中断'")
        self.assertIsNotNone(log)
        self.assertIn("过热保护", log["interrupt_reason"])
        pool = next(x for x in self.svc.pool() if x["id"] == "BAT-09")
        self.assertFalse(pool["available"])
        self.assertTrue(any("过热" in r for r in pool["exclude_reasons"]))

    def test_normal_charge_finishes_and_enters_pool(self):
        # BAT-08 正常充电直至充满，自动释放槽位并入池
        adapter = ChargerAdapter(self.db, self.svc, overheat_when={})
        for _ in range(20):
            adapter.pulse()
        b = self.db.query_one("SELECT * FROM batteries WHERE id='BAT-08'")
        self.assertEqual(b["soc"], 100)
        self.assertEqual(b["overheating"], 0)
        self.assertIsNone(self.db.query_one(
            "SELECT 1 FROM bonds WHERE battery_id='BAT-08'"))
        pool = next(x for x in self.svc.pool() if x["id"] == "BAT-08")
        self.assertTrue(pool["available"])


class TestConcurrentScan(unittest.TestCase):
    """两人并发换电、扫同一块新电池：事务 + 唯一约束下仅一人成功。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        path = os.path.join(self.tmp.name, "conc.db")
        self.db, self.svc = fresh_service(path)
        # FK-02 的演示单（旧 BAT-02）
        self.oid_a = "SW-DEMO-001"
        # 再为 FK-01 建一单（旧 BAT-01）
        self.oid_b = self.svc.reserve("FK-01", "zhao", "并发测试")
        for oid, old, slot in [(self.oid_a, "BAT-02", "SLOT-01"),
                               (self.oid_b, "BAT-01", "SLOT-04")]:
            self.svc.start(oid, "wang")
            self.svc.scan_old_remove(oid, old, "wang")
            self.svc.scan_slot_in(oid, slot, "wang")

    def tearDown(self):
        self.tmp.cleanup()

    def test_only_one_install_wins(self):
        barrier = threading.Barrier(2)
        results = {}

        def worker(tag, oid, user):
            svc = SwapService(self.db)  # 同库文件，线程各自取连接
            barrier.wait()
            try:
                svc.scan_new_install(oid, "BAT-05", user)
                results[tag] = ("ok", None)
            except ServiceError as e:
                results[tag] = ("reject", str(e))

        t1 = threading.Thread(target=worker, args=("A", self.oid_a, "wang"))
        t2 = threading.Thread(target=worker, args=("B", self.oid_b, "zhao"))
        t1.start(); t2.start(); t1.join(); t2.join()

        outcomes = sorted(results.values())
        self.assertEqual([o[0] for o in outcomes], ["ok", "reject"])
        self.assertIn("并发冲突", outcomes[0][1] or outcomes[1][1] or "")
        # 数据库中 BAT-05 只有一条绑定，且只绑在赢的那辆车上
        rows = self.db.query("SELECT * FROM bonds WHERE battery_id='BAT-05'")
        self.assertEqual(len(rows), 1)
        winner_fork = self.db.query_one(
            "SELECT forklift_id FROM bonds WHERE battery_id='BAT-05'")["forklift_id"]
        self.assertIn(winner_fork, ("FK-01", "FK-02"))
        # 两单的扫码记录：一成功一拒绝
        scans = self.db.query(
            "SELECT result FROM scan_events WHERE phase='NEW_INSTALL' AND battery_id='BAT-05'")
        self.assertEqual({s["result"] for s in scans}, {"拒绝", "成功"})


class TestHTTPEndToEnd(unittest.TestCase):
    """真实 HTTP 服务上跑一遍 API。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        path = os.path.join(self.tmp.name, "http.db")
        self.app = App(path, enable_charger=False)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app))
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.tmp.cleanup()

    def _post(self, path, payload):
        req = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (self.port, path),
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def _get(self, path):
        with urllib.request.urlopen("http://127.0.0.1:%d%s" % (self.port, path)) as r:
            return r.status, r.read()

    def test_api_and_static(self):
        st, body = self._get("/api/state")
        self.assertEqual(st, 200)
        state = json.loads(body)
        self.assertIn("BAT-07", [b["id"] for b in state["pool"]])
        st, html = self._get("/")
        self.assertEqual(st, 200)
        self.assertIn("换电".encode(), html)

    def test_reserve_and_permission_denied(self):
        st, j = self._post("/api/reserve",
                           {"forklift_id": "FK-04", "user": "wang", "note": "接口测试"})
        self.assertEqual(st, 200)
        self.assertTrue(j["ok"])
        # 调度员越权隔离 → 403
        st, j = self._post("/api/health",
                           {"battery_id": "BAT-06", "user": "zhang", "result": "隔离"})
        self.assertEqual(st, 403)
        self.assertFalse(j["ok"])

    def test_concurrent_scan_over_http(self):
        # 两单走到待安装后，两个并发 HTTP 请求抢 BAT-06（B-48V，FK-01/FK-02 均兼容）
        oid_b = self.app.service.reserve("FK-01", "zhao", "http并发")
        for oid, old, slot in [("SW-DEMO-001", "BAT-02", "SLOT-01"),
                               (oid_b, "BAT-01", "SLOT-04")]:
            self.app.service.start(oid, "wang")
            self.app.service.scan_old_remove(oid, old, "wang")
            self.app.service.scan_slot_in(oid, slot, "wang")
        out = {}

        def fire(tag, oid, user):
            out[tag] = self._post("/api/scan",
                                  {"kind": "new", "order_id": oid, "code": "BAT-06",
                                   "user": user})

        barrier = threading.Barrier(2)

        def fired(tag, oid, user):
            barrier.wait()
            fire(tag, oid, user)

        t1 = threading.Thread(target=fired, args=("a", "SW-DEMO-001", "wang"))
        t2 = threading.Thread(target=fired, args=("b", oid_b, "zhao"))
        t1.start(); t2.start(); t1.join(); t2.join()
        codes = sorted(out[k][0] for k in out)
        self.assertEqual(codes, [200, 409])


if __name__ == "__main__":
    unittest.main(verbosity=2)
