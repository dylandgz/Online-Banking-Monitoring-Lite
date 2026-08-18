"""FastAPI routes: dashboard shell, JSON API, CSV export, basic auth on everything but
/healthz.

Backend only -- the dashboard's markup, styling, and client logic live in
monitor/web/templates/ and monitor/web/static/, served by the routes at the bottom of
this file. Nothing here builds HTML.
"""
import csv
import io
import secrets
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

import config
from monitor import db
from monitor.timeutil import to_eastern
from monitor.verdict import layer_wording, severity, unified_verdict

app = FastAPI()
security = HTTPBasic()

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_STATIC_DIR = Path(__file__).parent / "static"

# Only the two assets the dashboard actually loads. An explicit allowlist rather than a
# mounted StaticFiles directory, for two reasons: StaticFiles can't sit behind the basic-auth
# dependency (Rule: auth on everything but /healthz), and an allowlist can't be walked into
# serving something unintended out of the package directory.
_STATIC_FILES = {
    "dashboard.css": "text/css",
    "dashboard.js": "application/javascript",
    # Brand mark. Both extensions are allowed so the official asset can be dropped in as
    # either without touching this file -- see the note in logo.svg.
    "logo.svg": "image/svg+xml",
    "logo.png": "image/png",
}

# Brand web fonts (Montserrat + Nunito Sans, the faces teachersfcu.org itself serves).
# Discovered from the package's own directory at import so adding a weight is a drop-in,
# and self-hosted rather than pulled from a CDN: this dashboard is meant to run on a VM
# reachable only over Tailscale/SSH, where an external font request would simply hang.
# Still an allowlist -- only files shipped in this directory are serveable, and lookup is
# by exact filename, so there's no path traversal.
_STATIC_FILES.update({path.name: "font/woff2" for path in _STATIC_DIR.glob("*.woff2")})


def require_auth(credentials: HTTPBasicCredentials = Depends(security)) -> None:
    user_ok = secrets.compare_digest(credentials.username, config.DASHBOARD_USER or "")
    pass_ok = secrets.compare_digest(credentials.password, config.DASHBOARD_PASSWORD or "")
    if not (user_ok and pass_ok):
        raise HTTPException(status_code=401, detail="Unauthorized", headers={"WWW-Authenticate": "Basic"})


def get_conn():
    conn = db.get_connection(config.DB_PATH)
    try:
        yield conn
    finally:
        conn.close()


@app.get("/healthz")
def healthz():
    conn = db.get_connection(config.DB_PATH)
    try:
        last_ts = db.get_last_check_ts(conn)
    finally:
        conn.close()

    if last_ts is None:
        return JSONResponse({"status": "unknown", "reason": "no checks recorded yet"}, status_code=503)

    age_s = (datetime.now(timezone.utc) - datetime.fromisoformat(last_ts)).total_seconds()
    healthy = age_s <= config.CHECK_INTERVAL_S * 3
    body = {"status": "ok" if healthy else "unhealthy", "last_check_age_s": round(age_s)}
    return JSONResponse(body, status_code=200 if healthy else 503)


@app.get("/api/status", dependencies=[Depends(require_auth)])
def api_status(conn=Depends(get_conn)):
    """[v3.8 / Stage R] platform_status = worst_of(main, auth) -- the banner and every
    field here speak platform-level, not just the pulse/render track, per Rule 16/the
    "one platform, one verdict" realignment."""
    main_state = db.get_state(conn, track="main")
    auth_state = db.get_state(conn, track="auth")
    verdict = unified_verdict(main_state.status, auth_state.status)

    since_ts = None
    fail_layer = None
    fail_wording = None
    screenshot_path = None
    if verdict != "UP":
        # Whichever track is at least as severe explains the verdict (Rule 4: name the
        # failed layer). Ties (both DOWN, say) favor main -- it's the precursor.
        responsible_track = "main" if severity(main_state.status) >= severity(auth_state.status) else "auth"
        responsible_state = main_state if responsible_track == "main" else auth_state
        since_ts = responsible_state.since_ts
        open_incident = db.get_open_incident(conn, track=responsible_track)
        if open_incident:
            fail_layer = open_incident["trigger_layer"]
            screenshot_path = open_incident["screenshot_path"]
        if verdict == "DOWN":
            fail_wording = layer_wording(fail_layer)

    last_cycle = db.get_last_cycle(conn)
    if last_cycle is not None:
        last_cycle["ts"] = to_eastern(last_cycle["ts"])

    now = datetime.now(timezone.utc)
    uptime = {
        "24h": db.uptime_pct(conn, (now - timedelta(hours=24)).isoformat()),
        "7d": db.uptime_pct(conn, (now - timedelta(days=7)).isoformat()),
        "30d": db.uptime_pct(conn, (now - timedelta(days=30)).isoformat()),
    }

    # get_recent_incidents() is track-agnostic (no filter) -- already merges both tracks.
    incidents = db.get_recent_incidents(conn, limit=20)
    for inc in incidents:
        if inc.get("started_at"):
            inc["started_at"] = to_eastern(inc["started_at"])
        if inc.get("ended_at"):
            inc["ended_at"] = to_eastern(inc["ended_at"])

    return {
        "target_name": config.TARGET_NAME,
        "target_url": config.TARGET_URL,
        "status": verdict,
        "since_ts": to_eastern(since_ts) if since_ts else None,
        "fail_layer": fail_layer,
        "fail_wording": fail_wording,
        "last_cycle": last_cycle,
        "screenshot_path": screenshot_path,
        "uptime_pct": uptime,
        "incidents": incidents,
    }


