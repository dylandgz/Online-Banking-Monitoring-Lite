# CLAUDE.md — Monitor Lite

> **Amendment v3 — 2026-08-03.** Supersedes the v2 amendment. Sessions 1–3 remain **complete and signed off — do not rebuild.** v3 re-sequences everything after them, adds a contribution seam for an external SMS PR, codifies the browser-mode decision, confirms TOTP as the MFA factor, and introduces a **sanctioned login-stress experiment** during the cloud soak (test account + test site provided by the security team — approved usage). Changes vs v2 are marked **[v3]**. Read "What changed in v3" before touching anything.
>
> **Amendment v3.1 — 2026-08-04.** Supersedes nothing built so far — Stage 5 already shipped and is **not** being reopened by this amendment; it updates the *spec* for confirmation-burst confidence scoring (a floor on distinct failed probes, not just weighted score) for whenever that logic is next touched, adds a dashboard requirement that burst probes stay visible as first-class audit rows, and moves all *presentation* timestamps (dashboard, email, API responses that feed the UI, CSV) to `America/New_York` while storage stays UTC. Changes vs v3 are marked **[v3.1]**. This amendment also authorizes one immediate bugfix-scale retrofit: applying the Eastern-time presentation change to the already-built dashboard/email/CSV output (not a new stage).

