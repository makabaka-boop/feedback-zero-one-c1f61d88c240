"""固定表结构定义。

约定：
- 行身份 = 主键值（稳定行标识），所有字段值为字符串或 NULL；
- teams.lead_id -> researchers.researcher_id 且 researchers.team_id -> teams.team_id，
  构成循环引用（合法，校验按闭世界快照整体判定，与插入顺序无关）；
- datasets 含组合唯一键 (owner_id, project_code)；唯一键任何分量为 NULL 即违规；
- 引用仅允许指向被引表主键。
"""

TABLE_ORDER = ("teams", "researchers", "datasets")

TABLES = {
    "teams": {
        "pk": "team_id",
        "fields": ("team_id", "team_name", "lead_id"),
        "not_null": ("team_id", "team_name"),
        "uniques": (("team_id",),),
        "references": (("lead_id", "researchers", "researcher_id"),),
    },
    "researchers": {
        "pk": "researcher_id",
        "fields": ("researcher_id", "full_name", "team_id"),
        "not_null": ("researcher_id", "full_name"),
        "uniques": (("researcher_id",),),
        "references": (("team_id", "teams", "team_id"),),
    },
    "datasets": {
        "pk": "dataset_id",
        "fields": ("dataset_id", "title", "owner_id", "project_code"),
        "not_null": ("dataset_id", "title", "owner_id", "project_code"),
        "uniques": (("dataset_id",), ("owner_id", "project_code")),
        "references": (("owner_id", "researchers", "researcher_id"),),
    },
}
