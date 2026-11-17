"""服务层端到端测试: 初始化、建单、裁决、校验、交付、幂等、重启。"""

import os
import tempfile
import unittest

from merger.errors import ApiError
from merger.service import MergeService


def rec(rid, dc="DS-001", dv="1.0", subj="S-01", value=None, nxt=None):
    return {
        "id": rid,
        "dataset_code": dc,
        "dataset_version": dv,
        "subject": subj,
        "value": value,
        "next_record_id": nxt,
    }


def ds(rid, code, ver, title=None, lead=None):
    return {"id": rid, "code": code, "version": ver, "title": title, "lead_record_id": lead}


# 目标(当前)数据: 合法, 含循环引用
TARGET = {
    "datasets": [
        ds("D1", "DS-001", "1.0", "基线调查", "R2"),
        ds("D2", "DS-002", "2.0", "随访数据"),
        ds("D3", "DS-003", "1.0", "旧数据(目标改)"),
    ],
    "records": [
        rec("R1", subj="S-01", value="9", nxt="R2"),
        rec("R2", subj="S-02", value="20", nxt="R1"),
        rec("R3", "DS-002", "2.0", "S-09"),
        rec("R4", "DS-003", "1.0", "S-10", "x"),
        rec("R6", "DS-002", "2.0", "S-11", "T"),
        rec("R7", subj="S-04", value="目标侧"),
    ],
}

BASELINE = {
    "datasets": [
        ds("D1", "DS-001", "1.0", "基线调查"),
        ds("D2", "DS-002", "2.0", "随访"),
        ds("D3", "DS-003", "1.0", "旧数据"),
    ],
    "records": [
        rec("R1", subj="S-01", value="5"),
        rec("R2", subj="S-02", value="20"),
        rec("R3", "DS-002", "2.0", "S-09"),
        rec("R4", "DS-003", "1.0", "S-10", "x"),
    ],
}

SOURCE = {
    "datasets": [
        ds("D1", "DS-001", "1.0", "基线调查(修订)", "R2"),
        ds("D2", "DS-002", "2.0", "随访"),
        # D3 被来源侧删除
    ],
    "records": [
        rec("R1", subj="S-01", value="7", nxt="R2"),
        rec("R2", subj="S-02", value="20"),
        rec("R3", "DS-002", "2.0", "S-09"),
        # R4 被来源侧删除
        rec("R5", subj="S-03", value="A"),
        rec("R7", subj="S-04", value="来源侧"),
    ],
}


class ServiceTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "test.db")
        self.svc = MergeService(self.db)

    def tearDown(self):
        self.svc.close()
        self.tmp.cleanup()

    def init_target(self):
        return self.svc.initialize(TARGET)

    def make_order(self):
        """建单: 产生 c1=D3 删改冲突, c2=R1.value 字段冲突, c3=R7 新增冲突。"""
        self.init_target()
        return self.svc.create_order(BASELINE, SOURCE)

    def resolve_all(self, oid):
        v = self.svc.apply_actions(oid, 1, [
            {"type": "resolve_row", "conflict_id": "c1", "keep": False},
            {"type": "resolve_field", "conflict_id": "c2", "value": "8"},
            {"type": "resolve_row", "conflict_id": "c3", "keep": True, "side": "target"},
        ])
        self.assertEqual(v["integrity"]["status"], "ok")
        return v