## What this is
A quick-and-dirty uptime monitor for a banking web platform. Plain Python process (no Docker — Docker is post-soak backlog). Every 60s: httpx pulse + Playwright page check, extended to a full **authenticated sign-in journey** (Stage 6) proving the platform is up *past the login wall*. Results in SQLite. Email alert on state transitions only (SMS arrives later via a colleague's PR against the channels seam). FastAPI dashboard with uptime %, incidents, an audit-grade check log, and CSV export. Screenshot on failure only. It replaces a Selenium tool that false-positived on minor DOM changes — **low false positives outrank every other concern, including speed.**

## What changed in v3 (orientation for Claude Code)
1. **New stage order:** 4 GitHub-ready + channels seam → 5 burst/confidence (moved BEFORE auth: sub-60s detection on the public check first) → 6 sign-in + MFA → 7 Azure deploy + stress soak → 8 session reuse + login budget → 9 data plane + API probe → ad hoc backlog.
2. **SMS is no longer out of scope.** A colleague will contribute it via pull request. Stage 4 builds the seam and contribution kit they'll code against.
3. **Browser mode decided:** headless is the only scheduled mode; `HEADLESS=false` + xvfb is a documented escape hatch for local/diagnostic use; a dashboard headless/headed toggle is **forbidden** (Rule 14). Every check row now records `browser_mode`.
4. **Chrome focus:** `BROWSER_CHANNEL=chrome` (branded Chrome) replaces `msedge` as primary. Edge + Firefox are the future cross-browser pair (backlog). **WebKit/Safari is removed permanently** — Playwright cannot drive real Safari; it is not part of this project's definition of done.
5. **MFA factor is known: TOTP (authenticator app).** `pyotp` implementation confirmed; alternatives demoted to an appendix.
6. **The login-stress experiment [v3]:** during the soak, the monitor deliberately signs in every 60s (~1,440/day) to measure the platform's real limits — lockout threshold, rate limits, bot challenges. This is approved: test account and test site were issued by the security team for exactly this. Stage 8 then engineers session reuse using the measured numbers instead of guesses, and stress mode is **removed** (not disabled) when Stage 8 lands.
7. Burst evaluation runs pre-auth at Stage 5 (pulse + fresh-context render probes only); its "zero logins consumed" property is re-asserted at Stage 8 once sessions exist.
8. Self-health/BLIND, maintenance windows, Docker, cross-browser, and the diagnostic runner move to an **ad hoc post-soak backlog** with explicit return conditions.
9. **Deferred on purpose:** a supportability/code-optimization session (structure, logging, second-maintainer onboarding) happens **after the soak**. Claude Code must not proactively refactor for it. (Standing reminder — see end of file.)

## What changed in v3.1 (orientation for Claude Code)
1. **[v3.1] Confirmation-burst decision rule tightened:** DOWN now requires BOTH the weighted score reaching `DOWN_CONFIDENCE` **and** at least `MIN_FAILED_PROBES` distinct failed probes within the burst window, with no intervening pass. This is a spec update for the next time `state.py`'s burst logic is touched — it does **not** retroactively reopen Stage 5, which is already built and signed off against the old (score-only) rule.
2. **[v3.1] New burst defaults:** `DOWN_CONFIDENCE=4`, `MIN_FAILED_PROBES=3`, `BURST_DELAYS_S=0,15,35,55` (four probes), `BURST_WINDOW_S=90`. Weights unchanged (Hard=2, Soft=1, Config=0; config-class still never contributes to score or the probe floor).
3. **[v3.1] Dashboard must surface bursts as audit evidence:** burst probes are first-class rows in the check log, visually grouped/badged by `burst_id`, filterable, newest first — they can never be hidden from the UI.
4. **[v3.1] Presentation timestamps move to Eastern:** storage stays UTC ISO-8601 (Rule 6 unchanged) everywhere in SQLite. Everything a human reads — dashboard, email alert bodies, `/api` responses that feed the UI, CSV exports — renders in `America/New_York` (not fixed EST/EDT, so DST is handled automatically) via Python's `zoneinfo`, no new dependency. CSV's timestamp column is renamed `ts_eastern` and every value embeds its UTC offset so an auditor is never ambiguous about which wall-clock moment a row means. **Flag to the human:** if they actually want a fixed offset (e.g. always EST, never adjusting for DST) rather than the real Eastern civil time, that's a different, smaller change — confirm before assuming `America/New_York` is wrong.
5. **This session's retrofit is a bugfix, not a stage.** The Eastern-time presentation change was applied immediately to Sessions 1–3's existing dashboard/email/CSV output — it did not wait for a future stage, since it touches already-shipped display code, not new monitoring capability.

## How we work
- Build in the stages below, **in order, one feature per stage**. Each has an acceptance test; it passes and the human signs off before the next stage starts. Do not build ahead, do not bundle stages.
- The human is learning: explain what you're building and why *before* writing it; after each stage, append to `PROGRESS.md` (date, what was built, decisions, open issues).
- Keep it small. New dependency, background service, or second process → **stop and ask.** (Pre-approved: `pyotp` at Stage 6.)

## Rules (non-negotiable)
1. `state.py` is a pure function of (previous_state, check_result) → (new_state, events). No I/O. Real unit tests before wiring. Includes burst evaluation, confidence scoring, and suppression — all pure.
2. Alert ONLY on transitions. DOWN is declared when a confirmation burst's confidence reaches `DOWN_CONFIDENCE` within `BURST_WINDOW_S` (Stage 5). **[v3.1]** AND at least `MIN_FAILED_PROBES` distinct failed probes have occurred in that burst, no intervening pass — see "Confidence scoring" below for the updated defaults and outcome table. First success after an incident → one RECOVERY email. Never re-email during an incident.
3. Playwright locators: `get_by_role` / `get_by_text` / `get_by_label` ONLY. No structural CSS/XPath — that brittleness is the disease this project cures. Applies to authenticated pages and screenshot-mask targets.
4. The alert body names the failed layer: unreachable · loads-but-content-missing · login rejected · logged-in-but-data-plane-dead. The operator must distinguish "bank down" from "page changed" without opening a browser.
5. Screenshots on failure only → `./data/artifacts/`. Post-login screenshots **must** use Playwright `mask=[...]` over balance/account/PII locators. An unmasked authenticated screenshot is a data-leak bug.
6. Every check writes a row, pass or fail — the log is an audit artifact. CSV export row-for-row faithful. Timestamps UTC ISO-8601. Burst probes write their own rows tagged `burst_id` and `layer`. **[v3]** Every row records `browser_mode`.
7. Secrets from `.env` only (gitignore `.env`, `data/`, session state). `chmod 600` on `.env` and session files. Parameterized SQL. Credentials and `TOTP_SECRET` are never logged, rendered, or screenshotted.
8. No frontend framework, no build step, no ORM, no queue, no second process. Single asyncio process with overlap guard.
9. **Learning & documentation:** when the human asks to learn something, document it in `LEARNING.md` with explanations, analogies, and examples.
10. **Never burst-retry a credential rejection.** `auth_rejected` is a config error: excluded from confidence scoring, distinct CONFIG alert, halts further logins until a human clears it. **[v3] This rule holds during the stress experiment** — stress mode raises login *cadence*; it never retries a rejection. A lockout mid-experiment is the experiment's result, not an error to push through.
11. The login budget is a hard limit (`LOGIN_INTERVAL_S`, `MAX_LOGINS_PER_DAY`); at the cap, degrade to unauthenticated checks and say so. **[v3] Single sanctioned exception:** `LOGIN_STRESS_MODE=true`, only during Stages 6–7, only against the security-team test site/account, and removed from the codebase at Stage 8.
12. Detect bot challenges; never defeat them. `bot_challenge` never pages. Stealth/fingerprint-spoofing libraries are forbidden. Permitted mitigations: a real branded browser (**[v3]** `BROWSER_CHANNEL=chrome`) and a polite, identifiable User-Agent.
13. `DEGRADED` and `BLIND` never page. Only `DOWN` sends the urgent email.
14. **[v3] Browser mode:** headless is the only mode the scheduler may run. `HEADLESS=false` (+ `xvfb-run` on Linux) exists for local debugging and future diagnostics only — never scheduled, never toggled from the dashboard. Building a dashboard headless/headed toggle is forbidden.
15. **[v3] Channels are plug-ins.** Every alert channel implements exactly `send(event)`. Dispatch fans out over `ALERT_CHANNELS` with per-channel try/except — one channel's failure never blocks another or the loop. External contributions (SMS) touch only `monitor/channels/sms_*.py`, its tests, and `.env.example`.

## Stack
Python 3.12 · fastapi · uvicorn · httpx · playwright (**Chrome channel**; Edge/Firefox in backlog) · sqlite3 stdlib · smtplib (Gmail app password) · python-dotenv · pytest · **pyotp** (Stage 6).

## Layout
```
monitor/check.py       # pulse + browser check → CheckResult(ok, layer, http_status, latency_ms, fail_reason)
monitor/journey.py     # Stage 6: login → TOTP → authed assertions → logout
monitor/session.py     # Stage 8: storageState save/load/expiry, login budget accounting
monitor/state.py       # pure: burst evaluation, confidence scoring, suppression
monitor/channels/      # [v3] Stage 4: base contract, email_gmail.py, sms_stub.py (colleague PR lands here)
monitor/db.py          # schema init + v3 migration (browser_mode column), append_check, state, incidents, query_range
monitor/web.py         # routes + basic auth (all but /healthz)
monitor/main.py        # composition: loop + uvicorn
config.py  tests/  data/  .env.example  requirements.txt  PROGRESS.md  LEARNING.md  CONTRIBUTING.md  # [v3]
```
*(`monitor/selfcheck.py` moves to the backlog with its stage.)*

## Behavior

### Check layers (cheapest → strongest evidence)
| Layer | Proves | Mechanism | Arrives |
|---|---|---|---|
| `pulse` | DNS/TCP/TLS/HTTP reachable | httpx GET | live |
| `render` | Login page renders | login-form locator visible | live |
| `auth` | Sign-in works | creds + TOTP → authed-only element visible AND error banner absent | Stage 6 |
| `data` | Core banking alive, not just auth | authed data-bearing element visible | Stage 9 |
| `api` | Backend answers, DOM-independent | `page.request.get(API_PROBE_PATH)` on the shared cookie jar → 200 + expected JSON key | Stage 9 |

Governing rule (Stage 9): DOM fails AND API fails → DOWN. DOM fails but API passes → DEGRADED ("page may have changed — verify, don't escalate"), never pages. API fails but DOM passes → DEGRADED, investigate probe config.

### States
`UP` · `DEGRADED` · `DOWN` (paging) · `CONFIG_ERROR` (bad creds / bot challenge / expired secret — a human, not an on-call). *(`BLIND` arrives with the self-health backlog item.)*

### fail_reason
`timeout | dns | conn_refused | bad_status:<code> | element_missing | nav_error | auth_rejected | auth_unavailable | mfa_failed | bot_challenge | rate_limited | session_expired | data_plane_missing | api_bad_status:<code> | api_shape_mismatch | logout_failed` **[v3: adds `rate_limited` (429/throttle signatures) — first-class for the stress experiment]**

### Confidence scoring (Stage 5) **[v3.1: updated decision rule and defaults]**
| Class | Reasons | Weight |
|---|---|---|
| Hard | `conn_refused`, `dns`, `bad_status:5xx`, `api_bad_status:5xx`, `auth_unavailable` | 2 |
| Soft | `timeout`, `element_missing`, `nav_error`, `data_plane_missing`, `api_shape_mismatch` | 1 |
| Config | `auth_rejected`, `bot_challenge`, `mfa_failed`, `rate_limited` | 0 → CONFIG_ERROR routing; never contributes to score or the probe floor |

**[v3.1]** DOWN requires BOTH: weighted score ≥ `DOWN_CONFIDENCE` (default **4**) AND at least `MIN_FAILED_PROBES` (default **3**) distinct failed probes within `BURST_WINDOW_S` (default **90**), with no intervening passing probe. A passing probe still clears the burst at any point (flap logged, no alert).

Outcome table (defaults: `DOWN_CONFIDENCE=4`, `MIN_FAILED_PROBES=3`, `BURST_DELAYS_S=0,15,35,55`):
| Sequence | Score | Probes failed | Pages? | Decision time | Email by |
|---|---|---|---|---|---|
| hard+hard+hard | 6 | 3 | yes | ~40s | <60s |
| hard+hard+soft | 5 | 3 | yes | ~40s | <60s |
| hard+soft+soft | 4 | 3 | yes | ~40s | <60s |
| soft+soft+soft | 3 | 3 | **no** — score short of 4, needs a 4th soft failure (score 4, 4 probes) | ~55–60s | ~70–75s |
| hard+hard (2 probes only) | 4 | 2 | **no** — probe floor not met even though score already hit 4 | — | — |
| any sequence with a passing probe | — | — | no — flap logged | — | — |

Nothing ever pages on fewer than 3 failed probes, regardless of score.

### Confirmation burst (Stage 5) **[v3.1: updated defaults and timing budget]**
On first failure, re-probe at `BURST_DELAYS_S` (default `0,15,35,55`) ± jitter, **varying the probe** (pulse → fresh browser context render → [post-Stage 6] journey). Diverse probes are independent evidence; identical retries can share one glitch. Pre-auth (Stages 5–7) bursts use pulse+render only. Post-Stage 8, bursts run on the cached session — zero logins.
Timing budget: **typical alert <60s** (hard-weighted evidence resolves on the 3rd failed probe, ~40s); **worst case ≤90s** (all-soft evidence needs a 4th failed probe, ~55–60s decision, email by ~70–75s). Delivery latency is measured, not assumed.

### The login-stress experiment [v3] (Stages 6–7 only)
Purpose: measure the platform's true tolerance before engineering around it. With `LOGIN_STRESS_MODE=true`: full sign-in every `CHECK_INTERVAL_S` (~1,440/day), each attempt logged to `login_events` (ts, ok, latency_ms, fail_reason, http_status). The monitor watches for limit signatures: `auth_rejected` after previously-good creds (lockout), `rate_limited`, `bot_challenge`, CAPTCHA appearance, or sustained login-latency degradation. First signature ⇒ experiment result: record exact time-to-limit and attempt count, send one `[MONITOR-CONFIG]` notice (never an outage page), halt logins cleanly per Rule 10. Dashboard gets a stress-report panel (attempts/hour, failure onset, latency trend) — exportable for the security-team soak report. Stress mode and its flag are **deleted at Stage 8**.

### Tables
- `checks(id, ts, ok, layer, browser, browser_mode, http_status, latency_ms, fail_reason, burst_id, degraded)` — index `(ts)`, `(burst_id)` **[v3: +browser_mode; migration: ALTER TABLE on existing DB, backfill 'headless']**
- `incidents(id, started_at, ended_at, duration_s, confidence, trigger_layer, screenshot_path)`
- `state(id=1, status, confidence, since_ts)`
- `login_events(id, ts, ok, latency_ms, reason, http_status, session_reused)` — Stage 6; the stress-experiment and login-budget audit trail
- `suppressions` — backlog (maintenance windows)

### Routes
`GET /` dashboard · `GET /api/status` · `GET /api/history?from&to&page` · `GET /api/export?from&to` (streamed CSV) · `GET /api/artifact/{incident_id}` · `GET /healthz` (no auth) · `GET /api/logins` (Stage 6: budget/stress status).

### Dashboard
UP/DEGRADED/DOWN banner (+ masked screenshot link), uptime % 24h/7d/30d, incidents table, paged+filterable check log, Download CSV, 30s auto-refresh, layer badges per check. **[v3]** Stress-report panel (Stage 6, removed with stress mode) → replaced by login-budget gauge (Stage 8). **[v3.1]** Burst probes are first-class rows in the check log — visually grouped/badged by `burst_id`, filterable, newest first; bursts are audit evidence and cannot be hidden from the UI. Plain HTML/JS.

### Timestamps **[v3.1]**
Storage stays UTC ISO-8601 in SQLite (Rule 6 unchanged) — this is the audit source of truth. All *presentation* — dashboard, email alert bodies, `/api` responses that feed the UI, CSV exports — renders in `America/New_York` via Python's `zoneinfo` (no new dependency), not a fixed UTC offset, so DST transitions (EST/EDT) are handled automatically. The CSV timestamp column is labeled `ts_eastern` and every value includes its UTC offset so auditors are never ambiguous about which instant a row records. If the human actually wants a fixed EST-year-round display instead of true Eastern civil time, that's a different, smaller change — confirm before assuming otherwise.

### Email copy
- `[MONITOR] {name} DOWN since {eastern_ts_with_offset} — {trigger_layer} failed (confidence {n}: {reasons}). {layer_hint} Dashboard: {url}` **[v3.1: since-time now Eastern with UTC offset, was UTC]**
- `[MONITOR] {name} RECOVERED after {duration}.`
- `[MONITOR-INFO] {name} DEGRADED — {reason}. Serving normally; verify before escalating.` (max once per 24h per distinct reason)
- `[MONITOR-CONFIG] {name} needs attention — {reason}. Checks paused.` (includes stress-experiment limit findings)

## .env
```
# --- core (live) ---
TARGET_NAME= TARGET_URL= REQUIRED_TEXT=            # or REQUIRED_ROLE + REQUIRED_NAME
CHECK_INTERVAL_S=60 BROWSER_TIMEOUT_MS=15000
RECIPIENTS_EMAIL= GMAIL_USER= GMAIL_APP_PASSWORD=
DASHBOARD_USER= DASHBOARD_PASSWORD= PORT=8080 DB_PATH=./data/monitor.db
BROWSER_CHANNEL=chrome HEADLESS=true               # [v3] Rule 14: headless only in the scheduler

# --- Stage 4: channels ---
ALERT_CHANNELS=email                                # colleague PR adds: email,sms
# SMS_* vars are defined by the SMS PR in .env.example; core never reads them directly

# --- Stage 5: fast detection --- # [v3.1] defaults updated: was 0,15,35 / 60 / 3
BURST_DELAYS_S=0,15,35,55 BURST_JITTER_S=5
BURST_WINDOW_S=90 DOWN_CONFIDENCE=4 MIN_FAILED_PROBES=3

# --- Stage 6: sign-in + MFA (TOTP confirmed) ---
LOGIN_URL= LOGIN_USER= LOGIN_PASSWORD=
MFA_ENABLED=true TOTP_SECRET=                       # captured at authenticator enrollment ("can't scan?")
AUTHED_REQUIRED_TEXT=                               # or AUTHED_REQUIRED_ROLE + AUTHED_REQUIRED_NAME
ERROR_BANNER_TEXT=                                  # must be ABSENT for auth to pass
MASK_TEXT=                                          # PII locators masked from screenshots
LOGIN_STRESS_MODE=false                             # [v3] Stages 6–7 only; deleted at Stage 8

# --- Stage 8: session & budget (values informed by the stress experiment) ---
LOGIN_INTERVAL_S=900 MAX_LOGINS_PER_DAY=150
SESSION_STATE_PATH=./data/session_state.json SESSION_MAX_AGE_S=1800

# --- Stage 9: data plane ---
DATA_PLANE_TEXT=                                    # or DATA_PLANE_ROLE + DATA_PLANE_NAME
API_PROBE_PATH= API_PROBE_JSON_KEY=
```
Validate at startup; fail fast listing what's missing (only for enabled stages). Secrets redacted from all validation output.

## Stages

**Sessions 1–3 — COMPLETE, signed off. Do not rebuild.**
1. ~~It checks.~~ ✅  2. ~~It alerts.~~ ✅  3. ~~You can see it.~~ ✅

---

**4. It's shareable.** GitHub-ready + channels seam + SMS contribution kit.
Refactor `alert.py` → `monitor/channels/` (base contract `send(event)`, `email_gmail.py` moved in unchanged, fan-out with per-channel try/except per Rule 15). Add `channels/sms_stub.py` demonstrating the contract + a mock-based test template. Write `CONTRIBUTING.md`: which files an SMS PR may touch (`channels/sms_*.py`, its tests, `.env.example` additions), test expectations, and the no-blocking rule. Hygiene gate before push: `.gitignore` covers `.env`/`data/`/session state; history scanned for secrets; no bank URLs or credentials in committed files or PROGRESS.md. DB migration: add `browser_mode` to `checks` (backfill `headless`).
*Accept:* all existing tests pass post-refactor; a deliberately-crashing fake channel doesn't stop email or the loop; fresh clone + `.env.example` boots; repo pushed; colleague can explain their task from CONTRIBUTING.md alone.

**5. It's fast.** Confirmation bursts + confidence scoring in `state.py` (pure, exhaustively tested), pre-auth probe set (pulse, fresh-context render). **[v3.1: acceptance criteria updated for the score-AND-probe-floor rule — see CLAUDE.md's amendment note before re-touching this stage's already-shipped code.]**
*Accept:* (a) a hard-mixed outage pages on exactly 3 failed probes within 60s; (b) an all-soft outage pages only after a 4th failed probe, email within 90s; (c) two failures then a pass never alerts; (d) no code path can declare DOWN with fewer than 3 failed probes — explicit unit test for hard+hard (score 4, only 2 probes: must NOT page until a 3rd fails); (e) config-class failures never count toward score or the probe floor; every Session-2 state test still passes.

**6. It signs in.** `journey.py`: login → TOTP via `pyotp` → authed-only element visible AND error banner absent → logout. New fail_reasons wired (`auth_rejected`, `auth_unavailable`, `mfa_failed`, `bot_challenge`, `rate_limited`, `logout_failed`). Screenshot masking live. `login_events` table + `/api/logins`. `LOGIN_STRESS_MODE` logic + dashboard stress panel built (off by default).
*Accept:* real sign-in against the security-team test site succeeds and logs out cleanly; wrong-password drill → `auth_rejected`, CONFIG alert, zero retries, logins halt; masked failure screenshot contains no PII; TOTP codes verified against the phone app's output.

**7. It ships to Azure and finds the limit.** Deploy to an **x86 Azure VM** (B2als_v2 or B2s): venv, `playwright install --with-deps chrome`, systemd unit, NTP verified, `.env` chmod 600, dashboard via Tailscale or SSH tunnel — port 8080 never public. Then the **stress soak** (2–3 days): `LOGIN_STRESS_MODE=true`, full sign-in every 60s until a limit signature appears or the soak window ends; alongside it, normal pulse/render checks + burst alerting run continuously.
*Accept:* monitor survives 48h+ unattended on the VM; stress report shows either a measured limit (time-to-lockout / rate-limit onset, exported for the security team) or "no limit found at 1,440/day"; zero false DOWN pages during the soak; any `bot_challenge` from the datacenter IP is logged as CONFIG, not paged.

**8. It doesn't get locked out.** `session.py`: `storageState` save/load, expiry detection, clean re-login on 401, budget accounting. Set `LOGIN_INTERVAL_S`/`MAX_LOGINS_PER_DAY` **from Stage 7's measured numbers** (with safety margin, e.g. ≤10% of the observed limit). **Delete stress mode**: flag, code path, and dashboard panel replaced by the login-budget gauge.
*Accept:* logins/hour falls to the configured budget while check cadence is unchanged; forced session expiry → exactly one clean re-login; deleting the state file recovers without a login stampede; `grep -r LOGIN_STRESS_MODE` returns nothing.

**9. It knows the bank is really up.** Data-plane assertion + `page.request` API corroboration on the shared cookie jar. Introduces `DEGRADED` per the governing rule.
*Accept:* rename a DOM label → DEGRADED, no page; break the API probe path → DOWN; both healthy → UP. A cosmetic markup change cannot page anyone.

---

## Ad hoc backlog (post-soak — pull in deliberately, one at a time, when its trigger fires)
| Item | What | Trigger to schedule it |
|---|---|---|
| Self-health + BLIND | `selfcheck.py`: control-URL probes, DNS/clock checks, BLIND state, dead-man heartbeat (healthchecks.io) | Before the monitor is trusted unattended in production |
| Maintenance windows | `suppressions` table + windowed alert suppression | First time a planned-maintenance page fires (or is announced) |
| Docker | Containerize (browsers baked, non-root, volume for data/, healthcheck) | When the deployment story moves past "one pet VM" |
| Cross-browser: Edge + Firefox | Secondary non-paging checks at a slow cadence; **no WebKit, ever** | Full solution stable; or a user reports browser-specific breakage |
| Diagnostic runner | `POST /api/diagnostic`: on-demand traced/videoed headless run, replayable from dashboard | When non-technical stakeholders start using the dashboard |

## Out of scope (do not build, even if asked casually — confirm first)
`journeys.json`, multi-target, voice alerts, AI judge, classifier module, traces on scheduled runs, retention pruning, CI, **any stealth/fingerprint-spoofing/anti-bot-evasion library (permanently forbidden — Rule 12)**, **dashboard headless/headed toggle (permanently forbidden — Rule 14)**, WebKit/Safari (permanently removed), any second process or background service.
*(SMS moved IN — Stage 4 seam, colleague's PR. Deploy moved IN — Stage 7.)*

## Standing reminder
A **supportability / code-optimization session** (maintainer-friendly structure, logging quality, onboarding the SMS colleague as a second maintainer) is deliberately deferred until after the Stage 7 soak. Claude Code: do not refactor toward it proactively. Human: raise it after the soak report is written.

## Appendix — MFA alternatives (not in use; kept for reference)
SMS OTP via a rented Twilio number (if TOTP were unavailable) · voice OTP via Twilio + transcription (last resort) · device-trust cookie reuse (subsumed by Stage 8 storageState) · organizational MFA exemption scoped to the VM's static IP (worth requesting if stress results argue for it).
