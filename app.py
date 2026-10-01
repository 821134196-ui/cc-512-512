#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""单条命令启动：python3 app.py [--port 8000] [--reset]"""
import argparse
import os

from battery_swap.seed import seed
from battery_swap.server import serve

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="叉车动力电池换电管理系统")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--db", default=os.path.join("data", "battery_swap.db"))
    ap.add_argument("--reset", action="store_true", help="清空并重新生成演示数据")
    args = ap.parse_args()

    if args.reset and os.path.exists(args.db):
        for suffix in ("", "-wal", "-shm"):
            p = args.db + suffix
            if os.path.exists(p):
                os.remove(p)

    serve(host=args.host, port=args.port, db_path=args.db,
          overheat_when={"BAT-09": 72}, enable_charger=True)
