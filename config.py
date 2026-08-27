"""Loads and validates settings from .env. Fails fast at startup if required values are missing."""
import os
import sys

from dotenv import load_dotenv

load_dotenv()

TARGET_NAME = os.getenv("TARGET_NAME")
TARGET_URL = os.getenv("TARGET_URL")
REQUIRED_TEXT = os.getenv("REQUIRED_TEXT")
REQUIRED_ROLE = os.getenv("REQUIRED_ROLE")
REQUIRED_NAME = os.getenv("REQUIRED_NAME")

CHECK_INTERVAL_S = int(os.getenv("CHECK_INTERVAL_S", "60"))
BROWSER_TIMEOUT_MS = int(os.getenv("BROWSER_TIMEOUT_MS", "15000"))

# Stage 5: confirmation burst + confidence scoring (replaces the old FAILS_TO_DOWN rule).
# [v3.1] defaults updated: DOWN requires score >= DOWN_CONFIDENCE AND >= MIN_FAILED_PROBES
# distinct failed probes in the burst window -- see CLAUDE.md's confidence-scoring section.
BURST_DELAYS_S = [int(x) for x in os.getenv("BURST_DELAYS_S", "0,15,35,55").split(",") if x.strip()]
BURST_JITTER_S = int(os.getenv("BURST_JITTER_S", "5"))
BURST_WINDOW_S = int(os.getenv("BURST_WINDOW_S", "90"))
DOWN_CONFIDENCE = int(os.getenv("DOWN_CONFIDENCE", "4"))
MIN_FAILED_PROBES = int(os.getenv("MIN_FAILED_PROBES", "3"))

RECIPIENTS_EMAIL = os.getenv("RECIPIENTS_EMAIL")
GMAIL_USER = os.getenv("GMAIL_USER")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")

DASHBOARD_USER = os.getenv("DASHBOARD_USER")
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD")
PORT = int(os.getenv("PORT", "8080"))
DB_PATH = os.getenv("DB_PATH", "./data/monitor.db")

ARTIFACTS_DIR = "./data/artifacts"

ALERT_CHANNELS = [c.strip() for c in os.getenv("ALERT_CHANNELS", "email").split(",") if c.strip()]

# Rule 14: headless is the only mode the scheduler may run; HEADLESS=false is a local-debug
# escape hatch only, never scheduled. Recorded on every check row (checks.browser_mode).
HEADLESS = os.getenv("HEADLESS", "true").lower() != "false"
BROWSER_MODE = "headless" if HEADLESS else "headed"

# [v3.3] Sign-in journey rows never use BROWSER_MODE above -- the journey always runs
# headed under xvfb (or a real display locally), regardless of HEADLESS, so it needs its
# own distinct value that can't collide with the scheduler's "headless"/"headed".
JOURNEY_BROWSER_MODE = "headed-xvfb"

# Rule 12: only a real branded browser binary + a polite UA are permitted bot-challenge mitigations.
BROWSER_CHANNEL = os.getenv("BROWSER_CHANNEL", "chrome")

# --- Stage 6: sign-in journey ---
# [v3.2] patchright is a named, scoped exception to Rule 12 for this journey only.
# [v3.3] the journey always runs headed under xvfb, never honors HEADLESS -- see journey.py.
LOGIN_URL = os.getenv("LOGIN_URL")
# [v3.8 / Stage R] The cheap session-reuse check (run_authed_check) navigates directly
# here, never to LOGIN_URL hoping for a post-login redirect -- the login route and the
# authed-home route are different paths with different dependencies (auth service, MFA
# backend, login UI bundle, Cloudflare challenge vs. just session-cookie validation), so
# a login-route outage and a home-route outage are genuinely independent evidence. See
# CLAUDE.md Rule 16.
AUTHED_URL = os.getenv("AUTHED_URL")
LOGIN_USER = os.getenv("LOGIN_USER")
LOGIN_PASSWORD = os.getenv("LOGIN_PASSWORD")
MFA_ENABLED = os.getenv("MFA_ENABLED", "true").lower() != "false"
TOTP_SECRET = os.getenv("TOTP_SECRET")
# Testing-only, off by default: lets the auth track START (session-reuse checks run
# normally) without a captured TOTP_SECRET. Any recovery login it does need still can't
# complete MFA -- it reports mfa_failed -> CONFIG_ERROR cleanly (journey.py's submit_totp
# guard) instead of the track never starting at all. Same pattern as the manual-MFA
# drill-script escape hatch: named, tracked, temporary. Production still requires a real
# TOTP_SECRET -- this does not relax that, it only unblocks testing session lifetime.
ALLOW_MFA_UNCONFIGURED = os.getenv("ALLOW_MFA_UNCONFIGURED", "false").lower() == "true"
AUTHED_REQUIRED_TEXT = os.getenv("AUTHED_REQUIRED_TEXT")
AUTHED_REQUIRED_ROLE = os.getenv("AUTHED_REQUIRED_ROLE")
AUTHED_REQUIRED_NAME = os.getenv("AUTHED_REQUIRED_NAME")
ERROR_BANNER_TEXT = os.getenv("ERROR_BANNER_TEXT")
MASK_TEXT = [t.strip() for t in os.getenv("MASK_TEXT", "").split(";") if t.strip()]
MASKING_ENABLED = os.getenv("MASKING_ENABLED", "true").lower() != "false"
CHALLENGE_TIMEOUT_MS = int(os.getenv("CHALLENGE_TIMEOUT_MS", "25000"))

