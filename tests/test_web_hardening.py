"""Tests for the Stage H P1 web/DB hardening.

The web layer has never had automated coverage (verified by throwaway smoke scripts
instead -- see PROGRESS.md). These pin the four P1 guarantees specifically, because three
of them are security properties that would fail silently and the fourth (export memory)
is what keeps the dashboard from killing the monitor it reports on:

  1. /docs, /redoc, /openapi.json are not served;
  2. unset dashboard credentials deny everyone instead of admitting everyone;
  3. WAL + busy_timeout are actually set on every connection;
  4. the export streams lazily, stays byte-identical to the materialised path (Rule 15 "every probe writes a checks row"),
     and refuses an oversized zip rather than truncating it.

starlette's TestClient needs httpx2, which isn't a project dependency (CLAUDE.md requires
stop-and-ask before adding one), so these drive the ASGI app directly. The helper is small
and deliberately local to this file rather than a shared fixture.
"""
import asyncio
import base64
import io
import sys
import zipfile
from urllib.parse import urlsplit

import pytest

import config
from monitor import db

# The FastAPI instance re-exported by monitor/web/__init__.py shadows this submodule, so
# the module object has to come from sys.modules -- see that file's docstring.
import monitor.web.app  # noqa: F401  (registers the module)
web_app = sys.modules["monitor.web.app"]


def _call(path: str, auth: tuple[str, str] | None = None):
    """Minimal ASGI call. Returns (status, body_bytes)."""
    split = urlsplit(path)
    headers = []
    if auth is not None:
        token = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
        headers.append((b"authorization", f"Basic {token}".encode()))

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "GET", "scheme": "http", "path": split.path,
        "raw_path": split.path.encode(), "query_string": split.query.encode(),
        "root_path": "", "headers": headers,
        "client": ("127.0.0.1", 1), "server": ("127.0.0.1", 8080),
    }
    got = {"status": None, "chunks": []}
    sent_body = {"done": False}

    async def receive():
        if not sent_body["done"]:
            sent_body["done"] = True
            return {"type": "http.request", "body": b"", "more_body": False}
        # Must block, not return http.disconnect: StreamingResponse races its body
        # generator against a disconnect watcher, and an immediate disconnect makes
        # starlette abandon the body and emit an empty response.
        await asyncio.Event().wait()

    async def send(message):
        if message["type"] == "http.response.start":
            got["status"] = message["status"]
        elif message["type"] == "http.response.body":
            got["chunks"].append(message.get("body", b""))

    asyncio.run(web_app.app(scope, receive, send))
    return got["status"], b"".join(got["chunks"])


@pytest.fixture
def seeded_db(tmp_path, monkeypatch):
    """A real sqlite file with 30 cycles and 30 checks, wired in as config.DB_PATH."""
    path = str(tmp_path / "web.db")
    monkeypatch.setattr(config, "DB_PATH", path)
    monkeypatch.setattr(config, "DASHBOARD_USER", "user")
    monkeypatch.setattr(config, "DASHBOARD_PASSWORD", "pass")

    conn = db.get_connection(path)
    db.init_db(conn)
    for i in range(30):
        ts = f"2026-08-24T10:{i:02d}:00.000000+00:00"
        cid = f"cycle-{i:03d}"
        db.append_cycle(conn, cycle_id=cid, ts=ts, pulse_ok=True, render_ok=True,
                        authed_ok=None, verdict="UP")
        db.append_check(conn, ts=ts, ok=True, http_status=200, latency_ms=float(i),
                        fail_reason=None, browser_mode="headless", layer="render",
                        burst_id=None, cycle_id=cid)
    conn.commit()
    yield conn, path
    conn.close()


# --- 1: the interactive docs routes must not be served ------------------------------

@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"])
def test_docs_routes_are_not_served(seeded_db, path):
    """FastAPI's defaults exposed these with no auth dependency, contradicting the
    "auth on everything but /healthz" contract and handing out the full route map."""
    status, _ = _call(path)
    assert status == 404


def test_healthz_is_still_open_and_api_still_requires_auth(seeded_db):
    """Closing the docs routes must not have closed /healthz or opened the API.

    /healthz is asserted as "not 401" rather than "200": the fixture's newest row is
    deliberately older than 3x CHECK_INTERVAL_S, so the staleness guard correctly reports
    503. What matters here is that the route needs no credentials -- and 503-on-stale is
    itself the behaviour a systemd/Tailscale probe depends on."""
    status, _ = _call("/healthz")
    assert status != 401
    assert status in (200, 503)

    assert _call("/api/status")[0] == 401
    assert _call("/api/status", auth=("user", "pass"))[0] == 200
    assert _call("/api/status", auth=("user", "wrong"))[0] == 401


# --- 2: auth must fail closed when unconfigured -------------------------------------

