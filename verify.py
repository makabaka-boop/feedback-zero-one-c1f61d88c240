#!/usr/bin/env python3
"""端到端验证：合并规则、裁决、候选修正、校验、交付门禁、幂等、持久化与并发。

运行：python3 verify.py
"""
import json
import os
import tempfile
import threading

from adjudicator import MergeService
from adjudicator.errors import (AlreadyInitialized, IntegrityViolation, NotFound,
                                OpenConflicts, OrderClosed, RequestConflict,
                                StaleRevision, TargetChanged, ValidationError)

_checks = 0


def check(cond, msg):
    global _checks
    if not cond:
        raise AssertionError("验证失败: " + msg)
    _checks += 1
    print("  ok -", msg)


def expect(exc, fn, msg):
    try:
        fn()
    except exc as e:
        check(True, f"{msg}（{e.code}）")
        return e
    raise AssertionError(f"{msg}: 未抛出期望异常 {exc.__name__}")


def new_db():
    return os.path.join(tempfile.mkdtemp(prefix="adjudicator-verify-"), "test.db")


# ---------------------------------------------------------------- 数据

TARGET0 = {
    "teams": [
        {"team_id": "T1", "team_name": "核心A组", "lead_id": "R1"},
        {"team_id": "T2", "team_name": "外联组", "lead_id": None},
    ],
    "researchers": [
        {"researcher_id": "R1", "full_name": "林澜", "team_id": "T1"},
        {"researcher_id": "R2", "full_name": "赵启", "team_id": "T2"},
    ],
    "datasets": [
        {"dataset_id": "D1", "title": "澜江水质2023", "owner_id": "R1", "project_code": "WQ"},
        {"dataset_id": "D2", "title": "齐山植被2023", "owner_id": "R2", "project_code": "VG"},
    ],
}

BASE = {
    "teams": [
        {"team_id": "T1", "team_name": "核心组", "lead_id": "R1"},
        {"team_id": "T2", "team_name": "外联组", "lead_id": None},
    ],
    "researchers": [
        {"researcher_id": "R1", "full_name": "林澜", "team_id": "T1"},
        {"researcher_id": "R2", "full_name": "赵齐", "team_id": "T2"},
        {"researcher_id": "R3", "full_name": "钱七", "team_id": "T2"},
    ],
    "datasets": [
        {"dataset_id": "D1", "title": "澜江水质2023", "owner_id": "R1", "project_code": "WQ"},
    ],
}

SOURCE = {
    "teams": [
        {"team_id": "T1", "team_name": "核心一组", "lead_id": None},
        {"team_id": "T2", "team_name": "外联组", "lead_id": None},
    ],
    "researchers": [
        {"researcher_id": "R1", "full_name": "林澜", "team_id": "T1"},
        {"researcher_id": "R2", "full_name": "赵齐", "team_id": "T2"},
        {"researcher_id": "R3", "full_name": "钱七改", "team_id": "T2"},
    ],
    "datasets": [
        {"dataset_id": "D1", "title": "澜江水质2024", "owner_id": "R1", "project_code": "WQ"},
    ],
}

EXPECTED_V2 = {
    "teams": [
        {"team_id": "T1", "team_name": "核心审定组", "lead_id": None},
        {"team_id": "T2", "team_name": "外联组", "lead_id": None},
    ],
    "researchers": [
        {"researcher_id": "R1", "full_name": "林澜", "team_id": "T1"},
        {"researcher_id": "R2", "full_name": "赵启", "team_id": "T2"},
        {"researcher_id": "R3", "full_name": "钱七终审", "team_id": "T2"},
    ],
    "datasets": [
        {"dataset_id": "D1", "title": "澜江水质2024", "owner_id": "R1", "project_code": "WQ"},
        {"dataset_id": "D2", "title": "齐山植被2023", "owner_id": "R2", "project_code": "VG"},
    ],
}

