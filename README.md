# 研究数据修订合并与审定交付系统

纯后端实现，仅依赖 **Python 3 标准库 + SQLite**（无第三方依赖）。面向研究数据管理
员：把两个团队的独立修订与当前目标数据做三方合并，人工审定冲突与候选，校验关系
完整性后，原子、一致地交付到目标数据。

## 功能总览

| 能力 | 说明 |
| --- | --- |
| 修订合并 | 建单时固定共同基线、来源修订、当前目标数据及版本，按行身份逐字段三方合并 |
| 冲突审定 | 字段级裁决、整行保留/删除、候选行修正；未决内容与空值、删除严格区分 |
| 关系校验 | 未决冲突时完整性待定；否则列出全部重复键组与悬空引用及相关行；允许暂存违规；绝不自动删行或清空引用 |
| 一致交付 | 冲突清零 + 关系合法 + 目标仍为建单版本时，单事务原子完成全部目标表替换、目标升版、回执保存；幂等重试返回原回执 |

## 目录结构

```
merger/            核心包
  schema.py        固定表结构定义
  merge.py         三方逐字段合并引擎
  validate.py      结构/唯一键/引用校验
  storage.py       SQLite 持久层(全部状态落库)
  service.py       业务规则、并发控制、原子交付
  server.py        HTTP JSON 接口(标准库 http.server)
run_server.py      服务启动入口
demo.py            端到端演示脚本
tests/             单元/集成/并发/HTTP 测试(46 个用例)
```

## 固定表结构

两张表，字段值只能是**字符串或空值**；`id` 为稳定行标识；允许循环引用。

- **datasets**(`id`, `code`, `version`, `title`, `lead_record_id`)
  - 非空： `id`, `code`, `version`
  - 组合唯一键： `(code, version)`，唯一键不得含空值
  - 引用： `lead_record_id -> records.id`（可空，空表示无引用）
- **records**(`id`, `dataset_code`, `dataset_version`, `subject`, `value`, `next_record_id`)
  - 非空： `id`, `dataset_code`, `dataset_version`, `subject`
  - 组合唯一键： `(dataset_code, dataset_version, subject)`，不得含空值
  - 引用： `(dataset_code, dataset_version) -> datasets(code, version)`；
    `next_record_id -> records.id`（可空）

`datasets -> records` 与 `records -> datasets` 双向引用可构成跨表环，
`records.next_record_id` 可在表内成环；**环不属于违规**，校验不报。

## 核心概念与约定（自行约定的部分）

- **目标数据与版本**：库内唯一的目标数据集，带整数版本号，初始化为版本 1，
  每次交付升 1 版。`GET /target` 返回版本及完整数据。
- **审定单**：`POST /orders` 建单，固定共同基线（须全量合法，否则拒单）、来源
  修订（要求结构合法，关系问题允许随候选暂存）、当前目标数据及版本快照；可并
  存多单。每单有独立**审定修订号**，从 1 开始，每次成功修改 +1。
- **候选与冲突**：合并产出的候选行集 + 冲突列表。未决字段以
  `{"$pending": "<冲突id>"}` 表示——它是 JSON 对象，与空值 `null`、删除（行
  缺席）都可区分；合法字段值只会是字符串或空值，不会与之混淆。
- **冲突类型**：`field`（双方改成不同值）、`delete_modify`（删改冲突）、
  `add_add`（同标识不同新增）。原冲突内容（三方取值）永久保留，裁决结果单独
  记录，裁决历史仅追加。
- **幂等交付键**：交付请求自带 `request_id`。相同请求重试始终返回原回执（即
  使目标后来变化）；同键不同参数视为冲突并拒绝。

## 合并规则（按行身份逐字段）

| 基线 | 来源 | 目标 | 结果 |
| --- | --- | --- | --- |
| 有 | 与基线同 | 与基线同 | 保留原值 |
| 有 | 改了 | 未改 | 采用来源侧 |
| 有 | 未改 | 改了 | 采用目标侧 |
| 有 | 改成 A | 改成 A | 合一采用 A |
| 有 | 改成 A | 改成 B(≠A) | **field 冲突**，候选字段置未决标记 |
| 有 | 删除 | 删除 | 删除 |
| 有 | 删除 | 未改 | 删除 |
| 有 | 删除 | 改了 | **delete_modify 冲突** |
| 有 | 改了 | 删除 | **delete_modify 冲突** |
| 无 | 新增 X | 无 | 保留 X |
| 无 | 无 | 新增 Y | 保留 Y |
| 无 | 新增 X | 新增 X | 合一保留 |
| 无 | 新增 X | 新增 Y(≠X) | **add_add 冲突** |

字段级合并意味着：两侧各改不同字段可干净合并；同一字段两侧改不同值才裁决。

## 裁决与候选修正（`POST /orders/{id}/actions`）

请求体 `{"revision": <当前审定修订号>, "actions": [...]}`，一批动作原子生效，
全部合法才提交并推进修订号；任何非法动作或旧修订号整体拒绝，不改状态。

