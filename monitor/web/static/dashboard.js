// Dashboard client. Reads /api/status and /api/history (the [v3.8] cycles view: one row
// per minute, expandable to its probe-level evidence via /api/cycle/{id}). All timestamps
// arrive already converted to Eastern by the server (Rule 6) -- this file never does
// timezone math, it only renders what it's given.
const state = { page: 1, pageSize: 50, total: 0 };

// Executive-facing label for each state-machine verdict. The raw verdict is still shown
// beside it (the .banner-code chip) because that exact word is what the incident record,
// the alert emails and the CSV exports use -- an operator comparing the dashboard to a
// page at 3am must not have to translate between two vocabularies.
const STATUS_PRESENTATION = {
  UP:           { label: "Operational",     cls: "up" },
  DOWN:         { label: "Service Outage",  cls: "down" },
  CONFIG_ERROR: { label: "Needs Attention", cls: "warn" },
  DEGRADED:     { label: "Degraded",        cls: "warn" },
};

// --- HTML escaping [AppScan XSS findings, 2026-09-22] -------------------------------
//
// THE RULE, and it has exactly two cases. Inside any template literal that reaches
// `innerHTML`:
//
//   * a value from the API  -> MUST be wrapped in esc(...)
//   * a call to a helper that RETURNS MARKUP (layerBadge, sessionBadge, probeStatus,
//     probeLatency, probeLayer, verdictClass) -> MUST NOT be wrapped; escaping it would
//     render the tags as visible text. Those helpers own the escaping of anything they
//     interpolate, which is why probeLayer escapes p.layer itself.
//
// Why this exists. AppScan flagged the four innerHTML assignments as Critical reflected
// XSS. Reflected is the wrong word -- no request input reaches these sinks -- and nothing
// rendered today is attacker-controlled, so it was not exploitable. But that safety was a
// coincidence: it held only because every field that happened to be rendered happened to
// contain no markup, maintained by hand with nothing enforcing it.
//
// The trap was one token deep. /api/cycle/{id} already ships the full `checks` row, so
// `p.page_url` and `p.evidence_text` sit inside the probe-detail template right now,
// unrendered. evidence_text is the live text of the monitored bank's role="alert" banner
// (journey.py `_visible_alert_text`, added by B63 so a diagnosis does not require opening a
// screenshot) -- i.e. verbatim remote content, already in the payload. Writing
// `${p.evidence_text}` to show it would have been a reasonable-looking one-line diagnostic
// improvement, and genuine stored XSS against an authenticated operator, with nothing in
// the file to warn whoever made it. escaping-by-default removes the trap rather than
// relying on nobody stepping in it.
//
// Text and quoted-attribute contexts are both covered: every attribute in this file is
// double-quoted, and &quot;/&#39; close both. There is no URL-context interpolation (the
// one href built from data uses inc.id, a server-generated integer) and no interpolation
// inside a <script> or style attribute, so this single escaper is sufficient here. If a
// value ever has to go into an href/src, this is NOT enough -- javascript: survives it.
// tests/test_dashboard_escaping.py enforces the rule above; read it before adding a sink.
function esc(v) {
  if (v === null || v === undefined) return "";
  return String(v)
    .replace(/&/g, "&amp;")   // must be first, or it double-escapes the entities below
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

// Timestamps arrive as "2026-08-18 15:45:14 EDT (UTC-04:00)". Tables show the form without
// the offset -- EDT/EST already conveys the zone, and the suffix was wrapping cells onto a
// second line -- with the full string kept on the title attribute, and in the CSV export
// untouched, so nothing loses precision.
function compactTs(ts) {
  return ts ? ts.replace(/\s*\(UTC[^)]*\)\s*$/, "") : "";
}

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
  // The masthead wordmark is static brand markup, so target_name goes to the browser tab
  // instead -- that's where it's actually useful when several dashboards are open.
  document.title = data.target_name + " — Online Banking Monitor";
  document.getElementById("target-url").textContent = "Watching: " + data.target_url;

  // Only DOWN wears alarm red. CONFIG_ERROR and DEGRADED get the gold "warn" treatment:
  // per Rule 13 neither of them pages, so showing them in the same red as a real outage
  // overstated what the operator was looking at.
  const presentation = STATUS_PRESENTATION[data.status] || { label: data.status, cls: "warn" };
  document.getElementById("banner").className = "banner " + presentation.cls;
  document.getElementById("banner-value").textContent = presentation.label;
  document.getElementById("banner-code").textContent = data.status;

  let detail;
  if (data.status === "UP") {
    detail = "All monitored layers responding.";
  } else if (data.status === "CONFIG_ERROR") {
    // Rule 10: a config error halts the sign-in track only; pulse/render keeps checking.
    detail = "Sign-in checks paused — since " + compactTs(data.since_ts);
  } else {
    // Rule 4: the failed layer must be named here, in the operator's exact wording.
    detail = (data.fail_wording ? data.fail_wording + " — " : "") + "since " + compactTs(data.since_ts);
  }
  const detailEl = document.getElementById("banner-detail");
  detailEl.textContent = detail;
  detailEl.title = data.since_ts || "";

  const uptimeEl = document.getElementById("uptime");
  uptimeEl.innerHTML = ["24h", "7d", "30d"].map(k => {
    const v = data.uptime_pct[k];
    return `<div><strong>${esc(k)}</strong><span class="uptime-value">${esc(v == null ? "n/a" : v + "%")}</span></div>`;
  }).join("");

  const tbody = document.querySelector("#incidents-table tbody");
  tbody.innerHTML = data.incidents.map(inc => `
    <tr>
      <td>${esc(inc.track ?? "main")}</td>
      <td>${esc(inc.trigger_layer ?? "-")}</td>
      <td class="ts" title="${esc(inc.started_at ?? "")}">${esc(compactTs(inc.started_at))}</td>
      <td class="ts" title="${esc(inc.ended_at ?? "")}">${esc(inc.ended_at ? compactTs(inc.ended_at) : "(ongoing)")}</td>
      <td>${esc(fmtDuration(inc.duration_s))}</td>
      <td>${esc(inc.checks_failed ?? "-")}</td>
      <td>${inc.screenshot_path ? `<a href="/api/artifact/${esc(inc.id)}" target="_blank">view</a>` : "-"}</td>
    </tr>
  `).join("");
}