BASE6 = {
    "teams": [{"team_id": "T1", "team_name": "组", "lead_id": None}],
    "researchers": [
        {"researcher_id": "R1", "full_name": "甲", "team_id": "T1"},
        {"researcher_id": "R2", "full_name": "乙", "team_id": "T1"},
        {"researcher_id": "R3", "full_name": "丙", "team_id": "T1"},
        {"researcher_id": "R4", "full_name": "丁", "team_id": None},
    ],
    "datasets": [],
}
TGT6 = {
    "teams": [{"team_id": "T1", "team_name": "组", "lead_id": None}],
    "researchers": [
        {"researcher_id": "R1", "full_name": "甲", "team_id": "T1"},
        {"researcher_id": "R6", "full_name": "己", "team_id": "T1"},
        {"researcher_id": "R7", "full_name": "庚", "team_id": "T1"},
        {"researcher_id": "R8", "full_name": "辛-目标", "team_id": "T1"},
    ],
    "datasets": [],
}
SRC6 = {
    "teams": [{"team_id": "T1", "team_name": "组", "lead_id": None}],
    "researchers": [
        {"researcher_id": "R1", "full_name": "甲", "team_id": "T1"},
        {"researcher_id": "R5", "full_name": "戊", "team_id": "T1"},
        {"researcher_id": "R7", "full_name": "庚", "team_id": "T1"},
        {"researcher_id": "R8", "full_name": "辛-来源", "team_id": "T1"},
    ],
    "datasets": [],
}

_RESEARCHERS7 = [
    {"researcher_id": "R1", "full_name": "林", "team_id": None},
    {"researcher_id": "R2", "full_name": "赵", "team_id": None},
]
BASE7 = {
    "teams": [{"team_id": "T1", "team_name": "甲组", "lead_id": "R1"}],
    "researchers": json.loads(json.dumps(_RESEARCHERS7)),
    "datasets": [],
}
TGT7 = {
    "teams": [{"team_id": "T1", "team_name": "甲组", "lead_id": "R2"}],
    "researchers": json.loads(json.dumps(_RESEARCHERS7)),
    "datasets": [],
}
SRC7 = {
    "teams": [{"team_id": "T1", "team_name": "甲组", "lead_id": None}],
    "researchers": json.loads(json.dumps(_RESEARCHERS7)),
    "datasets": [],
}


# ---------------------------------------------------------------- 场景

def s1_init():
    print("[1] 初始化与输入校验")
    svc = MergeService(new_db())
    bad_ref = {
        "teams": [{"team_id": "T1", "team_name": "组", "lead_id": None}],
        "researchers": [{"researcher_id": "R1", "full_name": "林", "team_id": "TX"}],
        "datasets": [],
    }
    e = expect(ValidationError, lambda: svc.initialize_target(bad_ref), "非法种子（悬空引用）拒绝")
    check(e.details["violations"][0]["type"] == "dangling_reference", "违规详情包含悬空引用")
    check(not svc.is_initialized(), "非法操作不改状态（仍未初始化）")
    bad_null = {
        "teams": [{"team_id": "T1", "team_name": None, "lead_id": None}],
        "researchers": [],
        "datasets": [],
    }
    expect(ValidationError, lambda: svc.initialize_target(bad_null), "非法种子（非空约束）拒绝")
    svc.initialize_target(TARGET0)
    t = svc.get_target()
    check(t["version"] == 1 and t["data"] == TARGET0, "初始化合法目标数据，查询返回版本及完整数据")
    expect(AlreadyInitialized, lambda: svc.initialize_target(TARGET0), "重复初始化拒绝")
    dup = json.loads(json.dumps(BASE))
    dup["datasets"].append({"dataset_id": "D9", "title": "重复", "owner_id": "R1", "project_code": "WQ"})
    e = expect(ValidationError, lambda: svc.create_order("OB", dup, SOURCE), "非法基线（重复唯一键）拒绝")
    check(any(v["type"] == "duplicate_unique_key" for v in e.details["base"]), "违规详情包含重复键组")
    expect(NotFound, lambda: svc.get_order("OB"), "失败建单不留痕")
    expect(ValidationError, lambda: svc.create_order("OB2", {"teams": []}, SOURCE), "缺表拒绝")
    bad_type = json.loads(json.dumps(BASE))
    bad_type["teams"][0]["team_name"] = 5
    expect(ValidationError, lambda: svc.create_order("OB3", bad_type, SOURCE), "非字符串值拒绝")
    dup_pk = json.loads(json.dumps(BASE))
    dup_pk["researchers"].append(dict(dup_pk["researchers"][0]))
    expect(ValidationError, lambda: svc.create_order("OB4", dup_pk, SOURCE), "重复主键拒绝")
    svc.close()


