"""三方逐字段合并引擎。

输入三个快照(共同基线 baseline / 来源修订 source / 当前目标 target),
均为 {表名: {行id: 行字典}} 结构, 按行身份(id)对齐后逐字段合并:

- 两侧相同 -> 合一采用;
- 仅一侧相对基线变化 -> 采用该侧;
- 双方改成不同值 -> 产生 field 冲突, 候选行该字段写入未决标记;
- 两侧均删除, 或单侧删除且另一侧未改 -> 删除(无候选行);
- 一侧删除而另一侧修改 -> delete_modify 行级冲突;
- 同标识不同新增 -> add_add 行级冲突;
- 其余单侧新增 -> 保留。

未决标记为 {"$pending": "<冲突id>"} 的对象, 与空值(null)和删除(行缺席)
都可明确区分; 合法字段值只会是字符串或空值, 因此不会与未决标记混淆。
"""

from .schema import TABLES, TABLE_ORDER

PENDING_KEY = "$pending"


def pending_marker(conflict_id):
    return {PENDING_KEY: conflict_id}


def is_pending(value):
    return isinstance(value, dict) and PENDING_KEY in value


def row_has_pending(row):
    return any(is_pending(v) for v in row.values())


def merge_datasets(baseline, source, target):
    """返回 (candidates, conflicts)。

    candidates: {表名: {行id: 行字典}}, 未决字段内含未决标记;
    conflicts: 冲突字典列表, id 按表顺序、行 id、字段顺序确定性地编号。
    """
    candidates = {}
    conflicts = []
    seq = 0

    def next_conflict_id():
        nonlocal seq
        seq += 1
        return f"c{seq}"

    for table in TABLE_ORDER:
        fields = TABLES[table]["fields"]
        b = baseline.get(table, {})
        s = source.get(table, {})
        t = target.get(table, {})
        cand = {}
        for rid in sorted(set(b) | set(s) | set(t)):
            rb, rs, rt = b.get(rid), s.get(rid), t.get(rid)
            if rs is not None and rt is not None:
                if rs == rt:
                    cand[rid] = dict(rs)  # 相同值合一
                elif rb is not None and rs == rb:
                    cand[rid] = dict(rt)  # 仅目标侧变化
                elif rb is not None and rt == rb:
                    cand[rid] = dict(rs)  # 仅来源侧变化
                elif rb is None:
                    cid = next_conflict_id()  # 同标识不同新增, 须裁决
                    conflicts.append(
                        {
                            "conflict_id": cid,
                            "table": table,
                            "row_id": rid,
                            "field": None,
                            "kind": "add_add",
                            "detail": {"source": dict(rs), "target": dict(rt)},
                        }
                    )
                else:
                    merged = {}
                    for f in fields:
                        sv, tv, bv = rs[f], rt[f], rb[f]
                        if sv == tv:
                            merged[f] = sv
                        elif sv == bv:
                            merged[f] = tv
                        elif tv == bv:
                            merged[f] = sv
                        else:
                            cid = next_conflict_id()  # 双方改成不同值, 须裁决
                            merged[f] = pending_marker(cid)
                            conflicts.append(
                                {
                                    "conflict_id": cid,
                                    "table": table,
                                    "row_id": rid,
                                    "field": f,
                                    "kind": "field",
                                    "detail": {"base": bv, "source": sv, "target": tv},
                                }

                            )
                    cand[rid] = merged
            elif rs is not None:
                if rb is None:
                    cand[rid] = dict(rs)  # 来源侧新增, 保留
                elif rs == rb:
                    pass  # 目标删除且来源未改 -> 删除
                else:
                    cid = next_conflict_id()  # 目标删除 vs 来源修改, 删改冲突
                    conflicts.append(
                        {
                            "conflict_id": cid,
                            "table": table,
                            "row_id": rid,
                            "field": None,
                            "kind": "delete_modify",
                            "detail": {
                                "deleted_side": "target",
                                "modified_side": "source",
                                "base": dict(rb),
                                "modified": dict(rs),
                            },
                        }
                    )
            elif rt is not None:
                if rb is None:
                    cand[rid] = dict(rt)  # 目标侧新增, 保留
                elif rt == rb:
                    pass  # 来源删除且目标未改 -> 删除
                else:
                    cid = next_conflict_id()  # 来源删除 vs 目标修改, 删改冲突
                    conflicts.append(
                        {
                            "conflict_id": cid,
                            "table": table,
                            "row_id": rid,
                            "field": None,
                            "kind": "delete_modify",
                            "detail": {
                                "deleted_side": "source",
                                "modified_side": "target",
                                "base": dict(rb),
                                "modified": dict(rt),
                            },
                        }
                    )
            # 两侧均删除(或均不存在) -> 删除, 无候选行
        candidates[table] = cand
    for c in conflicts:
        c["status"] = "pending"
        c["resolution"] = None
        c["resolved_at"] = None
    return candidates, conflicts
