# Progress Log

## Session 1 — "It checks" — 2026-07-30

**Built:**
- `config.py` — loads `.env`, exposes settings as module constants, `validate_core()` fails fast if TARGET_NAME/TARGET_URL/required-locator are missing. Email and dashboard settings are read but not yet validated — nothing in session 1 uses them.
- `monitor/check.py` — `perform_check()`: httpx pulse first (skips the browser check if pulse already fails, since a doomed browser load is wasted work); Playwright Chromium (headless) browser check second, using `get_by_text` or `get_by_role`+name per the locator rule. Returns a `CheckResult(ok, http_status, latency_ms, fail_reason, screenshot_path)`. Screenshot is taken on any browser-check failure (`nav_error` or `element_missing`) and written to `data/artifacts/<UTC timestamp>.png`.
- `monitor/db.py` — schema for `checks`, `incidents`, `state` tables (incidents/state used starting session 2); `init_db()` and `append_check()` only, parameterized SQL.
- `monitor/main.py` — asyncio scheduler that ticks every `CHECK_INTERVAL_S`, launching each check as a background task guarded by a lock; if the previous check is still running when the next tick fires, the cycle is skipped and logged rather than overlapping.
- `.env` / `.env.example`, `.gitignore` (`.env` and `data/` excluded).

**Decisions:**
- **Target changed from the original ask.** The first candidate target sat behind Cloudflare bot protection — headless Chromium and plain httpx both got served a JS-challenge page (403), while a headed browser passed. Separately, its actual account content is behind login, which CLAUDE.md explicitly excludes from Lite scope ("login journey... confirm first"). Rather than build login automation or fight Cloudflare, switched targets.
- **Current target:** the bank's public login page (`TARGET_URL` in `.env`, kept out of version control), checking for visible text `"Username"` on the page. No bot-blocking observed; both pulse and headless browser check pass cleanly. Note: the form is JS-rendered and the text only appears ~4s after `page.goto` — `expect(...).to_be_visible()` polls up to `BROWSER_TIMEOUT_MS`, so this isn't an issue in practice, just worth knowing if the timeout is ever lowered.
- **Pulse-vs-browser ordering:** pulse runs first; if it fails, the browser check is skipped and the pulse's fail_reason (`timeout`/`dns`/`conn_refused`/`bad_status:<code>`) is used directly. This is what lets the future alert body distinguish "site unreachable" from "page loads, content missing."
- **Config validation is currently partial by design** — only fields the check loop uses (target, locator) are required at startup. Email/dashboard fields will be validated once `alert.py`/`web.py` land, so we don't block session 1 on unused config.

**Open issues:**
- If the current target ever gets Cloudflare-style bot protection too, we'd need a fallback decision (headed browser + Xvfb, stealth patches, or another target) — same tradeoff surfaced with the original candidate.
- `get/set_state`, `open_incident`/`close_incident`, `query_range` in `db.py` are not implemented yet — coming in sessions 2/3 alongside `state.py`/`alert.py`/`web.py`.

**Acceptance test (passed):**
- Ran the loop live against the real target's login page: one row/minute, `ok=1`, real latency + HTTP 200.
- Forced failures: nonexistent domain → `fail_reason='dns'`, no screenshot (page never loaded, correctly skipped); reachable page with a deliberately wrong required-text → `fail_reason='element_missing'` + screenshot written to `data/artifacts/`.
- Restarted the process mid-run: prior rows stayed in `data/monitor.db`, new rows appended — no reset, no duplication.

## Session 2 — "It alerts" — 2026-07-30