def s2_merge():
    print("[2] 三方合并规则")
    svc = MergeService(new_db())
    svc.initialize_target(TARGET0)
    o = svc.create_order("O1", BASE, SOURCE, base_version=1)
    check(o["current_rev"] == 1 and o["status"] == "open", "建单后修订号为 1")
    check(o["target_version"] == 1 and o["base_version"] == 1, "建单固定目标版本与基线版本")
    teams = {r["row_id"]: r for r in o["candidate"]["teams"]}
    t1 = teams["T1"]["fields"]
    check(t1["team_name"]["pending"] is True, "双方改成不同值 → 字段未决")
    check(t1["lead_id"]["value"] is None and t1["lead_id"]["pending"] is False,
          "单侧改为空值 → 采用该侧（空值≠未决）")
    researchers = {r["row_id"]: r for r in o["candidate"]["researchers"]}
    check(researchers["R2"]["fields"]["full_name"]["value"] == "赵启", "仅目标侧变化取目标侧")
    check(researchers["R3"]["state"] == "pending" and researchers["R3"]["pending"] is True,
          "删改冲突 → 整行未决（区别于删除）")
    datasets = {r["row_id"]: r for r in o["candidate"]["datasets"]}
    check(datasets["D1"]["fields"]["title"]["value"] == "澜江水质2024", "仅来源侧变化取来源侧")
    check("D2" in datasets, "单侧新增保留")
    cs = o["conflicts"]
    check(len(cs) == 2, "冲突数量正确")
    c1, c2 = cs
    check(c1["kind"] == "both_modified" and c1["field"] == "team_name"
          and c1["base_value"] == "核心组" and c1["src_value"] == "核心一组"
          and c1["tgt_value"] == "核心A组", "字段冲突记录三方值")
    check(c2["kind"] == "delete_modified" and c2["field"] is None
          and c2["proposed_row"]["full_name"] == "钱七改", "删改冲突记录修改侧内容")
    check(o["integrity"]["status"] == "pending" and o["integrity"]["open_conflicts"] == 2,
          "有未决冲突时完整性待定")
    svc.close()


def s3_adjudicate():
    print("[3] 冲突裁决与历史")
    svc = MergeService(new_db())
    svc.initialize_target(TARGET0)
    svc.create_order("O1", BASE, SOURCE)
    expect(StaleRevision, lambda: svc.adjudicate("O1", 0, 1, {"choose": "src"}), "旧修订裁决拒绝")
    expect(ValidationError, lambda: svc.adjudicate("O1", 1, 1, {"choose": "value", "value": None}),
           "非空字段裁决为空拒绝")
    expect(ValidationError, lambda: svc.adjudicate("O1", 1, 1, {"choose": "keep"}),
           "字段冲突误用行裁决拒绝")
    check(svc.get_order("O1")["current_rev"] == 1, "非法操作不推进修订号")
    o = svc.adjudicate("O1", 1, 1, {"choose": "value", "value": "核心审定组"})
    check(o["current_rev"] == 2, "成功裁决推进修订号")
    c1 = o["conflicts"][0]
    check(c1["status"] == "resolved" and c1["resolution"] == "value"
          and c1["resolved_value"] == "核心审定组" and c1["resolved_rev"] == 2, "裁决结果留痕")
    t1 = {r["row_id"]: r for r in o["candidate"]["teams"]}["T1"]
    check(t1["fields"]["team_name"]["value"] == "核心审定组"
          and t1["fields"]["team_name"]["pending"] is False, "候选字段按裁决更新")
    expect(RequestConflict, lambda: svc.adjudicate("O1", 2, 1, {"choose": "src"}), "重复裁决拒绝")
    o = svc.adjudicate("O1", 2, 2, {"choose": "keep"})
    researchers = {r["row_id"]: r for r in o["candidate"]["researchers"]}
    check(researchers["R3"]["state"] == "present", "行冲突裁决保留")
    o = svc.fix_row("O1", 3, "researchers", "R3", {"full_name": "钱七终审"})
    check({r["row_id"]: r for r in o["candidate"]["researchers"]}["R3"]["fields"]["full_name"]["value"]
          == "钱七终审", "修正候选行生效")
    check(len(o["conflicts"]) == 2 and all(c["status"] == "resolved" for c in o["conflicts"]),
          "原冲突全部保留")
    actions = [h["action"] for h in o["history"]]
    check(actions == ["create", "adjudicate", "adjudicate", "fix_row"], "裁决历史保留")
    check(o["integrity"]["status"] == "valid", "冲突清零且关系合法")
    svc.close()