class TestInit(ServiceTestBase):
    def test_initialize_and_get_target(self):
        r = self.init_target()
        self.assertEqual(r["version"], 1)
        t = self.svc.get_target()
        self.assertEqual(t["version"], 1)
        self.assertEqual(len(t["data"]["datasets"]), 3)
        self.assertEqual(len(t["data"]["records"]), 6)

    def test_initialize_twice_rejected(self):
        self.init_target()
        with self.assertRaises(ApiError) as cm:
            self.svc.initialize(TARGET)
        self.assertEqual(cm.exception.code, "ALREADY_INITIALIZED")

    def test_initialize_invalid_data_rejected(self):
        bad = {"datasets": [ds("D1", "DS-X", "1")], "records": [rec("R1", "DS-404", "1", "S-1")]}
        with self.assertRaises(ApiError) as cm:
            self.svc.initialize(bad)
        self.assertEqual(cm.exception.code, "INVALID_INITIAL_DATA")
        self.assertTrue(cm.exception.details["dangling_references"])
        with self.assertRaises(ApiError):
            self.svc.get_target()  # 状态未变: 仍未初始化

    def test_not_initialized(self):
        with self.assertRaises(ApiError) as cm:
            self.svc.get_target()
        self.assertEqual(cm.exception.code, "NOT_INITIALIZED")
        with self.assertRaises(ApiError):
            self.svc.create_order(BASELINE, SOURCE)


class TestOrderAndMerge(ServiceTestBase):
    def test_create_order_merge_result(self):
        v = self.make_order()
        self.assertEqual(v["revision"], 1)
        self.assertEqual(v["status"], "open")
        self.assertEqual(v["target_version_at_creation"], 1)
        kinds = {c["conflict_id"]: c["kind"] for c in v["conflicts"]}
        self.assertEqual(kinds, {"c1": "delete_modify", "c2": "field", "c3": "add_add"})
        # 候选: R1.value 为未决标记(区别于空值与删除)
        r1 = next(r for r in v["candidates"]["records"] if r["id"] == "R1")
        self.assertEqual(r1["value"], {"$pending": "c2"})
        self.assertEqual(r1["next_record_id"], "R2")
        ids = {r["id"] for r in v["candidates"]["records"]}
        self.assertNotIn("R4", ids)  # 来源删除且目标未改 -> 删除
        self.assertNotIn("R7", ids)  # add_add 未决
        self.assertIn("R5", ids)     # 来源新增保留
        self.assertIn("R6", ids)     # 目标新增保留
        d_ids = {r["id"] for r in v["candidates"]["datasets"]}
        self.assertNotIn("D3", d_ids)  # 删改冲突未决
        d1 = next(r for r in v["candidates"]["datasets"] if r["id"] == "D1")
        self.assertEqual(d1["title"], "基线调查(修订)")  # 仅来源侧变化
        d2 = next(r for r in v["candidates"]["datasets"] if r["id"] == "D2")
        self.assertEqual(d2["title"], "随访数据")        # 仅目标侧变化
        # 有未决冲突时完整性待定
        self.assertEqual(v["integrity"]["status"], "pending")
        self.assertEqual(v["integrity"]["pending_conflicts"], 3)

    def test_create_order_invalid_baseline_rejected(self):
        self.init_target()
        bad = {
            "datasets": [ds("D1", "DS-001", "1.0"), ds("D2", "DS-001", "1.0")],  # 重复组合键
            "records": [],
        }
        with self.assertRaises(ApiError) as cm:
            self.svc.create_order(bad, SOURCE)
        self.assertEqual(cm.exception.code, "INVALID_BASELINE")
        self.assertEqual(self.svc.list_orders()["orders"], [])  # 状态未变

    def test_create_order_structural_violation_rejected(self):
        self.init_target()
        bad = {"datasets": [ds("D1", "DS-001", None)], "records": []}  # 非空约束
        with self.assertRaises(ApiError) as cm:
            self.svc.create_order(bad, SOURCE)
        self.assertEqual(cm.exception.code, "INVALID_DATA")

    def test_multiple_orders_coexist(self):
        self.make_order()
        v2 = self.svc.create_order(BASELINE, SOURCE)
        self.assertEqual(len(self.svc.list_orders()["orders"]), 2)
        self.assertNotEqual(v2["order_id"], "O-000001")


