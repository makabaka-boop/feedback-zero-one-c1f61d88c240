"""三方合并引擎的全分支单元测试。"""

import unittest

from merger.merge import is_pending, merge_datasets


def rec(rid, dc="DS-1", dv="1.0", subj="S-1", value=None, nxt=None):
    return {
        "id": rid,
        "dataset_code": dc,
        "dataset_version": dv,
        "subject": subj,
        "value": value,
        "next_record_id": nxt,
    }


def ds(rid, code="DS-1", ver="1.0", title=None, lead=None):
    return {"id": rid, "code": code, "version": ver, "title": title, "lead_record_id": lead}


def snap(datasets=(), records=()):
    return {
        "datasets": {r["id"]: r for r in datasets},
        "records": {r["id"]: r for r in records},
    }


class TestMergeRows(unittest.TestCase):
    def test_unchanged_kept(self):
        b = rec("R1", value="x")
        cand, conf = merge_datasets(snap(records=[b]), snap(records=[rec("R1", value="x")]), snap(records=[rec("R1", value="x")]))
        self.assertEqual(cand["records"]["R1"], b)
        self.assertEqual(conf, [])

    def test_source_only_change_taken(self):
        b = rec("R1", value="x")
        s = rec("R1", value="y")
        t = rec("R1", value="x")
        cand, conf = merge_datasets(snap(records=[b]), snap(records=[s]), snap(records=[t]))
        self.assertEqual(cand["records"]["R1"]["value"], "y")
        self.assertEqual(conf, [])

    def test_target_only_change_taken(self):
        b = rec("R1", value="x")
        s = rec("R1", value="x")
        t = rec("R1", value="z")
        cand, conf = merge_datasets(snap(records=[b]), snap(records=[s]), snap(records=[t]))
        self.assertEqual(cand["records"]["R1"]["value"], "z")
        self.assertEqual(conf, [])

    def test_both_same_change_merged(self):
        b = rec("R1", value="x")
        s = rec("R1", value="y")
        t = rec("R1", value="y")
        cand, conf = merge_datasets(snap(records=[b]), snap(records=[s]), snap(records=[t]))
        self.assertEqual(cand["records"]["R1"]["value"], "y")
        self.assertEqual(conf, [])

    def test_field_conflict_when_both_change_differently(self):
        b = rec("R1", value="x", nxt=None)
        s = rec("R1", value="y", nxt="R9")
        t = rec("R1", value="z", nxt="R9")
        cand, conf = merge_datasets(snap(records=[b]), snap(records=[s]), snap(records=[t]))
        row = cand["records"]["R1"]
        self.assertTrue(is_pending(row["value"]))  # 未决标记
        self.assertEqual(row["value"], {"$pending": "c1"})
        self.assertEqual(row["next_record_id"], "R9")  # 两侧一致字段正常合一
        self.assertEqual(len(conf), 1)
        c = conf[0]
        self.assertEqual(c["kind"], "field")
        self.assertEqual(c["field"], "value")
        self.assertEqual(c["detail"], {"base": "x", "source": "y", "target": "z"})

    def test_independent_field_changes_merge_cleanly(self):
        b = rec("R1", value="x", nxt=None)
        s = rec("R1", value="y", nxt=None)  # 来源改 value
        t = rec("R1", value="x", nxt="R2")  # 目标改 next
        cand, conf = merge_datasets(snap(records=[b]), snap(records=[s]), snap(records=[t]))
        self.assertEqual(cand["records"]["R1"]["value"], "y")
        self.assertEqual(cand["records"]["R1"]["next_record_id"], "R2")
        self.assertEqual(conf, [])

    def test_both_deleted(self):
        b = rec("R1")
        cand, conf = merge_datasets(snap(records=[b]), snap(), snap())
        self.assertNotIn("R1", cand["records"])
        self.assertEqual(conf, [])

    def test_source_deleted_target_unchanged_deleted(self):
        b = rec("R1", value="x")
        cand, conf = merge_datasets(snap(records=[b]), snap(), snap(records=[rec("R1", value="x")]))
        self.assertNotIn("R1", cand["records"])
        self.assertEqual(conf, [])

    def test_target_deleted_source_unchanged_deleted(self):
        b = rec("R1", value="x")
        cand, conf = merge_datasets(snap(records=[b]), snap(records=[rec("R1", value="x")]), snap())
        self.assertNotIn("R1", cand["records"])
        self.assertEqual(conf, [])

    def test_delete_modify_conflict_source_deleted(self):
        b = rec("R1", value="x")
        t = rec("R1", value="y")
        cand, conf = merge_datasets(snap(records=[b]), snap(), snap(records=[t]))
        self.assertNotIn("R1", cand["records"])  # 未决行不进入候选
        self.assertEqual(len(conf), 1)
        c = conf[0]
        self.assertEqual(c["kind"], "delete_modify")
        self.assertEqual(c["detail"]["deleted_side"], "source")
        self.assertEqual(c["detail"]["modified_side"], "target")
        self.assertEqual(c["detail"]["modified"], t)
        self.assertEqual(c["detail"]["base"], b)

    def test_delete_modify_conflict_target_deleted(self):
        b = rec("R1", value="x")
        s = rec("R1", value="y")
        cand, conf = merge_datasets(snap(records=[b]), snap(records=[s]), snap())
        self.assertNotIn("R1", cand["records"])
        c = conf[0]
        self.assertEqual(c["kind"], "delete_modify")
        self.assertEqual(c["detail"]["deleted_side"], "target")
        self.assertEqual(c["detail"]["modified"], s)

    def test_source_addition_kept(self):
        s = rec("R1", value="new")
        cand, conf = merge_datasets(snap(), snap(records=[s]), snap())
        self.assertEqual(cand["records"]["R1"], s)
        self.assertEqual(conf, [])

    def test_target_addition_kept(self):
        t = rec("R1", value="new")
        cand, conf = merge_datasets(snap(), snap(), snap(records=[t]))
        self.assertEqual(cand["records"]["R1"], t)
        self.assertEqual(conf, [])

    def test_identical_addition_merged(self):
        s = rec("R1", value="new")
        cand, conf = merge_datasets(snap(), snap(records=[rec("R1", value="new")]), snap(records=[rec("R1", value="new")]))
        self.assertEqual(cand["records"]["R1"], s)
        self.assertEqual(conf, [])

    def test_add_add_conflict(self):
        s = rec("R1", value="from-source")
        t = rec("R1", value="from-target")
        cand, conf = merge_datasets(snap(), snap(records=[s]), snap(records=[t]))
        self.assertNotIn("R1", cand["records"])
        self.assertEqual(len(conf), 1)
        c = conf[0]
        self.assertEqual(c["kind"], "add_add")
        self.assertEqual(c["detail"]["source"], s)
        self.assertEqual(c["detail"]["target"], t)

    def test_datasets_table_and_conflict_id_order(self):
        # 冲突编号按表顺序、行 id、字段顺序确定
        b_d = [ds("D1", title="t0"), ds("D2", title="x")]
        s_d = [ds("D1", title="t1"), ds("D2", title="y")]
        t_d = [ds("D1", title="t2"), ds("D2", title="z")]
        b_r = [rec("R1", value="a")]
        s_r = [rec("R1", value="b")]
        t_r = [rec("R1", value="c")]
        cand, conf = merge_datasets(snap(b_d, b_r), snap(s_d, s_r), snap(t_d, t_r))
        kinds = [(c["conflict_id"], c["table"], c["row_id"], c["kind"]) for c in conf]
        self.assertEqual(
            kinds,
            [("c1", "datasets", "D1", "field"), ("c2", "datasets", "D2", "field"), ("c3", "records", "R1", "field")],
        )
        self.assertTrue(is_pending(cand["datasets"]["D1"]["title"]))
        self.assertTrue(is_pending(cand["records"]["R1"]["value"]))


if __name__ == "__main__":
    unittest.main()
