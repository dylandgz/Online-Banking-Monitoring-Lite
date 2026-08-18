# Monitor Lite

A deliberately small uptime monitor for a banking web platform. It replaces a Selenium
tool that false-positived on minor DOM changes — **low false positives outrank every
other concern, including speed.**

One platform, one verdict, every 60 seconds: a TLS-fingerprint-aware HTTP pulse check, a
headless Playwright render check against the public login page, and a cheap authenticated
check (session-cookie reuse, no fresh login) proving the platform is reachable *past*
login — not just that the login page loads. Results are written to SQLite, every probe
pass or fail — the log is an audit artifact. State transitions (and only transitions)
trigger an email alert. A FastAPI dashboard shows the unified verdict, uptime %,
incidents, a filterable cycle log (one row per minute, expandable to individual probes),
and CSV export.

Full project rules, stage plan, and design rationale live in [CLAUDE.md](CLAUDE.md) — this
README is the quick-start; that file is the spec.

## Status

Sessions 1–3, Stage 4 (shareable/channels), Stage 5 (fast detection), Stage 6 (sign-in
journey — live end-to-end against the real test site), Stage 8's session-reuse mechanism,
and Stage R (the "one platform, one verdict" realignment: unified main+auth verdict,
per-minute cycles, cross-track alert suppression) are all built and live-tested against
the real target. See [PROGRESS.md](PROGRESS.md) for the full build log and decisions made
along the way, and CLAUDE.md's Stages section for what's still open (Stage 7's VM deploy
+ stress soak, a captured `TOTP_SECRET`, and a few smaller deferred items).

## Requirements

