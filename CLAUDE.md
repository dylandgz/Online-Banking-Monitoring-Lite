# CLAUDE.md — Monitor Lite

> **Amendment v3.9 — 2026-08-24 — SELF-HEALTH.** The monitor is allowed to fail, but never silently. Scoped: adds Rule 18 and the `monitor`/`internal_error` vocabulary; changes no probe, scoring, alerting or schema behavior.
>
> **Amendment v3.8 — 2026-08-11 — THE REALIGNMENT.** One platform, one verdict.
> ⚠️ **Merge note for Claude Code:** this file was produced from a copy of CLAUDE.md that predates amendments v3.1–v3.7; their substance has been reconstructed from PROGRESS.md and integrated below. Before adopting this file, diff it against the repo's current CLAUDE.md and preserve any v3.2–v3.5 exception wording (patchright scope, headed-xvfb, retired USER_DATA_DIR note, `.nth()` scope) that is more precise than what's here. Flag discrepancies to the human; do not silently drop text.

**Amendment history (one line each):** v3 stages/stress-experiment · v3.1 burst floor (`MIN_FAILED_PROBES=3`, `DOWN_CONFIDENCE=4`, 4 probes, 90s window) + Eastern-time presentation · v3.2 patchright approved as scoped Rule-12 exception (Cloudflare, logged clearances) · v3.3 sign-in journey runs headed under xvfb (scoped Rule-14 exception) · v3.4 (retired by v3.7) · v3.5 no `"unknown"` fail_reason; `.nth()` scoped to username/password only · v3.6 auth runs on its own track/scheduler · v3.7 session reuse pulled forward; cheap `run_authed_check()` every 60s; `USER_DATA_DIR` retired · v3.8 one platform / one verdict / `cycles` table / Rules 16–17 · **v3.9 this amendment: Rule 18 self-health net, `internal_error` + `fail_layer='monitor'`, unexpected browser errors mapped onto the taxonomy.**

## What this is
A monitor that answers ONE question every minute: **is the online banking platform up for a customer?** "Up" is defined as: the authenticated area behind login returns HTTP 200 **and renders the expected content**. The public pulse + login-page render checks are **precursor layers** — the cheap outer evidence of the same single question, not a separate product. Results in SQLite (audit-grade). Email alerts on state transitions only. FastAPI dashboard. Replaces a Selenium tool that false-positived on DOM changes — **low false positives outrank everything, and no alert ever fires on fewer than 3 failed probes (executive commitment).**

## What changed in v3.8 (orientation)
1. **Unified verdict.** The "site check" and "sign-in check" are no longer two products with two verdicts. There is ONE platform status. Precursor failure (pulse/render) can drive DOWN — labeled **"login screen unreachable / not rendering."** Authed-layer failure can drive DOWN — labeled **"online banking behind login not rendering."** Same burst + confidence + floor rules on both paths.
2. **Internally, the two state tracks (`main`, `auth`) remain** — PROGRESS.md documents the real bug a shared status caused (an auth success falsely recovering a site outage). Unification happens at presentation and alerting: platform status = worst-of(main, auth); one row per minute; layer-labeled incidents; **no double-paging** (see suppression rule, Rule 16).
3. **The 3-probe floor now applies to the auth track too.** v3.6's `AUTH_MIN_FAILED_PROBES=1` is retired — it violated the "at least 3 checks" commitment. Since v3.7, authed probes are cheap (session reuse, zero credentials, zero logins), so the auth layer bursts like any other layer: re-probe `run_authed_check()` at the burst offsets. `AUTH_MIN_FAILED_PROBES=3`, `AUTH_DOWN_CONFIDENCE=4`.
4. **`session_expired` is not platform evidence.** A cached-session probe failing with `session_expired` speaks to the session, not the bank. It never scores toward DOWN. It triggers the (single, budgeted) recovery login; the recovery's outcome is the evidence: success → self-healed non-event; `auth_unavailable`/5xx → hard platform evidence; `auth_rejected` → CONFIG. If the session is broken, burst probes each fail fast on the cheap path — that fast-fail IS correct evidence collection, and it still costs zero logins.
5. **Data-plane / API probes (old Stage 9) are OUT OF SCOPE.** The behind-the-wall check is: 200 + authed content locator renders. Nothing deeper. Old Stage 9 is deleted from the plan; `data`/`api` layers, `data_plane_missing`, `api_*` fail_reasons and env vars are removed from the spec (leave dead constants in code untouched until the next natural refactor; do not build on them).
6. **One row per minute: the `cycles` table.** Each 60s cycle writes exactly one summary row combining both tracks; probe-level rows in `checks` link to it via `cycle_id`. Dashboard log becomes the cycles view (one line/minute, layer badges), expandable to probe detail. Both tables CSV-exportable.

