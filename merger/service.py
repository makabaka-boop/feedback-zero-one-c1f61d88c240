"""核心服务层: 初始化、建单、合并、裁决、校验、交付。

并发约定: 所有公开方法先取全局可重入锁, 写操作再在单个
BEGIN IMMEDIATE 事务内完成"检查 + 修改", 因此并发调用的效果
等价于某种逐次执行(可串行化); 任何非法或冲突操作在写入前抛出
ApiError, 事务回滚, 不产生任何状态变更。
"""

import hashlib
import json
import threading
from datetime import datetime, timezone

from .errors import ApiError
from .merge import merge_datasets, row_has_pending
from .schema import TABLES, TABLE_ORDER
from .storage import Storage
from .validate import check_row_structure, order_integrity, validate_dataset


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


# 默认初始目标数据(合法, 含 R1<->R2 表内环与 D1->R2->D1 跨表环)。
DEFAULT_SEED = {
    "datasets": [
        {"id": "D1", "code": "DS-001", "version": "1.0", "title": "基线调查", "lead_record_id": "R2"},
        {"id": "D2", "code": "DS-002", "version": "2.0", "title": "随访数据", "lead_record_id": None},
    ],
    "records": [
        {"id": "R1", "dataset_code": "DS-001", "dataset_version": "1.0", "subject": "S-01", "value": "10", "next_record_id": "R2"},
        {"id": "R2", "dataset_code": "DS-001", "dataset_version": "1.0", "subject": "S-02", "value": "20", "next_record_id": "R1"},
        {"id": "R3", "dataset_code": "DS-002", "dataset_version": "2.0", "subject": "S-09", "value": None, "next_record_id": None},
    ],
}


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


