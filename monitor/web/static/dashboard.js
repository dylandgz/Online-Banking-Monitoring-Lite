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

// --- DOM building [AppScan XSS findings, 2026-09-22; rewritten 2026-09-23] -----------
//
// THE RULE, and it has no exceptions: **this file never builds HTML from a string.** No
// `innerHTML`, no `outerHTML`, no `insertAdjacentHTML`, no `document.write`. Every element
// is created with `el()` below, and every value from the API is inserted as *text*, which
// the browser stores as character data and never parses as markup. There is nothing to
// escape because there is no markup parser on the path.
//
// Why it is written this way rather than escaped. AppScan rated the four `innerHTML`
// assignments this file used to have as Critical reflected XSS. "Reflected" was the wrong
// word -- no request input reaches them -- and the previous version escaped every
// interpolation with a hand-written esc(), so it was not exploitable either. Two problems
// with that answer. A static scanner cannot verify that a human escaped every field, so it
// flags the sink regardless and the finding never clears. And the safety depended on a
// convention: one forgotten esc() at one call site, and it was gone.
//
// The trap was one token deep. /api/cycle/{id} ships the full `checks` row, so `p.page_url`
// and `p.evidence_text` are in the probe-detail payload right now, unrendered.
// evidence_text is the live text of the monitored bank's role="alert" banner (journey.py
// `_visible_alert_text`, added by B63 so a diagnosis does not require opening a screenshot)
// -- i.e. verbatim remote content. Rendering it with `${p.evidence_text}` would have looked
// like a one-line diagnostic improvement and been genuine stored XSS against an
// authenticated operator. Under textContent that same edit is simply safe.
//
// THE ONE THING THIS DOES NOT COVER: urls. `textContent` protects text and `el()` protects
// attribute *values*, but a `javascript:` url in an href or src executes no matter how it
// got there. So urls are built from a code-literal prefix plus a server-generated id, and
// nothing remote-influenced (page_url, evidence_text, screenshot_path) may ever reach one.
// tests/test_dashboard_escaping.py enforces both halves of this -- read it before adding a
// sink, and do not reintroduce an HTML string to save a few lines.

// Appends children to a node: a string (or number) becomes a text node, a node or fragment
// is appended as-is, an array is flattened. null/undefined/false/"" are skipped, which is
// what lets the callers below keep their `cond ? x : null` shape inline. Note 0 is NOT
// skipped -- `checks_failed` of 0 is a real value that must render as "0".
function append(parent, children) {
  for (const child of children) {
    if (child === null || child === undefined || child === false || child === "") continue;
    if (Array.isArray(child)) { append(parent, child); continue; }
    parent.appendChild(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return parent;
}

// The complete set of properties el() can set, one literal assignment each.
//
// [AppScan prototype-pollution finding, dashboard.js:72 -- 2026-09-23] This used to be a
// single `node[key] = value` inside el(). Writing to a property whose NAME comes from a
// variable is the shape of a prototype-pollution bug: if the name can ever be "__proto__" or
// "constructor", the write lands on the object every other object inherits from instead of on
// this one. It could not happen here -- every key in this file is a literal in an object
// written a few lines below, there are five of them, and none comes from the API, the url or
// any other input -- and the target is a DOM element, so even "__proto__" would have swapped
// one node's prototype rather than polluting Object.prototype. But a scanner cannot see where
// a variable key came from, and neither can the next person, so there is no longer a line
// capable of writing a name that is not spelled out here.
//
// A Map, not an object literal: `SETTERS["toString"]` on a plain object hands back an
// inherited function, which is the same class of mistake this is closing. Map.get returns
// undefined for anything not explicitly put in.
//
// An unknown key is skipped rather than thrown or logged: it can only ever be a typo in this
// file (the keys are literals), a throw would blank the dashboard over one, and a console
// call is its own scanner finding. tests/test_dashboard_escaping.py fails instead, at build
// time, listing the key -- which is where a code-only mistake belongs.
//
// href stays subject to the url rule in the header comment: a literal path, never a bare
// server value.
const SETTERS = new Map([
  ["className", (node, value) => { node.className = value; }],
  ["title",     (node, value) => { node.title = value; }],
  ["id",        (node, value) => { node.id = value; }],
  ["href",      (node, value) => { node.href = value; }],
  ["target",    (node, value) => { node.target = value; }],
  ["colSpan",   (node, value) => { node.colSpan = value; }],
]);

// el("td", { className: "ts", title: ts }, "text", childNode, cond ? "x" : null)
function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined) continue;
    const set = SETTERS.get(key);
    if (set) set(node, value);
  }
  return append(node, children);
}

