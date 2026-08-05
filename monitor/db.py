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
    browser_mode TEXT,
    layer TEXT,
    burst_id TEXT
);

CREATE TABLE IF NOT EXISTS incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    duration_s INTEGER,
    checks_failed INTEGER,
    confidence INTEGER,
    trigger_layer TEXT,
    screenshot_path TEXT
);

CREATE TABLE IF NOT EXISTS state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    status TEXT NOT NULL,
    since_ts TEXT,
    burst_started_ts TEXT,
    confidence INTEGER NOT NULL DEFAULT 0,
    fail_reasons TEXT
);
"""


def get_connection(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: each web request gets its own connection (opened and closed
    # within that one request, never shared across concurrent requests), but FastAPI's sync
    # dependency-with-yield and the route handler it feeds aren't guaranteed to run on the
    # same anyio threadpool worker -- sqlite3's default same-thread check flags that as a
    # violation even though there's no actual concurrent access.
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _add_missing_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    for name, decl in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def _migrate_browser_mode(conn: sqlite3.Connection) -> None:
    """v3: checks.browser_mode was added after this table already existed in the wild.
    Backfill pre-existing rows as 'headless' (the only mode the scheduler has ever run)."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(checks)")}
    if "browser_mode" not in columns:
        conn.execute("ALTER TABLE checks ADD COLUMN browser_mode TEXT")
        conn.execute("UPDATE checks SET browser_mode = 'headless' WHERE browser_mode IS NULL")


def _migrate_stage5_burst_columns(conn: sqlite3.Connection) -> None:
    """Stage 5: checks gain layer/burst_id (each burst probe is tagged and audit-loggable);
    incidents gain confidence/trigger_layer (replacing the old consecutive-fail-count model);
    state gains burst_started_ts/confidence/fail_reasons and drops reliance on the old
    consecutive_fails column (left in place, unused, rather than dropped -- simplest safe
    migration for a column SQLite versions don't all support dropping)."""
    _add_missing_columns(conn, "checks", {"layer": "TEXT", "burst_id": "TEXT"})
    _add_missing_columns(conn, "incidents", {"confidence": "INTEGER", "trigger_layer": "TEXT"})
    _add_missing_columns(conn, "state", {
        "since_ts": "TEXT",
        "burst_started_ts": "TEXT",
        "confidence": "INTEGER NOT NULL DEFAULT 0",
        "fail_reasons": "TEXT",
    })


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _migrate_browser_mode(conn)
    _migrate_stage5_burst_columns(conn)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_checks_ts ON checks(ts)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_checks_burst_id ON checks(burst_id)")
    conn.execute(
        "INSERT OR IGNORE INTO state (id, status, since_ts, burst_started_ts, confidence, fail_reasons) "
        "VALUES (1, 'UP', NULL, NULL, 0, NULL)"
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
    layer: str = "render",
    burst_id: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO checks (ts, ok, http_status, latency_ms, fail_reason, browser_mode, layer, burst_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (ts, int(ok), http_status, latency_ms, fail_reason, browser_mode, layer, burst_id),
    )
    conn.commit()


def get_state(conn: sqlite3.Connection) -> MonitorState:
    row = conn.execute(
        "SELECT status, since_ts, burst_started_ts, confidence, fail_reasons FROM state WHERE id = 1"
    ).fetchone()
    fail_reasons = tuple(row["fail_reasons"].split(",")) if row["fail_reasons"] else ()
    return MonitorState(
        status=row["status"],
        since_ts=row["since_ts"],
        burst_started_ts=row["burst_started_ts"],
        confidence=row["confidence"],
        fail_reasons=fail_reasons,
    )


def set_state(conn: sqlite3.Connection, state: MonitorState) -> None:
    conn.execute(
        "UPDATE state SET status = ?, since_ts = ?, burst_started_ts = ?, confidence = ?, fail_reasons = ? WHERE id = 1",
        (
            state.status,
            state.since_ts,
            state.burst_started_ts,
            state.confidence,
            ",".join(state.fail_reasons) if state.fail_reasons else None,
        ),
    )
    conn.commit()


def open_incident(
    conn: sqlite3.Connection,
    started_at: str,
    confidence: int,
    trigger_layer: str,
    checks_failed: int,
    screenshot_path: str | None,
) -> int:
    cur = conn.execute(
        "INSERT INTO incidents (started_at, confidence, trigger_layer, checks_failed, screenshot_path) "
        "VALUES (?, ?, ?, ?, ?)",
        (started_at, confidence, trigger_layer, checks_failed, screenshot_path),
    )
    conn.commit()
    return cur.lastrowid


def close_incident(
    conn: sqlite3.Connection,
    ended_at: str,
    duration_s: int,
    confidence: int,
    checks_failed: int,
) -> None:
    conn.execute(
        """
        UPDATE incidents SET ended_at = ?, duration_s = ?, confidence = ?, checks_failed = ?
        WHERE id = (SELECT id FROM incidents WHERE ended_at IS NULL ORDER BY id DESC LIMIT 1)
        """,
        (ended_at, duration_s, confidence, checks_failed),
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
