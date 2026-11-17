"""按行身份的三方合并（base / src / tgt 均为规范化快照）。

规则：
- 逐字段：两侧相同合一；仅一侧相对基线变化取该侧；两侧改成不同值 → 字段冲突；
- 两侧均删除、或一侧删除且另一侧未改 → 删除；
- 一侧删除而另一侧修改 → 行级删改冲突（候选行整体置为未决，待人工保留或删除）；
- 同标识新增：内容一致直接保留，不一致的字段 → 字段冲突（both_added）；
- 仅一侧新增 → 保留。

未决用 pending 标记表示，与“空值”（value=None, pending=False）和
“删除”（候选中不存在该行）严格区分。
"""
from .schema import TABLES, TABLE_ORDER


def merge_snapshots(base, src, tgt):
    """返回 (candidate, conflicts)。

    candidate: {table: {row_id: {"state": "present"|"pending",
                                 "cells": {field: (value, pending)}}}}
    conflicts: [{table, row_id, field|None, kind, base_value, src_value, tgt_value,
                 proposed_row?, conflict_id}]，conflict_id 按确定顺序从 1 编号。
    """
    candidate = {}
    conflicts = []
    for table in TABLE_ORDER:
        fields = TABLES[table]["fields"]
        b, s, t = base[table], src[table], tgt[table]
        cand_rows = {}
        for rid in sorted(set(b) | set(s) | set(t)):
            rb, rs, rt = b.get(rid), s.get(rid), t.get(rid)
            if rs is not None and rt is not None:
                cells = {}
                for f in fields:
                    sv, tv = rs[f], rt[f]
                    bv = rb[f] if rb is not None else None
                    if sv == tv:
                        cells[f] = (sv, False)
                    elif rb is not None and bv == sv:
                        cells[f] = (tv, False)   # 仅目标侧变化
                    elif rb is not None and bv == tv:
                        cells[f] = (sv, False)   # 仅来源侧变化
                    else:
                        cells[f] = (None, True)  # 未决：区别于空值
                        conflicts.append({
                            "table": table, "row_id": rid, "field": f,
                            "kind": "both_added" if rb is None else "both_modified",
                            "base_value": bv, "src_value": sv, "tgt_value": tv,
                        })
                cand_rows[rid] = {"state": "present", "cells": cells}
            elif rs is not None:
                if rb is None:
                    cand_rows[rid] = {"state": "present",
                                      "cells": {f: (rs[f], False) for f in fields}}
                elif rs == rb:
                    pass  # 目标侧删除且来源侧未改 → 删除
                else:
                    cand_rows[rid] = {"state": "pending",
                                      "cells": {f: (rs[f], False) for f in fields}}
                    conflicts.append({
                        "table": table, "row_id": rid, "field": None,
                        "kind": "delete_modified",
                        "base_value": None, "src_value": None, "tgt_value": None,
                        "proposed_row": dict(rs),
                    })
            elif rt is not None:
                if rb is None:
                    cand_rows[rid] = {"state": "present",
                                      "cells": {f: (rt[f], False) for f in fields}}
                elif rt == rb:
                    pass  # 来源侧删除且目标侧未改 → 删除
                else:
                    cand_rows[rid] = {"state": "pending",
                                      "cells": {f: (rt[f], False) for f in fields}}
                    conflicts.append({
                        "table": table, "row_id": rid, "field": None,
                        "kind": "modified_deleted",
                        "base_value": None, "src_value": None, "tgt_value": None,
                        "proposed_row": dict(rt),
                    })
            # 两侧均删除 → 删除（候选中不出现该行）
        candidate[table] = cand_rows
    for seq, c in enumerate(conflicts, 1):
        c["conflict_id"] = seq
    return candidate, conflicts