class TestAdjudication(ServiceTestBase):
    def test_stale_revision_rejected_and_state_unchanged(self):
        v = self.make_order()
        with self.assertRaises(ApiError) as cm:
            self.svc.apply_actions(v["order_id"], 99, [{"type": "resolve_field", "conflict_id": "c2", "value": "8"}])
        self.assertEqual(cm.exception.code, "STALE_REVISION")
        self.assertEqual(self.svc.get_order(v["order_id"])["revision"], 1)

    def test_resolve_and_revision_advances(self):
        v = self.make_order()
        oid = v["order_id"]
        v = self.svc.apply_actions(oid, 1, [{"type": "resolve_field", "conflict_id": "c2", "value": "8"}])
        self.assertEqual(v["revision"], 2)
        r1 = next(r for r in v["candidates"]["records"] if r["id"] == "R1")
        self.assertEqual(r1["value"], "8")
        c2 = next(c for c in v["conflicts"] if c["conflict_id"] == "c2")
        self.assertEqual(c2["status"], "resolved")
        self.assertEqual(c2["resolution"], {"value": "8"})
        self.assertEqual(c2["detail"], {"base": "5", "source": "7", "target": "9"})  # 原冲突保留
        h = self.svc.get_history(oid)["history"]
        self.assertEqual(len(h), 1)
        self.assertEqual((h[0]["revision_before"], h[0]["revision_after"]), (1, 2))

    def test_resolve_row_keep_and_delete(self):
        v = self.make_order()
        oid = v["order_id"]
        v = self.svc.apply_actions(oid, 1, [
            {"type": "resolve_row", "conflict_id": "c1", "keep": True},  # 默认保留修改侧(目标侧)
            {"type": "resolve_row", "conflict_id": "c3", "keep": True, "side": "source"},
        ])
        d3 = next(r for r in v["candidates"]["datasets"] if r["id"] == "D3")
        self.assertEqual(d3["title"], "旧数据(目标改)")
        r7 = next(r for r in v["candidates"]["records"] if r["id"] == "R7")
        self.assertEqual(r7["value"], "来源侧")
        # 再删除 D3(先裁决保留、再显式删行是允许的修正路径)
        v = self.svc.apply_actions(oid, 2, [{"type": "delete_row", "table": "datasets", "id": "D3"}])
        d_ids = {r["id"] for r in v["candidates"]["datasets"]}
        self.assertNotIn("D3", d_ids)

    def test_resolve_twice_rejected(self):
        v = self.make_order()
        oid = v["order_id"]
        self.svc.apply_actions(oid, 1, [{"type": "resolve_row", "conflict_id": "c1", "keep": False}])
        with self.assertRaises(ApiError) as cm:
            self.svc.apply_actions(oid, 2, [{"type": "resolve_row", "conflict_id": "c1", "keep": True}])
        self.assertEqual(cm.exception.code, "CONFLICT_RESOLVED")

    def test_add_add_keep_requires_choice(self):
        v = self.make_order()
        with self.assertRaises(ApiError) as cm:
            self.svc.apply_actions(v["order_id"], 1, [{"type": "resolve_row", "conflict_id": "c3", "keep": True}])
        self.assertEqual(cm.exception.code, "INVALID_ACTION")

    def test_resolve_field_null_on_notnull_rejected(self):
        # 构造 subject(非空且属唯一键)上的字段冲突: 基线 S-01, 来源 S-02, 目标 S-03
        with tempfile.TemporaryDirectory() as td:
            svc = MergeService(os.path.join(td, "t.db"))
            try:
                svc.initialize({
                    "datasets": [ds("D1", "DS-001", "1.0")],
                    "records": [rec("R1", subj="S-03")],
                })
                baseline = {"datasets": [ds("D1", "DS-001", "1.0")], "records": [rec("R1", subj="S-01")]}
                source = {"datasets": [ds("D1", "DS-001", "1.0")], "records": [rec("R1", subj="S-02")]}
                v = svc.create_order(baseline, source)
                c1 = v["conflicts"][0]
                self.assertEqual(c1["field"], "subject")
                with self.assertRaises(ApiError) as cm:
                    svc.apply_actions(v["order_id"], 1, [{"type": "resolve_field", "conflict_id": "c1", "value": None}])
                self.assertEqual(cm.exception.code, "NULL_NOT_ALLOWED")
            finally:
                svc.close()

    def test_upsert_validation(self):
        v = self.make_order()
        oid = v["order_id"]
        # 未知表
        with self.assertRaises(ApiError) as cm:
            self.svc.apply_actions(oid, 1, [{"type": "upsert_row", "table": "nope", "values": {}}])
        self.assertEqual(cm.exception.code, "UNKNOWN_TABLE")
        # 缺字段
        with self.assertRaises(ApiError) as cm:
            self.svc.apply_actions(oid, 1, [{"type": "upsert_row", "table": "records", "values": {"id": "RX"}}])
        self.assertEqual(cm.exception.code, "INVALID_ROW")
        # 非空约束
        bad = rec("RX", subj=None)
        with self.assertRaises(ApiError) as cm:
            self.svc.apply_actions(oid, 1, [{"type": "upsert_row", "table": "records", "values": bad}])
        self.assertEqual(cm.exception.code, "INVALID_ROW")
        # 非字符串值(布尔)
        bad = rec("RX", value=True)
        with self.assertRaises(ApiError):
            self.svc.apply_actions(oid, 1, [{"type": "upsert_row", "table": "records", "values": bad}])
        # 伪造未决标记(对象值)
        bad = rec("RX", value={"$pending": "c9"})
        with self.assertRaises(ApiError):
            self.svc.apply_actions(oid, 1, [{"type": "upsert_row", "table": "records", "values": bad}])
        self.assertEqual(self.svc.get_order(oid)["revision"], 1)  # 全部拒绝, 状态未变

    def test_upsert_pending_row_rejected(self):
        v = self.make_order()
        with self.assertRaises(ApiError) as cm:
            self.svc.apply_actions(v["order_id"], 1, [{"type": "upsert_row", "table": "records", "values": rec("R1", value="8")}])
        self.assertEqual(cm.exception.code, "ROW_HAS_PENDING_CONFLICTS")

    def test_delete_row_rules(self):
        v = self.make_order()
        oid = v["order_id"]
        # 删除含未决字段的行 -> 拒绝
        with self.assertRaises(ApiError) as cm:
            self.svc.apply_actions(oid, 1, [{"type": "delete_row", "table": "records", "id": "R1"}])
        self.assertEqual(cm.exception.code, "ROW_HAS_PENDING_CONFLICTS")
        # 删除不存在的行 -> 404
        with self.assertRaises(ApiError) as cm:
            self.svc.apply_actions(oid, 1, [{"type": "delete_row", "table": "records", "id": "NOPE"}])
        self.assertEqual(cm.exception.code, "ROW_NOT_FOUND")
        # 正常删除
        v = self.svc.apply_actions(oid, 1, [{"type": "delete_row", "table": "records", "id": "R6"}])
        ids = {r["id"] for r in v["candidates"]["records"]}
        self.assertNotIn("R6", ids)
        self.assertEqual(v["revision"], 2)

    def test_empty_actions_rejected(self):
        v = self.make_order()
        with self.assertRaises(ApiError) as cm:
            self.svc.apply_actions(v["order_id"], 1, [])
        self.assertEqual(cm.exception.code, "EMPTY_ACTIONS")


