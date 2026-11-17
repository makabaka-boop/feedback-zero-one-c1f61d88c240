"""HTTP JSON 接口层(仅标准库 http.server), 薄封装在 MergeService 之上。

编码约定: 请求/响应均为 UTF-8 JSON; 错误响应形如
{"error": {"code": <稳定错误码>, "message": <说明>, "details": <可选>}}。
"""

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .errors import ApiError
from .schema import TABLES, TABLE_ORDER

API_INDEX = {
    "endpoints": [
        "GET  /health",
        "GET  /schema",
        "POST /init                      初始化合法目标数据(仅一次), 可选 body: {\"data\": {表: [行]}}",
        "GET  /target                    查询目标: 版本及完整数据",
        "POST /orders                    建单, body: {\"baseline\": {...}, \"source\": {...}}",
        "GET  /orders                    审定单列表",
        "GET  /orders/{id}               查询: 当前修订、候选与冲突(含完整性)",
        "GET  /orders/{id}/history       裁决/修正历史",
        "POST /orders/{id}/actions       裁决与修正, body: {\"revision\": n, \"actions\": [...]}",
        "POST /deliveries                交付, body: {\"request_id\", \"order_id\", \"revision\"}",
        "GET  /deliveries/{request_id}   查询交付回执",
    ]
}


def make_server(service, host="127.0.0.1", port=8080):
    routes = []

    def route(method, pattern):
        rx = re.compile("^" + pattern + "$")

        def deco(fn):
            routes.append((method, rx, fn))
            return fn

        return deco

    @route("GET", r"/")
    def _(body, m):
        return 200, API_INDEX

    @route("GET", r"/health")
    def _(body, m):
        return 200, {"status": "ok"}

    @route("GET", r"/schema")
    def _(body, m):
        return 200, {
            "tables": TABLES,
            "table_order": TABLE_ORDER,
            "conventions": {
                "values": "字段值只能是字符串或空值(null)",
                "row_identity": "id 字段为稳定行标识",
                "pending_marker": "未决内容表示为 {\"%s\": \"<冲突id>\"}, 区别于空值与删除" % "$pending",
                "cycles": "允许循环引用",
            },
        }

    @route("POST", r"/init")
    def _(body, m):
        data = body.get("data") if isinstance(body, dict) else None
        return 201, service.initialize(data)

    @route("GET", r"/target")
    def _(body, m):
        return 200, service.get_target()

    @route("POST", r"/orders")
    def _(body, m):
        body = body if isinstance(body, dict) else {}
        return 201, service.create_order(body.get("baseline"), body.get("source"))

    @route("GET", r"/orders")
    def _(body, m):
        return 200, service.list_orders()

    @route("GET", r"/orders/(?P<oid>[^/]+)")
    def _(body, m):
        return 200, service.get_order(m.group("oid"))

    @route("GET", r"/orders/(?P<oid>[^/]+)/history")
    def _(body, m):
        return 200, service.get_history(m.group("oid"))

    @route("POST", r"/orders/(?P<oid>[^/]+)/actions")
    def _(body, m):
        body = body if isinstance(body, dict) else {}
        return 200, service.apply_actions(m.group("oid"), body.get("revision"), body.get("actions"))

    @route("POST", r"/deliveries")
    def _(body, m):
        body = body if isinstance(body, dict) else {}
        result = service.deliver(body.get("request_id"), body.get("order_id"), body.get("revision"))
        return (200 if result["idempotent_replay"] else 201), result

    @route("GET", r"/deliveries/(?P<rid>[^/]+)")
    def _(body, m):
        return 200, service.get_delivery(m.group("rid"))

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "MergerHTTP/1.0"

        def _handle(self, method):
            try:
                body = None
                if method == "POST":
                    length = int(self.headers.get("Content-Length") or 0)
                    raw = self.rfile.read(length) if length else b""
                    if raw:
                        try:
                            body = json.loads(raw.decode("utf-8"))
                        except (UnicodeDecodeError, json.JSONDecodeError):
                            raise ApiError(400, "BAD_JSON", "请求体不是合法 JSON")
                path = self.path.split("?", 1)[0]
                for m, rx, fn in routes:
                    if m != method:
                        continue
                    match = rx.match(path)
                    if match:
                        status, obj = fn(body, match)
                        self._send(status, obj)
                        return
                self._send(404, {"error": {"code": "NOT_FOUND", "message": f"未知路径 {method} {path}"}})
            except ApiError as e:
                self._send(e.status, e.to_dict())
            except Exception as e:  # pragma: no cover - 兜底, 不影响其他请求
                self._send(500, {"error": {"code": "INTERNAL", "message": repr(e)}})

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

        def _send(self, status, obj):
            payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    return ThreadingHTTPServer((host, port), Handler)
