# 研究数据修订合并审定服务

纯后端实现（Python 标准库 + SQLite，零第三方依赖）：合并两个团队的独立修订，
人工审定冲突，校验关系完整性，并在目标版本未漂移的前提下原子交付。

## 运行

```bash
python3 demo.py      # 演示：初始化 → 建单 → 裁决/修正 → 校验 → 交付 → 幂等重试
python3 verify.py    # 端到端验证：9 个场景、90 项检查
```

要求 Python 3.8+（开发验证环境为 3.11）。代码仅依赖标准库（`sqlite3`、`json`、
`threading`、`hashlib`、`datetime`）。

## 目录结构

```
adjudicator/
  __init__.py    包入口（导出 MergeService、errors）
  schema.py      固定表结构定义
  errors.py      业务错误类型（均含稳定 code 与结构化 details）
  validate.py    输入规范化与完整性校验
  merge.py       按行身份的三方合并
  store.py       SQLite 持久层结构
  service.py     审定合并服务（对外接口）
demo.py          演示脚本
verify.py        验证脚本
```

## 固定表结构

行身份 = 主键值（稳定行标识）；所有字段值为字符串或 NULL。

| 表 | 字段 | 非空 | 唯一键 | 引用 |
|---|---|---|---|---|
| teams | team_id(PK), team_name, lead_id | team_id, team_name | (team_id) | lead_id → researchers.researcher_id |
| researchers | researcher_id(PK), full_name, team_id | researcher_id, full_name | (researcher_id) | team_id → teams.team_id |
| datasets | dataset_id(PK), title, owner_id, project_code | 全部 | (dataset_id)、(owner_id, project_code) | owner_id → researchers.researcher_id |

- `teams ↔ researchers` 构成**循环引用**（合法；校验按闭世界快照整体判定，与插入顺序无关）；
- `datasets` 含**组合唯一键**；唯一键任一分量为 NULL 即违规（`null_in_unique_key`）；
- 引用仅允许指向被引表主键。

## 数据与编码约定

- 快照输入/输出格式：`{table: [row, ...]}`，row 为恰好包含全部字段的字典，值为 `str` 或 `None`；
  缺表、缺字段、多字段、非字符串值、重复主键均为结构错误，立即拒绝。
- 文本一律 UTF-8；结构化边界（历史负载、回执）使用 JSON；时间戳为 UTC ISO 8601。
- 三种单元状态严格区分：**未决**（`pending: true`，值无意义）、**空值**
  （`value: null, pending: false`）、**删除**（候选中不存在该行）。

## 接口一览（MergeService）

| 方法 | 说明 |
|---|---|
| `initialize_target(seed)` | 初始化合法目标数据（仅一次），版本从 1 开始 |
| `get_target()` | 返回 `{version, data}`（版本及完整数据） |
| `create_order(order_id, base, source, base_version=None)` | 建单：固定合法基线、来源修订与当前目标（数据及版本），三方合并，初始修订号 1；多单并存 |
| `get_order(order_id)` | 返回当前修订号、候选（含未决标记）、全部冲突（含已裁决）、完整性、历史、回执 |
| `list_orders()` / `get_snapshot(order_id, which)` / `get_history(order_id)` | 辅助查询 |
| `adjudicate(order_id, rev, conflict_id, decision)` | 裁决冲突；成功推进修订号 |
| `fix_row(order_id, rev, table, row_id, changes)` | 修正候选行（按字段合并，可新建行）；成功推进修订号 |
| `validate_order(order_id)` | 重新校验候选完整性 |
| `deliver(order_id, rev, request_id)` | 原子交付指定审定修订 |

### 合并规则（按行身份逐字段）

- 两侧相同 → 合一；仅一侧相对基线变化 → 采用该侧；双方改成不同值 → 字段冲突；
- 两侧删除、或单侧删除且另一侧未改 → 删除；一侧删除另一侧修改 → 行级删改冲突；
- 同标识新增：内容一致保留，不一致字段 → 字段冲突（`both_added`）；仅一侧新增 → 保留。

### 裁决与候选修正