// For the places that need several siblings with no wrapper element around them -- adding a
// wrapper would change what dashboard.css's descendant selectors match.
function frag(...children) {
  return append(document.createDocumentFragment(), children);
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

  // The div/strong/span shape is load-bearing: dashboard.css selects `.uptime div` and
  // `.uptime strong` by tag name, not by class.
  document.getElementById("uptime").replaceChildren(
    ...["24h", "7d", "30d"].map(k => {
      const v = data.uptime_pct[k];
      return el("div", null,
        el("strong", null, k),
        el("span", { className: "uptime-value" }, v == null ? "n/a" : v + "%"),
      );
    })
  );

  const tbody = document.querySelector("#incidents-table tbody");
  tbody.replaceChildren(
    ...data.incidents.map(inc => el("tr", null,
      el("td", null, inc.track ?? "main"),
      el("td", null, inc.trigger_layer ?? "-"),
      el("td", { className: "ts", title: inc.started_at ?? "" }, compactTs(inc.started_at)),
      el("td", { className: "ts", title: inc.ended_at ?? "" },
        inc.ended_at ? compactTs(inc.ended_at) : "(ongoing)"),
      el("td", null, fmtDuration(inc.duration_s)),
      el("td", null, inc.checks_failed ?? "-"),
      // Literal path + a server-generated integer id, encoded as one path segment. This is
      // the only data-derived url in the file; see the header comment's url rule.
      el("td", null, inc.screenshot_path
        ? el("a", { href: "/api/artifact/" + encodeURIComponent(inc.id), target: "_blank" }, "view")
        : "-"),
    ))
  );
}

// Same three-way split as the banner: DOWN is red, UP is teal, and anything that doesn't
// page (CONFIG_ERROR, DEGRADED) is gold rather than being lumped in with a real outage.
// Returns a class name, not a node -- it is the one helper here that stays a string.
function verdictClass(verdict) {
  if (verdict === "UP") return "ok";
  if (verdict === "DOWN") return "fail";
  return "warn";
}

function layerBadge(ok) {
  if (ok === null || ok === undefined) return el("span", { className: "layer-badge na" }, "-");
  return el("span", { className: ok ? "layer-badge ok" : "layer-badge fail" }, ok ? "OK" : "FAIL");
}

// Both places that show a burst_id show it the same way: the gold chip, then the first 8
// characters of the id. String() first -- burst_id is not reliably a string (it arrives
// straight out of SQLite), and .slice() on a number throws.
function burstBadge(burstId) {
  return frag(
    el("span", { className: "badge-burst" }, "burst"),
    el("span", { className: "burst-id" }, String(burstId).slice(0, 8)),
  );
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
  if (r.session_reused) return el("span", { className: "badge-reused" }, "reused");
  if (r.authed_ok === null || r.authed_ok === undefined) {
    return el("span", { className: "badge-muted", title: "Auth track did not run this cycle" },
      "not checked");
  }
  if (!r.authed_ok) {
    return el("span", {
      className: "badge-muted",
      title: "Authed check failed -- whether a login was spent is not recorded on the cycle row",
    }, "n/a");
  }
  return el("span", { className: "badge-login" }, "new login");
}

// [B25] 461 early rows were written before `layer` existed and carry NULL/empty. Rendering
// that as a blank leaves a line whose middle is missing, which reads as a rendering bug
// rather than as missing data -- say "unknown" instead. (The `auth`/`authed` spelling split
// is normalised server-side, in _split_main_probe's caller.)
//
// Returns the layer name as plain text, or the muted chip. Its caller wraps the result in
// <strong> either way, which is what the previous template did.
function probeLayer(p) {
  return p.layer ? String(p.layer) : el("span", { className: "badge-muted" }, "unknown");
}