- Python 3.12
- Real Chrome installed for [Playwright](https://playwright.dev/python/)/[patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright-python)
  (`playwright install chrome`) — the sign-in journey specifically needs branded Chrome,
  not the bundled Chromium, to reliably clear Cloudflare's bot challenge

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chrome

cp .env.example .env
# fill in .env: TARGET_URL, REQUIRED_TEXT (or REQUIRED_ROLE+REQUIRED_NAME),
# DASHBOARD_USER/PASSWORD are required at minimum. Email alerting needs
# GMAIL_USER/GMAIL_APP_PASSWORD/RECIPIENTS_EMAIL — the loop runs fine without them,
# it just logs and skips sending. SMS (Twilio) needs TWILIO_ACCOUNT_SID/
# TWILIO_AUTH_TOKEN/TWILIO_FROM_NUMBER/SMS_TO_NUMBER plus ALERT_CHANNELS=email,sms.
# Fill in .env. See the file's own comments -- it's organized CORE (required to start)
# -> PULSE+RENDER (the public login-page check) -> AUTH (the sign-in journey + session
# reuse) -> ALERTING -> DEAD (safe to ignore/delete). At minimum: TARGET_URL,
# REQUIRED_TEXT (or REQUIRED_ROLE+REQUIRED_NAME), DASHBOARD_USER/PASSWORD. Email alerting
# needs GMAIL_USER/GMAIL_APP_PASSWORD/RECIPIENTS_EMAIL -- the loop runs fine without them,
# it just logs and skips sending. The sign-in journey needs its own block filled in too
# (LOGIN_URL, AUTHED_URL, LOGIN_USER/PASSWORD, TOTP_SECRET, ...) -- see "Sign-in journey /
# session cookies" below if you don't have a captured TOTP_SECRET yet.
```

## Running

```bash
python -m monitor.main
```

This starts both the unified check loop (pulse + render + auth) and the dashboard
(single process, per the project's "no second process" rule). Dashboard:
`http://localhost:8080` (basic auth via `DASHBOARD_USER`/`DASHBOARD_PASSWORD`).
`/healthz` is unauthenticated.

## Sign-in journey / session cookies

The authenticated check reuses a saved browser session (`SESSION_STATE_PATH`) instead of
logging in fresh every minute — a real, credentialed login only happens occasionally, to
refresh that session, and normally that's fully automated via a captured `TOTP_SECRET`
(an authenticator-app secret that generates fresh MFA codes by itself).

**If you don't have a `TOTP_SECRET` captured yet**, the automated loop can't complete MFA
on its own. As a temporary stand-in, set `ALLOW_MFA_UNCONFIGURED=true` in `.env` (lets the
auth track start and reuse a session normally; a fresh login it can't complete just
reports `mfa_failed` → `CONFIG_ERROR` cleanly instead of crashing or blocking startup),
and capture sessions by hand with:

```bash
python -m scripts.run_signin_drill --keep-session
```

This opens a real, visible browser, submits your credentials, then pauses for you to
complete MFA yourself (phone call, authenticator app, whatever factor you have) before
saving the session. Run it again any time the dashboard shows the auth track stuck in
`CONFIG_ERROR` — a successful run automatically clears that state, and the next cycle
picks the fresh session back up. This is temporary scaffolding, not a permanent
workaround: once a real `TOTP_SECRET` is captured, set `ALLOW_MFA_UNCONFIGURED=false` and
the loop no longer needs a human at all.

## How a DOWN alert gets decided

Every failing probe is weighted by how strong its evidence is:

| Class | Examples | Weight |
|---|---|---|
| Hard | `conn_refused`, `dns`, `bad_status:5xx`, `auth_unavailable` | 2 |
| Soft | `timeout`, `element_missing`, `nav_error` | 1 |
| Config | `auth_rejected`, `bot_challenge`, `mfa_failed`, `rate_limited` | 0 — routes to `CONFIG_ERROR`, never DOWN |
| Session | `session_expired` | 0 — never platform evidence, triggers a budgeted recovery login instead |

There are two independent status tracks — **main** (pulse/render, "is the login page
reachable") and **auth** (the authed check, "can we actually get past login") — each
running the same confirmation-burst rule separately. On the first failure, that track
re-probes a few more times (varying the probe so one shared glitch can't fake
corroboration). **DOWN fires only when both** are true within the burst window:

- the weighted score reaches that track's `DOWN_CONFIDENCE` (default 4), **and**
- at least `MIN_FAILED_PROBES` (default 3) distinct probes have failed, with no passing
  probe in between

So two hard failures alone (score 4) don't page on their own — a third failed probe is
required regardless of score. Any passing probe clears the burst immediately (a "flap":
logged, not alerted). Once DOWN, the incident stays silent — no repeat alerts — until the
first passing check, which sends exactly one RECOVERY email. While the main track is
DOWN, auth-track failures are absorbed into that same incident rather than paging twice.

The dashboard banner and alert emails speak one unified platform verdict (worst of the
two tracks), naming exactly which layer failed. Full weight table, defaults, and timing
budget: see CLAUDE.md's "Confidence scoring" section.

## Testing

```bash
pytest -q
```

`monitor/state.py` and `monitor/verdict.py` are pure and unit tested independently of
everything else — no network, no DB, no browser. `journey.py` and the web layer don't have
automated coverage yet (both verified via live drills and manual smoke checks instead — see
PROGRESS.md).

## Layout

```
monitor/check.py       # pulse + browser check -> CheckResult (main track)
monitor/journey.py     # sign-in journey: login -> TOTP -> authed assertion -> logout; cheap session-reuse check
monitor/session.py     # storageState save/load, session-freshness check
monitor/state.py       # pure state machine: (previous_state, check_result) -> (new_state, events)
monitor/verdict.py     # pure: worst_of(main, auth) + Rule 4's operator wording (one source for dashboard + alerts)
monitor/timeutil.py    # presentation-only UTC -> America/New_York formatting
monitor/channels/      # alert channels: email (Gmail), sms (Twilio), sms_gateway — see CONTRIBUTING.md to add another
monitor/db.py          # SQLite schema, migrations, reads/writes (checks, cycles, incidents, state, login_events)
monitor/web/app.py     # FastAPI routes: unified verdict, cycle log, CSV export (backend only, builds no HTML)
monitor/web/templates/ # dashboard.html — the page shell
monitor/web/static/    # dashboard.css, dashboard.js — served behind the same basic auth as the rest
monitor/main.py        # composition root: unified cycle loop + uvicorn in one asyncio process
config.py               # loads and validates .env
scripts/                # manual drill runners (sign-in journey, live cycle drill) -- see their docstrings
tests/                  # pytest suite
```

## Contributing

Alert channels are plug-ins (`email` and `sms` are both live) — see
[CONTRIBUTING.md](CONTRIBUTING.md) for the contract and what files a PR may touch to add
another one. Everything else in this repo is being built stage-by-stage against CLAUDE.md's
plan; please open an issue before sending an unsolicited PR against the core
check/alert/state path.
