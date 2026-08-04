# Monitor Lite

A deliberately small uptime monitor for a banking web platform. It replaces a Selenium
tool that false-positived on minor DOM changes — **low false positives outrank every
other concern, including speed.**

Every 60 seconds: a TLS-fingerprint-aware HTTP pulse check, then a headless Playwright
page check against a specific, role/text-based locator (no brittle CSS/XPath selectors).
Results are written to SQLite, every check pass or fail — the log is an audit artifact.
State transitions (and only transitions) trigger an email alert. A small FastAPI
dashboard shows uptime %, incidents, a filterable check log, and CSV export.

Full project rules, stage plan, and design rationale live in [CLAUDE.md](CLAUDE.md) — this
README is the quick-start; that file is the spec.

## Status

Sessions 1–3 (basic check loop, alerting, dashboard) are complete. The project is now on
Stage 4 of a longer v3 plan that adds an authenticated login journey, sub-60s detection,
and more — see CLAUDE.md's Stages section and [PROGRESS.md](PROGRESS.md) for the full
build log and decisions made along the way.

## Requirements

- Python 3.12
- [Playwright](https://playwright.dev/python/) browsers installed locally (`playwright install chromium`)

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium

cp .env.example .env
# fill in .env: TARGET_URL, REQUIRED_TEXT (or REQUIRED_ROLE+REQUIRED_NAME),
# DASHBOARD_USER/PASSWORD are required at minimum. Email alerting needs
# GMAIL_USER/GMAIL_APP_PASSWORD/RECIPIENTS_EMAIL — the loop runs fine without them,
# it just logs and skips sending.
```

## Running

```bash
python -m monitor.main
```

This starts both the check loop and the dashboard (single process, per the project's
"no second process" rule). Dashboard: `http://localhost:8080` (basic auth via
`DASHBOARD_USER`/`DASHBOARD_PASSWORD`). `/healthz` is unauthenticated.

## Testing

```bash
pytest
```

`monitor/state.py` is a pure function and is unit tested independently of everything
else — no network, no DB, no browser.

## Layout

```
monitor/check.py       # pulse + browser check -> CheckResult
monitor/state.py       # pure state machine: (previous_state, check_result) -> (new_state, events)
monitor/channels/      # alert channels (email live; sms is an open contribution seam — see CONTRIBUTING.md)
monitor/db.py          # SQLite schema, migrations, reads/writes
monitor/web.py         # FastAPI routes + dashboard
monitor/main.py        # composition root: check loop + uvicorn in one asyncio process
config.py               # loads and validates .env
tests/                  # pytest suite
```

## Contributing

The SMS alert channel is an open contribution seam — see [CONTRIBUTING.md](CONTRIBUTING.md)
for the contract and what files a PR may touch. Everything else in this repo is being
built stage-by-stage against CLAUDE.md's plan; please open an issue before sending an
unsolicited PR against the core check/alert/state path.