// Three states, not two. The pulse line reads its ok from cycles.pulse_ok, which is NULL on
// any cycle predating that column -- and `null ? "OK" : "FAIL"` would print FAIL for a probe
// that was never recorded either way. Matches layerBadge()'s handling in the row above.
// Values arrive as SQLite 1/0/null, not JS booleans.
function probeStatus(p) {
  if (p.ok === null || p.ok === undefined) return el("span", { className: "badge-muted" }, "n/a");
  return el("span", { className: p.ok ? "ok" : "fail" }, p.ok ? "OK" : "FAIL");
}

// [B25] A latency of exactly 0 is never a measurement. It is the old hardcoded
// render_only_probe placeholder (102 rows, all burst re-probes) or main.py's synthetic
// session_expired result, which records that the monitor wanted to look and could not.
// Both mean "not timed", and "0ms" reads as "instant" -- the opposite. Show an em dash.
// render_only_probe now measures itself, so new burst rows carry real values.
//
// A fragment, not an element: this contributes the " — " separator *and* the value as
// siblings of the probe line, exactly as the old template's text did. Wrapping them in a
// span would add a box dashboard.css has no rule for.
function probeLatency(p) {
  if (p.latency_ms == null || p.latency_ms === 0) {
    return frag(" — ", el("span", { className: "badge-muted" }, "—"));
  }
  return frag(" — " + Math.round(p.latency_ms) + "ms");
}

// [AppScan "SSRF" finding, dashboard.js:153 -- 2026-09-23] The id this guards is a uuid4 the
// monitor generated (main.py), delivered by /api/history and handed straight back to
// /api/cycle on the same origin, so the scanner's reading -- a request whose destination an
// attacker could steer -- does not describe anything here. It is still worth validating: this
// is the file's only url built from a value rather than written out in full, and a check at
// the point of use costs one line and settles the question for good.
//
// A character allowlist, not a uuid shape, and the same one monitor/web/app.py enforces on
// the route. Everything that could change what the url MEANS is excluded (/ ? # % : @ \ and
// whitespace); an id in an unexpected format still works. The server's copy is the one that
// counts -- a browser check is advisory, since the browser is what an attacker controls.
const CYCLE_ID = /^[A-Za-z0-9_.-]{1,64}$/;

async function toggleProbes(cycleId, row) {
  const existing = document.getElementById("probes-" + cycleId);
  if (existing) { existing.remove(); return; }

  const id = String(cycleId);
  if (!CYCLE_ID.test(id)) {
    // Loud rather than silent: a bad shape here means the id format changed upstream, and a
    // drill-down that just stops opening is the hardest version of that to diagnose.
    console.warn("dashboard: refusing to request a cycle id of an unexpected shape");

    return;
  }
  const res = await fetch("/api/cycle/" + encodeURIComponent(id));
  const data = await res.json();
  // colSpan 7 matches both tables' seven columns (dashboard.html), and dashboard.css styles
  // this row via `tr.probe-detail td`, so the tr > td shape has to stay.
  const detail = el("tr", { id: "probes-" + cycleId, className: "probe-detail" },
    el("td", { colSpan: 7 },
      ...data.rows.map(p => el("div", null,
        el("span", { title: p.ts ?? "" }, compactTs(p.ts)),
        " — ",
        el("strong", null, probeLayer(p)),
        " — ",
        probeStatus(p),
        p.fail_reason ? " (" + p.fail_reason + ")" : null,
        probeLatency(p),
        p.burst_id ? " " : null,
        p.burst_id ? burstBadge(p.burst_id) : null,
      ))
    )
  );
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
  tbody.replaceChildren();
  data.rows.forEach(r => {
    const tr = el("tr", { className: "cycle-row" + (r.burst_id ? " burst-row" : "") },
      el("td", { className: "ts", title: r.ts ?? "" }, compactTs(r.ts)),
      el("td", null, layerBadge(r.pulse_ok)),
      el("td", null, layerBadge(r.render_ok)),
      el("td", null, layerBadge(r.authed_ok)),
      el("td", { className: verdictClass(r.verdict) }, r.verdict),
      el("td", null, sessionBadge(r)),
      el("td", null, r.burst_id ? burstBadge(r.burst_id) : "-"),
    );
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
