#!/usr/bin/env python3
"""演示：初始化 → 建单 → 冲突裁决/候选修正 → 关系校验 → 原子交付 → 幂等重试。

运行：python3 demo.py
"""
import json
import os
import tempfile

from adjudicator import MergeService
from adjudicator.errors import ServiceError

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


def show(title, obj):
    print(f"\n=== {title} ===")
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def main():
    db = os.path.join(tempfile.mkdtemp(prefix="adjudicator-demo-"), "demo.db")
    svc = MergeService(db)

    show("1. 初始化合法目标数据", svc.initialize_target(TARGET0))

    order = svc.create_order("O-20261117-01", BASE, SOURCE, base_version=1)
    show("2. 建单：三方合并产生候选与冲突", {
        "current_rev": order["current_rev"],
        "conflicts": order["conflicts"],
        "integrity": order["integrity"],
        "candidate_teams": order["candidate"]["teams"],
        "candidate_researchers": order["candidate"]["researchers"],
    })

    order = svc.adjudicate("O-20261117-01", 1, 1,
                           {"choose": "value", "value": "核心审定组"})
    order = svc.adjudicate("O-20261117-01", 2, 2, {"choose": "keep"})
    order = svc.fix_row("O-20261117-01", 3, "researchers", "R3",
                        {"full_name": "钱七终审"})
    show("3. 裁决与修正后（修订号推进、完整性重估）", {
        "current_rev": order["current_rev"],
        "integrity": order["integrity"],
        "history": [h["action"] for h in order["history"]],
    })

    receipt = svc.deliver("O-20261117-01", 4, "REQ-20261117-01")
    show("4. 原子交付回执", receipt)

    show("5. 交付后目标（版本及完整数据）", svc.get_target())

    show("6. 相同请求重试 → 原回执", svc.deliver("O-20261117-01", 4, "REQ-20261117-01"))

    try:
        svc.deliver("O-20261117-01", 4, "REQ-OTHER")
    except ServiceError as e:
        show("7. 冲突请求拒绝", e.to_dict())


if __name__ == "__main__":
    main()
