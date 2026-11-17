"""固定表结构定义。

约定:
- 所有字段值均为字符串或空值(None), 不允许其他类型;
- 行身份由 id 字段(稳定行标识)确定, 合并、裁决、校验均按 id 对齐;
- 非空约束列于 not_null; 组合唯一键列于 unique_keys, 唯一键字段不得含空值;
- 跨表引用列于 references; 引用字段整体为空视为"无引用", 允许循环引用
  (datasets.lead_record_id -> records.id 与 records -> datasets 可构成跨表环,
  records.next_record_id -> records.id 可在表内成环), 环不属于违规。
"""

TABLES = {
    "datasets": {
        "fields": ["id", "code", "version", "title", "lead_record_id"],
        "not_null": ["id", "code", "version"],
        "primary_key": ["id"],
        "unique_keys": [["code", "version"]],
        "references": [
            {
                "name": "datasets_lead_record",
                "fields": ["lead_record_id"],
                "ref_table": "records",
                "ref_fields": ["id"],
            }
        ],
    },
    "records": {
        "fields": [
            "id",
            "dataset_code",
            "dataset_version",
            "subject",
            "value",
            "next_record_id",
        ],
        "not_null": ["id", "dataset_code", "dataset_version", "subject"],
        "primary_key": ["id"],
        "unique_keys": [["dataset_code", "dataset_version", "subject"]],
        "references": [
            {
                "name": "records_dataset",
                "fields": ["dataset_code", "dataset_version"],
                "ref_table": "datasets",
                "ref_fields": ["code", "version"],
            },
            {
                "name": "records_next",
                "fields": ["next_record_id"],
                "ref_table": "records",
                "ref_fields": ["id"],
            },
        ],
    },
}

# 固定表顺序: 交付时按此顺序整体替换, 校验/合并亦按此顺序遍历, 保证结果确定。
TABLE_ORDER = ["datasets", "records"]
