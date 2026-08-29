# CLAUDE.md — Monitor Lite

## Purpose

A monitor that answers ONE question every 60 seconds: **is the online banking platform up for a customer?**

**UP** = the authenticated area behind login returns HTTP 200 **and renders the expected content.**

The public pulse and login-page render checks are *precursor* evidence for that same question — not a separate product. This replaces a Selenium tool that false-positived on DOM changes, so **low false positives outrank everything else**, and no alert ever fires on fewer than 3 failed probes (executive commitment).

Results go to SQLite (audit-grade — every probe writes a row, pass or fail). Email alerts fire on state transitions only. A FastAPI dashboard reads the same data.

> History, amendment logs, live-drill notes and per-session decisions live in **PROGRESS.md**. This file is the spec: what the system does now, why, and what it must never do. Keep it that way — do not append changelogs here.

## What this is not

- Not a data-plane or API monitor. UP is *authed 200 + rendered content*, nothing deeper. No balance checks, no transaction probes, no JSON shape assertions.
- Not multi-target. One platform, one verdict.
- Not a bot-challenge defeater. Challenges are detected and reported, never solved.
- Not a retry engine. A rejected credential is never retried, ever.

## Stack

Python 3.12 · fastapi · uvicorn · curl_cffi (pulse) · patchright (browser journey, playwright-style API) · playwright (pulse-track render check) · sqlite3 · smtplib · python-dotenv · pytest · pyotp

Single asyncio process. No ORM, no queues, no second process, no framework beyond FastAPI. `httpx` was retired after a TLS-fingerprint 403 incident — do not reintroduce it for the pulse probe.

## Code map

| File | Responsibility |
|---|---|
| `config.py` | Loads `.env`, validates at startup, holds every tunable. Fails fast. |
| `monitor/main.py` | Scheduler, cycle orchestration, bursts, login budget, self-health net. The composition root. |
| `monitor/check.py` | `pulse` + `render` probes (curl_cffi + Playwright). Returns `CheckResult`, no I/O. |
| `monitor/journey.py` | Sign-in journey and the cheap authed check (patchright). Locators, TOTP, `classify_*` steps. |
| `monitor/session.py` | `storageState` save + freshness/validity check. |
| `monitor/state.py` | **Pure** state machine: burst scoring, floors, suppression. No I/O. |
| `monitor/verdict.py` | Severity ladder, `worst_of()`, the locked operator wording. Pure. |
| `monitor/db.py` | Schema, additive migrations, queries, streaming export. |
| `monitor/web/app.py` | FastAPI routes + static dashboard. |
| `monitor/channels/` | Alert plug-ins (`email`, `sms`, `sms_gateway`). |
| `scripts/` | Manual drills: sign-in, live cycle, DOM dump, TOTP check, CONFIG_ERROR clear. |

## How a cycle works

`cycle_scheduler()` fires every `CHECK_INTERVAL_S` (60s). Each tick:

