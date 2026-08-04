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

app = FastAPI()
security = HTTPBasic()


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
    state = db.get_state(conn)
    last_check = db.get_last_check(conn)

    screenshot_path = None
    if state.status == "DOWN":
        open_incident = db.get_open_incident(conn)
        screenshot_path = open_incident["screenshot_path"] if open_incident else None

    now = datetime.now(timezone.utc)
    uptime = {
        "24h": db.uptime_pct(conn, (now - timedelta(hours=24)).isoformat()),
        "7d": db.uptime_pct(conn, (now - timedelta(days=7)).isoformat()),
        "30d": db.uptime_pct(conn, (now - timedelta(days=30)).isoformat()),
    }

    return {
        "target_name": config.TARGET_NAME,
        "status": state.status,
        "since_ts": state.since_ts,
        "last_check": last_check,
        "screenshot_path": screenshot_path,
        "uptime_pct": uptime,
        "incidents": db.get_recent_incidents(conn, limit=20),
    }


@app.get("/api/history", dependencies=[Depends(require_auth)])
def api_history(
    from_: str | None = Query(None, alias="from"),
    to: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    conn=Depends(get_conn),
):
    rows, total = db.query_checks(conn, from_, to, page, page_size)
    return {"rows": rows, "total": total, "page": page, "page_size": page_size}


@app.get("/api/export", dependencies=[Depends(require_auth)])
def api_export(
    from_: str | None = Query(None, alias="from"),
    to: str | None = Query(None),
):
    conn = db.get_connection(config.DB_PATH)
    rows = db.export_checks(conn, from_, to)
    conn.close()

    def generate():
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["id", "ts", "ok", "http_status", "latency_ms", "fail_reason", "browser_mode"])
        yield buf.getvalue()
        for row in rows:
            buf.seek(0)
            buf.truncate(0)
            writer.writerow([
                row["id"], row["ts"], row["ok"], row["http_status"], row["latency_ms"],
                row["fail_reason"], row["browser_mode"],
            ])
            yield buf.getvalue()

    return StreamingResponse(
        generate(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=checks.csv"},
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
</style>
</head>
<body>
  <h1 id="title">Monitor Lite</h1>
  <div id="banner" class="banner">Loading…</div>

  <h2>Uptime</h2>
  <div class="uptime" id="uptime"></div>

  <h2>Incidents</h2>
  <table id="incidents-table">
    <thead><tr><th>Started</th><th>Ended</th><th>Duration</th><th>Checks Failed</th><th>Screenshot</th></tr></thead>
    <tbody></tbody>
  </table>

  <h2>Check Log</h2>
  <div class="controls">
    <label>From <input type="datetime-local" id="from"></label>
    <label>To <input type="datetime-local" id="to"></label>
    <button onclick="state.page=1; loadHistory();">Filter</button>
    <a class="button" id="csv-link" href="/api/export">Download CSV</a>
  </div>
  <table id="history-table">
    <thead><tr><th>Timestamp (UTC)</th><th>OK</th><th>HTTP</th><th>Latency (ms)</th><th>Fail Reason</th></tr></thead>
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

  const banner = document.getElementById("banner");
  if (data.status === "UP") {
    banner.className = "banner up";
    banner.textContent = "UP";
  } else {
    banner.className = "banner down";
    let text = "DOWN since " + data.since_ts;
    banner.innerHTML = text;
  }

  const uptimeEl = document.getElementById("uptime");
  uptimeEl.innerHTML = ["24h", "7d", "30d"].map(k => {
    const v = data.uptime_pct[k];
    return `<div><strong>${k}</strong>: ${v == null ? "n/a" : v + "%"}</div>`;
  }).join("");

  const tbody = document.querySelector("#incidents-table tbody");
  tbody.innerHTML = data.incidents.map(inc => `
    <tr>
      <td>${inc.started_at}</td>
      <td>${inc.ended_at ?? "(ongoing)"}</td>
      <td>${fmtDuration(inc.duration_s)}</td>
      <td>${inc.checks_failed ?? "-"}</td>
      <td>${inc.screenshot_path ? `<a href="/api/artifact/${inc.id}" target="_blank">view</a>` : "-"}</td>
    </tr>
  `).join("");
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
  tbody.innerHTML = data.rows.map(r => `
    <tr>
      <td>${r.ts}</td>
      <td class="${r.ok ? 'ok' : 'fail'}">${r.ok ? "OK" : "FAIL"}</td>
      <td>${r.http_status ?? "-"}</td>
      <td>${r.latency_ms ? r.latency_ms.toFixed(0) : "-"}</td>
      <td>${r.fail_reason ?? "-"}</td>
    </tr>
  `).join("");

  const totalPages = Math.max(1, Math.ceil(state.total / state.pageSize));
  document.getElementById("page-label").textContent = `Page ${state.page} / ${totalPages}`;

  const csvParams = new URLSearchParams();
  if (from) csvParams.set("from", new Date(from).toISOString());
  if (to) csvParams.set("to", new Date(to).toISOString());
  document.getElementById("csv-link").href = "/api/export?" + csvParams.toString();
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