| 动作 | 说明 |
| --- | --- |
| `{"type":"resolve_field","conflict_id","value"}` | 裁决字段值（字符串或空值；非空字段不得置空） |
| `{"type":"resolve_row","conflict_id","keep":true[,"values"\|"side"]}` | 整行保留：删改冲突默认取修改侧；add_add 须给 `values` 或 `side:"source"/"target"` |
| `{"type":"resolve_row","conflict_id","keep":false}` | 整行删除 |
| `{"type":"upsert_row","table","values"}` | 修正/新增候选行（须符合字段结构与非空要求） |
| `{"type":"delete_row","table","id"}` | 删除候选行（显式人工操作，非自动） |

约束：已裁决冲突不可再裁；有未决冲突的行不可直接 upsert/delete（先裁决）；候选
值只能是字符串或空值（无法伪造未决标记）；关系违规（重复键、悬空引用）允许暂
存，由校验报告列出，只能由管理员手工消除。

## 关系校验

- 有未决冲突 → 完整性 `pending`（待定），附未决冲突清单。
- 否则全量校验候选集，报告**全部**结构违规、重复组合键组（含相关行 id）、悬
  空引用（含引用行、缺失目标）。每次修改后重新校验（查询时实时计算）。
- 系统**绝不**自动删行或清空引用来消除问题。

## 交付（`POST /deliveries`）

请求体 `{"request_id", "order_id", "revision"}`。在**单个事务**内依次：

1. 幂等键检查：已存在且参数一致 → 返回原回执；参数不同 → 409 冲突；
2. 审定单存在、未交付、`revision` 等于当前审定修订号（不得检查一个修订却交
   付另一个）；
3. 冲突清零、关系校验通过；
4. 目标版本仍等于建单时版本（否则 `TARGET_CHANGED`，须另建单）；
5. 原子完成：**全部**目标表整体替换 + 目标升版 + 交付回执保存（不会只交付
   部分表；任一步失败整体回滚）。

交付后审定单不可修改；回执含数据摘要（SHA-256）可核对。

## 并发与持久化

- 所有请求经全局可重入锁串行化，写操作在单个 `BEGIN IMMEDIATE` 事务内完成
  “检查 + 修改”，并发调用效果等价于某种逐次执行（可串行化）。
- 全部状态（目标数据与版本、审定单、快照、候选、冲突、裁决历史、交付回执、
  幂等键）存于 SQLite（WAL + FULL 同步），重启后已确认修订、裁决与交付结果
  均保留。
- 非法或冲突操作在写入前抛出错误，事务回滚，不产生任何状态变更。

## 运行说明

```bash
# 启动服务(首次可用 --init 用内置示例数据初始化目标)
python3 run_server.py --db merger.db --port 8080 --init

# 或完全手动:
python3 run_server.py --db merger.db --port 8080
curl -X POST http://127.0.0.1:8080/init            # 初始化(仅允许一次)
```

端到端演示（自建临时库，演示合并→裁决→校验→交付→幂等→重启）：

```bash
python3 demo.py
```

## HTTP 接口一览

| 方法/路径 | 说明 |
| --- | --- |
| `GET /` `GET /health` `GET /schema` | 索引 / 健康检查 / 表结构与约定 |
| `POST /init` | 初始化合法目标数据（仅一次），可选 `{"data": {表: [行]}}` |
| `GET /target` | 查询目标：版本及完整数据 |
| `POST /orders` | 建单：`{"baseline": {...}, "source": {...}}` |
| `GET /orders` / `GET /orders/{id}` | 列表 / 当前修订、候选与冲突（含完整性） |
| `GET /orders/{id}/history` | 裁决与修正历史 |
| `POST /orders/{id}/actions` | 裁决与修正：`{"revision": n, "actions": [...]}` |
| `POST /deliveries` | 交付：`{"request_id", "order_id", "revision"}` |
| `GET /deliveries/{request_id}` | 查询交付回执 |

数据集编码：`{"表名": [行对象, ...]}`；行对象须恰好包含该表全部字段。错误响
应：`{"error": {"code", "message", "details?"}}`，主要错误码：
`INVALID_DATA` `INVALID_BASELINE` `NOT_INITIALIZED` `ALREADY_INITIALIZED`
`STALE_REVISION` `ORDER_DELIVERED` `CONFLICT_RESOLVED`
`ROW_HAS_PENDING_CONFLICTS` `CONFLICTS_PENDING` `INTEGRITY_VIOLATIONS`
`TARGET_CHANGED` `REQUEST_ID_CONFLICT` 等。非法或冲突操作均不改状态。

## 验证

```bash
python3 -m unittest discover -s tests -t . -v   # 46 个用例
```

- `tests/test_merge.py`：合并矩阵全分支（相同合一、单侧采用、字段冲突、删
  除、删改冲突、新增冲突、冲突编号确定性）。
- `tests/test_service.py`：初始化合法性、建单快照、裁决/修正规则、旧修订拒
  绝、修订号推进、历史保留、完整性待定/违规清单/手工修复、循环引用放行、交
  付全条件、幂等重试（含目标已变化后）、目标变化须另建单、已交付单不可改、
  失败不改状态、重启保留。
- `tests/test_concurrency.py`：并发裁决无丢失（修订号 = 成功次数）、并发同
  键交付仅执行一次且回执一致、交付与裁决竞争结果必为两种逐次执行之一。
- `tests/test_http.py`：真实 HTTP 全流程、错误码、坏 JSON、未知路径。
