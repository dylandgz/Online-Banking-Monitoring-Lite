"""FastAPI routes: dashboard, JSON API, CSV export, basic auth on everything but /healthz."""
import csv
import io
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

import config
from monitor import db
from monitor.timeutil import to_eastern

app = FastAPI()
security = HTTPBasic()

# [v3.8 / Stage R] platform_status = worst_of(main, auth) -- small local copy rather than
# importing monitor.main, so the web layer doesn't depend on the scheduler's internals.
_STATUS_ORDER = {"UP": 0, "DEGRADED": 1, "CONFIG_ERROR": 2, "DOWN": 3}


def _unified_verdict(main_status: str, auth_status: str) -> str:
    return auth_status if _STATUS_ORDER[auth_status] > _STATUS_ORDER[main_status] else main_status


def _down_wording(fail_layer: str | None) -> str | None:
    """Rule 4: name the failed layer with exact operator wording."""
    if fail_layer in ("pulse", "render"):
        return "login screen unreachable / not rendering"
    if fail_layer == "authed":
        return "online banking behind login not rendering"
    return None


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
    verdict = _unified_verdict(main_state.status, auth_state.status)

    since_ts = None
    fail_layer = None
    fail_wording = None
    screenshot_path = None
    if verdict != "UP":
        # Whichever track is at least as severe explains the verdict (Rule 4: name the
        # failed layer). Ties (both DOWN, say) favor main -- it's the precursor.
        responsible_track = "main" if _STATUS_ORDER[main_state.status] >= _STATUS_ORDER[auth_state.status] else "auth"
        responsible_state = main_state if responsible_track == "main" else auth_state
        since_ts = responsible_state.since_ts
        open_incident = db.get_open_incident(conn, track=responsible_track)
        if open_incident:
            fail_layer = open_incident["trigger_layer"]
            screenshot_path = open_incident["screenshot_path"]
        if verdict == "DOWN":
            fail_wording = _down_wording(fail_layer)

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


@app.get("/api/export", dependencies=[Depends(require_auth)])
def api_export(
    from_: str | None = Query(None, alias="from"),
    to: str | None = Query(None),
    table: str = Query("cycles", pattern="^(cycles|checks)$"),
):
    """[v3.8 / Stage R] table=cycles (default) is the primary audit export -- one row per
    minute; table=checks is the probe-level evidence (bursts included)."""
    conn = db.get_connection(config.DB_PATH)
    if table == "cycles":
        rows = db.export_cycles(conn, from_, to)
    else:
        rows = db.export_checks(conn, from_, to)
    conn.close()

    if table == "cycles":
        header = [
            "cycle_id", "ts_eastern", "pulse_ok", "render_ok", "authed_ok", "session_reused",
            "pulse_latency_ms", "render_latency_ms", "authed_latency_ms", "verdict", "fail_layer",
            "fail_reason", "burst_id",
        ]

        def row_values(row):
            return [
                row["cycle_id"], to_eastern(row["ts"]), row["pulse_ok"], row["render_ok"], row["authed_ok"],
                row["session_reused"], row["pulse_latency_ms"], row["render_latency_ms"], row["authed_latency_ms"],
                row["verdict"], row["fail_layer"], row["fail_reason"], row["burst_id"],
            ]
    else:
        header = ["id", "ts_eastern", "ok", "http_status", "latency_ms", "fail_reason", "browser_mode", "layer", "burst_id", "cycle_id"]

        def row_values(row):
            return [
                row["id"], to_eastern(row["ts"]), row["ok"], row["http_status"], row["latency_ms"],
                row["fail_reason"], row["browser_mode"], row["layer"], row["burst_id"], row["cycle_id"],
            ]

    def generate():
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(header)
        yield buf.getvalue()
        for row in rows:
            buf.seek(0)
            buf.truncate(0)
            writer.writerow(row_values(row))
            yield buf.getvalue()

    return StreamingResponse(
        generate(),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={table}.csv"},
    )


@app.get("/api/artifact/{incident_id}", dependencies=[Depends(require_auth)])
def api_artifact(incident_id: int, conn=Depends(get_conn)):
    incident = db.get_incident(conn, incident_id)
    if not incident or not incident["screenshot_path"]:
        raise HTTPException(status_code=404, detail="No screenshot for this incident")
    return FileResponse(incident["screenshot_path"])