def s4_fixrow():
    print("[4] 候选修正的边界")
    svc = MergeService(new_db())
    svc.initialize_target(TARGET0)
    svc.create_order("O1", BASE, SOURCE)
    expect(RequestConflict, lambda: svc.fix_row("O1", 1, "teams", "T1", {"team_name": "x"}),
           "含未决冲突的行禁止直接修正")
    expect(NotFound, lambda: svc.fix_row("O1", 1, "nope", "X", {"a": "b"}), "未知表拒绝")
    expect(ValidationError, lambda: svc.fix_row("O1", 1, "datasets", "D1", {"junk": "x"}),
           "未知字段拒绝")
    expect(ValidationError, lambda: svc.fix_row("O1", 1, "datasets", "D1", {"dataset_id": "DX"}),
           "主键不可改")
    expect(ValidationError, lambda: svc.fix_row("O1", 1, "datasets", "D1", {"title": 3}),
           "非字符串值拒绝")
    expect(ValidationError, lambda: svc.fix_row("O1", 1, "datasets", "D1", {"title": None}),
           "非空约束拒绝")
    expect(ValidationError, lambda: svc.fix_row("O1", 1, "datasets", "D9", {"title": "新"}),
           "新建行缺非空字段拒绝")
    check(svc.get_order("O1")["current_rev"] == 1, "非法修正不推进修订号")
    o = svc.fix_row("O1", 1, "researchers", "R9", {"full_name": "王五", "team_id": "T9"})
    check(o["current_rev"] == 2, "合法修正（含暂存违规）推进修订号")
    check(o["integrity"]["status"] == "pending", "有未决冲突时完整性待定（即使存在暂存违规）")
    svc.adjudicate("O1", 2, 1, {"choose": "src"})
    svc.adjudicate("O1", 3, 2, {"choose": "delete"})
    o = svc.get_order("O1")
    check(o["integrity"]["status"] == "invalid"
          and any(v["type"] == "dangling_reference" and v["row_id"] == "R9"
                  for v in o["integrity"]["violations"]),
          "关系违规允许暂存并被校验列出")
    o = svc.fix_row("O1", 4, "researchers", "R9", {"team_id": "T2"})
    check(o["integrity"]["status"] == "valid", "修改后重新校验")
    svc.close()