**Built:**
- `monitor/state.py` — pure `apply_check(state, ok, fail_reason, ts, fails_to_down) -> (new_state, events)`. `MonitorState` is a frozen dataclass (`status`, `consecutive_fails`, `since_ts`). Emits a `DownEvent` only on the exact check that crosses the fail threshold, and a `RecoveryEvent` only on the first success after a DOWN — never on any other tick, so an ongoing incident never re-alerts. `since_ts` only updates on an actual status change (matches the `state` table's 3-column schema exactly; no hidden fields).
- `tests/test_state.py` — 9 unit tests, written and passing before wiring anything in: flap sequence (F,F,S,F,F,S) never alerts, no event below threshold, event fires exactly on the 3rd fail, recovery emits correct duration off the persisted `since_ts`, continued fails post-DOWN never re-alert, and two restart-simulation tests (reload state mid-incident from a `MonitorState` as if freshly read from the DB — no duplicate DOWN, and recovery still computes correctly from the persisted `since_ts`).
- `monitor/db.py` — added `get_state`/`set_state` (read/write the single `state` row), `open_incident`/`close_incident`. `close_incident` targets whichever incident has `ended_at IS NULL`, since only one target means only one incident can be open at a time — avoids needing an incident-id column anywhere else.
- `monitor/alert.py` — `send_email()` via `smtplib.SMTP_SSL` to Gmail; skips (logs, doesn't crash the loop) if email env vars aren't set. `down_message()`/`recovery_message()` build the exact copy from CLAUDE.md's spec, including the "site unreachable" vs "page loads, content missing" distinction (`_pulse_hint`, keyed off which `fail_reason`s imply the pulse itself failed).
- `monitor/main.py` — after each check row is appended, loads state, calls `apply_check`, persists the new state, and on a `DownEvent`/`RecoveryEvent` opens/closes the incident row and sends the email (via `asyncio.to_thread` so the blocking SMTP call doesn't stall the scheduler).

**Decisions:**
- Incident `screenshot_path` is taken from the *confirming* check's `CheckResult` (the exact check that crosses the fail threshold), not a separate lookup — `main.py` already has that result in hand when the `DownEvent` fires.
- `checks_failed` on the incident keeps accumulating for the entire DOWN duration (state.py doesn't stop incrementing `consecutive_fails` just because it stopped alerting), so the final incident row reflects the true total, not just the threshold count.
- Gmail requires an App Password (16-char, e.g. `abcd efgh ijkl mnop`) — the account's regular password fails SMTP auth outright once 2-Step Verification is on. Needed 2-Step Verification enabled on the test Gmail account before App Passwords became available.

**Open issues:**
- No incident-screenshot capture was exercised in this session's real drill (the junk-domain fails were `dns`, which fail before the browser ever loads, so there's nothing to screenshot — consistent with session 1's behavior).
- `query_range` in `db.py` still isn't implemented — lands in session 3 with `web.py`.

**Acceptance test (passed):**
- Ran the *actual* wired-in loop (not simulated) against a real junk domain at a 5s interval: exactly one DOWN email fired on the 3rd consecutive fail, no re-fire on the 4th/5th fails.
- Restored the real target URL: exactly one RECOVERY email fired on the first success, with correct duration (30s) matching the real elapsed time.
- Verified the `incidents` row end-to-end: correct `started_at` (the 3rd fail's timestamp), `ended_at`, `duration_s`, `checks_failed` (5, not 3 — full accumulated count), and `state` table cleanly reset to `UP`/`0` after.
- Bonus: the overlap guard ("skip cycle — previous check still running") fired correctly in this same real run, since the 5s test interval was shorter than the real check's duration.
- Both emails confirmed delivered to the real recipient inbox.

## Session 3 — "You can see it" — 2026-07-30

**Built:**
- `monitor/db.py` — added read-side helpers: `get_last_check`/`get_last_check_ts` (for `/healthz`), `uptime_pct(conn, since_ts)`, `get_open_incident`/`get_incident`/`get_recent_incidents`, `query_checks` (paged + optional from/to range, returns rows + total count), `export_checks` (same range filter, full unpaginated set, chronological order for CSV).
- `monitor/web.py` — FastAPI app with `/`, `/api/status`, `/api/history`, `/api/export`, `/api/artifact/{incident_id}`, `/healthz`. Basic auth via `HTTPBasic` + `secrets.compare_digest` on every route except `/healthz`. Dashboard is a single inline HTML/JS string (no build step, no framework) — banner, 24h/7d/30d uptime, incidents table with screenshot links, filterable+paged check log, CSV download link, 30s auto-refresh via `setInterval`.
- `config.py` — `validate_core()` now also requires `DASHBOARD_USER`/`DASHBOARD_PASSWORD` (auth would be silently broken without them).
- `monitor/main.py` — `uvicorn.Server` and the check `scheduler()` now run together via `asyncio.gather` in one process, per rule 8 (single process, no second service).
- Generated test dashboard credentials (`admin` / a random 16-char password) since none were supplied — see `.env`.

**Decisions:**
- Incidents/history/artifact routes are folded into the fixed route list from CLAUDE.md rather than adding new endpoints — e.g. there's no separate `/api/incidents`; the incidents list rides along in `/api/status`'s response, since the dashboard needs both together anyway.
- `close_incident` and `/api/artifact` both assume at most one incident is ever open at a time (true for a single-target monitor) — no incident-id tracking needed anywhere outside the DB itself.
- Web routes each open their own short-lived SQLite connection per request (via a FastAPI dependency), separate from the long-lived connection the check loop holds — avoids any doubt about SQLite cross-thread/cross-task connection sharing now that uvicorn's threadpool is in the picture.

**Open issues:**
- None carried forward — this closes out all three sessions in CLAUDE.md's plan. Per CLAUDE.md, next is a 2–3 day soak against the real target site before trusting it, and everything under "Out of scope" stays out unless explicitly revisited.

**Acceptance test (passed):**
- `/healthz` returns 200 with no credentials; `/` and all `/api/*` routes return 401 with no or wrong credentials, 200 with correct ones.
- Verified the dashboard renders correctly in an actual browser (Playwright, screenshotted): UP banner, uptime %, incidents table, paged check log all populate from real data.
- Induced a real failure (junk domain): dashboard flipped to the DOWN banner, 0% uptime, and an "(ongoing)" incident row within one 30s-refresh-equivalent check — confirmed both via `/api/status` and an actual browser screenshot showing all `FAIL`/`dns` rows.
- Restored the real URL: incident closed correctly (`ended_at`, `duration_s`, `checks_failed`) and dashboard returned to UP.
- CSV export: 18 data rows in the file vs. `SELECT COUNT(*) FROM checks` = 18 — exact row-for-row parity, chronological order, all columns intact.

## Incident — production 403s against the target's marketing page — 2026-08-01

**What happened:** during the soak, the real target (the site was switched at some point after session 1 from the original dbank login page to the bank's marketing-site login page) started returning `403 bad_status:403` on every pulse check. 83 consecutive fails over ~80 minutes, correctly triggered exactly one DOWN alert (no dupes) and stayed DOWN the whole time — the state machine behaved exactly as designed even under a real, sustained outage-shaped failure.

**Root cause (confirmed by direct testing, not assumed):** it wasn't a real outage or a Cloudflare JS challenge. Plain `httpx` gets TLS/HTTP2-fingerprinted and 403'd by the host's edge WAF (`server: Pantheon`, `x-pantheon-serious-reason: The page could not be loaded properly`) regardless of headers/User-Agent — the block happens at the TLS ClientHello, before headers are even read. Headless Playwright was *never* blocked (real Chromium's TLS stack passes cleanly) — only the `httpx` pulse check was affected. Verified directly: plain `httpx` → 403, `curl_cffi` with `impersonate="chrome"` → 200 with full page + required text, headless Playwright → 200 the whole time.

**Fix:** swapped `monitor/check.py`'s `pulse_check()` from `httpx` to `curl_cffi` (`AsyncSession().get(..., impersonate="chrome")`), preserving the exact same `fail_reason` taxonomy (`timeout`/`dns`/`conn_refused`/`bad_status:<code>`/`nav_error`) by mapping curl_cffi's exception types (`DNSError`, `ConnectTimeout`/`ReadTimeout`, `ConnectionError`, base `RequestException`). `httpx` was unused elsewhere so it was dropped from `requirements.txt` in favor of `curl_cffi` — no dependency net-add, straight swap. Browser check untouched (it was never the problem).

**What was deliberately not done:** a research doc explored heavier options first — residential proxies, browser-as-a-service, monitoring both the marketing page and the separate dbank backend as independent targets, stealth headless drivers (`nodriver`/`patchright`). None of that was needed: a 30-second direct test showed the block was TLS-fingerprint-only on one HTTP client, not a JS challenge or multi-layer WAF. Multi-target monitoring stays out of scope per CLAUDE.md unless explicitly revisited later.

**Verified live:** restarted the running monitor process; it immediately re-checked as UP (200), closed the incident (`duration_s=4827`, `checks_failed=83`), reset state to `UP`/`0`, and fired the real RECOVERY email to the configured recipient.

**Open issue:** if this WAF's fingerprinting rules change again (curl_cffi's Chrome impersonation profile can go stale as WAFs update detection), the same 403 pattern could recur. If it does, re-run the same direct-test approach (plain client vs. `curl_cffi` vs. headless browser against the live URL) before assuming a bigger fix is needed — it was TLS/header, not JS-challenge, both times so far.

## Stage 4 — "It's shareable" — 2026-08-04

**Built:**
- `monitor/channels/` — replaces `monitor/alert.py`. `base.py` defines the `AlertChannel` ABC (`name`, `send(event)`); `email_gmail.py` is the old `alert.py` logic moved in unchanged (same message copy, same skip-if-unconfigured behavior) wrapped in `EmailGmailChannel`; `sms_stub.py` is the contribution template — it deliberately raises `NotImplementedError` so it doubles as the "a broken channel can't take down the others" proof, not just a placeholder. `__init__.py` has `build_channels(names)` (reads `ALERT_CHANNELS`) and `dispatch(event, channels)` (fans an event out with a per-channel `try/except`, per Rule 15).
- `CONTRIBUTING.md` — scopes an SMS PR to `channels/sms_*.py`, its tests, and `.env.example`; states the contract, the "raise on failure, don't swallow" expectation, and the mocked-network testing bar.
- `monitor/db.py` — `checks.browser_mode` column + migration (`ALTER TABLE` + backfill `'headless'` for pre-existing rows, since the scheduler has only ever run headless). `append_check()` takes `browser_mode`, default `"headless"`.
- `monitor/check.py` — `browser_check()`/`perform_check()` take a `headless` param (was hardcoded `True`), sourced from the new `config.HEADLESS` (Rule 14: headless is scheduler-only; `HEADLESS=false` stays a local/manual escape hatch, never wired to anything scheduled).
- `config.py` — added `ALERT_CHANNELS` (comma-separated, default `email`), `HEADLESS`/`BROWSER_MODE`, and `BROWSER_CHANNEL` (read but not yet wired into the Playwright launch call — see Open issues).
- `monitor/web.py` — CSV export now includes `browser_mode` so the export stays row-for-row faithful to the DB (Rule 6).
- `README.md`, `.gitignore` additions, repo pushed to GitHub.

**Decisions:**
- Channels receive the raw `DownEvent`/`RecoveryEvent` dataclasses from `state.py`, not a pre-formatted string — each channel formats its own message (email stays long-form via `down_message`/`recovery_message`; SMS is expected to write its own terse formatter). Keeps the "operator can tell what failed" requirement (Rule 4) per-channel instead of forcing one message shape on every medium.
- `sms_stub.py` raising `NotImplementedError` was a deliberate choice over a no-op stub — it means the crash-isolation property (`dispatch()`'s per-channel try/except) is exercised by the codebase as it exists today, not just asserted in a docstring, even before anyone enables `ALERT_CHANNELS=email,sms`.
- **Hygiene gate finding:** `PROGRESS.md` (sessions 1–3, plus the 403 incident writeup) named the real bank and its URLs in plain text. Redacted to "the target"/`TARGET_URL` before this session's commit — CLAUDE.md's Stage 4 acceptance criteria explicitly requires this ("no bank URLs or credentials in committed files or PROGRESS.md"). Nothing else scanned (`.py`, `.env.example`) had it; `.env` itself was already gitignored and never staged.

**Open issues:**
- `BROWSER_CHANNEL=chrome` is read into `config.py` per the v3 `.env` spec but **not yet passed to `p.chromium.launch()`** in `check.py` — the real Chrome binary isn't installed on this machine (`playwright install chrome` hasn't been run), and wiring it now would silently break the live check loop. Left as config-only; wire it in once the binary is installed, ideally alongside Stage 6 (Rule 12's bot-challenge mitigation is most relevant once login is in play anyway).
- `channels/sms_stub.py` is unimplemented by design — waiting on the colleague's PR per `CONTRIBUTING.md`.

**Acceptance test (passed):**
- `pytest` — all 9 pre-existing `state.py` tests still pass post-refactor (untouched by this stage).
- Import smoke test confirmed `main.py`/`web.py`/`channels/*` all wire together with no circular-import or missing-symbol errors.
- Simulated a crashing channel + a working channel through `dispatch()`: the crashing one logs and is swallowed, the working one still receives the event — confirms Rule 15 holds before any real SMS code exists.
- Simulated the `browser_mode` migration against an in-memory DB seeded to look like a pre-v3 `checks` table (no `browser_mode` column): `init_db()` added the column and backfilled existing rows to `'headless'`; a fresh `append_check()` call wrote `'headless'` on the new row too.
- Fresh clone would still need a live target + real secrets in `.env` to run end-to-end — not re-verified against the live bank site this stage, since nothing in the check/alert path itself changed behaviorally (only where the code lives).

## Stage 5 — "It's fast" — 2026-08-04

**Built:**
- `monitor/state.py` — full rework, replacing the old "N consecutive fails" rule with confirmation-burst + confidence-weighted scoring (Rule 2, Stage 5). `MonitorState` gains `burst_started_ts`, `confidence`, `fail_reasons`; drops `consecutive_fails`. New `classify(fail_reason) -> (class, weight)`: hard=2 (`conn_refused`, `dns`, `auth_unavailable`, `bad_status:5xx`, `api_bad_status:5xx`), soft=1 (`timeout`, `element_missing`, `nav_error`, `data_plane_missing`, `api_shape_mismatch`, and any non-5xx `bad_status:`/unrecognized reason — ambiguous evidence defaults cautious rather than hard), config=0 (`auth_rejected`, `bot_challenge`, `mfa_failed`, `rate_limited`). `apply_check()` now takes `layer` and returns `DownEvent`/`RecoveryEvent`/new `ConfigErrorEvent`. Still pure, still no I/O.
- `tests/test_state.py` — rewritten, 20 tests: `classify()` coverage for all three classes plus the unknown-reason fallback; two-hard-fails pages at the second probe (~15s), three-soft-fails pages at the third (~35–40s); a below-threshold single/double fail never alerts; the (F,S,F,F,S) flap sequence never alerts; a stale failure outside `BURST_WINDOW_S` starts a fresh burst instead of compounding a dead one; no duplicate DOWN during a sustained incident; recovery duration math; two restart-from-DB simulations (mid-DOWN, mid-recovery); `CONFIG_ERROR` routing — never scored, never re-alerts, holds through unrelated ordinary failures underneath it, and recovers on the first pass like DOWN does.
- `monitor/check.py` — `CheckResult` gains `layer` (`"pulse"` | `"render"` — the strongest layer a probe actually evaluated). `perform_check()` tags it. New `pulse_only_probe()` / `render_only_probe()` — single-purpose probes for burst re-probing (Stage 5's "vary the probe" rule: pulse and render are independent evidence, unlike two identical retries).
- `monitor/db.py` — `checks` gains `layer`, `burst_id`; `incidents` gains `confidence`, `trigger_layer` (replacing the old `checks_failed`-only shape — `checks_failed` is kept, repurposed as "probe count in the deciding burst/incident"); `state` gains `burst_started_ts`, `confidence`, `fail_reasons` (comma-joined text — no new dependency for JSON). All migrated via `ALTER TABLE` against a pre-existing DB, same pattern as `browser_mode`. Had to split `CREATE INDEX` out of the initial `executescript` and run it *after* migrations — an index on a column that migration hasn't added yet fails on an existing DB (caught by the migration smoke test, see below).
- `monitor/main.py` — burst orchestration runs **inline**, synchronously, inside `run_check_cycle()`: the normal 60s-cadence check is always burst-probe index 0; if it starts a new burst, the remaining `BURST_DELAYS_S` offsets (render, then pulse) run right there with `asyncio.sleep` in between, before the function returns. Deliberately not spawned as a separate task — holding the scheduler's overlap-guard `lock` for the burst's ~40s span means the next 60s tick is correctly skipped/absorbed instead of double-probing mid-burst, with zero new concurrency primitives. Stops early the moment the burst resolves (DOWN fires, or a pass clears it) rather than always running all 3 probes.
- `monitor/channels/email_gmail.py` — `down_message`/`recovery_message` take the event object directly (confidence + fail_reasons + trigger_layer, not a single consecutive-fail count); new `config_message()` for `ConfigErrorEvent`, matching the `[MONITOR-CONFIG]` copy from CLAUDE.md.
- `config.py` / `.env.example` — `FAILS_TO_DOWN` removed; added `BURST_DELAYS_S`, `BURST_JITTER_S`, `BURST_WINDOW_S`, `DOWN_CONFIDENCE`.
- `monitor/web.py` — CSV export includes `layer`/`burst_id` for full audit fidelity; dashboard banner shows the actual status word (`CONFIG_ERROR since ...`) instead of hardcoding "DOWN" for every non-UP state.

**Decisions:**
- Burst probes run inline rather than as a detached `asyncio.create_task` — considered spawning a separate task so the scheduler's 60s loop stays untouched, but that needs a second lock (or careful coordination) to stop the *next* normal tick from racing the in-flight burst against the same DB row. Inline + the existing overlap-guard lock gets the same correctness for free.
- `checks.burst_id` is generated in `main.py` (I/O layer), not `state.py` — a pure function can't emit a random UUID and stay referentially transparent/testable. `state.py` only signals "a new burst started here" via `burst_started_ts == ts`; `main.py` reads that signal to decide whether to mint an ID and keep probing.
- A burst that exhausts all of `BURST_DELAYS_S` without reaching `DOWN_CONFIDENCE` and without a passing probe just... stops, with `state.status` still `UP` and confidence retained. No special "give up" event needed — the next normal-cadence failure either continues accumulating on the same burst (still inside `BURST_WINDOW_S`) or starts fresh (window expired), handled by the same pure rules with no extra state.
- `ConfigErrorEvent` does not open an `incidents` row. An incident is CLAUDE.md's outage record (confidence, trigger_layer, duration); a bad credential or bot challenge isn't an outage, it's a "needs a human" condition, so it only produces the `[MONITOR-CONFIG]` alert.
- Non-5xx `bad_status:` (e.g. a 403 or 404) and any *unrecognized* fail_reason string both default to Soft rather than Hard. CLAUDE.md's confidence table only lists `bad_status:5xx` under Hard — everything else is a judgment call filling a gap in the spec, resolved toward "ambiguous evidence stays cautious" per the project's stated false-positive priority.

**Open issues:**
- Not yet soaked against the real target — `perform_check`/`render_only_probe`/`pulse_only_probe` were exercised via mocks (see acceptance tests below), not a live outage. Worth a short live drill before Stage 6 lands on top of this.
- `BURST_DELAYS_S`'s 60s window and a 60s `CHECK_INTERVAL_S` are close enough that a burst which exhausts its 3 probes without resolving could have its *next* normal-cadence failure land right at the edge of `BURST_WINDOW_S`, going either way (continues vs. fresh burst) depending on exact timing. Inherent to the numbers in the spec, not a bug; flagged here in case it ever needs tightening.

**Bug fix (found while starting the live dashboard, not part of Stage 5's own work):** `db.get_connection()` opened SQLite connections without `check_same_thread=False`. FastAPI's sync dependency-with-yield (`get_conn` in `web.py`) and the route handler it feeds aren't guaranteed to run on the same anyio threadpool worker, so a connection created in one worker occasionally got used from another — intermittent `sqlite3.ProgrammingError` on `/api/status` under real traffic (never surfaced in tests since those don't go through FastAPI's threadpool). Pre-existing since Session 3; only noticed now because this was the first time the dashboard ran live against the Stage-5 code. Fixed by passing `check_same_thread=False` — safe here since each request's connection is only ever used sequentially within that one request, never shared concurrently. Verified with 20 concurrent `/api/status` requests post-fix: all 200, zero threading errors (was reproducible before the fix within the first couple of requests).

**Live verification:** killed a stale process from the previous day (pre-Stage-5 code) that was still holding port 8080 against the real target, confirmed the DB migration ran cleanly against its live `data/monitor.db` (pre-existing rows show blank `layer`/`burst_id` as expected, new rows tag `layer=render` correctly), then started the current build. Dashboard, `/api/status`, `/api/export` (CSV), and `/healthz` all verified live on port 8080.

**Acceptance test (passed):**
- `pytest` — 20/20 `state.py` tests pass, including every scenario from the Stage 5 acceptance bullet list (two hard fails page in ~15s equivalent, three soft fails page in ~35–40s equivalent, flap sequence never alerts) plus the pre-existing Session-2 scenarios ported to the new event shape (recovery duration, no duplicate DOWN, restart-safety).
- DB migration smoke test: seeded an in-memory SQLite DB shaped like the pre-Stage-5 schema (no `layer`/`burst_id`/`confidence`/`trigger_layer`/`burst_started_ts` columns), ran `init_db()`, confirmed all new columns exist and `get_state()`/`set_state()` round-trip correctly.
- Full `main.py` orchestration exercised end-to-end (real `run_check_cycle()`, mocked `check.*` probes, in-memory DB, a recording alert channel — no real network/browser):
  - Two hard failures (`conn_refused`) → burst starts, resolves DOWN on the 2nd probe, 3rd probe correctly never runs (early exit), `DownEvent` dispatched, incident row written with `confidence=4`, `trigger_layer='pulse'`.
  - A soft failure followed by a passing render re-probe → burst clears mid-flight, zero events, zero alerts, state back to a clean `UP`.
  - A `bot_challenge` on the render layer → routes straight to `CONFIG_ERROR`, `ConfigErrorEvent` dispatched, **no incident row created**, no burst attempted at all (config reasons bypass scoring, confirmed via `state.confidence == 0`).

## CLAUDE.md amendment v3.1 + Eastern-time retrofit — 2026-08-04

**Amended (docs only, no code implied by this part):**
- `CLAUDE.md` — v3.1 amendment. Confirmation-burst decision rule is spec'd to require BOTH weighted score ≥ `DOWN_CONFIDENCE` AND ≥ `MIN_FAILED_PROBES` distinct failed probes in the burst window (new defaults: `DOWN_CONFIDENCE=4`, `MIN_FAILED_PROBES=3`, `BURST_DELAYS_S=0,15,35,55`, `BURST_WINDOW_S=90`), with a full outcome table and updated timing budget (typical <60s, worst-case ≤90s). Dashboard spec now requires burst probes to stay visible as first-class, `burst_id`-badged, filterable rows. This is a **spec update for whenever `state.py`'s burst logic is next touched** — Stage 5 (already built and signed off against the old score-only rule) is not being reopened by this amendment.

**Built (bugfix-scale retrofit, not a stage — applies the other half of v3.1, the Eastern-time presentation change, to the already-shipped Sessions 1–3 output):**
- `monitor/timeutil.py` — new module, `to_eastern(ts_utc_iso)`: converts a stored UTC ISO-8601 timestamp to `America/New_York` (via stdlib `zoneinfo`, no new dependency) and formats it as `YYYY-MM-DD HH:MM:SS TZ (UTC±HH:MM)` — e.g. `2026-08-04 10:32:05 EDT (UTC-04:00)`. Using the zone rather than a fixed offset means DST (EST↔EDT) is handled automatically.
- `monitor/web.py` — `/api/status` now converts `since_ts`, `last_check.ts`, and each incident's `started_at`/`ended_at` to Eastern before returning; `/api/history` converts each row's `ts`; `/api/export` CSV renames the `ts` column to `ts_eastern` and converts every value. Dashboard check-log header changed from "Timestamp (UTC)" to "Timestamp (Eastern)". Storage (`monitor/db.py`, `checks`/`incidents`/`state` tables) is untouched — still UTC ISO-8601, per Rule 6.
- `monitor/channels/email_gmail.py` — `down_message()` renders `since_ts` via `to_eastern()` instead of the raw UTC string.
- `tests/test_timeutil.py` — 4 new tests: a winter UTC timestamp converts to EST, a summer one converts to EDT, a naive (no-offset) ISO string is treated as UTC (matches how `db.py` actually stores them), and the output always embeds an unambiguous UTC offset.

**Decisions:**
- Conversion happens server-side (Python) at the presentation boundary — API responses, CSV rows, email bodies — rather than client-side in dashboard JS. This keeps CSV/email (which have no JS runtime) and the dashboard consistent through one code path, per the instruction to use `zoneinfo` with no new dependency.
- Dashboard JS itself needed no changes beyond the header label: it already just displays whatever string the API sends for `ts`/`since_ts`/`started_at`/`ended_at`, so once those are Eastern-formatted server-side, the browser renders them correctly with no client-side parsing.
- Did **not** touch `state.py`, burst timing, confidence weights, or add burst-grouping UI — CLAUDE.md's v3.1 spec update for those is intentionally documentation-only this session, per explicit instruction that they land whenever Stage 5's burst logic is next revisited, not now.

**Open issues:**
- None. All 24 tests pass (20 pre-existing `state.py` tests, untouched, + 4 new `timeutil.py` tests). Smoke-tested `monitor.web`/`monitor.channels.email_gmail` imports and `to_eastern()` output against the project's `.venv` (Python 3.12.2) — not re-verified against a live browser/email send this session, since no check-loop or dashboard-rendering behavior changed, only timestamp formatting in already-exercised code paths.

## v3.1 burst-logic + dashboard implementation — 2026-08-04

**Built (implements the confidence/probe-floor half of the v3.1 amendment, previously docs-only):**
- `monitor/state.py` — `apply_check()` gains a required `min_failed_probes` param. DOWN now fires only when weighted score ≥ `down_confidence` **AND** `len(new_reasons) >= min_failed_probes` (distinct failed probes in the current burst, no intervening pass). A burst that hits the score threshold with too few probes (e.g. two hard failures, score 4) just keeps accumulating — no event — until a further failed probe satisfies the floor.
- `config.py` / `.env.example` / `.env` — new defaults per the amendment: `DOWN_CONFIDENCE=4`, `MIN_FAILED_PROBES=3`, `BURST_DELAYS_S=0,15,35,55`, `BURST_WINDOW_S=90`.
- `monitor/main.py` — `apply_check()` call now passes `config.MIN_FAILED_PROBES`. Burst re-probe orchestration (`_run_burst_reprobes`) now derives its probe-kind list from `len(config.BURST_DELAYS_S) - 1` (cycling render/pulse) instead of a hardcoded 2-item list, so the new 4-probe burst (index 0 + 3 re-probes) still varies its evidence instead of silently truncating to 3 probes.
- `monitor/web.py` — dashboard check-log table gains `Layer` and `Burst` columns; rows belonging to a burst get a `burst-row` CSS class (tinted background) and an amber "burst" badge with the first 8 chars of `burst_id`, so bursts stay visible as first-class, grouped, audit-evidence rows per CLAUDE.md's dashboard spec — they're never hidden, and the existing from/to filter and newest-first ordering apply to them like any other row.
- `tests/test_state.py` — rewritten around the new rule: separate tests for each acceptance scenario (a) hard-mixed pages on the 3rd probe, (b) all-soft needs a 4th, (c) two-then-pass never alerts, (d) explicit hard+hard-2-probes-must-not-page-yet floor test, (e) config-class never counts toward score or the probe floor — plus updated flap/restart/recovery/CONFIG_ERROR tests carried forward with new timing constants (`BURST_WINDOW_S=90`, `BTS` offsets matching `0,15,35,55`). 29 tests total, all passing.

**Decisions:**
- `min_failed_probes` was made a required positional param (no default) on `apply_check()`, matching how `down_confidence`/`burst_window_s` are already threaded through from `config` — keeps the pure function's inputs fully explicit rather than hiding a threshold behind a default that could silently diverge from `.env`.
- The probe-floor check uses `len(new_reasons)` rather than a separate counter — `fail_reasons` already accumulates exactly one entry per failed probe within the active burst (config-class failures never reach this code path), so probe count and reason-list length are already the same number; no new state field needed.
- Burst re-probe kinds cycle `render, pulse, render, ...` for however many re-probes `BURST_DELAYS_S` calls for, rather than hardcoding a fixed list — avoids silently under-running the configured burst length if `BURST_DELAYS_S` is ever tuned again.
- Dashboard burst grouping is visual only (badge + row tint), not a hide/show toggle — CLAUDE.md is explicit that bursts "cannot be hidden from the UI," so no control was added that would let an operator filter them out.

**Open issues:**
- Not yet exercised against a live outage with the new 4-probe/90s-window timing (only unit-tested via `state.py` and a prior live burst under the *old* 3-probe/60s rule, seen in this session's earlier "when was the email sent" investigation). Worth a short live drill before trusting the new timing in production.
- `monitor/db.py`'s `checks_failed` on `incidents` still reflects the same "probe count in the deciding burst/incident" semantics as before — unaffected by this change, but worth remembering the number will now typically be ≥3 at minimum (never 1 or 2) given the new floor.

**Acceptance test (passed):** `pytest` — 29/29, all 5 of the CLAUDE.md v3.1 Stage-5 acceptance scenarios (a)-(e) covered by name, plus every pre-existing scenario (flap, stale-window reset, sustained-incident no-dup, restart-safety, CONFIG_ERROR routing) re-verified under the new constants. Config/import smoke test confirmed `.env`'s new `BURST_*`/`MIN_FAILED_PROBES` values load correctly and `monitor.main`/`monitor.web` import with no errors.