1. **Overlap guard.** If the previous cycle (or a burst it started) still holds the lock, this tick is **skipped and logged**. Bursts run *inline*, so a confirming burst deliberately eats the next tick — reliability over a strict 60s cadence.
2. **Main track** runs `perform_check()`: pulse first, render only if the pulse passed.
3. If that probe **starts a new burst**, the rest of the burst runs inline right here (see [The DOWN algorithm](#the-down-algorithm)).
4. **Auth track** runs its cheap authed check, its own burst, and at most one budgeted recovery login.
5. Both tracks are combined into **one `cycles` row** and one platform verdict.
6. Every individual probe — burst re-probes included — has already written its own `checks` row, linked by `cycle_id`.

The whole cycle runs inside `guarded_cycle()`, which catches everything (see [Self-health](#self-health-degraded--internal_error)).

## The three layers

Cheapest → strongest. Each proves strictly more than the one above it.

| Layer | Proves | Mechanism | Track |
|---|---|---|---|
| `pulse` | reachable (DNS/TCP/TLS/HTTP) | `curl_cffi` GET impersonating Chrome's TLS/HTTP2 fingerprint | main |
| `render` | the login page renders | headless Chromium; `REQUIRED_TEXT` or `REQUIRED_ROLE`+`REQUIRED_NAME` locator visible | main |
| `authed` | **the platform is up — the definition of UP** | patchright, headed under xvfb; navigates directly to `AUTHED_URL` with a reused session, asserts `AUTHED_REQUIRED_*` visible and the error banner absent | auth |

### What each layer emits on failure

**`pulse`** — `dns` · `conn_refused` · `timeout` · `bad_status:<code>` (any status ≥ 400) · `nav_error` (other request errors). A pulse failure short-circuits the cycle's main probe; render is not attempted.

**`render`** — `nav_error` (`goto` timed out or errored) · `element_missing` (locator never became visible).

**`authed`** (`run_authed_check`) — in evaluation order:
- `nav_error` — `goto` raised (network-level failure).
- `bad_status:<code>` — the response status is ≥ 400. Playwright does **not** raise on HTTP error status, so this is read explicitly off the `Response`. A 5xx here is Hard evidence and is emphatically *not* a session problem.
- `session_expired` — the marker is missing **and** `bounced_to_login()` is true: we were redirected *away* from the authed route **and** a login form is present. Both conditions are required; a redirect to a maintenance or error page is platform evidence, not a session problem.
- `element_missing` — the marker is missing but we are still on the authed route. This is the platform serving something wrong. **This is the path that makes "online banking behind login not rendering" reachable.**

`AUTHED_URL` is navigated **directly** — configured, never derived from `LOGIN_URL`. The login route and the authed-home route have different dependencies (auth service / MFA / login UI / Cloudflare vs. plain session-cookie validation), so they are independent evidence.

A **full login** (`run_journey`: credentials → MFA → authed assertion → optional logout) is only ever the budgeted recovery path. It emits `nav_error`/`element_missing` at `layer="render"` before credentials are submitted, and `auth_rejected` · `bot_challenge` · `mfa_failed` · `element_missing` · `logout_failed` at `layer="authed"` after.

## The two tracks

`main` (pulse + render) and `auth` (authed) are **separate state machines** with separate rows in `state` and separate incidents. They must stay separate: a shared status previously let an auth success falsely recover a site outage.

Unification happens only at presentation and alerting.

## Verdict

`platform_status = worst_of(main, auth)`, on the severity ladder:

```
UP (0)  <  DEGRADED (1)  <  CONFIG_ERROR (2)  <  DOWN (3)
```

`auth_status = None` (track not configured) means the main track alone decides.

The dashboard banner, `/api/status` and alert subjects all speak platform-level, and always name the failing layer:

- precursor failure → **"login screen unreachable / not rendering"**
- authed failure → **"online banking behind login not rendering"**

The operator must never need a browser to know which wall failed. The wording lives in `monitor/verdict.py` and nowhere else. `fail_layer='monitor'` deliberately returns `None` — our own bug never gets operator phrasing.

## Failure classification

### `fail_reason` — the closed probe taxonomy

```
timeout | dns | conn_refused | bad_status:<code> | element_missing | nav_error |
auth_rejected | auth_unavailable | mfa_failed | bot_challenge | rate_limited |
session_expired | logout_failed
```

There is no `"unknown"` fail_reason. `internal_error` is **not** in this list — it describes the monitor, not the platform (see [Self-health](#self-health-degraded--internal_error)).

Three members cannot be produced by the running monitor, for **two different reasons** that should not be conflated:

- `auth_unavailable`† and `rate_limited`† — **no code anywhere emits them.** Dead vocabulary; closing this needs code (B20).
- `logout_failed`‡ — **real code emits it, but only a drill script can reach that code.** `run_journey()` returns it when logout fails, and the scheduler's only caller passes `should_logout=False` deliberately: a recovery login must keep the session alive for reuse (Rule 5's zero-login burst depends on that session). Only `scripts/run_signin_drill.py`, without `--keep-session`, can produce it. Closing this needs a decision, not code — either it stays drill-only (it is, and is now labelled) or something must start logging out periodically, which nothing wants.

See [Known limitations](#known-limitations) #3.

Layers are `pulse | render | authed`. `monitor` is a `cycles.fail_layer` value only. Note the spelling split: **`authed`** is the layer, **`auth`** is the track.

### Confidence weights (identical on both tracks)

| Class | Reasons | Weight | Effect |
|---|---|---|---|
| Hard | `conn_refused`, `dns`, `bad_status:5xx`, `auth_unavailable`† | 2 | scores toward DOWN |
| Soft | `timeout`, `element_missing`, `nav_error`, non-5xx `bad_status`, `logout_failed`‡, **anything unrecognized** | 1 | scores toward DOWN |
| Config | `auth_rejected`, `bot_challenge`, `mfa_failed`, `rate_limited`† | 0 | → CONFIG_ERROR, halts logins |
| Session | `session_expired` | 0 | → recovery-login path, **never scores** |

Unrecognized reasons fail *safe as Soft* — ambiguous evidence stays cautious rather than paging.

† Never actually emitted by any probe today — see [Known limitations](#known-limitations) #3 (tracked as B20).

‡ Reachable only from `scripts/`, never from the scheduler — see the taxonomy note above (tracked as B21). Listed here explicitly rather than relying on the "anything unrecognized" fallback: it scored Soft either way, but a reason absent from this table reads as one the ladder forgot rather than one deliberately placed.

### Unexpected browser errors

No `page.*` call may raise out of a probe. Anything unforeseen resolves through `journey.unexpected_fail_reason()`: timeout-shaped → `timeout`, everything else → `nav_error`. Both Soft. See [Known limitations](#known-limitations) for the residual risk this leaves.

## The DOWN algorithm

**DOWN = accumulated score ≥ `DOWN_CONFIDENCE` (4) AND ≥ `MIN_FAILED_PROBES` (3) distinct failed probes within a `BURST_WINDOW_S` (90s) window, with no intervening pass.**

Both tracks use the same numbers (`AUTH_DOWN_CONFIDENCE=4`, `AUTH_MIN_FAILED_PROBES=3`).

### State transition, precisely

Per failing probe, in `state.apply_check()`:

1. `session_expired` → **return unchanged**. No score, no burst, no event, on any track, in any status.
2. Config-class reason → `CONFIG_ERROR` + one alert (or nothing, if already CONFIG_ERROR).
3. Already `CONFIG_ERROR` → ignored. Only a human (or a passing probe) clears it.
4. Already `DOWN` → keep tallying evidence for the incident record; never re-alert.
5. Otherwise (status `UP`):
   - Burst is **active** if `burst_started_ts` is set and `now - burst_started_ts ≤ BURST_WINDOW_S`.
   - Active → `confidence += weight`, append the reason.
   - Not active → start a fresh burst: `confidence = weight`, `fail_reasons = (reason,)`, `burst_started_ts = now`.
   - If `confidence ≥ threshold` **and** `len(fail_reasons) ≥ floor` **and** not suppressed → **DOWN**, emit one `DownEvent`.

**Any passing probe** returns the track to `UP` with a clean burst. Mid-burst that is a flap: logged in `checks`, no alert. From `DOWN`/`CONFIG_ERROR` it emits one `RecoveryEvent`.

### Bursts

A burst launches only when a probe *starts a new burst* (not when it extends one). Re-probes run inline at `BURST_DELAYS_S` offsets `0, 15, 35, 55` ± `BURST_JITTER_S` (5s), measured **from burst start**, not per-iteration — so DB writes and alert dispatch don't accumulate drift. Offset 0 is the initiating probe; the loop covers the rest and **stops the moment the burst resolves** (DOWN fired, or a pass cleared it).

- **Main track** alternates the probe kind — `render`, `pulse`, `render` — so each is independent evidence rather than a correlated retry.
- **Auth track** re-probes with `run_authed_check()` only. **A burst therefore consumes zero logins.**
- Every burst probe carries `burst_id` and `cycle_id`.

### Worked examples

| Scenario | Probes | Score | Outcome |
|---|---|---|---|
| 3 hard failures (e.g. `dns`) at 0/15/35s | 3 | 6 | **DOWN** at ≈35s into the burst |
| 4 soft failures (e.g. `element_missing`) at 0/15/35/55s | 4 | 4 | **DOWN** at ≈55s into the burst |
| 2 hard failures, then the window closes | 2 | 4 | **not DOWN** — floor of 3 not met |
| 2 failures then a pass | — | — | burst cleared, logged as a flap, **no alert** |
| Any single failure | 1 | ≤2 | **never** pages |

Add probe execution time on top of the offsets; email lands well inside a minute for hard-heavy bursts, 70–90s for all-soft.

## Special routing

### `session_expired`

A cached-session probe failing this way speaks to the session, not the bank. It **never** scores and **never** clears a burst — it is completely inert. It routes to the recovery login, and the recovery's own outcome is the evidence:

- success → self-healed non-event
- `bad_status:5xx` → hard platform evidence (the spec's `auth_unavailable` is never emitted today — limitation #3)
- `auth_rejected` → CONFIG_ERROR

If the session is broken, burst probes each fail fast on the cheap path. That fast-fail is correct evidence collection and still costs zero logins.

### CONFIG_ERROR

Latches. Alerts once, never re-alerts, and never pages (it is not an outage). While the **auth** track is CONFIG_ERROR it is skipped entirely each cycle, so it cannot self-clear — clear it with `python -m scripts.clear_config_error --track auth`, or by a passing manual drill run. The **main** track is never skipped, so it self-clears on the next passing probe.

### Self-health (`DEGRADED` / `internal_error`)

The monitor is allowed to fail, but never silently. **No minute may vanish** — the cycle runner is fire-and-forget, so an unhandled exception once surfaced only as asyncio's "Task exception was never retrieved": no rows, no state advance, no alert, and a quiet dashboard reads as "the platform is fine".

Every crashed cycle therefore still writes a `cycles` row — `verdict='DEGRADED'`, `fail_layer='monitor'`, `fail_reason='internal_error'` — under the **same `cycle_id`** its probe rows already carry. That marker write is itself guarded: if the DB is what broke, it prints CRITICAL rather than raising.

The row is written **directly, never through `apply_check()`**, so it cannot touch either track's state, confidence or probe floor. `DEGRADED` is chosen precisely because it never pages — manufacturing a DOWN out of our own defect would create exactly the false positive this project ranks above everything else. The only other place `internal_error` appears is `login_events`, when a login attempt produced no result at all.

## Cross-track suppression

The cheap authed check runs **every cycle regardless of the main track's status** — a login-route outage can leave existing sessions served for roughly one session lifetime, and that distinction belongs in the incident record.

What is suppressed: while a **main-track DOWN incident is open**, an auth-track failure that would cross into its own DOWN is held back — logged and annotated onto the open main incident rather than opening a second incident or paging again. If the login screen is down, the alert already covers the symptom.

- Auth-track DOWN can only *open* while the precursor is passing.
- Suppressed evidence is never discarded — confidence and reasons keep accumulating, and it fires on the next unsuppressed failing probe once the precursor recovers.
- Recovery logins are **paused** while the precursor is DOWN — no budget spent chasing a possibly-down site.
- Auth-track recovery never closes a main-track incident, and vice versa.
- `cycles.authed_ok` is a real True/False whenever the authed check actually contacted the platform, **including while suppressed**. It is NULL only when nothing looked at all: track unconfigured, track in its own CONFIG_ERROR, or no usable session *and* the recovery login was paused or refused by the budget.

## Login budget

`LOGIN_INTERVAL_S` is the **single** login rate limit: a minimum gap since the last *attempt*, success or failure alike, read from the `login_events` ledger.

- Cheap session-reuse probes are **not** logins and are exempt.
- At most **one** budgeted recovery login per cycle, outside the burst's scoring.
- Every attempt is ledgered **on the failure path too** (`try/finally`). Load-bearing: the budget is derived from that ledger, so an unrecorded attempt let a crashing login loop every 60s, unbounded, against a real account and invisible in the audit trail.
- `config.py` refuses to start below a **60s floor** — a cooldown shorter than one cycle limits nothing.
- Keep `LOGIN_INTERVAL_S` well under `SESSION_MAX_AGE_S` and **never equal**. The session clock starts when the file is *written*, the login clock when the attempt is *recorded*; equal values open a dead window as wide as the gap between them. Observed live: a 300s teardown stall at 600/600 produced five minutes of false `session_expired` on a healthy session.
- Achievable rate is `ceil((LOGIN_INTERVAL_S + login_duration) / CHECK_INTERVAL_S) × CHECK_INTERVAL_S` — **not** `3600/LOGIN_INTERVAL_S`. Attempts only start on cycle boundaries.
- There is deliberately **no daily cap**. Exhausting one was a dead end until UTC midnight: the track stopped attempting logins while `session_expired` kept scoring 0, so the platform read UP on no authed evidence and nothing alerted. Any future total ceiling **must** raise CONFIG_ERROR on exhaustion so it is loud.

## Data model

- **`cycles`** — exactly one row per cycle, the primary audit export:
  `(cycle_id, ts, pulse_ok, render_ok, authed_ok, session_reused, pulse_latency_ms, render_latency_ms, authed_latency_ms, verdict, fail_layer, fail_reason, burst_id)`. Indexed on `ts`.
  `burst_id` marks the minute as burst-confirmed; when both tracks bursted it carries the one belonging to the track that explains the verdict (the other's probes are still tagged in `checks`).
- **`checks`** — one row per probe: `ts, ok, http_status, latency_ms, fail_reason, browser_mode, layer, burst_id, cycle_id`. Probe-level evidence, bursts included.
- **`incidents`** — `started_at, ended_at, duration_s, checks_failed, confidence, trigger_layer, screenshot_path, track`.
- **`state`** — one row per track: `status, since_ts, burst_started_ts, confidence, fail_reasons`.
- **`login_events`** — every login attempt and its outcome, including recovery logins.

Timestamps stored **UTC ISO-8601**; presented **America/New_York** everywhere (dashboard, CSV, email) via `monitor/timeutil.py`. Migrations are additive only; no backfill.

Connections open with `PRAGMA journal_mode=WAL` and `busy_timeout=15000` — rollback-journal mode let a long export read make the cycle's write raise `database is locked`.

`uptime_pct` counts only cycles that produced platform evidence: `verdict IN ('UP','DOWN')`. CONFIG_ERROR and DEGRADED are excluded from **both** sides, so a latched CONFIG_ERROR reports "n/a" rather than 0% uptime for a platform that is up every minute. A window with only excluded verdicts honestly reports "n/a", not 100%.

## Routes & dashboard

- `/healthz` — **the only unauthenticated route.** 503 on an unreadable DB or a stale last check (> 3 × `CHECK_INTERVAL_S`). Deliberately does **not** call `init_db`: the schema owner is the monitor process, and a health probe creating tables would hide the misconfiguration it reports.
- `/api/status` — platform verdict, failing layer + wording, last cycle, uptime, recent incidents.
- `/api/history` — the cycles view, paginated · `/api/cycle/{cycle_id}` — probe-level drill-down.
- `/api/export` — `table=cycles|checks|all`. Single tables stream off a live cursor at flat memory; `table=all` zips both member-by-member behind a 200k-row-per-table cap that **413s with guidance rather than truncating**.
- `/api/artifact/{incident_id}` · `/` · `/static/{filename}`.

`docs_url`/`redoc_url`/`openapi_url` are disabled, and `require_auth` **fails closed** on unset credentials (`compare_digest("","")` is `True`, so blank config once admitted `curl -u ":"`).

Dashboard log is one row per minute: three layer badges (pulse / render / authed), verdict, session-reused marker, burst badge when applicable. Failed cycles expand to their probes. Bursts are first-class and cannot be hidden. Only DOWN wears alarm red; CONFIG_ERROR and DEGRADED are gold.

## Email copy (locked wording)

```
[MONITOR] {name} DOWN since {eastern} — login screen unreachable / not rendering (confidence {n}: {reasons}). Dashboard: {url}
[MONITOR] {name} DOWN since {eastern} — online banking behind login not rendering (confidence {n}: {reasons}). Dashboard: {url}
[MONITOR] {name} RECOVERED after {duration}.
[MONITOR-CONFIG] {name} needs attention — {reason}. Sign-in checks paused.
```

One DOWN email and one RECOVERY email per incident, ever.

## `.env`

**Core (required):** `TARGET_NAME`, `TARGET_URL`, `REQUIRED_TEXT` *or* `REQUIRED_ROLE`+`REQUIRED_NAME`, `DASHBOARD_USER`, `DASHBOARD_PASSWORD`.

**Auth track (required together, or the track never runs):** `LOGIN_URL`, `AUTHED_URL`, `LOGIN_USER`, `LOGIN_PASSWORD`, `ERROR_BANNER_TEXT`, `AUTHED_REQUIRED_TEXT` *or* `AUTHED_REQUIRED_ROLE`+`AUTHED_REQUIRED_NAME`, `TOTP_SECRET` (unless `ALLOW_MFA_UNCONFIGURED=true`, testing only).

**Timing:** `CHECK_INTERVAL_S=60`, `BROWSER_TIMEOUT_MS=15000`, `CHALLENGE_TIMEOUT_MS=25000`.

**Detection:** `BURST_DELAYS_S=0,15,35,55`, `BURST_JITTER_S=5`, `BURST_WINDOW_S=90`, `DOWN_CONFIDENCE=4`, `MIN_FAILED_PROBES=3`, `AUTH_DOWN_CONFIDENCE=4`, `AUTH_MIN_FAILED_PROBES=3`.

**Session & budget:** `SESSION_STATE_PATH`, `SESSION_MAX_AGE_S=600`, `LOGIN_INTERVAL_S=120` (floor 60; never equal to `SESSION_MAX_AGE_S`).

**Screenshots:** `MASK_TEXT` (semicolon-separated regexes), `MASKING_ENABLED=true`.

**Alerts:** `ALERT_CHANNELS`, `RECIPIENTS_EMAIL`, `GMAIL_USER`, `GMAIL_APP_PASSWORD`, plus Twilio/gateway settings.

Eastern-time presentation is handled in code, not configured. `.env.example` carries the worked examples — keep it in sync when a value moves.

## Rules (non-negotiable)

1. **`state.py` stays pure** — no I/O, fully unit-tested: burst evaluation, confidence scoring, floors, suppression.
2. **Alert only on transitions.** DOWN requires *both* score ≥ threshold *and* ≥ 3 distinct failed probes in the window with no intervening pass — on both tracks. One DOWN + one RECOVERY email per incident, ever.
3. **`session_expired` never scores.** It routes to the recovery-login path and is recorded, but it cannot contribute to DOWN confidence or the probe floor on any track.
4. **Never retry a credential rejection.** `auth_rejected` → CONFIG_ERROR, logins halt until a human clears it. Always, everywhere.
5. **The login budget is a hard limit.** Burst re-probes on the auth track MUST use `run_authed_check()` — a burst consumes zero logins. Every attempt is ledgered, including failures.
6. **Bot challenges: detect, never defeat.** `bot_challenge` → CONFIG_ERROR, never pages. patchright is the one approved, scoped mitigation.
7. **Only DOWN pages.** `DEGRADED` and `CONFIG_ERROR` never do.
8. **A monitor bug is never evidence about the bank.** Self-health rows bypass `apply_check()` entirely; `internal_error` never reaches `classify()` and is never returned by a probe.
9. **No minute may vanish.** Every cycle writes a `cycles` row even when it crashes.
10. **Every DOWN names its layer** with the exact wording in [Verdict](#verdict), read from `monitor/verdict.py`.
11. **Locators:** `get_by_role` / `get_by_text` / `get_by_label` only. Scoped exception: `.nth()` on the username and password fields only. Use `.first` on any locator built from free-form `.env` text — Playwright strict mode raises `Error` (not `AssertionError`) on multiple matches, and no `classify_*` helper catches that.
12. **Never guess a locator name from a screenshot.** Run `scripts/dump_dom.py`, read `elements.txt`, cross-check the accessible name's source in `page.html`. Guessing is what produced the MFA misclassification bug.
13. **Screenshots on failure only**, and authed screenshots are always masked.
14. **Headless is the scheduler's mode** for pulse and render. Scoped exception: the sign-in journey and authed check run headed under xvfb, recorded as `browser_mode='headed-xvfb'`. A dashboard headless/headed toggle is forbidden.
15. **Every probe writes a `checks` row; every cycle writes a `cycles` row.** CSV export stays row-for-row faithful to both tables — refuse rather than truncate.
16. **Secrets from `.env` only.** `chmod 600` on `.env` and session state. Parameterized SQL. Credentials and TOTP are never logged, rendered, or screenshotted.
17. **Single asyncio process** with overlap guards. No frameworks, ORM, queues, or second process.
18. **Alert channels are plug-ins:** `send(event)`, per-channel try/except — one broken channel never blocks another or the cycle loop.

## Known limitations

Open and deliberate. Read this before diagnosing a bug — several "bugs" are already on this list.

1. **False-UP blind spot on the authed marker (iframe scope).** The live target's banking content lives inside a separate `nxg-olb` iframe; `authed_marker()` builds **page-level locators only**, and `get_by_text` does not pierce iframes. The configured marker is outer-shell chrome, so if the iframe fails to render — which is literally "online banking behind login not rendering" — the check still reports **UP**. This contradicts the definition of UP and is a silent false negative. Fix needs `frame_locator` support in `journey.py` plus a frame-URL setting.
2. **Three unverified `classify_*` fallback mappings.** `classify_after_submit` → `bot_challenge` (this one already fired wrongly on a live MFA screen and parked the auth track in CONFIG_ERROR); `classify_after_totp` → `mfa_failed`; `classify_authed` with marker *and* error banner both visible → `element_missing`. The MFA **locators** are now confirmed against captured DOM; these **fallbacks** are not.
3. **The authed layer's evidence vocabulary is narrower than this spec claims.** `auth_unavailable` and `rate_limited` exist only in `state.py`'s classification sets — nothing emits them; `classify_after_submit` lumps "bot challenge / auth service down / rate limited" into `bot_challenge`, which is Config-class and **never pages**, so a genuinely unavailable auth service reads as a config problem. `logout_failed`‡ is only reachable from the drill script (the scheduler always runs `should_logout=False`) — a different case from the other two, and marked separately in the taxonomy: the code exists and works, only the scheduler never calls it. Worse, `run_authed_check()` collapses *every* navigation exception to `nav_error`, so on the `authed` layer `dns` and `conn_refused` are unreachable too — its only Hard evidence is `bad_status:5xx`, and a hard network failure there scores Soft. Tracked as **B20/B21** in `personal/ISSUES.md`.
4. **`element_missing` conflates absence with ambiguity.** A locator matching 2+ elements fails `to_be_visible` identically to one matching 0. `.first` guards the known free-text locators; anything else raises and lands on `nav_error` via `unexpected_fail_reason` — Soft, and therefore scoring.
5. **A *persistent* monitor-side defect can page.** Unforeseen browser errors map to Soft reasons, and four Soft probes reach `DOWN_CONFIDENCE`. The 3-probe floor stops a single flake, not a systematic bug. Closing this needs a distinct non-scoring monitor-error class in the taxonomy — a spec change, not a code tweak. Do not assume it is already handled.
6. **Masking cannot redact `<input>` values.** `get_by_text` matches text nodes, so a filled login-ID field appears in any post-submit failure screenshot. A `MASK_TEXT` pattern that matches nothing fails *open* (no mask, no error). An empty `MASK_TEXT` under `MASKING_ENABLED=true` warns loudly but does not block. The current target is a security-team test site with fake data; this must be populated before pointing at a real account.
7. **TOTP is not yet captured.** The live step-up offers SMS / call / authenticator. Without a real `TOTP_SECRET`, any recovery login reports `mfa_failed` → CONFIG_ERROR. The code-entry screen also carries an unhandled device-registration choice ("register my private device" / "this is a public device") that `submit_totp` ignores — may matter for unattended logins.
8. **The auth track's CONFIG_ERROR cannot self-clear** (the track is skipped while latched). No dashboard or API control exists — use `scripts/clear_config_error.py` or a passing drill.
9. **`SESSION_STATE_PATH` defaults inside the synced repo tree**, and `os.chmod(0o600)` is a silent no-op on Windows despite `session.py` asserting it.
10. **`.env.example` declares `LOGIN_STRESS_MODE`, which `config.py` deliberately does not read.** Reserved for a future sanctioned stress window; it is not implemented.
11. **`db.count_login_events_since()` has no callers** — it was the daily-cap query, and was deliberately kept because the deferred `/api/logins` budget gauge is exactly that query. Wire it or drop it; don't leave it parked (**B22**).

## Out of scope

Confirm before building any of these:

Data-plane / API probes · `journeys.json` · multi-target · voice alerts · AI judge · retention pruning · CI · stealth libraries beyond the scoped patchright exception · dashboard mode toggle · WebKit/Safari · second process.

Backlog, not scope: a distinct non-paging `BLIND` status · maintenance windows · Docker · cross-browser Edge+Firefox · diagnostic runner · `/api/logins` route + budget gauge · a CONFIG_ERROR clear/retry control in the UI.

## How we work

Stages in order, acceptance test + human sign-off before the next. Explain before building. Update **PROGRESS.md** after every session. Stop and ask before adding a dependency or service. `LEARNING.md` on request.

**Where things stand:** the pulse/render monitor, the sign-in journey, session reuse, the unified one-verdict cycle and the hardening pass are all built; 117 tests pass. What remains is live verification of the hardening work, the Stage 6 closeout drills (wrong-password → `auth_rejected`, masked-screenshot review, TOTP cross-check), and the Azure deploy + multi-day soak. A soak is not clean if it contains any unexplained `DEGRADED`/`internal_error` cycle — Rule 9 exists to make those visible.