// Same three-way split as the banner: DOWN is red, UP is teal, and anything that doesn't
// page (CONFIG_ERROR, DEGRADED) is gold rather than being lumped in with a real outage.
function verdictClass(verdict) {
  if (verdict === "UP") return "ok";
  if (verdict === "DOWN") return "fail";
  return "warn";
}

function layerBadge(ok) {
  if (ok === null || ok === undefined) return `<span class="layer-badge na">-</span>`;
  return ok ? `<span class="layer-badge ok">OK</span>` : `<span class="layer-badge fail">FAIL</span>`;
}

// The Session column has to say four different things, because cycles.session_reused is
// written as bool(authed_ok and not login_used_this_cycle) -- so a bare `false` collapses
// "a budgeted login did the work", "the authed check failed", and "the track never looked"
// into one empty cell. authed_ok already carries that distinction: per Rule 16 it is a real
// True/False whenever the authed check actually contacted the platform, and NULL only when
// nothing looked at all (track unconfigured, track in CONFIG_ERROR, or no usable session and
// the recovery login was paused or refused by the budget).
//
// Note the comparisons: /api/history serves these straight out of SQLite, so they arrive as
// 1/0/null integers, NOT JS booleans. `authed_ok === false` would never match.
//
// The failed-check case deliberately reports "n/a" rather than "new login": whether a login
// was actually spent on a failing cycle is not recorded anywhere on the cycles row, and
// guessing would put a claim in the audit view that the data does not support.
function sessionBadge(r) {
  if (r.session_reused) return `<span class="badge-reused">reused</span>`;
  if (r.authed_ok === null || r.authed_ok === undefined) {
    return `<span class="badge-muted" title="Auth track did not run this cycle">not checked</span>`;
  }
  if (!r.authed_ok) {
    return `<span class="badge-muted" title="Authed check failed -- whether a login was spent is not recorded on the cycle row">n/a</span>`;
  }
  return `<span class="badge-login">new login</span>`;
}

// [B25] 461 early rows were written before `layer` existed and carry NULL/empty. Rendering
// that as a blank leaves a line whose middle is missing, which reads as a rendering bug
// rather than as missing data -- say "unknown" instead. (The `auth`/`authed` spelling split
// is normalised server-side, in _split_main_probe's caller.)
//
// Returns markup, so its callers must NOT esc() it -- which makes escaping p.layer here
// this function's own responsibility. It is the only markup-returning helper that passes a
// server value through; the others interpolate code-defined literals only.
function probeLayer(p) {
  return p.layer ? esc(p.layer) : `<span class="badge-muted">unknown</span>`;
}

