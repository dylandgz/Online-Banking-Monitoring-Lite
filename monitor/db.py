"""SQLite schema init and check-row writes. Parameterized SQL only."""
import sqlite3
from pathlib import Path

from monitor.state import MonitorState

SCHEMA = """
CREATE TABLE IF NOT EXISTS checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    ok INTEGER NOT NULL,
    http_status INTEGER,
    latency_ms REAL,
    fail_reason TEXT,
    browser_mode TEXT
);
CREATE INDEX IF NOT EXISTS idx_checks_ts ON checks(ts);

CREATE TABLE IF NOT EXISTS incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    duration_s INTEGER,
    checks_failed INTEGER,
    screenshot_path TEXT
);

CREATE TABLE IF NOT EXISTS state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    status TEXT NOT NULL,
    consecutive_fails INTEGER NOT NULL,
    since_ts TEXT
);
"""


def get_connection(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _migrate_browser_mode(conn: sqlite3.Connection) -> None:
    """v3: checks.browser_mode was added after this table already existed in the wild.
    CREATE TABLE IF NOT EXISTS won't add columns to an existing table, so add it explicitly
    and backfill pre-existing rows as 'headless' (the only mode the scheduler has ever run)."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(checks)")}
    if "browser_mode" not in columns:
        conn.execute("ALTER TABLE checks ADD COLUMN browser_mode TEXT")
        conn.execute("UPDATE checks SET browser_mode = 'headless' WHERE browser_mode IS NULL")
        conn.commit()


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _migrate_browser_mode(conn)
    conn.execute(
        "INSERT OR IGNORE INTO state (id, status, consecutive_fails, since_ts) VALUES (1, 'UP', 0, NULL)"
    )
    conn.commit()


def append_check(
    conn: sqlite3.Connection,
    ts: str,
    ok: bool,
    http_status: int | None,
    latency_ms: float,
    fail_reason: str | None,
    browser_mode: str = "headless",
) -> None:
    conn.execute(
        "INSERT INTO checks (ts, ok, http_status, latency_ms, fail_reason, browser_mode) VALUES (?, ?, ?, ?, ?, ?)",
        (ts, int(ok), http_status, latency_ms, fail_reason, browser_mode),
    )
    conn.commit()


def get_state(conn: sqlite3.Connection) -> MonitorState:
    row = conn.execute("SELECT status, consecutive_fails, since_ts FROM state WHERE id = 1").fetchone()
    return MonitorState(status=row["status"], consecutive_fails=row["consecutive_fails"], since_ts=row["since_ts"])


def set_state(conn: sqlite3.Connection, state: MonitorState) -> None:
    conn.execute(
        "UPDATE state SET status = ?, consecutive_fails = ?, since_ts = ? WHERE id = 1",
        (state.status, state.consecutive_fails, state.since_ts),
    )
    conn.commit()


def open_incident(
    conn: sqlite3.Connection,
    started_at: str,
    checks_failed: int,
    screenshot_path: str | None,
) -> int:
    cur = conn.execute(
        "INSERT INTO incidents (started_at, checks_failed, screenshot_path) VALUES (?, ?, ?)",
        (started_at, checks_failed, screenshot_path),
    )
    conn.commit()
    return cur.lastrowid


def close_incident(
    conn: sqlite3.Connection,
    ended_at: str,
    duration_s: int,
    checks_failed: int,
) -> None:
    conn.execute(
        """
        UPDATE incidents SET ended_at = ?, duration_s = ?, checks_failed = ?
        WHERE id = (SELECT id FROM incidents WHERE ended_at IS NULL ORDER BY id DESC LIMIT 1)
        """,
        (ended_at, duration_s, checks_failed),
    )
    conn.commit()


def get_last_check(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute("SELECT * FROM checks ORDER BY id DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def get_last_check_ts(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT ts FROM checks ORDER BY id DESC LIMIT 1").fetchone()
    return row["ts"] if row else None


def uptime_pct(conn: sqlite3.Connection, since_ts: str) -> float | None:
    row = conn.execute("SELECT AVG(ok) a, COUNT(*) c FROM checks WHERE ts >= ?", (since_ts,)).fetchone()
    if row["c"] == 0:
        return None
    return round(row["a"] * 100, 2)


def get_open_incident(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute("SELECT * FROM incidents WHERE ended_at IS NULL ORDER BY id DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def get_incident(conn: sqlite3.Connection, incident_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM incidents WHERE id = ?", (incident_id,)).fetchone()
    return dict(row) if row else None


def get_recent_incidents(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    rows = conn.execute("SELECT * FROM incidents ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def _range_clause(ts_from: str | None, ts_to: str | None) -> tuple[str, list]:
    where = []
    params: list = []
    if ts_from:
        where.append("ts >= ?")
        params.append(ts_from)
    if ts_to:
        where.append("ts <= ?")
        params.append(ts_to)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    return clause, params


def query_checks(
    conn: sqlite3.Connection,
    ts_from: str | None,
    ts_to: str | None,
    page: int,
    page_size: int,
) -> tuple[list[dict], int]:
    clause, params = _range_clause(ts_from, ts_to)
    total = conn.execute(f"SELECT COUNT(*) c FROM checks {clause}", params).fetchone()["c"]
    offset = (page - 1) * page_size
    rows = conn.execute(
        f"SELECT * FROM checks {clause} ORDER BY id DESC LIMIT ? OFFSET ?",
        params + [page_size, offset],
    ).fetchall()
    return [dict(r) for r in rows], total


def export_checks(conn: sqlite3.Connection, ts_from: str | None, ts_to: str | None) -> list[dict]:
    clause, params = _range_clause(ts_from, ts_to)
    rows = conn.execute(f"SELECT * FROM checks {clause} ORDER BY id ASC", params).fetchall()
    return [dict(r) for r in rows]