# NOTE: LOGIN_STRESS_MODE is deliberately absent. v3's sanctioned stress-mode exception to
# Rule 11 is a Stage 7 task and the logic behind it was never built, so the setting existed
# only as an unread module attribute -- removed here rather than left looking implemented.
# Stage 7 reintroduces it together with the code that actually reads it.

# --- Stage 8: session reuse (pulled forward ahead of Stage 7 -- see CLAUDE.md v3.7) ---
# [v3.4] SESSION_STATE_PATH is a real secret (chmod 600, gitignored): storageState
# (cookies + localStorage, including cf_clearance) saved after a successful full login,
# reused by the cheap every-CHECK_INTERVAL_S authed check so it needs zero logins.
SESSION_STATE_PATH = os.getenv("SESSION_STATE_PATH", "./data/session_state.json")
SESSION_MAX_AGE_S = int(os.getenv("SESSION_MAX_AGE_S", "1800"))

# Minimum gap between full login attempts (credentials + TOTP) -- a deliberately
# conservative, hand-picked value since there's no measured safe rate yet (that's Stage
# 7's job, which should replace it with a measured number).
#
# [v3.9] This is now the ONLY login rate limit -- MAX_LOGINS_PER_DAY was removed (see
# _login_budget_allows() in main.py for why). Because attempts can only start on a
# CHECK_INTERVAL_S boundary and each attempt takes real time, the achievable rate is
# NOT 3600/LOGIN_INTERVAL_S: it's one attempt every
#   ceil((LOGIN_INTERVAL_S + login_duration_s) / CHECK_INTERVAL_S) * CHECK_INTERVAL_S
# seconds. At 120s with a ~15-20s login that's every 180s (20/hour), not every 120s.
# Worked example in .env.example's reference section.
#
# It must stay meaningfully longer than one cycle or it stops limiting anything (a
# cooldown shorter than CHECK_INTERVAL_S is always already satisfied by the time the
# next cycle asks), hence the startup floor below rather than a daily counter.
LOGIN_INTERVAL_S = int(os.getenv("LOGIN_INTERVAL_S", "1800"))
MIN_LOGIN_INTERVAL_S = 60
if LOGIN_INTERVAL_S < MIN_LOGIN_INTERVAL_S:
    sys.exit(
        f"LOGIN_INTERVAL_S={LOGIN_INTERVAL_S} is below the {MIN_LOGIN_INTERVAL_S}s floor. "
        "It is the only limit on full credential+MFA logins (Rule 11) -- a lower value "
        "lets a persistently failing login retry every cycle indefinitely."
    )

# [v3.8 / Stage R] The auth track's own confidence thresholds -- now matched to the main
# track's (v3.6's AUTH_DOWN_CONFIDENCE=2/AUTH_MIN_FAILED_PROBES=1 shortcut is retired: it
# was only safe because the old design never actually bursted the auth track. Since
# every cheap session-reuse re-probe costs zero logins, the auth track can afford the
# same 3-probe floor as pulse/render -- see CLAUDE.md Rule 2/16.
AUTH_DOWN_CONFIDENCE = int(os.getenv("AUTH_DOWN_CONFIDENCE", "4"))
AUTH_MIN_FAILED_PROBES = int(os.getenv("AUTH_MIN_FAILED_PROBES", "3"))


def validate_stage6():
    """Called at cycle_scheduler() startup to decide whether the auth track runs at all,
    and by the manual drill runner. TOTP_SECRET is required whenever MFA_ENABLED, UNLESS
    ALLOW_MFA_UNCONFIGURED is explicitly set (testing-only escape hatch, off by default --
    see its definition above): the auth track then still starts and runs session-reuse
    checks normally, but any recovery login it needs will cleanly report mfa_failed
    instead of completing (journey.py's submit_totp guard)."""
    missing = []
    if not LOGIN_URL:
        missing.append("LOGIN_URL")
    if not AUTHED_URL:
        missing.append("AUTHED_URL")
    if not LOGIN_USER:
        missing.append("LOGIN_USER")
    if not LOGIN_PASSWORD:
        missing.append("LOGIN_PASSWORD")
    if not ERROR_BANNER_TEXT:
        missing.append("ERROR_BANNER_TEXT")
    if not AUTHED_REQUIRED_TEXT and not (AUTHED_REQUIRED_ROLE and AUTHED_REQUIRED_NAME):
        missing.append("AUTHED_REQUIRED_TEXT (or AUTHED_REQUIRED_ROLE + AUTHED_REQUIRED_NAME)")
    if MFA_ENABLED and not TOTP_SECRET and not ALLOW_MFA_UNCONFIGURED:
        missing.append("TOTP_SECRET (or set ALLOW_MFA_UNCONFIGURED=true for testing)")

    if missing:
        sys.exit(
            "Missing required .env values for the sign-in journey: " + ", ".join(missing) +
            "\nCopy .env.example to .env and fill these in."
        )


def validate_core():
    """Validates the settings the app needs at startup. Email settings are not
    required here — alert.py logs and skips sending if they're unset, since a
    missing app password shouldn't take the whole monitor down."""
    missing = []
    if not TARGET_NAME:
        missing.append("TARGET_NAME")
    if not TARGET_URL:
        missing.append("TARGET_URL")
    if not REQUIRED_TEXT and not (REQUIRED_ROLE and REQUIRED_NAME):
        missing.append("REQUIRED_TEXT (or REQUIRED_ROLE + REQUIRED_NAME)")
    if not DASHBOARD_USER:
        missing.append("DASHBOARD_USER")
    if not DASHBOARD_PASSWORD:
        missing.append("DASHBOARD_PASSWORD")

    if missing:
        sys.exit(
            "Missing required .env values: " + ", ".join(missing) +
            "\nCopy .env.example to .env and fill these in."
        )