class TestIntegrity(ServiceTestBase):
    def test_violations_listed_and_fixed_manually(self):
        v = self.make_order()
        oid = v["order_id"]
        v = self.resolve_all(oid)
        rev = v["revision"]
        # 暂存悬空引用(允许), 校验报告应列出相关行
        v = self.svc.apply_actions(oid, rev, [
            {"type": "upsert_row", "table": "records", "values": rec("R5", "DS-999", "9.9", "S-03", "A")}
        ])
        self.assertEqual(v["integrity"]["status"], "violations")
        dang = v["integrity"]["dangling_references"]
        self.assertEqual(len(dang), 1)
        self.assertEqual(dang[0]["row_id"], "R5")
        self.assertEqual(dang[0]["fields"], {"dataset_code": "DS-999", "dataset_version": "9.9"})
        # 暂存重复组合键, 校验报告应列出重复键组及相关行
        v = self.svc.apply_actions(oid, v["revision"], [
            {"type": "upsert_row", "table": "records", "values": rec("R6", "DS-002", "2.0", "S-09", "dup")}
        ])
        dups = v["integrity"]["duplicate_keys"]
        self.assertEqual(len(dups), 1)
        self.assertEqual(dups[0]["key"], {"dataset_code": "DS-002", "dataset_version": "2.0", "subject": "S-09"})
        self.assertEqual(dups[0]["row_ids"], ["R3", "R6"])
        # 手工修正后重新校验通过(系统不自动修)
        v = self.svc.apply_actions(oid, v["revision"], [
            {"type": "upsert_row", "table": "records", "values": rec("R5", subj="S-03", value="A")},
            {"type": "upsert_row", "table": "records", "values": rec("R6", "DS-002", "2.0", "S-11", "T")},
        ])
        self.assertEqual(v["integrity"]["status"], "ok")

    def test_cycles_allowed(self):
        # 目标数据本身含 R1<->R2 与 D1->R2->D1 环, 交付级校验应通过
        v = self.make_order()
        v = self.resolve_all(v["order_id"])
        self.assertEqual(v["integrity"]["status"], "ok")


