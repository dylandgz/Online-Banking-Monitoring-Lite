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