@app.get("/", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
def dashboard():
    return HTMLResponse(DASHBOARD_HTML)


DASHBOARD_HTML = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Monitor Lite</title>
<style>
  body { font-family: -apple-system, system-ui, sans-serif; max-width: 900px; margin: 2rem auto; padding: 0 1rem; color: #222; }
  .banner { padding: 1rem; border-radius: 8px; font-size: 1.2rem; font-weight: 600; margin-bottom: 1rem; }
  .banner.up { background: #d7f5dd; color: #146c2e; }
  .banner.down { background: #fbdada; color: #a11212; }
  .uptime { display: flex; gap: 2rem; margin-bottom: 1.5rem; }
  .uptime div { background: #f2f2f2; border-radius: 8px; padding: 0.75rem 1rem; }
  table { border-collapse: collapse; width: 100%; margin-bottom: 1.5rem; font-size: 0.9rem; }
  th, td { border: 1px solid #ddd; padding: 0.4rem 0.6rem; text-align: left; }
  th { background: #fafafa; }
  .fail { color: #a11212; }
  .ok { color: #146c2e; }
  .controls { margin-bottom: 0.5rem; display: flex; gap: 0.5rem; align-items: center; }
  a.button, button { padding: 0.4rem 0.8rem; border-radius: 6px; border: 1px solid #ccc; background: #fff; cursor: pointer; text-decoration: none; color: #222; }
  tr.burst-row { background: #fff7e0; }
  .badge-burst { display: inline-block; background: #f5a623; color: #5b3b00; border-radius: 4px; padding: 0.05rem 0.4rem; font-size: 0.75rem; font-weight: 600; }
  .burst-id { color: #888; font-size: 0.75rem; margin-left: 0.3rem; }
  .layer-badge { display: inline-block; border-radius: 4px; padding: 0.05rem 0.4rem; font-size: 0.75rem; font-weight: 600; margin-right: 0.2rem; }
  .layer-badge.ok { background: #d7f5dd; color: #146c2e; }
  .layer-badge.fail { background: #fbdada; color: #a11212; }
  .layer-badge.na { background: #eee; color: #888; }
  .badge-reused { display: inline-block; background: #e0ecff; color: #1a4d99; border-radius: 4px; padding: 0.05rem 0.4rem; font-size: 0.75rem; }
  tr.cycle-row { cursor: pointer; }
  tr.cycle-row:hover { background: #f7f7f7; }
  tr.probe-detail td { background: #fbfbfb; padding: 0.5rem 0.6rem 0.9rem 1.5rem; }
</style>
</head>
<body>
  <h1 id="title">Monitor Lite</h1>
  <p id="target-url" style="color:#666; font-size:0.9rem;"></p>
  <div id="banner" class="banner">Loading…</div>

  <h2>Uptime</h2>
  <div class="uptime" id="uptime"></div>

  <h2>Incidents</h2>
  <table id="incidents-table">
    <thead><tr><th>Track</th><th>Layer</th><th>Started</th><th>Ended</th><th>Duration</th><th>Probes</th><th>Screenshot</th></tr></thead>
    <tbody></tbody>
  </table>

  <h2>Cycle Log <span style="font-weight:normal; color:#666; font-size:0.85rem;">(one row per minute -- click a row to see its probes)</span></h2>
  <div class="controls">
    <label>From <input type="datetime-local" id="from"></label>
    <label>To <input type="datetime-local" id="to"></label>
    <button onclick="state.page=1; loadHistory();">Filter</button>
    <a class="button" id="csv-link" href="/api/export?table=cycles">Download cycles CSV</a>
    <a class="button" id="csv-link-checks" href="/api/export?table=checks">Download probes CSV</a>
  </div>
  <table id="history-table">
    <thead><tr><th>Timestamp (Eastern)</th><th>Pulse</th><th>Render</th><th>Authed</th><th>Verdict</th><th>Session</th><th>Burst</th></tr></thead>
    <tbody></tbody>
  </table>
  <div class="controls">
    <button onclick="prevPage()">Prev</button>
    <span id="page-label"></span>
    <button onclick="nextPage()">Next</button>
  </div>

<script>
const state = { page: 1, pageSize: 50, total: 0 };

function fmtDuration(s) {
  if (s == null) return "-";
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m ${sec}s`;
  return `${sec}s`;
}

async function loadStatus() {
  const res = await fetch("/api/status");
  const data = await res.json();
  document.getElementById("title").textContent = "Monitor Lite — " + data.target_name;
  document.getElementById("target-url").textContent = "Watching: " + data.target_url;

  const banner = document.getElementById("banner");
  if (data.status === "UP") {
    banner.className = "banner up";
    banner.textContent = "UP";
  } else if (data.status === "DOWN") {
    banner.className = "banner down";
    banner.textContent = "DOWN since " + data.since_ts + (data.fail_wording ? " — " + data.fail_wording : "");
  } else {
    banner.className = "banner down";
    banner.textContent = data.status + " since " + data.since_ts;
  }

  const uptimeEl = document.getElementById("uptime");
  uptimeEl.innerHTML = ["24h", "7d", "30d"].map(k => {
    const v = data.uptime_pct[k];
    return `<div><strong>${k}</strong>: ${v == null ? "n/a" : v + "%"}</div>`;
  }).join("");

  const tbody = document.querySelector("#incidents-table tbody");
  tbody.innerHTML = data.incidents.map(inc => `
    <tr>
      <td>${inc.track ?? "main"}</td>
      <td>${inc.trigger_layer ?? "-"}</td>
      <td>${inc.started_at}</td>
      <td>${inc.ended_at ?? "(ongoing)"}</td>
      <td>${fmtDuration(inc.duration_s)}</td>
      <td>${inc.checks_failed ?? "-"}</td>
      <td>${inc.screenshot_path ? `<a href="/api/artifact/${inc.id}" target="_blank">view</a>` : "-"}</td>
    </tr>
  `).join("");
}

function layerBadge(ok) {
  if (ok === null || ok === undefined) return `<span class="layer-badge na">-</span>`;
  return ok ? `<span class="layer-badge ok">OK</span>` : `<span class="layer-badge fail">FAIL</span>`;
}

async function toggleProbes(cycleId, row) {
  const existing = document.getElementById("probes-" + cycleId);
  if (existing) { existing.remove(); return; }

  const res = await fetch("/api/cycle/" + cycleId);
  const data = await res.json();
  const detail = document.createElement("tr");
  detail.id = "probes-" + cycleId;
  detail.className = "probe-detail";
  detail.innerHTML = `<td colspan="7">` + data.rows.map(p => `
    <div>
      ${p.ts} — <strong>${p.layer}</strong> —
      <span class="${p.ok ? 'ok' : 'fail'}">${p.ok ? "OK" : "FAIL"}</span>
      ${p.fail_reason ? ` (${p.fail_reason})` : ""}
      ${p.latency_ms != null ? ` — ${Math.round(p.latency_ms)}ms` : ""}
      ${p.burst_id ? `<span class="badge-burst">burst</span><span class="burst-id">${p.burst_id.slice(0, 8)}</span>` : ""}
    </div>
  `).join("") + `</td>`;
  row.after(detail);
}

async function loadHistory() {
  const from = document.getElementById("from").value;
  const to = document.getElementById("to").value;
  const params = new URLSearchParams({ page: state.page, page_size: state.pageSize });
  if (from) params.set("from", new Date(from).toISOString());
  if (to) params.set("to", new Date(to).toISOString());

  const res = await fetch("/api/history?" + params.toString());
  const data = await res.json();
  state.total = data.total;

  const tbody = document.querySelector("#history-table tbody");
  tbody.innerHTML = "";
  data.rows.forEach(r => {
    const tr = document.createElement("tr");
    tr.className = "cycle-row" + (r.burst_id ? " burst-row" : "");
    tr.innerHTML = `
      <td>${r.ts}</td>
      <td>${layerBadge(r.pulse_ok)}</td>
      <td>${layerBadge(r.render_ok)}</td>
      <td>${layerBadge(r.authed_ok)}</td>
      <td class="${r.verdict === 'UP' ? 'ok' : 'fail'}">${r.verdict}</td>
      <td>${r.session_reused ? `<span class="badge-reused">reused</span>` : "-"}</td>
      <td>${r.burst_id ? `<span class="badge-burst">burst</span><span class="burst-id">${r.burst_id.slice(0, 8)}</span>` : "-"}</td>
    `;
    tr.onclick = () => toggleProbes(r.cycle_id, tr);
    tbody.appendChild(tr);
  });

  const totalPages = Math.max(1, Math.ceil(state.total / state.pageSize));
  document.getElementById("page-label").textContent = `Page ${state.page} / ${totalPages}`;

  const csvParams = new URLSearchParams();
  if (from) csvParams.set("from", new Date(from).toISOString());
  if (to) csvParams.set("to", new Date(to).toISOString());
  document.getElementById("csv-link").href = "/api/export?table=cycles&" + csvParams.toString();
  document.getElementById("csv-link-checks").href = "/api/export?table=checks&" + csvParams.toString();
}

function prevPage() { if (state.page > 1) { state.page--; loadHistory(); } }
function nextPage() {
  const totalPages = Math.max(1, Math.ceil(state.total / state.pageSize));
  if (state.page < totalPages) { state.page++; loadHistory(); }
}

loadStatus();
loadHistory();
setInterval(loadStatus, 30000);
setInterval(loadHistory, 30000);
</script>
</body>
</html>
"""