class TestDelivery(ServiceTestBase):
    def test_full_delivery_flow(self):
        v = self.make_order()
        oid = v["order_id"]
        v = self.resolve_all(oid)
        rev = v["revision"]
        # 旧修订交付 -> 拒绝(不得检查一个修订却交付另一个)
        with self.assertRaises(ApiError) as cm:
            self.svc.deliver("req-1", oid, 1)
        self.assertEqual(cm.exception.code, "STALE_REVISION")
        # 正确交付
        r = self.svc.deliver("req-1", oid, rev)
        self.assertFalse(r["idempotent_replay"])
        receipt = r["receipt"]
        self.assertEqual(receipt["target_version"], 2)
        self.assertEqual(receipt["previous_target_version"], 1)
        self.assertEqual(receipt["tables"]["records"]["rows"], 6)
        # 目标已整体替换且升版
        t = self.svc.get_target()
        self.assertEqual(t["version"], 2)
        ids = {r["id"] for r in t["data"]["records"]}
        self.assertEqual(ids, {"R1", "R2", "R3", "R5", "R6", "R7"})
        r1 = next(r for r in t["data"]["records"] if r["id"] == "R1")
        self.assertEqual(r1["value"], "8")
        # 已交付单不可修改
        with self.assertRaises(ApiError) as cm:
            self.svc.apply_actions(oid, rev, [{"type": "delete_row", "table": "records", "id": "R6"}])
        self.assertEqual(cm.exception.code, "ORDER_DELIVERED")
        # 相同交付请求重试 -> 返回原回执
        r2 = self.svc.deliver("req-1", oid, rev)
        self.assertTrue(r2["idempotent_replay"])
        self.assertEqual(r2["receipt"], receipt)
        # 同键不同参数 -> 冲突
        with self.assertRaises(ApiError) as cm:
            self.svc.deliver("req-1", oid, 1)
        self.assertEqual(cm.exception.code, "REQUEST_ID_CONFLICT")
        # 不同请求重复交付同一单 -> 拒绝
        with self.assertRaises(ApiError) as cm:
            self.svc.deliver("req-2", oid, rev)
        self.assertEqual(cm.exception.code, "ORDER_DELIVERED")

    def test_delivery_blocked_by_pending_conflicts(self):
        v = self.make_order()
        with self.assertRaises(ApiError) as cm:
            self.svc.deliver("req-1", v["order_id"], 1)
        self.assertEqual(cm.exception.code, "CONFLICTS_PENDING")
        self.assertEqual(self.svc.get_target()["version"], 1)  # 状态未变

    def test_delivery_blocked_by_violations(self):
        v = self.make_order()
        oid = v["order_id"]
        v = self.resolve_all(oid)
        v = self.svc.apply_actions(oid, v["revision"], [
            {"type": "upsert_row", "table": "records", "values": rec("R5", "DS-999", "9.9", "S-03")}
        ])
        with self.assertRaises(ApiError) as cm:
            self.svc.deliver("req-1", oid, v["revision"])
        self.assertEqual(cm.exception.code, "INTEGRITY_VIOLATIONS")
        self.assertEqual(self.svc.get_target()["version"], 1)  # 目标未被部分替换

    def test_target_changed_requires_new_order(self):
        v1 = self.make_order()
        v2 = self.svc.create_order(BASELINE, SOURCE)  # 与 v1 并存
        v1 = self.resolve_all(v1["order_id"])
        self.svc.deliver("req-1", v1["order_id"], v1["revision"])
        v2 = self.resolve_all(v2["order_id"])
        with self.assertRaises(ApiError) as cm:
            self.svc.deliver("req-2", v2["order_id"], v2["revision"])
        self.assertEqual(cm.exception.code, "TARGET_CHANGED")
        # 目标保持 v1 交付结果, 未受影响
        self.assertEqual(self.svc.get_target()["version"], 2)

    def test_idempotent_replay_after_target_changed(self):
        v1 = self.make_order()
        v1 = self.resolve_all(v1["order_id"])
        r1 = self.svc.deliver("req-1", v1["order_id"], v1["revision"])["receipt"]
        # 第二个单基于新版本建单并交付, 目标再次变化
        t = self.svc.get_target()
        baseline2 = t["data"]
        v2 = self.svc.create_order(baseline2, baseline2)  # 无变化, 无冲突
        self.assertEqual(v2["integrity"]["status"], "ok")
        self.svc.deliver("req-2", v2["order_id"], v2["revision"])
        self.assertEqual(self.svc.get_target()["version"], 3)
        # 目标已变, 但重试原交付请求仍返回原回执
        r = self.svc.deliver("req-1", v1["order_id"], v1["revision"])
        self.assertTrue(r["idempotent_replay"])
        self.assertEqual(r["receipt"], r1)


