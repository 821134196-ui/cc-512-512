# -*- coding: utf-8 -*-
"""SQLite 存储层。

互斥绑定的核心是 ``bonds`` 表：
* ``battery_id`` 为主键 —— 一块电池同一时刻最多一行，不可能既连车又连槽；
* ``forklift_id`` / ``slot_id`` 均带 UNIQUE —— 一辆车 / 一个槽位同时只能绑一块；
* CHECK 约束保证一行绑定要么指向车、要么指向槽。

写操作一律走 ``BEGIN IMMEDIATE`` 事务，配合 WAL 与 busy_timeout，
两人并发扫码同一电池时由数据库裁决，只有一人能绑定成功。
"""
import os
import sqlite3
import threading
from contextlib import contextmanager

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS users (
  username TEXT PRIMARY KEY,
  name     TEXT NOT NULL,
  role     TEXT NOT NULL CHECK (role IN ('dispatcher','maintainer','operator'))
);

CREATE TABLE IF NOT EXISTS forklifts (
  id                 TEXT PRIMARY KEY,          -- FK-01
  model              TEXT NOT NULL,             -- 车型 CP-1T/CP-2T/CP-3T
  accepted_models    TEXT NOT NULL,             -- 兼容电池型号，逗号分隔
  current_battery_id TEXT,
  status             TEXT NOT NULL DEFAULT '作业中', -- 作业中/等待换电/换电中/停用
  updated_at         TEXT
);

CREATE TABLE IF NOT EXISTS batteries (
  id                 TEXT PRIMARY KEY,          -- BAT-01
  model              TEXT NOT NULL,             -- B-24V/B-48V/B-80V
  soc                INTEGER NOT NULL DEFAULT 0 CHECK (soc BETWEEN 0 AND 100),
  temperature        REAL NOT NULL DEFAULT 25.0,
  soh                INTEGER NOT NULL DEFAULT 100 CHECK (soh BETWEEN 0 AND 100),
  overheating        INTEGER NOT NULL DEFAULT 0,    -- 过热标志
  charge_interrupted INTEGER NOT NULL DEFAULT 0,    -- 最近一次充电中断未闭环
  health_status      TEXT NOT NULL DEFAULT '正常',  -- 正常/隔离/退役
  isolate_reason     TEXT,
  isolated_by        TEXT,
  isolated_at        TEXT,
  updated_at         TEXT
);

CREATE TABLE IF NOT EXISTS slots (
  id              TEXT PRIMARY KEY,              -- SLOT-01
  name            TEXT NOT NULL,
  accepted_models TEXT NOT NULL DEFAULT '*',     -- * 表示全部型号
  status          TEXT NOT NULL DEFAULT '空闲',  -- 空闲/充电中/故障
  fault           INTEGER NOT NULL DEFAULT 0,
  updated_at      TEXT
);

-- 互斥绑定表：电池 0..1 车，电池 0..1 槽，车/槽 0..1 电池
CREATE TABLE IF NOT EXISTS bonds (
  battery_id  TEXT PRIMARY KEY REFERENCES batteries(id),
  forklift_id TEXT UNIQUE REFERENCES forklifts(id),
  slot_id     TEXT UNIQUE REFERENCES slots(id),
  bound_at    TEXT NOT NULL,
  CHECK ((forklift_id IS NOT NULL AND slot_id IS NULL)
      OR (forklift_id IS NULL AND slot_id IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS swap_orders (
  id             TEXT PRIMARY KEY,               -- SW-20261001-001
  forklift_id    TEXT NOT NULL REFERENCES forklifts(id),
  old_battery_id TEXT REFERENCES batteries(id),
  new_battery_id TEXT REFERENCES batteries(id),
  slot_id        TEXT REFERENCES slots(id),
  status         TEXT NOT NULL,                  -- 已预约/换电中/待入槽/待安装/待复核/已完成/已取消
  operator       TEXT,
  scheduled_at   TEXT,
  started_at     TEXT,
  old_scan_at    TEXT,
  slot_scan_at   TEXT,
  new_scan_at    TEXT,
  finished_at    TEXT,
  check_secured  INTEGER NOT NULL DEFAULT 0,     -- 固定复核
  check_soc      INTEGER,                        -- 装新后可用电量
  note           TEXT
);

CREATE TABLE IF NOT EXISTS scan_events (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id   TEXT,
  battery_id TEXT,
  slot_id    TEXT,
  phase      TEXT NOT NULL,                      -- OLD_REMOVE/SLOT_IN/NEW_INSTALL
  operator   TEXT,
  result     TEXT NOT NULL,                      -- 成功/拒绝
  detail     TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS charge_logs (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  battery_id     TEXT NOT NULL,
  slot_id        TEXT NOT NULL,
  started_at     TEXT NOT NULL,
  ended_at       TEXT,
  start_soc      INTEGER,
  end_soc        INTEGER,
  energy_kwh     REAL NOT NULL DEFAULT 0,
  interrupt_reason TEXT,                         -- NULL=正常结束；过热保护/槽位故障/模拟中断
  status         TEXT NOT NULL                   -- 充电中/已充满/已中断
);

CREATE TABLE IF NOT EXISTS health_checks (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  battery_id  TEXT NOT NULL,
  soh         INTEGER,
  temperature REAL,
  result      TEXT NOT NULL,                     -- 合格/隔离
  reason      TEXT,
  inspector   TEXT NOT NULL,
  photo_path  TEXT,
  created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS event_log (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  kind       TEXT NOT NULL,
  message    TEXT NOT NULL,
  created_at TEXT NOT NULL
);

-- 模拟充电桩适配器周期上报的最新信号（每槽一行）
CREATE TABLE IF NOT EXISTS charger_signals (
  slot_id    TEXT PRIMARY KEY REFERENCES slots(id),
  online     INTEGER NOT NULL DEFAULT 1,
  voltage    REAL,
  current    REAL,
  power_kw   REAL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_scan_order ON scan_events(order_id);
CREATE INDEX IF NOT EXISTS idx_charge_battery ON charge_logs(battery_id);
CREATE INDEX IF NOT EXISTS idx_health_battery ON health_checks(battery_id);
"""


class Database:
    """每个线程一个连接的 SQLite 封装。"""

    def __init__(self, path: str):
        self.path = path
        self._local = threading.local()
        if path == ":memory:":
            # 共享缓存内存库：同一 Database 实例的所有线程连接看到同一份数据
            import uuid
            self._dsn = "file:mem-%s?mode=memory&cache=shared" % uuid.uuid4().hex
        else:
            self._dsn = path
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self._dsn, timeout=10, isolation_level=None,
                                   uri=self._dsn.startswith("file:"))
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=8000")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
        return conn

    @contextmanager
    def transaction(self):
        """串行化写事务：并发扫码时后到者在锁释放后读到最新绑定状态。"""
        conn = self.connect()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise

    def query(self, sql, params=()):
        return self.connect().execute(sql, params).fetchall()

    def query_one(self, sql, params=()):
        return self.connect().execute(sql, params).fetchone()

    def init_schema(self):
        conn = self.connect()
        conn.executescript(SCHEMA)


def now() -> str:
    import datetime
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
