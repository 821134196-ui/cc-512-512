"""零依赖 HTTP 服务：换电业务 API + 静态界面，单命令启动。"""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import db
import seed
from charger_adapter import ChargerAdapter

BASE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(BASE, "static")

# 端点 -> 允许的角色（调度员不能改健康结论；维修负责隔离）
ROLE_MATRIX = {
    "/api/queue": {"dispatcher", "maintenance"},
    "/api/queue/cancel": {"dispatcher", "maintenance"},
    "/api/swap/scan-old": {"operator", "maintenance"},
    "/api/swap/install": {"operator", "maintenance"},
    "/api/swap/complete": {"operator", "maintenance"},
    "/api/charge/start": {"operator", "maintenance"},
    "/api/health/check": {"maintenance"},
    "/api/slot/state": {"maintenance"},
}

# 每个请求使用独立连接；并发互斥由 BEGIN IMMEDIATE 事务 + UNIQUE 约束保证


class Handler(BaseHTTPRequestHandler):
    server_version = "ForkSwap/1.0"

    # ---------- 工具 ----------
    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            raise db.Conflict("请求体不是合法 JSON")

    def _guard_role(self, path, payload):
        allowed = ROLE_MATRIX.get(path)
        if allowed is None:
            return
        role = (payload.get("role") or "").strip()
        if role not in allowed:
            raise db.Forbidden(
                f"当前角色“{role or '未选择'}”无权操作；允许：{'/'.join(sorted(allowed))}")

    def log_message(self, fmt, *args):
        print(f"[http] {self.address_string()} - {fmt % args}", flush=True)

    # ---------- GET ----------
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        try:
            if path.startswith("/api/"):
                return self._api_get(path)
            if path.startswith("/photos/"):
                return self._serve_photo(path)
            return self._serve_static(path)
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def _api_get(self, path):
        conn = db.get_conn()
        try:
            if path == "/api/overview":
                return self._send_json(db.overview(conn))
            if path == "/api/pool":
                return self._send_json(db.pool_map(conn))
            if path == "/api/vehicles":
                return self._send_json(db.list_vehicles(conn))
            if path == "/api/queue":
                return self._send_json(db.list_queue(conn))
            if path == "/api/swaps":
                return self._send_json(db.list_swap_history(conn))
            if path == "/api/charges":
                return self._send_json(db.list_charge_records(conn))
            if path == "/api/health":
                return self._send_json(db.list_health_checks(conn))
            if path == "/api/batteries":
                qs = self.path.split("?", 1)[-1] if "?" in self.path else ""
                params = dict(p.split("=", 1) for p in qs.split("&") if "=" in p)
                from urllib.parse import unquote
                model = unquote(params.get("model", "")) or None
                usable = params.get("usable") == "1"
                return self._send_json(db.list_batteries(conn, model, usable))
            self._send_json({"error": "未知接口"}, 404)
        finally:
            conn.close()

    # ---------- POST ----------
    def do_POST(self):
        path = self.path.split("?", 1)[0]
        try:
            payload = self._read_json()
            self._guard_role(path, payload)
            conn = db.get_conn()
            try:
                self._api_post(path, payload, conn)
            finally:
                conn.close()
        except db.Forbidden as e:
            self._send_json({"error": str(e)}, 403)
        except db.Conflict as e:
            self._send_json({"error": str(e)}, 409)
        except KeyError as e:
            self._send_json({"error": f"缺少参数: {e}"}, 400)

    def _api_post(self, path, p, conn):
        actor = (p.get("actor") or "anonymous").strip()

        if path == "/api/queue":
            qid = db.enqueue(conn, p["vehicle_code"], p.get("window", ""), actor)
            return self._send_json({"ok": True, "queue_id": qid})
        if path == "/api/queue/cancel":
            db.cancel_queue(conn, int(p["queue_id"]), actor)
            return self._send_json({"ok": True})
        if path == "/api/swap/scan-old":
            return self._send_json(db.scan_old_battery(conn, int(p["queue_id"]), actor))
        if path == "/api/swap/install":
            return self._send_json(db.install_new_battery(
                conn, int(p["queue_id"]), p["battery_code"], actor))
        if path == "/api/swap/complete":
            return self._send_json(db.complete_swap(
                conn, int(p["queue_id"]), bool(p.get("fixation_ok", False)), actor))
        if path == "/api/charge/start":
            return self._send_json(db.start_charge(
                conn, p["battery_code"], p["slot_code"], actor))
        if path == "/api/health/check":
            db.health_check(
                conn, p["battery_code"], p["result"], int(p["health_score"]),
                p.get("reasons", ""), p.get("photo", ""), p.get("note", ""),
                actor, p["role"])
            return self._send_json({"ok": True})
        if path == "/api/slot/state":
            db.set_slot_state(conn, p["slot_code"], bool(p.get("serviceable", True)),
                              p.get("reason", ""), actor, p["role"])
            return self._send_json({"ok": True})
        self._send_json({"error": "未知接口"}, 404)

    # ---------- 静态资源 ----------
    def _serve_static(self, path):
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        full = os.path.normpath(os.path.join(STATIC, rel))
        if not full.startswith(STATIC) or not os.path.isfile(full):
            self.send_error(404)
            return
        ctype = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml",
                 ".png": "image/png"}.get(os.path.splitext(full)[1], "application/octet-stream")
        with open(full, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _serve_photo(self, path):
        name = os.path.basename(path)
        full = os.path.join(db.PHOTO_DIR, name)
        if not os.path.isfile(full):
            self.send_error(404)
            return
        with open(full, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)


def main():
    seed.seed(reset=os.environ.get("SWAP_RESET") == "1")
    conn = db.get_conn()
    db.init_db(conn)
    conn.close()
    adapter = ChargerAdapter()
    adapter.start()
    port = int(os.environ.get("SWAP_PORT", "8000"))
    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"叉车换电管理系统已启动: http://localhost:{port}", flush=True)
    print("  含过热样例：BAT-A03（充电中将迅速过热）、BAT-B04（已隔离 57.2℃）", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        adapter.stop()
        httpd.server_close()


if __name__ == "__main__":
    main()
