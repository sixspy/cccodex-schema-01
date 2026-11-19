import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from app.config import Config


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or Config.database()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=8, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 8000")
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
def immediate_tx(db_path: str | None = None):
    """排他写事务：切流、改指针等一次改库的动作都从这里进。

    BEGIN IMMEDIATE 在事务一开始就拿 SQLite 写锁；两名值班同时点切流时，
    后到者必须排队，进事务后再比对页面带来的现行版号，避免互相覆盖现行指针。
    """
    conn = connect(db_path)
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
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
  read_version INTEGER,
  write_versions TEXT NOT NULL DEFAULT '[]',
  cutover_mode TEXT NOT NULL DEFAULT '初始',
  registered_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS catalog_columns (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  table_name TEXT NOT NULL,
  column_name TEXT NOT NULL,
  column_type TEXT NOT NULL,
  added_version INTEGER,
  write_state TEXT NOT NULL DEFAULT '现行',
  UNIQUE(table_name, column_name)
);

CREATE TABLE IF NOT EXISTS cleaning_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_name TEXT NOT NULL UNIQUE,
  table_name TEXT NOT NULL,
  columns_json TEXT NOT NULL DEFAULT '[]',
  bound_version INTEGER,
  status TEXT NOT NULL DEFAULT '已对齐',
  last_detail TEXT NOT NULL DEFAULT '',
  last_aligned_at TEXT
);

CREATE TABLE IF NOT EXISTS cleaning_writes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id INTEGER NOT NULL,
  table_name TEXT NOT NULL,
  requested_columns TEXT NOT NULL DEFAULT '[]',
  persisted_columns TEXT NOT NULL DEFAULT '[]',
  accepted INTEGER NOT NULL DEFAULT 0,
  detail TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS progress_checks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scope TEXT NOT NULL CHECK (scope IN ('清洗','目录')),
  table_name TEXT NOT NULL,
  version_no INTEGER NOT NULL,
  run_id INTEGER NOT NULL DEFAULT 0,
  result TEXT NOT NULL CHECK (result IN ('通过','未通过')),
  detail TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS apply_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  contract_id INTEGER NOT NULL,
  table_name TEXT NOT NULL,
  from_version INTEGER,
  to_version INTEGER NOT NULL,
  result TEXT NOT NULL,
  strategy TEXT NOT NULL DEFAULT '',
  failure_reason TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS reconcile_reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  as_of TEXT NOT NULL,
  matched INTEGER NOT NULL,
  drifted INTEGER NOT NULL,
  summary TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

SEED = [
    ("dw_orders_d", 1, "现行", "初始现行", [("order_id", "INT"), ("amount", "DECIMAL"), ("biz_date", "DATE")]),
    ("dw_orders_d", 2, "草稿", "拟删 amount 改币种", [("order_id", "INT"), ("currency", "STRING"), ("biz_date", "DATE")]),
    ("dw_payments_d", 1, "现行", "支付现行", [("pay_id", "INT"), ("order_id", "INT"), ("paid_at", "TIMESTAMP")]),
]

CLEANING_SEED = [
    ("orders_core_clean", "dw_orders_d", ["order_id", "biz_date"], "只读主键与日期"),
    ("orders_amount_clean", "dw_orders_d", ["order_id", "amount"], "金额清洗仍按旧列读目录"),
    ("payments_clean", "dw_payments_d", ["pay_id", "paid_at"], "支付清洗"),
]


def _add_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def _migrate(conn: sqlite3.Connection) -> None:
    _add_column(conn, "catalog_tables", "read_version", "INTEGER")
    _add_column(conn, "catalog_tables", "write_versions", "TEXT NOT NULL DEFAULT '[]'")
    _add_column(conn, "catalog_tables", "cutover_mode", "TEXT NOT NULL DEFAULT '初始'")
    _add_column(conn, "catalog_columns", "write_state", "TEXT NOT NULL DEFAULT '现行'")
    _add_column(conn, "apply_runs", "strategy", "TEXT NOT NULL DEFAULT ''")
    _add_column(conn, "apply_runs", "failure_reason", "TEXT NOT NULL DEFAULT ''")
    conn.execute(
        "UPDATE catalog_tables SET read_version=live_version WHERE read_version IS NULL"
    )


def _seed_extras(conn: sqlite3.Connection) -> None:
    if conn.execute("SELECT value FROM meta WHERE key='seeded_v2'").fetchone():
        return

    for row in conn.execute("SELECT * FROM catalog_tables").fetchall():
        conn.execute(
            "UPDATE catalog_tables SET read_version=?, write_versions=? WHERE id=?",
            (row["live_version"], json.dumps([row["live_version"]]), row["id"]),
        )

    for live in conn.execute(
        "SELECT * FROM schema_contracts WHERE status='现行'"
    ).fetchall():
        for field in conn.execute(
            "SELECT field_name, field_type FROM contract_fields WHERE contract_id=?",
            (live["id"],),
        ).fetchall():
            conn.execute(
                "INSERT OR IGNORE INTO catalog_columns"
                "(table_name, column_name, column_type, added_version, write_state) "
                "VALUES (?,?,?,?, '现行')",
                (live["table_name"], field["field_name"], field["field_type"], live["version_no"]),
            )

    if not conn.execute("SELECT value FROM meta WHERE key='cutover_strategy'").fetchone():
        conn.execute(
            "INSERT INTO meta(key, value) VALUES ('cutover_strategy','lock')"
        )

    for job_name, table_name, columns, detail in CLEANING_SEED:
        live = conn.execute(
            "SELECT version_no FROM schema_contracts WHERE table_name=? AND status='现行'",
            (table_name,),
        ).fetchone()
        conn.execute(
            "INSERT OR IGNORE INTO cleaning_jobs"
            "(job_name, table_name, columns_json, bound_version, status, last_detail) "
            "VALUES (?,?,?,?,?,?)",
            (
                job_name,
                table_name,
                json.dumps(columns, ensure_ascii=False),
                live["version_no"] if live else None,
                "已对齐",
                detail,
            ),
        )

    conn.execute("INSERT INTO meta(key, value) VALUES ('seeded_v2','1')")


def _ensure_indexes(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_schema_contracts_live "
        "ON schema_contracts(table_name, status)"
    )


def init_db(db_path: str | None = None) -> None:
    with get_db(db_path) as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
        if not conn.execute("SELECT value FROM meta WHERE key='seeded'").fetchone():
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
                        "INSERT OR REPLACE INTO catalog_tables"
                        "(table_name, live_version, read_version, write_versions) "
                        "VALUES (?,?,?,?)",
                        (table, ver, ver, json.dumps([ver])),
                    )
            conn.execute("INSERT INTO meta(key, value) VALUES ('seeded','1')")
        _seed_extras(conn)
        _ensure_indexes(conn)