- 字段冲突：`{"choose": "src"|"tgt"}` 或 `{"choose": "value", "value": str|None}`
  （非空字段不得裁为 NULL）；
- 行级删改冲突：`{"choose": "keep"}`（保留修改侧内容）或 `{"choose": "delete"}`；
- `fix_row` 仅强制**字段结构与非空约束**；重复键、悬空引用等关系违规允许**暂存**，
  由校验与交付门禁把关；含未决冲突的行须先裁决；主键不可改；
- 原冲突与裁决历史永久保留（`status: resolved` + `history`），不可重复裁决；
- 每次成功修改（裁决/修正）修订号 +1；携带过期修订号的修改一律拒绝且不改状态。

### 完整性校验

- 有未决冲突 → `integrity.status = "pending"`（待定）；
- 否则对候选全量校验，列出**全部**重复键组（含键值与相关行）与悬空引用
  （含引用行、字段、值与被引表）→ `"invalid"` 或 `"valid"`；
- 每次修改后重新校验（查询时即时计算）；服务**绝不**自动删行或清空引用来消除问题。

### 交付语义

`deliver(order_id, rev, request_id)` 仅在三个条件同时满足时生效：冲突清零、
候选关系合法、目标版本仍等于建单版本。生效时在**单个事务**中原子完成：
全部目标表替换、目标升版（+1）、交付回执保存——交付的正是所检查的修订，
不存在只交付部分表或检查一个修订却交付另一个的窗口。

- 目标版本已漂移 → `TargetChanged`，须另建单；
- 已交付单不可修改（`OrderClosed`）；
- 相同 `request_id` 重试始终返回原回执（即使目标后来已变化）；
  不同 `request_id` 的交付请求拒绝（`RequestConflict`）；
- 回执含：request_id、order_id、delivered_rev、前后目标版本、各表行数、
  交付数据 SHA-256、交付时间。

### 错误码

`validation_error` / `not_found` / `stale_revision` / `open_conflicts` /
`integrity_violation` / `target_changed` / `order_closed` / `request_conflict` /
`already_initialized`。任何非法或冲突操作都不改变状态（校验先于写入，异常整体回滚）。

## 并发与持久化

- 所有公开操作在进程内互斥锁下串行执行，写操作包裹在 SQLite `BEGIN IMMEDIATE`
  事务中（检查与写入同事务），并发结果与逐次执行一致；
- 全部状态（目标数据与版本、三方快照、候选、冲突、历史、回执）持久化于单个
  SQLite 文件，重启后已确认的修订、裁决与交付结果完整保留；
- 崩溃安全：交付等多步写入在单事务内完成，中途失败整体回滚，不产生部分交付。

## 自行约定的非核心细节

- `order_id`、`request_id` 由调用方提供（字符串），分别作为建单与交付的标识/
  幂等键；冲突编号按（表序、行标识、字段序）从 1 确定性编号；
- 审定单修订号从 1 开始，每次成功修改 +1，交付本身不推进修订号；
- `base_version` 为可选的调用方溯源标签，仅记录不参与校验；
- `fix_row` 采用 upsert 语义：可修正既有候选行，也可新建缺失行（用于补齐被引行）；
- 删除候选行只能通过行级冲突的 `delete` 裁决完成，不提供其他删行入口。

## 验证覆盖（verify.py）

1. 初始化与输入校验（非法种子/基线拒绝且不留痕）；
2. 三方合并规则（逐字段、删除、新增、删改冲突、未决标记）；
3. 裁决（侧选/显式值/行保留删除、历史留痕、过期修订拒绝）；
4. 候选修正边界（结构/非空强制、关系违规暂存、重校验）；
5. 交付门禁（未决冲突/关系违规/版本漂移拒绝）、原子替换、幂等重试、
   已交付单不可变、冲突请求拒绝；
6. 删除与新增规则专项；
7. 空值/未决/删除三态区分；
8. 重启持久化（修订、裁决、快照、回执）；
9. 并发串行化（并发裁决恰一成功、并发交付同回执、过期修订全拒绝）。