class MergeService:
    def __init__(self, db_path):
        self._lock = threading.RLock()
        self._db = Storage(db_path)

    def close(self):
        self._db.close()

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _require_initialized(self):
        if self._db.get_meta("initialized") != "1":
            raise ApiError(409, "NOT_INITIALIZED", "目标数据尚未初始化, 请先调用 POST /init")

    def is_initialized(self):
        with self._lock:
            return self._db.get_meta("initialized") == "1"

    @staticmethod
    def _rows_to_lists(data):
        return {t: [data[t][rid] for rid in sorted(data[t])] for t in TABLE_ORDER}

    @staticmethod
    def _normalize(data, what):
        """校验并规范化数据集: {表: [行, ...]} -> {表: {id: 行}}。

        只接受固定表结构内的字段、字符串或空值, 非空约束与"唯一键不得含
        空值"在此强制; 结构问题整体拒绝, 不产生任何状态变更。
        """
        if not isinstance(data, dict):
            raise ApiError(400, "INVALID_DATA", f"{what}必须是对象, 形如 {{表名: [行, ...]}}")
        unknown = sorted(set(data) - set(TABLES))
        if unknown:
            raise ApiError(400, "UNKNOWN_TABLE", f"{what}包含未知表: {unknown}")
        problems = []
        out = {}
        for table in TABLE_ORDER:
            rows = data.get(table, [])
            if not isinstance(rows, list):
                raise ApiError(400, "INVALID_DATA", f"{what}.{table} 必须是数组")
            tbl = {}
            for i, row in enumerate(rows):
                probs = check_row_structure(table, row)
                if probs:
                    problems.append(
                        {
                            "table": table,
                            "index": i,
                            "row_id": row.get("id") if isinstance(row, dict) else None,
                            "problems": probs,
                        }
                    )
                    continue
                rid = row["id"]
                if rid in tbl:
                    problems.append(
                        {"table": table, "index": i, "row_id": rid, "problems": [f"重复的行标识 {rid}"]}
                    )
                    continue
                tbl[rid] = dict(row)
            out[table] = tbl
        if problems:
            raise ApiError(400, "INVALID_DATA", f"{what}存在结构问题", {"problems": problems})
        return out

    # ------------------------------------------------------------------
    # 初始化与目标查询
    # ------------------------------------------------------------------
    def initialize(self, data=None):
        """初始化合法目标数据(版本 1)。只允许初始化一次。"""
        with self._lock:
            payload = DEFAULT_SEED if data is None else data
            norm = self._normalize(payload, "初始目标数据")
            report = validate_dataset(norm)
            if report["status"] != "ok":
                raise ApiError(400, "INVALID_INITIAL_DATA", "初始目标数据不合法", report)
            with self._db.txn():
                if self._db.get_meta("initialized") == "1":
                    raise ApiError(409, "ALREADY_INITIALIZED", "目标数据已初始化, 不允许重复初始化")
                self._db.create_target_tables()
                self._db.replace_target(norm)
                self._db.set_meta("initialized", "1")
                self._db.set_meta("target_version", "1")
                self._db.set_meta("order_seq", "0")
            return {"version": 1, "data": self._rows_to_lists(norm)}

    def get_target(self):
        """查询目标: 返回版本号及完整数据。"""
        with self._lock:
            self._require_initialized()
            return {
                "version": int(self._db.get_meta("target_version")),
                "data": self._rows_to_lists(self._db.load_target()),
            }

    # ------------------------------------------------------------------
    # 建单与查询
    # ------------------------------------------------------------------
    def create_order(self, baseline, source):
        """建单: 固定合法的共同基线、来源修订、当前目标数据及版本。

        共同基线必须通过全量校验; 来源修订要求结构合法(关系问题允许随
        候选暂存, 由后续校验报告)。可并存多个审定单。
        """
        with self._lock:
            self._require_initialized()
            base = self._normalize(baseline, "共同基线")
            report = validate_dataset(base)
            if report["status"] != "ok":
                raise ApiError(400, "INVALID_BASELINE", "共同基线不合法", report)
            src = self._normalize(source, "来源修订")
            with self._db.txn():
                tgt = self._db.load_target()
                tver = int(self._db.get_meta("target_version"))
                candidates, conflicts = merge_datasets(base, src, tgt)
                seq = int(self._db.get_meta("order_seq", "0")) + 1
                self._db.set_meta("order_seq", str(seq))
                order_id = f"O-{seq:06d}"
                self._db.insert_order(order_id, 1, "open", tver, _now())
                self._db.save_rows(order_id, "baseline", base)
                self._db.save_rows(order_id, "source", src)
                self._db.save_rows(order_id, "target0", tgt)
                self._db.save_rows(order_id, "candidate", candidates)
                self._db.save_conflicts(order_id, conflicts)
        return self.get_order(order_id)

    def get_order(self, order_id):
        """查询审定单: 返回当前修订、候选与冲突(附完整性报告)。"""
        with self._lock:
            self._require_initialized()
            order = self._db.get_order_row(order_id)
            if order is None:
                raise ApiError(404, "ORDER_NOT_FOUND", f"审定单 {order_id} 不存在")
            candidates = self._db.load_rows(order_id, "candidate")
            conflicts = self._db.load_conflicts(order_id)
            view = {
                "order_id": order_id,
                "revision": order["revision"],
                "status": order["status"],
                "target_version_at_creation": order["target_version_at_creation"],
                "created_at": order["created_at"],
                "delivered_at": order["delivered_at"],
                "candidates": self._rows_to_lists(candidates),
                "conflicts": conflicts,
                "integrity": order_integrity(candidates, conflicts),
            }
            if order["status"] == "delivered":
                delivery = self._db.get_delivery_for_order(order_id)
                view["delivery"] = delivery["receipt"] if delivery else None
            return view

    def list_orders(self):
        with self._lock:
            self._require_initialized()
            return {"orders": self._db.list_orders()}

    def get_history(self, order_id):
        with self._lock:
            self._require_initialized()
            if self._db.get_order_row(order_id) is None:
                raise ApiError(404, "ORDER_NOT_FOUND", f"审定单 {order_id} 不存在")
            return {"order_id": order_id, "history": self._db.list_history(order_id)}

    # ------------------------------------------------------------------
    # 裁决与候选修正
    # ------------------------------------------------------------------
    def apply_actions(self, order_id, revision, actions):
        """对指定审定修订号应用一批动作(原子): 全部合法才提交。

        成功则审定修订号 +1; 旧修订号或任何非法动作整体拒绝, 不改状态。
        """
        with self._lock:
            self._require_initialized()
            if not _is_int(revision):
                raise ApiError(400, "INVALID_REVISION", "revision 必须是整数")
            if not isinstance(actions, list) or not actions:
                raise ApiError(400, "EMPTY_ACTIONS", "actions 必须是非空数组")
            with self._db.txn():
                order = self._db.get_order_row(order_id)
                if order is None:
                    raise ApiError(404, "ORDER_NOT_FOUND", f"审定单 {order_id} 不存在")
                if order["status"] != "open":
                    raise ApiError(409, "ORDER_DELIVERED", "已交付的审定单不可修改")
                if revision != order["revision"]:
                    raise ApiError(
                        409,
                        "STALE_REVISION",
                        "审定修订号已过期, 请重新查询后再修改",
                        {"current_revision": order["revision"]},
                    )
                candidates = self._db.load_rows(order_id, "candidate")
                conflicts = self._db.load_conflicts(order_id)
                cmap = {c["conflict_id"]: c for c in conflicts}
                for i, action in enumerate(actions):
                    self._apply_action(action, f"actions[{i}]", candidates, cmap)
                new_rev = order["revision"] + 1
                self._db.update_order_revision(order_id, new_rev)
                self._db.save_rows(order_id, "candidate", candidates)
                self._db.save_conflicts(order_id, conflicts)
                self._db.append_history(order_id, order["revision"], new_rev, actions, _now())
        return self.get_order(order_id)

    def _apply_action(self, action, where, candidates, cmap):
        if not isinstance(action, dict):
            raise ApiError(400, "INVALID_ACTION", f"{where} 必须是对象")
        atype = action.get("type")
        if atype == "resolve_field":
            self._resolve_field(action, where, candidates, cmap)
        elif atype == "resolve_row":
            self._resolve_row(action, where, candidates, cmap)
        elif atype == "upsert_row":
            self._upsert_row(action, where, candidates, cmap)
        elif atype == "delete_row":
            self._delete_row(action, where, candidates, cmap)
        else:
            raise ApiError(400, "UNKNOWN_ACTION", f"{where}: 未知动作类型 {atype!r}")

    @staticmethod
    def _get_pending_conflict(action, where, cmap):
        cid = action.get("conflict_id")
        if not isinstance(cid, str):
            raise ApiError(400, "INVALID_ACTION", f"{where}: conflict_id 必须是字符串")
        c = cmap.get(cid)
        if c is None:
            raise ApiError(404, "CONFLICT_NOT_FOUND", f"{where}: 冲突 {cid} 不存在")
        if c["status"] != "pending":
            raise ApiError(409, "CONFLICT_RESOLVED", f"{where}: 冲突 {cid} 已裁决, 原冲突与裁决历史保留, 不可再改")
        return c

    def _resolve_field(self, action, where, candidates, cmap):
        c = self._get_pending_conflict(action, where, cmap)
        if c["kind"] != "field":
            raise ApiError(400, "WRONG_CONFLICT_KIND", f"{where}: 冲突 {c['conflict_id']} 不是字段冲突")
        if "value" not in action:
            raise ApiError(400, "INVALID_ACTION", f"{where}: 缺少 value")
        value = action["value"]
        if value is not None and not isinstance(value, str):
            raise ApiError(400, "INVALID_VALUE", f"{where}: value 必须是字符串或空值")
        table, field = c["table"], c["field"]
        if value is None and field in TABLES[table]["not_null"]:
            raise ApiError(400, "NULL_NOT_ALLOWED", f"{where}: 字段 {field} 不允许为空")
        row = candidates[table].get(c["row_id"])
        if row is None:
            raise ApiError(409, "ROW_NOT_FOUND", f"{where}: 候选行 {c['row_id']} 不存在")
        row[field] = value
        c["status"] = "resolved"
        c["resolution"] = {"value": value}
        c["resolved_at"] = _now()

    def _resolve_row(self, action, where, candidates, cmap):
        c = self._get_pending_conflict(action, where, cmap)
        if c["kind"] not in ("delete_modify", "add_add"):
            raise ApiError(400, "WRONG_CONFLICT_KIND", f"{where}: 冲突 {c['conflict_id']} 不是行级冲突")
        keep = action.get("keep")
        if not isinstance(keep, bool):
            raise ApiError(400, "INVALID_ACTION", f"{where}: keep 必须是布尔值")
        table, rid = c["table"], c["row_id"]
        if not keep:
            candidates[table].pop(rid, None)  # 整行删除
            c["resolution"] = {"keep": False}
        else:
            values = action.get("values")
            if values is None:
                if c["kind"] == "delete_modify":
                    values = c["detail"]["modified"]  # 默认保留修改侧
                else:
                    side = action.get("side")
                    if side not in ("source", "target"):
                        raise ApiError(
                            400,
                            "INVALID_ACTION",
                            f"{where}: add_add 冲突保留时必须给出 values, 或用 side 选择 source/target",
                        )
                    values = c["detail"][side]
            values = self._validate_row_values(table, values, where)
            if values["id"] != rid:
                raise ApiError(400, "ID_MISMATCH", f"{where}: values.id 必须等于冲突行标识 {rid}")
            candidates[table][rid] = values  # 整行保留
            c["resolution"] = {"keep": True, "values": values}
        c["status"] = "resolved"
        c["resolved_at"] = _now()

    @staticmethod
    def _validate_row_values(table, values, where):
        probs = check_row_structure(table, values)
        if probs:
            raise ApiError(
                400,
                "INVALID_ROW",
                f"{where}: 行不符合字段结构或非空要求",
                {"table": table, "problems": probs},
            )
        return dict(values)

    @staticmethod
    def _pending_row_conflict(cmap, table, rid):
        for c in cmap.values():
            if (
                c["status"] == "pending"
                and c["kind"] in ("delete_modify", "add_add")
                and c["table"] == table
                and c["row_id"] == rid
            ):
                return c
        return None

    def _upsert_row(self, action, where, candidates, cmap):
        table = action.get("table")
        if table not in TABLES:
            raise ApiError(400, "UNKNOWN_TABLE", f"{where}: 未知表 {table!r}")
        if "values" not in action:
            raise ApiError(400, "INVALID_ACTION", f"{where}: 缺少 values")
        values = self._validate_row_values(table, action["values"], where)
        rid = values["id"]
        existing = candidates[table].get(rid)
        if existing is not None and row_has_pending(existing):
            raise ApiError(409, "ROW_HAS_PENDING_CONFLICTS", f"{where}: 行 {rid} 存在未决字段冲突, 请先裁决")
        pc = self._pending_row_conflict(cmap, table, rid)
        if pc is not None:
            raise ApiError(
                409,
                "ROW_HAS_PENDING_CONFLICTS",
                f"{where}: 行 {rid} 存在未决行级冲突 {pc['conflict_id']}, 请先裁决",
            )
        candidates[table][rid] = values

    def _delete_row(self, action, where, candidates, cmap):
        table = action.get("table")
        if table not in TABLES:
            raise ApiError(400, "UNKNOWN_TABLE", f"{where}: 未知表 {table!r}")
        rid = action.get("id")
        if not isinstance(rid, str):
            raise ApiError(400, "INVALID_ACTION", f"{where}: id 必须是字符串")
        existing = candidates[table].get(rid)
        if existing is None:
            raise ApiError(404, "ROW_NOT_FOUND", f"{where}: 候选行 {table}/{rid} 不存在")
        if row_has_pending(existing):
            raise ApiError(409, "ROW_HAS_PENDING_CONFLICTS", f"{where}: 行 {rid} 存在未决字段冲突, 请先裁决")
        del candidates[table][rid]

    # ------------------------------------------------------------------
    # 交付
    # ------------------------------------------------------------------
    def deliver(self, request_id, order_id, revision):
        """交付指定审定修订。

        仅在冲突清零、关系合法且目标仍为建单版本时, 于单个事务内原子完成
        全部目标表替换、目标升版与回执保存。request_id 为幂等键: 相同交付
        请求重试始终返回原回执(即使目标后来变化); 同键不同参数视为冲突。
        """
        with self._lock:
            self._require_initialized()
            if not isinstance(request_id, str) or not request_id:
                raise ApiError(400, "INVALID_REQUEST", "request_id 必须是非空字符串")
            if not isinstance(order_id, str) or not order_id:
                raise ApiError(400, "INVALID_REQUEST", "order_id 必须是非空字符串")
            if not _is_int(revision):
                raise ApiError(400, "INVALID_REQUEST", "revision 必须是整数")
            with self._db.txn():
                existing = self._db.get_delivery(request_id)
                if existing is not None:
                    if existing["order_id"] == order_id and existing["revision"] == revision:
                        return {"receipt": existing["receipt"], "idempotent_replay": True}
                    raise ApiError(
                        409,
                        "REQUEST_ID_CONFLICT",
                        "request_id 已被不同的交付请求使用",
                        {"original": {"order_id": existing["order_id"], "revision": existing["revision"]}},
                    )
                order = self._db.get_order_row(order_id)
                if order is None:
                    raise ApiError(404, "ORDER_NOT_FOUND", f"审定单 {order_id} 不存在")
                if order["status"] != "open":
                    raise ApiError(409, "ORDER_DELIVERED", "审定单已交付, 不可重复交付")
                if revision != order["revision"]:
                    raise ApiError(
                        409,
                        "STALE_REVISION",
                        "指定修订与审定单当前修订不一致, 拒绝交付(不得检查一个修订却交付另一个)",
                        {"current_revision": order["revision"]},
                    )
                conflicts = self._db.load_conflicts(order_id)
                pending = [c["conflict_id"] for c in conflicts if c["status"] == "pending"]
                if pending:
                    raise ApiError(409, "CONFLICTS_PENDING", "存在未决冲突, 禁止交付", {"pending_conflict_ids": pending})
                candidates = self._db.load_rows(order_id, "candidate")
                report = validate_dataset(candidates)
                if report["status"] != "ok":
                    raise ApiError(409, "INTEGRITY_VIOLATIONS", "关系校验未通过, 禁止交付", report)
                tver = int(self._db.get_meta("target_version"))
                if tver != order["target_version_at_creation"]:
                    raise ApiError(
                        409,
                        "TARGET_CHANGED",
                        "目标数据版本已变化, 本单不可交付, 请基于当前目标重新建单",
                        {"target_version": tver, "order_target_version": order["target_version_at_creation"]},
                    )
                # 原子完成: 全部目标表替换 + 目标升版 + 交付回执保存
                self._db.replace_target(candidates)
                self._db.set_meta("target_version", str(tver + 1))
                delivered_at = _now()
                canonical = self._rows_to_lists(candidates)
                digest = hashlib.sha256(
                    json.dumps(canonical, sort_keys=True, ensure_ascii=False).encode("utf-8")
                ).hexdigest()
                receipt = {
                    "request_id": request_id,
                    "order_id": order_id,
                    "revision": revision,
                    "previous_target_version": tver,
                    "target_version": tver + 1,
                    "delivered_at": delivered_at,
                    "tables": {t: {"rows": len(canonical[t])} for t in TABLE_ORDER},
                    "data_digest": digest,
                }
                self._db.mark_order_delivered(order_id, delivered_at)
                self._db.put_delivery(request_id, order_id, revision, receipt, delivered_at)
                return {"receipt": receipt, "idempotent_replay": False}

    def get_delivery(self, request_id):
        with self._lock:
            self._require_initialized()
            d = self._db.get_delivery(request_id)
            if d is None:
                raise ApiError(404, "DELIVERY_NOT_FOUND", f"交付请求 {request_id} 不存在")
            return d