def s5_delivery():
    print("[5] 交付门禁、原子性与幂等")
    svc = MergeService(new_db())
    svc.initialize_target(TARGET0)
    svc.create_order("O1", BASE, SOURCE)
    svc.create_order("O2", TARGET0, TARGET0)
    check(len(svc.list_orders()) == 2, "多审定单并存")
    e = expect(OpenConflicts, lambda: svc.deliver("O1", 1, "req-o1"), "未决冲突禁止交付")
    check(len(e.details["conflicts"]) == 2, "拒绝信息列出全部未决冲突")
    svc.fix_row("O2", 1, "datasets", "D2", {"owner_id": "R9"})
    e = expect(IntegrityViolation, lambda: svc.deliver("O2", 2, "req-o2"), "关系违规禁止交付")
    check(any(v["type"] == "dangling_reference" for v in e.details["violations"]), "违规详情完整")
    svc.fix_row("O2", 2, "datasets", "D2", {"owner_id": "R2"})
    svc.adjudicate("O1", 1, 1, {"choose": "value", "value": "核心审定组"})
    svc.adjudicate("O1", 2, 2, {"choose": "keep"})
    svc.fix_row("O1", 3, "researchers", "R3", {"full_name": "钱七终审"})
    svc.fix_row("O1", 4, "datasets", "D1", {"owner_id": "R9"})
    o = svc.get_order("O1")
    check(o["integrity"]["status"] == "invalid", "悬空引用被列出")
    svc.fix_row("O1", 5, "datasets", "D1", {"owner_id": "R1"})
    svc.fix_row("O1", 6, "datasets", "D2", {"owner_id": "R1", "project_code": "WQ"})
    o = svc.get_order("O1")
    dups = [v for v in o["integrity"]["violations"] if v["type"] == "duplicate_unique_key"]
    check(len(dups) == 1 and dups[0]["row_ids"] == ["D1", "D2"]
          and dups[0]["key"] == ["R1", "WQ"], "重复键组及相关行被列出")
    svc.fix_row("O1", 7, "datasets", "D2", {"owner_id": "R2", "project_code": "VG"})
    o = svc.get_order("O1")
    check(o["current_rev"] == 8 and o["integrity"]["status"] == "valid", "修改后重新校验通过")
    expect(StaleRevision, lambda: svc.deliver("O1", 7, "req-o1"), "旧修订交付拒绝")
    check(svc.get_target()["version"] == 1, "失败交付不改目标")
    receipt = svc.deliver("O1", 8, "req-o1")
    check(receipt["delivered_rev"] == 8 and receipt["previous_target_version"] == 1
          and receipt["new_target_version"] == 2, "交付的正是所检查的修订，回执内容正确")
    tgt = svc.get_target()
    check(tgt["version"] == 2 and tgt["data"] == EXPECTED_V2, "全部目标表原子替换并升版（无部分交付）")
    check(svc.get_order("O1")["receipt"] == receipt, "回执持久保存")
    expect(OrderClosed, lambda: svc.adjudicate("O1", 8, 1, {"choose": "src"}), "已交付单禁止裁决")
    expect(OrderClosed, lambda: svc.fix_row("O1", 8, "teams", "T1", {"team_name": "x"}),
           "已交付单禁止修正")
    expect(TargetChanged, lambda: svc.deliver("O2", 3, "req-o2"), "目标已变化须另建单")
    check(svc.deliver("O1", 8, "req-o1") == receipt, "相同交付请求重试返回原回执")
    expect(RequestConflict, lambda: svc.deliver("O1", 8, "req-x"), "冲突交付请求拒绝")
    mod = json.loads(json.dumps(EXPECTED_V2))
    mod["datasets"][0]["title"] = "澜江水质2024终审"
    svc.create_order("O3", EXPECTED_V2, mod)
    svc.deliver("O3", 1, "req-o3")
    check(svc.get_target()["version"] == 3, "后续交付正常升版")
    again = svc.deliver("O1", 8, "req-o1")
    check(again == receipt and again["new_target_version"] == 2, "目标变化后重试仍返回原回执")
    svc.close()


def s6_deletes_adds():
    print("[6] 删除与新增规则")
    svc = MergeService(new_db())
    svc.initialize_target(TGT6)
    o = svc.create_order("O1", BASE6, SRC6)
    ids = {r["row_id"] for r in o["candidate"]["researchers"]}
    check(ids == {"R1", "R5", "R6", "R7", "R8"}, "两侧删除/单侧删除未改均删除，单侧新增保留")
    cs = o["conflicts"]
    check(len(cs) == 1 and cs[0]["kind"] == "both_added" and cs[0]["row_id"] == "R8"
          and cs[0]["src_value"] == "辛-来源" and cs[0]["tgt_value"] == "辛-目标",
          "同标识不同新增须裁决")
    r8 = {r["row_id"]: r for r in o["candidate"]["researchers"]}["R8"]
    check(r8["fields"]["full_name"]["pending"] is True
          and r8["fields"]["team_id"]["value"] == "T1", "同标识新增：一致字段合一，差异字段未决")
    svc.adjudicate("O1", 1, 1, {"choose": "tgt"})
    svc.deliver("O1", 2, "req-6")
    names = {r["researcher_id"]: r["full_name"] for r in svc.get_target()["data"]["researchers"]}
    check(names == {"R1": "甲", "R5": "戊", "R6": "己", "R7": "庚", "R8": "辛-目标"}, "交付结果正确")
    svc.close()


