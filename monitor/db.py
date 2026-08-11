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
    screenshot_path TEXT,
    track TEXT NOT NULL DEFAULT 'main'
);

-- track distinguishes independent status tracks sharing this DB: 'main' is the
-- pulse/render scheduler (unchanged since Stage 5); 'auth' is the Stage 6 sign-in
-- journey. Kept separate so an auth failure/recovery can never fake-trigger or
-- fake-clear the site-wide pulse/render incident, and vice versa -- they're
-- independent evidence about different things (reachable vs. sign-in works).
CREATE TABLE IF NOT EXISTS state (
    track TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    since_ts TEXT,
    burst_started_ts TEXT,
    confidence INTEGER NOT NULL DEFAULT 0,
    fail_reasons TEXT
);

CREATE TABLE IF NOT EXISTS login_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    ok INTEGER NOT NULL,
    latency_ms REAL,
    reason TEXT,
    http_status INTEGER,
    session_reused INTEGER NOT NULL DEFAULT 0
);

-- [v3.8 / Stage R] One row per 60s cycle, combining both tracks into the single
-- platform verdict CLAUDE.md now presents (worst_of(main, auth)). Probe-level evidence
-- (including burst re-probes) still lives in `checks`, linked back here via cycle_id --
-- `cycles` is the audit-primary, one-line-per-minute view; `checks` is the detail.
-- authed_ok is NULL, not FALSE, when the authed layer wasn't evaluable that cycle
-- (precursor already down) -- Rule 16: NULL != fail, it means "not this cycle's evidence".
CREATE TABLE IF NOT EXISTS cycles (
    cycle_id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    pulse_ok INTEGER,
    render_ok INTEGER,
    authed_ok INTEGER,
    session_reused INTEGER NOT NULL DEFAULT 0,
    pulse_latency_ms REAL,
    render_latency_ms REAL,
    authed_latency_ms REAL,
    verdict TEXT NOT NULL,
    fail_layer TEXT,
    fail_reason TEXT,
    burst_id TEXT
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


def _migrate_stage6_tracks(conn: sqlite3.Connection) -> None:
    """Stage 6: state/incidents gain a `track` column so the auth journey's status
    (its own UP/DOWN/CONFIG_ERROR, independent of pulse/render) can never fake-trigger
    or fake-clear the main site incident, and vice versa. state's old schema had a
    hardcoded single row (id PRIMARY KEY CHECK (id=1)) which can't hold a second
    ('auth') row, so this recreates the table -- standard SQLite rename/copy/drop,
    since ALTER TABLE can't drop a CHECK constraint. incidents just gains the column
    (its rows were never singleton-constrained)."""
    state_columns = {row["name"] for row in conn.execute("PRAGMA table_info(state)")}
    if "track" not in state_columns:
        conn.execute("ALTER TABLE state RENAME TO state_old")
        conn.execute(
            """
            CREATE TABLE state (
                track TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                since_ts TEXT,
                burst_started_ts TEXT,
                confidence INTEGER NOT NULL DEFAULT 0,
                fail_reasons TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO state (track, status, since_ts, burst_started_ts, confidence, fail_reasons) "
            "SELECT 'main', status, since_ts, burst_started_ts, confidence, fail_reasons FROM state_old"
        )
        conn.execute("DROP TABLE state_old")

    _add_missing_columns(conn, "incidents", {"track": "TEXT NOT NULL DEFAULT 'main'"})


def _migrate_stage_r_cycles(conn: sqlite3.Connection) -> None:
    """[v3.8 / Stage R] Additive only, per the amendment: checks gains cycle_id (nullable
    FK, no backfill -- historic rows predate the cycles table and simply have no cycle to
    point at; cycles itself is created fresh by SCHEMA above)."""
    _add_missing_columns(conn, "checks", {"cycle_id": "TEXT"})


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _migrate_browser_mode(conn)
    _migrate_stage5_burst_columns(conn)
    _migrate_stage6_tracks(conn)
    _migrate_stage_r_cycles(conn)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_checks_ts ON checks(ts)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_checks_burst_id ON checks(burst_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_checks_cycle_id ON checks(cycle_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_login_events_ts ON login_events(ts)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cycles_ts ON cycles(ts)")
    conn.execute("INSERT OR IGNORE INTO state (track, status, since_ts, burst_started_ts, confidence, fail_reasons) "
                 "VALUES ('main', 'UP', NULL, NULL, 0, NULL)")
    conn.execute("INSERT OR IGNORE INTO state (track, status, since_ts, burst_started_ts, confidence, fail_reasons) "
                 "VALUES ('auth', 'UP', NULL, NULL, 0, NULL)")
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
    cycle_id: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO checks (ts, ok, http_status, latency_ms, fail_reason, browser_mode, layer, burst_id, cycle_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (ts, int(ok), http_status, latency_ms, fail_reason, browser_mode, layer, burst_id, cycle_id),
    )
    conn.commit()


def append_login_event(
    conn: sqlite3.Connection,
    ts: str,
    ok: bool,
    latency_ms: float,
    reason: str | None,
    http_status: int | None = None,
    session_reused: bool = False,
) -> None:
    conn.execute(
        "INSERT INTO login_events (ts, ok, latency_ms, reason, http_status, session_reused) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (ts, int(ok), latency_ms, reason, http_status, int(session_reused)),
    )
    conn.commit()


def get_recent_login_events(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    rows = conn.execute("SELECT * FROM login_events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def count_login_events_since(conn: sqlite3.Connection, since_ts: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) c FROM login_events WHERE ts >= ?", (since_ts,)
    ).fetchone()["c"]


def get_state(conn: sqlite3.Connection, track: str = "main") -> MonitorState:
    row = conn.execute(
        "SELECT status, since_ts, burst_started_ts, confidence, fail_reasons FROM state WHERE track = ?",
        (track,),
    ).fetchone()
    fail_reasons = tuple(row["fail_reasons"].split(",")) if row["fail_reasons"] else ()
    return MonitorState(
        status=row["status"],
        since_ts=row["since_ts"],
        burst_started_ts=row["burst_started_ts"],
        confidence=row["confidence"],
        fail_reasons=fail_reasons,
    )


def set_state(conn: sqlite3.Connection, state: MonitorState, track: str = "main") -> None:
    conn.execute(
        "UPDATE state SET status = ?, since_ts = ?, burst_started_ts = ?, confidence = ?, fail_reasons = ? WHERE track = ?",
        (
            state.status,
            state.since_ts,
            state.burst_started_ts,
            state.confidence,
            ",".join(state.fail_reasons) if state.fail_reasons else None,
            track,
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
    track: str = "main",
) -> int:
    cur = conn.execute(
        "INSERT INTO incidents (started_at, confidence, trigger_layer, checks_failed, screenshot_path, track) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (started_at, confidence, trigger_layer, checks_failed, screenshot_path, track),
    )
    conn.commit()
    return cur.lastrowid


def close_incident(
    conn: sqlite3.Connection,
    ended_at: str,
    duration_s: int,
    confidence: int,
    checks_failed: int,
    track: str = "main",
) -> None:
    # Filtered by track: without this, two tracks each holding an open incident could
    # close each other's row (whichever is more recent by id) instead of their own.
    conn.execute(
        """
        UPDATE incidents SET ended_at = ?, duration_s = ?, confidence = ?, checks_failed = ?
        WHERE id = (SELECT id FROM incidents WHERE ended_at IS NULL AND track = ? ORDER BY id DESC LIMIT 1)
        """,
        (ended_at, duration_s, confidence, checks_failed, track),
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


def get_open_incident(conn: sqlite3.Connection, track: str = "main") -> dict | None:
    row = conn.execute(
        "SELECT * FROM incidents WHERE ended_at IS NULL AND track = ? ORDER BY id DESC LIMIT 1", (track,)
    ).fetchone()
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


# --- cycles [v3.8 / Stage R]: one row per minute, the primary audit view -----------------

def append_cycle(
    conn: sqlite3.Connection,
    cycle_id: str,
    ts: str,
    pulse_ok: bool | None,
    render_ok: bool | None,
    authed_ok: bool | None,
    verdict: str,
    session_reused: bool = False,
    pulse_latency_ms: float | None = None,
    render_latency_ms: float | None = None,
    authed_latency_ms: float | None = None,
    fail_layer: str | None = None,
    fail_reason: str | None = None,
    burst_id: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO cycles (
            cycle_id, ts, pulse_ok, render_ok, authed_ok, session_reused,
            pulse_latency_ms, render_latency_ms, authed_latency_ms,
            verdict, fail_layer, fail_reason, burst_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            cycle_id, ts,
            None if pulse_ok is None else int(pulse_ok),
            None if render_ok is None else int(render_ok),
            None if authed_ok is None else int(authed_ok),
            int(session_reused),
            pulse_latency_ms, render_latency_ms, authed_latency_ms,
            verdict, fail_layer, fail_reason, burst_id,
        ),
    )
    conn.commit()


def query_cycles(
    conn: sqlite3.Connection,
    ts_from: str | None,
    ts_to: str | None,
    page: int,
    page_size: int,
) -> tuple[list[dict], int]:
    clause, params = _range_clause(ts_from, ts_to)
    total = conn.execute(f"SELECT COUNT(*) c FROM cycles {clause}", params).fetchone()["c"]
    offset = (page - 1) * page_size
    # cycles has no autoincrement id (cycle_id is a UUID text PK) -- order by ts instead.
    rows = conn.execute(
        f"SELECT * FROM cycles {clause} ORDER BY ts DESC LIMIT ? OFFSET ?",
        params + [page_size, offset],
    ).fetchall()
    return [dict(r) for r in rows], total


def export_cycles(conn: sqlite3.Connection, ts_from: str | None, ts_to: str | None) -> list[dict]:
    clause, params = _range_clause(ts_from, ts_to)
    rows = conn.execute(f"SELECT * FROM cycles {clause} ORDER BY ts ASC", params).fetchall()
    return [dict(r) for r in rows]


def get_checks_for_cycle(conn: sqlite3.Connection, cycle_id: str) -> list[dict]:
    """Expandable probe detail for a single cycle row (dashboard drill-down)."""
    rows = conn.execute(
        "SELECT * FROM checks WHERE cycle_id = ? ORDER BY id ASC", (cycle_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_last_cycle(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute("SELECT * FROM cycles ORDER BY ts DESC LIMIT 1").fetchone()
    return dict(row) if row else None
