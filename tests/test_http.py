"""HTTP 接口测试: 真实启动服务, 通过 HTTP 请求验证。"""

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from merger.server import make_server
from merger.service import MergeService
from tests.test_service import BASELINE, SOURCE, TARGET


def http(port, method, path, body=None):
    url = f"http://127.0.0.1:{port}{path}"
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


class TestHttp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.svc = MergeService(os.path.join(cls.tmp.name, "http.db"))
        cls.server = make_server(cls.svc, "127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.svc.close()
        cls.tmp.cleanup()

    def test_full_http_flow(self):
        p = self.port
        # 健康检查与未知路径
        s, b = http(p, "GET", "/health")
        self.assertEqual((s, b["status"]), (200, "ok"))
        s, b = http(p, "GET", "/nope")
        self.assertEqual(s, 404)
        # 初始化
        s, b = http(p, "POST", "/init", {"data": TARGET})
        self.assertEqual(s, 201)
        self.assertEqual(b["version"], 1)
        s, b = http(p, "POST", "/init", {"data": TARGET})
        self.assertEqual((s, b["error"]["code"]), (409, "ALREADY_INITIALIZED"))
        # 目标查询
        s, b = http(p, "GET", "/target")
        self.assertEqual((s, b["version"]), (200, 1))
        # 建单
        s, b = http(p, "POST", "/orders", {"baseline": BASELINE, "source": SOURCE})
        self.assertEqual(s, 201)
        oid = b["order_id"]
        self.assertEqual(b["revision"], 1)
        self.assertEqual(len(b["conflicts"]), 3)
        self.assertEqual(b["integrity"]["status"], "pending")
        # 错误 JSON
        req = urllib.request.Request(f"http://127.0.0.1:{p}/orders", data=b"{bad", method="POST")
        try:
            with urllib.request.urlopen(req) as resp:
                self.fail("应返回 400")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 400)
            self.assertEqual(json.loads(e.read())["error"]["code"], "BAD_JSON")
        # 裁决(旧修订号拒绝)
        s, b = http(p, "POST", f"/orders/{oid}/actions", {"revision": 7, "actions": [{"type": "resolve_field", "conflict_id": "c2", "value": "8"}]})
        self.assertEqual((s, b["error"]["code"]), (409, "STALE_REVISION"))
        s, b = http(p, "POST", f"/orders/{oid}/actions", {"revision": 1, "actions": [
            {"type": "resolve_row", "conflict_id": "c1", "keep": False},
            {"type": "resolve_field", "conflict_id": "c2", "value": "8"},
            {"type": "resolve_row", "conflict_id": "c3", "keep": True, "side": "target"},
        ]})
        self.assertEqual(s, 200)
        self.assertEqual(b["revision"], 2)
        self.assertEqual(b["integrity"]["status"], "ok")
        # 历史
        s, b = http(p, "GET", f"/orders/{oid}/history")
        self.assertEqual(len(b["history"]), 1)
        # 交付
        s, b = http(p, "POST", "/deliveries", {"request_id": "req-http-1", "order_id": oid, "revision": 2})
        self.assertEqual(s, 201)
        receipt = b["receipt"]
        self.assertEqual(receipt["target_version"], 2)
        # 重试 -> 原回执
        s, b = http(p, "POST", "/deliveries", {"request_id": "req-http-1", "order_id": oid, "revision": 2})
        self.assertEqual((s, b["idempotent_replay"]), (200, True))
        self.assertEqual(b["receipt"], receipt)
        # 回执查询
        s, b = http(p, "GET", "/deliveries/req-http-1")
        self.assertEqual((s, b["receipt"]), (200, receipt))
        # 目标已升版且数据完整
        s, b = http(p, "GET", "/target")
        self.assertEqual((s, b["version"]), (200, 2))
        self.assertEqual(len(b["data"]["records"]), 6)


if __name__ == "__main__":
    unittest.main()