def test_unset_credentials_deny_rather_than_admit(seeded_db, monkeypatch):
    """compare_digest("", "") is True, so blank config used to authenticate `curl -u ":"`.
    Reachable via `uvicorn monitor.web:app`, which never calls config.validate_core()."""
    monkeypatch.setattr(config, "DASHBOARD_USER", None)
    monkeypatch.setattr(config, "DASHBOARD_PASSWORD", None)

    assert _call("/api/status", auth=("", ""))[0] == 503
    assert _call("/api/status", auth=("anything", "atall"))[0] == 503


# --- 3: WAL + busy_timeout on every connection --------------------------------------

def test_connections_use_wal_and_a_long_busy_timeout(tmp_path):
    """Without WAL a dashboard export's read lock can make the cycle's write raise
    `database is locked`, which costs a whole minute of monitoring."""
    conn = db.get_connection(str(tmp_path / "pragma.db"))
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 15000
    finally:
        conn.close()


def test_a_reader_does_not_block_the_writer(seeded_db):
    """The behavioural point of WAL: an open read must not stop an append from committing."""
    conn, path = seeded_db
    reader = db.get_connection(path)
    try:
        cursor = reader.execute("SELECT * FROM checks")
        cursor.fetchone()  # leave the read open mid-result
        db.append_check(conn, ts="2026-08-24T11:00:00+00:00", ok=True, http_status=200,
                        latency_ms=1.0, fail_reason=None, browser_mode="headless",
                        layer="render", burst_id=None, cycle_id="cycle-000")
        conn.commit()
    finally:
        reader.close()


# --- 4: the export is lazy, faithful, and bounded -----------------------------------

def test_iter_export_is_lazy(seeded_db):
    """It must yield off a live cursor, not materialise a list -- that laziness is the
    whole memory fix (measured 715MB+ peak at one year of data before it)."""
    conn, _ = seeded_db
    import types
    it = db.iter_export(conn, "cycles", None, None)
    assert isinstance(it, types.GeneratorType)
    assert next(it)["cycle_id"] == "cycle-000"  # first row available before the rest are read


def test_iter_export_rejects_a_table_not_on_the_allowlist(seeded_db):
    """A table name selects a constant query by comparison; anything else must not run SQL.

    ValueError, not the KeyError this asserted before the 2026-09-22 AppScan pass: the
    lookup happened inside the generator, so the rejection only surfaced once the route was
    already streaming a response."""
    conn, _ = seeded_db
    with pytest.raises(ValueError):
        next(db.iter_export(conn, "incidents; DROP TABLE checks", None, None))


def test_count_export_rows_rejects_a_table_not_on_the_allowlist(seeded_db):
    """The zip's pre-flight count shares iter_export's gate, so it must share its refusal --
    it runs first, and a gap here would build SQL before the streaming path ever checked."""
    conn, _ = seeded_db
    with pytest.raises(ValueError):
        db.count_export_rows(conn, "incidents; DROP TABLE checks", None, None)


@pytest.mark.parametrize("table", ["cycles", "checks"])
def test_streamed_export_is_byte_identical_to_a_plain_select(seeded_db, table):
    """Rule 15 "every probe writes a checks row": the export stays row-for-row faithful.

    The oracle is a plain SELECT rather than db.export_cycles/export_checks, which this
    asserted against until they were removed on 2026-09-22. That is a strengthening, not a
    substitution: those were siblings of iter_export inside the module under test, sharing
    its helpers, so a defect in the shared part could have moved both sides of the
    comparison together. Rows straight off the connection cannot."""
    conn, _ = seeded_db
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM cycles ORDER BY ts ASC" if table == "cycles"
        else "SELECT * FROM checks ORDER BY id ASC"
    )]
    expected = "".join(web_app._csv_lines(table, rows)).encode()

    status, streamed = _call(f"/api/export?table={table}", auth=("user", "pass"))

    assert status == 200
    assert streamed == expected
    assert streamed.count(b"\n") == 31  # header + 30 rows


def test_zip_members_match_the_single_table_exports(seeded_db):
    """table=all must stay reconcilable against the individual exports -- the reason the
    two tables are zipped rather than joined."""
    status, body = _call("/api/export?table=all", auth=("user", "pass"))
    assert status == 200

    bundle = zipfile.ZipFile(io.BytesIO(body))
    assert sorted(bundle.namelist()) == ["checks.csv", "cycles.csv"]
    for table in ("cycles", "checks"):
        _, single = _call(f"/api/export?table={table}", auth=("user", "pass"))
        assert bundle.read(f"{table}.csv") == single


def test_oversized_zip_is_refused_not_truncated(seeded_db, monkeypatch):
    """A partial audit export presented as complete would be worse than no export, so the
    cap returns 413 with guidance instead of silently cutting rows."""
    monkeypatch.setattr(web_app, "_MAX_ZIP_ROWS_PER_TABLE", 5)

    status, body = _call("/api/export?table=all", auth=("user", "pass"))

    assert status == 413
    detail = body.decode().lower()
    assert "30" in detail and "narrow" in detail


