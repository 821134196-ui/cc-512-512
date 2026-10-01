# -*- coding: utf-8 -*-
"""零依赖 HTTP 服务：JSON API + 静态页面 + 本地检测照片。"""
import base64
import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .db import Database, now
from .services import SwapService, ServiceError, PHOTO_DIR
from .charger import ChargerAdapter
from .seed import seed

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

_MIME = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
         ".js": "application/javascript; charset=utf-8", ".png": "image/png",
         ".svg": "image/svg+xml", ".ico": "image/x-icon"}


class App:
    def __init__(self, db_path, overheat_when=None, enable_charger=True):
        self.db = Database(db_path)
        self.db.init_schema()
        seed(self.db)
        self.service = SwapService(self.db)
        self.charger = ChargerAdapter(
            self.db, self.service, overheat_when=overheat_when or {"BAT-09": 72})
        if enable_charger:
            self.charger.start()


def make_handler(app: App):
    svc = app.service

    class Handler(BaseHTTPRequestHandler):
        server_version = "BatterySwap/1.0"

        def log_message(self, fmt, *args):
            print("[http] " + fmt % args)

        # ---------- 响应工具 ----------
        def _json(self, obj, status=200):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self):
            n = int(self.headers.get("Content-Length", 0))
            if not n:
                return {}
            try:
                return json.loads(self.rfile.read(n).decode("utf-8"))
            except json.JSONDecodeError:
                raise ServiceError("请求体不是合法 JSON")

        def _ok(self, message, **extra):
            self._json({"ok": True, "message": message, "state": svc.state(), **extra})

        # ---------- 路由 ----------
        def do_GET(self):
            path = self.path.split("?", 1)[0]
            try:
                if path == "/api/state":
                    qs = self._qs()
                    self._json(svc.state(qs.get("forklift")))
                elif path.startswith("/photos/"):
                    self._serve_photo(os.path.basename(path[len("/photos/"):]))
                else:
                    self._serve_static(path)
            except ServiceError as e:
                self._json({"ok": False, "error": str(e)}, e.status)
            except FileNotFoundError:
                self._json({"ok": False, "error": "not found"}, 404)

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            try:
                data = self._read_json()
                if path == "/api/reserve":
                    oid = svc.reserve(data["forklift_id"], data["user"], data.get("note", ""))
                    self._ok("预约成功：" + oid, order_id=oid)
                elif (m := re.fullmatch(r"/api/orders/([\w-]+)/(start|confirm|cancel)", path)):
                    oid, action = m.group(1), m.group(2)
                    getattr(svc, action)(oid, data["user"])
                    label = {"start": "已开工", "confirm": "复核通过，换电完成",
                             "cancel": "工单已取消"}[action]
                    self._ok(label)
                elif path == "/api/scan":
                    kind = data["kind"]
                    fn = {"old": svc.scan_old_remove, "slot": svc.scan_slot_in,
                          "new": svc.scan_new_install}[kind]
                    args = [data["order_id"], data["code"], data["user"]]
                    fn(*args)
                    label = {"old": "旧电池已取下交接", "slot": "旧电池已入槽充电",
                             "new": "新电池已安装，待复核"}[kind]
                    self._ok(label)
                elif path == "/api/health":
                    photo_path = self._save_photo(data) if data.get("photo") else None
                    svc.health_check(data["battery_id"], data["user"], data["result"],
                                     data.get("reason", ""), data.get("soh"), photo_path)
                    self._ok("检测结论已登记：%s %s" % (data["battery_id"], data["result"]))
                elif path == "/api/overheat-reset":
                    svc.reset_overheat(data["battery_id"], data["user"])
                    self._ok("%s 过热/中断标记已复位" % data["battery_id"])
                elif path == "/api/slot-fault":
                    svc.set_slot_fault(data["slot_id"], data["user"],
                                       bool(data.get("fault")), data.get("reason", ""))
                    self._ok("%s 故障状态已更新" % data["slot_id"])
                else:
                    self._json({"ok": False, "error": "未知接口"}, 404)
            except ServiceError as e:
                self._json({"ok": False, "error": str(e)}, e.status)
            except KeyError as e:
                self._json({"ok": False, "error": "缺少参数：%s" % e.args[0]}, 400)

        # ---------- 静态资源 / 照片 ----------
        def _serve_static(self, path):
            if path == "/":
                path = "/index.html"
            target = os.path.normpath(os.path.join(STATIC_DIR, path.lstrip("/")))
            if not target.startswith(STATIC_DIR) or not os.path.isfile(target):
                raise FileNotFoundError(path)
            with open(target, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type",
                             _MIME.get(os.path.splitext(target)[1], "application/octet-stream"))
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _serve_photo(self, name):
            target = os.path.join(PHOTO_DIR, name)
            if not os.path.normpath(target).startswith(PHOTO_DIR) or not os.path.isfile(target):
                raise FileNotFoundError(name)
            with open(target, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        @staticmethod
        def _save_photo(data):
            os.makedirs(PHOTO_DIR, exist_ok=True)
            raw = data["photo"]
            m = re.match(r"data:image/(\w+);base64,(.*)", raw, re.S)
            if not m:
                raise ServiceError("照片格式不支持")
            ext = "jpg" if m.group(1) in ("jpeg", "jpg") else m.group(1)
            name = "%s-%s.%s" % (data["battery_id"], now().replace(":", "").replace(" ", "-"),
                                 ext)
            path = os.path.join(PHOTO_DIR, name)
            with open(path, "wb") as f:
                f.write(base64.b64decode(m.group(2)))
            return path

        def _qs(self):
            qs = {}
            if "?" in self.path:
                for pair in self.path.split("?", 1)[1].split("&"):
                    if "=" in pair:
                        k, v = pair.split("=", 1)
                        qs[k] = v
            return qs

    return Handler


def serve(host="127.0.0.1", port=8000, db_path="data/battery_swap.db",
          overheat_when=None, enable_charger=True):
    app = App(db_path, overheat_when=overheat_when, enable_charger=enable_charger)
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    print("换电管理系统已启动：http://%s:%d" % (host, port))
    print("账号：zhang(调度员) / li(维修) / wang、zhao(换电员)；BAT-09 将在充电中触发热失控样例")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        app.charger.stop()
        httpd.shutdown()
    return app