class TestRestart(ServiceTestBase):
    def test_restart_preserves_state(self):
        v = self.make_order()
        oid = v["order_id"]
        self.svc.apply_actions(oid, 1, [{"type": "resolve_field", "conflict_id": "c2", "value": "8"}])
        self.svc.close()
        # 重启: 同一数据库文件重新打开
        self.svc = MergeService(self.db)
        v = self.svc.get_order(oid)
        self.assertEqual(v["revision"], 2)
        c2 = next(c for c in v["conflicts"] if c["conflict_id"] == "c2")
        self.assertEqual(c2["status"], "resolved")
        self.assertEqual(len(self.svc.get_history(oid)["history"]), 1)
        # 重启后继续裁决并交付
        v = self.svc.apply_actions(oid, 2, [
            {"type": "resolve_row", "conflict_id": "c1", "keep": False},
            {"type": "resolve_row", "conflict_id": "c3", "keep": True, "side": "target"},
        ])
        r = self.svc.deliver("req-1", oid, v["revision"])
        self.svc.close()
        # 再次重启: 交付结果与回执仍在
        self.svc = MergeService(self.db)
        self.assertEqual(self.svc.get_target()["version"], 2)
        r2 = self.svc.deliver("req-1", oid, v["revision"])
        self.assertTrue(r2["idempotent_replay"])
        self.assertEqual(r2["receipt"], r["receipt"])
        self.assertEqual(self.svc.get_order(oid)["status"], "delivered")


if __name__ == "__main__":
    unittest.main()
