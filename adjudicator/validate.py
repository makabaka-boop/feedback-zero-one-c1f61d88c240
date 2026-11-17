"""输入快照规范化与完整性校验（结构、非空、唯一键、跨表引用）。

校验只报告问题，绝不自动删行或清空引用。
"""
from .errors import ValidationError
from .schema import TABLES, TABLE_ORDER


def normalize_snapshot(data):
    """校验快照结构并规范化为 {table: {row_id: row}}；结构非法抛 ValidationError。

    输入格式：{table: [row, ...]}，row 为恰好包含全部字段的字典，值为 str 或 None。
    """
    if not isinstance(data, dict):
        raise ValidationError("snapshot_must_be_object")
    unknown = sorted(set(data) - set(TABLE_ORDER))
    if unknown:
        raise ValidationError("unknown_tables", {"tables": unknown})
    missing = sorted(set(TABLE_ORDER) - set(data))
    if missing:
        raise ValidationError("missing_tables", {"tables": missing})
    out = {}
    for table in TABLE_ORDER:
        spec = TABLES[table]
        pk, fields = spec["pk"], set(spec["fields"])
        rows = data[table]
        if not isinstance(rows, list):
            raise ValidationError("table_rows_must_be_list", {"table": table})
        keyed = {}
        for row in rows:
            if not isinstance(row, dict):
                raise ValidationError("row_must_be_object", {"table": table})
            extra = sorted(set(row) - fields)
            miss = sorted(fields - set(row))
            if extra or miss:
                raise ValidationError(
                    "bad_fields", {"table": table, "extra": extra, "missing": miss})
            rid = row[pk]
            if not isinstance(rid, str) or not rid:
                raise ValidationError("bad_pk", {"table": table, "pk": pk})
            for f, v in row.items():
                if v is not None and not isinstance(v, str):
                    raise ValidationError(
                        "bad_value_type", {"table": table, "row_id": rid, "field": f})
            if rid in keyed:
                raise ValidationError("duplicate_pk", {"table": table, "row_id": rid})
            keyed[rid] = dict(row)
        out[table] = keyed
    return out


def snapshot_violations(data):
    """对规范化快照 {table: {row_id: row}} 做完整性校验，返回全部违规。

    违规类型：not_null / null_in_unique_key / duplicate_unique_key / dangling_reference。
    """
    violations = []
    for table in TABLE_ORDER:
        spec = TABLES[table]
        rows = data[table]
        for rid in sorted(rows):
            for f in spec["not_null"]:
                if rows[rid].get(f) is None:
                    violations.append({"type": "not_null", "table": table,
                                       "row_id": rid, "field": f})
        for uk in spec["uniques"]:
            groups = {}
            for rid in sorted(rows):
                key = tuple(rows[rid].get(f) for f in uk)
                if any(v is None for v in key):
                    violations.append({"type": "null_in_unique_key", "table": table,
                                       "row_id": rid, "key_fields": list(uk)})
                    continue
                groups.setdefault(key, []).append(rid)
            for key in sorted(groups):
                rids = groups[key]
                if len(rids) > 1:
                    violations.append({"type": "duplicate_unique_key", "table": table,
                                       "key_fields": list(uk), "key": list(key),
                                       "row_ids": sorted(rids)})
    for table in TABLE_ORDER:
        for (field, ref_table, ref_field) in TABLES[table]["references"]:
            ref_rows = data[ref_table]
            for rid in sorted(data[table]):
                value = data[table][rid].get(field)
                if value is not None and value not in ref_rows:
                    violations.append({"type": "dangling_reference", "table": table,
                                       "row_id": rid, "field": field, "value": value,
                                       "ref_table": ref_table, "ref_field": ref_field})
    return violations