def test_single_table_export_is_not_subject_to_the_zip_cap(seeded_db, monkeypatch):
    """The cap exists because the zip must be buffered; the streaming path has no such
    constraint and must stay usable for a full-history audit export."""
    monkeypatch.setattr(web_app, "_MAX_ZIP_ROWS_PER_TABLE", 5)

    status, body = _call("/api/export?table=checks", auth=("user", "pass"))

    assert status == 200
    assert body.count(b"\n") == 31


# The fixture seeds 30 rows at 2026-08-24T10:00 .. T10:29, one per minute.
_T = "2026-08-24T10:%02d:00.000000+00:00"

@pytest.mark.parametrize("ts_from, ts_to, expected", [
    (None,      None,      30),  # no window at all
    (_T % 10,   None,      20),  # lower bound only  -> minutes 10..29
    (None,      _T % 9,    10),  # upper bound only  -> minutes 00..09
    (_T % 10,   _T % 19,   10),  # both bounds
    ("",        "",        30),  # see the regression note below
    ("",        None,      30),
    (None,      "",        30),
])
def test_ts_window_bounds_select_the_same_rows_on_every_query_path(
    seeded_db, ts_from, ts_to, expected
):
    """[AppScan SQLi pass, 2026-09-22] The sentinel bounds must window exactly as the old
    runtime-assembled WHERE clause did, on all four query paths at once.

    The empty-string rows are the regression guard, and they caught a real defect during
    this rewrite. `_range_clause` tested `if ts_to:`, so '' meant "no bound"; COALESCE only
    substitutes on NULL, so binding '' directly makes `ts <= ''` match nothing. Before
    `_range_bounds` normalised it, `/api/export?table=cycles&to=` returned a zero-row CSV
    with HTTP 200 -- a silent truncation of an audit export, which Rule 15 requires be a
    refusal. The dashboard never sends a bare `to=` (dashboard.js guards with `if (to)`),
    so nothing in the UI would have shown this."""
    conn, _ = seeded_db

    assert len(list(db.iter_export(conn, "cycles", ts_from, ts_to))) == expected
    assert len(list(db.iter_export(conn, "checks", ts_from, ts_to))) == expected
    assert db.count_export_rows(conn, "cycles", ts_from, ts_to) == expected
    assert db.count_export_rows(conn, "checks", ts_from, ts_to) == expected

    _, total = db.query_cycles(conn, ts_from, ts_to, page=1, page_size=500)
    assert total == expected


def test_export_still_honours_the_date_filter(seeded_db):
    """Regression guard on the streaming rewrite: the range clause must still apply."""
    status, body = _call(
        "/api/export?table=cycles&from=2026-08-24T10:20:00.000000%2B00:00",
        auth=("user", "pass"),
    )
    assert status == 200
    lines = body.count(b"\n")
    assert 1 < lines < 31


# --- 5: the cycle_id path parameter is constrained ----------------------------------
#
# [AppScan "SSRF" finding against dashboard.js, 2026-09-23] The finding itself is mislabelled
# -- the flagged call is the browser fetching a relative same-origin path, and this route
# makes no outbound request with the value; it binds it into a SQL parameter. But cycle_id
# was the only path parameter in the app with no constraint on it (incident_id is an int,
# /static/{filename} is an allowlist lookup), and the browser-side guard in dashboard.js is
# advisory -- the browser is what an attacker controls. This is the half that cannot be
# bypassed, so it is the half worth pinning.
#
# The pattern is a character allowlist rather than a uuid shape, deliberately: see the
# comment on the route. These tests therefore assert what it must REFUSE (anything that could
# change what a url means) and that an ordinary id still works -- note the fixture writes
# "cycle-000", not a uuid, which is exactly the format drift the looser pattern tolerates.

def test_the_cycle_drilldown_still_serves_an_ordinary_id(seeded_db):
    status, body = _call("/api/cycle/cycle-000", auth=("user", "pass"))
    assert status == 200
    assert b'"layer"' in body, "the drill-down returned no probe rows"


@pytest.mark.parametrize("bad", [
    "..%2f..%2fetc%2fpasswd",   # traversal, percent-encoded so it survives to the route
    "javascript:alert(1)",      # a scheme
    "http:%2f%2fevil.example",  # an absolute url
    "id with spaces",
    "a" * 65,                   # longer than any id this app generates
])
def test_a_malformed_cycle_id_is_refused_at_the_boundary(seeded_db, bad):
    """422, not an empty 200. The old behaviour ran the query and returned {"rows": []},
    which is indistinguishable from a cycle that genuinely has no probes."""
    status, _ = _call(f"/api/cycle/{bad}", auth=("user", "pass"))
    assert status == 422, f"{bad!r} was not rejected"


def test_validation_does_not_run_ahead_of_authentication(seeded_db):
    """An unauthenticated caller must get 401 whatever they put in the path -- otherwise the
    new pattern would be a free oracle for probing the route without credentials."""
    assert _call("/api/cycle/javascript:alert(1)")[0] == 401
    assert _call("/api/cycle/cycle-000")[0] == 401
