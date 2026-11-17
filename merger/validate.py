"""关系校验: 结构检查、组合唯一键重复组、悬空引用。

校验只报告问题, 绝不自动删行或清空引用; 消除问题只能由数据管理员
通过裁决或修正候选行显式完成。循环引用不算违规。
"""

from .schema import TABLES, TABLE_ORDER


def check_row_structure(table, row):
    """检查单行是否符合字段结构与非空要求, 返回问题列表(空列表表示通过)。"""
    spec = TABLES[table]
    if not isinstance(row, dict):
        return ["行必须是对象"]
    problems = []
    fields = set(spec["fields"])
    keys = set(row.keys())
    missing = sorted(fields - keys)
    extra = sorted(keys - fields)
    if missing:
        problems.append(f"缺少字段: {missing}")
    if extra:
        problems.append(f"未知字段: {extra}")
    for f in spec["fields"]:
        if f in row:
            v = row[f]
            if v is not None and not isinstance(v, str):
                problems.append(f"字段 {f} 的值必须是字符串或空值")
    for f in spec["not_null"]:
        if f in row and row[f] is None:
            problems.append(f"字段 {f} 不允许为空")
    for uk in spec["unique_keys"]:
        for f in uk:
            if f in row and row[f] is None:
                problems.append(f"组合唯一键字段 {f} 不得含空值")
    return problems


def validate_dataset(data):
    """对完整数据集(或候选集)做全量校验, 返回校验报告。

    data: {表名: {行id: 行字典}}。报告列出全部重复键组与悬空引用及相关行。
    """
    structural = []
    for table in TABLE_ORDER:
        for rid, row in data.get(table, {}).items():
            probs = check_row_structure(table, row)
            if probs:
                structural.append({"table": table, "row_id": rid, "problems": probs})

    duplicates = []
    for table in TABLE_ORDER:
        for uk in TABLES[table]["unique_keys"]:
            groups = {}
            for rid, row in data.get(table, {}).items():
                key = tuple(row.get(f) for f in uk)
                if any(not isinstance(v, str) for v in key):
                    continue  # 空值/非法值属于结构问题, 已在上面报告
                groups.setdefault(key, []).append(rid)
            for key in sorted(groups):
                rids = groups[key]
                if len(rids) > 1:
                    duplicates.append(
                        {
                            "table": table,
                            "key": dict(zip(uk, key)),
                            "row_ids": sorted(rids),
                        }
                    )

    dangling = []
    for table in TABLE_ORDER:
        for ref in TABLES[table]["references"]:
            ref_table = ref["ref_table"]
            ref_fields = ref["ref_fields"]
            keyset = set()
            for row in data.get(ref_table, {}).values():
                key = tuple(row.get(f) for f in ref_fields)
                if all(isinstance(v, str) for v in key):
                    keyset.add(key)
            for rid in sorted(data.get(table, {})):
                row = data[table][rid]
                vals = tuple(row.get(f) for f in ref["fields"])
                if any(v is None for v in vals):
                    continue  # 引用字段为空视为无引用
                if not all(isinstance(v, str) for v in vals):
                    continue  # 非法值属于结构问题
                if vals not in keyset:
                    dangling.append(
                        {
                            "table": table,
                            "row_id": rid,
                            "reference": ref["name"],
                            "fields": dict(zip(ref["fields"], vals)),
                            "ref_table": ref_table,
                            "ref_fields": ref_fields,
                        }
                    )

    ok = not structural and not duplicates and not dangling
    return {
        "status": "ok" if ok else "violations",
        "structural_violations": structural,
        "duplicate_keys": duplicates,
        "dangling_references": dangling,
    }


def order_integrity(candidates, conflicts):
    """审定单完整性: 有未决冲突时为"待定", 否则对候选集做全量校验。"""
    pending = [c for c in conflicts if c["status"] == "pending"]
    if pending:
        return {
            "status": "pending",
            "pending_conflicts": len(pending),
            "pending_conflict_ids": [c["conflict_id"] for c in pending],
        }
    return validate_dataset(candidates)
