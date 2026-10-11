"""SQLite access: one connection per request, schema creation."""
import sqlite3
from datetime import datetime, timezone

from flask import current_app, g

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    username             TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash        TEXT NOT NULL,
    role                 TEXT NOT NULL CHECK (role IN ('admin', 'agent', 'user')),
    is_active            INTEGER NOT NULL DEFAULT 1,
    must_change_password INTEGER NOT NULL DEFAULT 1,
    failed_logins        INTEGER NOT NULL DEFAULT 0,
    locked_until         TEXT,
    password_changed_at  TEXT NOT NULL,
    last_login_at        TEXT,
    created_at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS voters (
    national_id TEXT PRIMARY KEY,
    full_name   TEXT NOT NULL,
    sex         TEXT NOT NULL DEFAULT '' CHECK (sex IN ('M', 'F', '')),
    age         INTEGER CHECK (age IS NULL OR (age >= 0 AND age <= 130)),
    house_name  TEXT NOT NULL DEFAULT '',
    party       TEXT NOT NULL DEFAULT '',
    phone       TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'new',
    status_at   TEXT,
    status_by   INTEGER REFERENCES users(id) ON DELETE SET NULL,
    notes       TEXT NOT NULL DEFAULT '',
    photo_path  TEXT,
    source      TEXT NOT NULL DEFAULT 'import',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    updated_by  INTEGER REFERENCES users(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS ix_voters_name  ON voters (full_name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS ix_voters_house ON voters (house_name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS ix_voters_party ON voters (party);

CREATE TABLE IF NOT EXISTS house_assignments (
    house_name  TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    assigned_at TEXT NOT NULL,
    assigned_by INTEGER REFERENCES users(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS ix_assign_user ON house_assignments (user_id);

CREATE TABLE IF NOT EXISTS audit_log (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    at       TEXT NOT NULL,
    user_id  INTEGER,
    username TEXT,
    action   TEXT NOT NULL,
    target   TEXT,
    detail   TEXT,
    ip       TEXT
);
CREATE INDEX IF NOT EXISTS ix_audit_at ON audit_log (at);
"""


def now_iso(precise=False):
    now = datetime.now(timezone.utc)
    return now.isoformat() if precise else now.replace(microsecond=0).isoformat()


def connect(path):
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


def _allow_agent_role(conn):
    """Databases from before the agent role have CHECK (role IN ('admin', 'user')): rebuild the table."""
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'users'").fetchone()
    if not sql or "'agent'" in sql[0]:
        return
    new_sql = sql[0].replace("CREATE TABLE users", "CREATE TABLE users_new", 1).replace(
        "CHECK (role IN ('admin', 'user'))", "CHECK (role IN ('admin', 'agent', 'user'))")
    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")  # other tables reference users; keep their rows as they are
    try:
        conn.executescript(
            "BEGIN;"
            f"{new_sql};"
            "INSERT INTO users_new SELECT * FROM users;"
            "DROP TABLE users;"
            "ALTER TABLE users_new RENAME TO users;"
            "COMMIT;"
        )
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def init_schema(conn):
    _allow_agent_role(conn)
    conn.executescript(SCHEMA)
    # Columns added after the first release
    columns = {r[1] for r in conn.execute("PRAGMA table_info(voters)")}
    for name, ddl in (
        ("phone", "phone TEXT NOT NULL DEFAULT ''"),
        ("status", "status TEXT NOT NULL DEFAULT 'new'"),
        ("status_at", "status_at TEXT"),
        ("status_by", "status_by INTEGER REFERENCES users(id) ON DELETE SET NULL"),
        ("notes", "notes TEXT NOT NULL DEFAULT ''"),
    ):
        if name not in columns:
            conn.execute(f"ALTER TABLE voters ADD COLUMN {ddl}")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_voters_status ON voters (status)")
    conn.commit()


def get_db():
    if "db" not in g:
        g.db = connect(current_app.config["DATABASE"])
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()
