import sqlite3
from contextlib import contextmanager
from pathlib import Path

from app.config import Config


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or Config.database()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def get_db(db_path: str | None = None):
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def transaction(db_path: str | None = None, immediate: bool = False):
    """排他写事务：immediate=True 时入口即拿 RESERVED 写锁，并发升版串行化。"""
    conn = connect(db_path)
    try:
        if immediate:
            conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_contracts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  table_name TEXT NOT NULL,
  version_no INTEGER NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('现行','草稿','停用')),
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(table_name, version_no)
);

CREATE TABLE IF NOT EXISTS contract_fields (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  contract_id INTEGER NOT NULL REFERENCES schema_contracts(id),
  field_name TEXT NOT NULL,
  field_type TEXT NOT NULL,
  UNIQUE(contract_id, field_name)
);

CREATE TABLE IF NOT EXISTS catalog_tables (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  table_name TEXT NOT NULL UNIQUE,
  live_version INTEGER NOT NULL,
  registered_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS apply_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  contract_id INTEGER NOT NULL,
  table_name TEXT NOT NULL,
  from_version INTEGER,
  to_version INTEGER NOT NULL,
  result TEXT NOT NULL,
  gate_verdict TEXT NOT NULL DEFAULT '',
  reason TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS reconcile_reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  as_of TEXT NOT NULL,
  matched INTEGER NOT NULL,
  drifted INTEGER NOT NULL,
  blocked INTEGER NOT NULL DEFAULT 0,
  summary TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS breaking_checks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  contract_id INTEGER NOT NULL REFERENCES schema_contracts(id),
  table_name TEXT NOT NULL,
  from_version INTEGER,
  to_version INTEGER NOT NULL,
  removed_fields TEXT NOT NULL,
  type_changed_fields TEXT NOT NULL,
  added_fields TEXT NOT NULL,
  verdict TEXT NOT NULL CHECK (verdict IN ('破坏性','兼容')),
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS gate_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  contract_id INTEGER NOT NULL,
  table_name TEXT NOT NULL,
  event TEXT NOT NULL CHECK (event IN ('硬拒','已应用','已回滚')),
  reason TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

# 旧库补列（已存在的表不会被 CREATE TABLE IF NOT EXISTS 改写）
_COLUMN_MIGRATIONS = {
    "apply_runs": {
        "gate_verdict": "TEXT NOT NULL DEFAULT ''",
        "reason": "TEXT NOT NULL DEFAULT ''",
    },
    "reconcile_reports": {
        "blocked": "INTEGER NOT NULL DEFAULT 0",
    },
}


def _ensure_columns(conn: sqlite3.Connection) -> None:
    for table, columns in _COLUMN_MIGRATIONS.items():
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, ddl in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


SEED = [
    ("dw_orders_d", 1, "现行", "初始现行", [("order_id", "INT"), ("amount", "DECIMAL"), ("biz_date", "DATE")]),
    ("dw_orders_d", 2, "草稿", "拟删 amount 改币种", [("order_id", "INT"), ("currency", "STRING"), ("biz_date", "DATE")]),
    ("dw_payments_d", 1, "现行", "支付现行", [("pay_id", "INT"), ("order_id", "INT"), ("paid_at", "TIMESTAMP")]),
]


def init_db(db_path: str | None = None) -> None:
    with get_db(db_path) as conn:
        conn.executescript(SCHEMA)
        _ensure_columns(conn)
        if conn.execute("SELECT value FROM meta WHERE key='seeded'").fetchone():
            return
        for table, ver, status, note, fields in SEED:
            cur = conn.execute(
                "INSERT INTO schema_contracts(table_name, version_no, status, note) VALUES (?,?,?,?)",
                (table, ver, status, note),
            )
            cid = cur.lastrowid
            for fname, ftype in fields:
                conn.execute(
                    "INSERT INTO contract_fields(contract_id, field_name, field_type) VALUES (?,?,?)",
                    (cid, fname, ftype),
                )
            if status == "现行":
                conn.execute(
                    "INSERT OR REPLACE INTO catalog_tables(table_name, live_version) VALUES (?,?)",
                    (table, ver),
                )
        conn.execute("INSERT INTO meta(key, value) VALUES ('seeded','1')")
