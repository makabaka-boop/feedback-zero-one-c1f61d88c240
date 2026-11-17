"""SQLite 持久层: 全部状态落库, 重启后已确认修订、裁决与交付结果均保留。

内部表:
- meta: 初始化标记、目标版本号、审定单序号;
- tgt_<表>: 当前目标数据(真实表, 交付时整体替换);
- orders: 审定单(修订号、状态、建单时目标版本);
- snapshots: 建单快照与候选行(EAV 存储, 值以 JSON 编码,
  未决标记编码为 JSON 对象, 与字符串/空值天然区分);
- conflicts: 冲突(原冲突内容永久保留, 裁决结果单独记录);
- history: 裁决/修正历史(仅追加);
- deliveries: 交付回执与幂等键。
"""

import json
import sqlite3
from contextlib import contextmanager

from .schema import TABLES, TABLE_ORDER

INTERNAL_DDL = """
CREATE TABLE IF NOT EXISTS meta(
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orders(
  order_id                   TEXT PRIMARY KEY,
  revision                   INTEGER NOT NULL,
  status                     TEXT NOT NULL CHECK(status IN ('open','delivered')),
  target_version_at_creation INTEGER NOT NULL,
  created_at                 TEXT NOT NULL,
  delivered_at               TEXT
);
CREATE TABLE IF NOT EXISTS snapshots(
  order_id   TEXT NOT NULL,
  kind       TEXT NOT NULL,
  table_name TEXT NOT NULL,
  row_id     TEXT NOT NULL,
  field      TEXT NOT NULL,
  value_json TEXT,
  PRIMARY KEY(order_id, kind, table_name, row_id, field)
);
CREATE TABLE IF NOT EXISTS conflicts(
  order_id        TEXT NOT NULL,
  conflict_id     TEXT NOT NULL,
  table_name      TEXT NOT NULL,
  row_id          TEXT NOT NULL,
  field           TEXT,
  kind            TEXT NOT NULL,
  detail_json     TEXT NOT NULL,
  status          TEXT NOT NULL CHECK(status IN ('pending','resolved')),
  resolution_json TEXT,
  resolved_at     TEXT,
  PRIMARY KEY(order_id, conflict_id)
);
CREATE TABLE IF NOT EXISTS history(
  seq             INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id        TEXT NOT NULL,
  revision_before INTEGER NOT NULL,
  revision_after  INTEGER NOT NULL,
  actions_json    TEXT NOT NULL,
  at              TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS deliveries(
  request_id   TEXT PRIMARY KEY,
  order_id     TEXT NOT NULL,
  revision     INTEGER NOT NULL,
  receipt_json TEXT NOT NULL,
  at           TEXT NOT NULL
);
"""


def _rowdict(row):
    return {k: row[k] for k in row.keys()}