// Three states, not two. The pulse line reads its ok from cycles.pulse_ok, which is NULL on
// any cycle predating that column -- and `null ? "OK" : "FAIL"` would print FAIL for a probe
// that was never recorded either way. Matches layerBadge()'s handling in the row above.
// Values arrive as SQLite 1/0/null, not JS booleans.
//
// The esc() calls below are no-ops -- both interpolations pick between two literals defined
// right here. They are written anyway so the rule at the top of this file holds without
// exception: inside a template, a value is esc()'d or it is a markup helper, full stop. An
// invariant with a list of "safe because I checked" exemptions is one nobody can apply
// without re-deriving the analysis, which is how the original four sinks got written.
function probeStatus(p) {
  if (p.ok === null || p.ok === undefined) return `<span class="badge-muted">n/a</span>`;
  return `<span class="${esc(p.ok ? "ok" : "fail")}">${esc(p.ok ? "OK" : "FAIL")}</span>`;
}

// [B25] A latency of exactly 0 is never a measurement. It is the old hardcoded
// render_only_probe placeholder (102 rows, all burst re-probes) or main.py's synthetic
// session_expired result, which records that the monitor wanted to look and could not.
// Both mean "not timed", and "0ms" reads as "instant" -- the opposite. Show an em dash.
// render_only_probe now measures itself, so new burst rows carry real values.
function probeLatency(p) {
  if (p.latency_ms == null || p.latency_ms === 0) return ` — <span class="badge-muted">—</span>`;
  return ` — ${esc(Math.round(p.latency_ms))}ms`;
}

async function toggleProbes(cycleId, row) {
  const existing = document.getElementById("probes-" + cycleId);
  if (existing) { existing.remove(); return; }

  const res = await fetch("/api/cycle/" + cycleId);
  const data = await res.json();
  const detail = document.createElement("tr");
  detail.id = "probes-" + cycleId;
  detail.className = "probe-detail";
  // probeLayer/probeStatus/probeLatency return markup and are deliberately un-esc()'d --
  // see the rule at the top of this file. Everything else here is a server value.
  detail.innerHTML = `<td colspan="7">` + data.rows.map(p => `
    <div>
      <span title="${esc(p.ts)}">${esc(compactTs(p.ts))}</span> — <strong>${probeLayer(p)}</strong> —
      ${probeStatus(p)}
      ${p.fail_reason ? ` (${esc(p.fail_reason)})` : ""}
      ${probeLatency(p)}
      ${p.burst_id ? `<span class="badge-burst">burst</span><span class="burst-id">${esc(String(p.burst_id).slice(0, 8))}</span>` : ""}
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
    // layerBadge/verdictClass/sessionBadge return markup or a code-defined class name and
    // are deliberately un-esc()'d -- see the rule at the top of this file.
    tr.innerHTML = `
      <td class="ts" title="${esc(r.ts)}">${esc(compactTs(r.ts))}</td>
      <td>${layerBadge(r.pulse_ok)}</td>
      <td>${layerBadge(r.render_ok)}</td>
      <td>${layerBadge(r.authed_ok)}</td>
      <td class="${verdictClass(r.verdict)}">${esc(r.verdict)}</td>
      <td>${sessionBadge(r)}</td>
      <td>${r.burst_id ? `<span class="badge-burst">burst</span><span class="burst-id">${esc(String(r.burst_id).slice(0, 8))}</span>` : "-"}</td>
    `;
    tr.onclick = () => toggleProbes(r.cycle_id, tr);
    tbody.appendChild(tr);
  });

  const totalPages = Math.max(1, Math.ceil(state.total / state.pageSize));
  document.getElementById("page-label").textContent = `Page ${state.page} / ${totalPages}`;

  // One download: a zip of cycles.csv + checks.csv, honoring whatever range is filtered.
  const csvParams = new URLSearchParams();
  if (from) csvParams.set("from", new Date(from).toISOString());
  if (to) csvParams.set("to", new Date(to).toISOString());
  document.getElementById("csv-link").href = "/api/export?table=all&" + csvParams.toString();
}

function prevPage() { if (state.page > 1) { state.page--; loadHistory(); } }
function nextPage() {
  const totalPages = Math.max(1, Math.ceil(state.total / state.pageSize));
  if (state.page < totalPages) { state.page++; loadHistory(); }
}

document.getElementById("filter-btn").onclick = () => { state.page = 1; loadHistory(); };
document.getElementById("prev-btn").onclick = prevPage;
document.getElementById("next-btn").onclick = nextPage;

loadStatus();
loadHistory();
setInterval(loadStatus, 30000);
setInterval(loadHistory, 30000);
