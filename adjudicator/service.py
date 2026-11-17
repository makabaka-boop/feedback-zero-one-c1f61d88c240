"""审定合并服务：建单、三方合并、冲突裁决、候选修正、关系校验、原子交付。

线程安全与持久化：
- 所有公开操作在进程内互斥锁下串行执行，写操作包裹在 SQLite
  BEGIN IMMEDIATE 事务中，并发结果与逐次执行一致；
- 全部状态（目标数据与版本、快照、候选、冲突、历史、回执）落库，
  重启后已确认的修订、裁决与交付结果完整保留；
- 任何非法或冲突操作在写库前校验失败并整体回滚，不改变状态。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

from . import store
from .errors import (AlreadyInitialized, IntegrityViolation, NotFound,
                     OpenConflicts, OrderClosed, RequestConflict, StaleRevision,
                     TargetChanged, ValidationError)
from .merge import merge_snapshots
from .schema import TABLES, TABLE_ORDER
from .validate import normalize_snapshot, snapshot_violations


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _hash_view(view):
    blob = json.dumps(view, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _public_snapshot(snap):
    return {t: [dict(snap[t][rid]) for rid in sorted(snap[t])] for t in TABLE_ORDER}


class MergeService:
    """单数据库文件上的线程安全审定合并服务。"""

    def __init__(self, db_path):
        self._db_path = str(db_path)
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False,
                                     isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout = 5000")
        store.ensure_schema(self._conn)
        self._lock = threading.RLock()

    def close(self):
        with self._lock:
            self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    @contextmanager
    def _tx(self):
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    # ------------------------------------------------------------------
    # 元信息 / 目标数据
    # ------------------------------------------------------------------

    def _get_meta(self, key):
        row = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return None if row is None else row["value"]

    def _set_meta(self, key, value):
        self._conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)",
                           (key, value))

    def is_initialized(self):
        with self._lock:
            return self._get_meta("target_version") is not None

    def initialize_target(self, seed):
        """初始化目标数据（仅允许一次）；种子须完整合法，初始版本为 1。"""
        snap = normalize_snapshot(seed)
        with self._tx():
            if self._get_meta("target_version") is not None:
                raise AlreadyInitialized("target_already_initialized")
            violations = snapshot_violations(snap)
            if violations:
                raise ValidationError("illegal_seed", {"violations": violations})
            self._replace_target(snap)
            self._set_meta("target_version", "1")
        return self.get_target()

    def get_target(self):
        """返回 {"version": int, "data": {table: [row, ...]}}（完整数据）。"""
        with self._lock:
            version = self._get_meta("target_version")
            if version is None:
                raise NotFound("target_not_initialized")
            return {"version": int(version),
                    "data": _public_snapshot(self._read_target())}

    def _read_target(self):
        data = {}
        for table in TABLE_ORDER:
            pk = TABLES[table]["pk"]
            rows = {}
            for r in self._conn.execute(f"SELECT * FROM target_{table}"):
                row = {f: r[f] for f in TABLES[table]["fields"]}
                rows[row[pk]] = row
            data[table] = rows
        return data

    def _replace_target(self, snap):
        for table in TABLE_ORDER:
            fields = TABLES[table]["fields"]
            self._conn.execute(f"DELETE FROM target_{table}")
            sql = (f"INSERT INTO target_{table} ({', '.join(fields)}) "
                   f"VALUES ({', '.join('?' for _ in fields)})")
            for rid in sorted(snap[table]):
                row = snap[table][rid]
                self._conn.execute(sql, [row[f] for f in fields])

    # ------------------------------------------------------------------
    # 建单
    # ------------------------------------------------------------------

    def create_order(self, order_id, base, source, base_version=None):
        """建单：固定合法基线、来源修订与当前目标（数据及版本），执行三方合并。

        基线与来源须完整合法（结构、非空、唯一键、引用），否则拒绝且不留痕；
        目标数据与版本在建单时快照固定；可并存多个审定单。初始修订号为 1。
        """
        if not isinstance(order_id, str) or not order_id:
            raise ValidationError("bad_order_id")
        base_snap = normalize_snapshot(base)
        src_snap = normalize_snapshot(source)
        with self._tx():
            version = self._get_meta("target_version")
            if version is None:
                raise NotFound("target_not_initialized")
            if self._conn.execute("SELECT 1 FROM orders WHERE order_id=?",
                                  (order_id,)).fetchone():
                raise RequestConflict("order_exists", {"order_id": order_id})
            problems = {}
            base_violations = snapshot_violations(base_snap)
            if base_violations:
                problems["base"] = base_violations
            src_violations = snapshot_violations(src_snap)
            if src_violations:
                problems["source"] = src_violations
            if problems:
                raise ValidationError("illegal_base_or_source", problems)
            tgt_snap = self._read_target()
            candidate, conflicts = merge_snapshots(base_snap, src_snap, tgt_snap)
            self._conn.execute(
                "INSERT INTO orders(order_id, base_version, target_version, status,"
                " current_rev, created_at) VALUES (?,?,?,?,?,?)",
                (order_id, base_version, int(version), "open", 1, _now()))
            self._insert_snapshot(order_id, "base", base_snap)
            self._insert_snapshot(order_id, "src", src_snap)
            self._insert_snapshot(order_id, "tgt", tgt_snap)
            self._insert_candidate(order_id, candidate)
            self._insert_conflicts(order_id, conflicts)
            self._log(order_id, 1, "create", {"open_conflicts": len(conflicts)})
        return self.get_order(order_id)

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def get_order(self, order_id):
        """返回当前修订号、候选（含未决标记）、全部冲突、完整性与历史。"""
        with self._lock:
            o = self._load_order(order_id)
            cand = self._read_candidate(order_id)
            conflicts = self._read_conflicts(order_id)
            receipt = self._load_receipt(order_id)
            return {
                "order_id": o["order_id"],
                "status": o["status"],
                "current_rev": o["current_rev"],
                "base_version": o["base_version"],
                "target_version": o["target_version"],
                "created_at": o["created_at"],
                "delivered_at": o["delivered_at"],
                "candidate": self._candidate_public(cand),
                "conflicts": conflicts,
                "integrity": self._integrity(order_id, conflicts),
                "history": self._read_history(order_id),
                "receipt": None if receipt is None else json.loads(receipt["payload"]),
            }

    def list_orders(self):
        with self._lock:
            return [dict(r) for r in self._conn.execute(
                "SELECT order_id, status, current_rev, target_version, created_at,"
                " delivered_at FROM orders ORDER BY created_at, order_id")]

    def get_snapshot(self, order_id, which):
        """读取建单时固定的快照：which ∈ {"base", "src", "tgt"}。"""
        if which not in ("base", "src", "tgt"):
            raise ValidationError("bad_snapshot_which", {"which": which})
        with self._lock:
            self._load_order(order_id)
            return _public_snapshot(self._read_snapshot(order_id, which))

    def get_history(self, order_id):
        with self._lock:
            self._load_order(order_id)
            return self._read_history(order_id)

    def validate_order(self, order_id):
        """重新校验候选：有未决冲突 → pending；否则列出全部关系违规。"""
        with self._lock:
            self._load_order(order_id)
            return self._integrity(order_id, self._read_conflicts(order_id))

    # ------------------------------------------------------------------
    # 裁决与候选修正
    # ------------------------------------------------------------------

    def adjudicate(self, order_id, rev, conflict_id, decision):
        """裁决冲突并推进修订号。

        字段冲突：{"choose": "src"|"tgt"} 或 {"choose": "value", "value": str|None}；
        行级删改冲突：{"choose": "keep"}（保留修改侧内容）或 {"choose": "delete"}。
        原冲突与裁决历史永久保留；已裁决冲突不可重复裁决。
        """
        if not isinstance(decision, dict):
            raise ValidationError("bad_decision")
        choose = decision.get("choose")
        with self._tx():
            o = self._load_order(order_id)
            self._check_open(o)
            self._check_rev(o, rev)
            c = self._load_conflict(order_id, conflict_id)
            if c["status"] != "open":
                raise RequestConflict("conflict_already_resolved",
                                      {"conflict_id": conflict_id})
            new_rev = o["current_rev"] + 1
            if c["field"] is not None:
                if choose == "src":
                    value = c["src_value"]
                elif choose == "tgt":
                    value = c["tgt_value"]
                elif choose == "value":
                    if "value" not in decision:
                        raise ValidationError("missing_decision_value")
                    value = decision["value"]
                    if value is not None and not isinstance(value, str):
                        raise ValidationError("bad_value_type")
                else:
                    raise ValidationError("bad_decision_for_field_conflict")
                if value is None and c["field"] in TABLES[c["table"]]["not_null"]:
                    raise ValidationError("not_null_violation",
                                          {"table": c["table"], "row_id": c["row_id"],
                                           "field": c["field"]})
                self._conn.execute(
                    "UPDATE cand_cells SET value=?, pending=0"
                    " WHERE order_id=? AND table_name=? AND row_id=? AND field=?",
                    (value, order_id, c["table"], c["row_id"], c["field"]))
                resolution, resolved_value = choose, value
            else:
                if choose == "keep":
                    self._conn.execute(
                        "UPDATE cand_rows SET state='present'"
                        " WHERE order_id=? AND table_name=? AND row_id=?",
                        (order_id, c["table"], c["row_id"]))
                elif choose == "delete":
                    self._conn.execute(
                        "DELETE FROM cand_cells WHERE order_id=? AND table_name=? AND row_id=?",
                        (order_id, c["table"], c["row_id"]))
                    self._conn.execute(
                        "DELETE FROM cand_rows WHERE order_id=? AND table_name=? AND row_id=?",
                        (order_id, c["table"], c["row_id"]))
                else:
                    raise ValidationError("bad_decision_for_row_conflict")
                resolution, resolved_value = choose, None
            self._conn.execute(
                "UPDATE conflicts SET status='resolved', resolution=?,"
                " resolved_value=?, resolved_rev=? WHERE order_id=? AND conflict_id=?",
                (resolution, resolved_value, new_rev, order_id, conflict_id))
            self._bump_rev(order_id, new_rev)
            self._log(order_id, new_rev, "adjudicate",
                      {"conflict_id": conflict_id, "decision": decision})
        return self.get_order(order_id)

    def fix_row(self, order_id, rev, table, row_id, changes):
        """修正候选行（按字段合并；行不存在则新建，可用于补齐被引行）。

        仅强制字段结构与非空约束；重复键、悬空引用等关系违规允许暂存，
        由 validate_order / 交付门禁把关。含未决冲突的行须先裁决。
        """
        if table not in TABLES:
            raise NotFound("unknown_table", {"table": table})
        spec = TABLES[table]
        pk = spec["pk"]
        if not isinstance(row_id, str) or not row_id:
            raise ValidationError("bad_row_id")
        if not isinstance(changes, dict):
            raise ValidationError("bad_changes")
        extra = sorted(set(changes) - set(spec["fields"]))
        if extra:
            raise ValidationError("unknown_fields", {"table": table, "fields": extra})
        if pk in changes:
            raise ValidationError("pk_immutable", {"table": table, "pk": pk})
        for f, v in changes.items():
            if v is not None and not isinstance(v, str):
                raise ValidationError("bad_value_type", {"table": table, "field": f})
        with self._tx():
            o = self._load_order(order_id)
            self._check_open(o)
            self._check_rev(o, rev)
            open_conf = self._conn.execute(
                "SELECT conflict_id FROM conflicts"
                " WHERE order_id=? AND table_name=? AND row_id=? AND status='open'",
                (order_id, table, row_id)).fetchall()
            if open_conf:
                raise RequestConflict("row_has_open_conflicts",
                                      {"table": table, "row_id": row_id,
                                       "conflict_ids": [r["conflict_id"] for r in open_conf]})
            existing = self._conn.execute(
                "SELECT field, value FROM cand_cells"
                " WHERE order_id=? AND table_name=? AND row_id=?",
                (order_id, table, row_id)).fetchall()
            merged = {f: None for f in spec["fields"]}
            for r in existing:
                merged[r["field"]] = r["value"]
            merged[pk] = row_id
            merged.update(changes)
            for f in spec["not_null"]:
                if merged[f] is None:
                    raise ValidationError("not_null_violation",
                                          {"table": table, "row_id": row_id, "field": f})
            self._conn.execute(
                "INSERT OR REPLACE INTO cand_rows(order_id, table_name, row_id, state)"
                " VALUES (?,?,?,'present')",
                (order_id, table, row_id))
            for f in spec["fields"]:
                self._conn.execute(
                    "INSERT OR REPLACE INTO cand_cells(order_id, table_name, row_id,"
                    " field, value, pending) VALUES (?,?,?,?,?,0)",
                    (order_id, table, row_id, f, merged[f]))
            new_rev = o["current_rev"] + 1
            self._bump_rev(order_id, new_rev)
            self._log(order_id, new_rev, "fix_row",
                      {"table": table, "row_id": row_id, "changes": changes})
        return self.get_order(order_id)

    # ------------------------------------------------------------------
    # 交付
    # ------------------------------------------------------------------

    def deliver(self, order_id, rev, request_id):
        """原子交付指定审定修订。

        仅当：冲突清零、候选关系合法、目标仍为建单版本时，在单个事务中完成
        全部目标表替换、目标升版与回执保存（交付的正是所检查的修订）。
        同一 request_id 重试始终返回原回执（即使目标后来变化）；
        已交付单收到不同 request_id 则拒绝。
        """
        if not isinstance(request_id, str) or not request_id:
            raise ValidationError("bad_request_id")
        with self._tx():
            o = self._load_order(order_id)
            if o["status"] == "delivered":
                receipt = self._load_receipt(order_id)
                payload = json.loads(receipt["payload"])
                if receipt["request_id"] != request_id:
                    raise RequestConflict("order_already_delivered",
                                          {"receipt": payload})
                return payload
            self._check_rev(o, rev)
            open_conflicts = [c for c in self._read_conflicts(order_id)
                              if c["status"] == "open"]
            if open_conflicts:
                raise OpenConflicts("unresolved_conflicts",
                                    {"conflicts": open_conflicts})
            view = self._resolved_view(order_id)
            violations = snapshot_violations(view)
            if violations:
                raise IntegrityViolation("candidate_not_deliverable",
                                         {"violations": violations})
            current_version = int(self._get_meta("target_version"))
            if current_version != o["target_version"]:
                raise TargetChanged("target_changed_recreate_order",
                                    {"order_target_version": o["target_version"],
                                     "current_target_version": current_version})
            self._replace_target(view)
            new_version = current_version + 1
            self._set_meta("target_version", str(new_version))
            now = _now()
            receipt = {
                "request_id": request_id,
                "order_id": order_id,
                "delivered_rev": rev,
                "base_version": o["base_version"],
                "previous_target_version": current_version,
                "new_target_version": new_version,
                "delivered_at": now,
                "tables": {t: len(view[t]) for t in TABLE_ORDER},
                "sha256": _hash_view(view),
            }
            self._conn.execute(
                "UPDATE orders SET status='delivered', delivered_at=? WHERE order_id=?",
                (now, order_id))
            self._conn.execute(
                "INSERT INTO receipts(order_id, request_id, payload, delivered_at)"
                " VALUES (?,?,?,?)",
                (order_id, request_id,
                 json.dumps(receipt, ensure_ascii=False, sort_keys=True), now))
            self._log(order_id, rev, "deliver",
                      {"request_id": request_id, "new_target_version": new_version})
            return receipt

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    def _load_order(self, order_id):
        row = self._conn.execute("SELECT * FROM orders WHERE order_id=?",
                                 (order_id,)).fetchone()
        if row is None:
            raise NotFound("order_not_found", {"order_id": order_id})
        return dict(row)

    @staticmethod
    def _check_open(o):
        if o["status"] != "open":
            raise OrderClosed("order_not_open",
                              {"order_id": o["order_id"], "status": o["status"]})

    @staticmethod
    def _check_rev(o, rev):
        if o["current_rev"] != rev:
            raise StaleRevision("stale_revision",
                                {"order_id": o["order_id"],
                                 "current_rev": o["current_rev"],
                                 "provided_rev": rev})

    def _bump_rev(self, order_id, new_rev):
        self._conn.execute("UPDATE orders SET current_rev=? WHERE order_id=?",
                           (new_rev, order_id))

    def _insert_snapshot(self, order_id, which, snap):
        for table in TABLE_ORDER:
            for rid, row in snap[table].items():
                for f, v in row.items():
                    self._conn.execute(
                        "INSERT INTO snap_cells(order_id, which, table_name, row_id,"
                        " field, value) VALUES (?,?,?,?,?,?)",
                        (order_id, which, table, rid, f, v))

    def _read_snapshot(self, order_id, which):
        snap = {t: {} for t in TABLE_ORDER}
        for r in self._conn.execute(
                "SELECT table_name, row_id, field, value FROM snap_cells"
                " WHERE order_id=? AND which=?", (order_id, which)):
            snap[r["table_name"]].setdefault(r["row_id"], {})[r["field"]] = r["value"]
        return snap

    def _insert_candidate(self, order_id, candidate):
        for table, rows in candidate.items():
            for rid, e in rows.items():
                self._conn.execute(
                    "INSERT INTO cand_rows(order_id, table_name, row_id, state)"
                    " VALUES (?,?,?,?)",
                    (order_id, table, rid, e["state"]))
                for f, (v, p) in e["cells"].items():
                    self._conn.execute(
                        "INSERT INTO cand_cells(order_id, table_name, row_id, field,"
                        " value, pending) VALUES (?,?,?,?,?,?)",
                        (order_id, table, rid, f, v, 1 if p else 0))

    def _read_candidate(self, order_id):
        cand = {t: {} for t in TABLE_ORDER}
        for r in self._conn.execute(
                "SELECT table_name, row_id, state FROM cand_rows WHERE order_id=?",
                (order_id,)):
            cand[r["table_name"]][r["row_id"]] = {"state": r["state"], "cells": {}}
        for r in self._conn.execute(
                "SELECT table_name, row_id, field, value, pending FROM cand_cells"
                " WHERE order_id=?", (order_id,)):
            cand[r["table_name"]][r["row_id"]]["cells"][r["field"]] = (
                r["value"], bool(r["pending"]))
        return cand

    def _candidate_public(self, cand):
        out = {}
        for table in TABLE_ORDER:
            rows = []
            for rid in sorted(cand.get(table, {})):
                e = cand[table][rid]
                fields = {}
                for f in TABLES[table]["fields"]:
                    value, pending = e["cells"].get(f, (None, False))
                    fields[f] = {"value": value, "pending": pending}
                row_pending = e["state"] == "pending" or any(
                    p for (_, p) in e["cells"].values())
                rows.append({"row_id": rid, "state": e["state"],
                             "pending": row_pending, "fields": fields})
            out[table] = rows
        return out

    def _resolved_view(self, order_id):
        cand = self._read_candidate(order_id)
        view = {}
        for table in TABLE_ORDER:
            rows = {}
            for rid, e in cand.get(table, {}).items():
                if e["state"] != "present":
                    raise AssertionError("pending row survived with no open conflict")
                row = {}
                for f in TABLES[table]["fields"]:
                    value, pending = e["cells"].get(f, (None, False))
                    if pending:
                        raise AssertionError("pending cell survived with no open conflict")
                    row[f] = value
                rows[rid] = row
            view[table] = rows
        return view

    def _insert_conflicts(self, order_id, conflicts):
        for c in conflicts:
            self._conn.execute(
                "INSERT INTO conflicts(order_id, conflict_id, table_name, row_id,"
                " field, kind, base_value, src_value, tgt_value, proposed_row, status)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,'open')",
                (order_id, c["conflict_id"], c["table"], c["row_id"], c["field"],
                 c["kind"], c["base_value"], c["src_value"], c["tgt_value"],
                 json.dumps(c["proposed_row"], ensure_ascii=False)
                 if c.get("proposed_row") is not None else None))

    @staticmethod
    def _conflict_from_row(r):
        d = {"conflict_id": r["conflict_id"], "table": r["table_name"],
             "row_id": r["row_id"], "field": r["field"], "kind": r["kind"],
             "base_value": r["base_value"], "src_value": r["src_value"],
             "tgt_value": r["tgt_value"], "status": r["status"],
             "resolution": r["resolution"], "resolved_value": r["resolved_value"],
             "resolved_rev": r["resolved_rev"]}
        if r["proposed_row"] is not None:
            d["proposed_row"] = json.loads(r["proposed_row"])
        return d

    def _read_conflicts(self, order_id):
        return [self._conflict_from_row(r) for r in self._conn.execute(
            "SELECT * FROM conflicts WHERE order_id=? ORDER BY conflict_id",
            (order_id,))]

    def _load_conflict(self, order_id, conflict_id):
        r = self._conn.execute(
            "SELECT * FROM conflicts WHERE order_id=? AND conflict_id=?",
            (order_id, conflict_id)).fetchone()
        if r is None:
            raise NotFound("conflict_not_found",
                           {"order_id": order_id, "conflict_id": conflict_id})
        return self._conflict_from_row(r)

    def _load_receipt(self, order_id):
        r = self._conn.execute("SELECT * FROM receipts WHERE order_id=?",
                               (order_id,)).fetchone()
        return None if r is None else dict(r)

    def _integrity(self, order_id, conflicts):
        open_list = [c for c in conflicts if c["status"] == "open"]
        if open_list:
            return {"status": "pending", "open_conflicts": len(open_list)}
        violations = snapshot_violations(self._resolved_view(order_id))
        if violations:
            return {"status": "invalid", "violations": violations}
        return {"status": "valid", "violations": []}

    def _log(self, order_id, rev, action, payload):
        seq = self._conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS s FROM history WHERE order_id=?",
            (order_id,)).fetchone()["s"]
        self._conn.execute(
            "INSERT INTO history(order_id, seq, rev, action, payload, at)"
            " VALUES (?,?,?,?,?,?)",
            (order_id, seq, rev, action,
             json.dumps(payload, ensure_ascii=False, sort_keys=True), _now()))

    def _read_history(self, order_id):
        out = []
        for r in self._conn.execute(
                "SELECT seq, rev, action, payload, at FROM history"
                " WHERE order_id=? ORDER BY seq", (order_id,)):
            out.append({"seq": r["seq"], "rev": r["rev"], "action": r["action"],
                        "payload": json.loads(r["payload"]), "at": r["at"]})
        return out