class Storage:
    def __init__(self, path):
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.executescript(INTERNAL_DDL)

    def close(self):
        self.conn.close()

    @contextmanager
    def txn(self):
        """写事务: BEGIN IMMEDIATE 保证检查与写入原子完成, 异常即回滚。"""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    # ---------- meta ----------
    def get_meta(self, key, default=None):
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key, value):
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )

    # ---------- 目标表 ----------
    def create_target_tables(self):
        for table in TABLE_ORDER:
            spec = TABLES[table]
            cols = []
            for f in spec["fields"]:
                nn = " NOT NULL" if f in spec["not_null"] else ""
                cols.append(f'"{f}" TEXT{nn}')
            cols.append('PRIMARY KEY("id")')
            for uk in spec["unique_keys"]:
                cols.append("UNIQUE(" + ", ".join(f'"{f}"' for f in uk) + ")")
            self.conn.execute(f'CREATE TABLE IF NOT EXISTS "tgt_{table}" ({", ".join(cols)})')

    def load_target(self):
        out = {}
        for table in TABLE_ORDER:
            fields = TABLES[table]["fields"]
            cur = self.conn.execute(
                f"SELECT {', '.join(fields)} FROM \"tgt_{table}\""
            )
            out[table] = {r["id"]: {f: r[f] for f in fields} for r in cur}
        return out

    def replace_target(self, data):
        """整体替换全部目标表(须在事务内调用, 保证不会只替换部分表)。"""
        for table in TABLE_ORDER:
            fields = TABLES[table]["fields"]
            self.conn.execute(f'DELETE FROM "tgt_{table}"')
            cols = ", ".join(fields)
            marks = ", ".join("?" for _ in fields)
            for row in data[table].values():
                self.conn.execute(
                    f'INSERT INTO "tgt_{table}" ({cols}) VALUES ({marks})',
                    [row[f] for f in fields],
                )

    # ---------- 快照 / 候选 ----------
    def save_rows(self, order_id, kind, data):
        self.conn.execute(
            "DELETE FROM snapshots WHERE order_id=? AND kind=?", (order_id, kind)
        )
        for table in TABLE_ORDER:
            for rid, row in data.get(table, {}).items():
                for f in TABLES[table]["fields"]:
                    self.conn.execute(
                        "INSERT INTO snapshots(order_id, kind, table_name, row_id, field, value_json)"
                        " VALUES(?,?,?,?,?,?)",
                        (order_id, kind, table, rid, f, json.dumps(row.get(f), ensure_ascii=False)),
                    )

    def load_rows(self, order_id, kind):
        out = {table: {} for table in TABLE_ORDER}
        cur = self.conn.execute(
            "SELECT table_name, row_id, field, value_json FROM snapshots"
            " WHERE order_id=? AND kind=?",
            (order_id, kind),
        )
        for r in cur:
            table = r["table_name"]
            if table not in out:
                continue
            row = out[table].setdefault(r["row_id"], {})
            row[r["field"]] = json.loads(r["value_json"])
        return out

    # ---------- 审定单 ----------
    def insert_order(self, order_id, revision, status, tvac, created_at):
        self.conn.execute(
            "INSERT INTO orders(order_id, revision, status, target_version_at_creation, created_at)"
            " VALUES(?,?,?,?,?)",
            (order_id, revision, status, tvac, created_at),
        )

    def get_order_row(self, order_id):
        r = self.conn.execute(
            "SELECT * FROM orders WHERE order_id=?", (order_id,)
        ).fetchone()
        return _rowdict(r) if r else None

    def list_orders(self):
        cur = self.conn.execute(
            "SELECT order_id, revision, status, target_version_at_creation, created_at, delivered_at"
            " FROM orders ORDER BY order_id"
        )
        return [_rowdict(r) for r in cur]

    def update_order_revision(self, order_id, revision):
        self.conn.execute(
            "UPDATE orders SET revision=? WHERE order_id=?", (revision, order_id)
        )

    def mark_order_delivered(self, order_id, delivered_at):
        self.conn.execute(
            "UPDATE orders SET status='delivered', delivered_at=? WHERE order_id=?",
            (delivered_at, order_id),
        )

    # ---------- 冲突 ----------
    def save_conflicts(self, order_id, conflicts):
        self.conn.execute("DELETE FROM conflicts WHERE order_id=?", (order_id,))
        for c in conflicts:
            self.conn.execute(
                "INSERT INTO conflicts(order_id, conflict_id, table_name, row_id, field, kind,"
                " detail_json, status, resolution_json, resolved_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    order_id,
                    c["conflict_id"],
                    c["table"],
                    c["row_id"],
                    c["field"],
                    c["kind"],
                    json.dumps(c["detail"], ensure_ascii=False),
                    c["status"],
                    json.dumps(c["resolution"], ensure_ascii=False)
                    if c.get("resolution") is not None
                    else None,
                    c.get("resolved_at"),
                ),
            )

    def load_conflicts(self, order_id):
        cur = self.conn.execute(
            "SELECT * FROM conflicts WHERE order_id=?"
            " ORDER BY CAST(SUBSTR(conflict_id, 2) AS INTEGER)",
            (order_id,),
        )
        out = []
        for r in cur:
            out.append(
                {
                    "conflict_id": r["conflict_id"],
                    "table": r["table_name"],
                    "row_id": r["row_id"],
                    "field": r["field"],
                    "kind": r["kind"],
                    "detail": json.loads(r["detail_json"]),
                    "status": r["status"],
                    "resolution": json.loads(r["resolution_json"])
                    if r["resolution_json"]
                    else None,
                    "resolved_at": r["resolved_at"],
                }
            )
        return out

    # ---------- 历史 ----------
    def append_history(self, order_id, before, after, actions, at):
        self.conn.execute(
            "INSERT INTO history(order_id, revision_before, revision_after, actions_json, at)"
            " VALUES(?,?,?,?,?)",
            (order_id, before, after, json.dumps(actions, ensure_ascii=False), at),
        )

    def list_history(self, order_id):
        cur = self.conn.execute(
            "SELECT seq, revision_before, revision_after, actions_json, at"
            " FROM history WHERE order_id=? ORDER BY seq",
            (order_id,),
        )
        return [
            {
                "seq": r["seq"],
                "revision_before": r["revision_before"],
                "revision_after": r["revision_after"],
                "actions": json.loads(r["actions_json"]),
                "at": r["at"],
            }
            for r in cur
        ]

    # ---------- 交付 ----------
    def put_delivery(self, request_id, order_id, revision, receipt, at):
        self.conn.execute(
            "INSERT INTO deliveries(request_id, order_id, revision, receipt_json, at)"
            " VALUES(?,?,?,?,?)",
            (request_id, order_id, revision, json.dumps(receipt, ensure_ascii=False), at),
        )

    def get_delivery(self, request_id):
        r = self.conn.execute(
            "SELECT * FROM deliveries WHERE request_id=?", (request_id,)
        ).fetchone()
        if not r:
            return None
        return {
            "request_id": r["request_id"],
            "order_id": r["order_id"],
            "revision": r["revision"],
            "receipt": json.loads(r["receipt_json"]),
            "at": r["at"],
        }

    def get_delivery_for_order(self, order_id):
        r = self.conn.execute(
            "SELECT * FROM deliveries WHERE order_id=? ORDER BY at", (order_id,)
        ).fetchone()
        if not r:
            return None
        return {
            "request_id": r["request_id"],
            "order_id": r["order_id"],
            "revision": r["revision"],
            "receipt": json.loads(r["receipt_json"]),
            "at": r["at"],
        }