@app.get("/api/history", dependencies=[Depends(require_auth)])
def api_history(
    from_: str | None = Query(None, alias="from"),
    to: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    conn=Depends(get_conn),
):
    """[v3.8 / Stage R] Serves the cycles view -- one row per minute, per CLAUDE.md's
    dashboard spec. Probe-level detail (including bursts) is fetched per-cycle via
    /api/cycle/{cycle_id}, not inlined here."""
    rows, total = db.query_cycles(conn, from_, to, page, page_size)
    for row in rows:
        row["ts"] = to_eastern(row["ts"])
    return {"rows": rows, "total": total, "page": page, "page_size": page_size}


@app.get("/api/cycle/{cycle_id}", dependencies=[Depends(require_auth)])
def api_cycle_probes(cycle_id: str, conn=Depends(get_conn)):
    """Expandable probe-level detail for one cycle row -- bursts are first-class,
    unhideable evidence (CLAUDE.md), surfaced here on drill-down."""
    rows = db.get_checks_for_cycle(conn, cycle_id)
    for row in rows:
        row["ts"] = to_eastern(row["ts"])
    return {"rows": rows}


# CSV export column order, per table. Kept as (header, row-key) pairs so the header and the
# values can't drift apart -- Rule 6 requires the export to be row-for-row faithful.
_EXPORT_COLUMNS = {
    "cycles": [
        ("cycle_id", "cycle_id"), ("ts_eastern", "ts"), ("pulse_ok", "pulse_ok"),
        ("render_ok", "render_ok"), ("authed_ok", "authed_ok"), ("session_reused", "session_reused"),
        ("pulse_latency_ms", "pulse_latency_ms"), ("render_latency_ms", "render_latency_ms"),
        ("authed_latency_ms", "authed_latency_ms"), ("verdict", "verdict"),
        ("fail_layer", "fail_layer"), ("fail_reason", "fail_reason"), ("burst_id", "burst_id"),
    ],
    "checks": [
        ("id", "id"), ("ts_eastern", "ts"), ("ok", "ok"), ("http_status", "http_status"),
        ("latency_ms", "latency_ms"), ("fail_reason", "fail_reason"), ("browser_mode", "browser_mode"),
        ("layer", "layer"), ("burst_id", "burst_id"), ("cycle_id", "cycle_id"),
    ],
}


def _csv_lines(table: str, rows: list[dict]):
    """Yields the CSV for one table, header first, one row at a time."""
    columns = _EXPORT_COLUMNS[table]
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([header for header, _ in columns])
    yield buf.getvalue()
    for row in rows:
        buf.seek(0)
        buf.truncate(0)
        # ts is the only converted column (Rule 6: storage UTC, presentation Eastern).
        writer.writerow([to_eastern(row[key]) if key == "ts" else row[key] for _, key in columns])
        yield buf.getvalue()


@app.get("/api/export", dependencies=[Depends(require_auth)])
def api_export(
    from_: str | None = Query(None, alias="from"),
    to: str | None = Query(None),
    table: str = Query("cycles", pattern="^(cycles|checks|all)$"),
):
    """[v3.8 / Stage R] table=cycles is the primary audit export (one row per minute);
    table=checks is the probe-level evidence, bursts included.

    table=all zips both together as a single download -- what the dashboard's "Download
    Logs" button uses. The two tables are deliberately NOT merged into one CSV: they have
    different schemas and different grains (one row per minute vs. one row per probe), and
    Rule 6 requires each export to stay row-for-row faithful to its table. A zip keeps both
    intact and keeps them reconcilable against each other, which a flattened join would
    destroy."""
    conn = db.get_connection(config.DB_PATH)
    try:
        cycles = db.export_cycles(conn, from_, to) if table in ("cycles", "all") else None
        checks = db.export_checks(conn, from_, to) if table in ("checks", "all") else None
    finally:
        conn.close()

    if table == "all":
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr("cycles.csv", "".join(_csv_lines("cycles", cycles)))
            bundle.writestr("checks.csv", "".join(_csv_lines("checks", checks)))
        return Response(
            archive.getvalue(),
            media_type="application/zip",
            headers={"Content-Disposition": "attachment; filename=monitor-logs.zip"},
        )

    return StreamingResponse(
        _csv_lines(table, cycles if table == "cycles" else checks),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={table}.csv"},
    )


@app.get("/api/artifact/{incident_id}", dependencies=[Depends(require_auth)])
def api_artifact(incident_id: int, conn=Depends(get_conn)):
    incident = db.get_incident(conn, incident_id)
    if not incident or not incident["screenshot_path"]:
        raise HTTPException(status_code=404, detail="No screenshot for this incident")
    return FileResponse(incident["screenshot_path"])


# --- frontend shell ---------------------------------------------------------------------
# The dashboard is three static files under monitor/web/. They're served through
# authenticated routes rather than a mounted StaticFiles app so the whole dashboard
# (markup, styles, and the client that knows the API shape) stays behind basic auth.

@app.get("/", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
def dashboard():
    return HTMLResponse((_TEMPLATES_DIR / "dashboard.html").read_text(encoding="utf-8"))


@app.get("/static/{filename}", dependencies=[Depends(require_auth)])
def static_asset(filename: str):
    media_type = _STATIC_FILES.get(filename)
    if media_type is None:
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(_STATIC_DIR / filename, media_type=media_type)
