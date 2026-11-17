"""并发测试: 并发调用的结果必须与某种逐次执行一致(可串行化)。"""

import os
import tempfile
import threading
import unittest

from merger.errors import ApiError
from merger.service import MergeService
from tests.test_service import BASELINE, SOURCE, TARGET, rec


class ConcurrencyBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "c.db")
        self.svc = MergeService(self.db)
        self.svc.initialize(TARGET)

    def tearDown(self):
        self.svc.close()
        self.tmp.cleanup()

    def make_order(self):
        return self.svc.create_order(BASELINE, SOURCE)


class TestConcurrentAdjudications(ConcurrencyBase):
    def test_concurrent_upserts_equal_sequential_result(self):
        v = self.make_order()
        oid = v["order_id"]
        threads_n, per_thread = 6, 4
        successes = []
        lock = threading.Lock()

        def worker(ti):
            for k in range(per_thread):
                rid = f"RT{ti}_{k}"
                while True:
                    rev = self.svc.get_order(oid)["revision"]
                    try:
                        self.svc.apply_actions(oid, rev, [
                            {"type": "upsert_row", "table": "records",
                             "values": rec(rid, "DS-002", "2.0", f"S-T{ti}-{k}", "v")}
                        ])
                    except ApiError as e:
                        if e.code == "STALE_REVISION":
                            continue  # 修订号竞争, 重试
                        raise
                    with lock:
                        successes.append(rid)
                    break

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(threads_n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        total = threads_n * per_thread
        self.assertEqual(len(successes), total)
        final = self.svc.get_order(oid)
        # 每次成功修改恰好推进一个修订号: 无丢失、无重复
        self.assertEqual(final["revision"], 1 + total)
        self.assertEqual(len(self.svc.get_history(oid)["history"]), total)
        ids = {r["id"] for r in final["candidates"]["records"]}
        for rid in successes:
            self.assertIn(rid, ids)


class TestConcurrentDeliveries(ConcurrencyBase):
    def _deliverable_order(self):
        v = self.make_order()
        oid = v["order_id"]
        v = self.svc.apply_actions(oid, 1, [
            {"type": "resolve_row", "conflict_id": "c1", "keep": False},
            {"type": "resolve_field", "conflict_id": "c2", "value": "8"},
            {"type": "resolve_row", "conflict_id": "c3", "keep": True, "side": "target"},
        ])
        self.assertEqual(v["integrity"]["status"], "ok")
        return oid, v["revision"]

    def test_same_request_concurrent_replays(self):
        oid, rev = self._deliverable_order()
        results, errors = [], []
        lock = threading.Lock()

        def worker():
            try:
                r = self.svc.deliver("req-shared", oid, rev)
                with lock:
                    results.append(r)
            except ApiError as e:
                with lock:
                    errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 8)
        fresh = [r for r in results if not r["idempotent_replay"]]
        self.assertEqual(len(fresh), 1)  # 恰好执行一次真实交付
        receipts = {str(r["receipt"]) for r in results}
        self.assertEqual(len(receipts), 1)  # 所有响应回执一致
        self.assertEqual(self.svc.get_target()["version"], 2)  # 只升版一次

    def test_delivery_vs_adjudication_serialized(self):
        outcomes = set()
        for round_no in range(12):
            with tempfile.TemporaryDirectory() as td:
                svc = MergeService(os.path.join(td, "r.db"))
                svc.initialize(TARGET)
                v = svc.create_order(BASELINE, SOURCE)
                oid = v["order_id"]
                v = svc.apply_actions(oid, 1, [
                    {"type": "resolve_row", "conflict_id": "c1", "keep": False},
                    {"type": "resolve_field", "conflict_id": "c2", "value": "8"},
                    {"type": "resolve_row", "conflict_id": "c3", "keep": True, "side": "target"},
                ])
                rev = v["revision"]
                result = {}

                def do_deliver():
                    try:
                        svc.deliver(f"req-{round_no}", oid, rev)
                        result["deliver"] = "ok"
                    except ApiError as e:
                        result["deliver"] = e.code

                def do_adjudicate():
                    try:
                        svc.apply_actions(oid, rev, [
                            {"type": "upsert_row", "table": "records",
                             "values": rec("RX", "DS-002", "2.0", "S-XX", "v")}
                        ])
                        result["adjudicate"] = "ok"
                    except ApiError as e:
                        result["adjudicate"] = e.code

                t1 = threading.Thread(target=do_deliver)
                t2 = threading.Thread(target=do_adjudicate)
                t1.start()
                t2.start()
                t1.join()
                t2.join()

                # 两种合法的逐次执行结果必居其一, 且状态与之吻合
                if result["deliver"] == "ok":
                    self.assertEqual(result["adjudicate"], "ORDER_DELIVERED")
                    self.assertEqual(svc.get_order(oid)["status"], "delivered")
                    self.assertEqual(svc.get_target()["version"], 2)
                    outcomes.add("deliver_first")
                else:
                    self.assertEqual(result["deliver"], "STALE_REVISION")
                    self.assertEqual(result["adjudicate"], "ok")
                    self.assertEqual(svc.get_order(oid)["status"], "open")
                    self.assertEqual(svc.get_order(oid)["revision"], rev + 1)
                    self.assertEqual(svc.get_target()["version"], 1)
                    outcomes.add("adjudicate_first")
                svc.close()
        # 两种交错都可能发生, 至少确认结果始终是二者之一(上面已逐轮断言)
        self.assertTrue(outcomes <= {"deliver_first", "adjudicate_first"})


if __name__ == "__main__":
    unittest.main()
