"""SQLite 持久层结构：元信息、目标表、审定单（快照/候选/冲突/历史/回执）。

目标表按固定表结构建真实列（主键 + 非空约束由 SQLite 兜底，
唯一键与引用约束由服务层校验把关，见 validate.py）。
"""
from .schema import TABLES, TABLE_ORDER

_DDL = [
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS orders (
         order_id TEXT PRIMARY KEY,
         base_version INTEGER,
         target_version INTEGER NOT NULL,
         status TEXT NOT NULL,
         current_rev INTEGER NOT NULL,
         created_at TEXT NOT NULL,
         delivered_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS snap_cells (
         order_id TEXT NOT NULL, which TEXT NOT NULL, table_name TEXT NOT NULL,
         row_id TEXT NOT NULL, field TEXT NOT NULL, value TEXT,
         PRIMARY KEY (order_id, which, table_name, row_id, field))""",
    """CREATE TABLE IF NOT EXISTS cand_rows (
         order_id TEXT NOT NULL, table_name TEXT NOT NULL, row_id TEXT NOT NULL,
         state TEXT NOT NULL,
         PRIMARY KEY (order_id, table_name, row_id))""",
    """CREATE TABLE IF NOT EXISTS cand_cells (
         order_id TEXT NOT NULL, table_name TEXT NOT NULL, row_id TEXT NOT NULL,
         field TEXT NOT NULL, value TEXT, pending INTEGER NOT NULL DEFAULT 0,
         PRIMARY KEY (order_id, table_name, row_id, field))""",
    """CREATE TABLE IF NOT EXISTS conflicts (
         order_id TEXT NOT NULL, conflict_id INTEGER NOT NULL,
         table_name TEXT NOT NULL, row_id TEXT NOT NULL, field TEXT,
         kind TEXT NOT NULL,
         base_value TEXT, src_value TEXT, tgt_value TEXT, proposed_row TEXT,
         status TEXT NOT NULL, resolution TEXT, resolved_value TEXT, resolved_rev INTEGER,
         PRIMARY KEY (order_id, conflict_id))""",
    """CREATE TABLE IF NOT EXISTS history (
         order_id TEXT NOT NULL, seq INTEGER NOT NULL, rev INTEGER NOT NULL,
         action TEXT NOT NULL, payload TEXT NOT NULL, at TEXT NOT NULL,
         PRIMARY KEY (order_id, seq))""",
    """CREATE TABLE IF NOT EXISTS receipts (
         order_id TEXT PRIMARY KEY, request_id TEXT NOT NULL,
         payload TEXT NOT NULL, delivered_at TEXT NOT NULL)""",
]


def _target_ddl(table):
    spec = TABLES[table]
    cols = []
    for f in spec["fields"]:
        col = f'"{f}" TEXT'
        if f in spec["not_null"]:
            col += " NOT NULL"
        cols.append(col)
    cols.append(f'PRIMARY KEY ("{spec["pk"]}")')
    return f'CREATE TABLE IF NOT EXISTS target_{table} ({", ".join(cols)})'


def ensure_schema(conn):
    for stmt in _DDL:
        conn.execute(stmt)
    for table in TABLE_ORDER:
        conn.execute(_target_ddl(table))