## How we work
Unchanged: stages in order, acceptance test + human sign-off before the next, explain before building, PROGRESS.md after every session, stop-and-ask for new dependencies/services. LEARNING.md on request (Rule 9).

## Rules (non-negotiable) — v3.8 consolidated
1. `state.py` stays pure (no I/O), fully unit-tested: burst evaluation, confidence scoring, floors, suppression.
2. Alert ONLY on transitions. DOWN requires BOTH weighted score ≥ threshold AND ≥ floor distinct failed probes within the burst window, no intervening pass. Main track: `DOWN_CONFIDENCE=4`, `MIN_FAILED_PROBES=3`. **[v3.8]** Auth track: `AUTH_DOWN_CONFIDENCE=4`, `AUTH_MIN_FAILED_PROBES=3` (cheap session-reuse probes make this affordable). One DOWN + one RECOVERY email per incident, ever.
3. Locators: `get_by_role`/`get_by_text`/`get_by_label` only. (v3.5 exception: `.nth()` scoped to username/password fields, as shipped.)
4. Every DOWN names its layer with exact operator wording: precursor → **"login screen unreachable / not rendering"**; authed → **"online banking behind login not rendering"**. The operator must never need a browser to know which wall failed.
5. Screenshots on failure only; authed screenshots always masked (PII = data-leak bug).
6. Every probe writes a `checks` row (with `cycle_id`, `layer`, `burst_id`, `browser_mode`); every cycle writes a `cycles` row. CSV export row-for-row faithful for BOTH tables. Storage UTC ISO-8601; presentation (dashboard, CSV, email) America/New_York per v3.1.
7. Secrets from `.env` only; chmod 600 on `.env` and session state; parameterized SQL; credentials/TOTP never logged, rendered, or screenshotted.
8. Single asyncio process, overlap guards, no frameworks/ORM/queues/second process.
9. LEARNING.md documentation duty unchanged.
10. Never retry a credential rejection. `auth_rejected` → CONFIG, halts logins until a human clears it. Applies everywhere, always.
11. Login budget is a hard limit (`LOGIN_INTERVAL_S`, `MAX_LOGINS_PER_DAY`). Cheap session-reuse probes are NOT logins and are exempt. Burst re-probes on the auth layer MUST use `run_authed_check()` — a burst consumes zero logins; at most one budgeted recovery login may run per cycle, outside the burst's scoring.
12. Bot challenges: detect, never defeat. `bot_challenge` → CONFIG, never pages. (v3.2 exception: patchright, scoped and logged, as shipped.)
13. `DEGRADED`/`BLIND` never page. Only DOWN pages.
14. Headless is the scheduler's mode for pulse/render. (v3.3 exception: the sign-in journey runs headed under xvfb, `browser_mode='headed-xvfb'`, as shipped.) Dashboard headless/headed toggle remains forbidden.
15. Channels are plug-ins (`send(event)`, per-channel try/except). SMS = colleague's PR, scoped per CONTRIBUTING.md.
16. **[v3.8, amended 2026-08-11 — Stage R implementation] Layer attribution & no double-paging.** The cheap authed check keeps running every cycle regardless of the main track's status — login-route and authed-home-route outages are genuinely independent evidence (different backends: auth service/MFA/login UI/Cloudflare vs. plain session-cookie validation), and a login-route outage can leave existing sessions still served for roughly one session lifetime. That distinction belongs in the incident record, so it's not skipped. What Rule 16 actually suppresses: while a main-track (precursor) DOWN incident is open, an auth-track failure that would cross into its own DOWN is held back (logged, annotated onto the open main incident) instead of opening a second incident or paging again — if the login screen is down, the alert already covers the symptom. Auth-track DOWN can only *open* while the precursor is passing; suppressed evidence is never discarded, and fires immediately on the next failing probe once the precursor recovers, with nothing lost. Recovery logins (Rule 11's budgeted fallback) are paused while the precursor is DOWN — no budget is spent chasing a possibly-down site; a `session_expired` result during that window is recorded and, per Rule 17, contributes nothing to score either way. Conversely, auth-track recovery never closes a main-track incident, and vice versa. `cycles.authed_ok` is a real True/False whenever the authed check actually ran (including while suppressed) — it is NULL only when the auth track didn't run at all that cycle (unconfigured, or its own CONFIG_ERROR per Rule 10), never because of suppression.
17. **[v3.8] `session_expired` never scores.** It routes to the recovery-login path (Rule 11) and is recorded, but it cannot contribute to DOWN confidence or the probe floor on any track.
18. **[v3.9] The monitor's own failures are recorded, never silent, and never page.** Three parts, all non-negotiable:
    - **(a) No minute may vanish.** The cycle runner is launched fire-and-forget (`asyncio.create_task`), so an unhandled exception used to surface only as asyncio's "Task exception was never retrieved" — no `checks` row, no `cycles` row, no state advance, no alert. A monitor that silently skips a minute is worse than one that reports a wrong answer, because a quiet dashboard reads as "the platform is fine". Every cycle therefore writes a `cycles` row **even when it crashes**: `verdict='DEGRADED'`, `fail_layer='monitor'`, `fail_reason='internal_error'`. The marker write is itself guarded (the DB may be what broke) and failing that too must print a CRITICAL line, not raise.
    - **(b) A monitor bug is not evidence about the bank.** The self-health row is written **directly, never through `apply_check()`** — it cannot touch either track's state, confidence, or probe floor. `DEGRADED` is chosen precisely because Rule 13 guarantees it never pages: manufacturing a DOWN from our own defect would create exactly the false positive this project ranks above every other concern. `internal_error` never reaches `classify()` and has no weight; it is not part of the probe taxonomy below and must never be returned by a probe.
    - **(c) Browser exceptions map onto the closed taxonomy, they don't escape.** No `page.*` call may be left unguarded such that it raises out of a probe. Anything unforeseen resolves via `journey.unexpected_fail_reason()`: timeout-shaped → `timeout`, everything else → `nav_error`, both Soft. **Known limitation, deliberately accepted and documented in code:** a *persistent* monitor-side defect (e.g. a locator that always matches two elements) is indistinguishable from a real soft outage and four such probes reach `DOWN_CONFIDENCE`. The 3-probe floor stops a single flake, not a systematic bug. Closing that needs a distinct non-scoring monitor-error class in the taxonomy — a future amendment, not something to assume is already handled.

## Stack
Python 3.12 · fastapi · uvicorn · curl_cffi (pulse — v: httpx retired after the TLS-fingerprint 403 incident) · patchright (journey; scoped v3.2) · playwright-style API via patchright · sqlite3 · smtplib · python-dotenv · pytest · pyotp.

## Behavior

### Layers of the ONE check (cheapest → strongest)
| Layer | Proves | Mechanism | Track |
|---|---|---|---|
| `pulse` | reachable (DNS/TCP/TLS/HTTP) | curl_cffi GET | main |
| `render` | login page renders | login-form locator visible | main |
| `authed` | **the platform is up (the definition of UP)** | session-reuse check: navigates DIRECTLY to `AUTHED_URL` (never derived from `LOGIN_URL`) and asserts the authed content locator; fresh login only via budgeted recovery path | auth |

### Verdict (presentation layer)
`platform_status = worst_of(main, auth)` where DOWN > CONFIG_ERROR > DEGRADED > UP. The dashboard banner, `/api/status`, and alert subjects all speak platform-level, with the layer named per Rule 4.

### fail_reason (v3.8 — data/api reasons removed)
`timeout | dns | conn_refused | bad_status:<code> | element_missing | nav_error | auth_rejected | auth_unavailable | mfa_failed | bot_challenge | rate_limited | session_expired | logout_failed`

**[v3.9]** This list is the **probe** taxonomy and is unchanged. `internal_error` is deliberately *not* in it: it describes the monitor, not the platform, is only ever written by Rule 18's self-health path (`fail_layer='monitor'`) and by `login_events` when a login attempt produced no result at all, and it must never be returned by a probe or passed to `classify()`. Layers likewise stay `pulse | render | authed`; `monitor` is a `cycles.fail_layer` value only, and `verdict.layer_wording()` returns `None` for it so Rule 4's operator phrasing is never misapplied to our own bug.

### Confidence scoring (both tracks)
| Class | Reasons | Weight |
|---|---|---|
| Hard | `conn_refused`, `dns`, `bad_status:5xx`, `auth_unavailable` | 2 |
| Soft | `timeout`, `element_missing`, `nav_error`, non-5xx `bad_status`, unrecognized | 1 |
| Config | `auth_rejected`, `bot_challenge`, `mfa_failed`, `rate_limited` | 0 → CONFIG_ERROR |
| **[v3.8] Session** | `session_expired` | **0 → recovery-login path (Rule 17), never scores** |
| **[v3.9] Monitor self-health** | `internal_error` | **never enters this table at all — bypasses `classify()`/`apply_check()` entirely (Rule 18b)** |

DOWN = score ≥ 4 AND ≥ 3 failed probes within 90s, no intervening pass. Outcome table (per v3.1): hard-heavy pages on 3 probes ~40s (email <60s); all-soft needs a 4th (~email 70–90s); any pass clears (flap logged); nothing pages under 3 failed probes — on either track.

### Bursts (both tracks)
Main track: as shipped (probe variation render/pulse, offsets `0,15,35,55` ±jitter, window 90s, inline under the overlap lock).
**[v3.8] Auth track:** on a first authed failure (non-config, non-session), burst re-probes call `run_authed_check()` at the same offsets — cheap, zero logins. If a probe fails `session_expired`, it's inert (Rule 17: doesn't count, doesn't clear the burst either) and triggers at most one budgeted recovery login per cycle (paused entirely if the precursor is DOWN, per Rule 16) — that login's real outcome is what actually feeds the burst's scoring. Auth burst rows carry `burst_id` + `cycle_id` like any other probe.

### Tables (v3.8)
- **`cycles` (new):** `(cycle_id, ts, pulse_ok, render_ok, authed_ok, session_reused, pulse_latency_ms, render_latency_ms, authed_latency_ms, verdict, fail_layer, fail_reason, burst_id NULLABLE)` — exactly one row per 60s cycle. `authed_ok` reflects the initiating auth probe's real True/False whenever the auth track ran that cycle (including while its alert was suppressed per Rule 16); it's NULL only when the auth track didn't run at all (unconfigured, or its own CONFIG_ERROR). Index `(ts)`.
- `checks` — as shipped, plus `cycle_id` FK. Probe-level evidence (bursts included) under its cycle.
- `incidents` — as shipped (`track`, `confidence`, `trigger_layer`), plus absorbed-auth annotation per Rule 16.
- `state` — as shipped (per-track rows).
- `login_events` — as shipped; recovery logins and their outcomes land here.
- Migration: additive (`cycles` table, `checks.cycle_id`); no backfill required for historic rows (note in PROGRESS.md).

### Routes & dashboard
Routes as shipped. **[v3.8]** `/api/history` serves cycles (with expandable probe detail); `/api/export` gains `table=cycles|checks` (cycles = primary audit export, checks = probe evidence). Dashboard log = one row per minute: three layer badges (pulse/render/authed), verdict, session-reused marker, burst badge when applicable; failed cycles expand to their probes. Bursts remain first-class and unhideable. Banner speaks platform-level per the verdict rule. Timestamps Eastern everywhere per v3.1.

### Email copy (v3.8 wording locked)
- `[MONITOR] {name} DOWN since {eastern} — login screen unreachable / not rendering (confidence {n}: {reasons}). Dashboard: {url}`
- `[MONITOR] {name} DOWN since {eastern} — online banking behind login not rendering (confidence {n}: {reasons}). Dashboard: {url}`
- `[MONITOR] {name} RECOVERED after {duration}.`
- `[MONITOR-CONFIG] {name} needs attention — {reason}. Sign-in checks paused.`
- DEGRADED info copy unchanged.

## .env (v3.8 deltas only)
Remove: `DATA_PLANE_*`, `API_PROBE_*` (out of scope). Change: `AUTH_DOWN_CONFIDENCE=4`, `AUTH_MIN_FAILED_PROBES=3`. Add: `AUTHED_URL` (the authenticated home route the cheap session-reuse check navigates to directly — required alongside `LOGIN_URL` once Stage 6 is configured; see Rule 16). Everything else as currently shipped (`SESSION_STATE_PATH`, `SESSION_MAX_AGE_S`, `LOGIN_INTERVAL_S`, `MAX_LOGINS_PER_DAY=60`, burst constants, Eastern handled in code).

## Stages (v3.8 — reflects reality per PROGRESS.md)

**Complete, signed off:** Sessions 1–3 ✅ · Stage 4 (shareable + channels + GitHub) ✅ · Stage 5 (+v3.1 floor) ✅ · Stage 6 core journey ✅ (live sign-in end-to-end) · Stage 8 mechanism (session reuse, pulled forward per v3.7) ✅

---

**Stage R — Realignment (built 2026-08-11, pending live verification + human sign-off).** Implement v3.8: `cycles` table + `cycle_id` FK + migration; unified verdict in `/api/status` + banner; Rule 16 suppression/absorption; Rule 17 session_expired routing; auth-track burst via `run_authed_check()` with floor 3 / confidence 4 (retire `AUTH_MIN_FAILED_PROBES=1`); dashboard cycles view + `table=` export param; remove data/api from specs. All state-machine changes land as pure `state.py` logic with tests first. Built and verified via `pytest` + mocked-probe/seeded-DB smoke tests (see PROGRESS.md 2026-08-11) -- not yet drilled against the real target live, so not moved to "complete, signed off" until that happens.
*Accept:* (a) induced precursor outage → exactly one DOWN, precursor wording, auth failures during it absorbed (no second alert); (b) induced authed failure with precursor healthy → burst of ≥3 cheap probes (zero `login_events` rows from the burst itself), one DOWN with authed wording; (c) forced `session_expired` → recovery login runs (≤1, budgeted), success = non-event, and `session_expired` provably contributes 0 to score/floor; (d) dashboard shows one row/minute with three badges; cycles CSV + checks CSV both export and reconcile; (e) `grep` confirms no path can page under 3 failed probes on either track; (f) all existing tests still pass.

**Stage 6 closeout (small, before or with Stage R):** the deferred acceptance drills — wrong-password → `auth_rejected` CONFIG alert, zero retries, logins halt; masked-screenshot PII eyeball check; TOTP cross-check vs the phone app. Plus the three flagged `classify_*` judgment calls (see PROGRESS.md 2026-08-10) — review against live behavior.

**Stage H — Hardening (opened 2026-08-24, blocks Stage 7).** A code-quality review found one systemic defect and a set of specific ones; full findings and measurements in PROGRESS.md 2026-08-24. Sequenced P0→P3, and **Stage 7's soak is not trustworthy until at least P0+P1 are in** — several findings are precisely the kind that a soak would otherwise hide (a silently-skipped minute looks identical to a healthy one).
- **P0 — done 2026-08-24, pending live verification.** Rule 18's self-health net; Rule 11's login ledger written on the failure path (it was bypassable, allowing unbounded credentialed logins against a real account); every Playwright call mapped onto the taxonomy; `.first` on ambiguous free-text locators; guarded screenshot/session-save/TOTP-parse. First automated coverage of `main.py` (11 tests, validated by stashing the fixes — 8 failed against the pre-fix tree).
- **P1 — done 2026-08-24, pending live verification.** `/docs`/`/redoc`/`/openapi.json` disabled (they were served **unauthenticated**, verified live, despite the "only `/healthz` is open" claim); `require_auth` now **fails closed** on unset credentials (`compare_digest("","")` is `True`, so blank config admitted `curl -u ":"` — reachable via `uvicorn monitor.web:app`, which never calls `validate_core`); `PRAGMA journal_mode=WAL` + `busy_timeout=15000` on every connection (rollback-journal mode let a long export read make the cycle's write raise `database is locked`); `/api/export` single-table now streams off a live cursor at flat memory and the `table=all` zip is written member-by-member behind a 200k-row cap that **413s with guidance rather than truncating** (it previously materialised both unbounded tables in the monitor's own process — measured 715–736 MB at one year of data, an OOM kill on the planned B2s). 16 new tests; export output verified byte-identical to the old materialised path, so Rule 6 still holds.
- **P2 — done 2026-08-24, pending live verification.** `uptime_pct`'s denominator now counts only cycles that produced platform evidence (`verdict IN ('UP','DOWN')`); CONFIG_ERROR and DEGRADED are excluded from both sides, so a latched auth CONFIG_ERROR reports "n/a" instead of **0% uptime for a platform that is up every minute** (Rule 13 said it wasn't an outage; the arithmetic now agrees). `cycles.authed_ok`/`authed_latency_ms` are NULL when nothing actually contacted the platform, per Rule 16 — `_run_auth_probe` returns an explicit `probed` flag rather than letting a synthetic `session_expired` masquerade as an observation. `is_session_fresh` now validates the JSON, not just mtime: **this closes a false-DOWN path that P0 opened** (a corrupt file used to die silently; with P0's guard it reported `nav_error` every cycle, and 4 Soft probes reach `AUTH_DOWN_CONFIDENCE`, so a damaged local file would page a false authed DOWN — now it reads as "no session" and the budgeted recovery login self-heals it). Empty `MASK_TEXT` under `MASKING_ENABLED=true` now warns loudly (previously the silent default). `/healthz` returns 503 rather than an unhandled 500 on an unreadable DB, and deliberately does **not** call `init_db` — the schema owner is the monitor process, and a health probe creating tables would hide the very misconfiguration it reports.
  *Still open from P2, needs a human decision:* `SESSION_STATE_PATH` defaults inside the OneDrive-synced repo tree (latent — no session file exists today), and `os.chmod(0o600)` is a silent no-op on Windows despite `session.py` asserting it.
- **P3 — mostly dropped by decision (2026-08-24); one item folded into P2.** (i) **The unreachable authed DOWN was fixed, not deferred** — it was a functional gap, not a spec question: `run_authed_check` now reads `goto`'s `Response` status (Playwright does not raise on HTTP error status, so a 5xx behind login previously produced *zero* evidence) and emits `bad_status:<code>`, and it only calls a missing marker `session_expired` when the page was actually **bounced to a login form** (`bounced_to_login`: redirect away from the authed route *and* a login form present). Still on the authed route with no marker is now `element_missing` — Soft, scoring, burst-opening — so Rule 4's "online banking behind login not rendering" is reachable and Stage R acceptance (b) can pass. (ii) `MASK_TEXT` as a *required* setting: dropped in favour of P2's loud warning; the structural limit stands and is documented in code — `get_by_text` matches text nodes, so masking can never redact an `<input>` value and the login ID will appear in any post-submit failure screenshot. (iii) A non-scoring monitor-error class: dropped — validating the session file removed its main realistic trigger. Rule 18c's limitation is still real but no longer has a known path to fire.

**Stage 7 — Azure deploy + soak (after Stage R **and** Stage H P0+P1).** x86 VM (B2als_v2/B2s), venv, systemd, NTP, chmod 600, dashboard via Tailscale/SSH tunnel only. Soak 2–3 days with the *unified* monitor. `LOGIN_STRESS_MODE` remains available for the sanctioned limit-finding experiment per v3 (test account/site from security) — run it as a bounded window within the soak, then Stage 8's budget numbers get replaced with measured ones and stress mode is deleted per the original plan.
*Accept:* 48h+ unattended; zero false DOWN pages; **zero `verdict='DEGRADED'`/`internal_error` cycles unexplained (Rule 18 makes these visible — a soak with any is not a clean soak)**; stress window produces a measured limit or "none found"; budget values updated from data; stress code deleted (`grep` clean).

---

## Ad hoc backlog (unchanged triggers)
BLIND status (the *self-health* half landed as Rule 18 / Stage H P0 — what remains is a distinct BLIND status + surfacing it, still non-paging per Rule 13) · Maintenance windows · Docker · Cross-browser Edge+Firefox (no WebKit, ever) · Diagnostic runner · `/api/logins` route + login-budget gauge (deferred from Stage 6) · CONFIG_ERROR clear/retry control, dashboard or API (currently the only way to clear a stuck track is a manual DB edit or a passing manual drill run — flagged 2026-08-11 during live Stage R testing) · supportability/code-optimization session (**standing reminder: raise after the Stage 7 soak report; do not refactor toward it proactively**).

## Out of scope (confirm first, always)
**Data-plane / API probes — removed per v3.8: UP is defined as authed 200 + rendered content, nothing deeper.** `journeys.json`, multi-target, voice alerts, AI judge, retention pruning, CI, stealth libraries beyond the scoped patchright exception, dashboard mode toggle, WebKit/Safari, second process.

## Appendix — MFA alternatives (reference only)
Unchanged from v3.