def s7_null_pending():
    print("[7] 空值、未决与删除的区分")
    svc = MergeService(new_db())
    svc.initialize_target(TGT7)
    o = svc.create_order("O1", BASE7, SRC7)
    t1 = o["candidate"]["teams"][0]
    check(t1["fields"]["lead_id"]["pending"] is True and t1["fields"]["lead_id"]["value"] is None,
          "未决：值为空但 pending 标记区分于空值")
    c = o["conflicts"][0]
    check(c["src_value"] is None and c["tgt_value"] == "R2", "冲突三方值可含空值")
    expect(OpenConflicts, lambda: svc.deliver("O1", 1, "req-7"), "未决内容阻塞交付（区别于空值）")
    o = svc.adjudicate("O1", 1, 1, {"choose": "src"})
    t1 = o["candidate"]["teams"][0]
    check(t1["fields"]["lead_id"]["value"] is None and t1["fields"]["lead_id"]["pending"] is False,
          "裁决为空值：pending 清除")
    check(o["integrity"]["status"] == "valid", "空值本身合法")
    svc.deliver("O1", 2, "req-7")
    check(svc.get_target()["data"]["teams"][0]["lead_id"] is None, "空值可交付")
    svc.close()


def s8_restart():
    print("[8] 重启持久化")
    path = new_db()
    svc = MergeService(path)
    svc.initialize_target(TARGET0)
    svc.create_order("O1", BASE, SOURCE)
    svc.adjudicate("O1", 1, 1, {"choose": "src"})
    svc.close()

    svc = MergeService(path)
    o = svc.get_order("O1")
    check(o["current_rev"] == 2 and o["conflicts"][0]["status"] == "resolved",
          "重启保留已确认修订与裁决")
    check([h["action"] for h in o["history"]] == ["create", "adjudicate"], "重启保留历史")
    check(svc.get_snapshot("O1", "base") == BASE, "重启保留基线快照")
    svc.adjudicate("O1", 2, 2, {"choose": "delete"})
    receipt = svc.deliver("O1", 3, "req-restart")
    svc.close()

    svc = MergeService(path)
    check(svc.get_target()["version"] == 2, "重启保留目标版本")
    check(svc.deliver("O1", 3, "req-restart") == receipt, "重启保留交付回执（重试幂等）")
    svc.close()


def s9_concurrency():
    print("[9] 并发串行化")
    svc = MergeService(new_db())
    svc.initialize_target(TARGET0)
    svc.create_order("OC", BASE, SOURCE)

    def run_threads(n, fn):
        barrier = threading.Barrier(n)
        results = [None] * n

        def worker(i):
            barrier.wait()
            try:
                results[i] = ("ok", fn())
            except Exception as e:  # noqa: BLE001
                results[i] = ("err", e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return results

    results = run_threads(8, lambda: svc.adjudicate("OC", 1, 1, {"choose": "src"}))
    oks = [r for r in results if r[0] == "ok"]
    errs = [r for r in results if r[0] == "err"]
    check(len(oks) == 1 and all(isinstance(r[1], StaleRevision) for r in errs),
          "并发裁决恰好一个成功，其余因修订过期拒绝")
    check(svc.get_order("OC")["current_rev"] == 2, "修订号只推进一次")

    results = run_threads(8, lambda: svc.fix_row("OC", 1, "datasets", "D1", {"title": "x"}))
    check(all(r[0] == "err" and isinstance(r[1], StaleRevision) for r in results)
          and svc.get_order("OC")["current_rev"] == 2, "过期修订并发修改全部拒绝")

    svc.adjudicate("OC", 2, 2, {"choose": "delete"})

    results = run_threads(8, lambda: svc.deliver("OC", 3, "req-race"))
    receipts = [r[1] for r in results if r[0] == "ok"]
    check(len(receipts) == 8 and all(r == receipts[0] for r in receipts),
          "并发同一交付请求均返回原回执")
    check(svc.get_target()["version"] == 2, "目标只升版一次")

    results = run_threads(4, lambda: svc.deliver("OC", 3, "req-other"))
    check(all(r[0] == "err" and isinstance(r[1], RequestConflict) for r in results),
          "并发冲突请求全部拒绝")
    svc.close()


def main():
    scenarios = [s1_init, s2_merge, s3_adjudicate, s4_fixrow, s5_delivery,
                 s6_deletes_adds, s7_null_pending, s8_restart, s9_concurrency]
    for s in scenarios:
        s()
    print(f"\n全部 {len(scenarios)} 个场景、{_checks} 项检查通过。")


if __name__ == "__main__":
    main()
