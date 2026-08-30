"""Loads and validates settings from .env. Fails fast at startup if required values are missing."""
import os
import sys

from dotenv import find_dotenv, load_dotenv
from dotenv.parser import parse_stream


def _load_env_or_exit() -> None:
    """[B33, 2026-08-29] Refuse to start on a .env containing any line python-dotenv
    cannot parse, instead of load_dotenv()'s default behaviour of printing a warning to
    stderr and carrying on.

    Why this is worth a startup guard rather than a warning: dotenv treats every
    unparseable line identically -- warn once, skip it, continue. A stray note missing its
    '#' is harmless that way, but a REAL assignment that got mangled is silently dropped
    and os.getenv() then falls through to the hardcoded default below, so the monitor comes
    up looking healthy while running on a value nobody chose, with one line on stderr as
    the only evidence. That is the same class of silent misconfiguration this file already
    refuses to allow for missing required settings and for LOGIN_INTERVAL_S below its
    floor; an unreadable config line gets the same treatment.

    Verified shapes dotenv rejects: a line whose '=' is missing entirely (LOGIN_PASSWORD
    "x" -- easy to produce by pasting out of a document), an unterminated quote
    (KEY='value with no closing quote), and any prose line. Note it does NOT reject
    'KEY =value' -- surrounding whitespace is tolerated -- so this catches structural
    damage, not every possible typo.

    Rule 16 "secrets from .env only": the offending line's CONTENT is never echoed. A
    mangled line is most likely a mangled assignment, and the value half of it may well be
    LOGIN_PASSWORD or TOTP_SECRET. Line numbers are enough to find it and are safe to print
    into a log or a terminal someone else can see.

    One subtlety, found while verifying this function and worth keeping: the path is
    resolved ONCE and both the check and the load use it. Bare load_dotenv() runs its own
    discovery that walks up from the calling frame's file (this config.py's directory),
    while find_dotenv(usecwd=True) walks up from the process working directory. Those
    resolve to different files whenever the monitor is started from outside the repo root,
    which would let this guard hand a clean bill of health to a file that was never
    loaded -- exactly the failure it exists to prevent."""
    path = find_dotenv(usecwd=True)
    if not path:
        return  # no .env anywhere; the environment alone must supply everything

    try:
        with open(path, encoding="utf-8") as handle:
            bad_lines = [b.original.line for b in parse_stream(handle) if b.error]
    except OSError as exc:
        sys.exit(f"Could not read {path}: {exc.strerror}")

    if bad_lines:
        numbers = ", ".join(str(n) for n in bad_lines)
        plural = "s" if len(bad_lines) > 1 else ""
        sys.exit(
            f"{path} has {len(bad_lines)} line{plural} that could not be parsed as a "
            f"setting: line{plural} {numbers}.\n"
            "Refusing to start: an unparseable line is skipped silently, so a mangled "
            "assignment would fall back to a hardcoded default and the monitor would run "
            "misconfigured while looking healthy.\n"
            "Fix it, or prefix it with '#' if it was meant to be a comment. (The line's "
            "contents are deliberately not shown here -- it may contain a credential.)"
        )

    load_dotenv(path)


_load_env_or_exit()

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
# [2026-08-30 / B38] BURST_WINDOW_S is retired. DOWN now requires N *consecutive* failed
# probes on one layer with no intervening pass -- no clock. The window's failure mode was
# that a probe slower than its slot timestamped itself outside the window it was scheduled
# in, so the count reset forever and the auth track could never page (B13). A slow probe now
# delays detection instead of preventing it.
#
# EVIDENCE_STALE_AFTER_S replaces the window's one useful job. Evidence persists until a
# pass clears it, so the case a pass cannot cover is the monitor not looking at all --
# restart, host asleep, the B42 hang. If a layer has gone unprobed this long, its run is
# discarded rather than completed by a much later failure. Without it the machine pages on
# hours-old evidence; that was tested, not assumed.
EVIDENCE_STALE_AFTER_S = int(os.getenv("EVIDENCE_STALE_AFTER_S", "600"))

# [B39] Entering DOWN takes MIN_FAILED_PROBES corroborated probes; leaving it took one, so
# every bit of the project's false-positive discipline sat on one side of the incident. A
# premature RECOVERED tells an operator to stop looking at something still broken.
RECOVERY_PASSES = int(os.getenv("RECOVERY_PASSES", "3"))
DOWN_CONFIDENCE = int(os.getenv("DOWN_CONFIDENCE", "4"))
MIN_FAILED_PROBES = int(os.getenv("MIN_FAILED_PROBES", "4"))

RECIPIENTS_EMAIL = os.getenv("RECIPIENTS_EMAIL")
GMAIL_USER = os.getenv("GMAIL_USER")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")

DASHBOARD_USER = os.getenv("DASHBOARD_USER")
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD")
PORT = int(os.getenv("PORT", "8080"))
DB_PATH = os.getenv("DB_PATH", "./data/monitor.db")

ARTIFACTS_DIR = "./data/artifacts"

ALERT_CHANNELS = [c.strip() for c in os.getenv("ALERT_CHANNELS", "email").split(",") if c.strip()]

# Rule 14 "headless is the scheduler's mode": headless is the only mode the scheduler may run; HEADLESS=false is a local-debug
# escape hatch only, never scheduled. Recorded on every check row (checks.browser_mode).
HEADLESS = os.getenv("HEADLESS", "true").lower() != "false"
BROWSER_MODE = "headless" if HEADLESS else "headed"

# [v3.3] Sign-in journey rows never use BROWSER_MODE above -- the journey always runs
# headed under xvfb (or a real display locally), regardless of HEADLESS, so it needs its
# own distinct value that can't collide with the scheduler's "headless"/"headed".
JOURNEY_BROWSER_MODE = "headed-xvfb"

# Rule 6 "bot challenges: detect, never defeat": only a real branded browser binary + a polite UA are permitted bot-challenge mitigations.
BROWSER_CHANNEL = os.getenv("BROWSER_CHANNEL", "chrome")

# --- Stage 6: sign-in journey ---
# [v3.2] patchright is a named, scoped exception to Rule 6 "bot challenges: detect, never defeat" for this journey only.
# [v3.3] the journey always runs headed under xvfb, never honors HEADLESS -- see journey.py.
LOGIN_URL = os.getenv("LOGIN_URL")
# [v3.8 / Stage R] The cheap session-reuse check (run_authed_check) navigates directly
# here, never to LOGIN_URL hoping for a post-login redirect -- the login route and the
# authed-home route are different paths with different dependencies (auth service, MFA
# backend, login UI bundle, Cloudflare challenge vs. just session-cookie validation), so
# a login-route outage and a home-route outage are genuinely independent evidence. See
# CLAUDE.md's "The three layers" section (AUTHED_URL is navigated directly,
# configured, never derived from LOGIN_URL).
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
# Rule 5 "the login budget is a hard limit" is a Stage 7 task and the logic behind it was never built, so the setting existed
# only as an unread module attribute -- removed here rather than left looking implemented.
# Stage 7 reintroduces it together with the code that actually reads it.

# --- Stage 8: session reuse (pulled forward ahead of Stage 7 -- see CLAUDE.md v3.7) ---
# [v3.4] SESSION_STATE_PATH is a real secret (chmod 600, gitignored): storageState
# (cookies + localStorage, including cf_clearance) saved after a successful full login,
# reused by the cheap every-CHECK_INTERVAL_S authed check so it needs zero logins.
SESSION_STATE_PATH = os.getenv("SESSION_STATE_PATH", "./data/session_state.json")
# [B30, 2026-08-29] Default was 1800, equal to LOGIN_INTERVAL_S's old default -- the
# exact pairing Rule 5 "the login budget is a hard limit" forbids. v3.9 fixed .env and
# .env.example to 600/120 but missed the fallbacks behind them, so anyone running
# without these two variables set got the forbidden config silently. Now matches the
# shipped example, and the headroom guard below enforces the rule rather than just
# avoiding one violation of it.
SESSION_MAX_AGE_S = int(os.getenv("SESSION_MAX_AGE_S", "600"))

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
LOGIN_INTERVAL_S = int(os.getenv("LOGIN_INTERVAL_S", "120"))  # [B30] was 1800; see SESSION_MAX_AGE_S above
MIN_LOGIN_INTERVAL_S = 60
if LOGIN_INTERVAL_S < MIN_LOGIN_INTERVAL_S:
    sys.exit(
        f"LOGIN_INTERVAL_S={LOGIN_INTERVAL_S} is below the {MIN_LOGIN_INTERVAL_S}s floor. "
        "It is the only limit on full credential+MFA logins (Rule 5 'the login budget is a hard limit') -- a lower value "
        "lets a persistently failing login retry every cycle indefinitely."
    )

# [B30, 2026-08-29] The two clocks must not converge. SESSION_MAX_AGE_S is measured from
# when the session FILE is written (inside run_journey, before it returns);
# LOGIN_INTERVAL_S is measured from when the login ROW is recorded (after run_journey
# returns). The gap between those two moments is post-login browser/driver teardown, and
# it is not bounded by anything we control -- so the session can go stale while the budget
# still refuses to authorise the login that would refresh it. Every second of that window
# is a cycle reporting session_expired on a perfectly healthy session, with no authed
# evidence behind the platform verdict.
#
# Observed live 2026-08-26: a 300.1s teardown stall on a SUCCESSFUL login, with both
# values at 600, produced five consecutive dead minutes. Normal overhead is 0.3-0.5s.
#
# The invariant is headroom, not a ratio: the window opens when
#   LOGIN_INTERVAL_S + teardown_skew >= SESSION_MAX_AGE_S
# so what must be reserved is SESSION_MAX_AGE_S - LOGIN_INTERVAL_S, and it must cover the
# worst teardown we have actually measured. 300s is therefore a floor derived from
# evidence, not a round number -- and it is a floor, not a target. The shipped 600/120
# leaves 480s.
MIN_SESSION_HEADROOM_S = 300
_headroom = SESSION_MAX_AGE_S - LOGIN_INTERVAL_S
if _headroom < MIN_SESSION_HEADROOM_S:
    sys.exit(
        f"SESSION_MAX_AGE_S={SESSION_MAX_AGE_S} and LOGIN_INTERVAL_S={LOGIN_INTERVAL_S} "
        f"leave only {_headroom}s of headroom; at least {MIN_SESSION_HEADROOM_S}s is "
        "required.\n"
        "The session clock starts when the session file is written and the login clock "
        "when the login row is recorded, so post-login teardown time sits between them. "
        "Too little headroom opens a window where the session is stale but the budget "
        "still refuses the login that would refresh it -- observed live 2026-08-26 as five "
        "minutes of false session_expired on a healthy session (300.1s teardown at "
        "600/600).\n"
        f"Lower LOGIN_INTERVAL_S or raise SESSION_MAX_AGE_S. Shipped values are "
        "SESSION_MAX_AGE_S=600 / LOGIN_INTERVAL_S=120 (480s of headroom)."
    )

# [v3.8 / Stage R] The auth track's own confidence thresholds -- now matched to the main
# track's (v3.6's AUTH_DOWN_CONFIDENCE=2/AUTH_MIN_FAILED_PROBES=1 shortcut is retired: it
# was only safe because the old design never actually bursted the auth track. Since
# every cheap session-reuse re-probe costs zero logins, the auth track can afford the
# same 3-probe floor as pulse/render -- see CLAUDE.md Rule 2 "alert only on transitions"/16.
AUTH_DOWN_CONFIDENCE = int(os.getenv("AUTH_DOWN_CONFIDENCE", "4"))
AUTH_MIN_FAILED_PROBES = int(os.getenv("AUTH_MIN_FAILED_PROBES", "4"))


def _collect_missing(*checks: tuple[bool, str]) -> list[str]:
    """Shared shape behind every "required .env values" check below: each check is
    (condition_is_missing, label); returns the labels whose condition is True. Exists so
    validate_stage6()'s field list can be reused as-is by the manual drill runner (which
    needs the identical set minus TOTP_SECRET) instead of hand-copying it -- see
    validate_stage6's require_totp parameter."""
    return [label for is_missing, label in checks if is_missing]


def _exit_if_missing(missing: list[str], context: str = "") -> None:
    if not missing:
        return
    suffix = f" {context}" if context else ""
    sys.exit(
        f"Missing required .env values{suffix}: " + ", ".join(missing) +
        "\nCopy .env.example to .env and fill these in."
    )


def validate_stage6(require_totp: bool = True, require_authed_url: bool = True) -> list[str]:
    """Called at cycle_scheduler() startup to decide whether the auth track runs at all,
    and by the manual drill runner (scripts/run_signin_drill.py) with both flags off: that
    script drives monitor.journey.run_journey() directly, which asserts the authed content
    on the page reached via LOGIN_URL's own post-login redirect and never touches AUTHED_URL
    at all -- only the cheap session-reuse check (run_authed_check) navigates there. It also
    substitutes a manual-MFA pause for a captured TOTP_SECRET (see that script's docstring).

    TOTP_SECRET is otherwise required whenever MFA_ENABLED, UNLESS ALLOW_MFA_UNCONFIGURED is
    explicitly set (testing-only escape hatch, off by default -- see its definition above):
    the auth track then still starts and runs session-reuse checks normally, but any
    recovery login it needs will cleanly report mfa_failed instead of completing
    (journey.py's submit_totp guard).

    Exits the process (via _exit_if_missing) if anything required is missing, and also
    returns the missing-labels list so a caller could inspect it without exiting -- not
    currently used that way, but keeps the check itself reusable independent of the exit."""
    missing = _collect_missing(
        (not LOGIN_URL, "LOGIN_URL"),
        (require_authed_url and not AUTHED_URL, "AUTHED_URL"),
        (not LOGIN_USER, "LOGIN_USER"),
        (not LOGIN_PASSWORD, "LOGIN_PASSWORD"),
        (not ERROR_BANNER_TEXT, "ERROR_BANNER_TEXT"),
        (not AUTHED_REQUIRED_TEXT and not (AUTHED_REQUIRED_ROLE and AUTHED_REQUIRED_NAME),
         "AUTHED_REQUIRED_TEXT (or AUTHED_REQUIRED_ROLE + AUTHED_REQUIRED_NAME)"),
    )
    if require_totp and MFA_ENABLED and not TOTP_SECRET and not ALLOW_MFA_UNCONFIGURED:
        missing.append("TOTP_SECRET (or set ALLOW_MFA_UNCONFIGURED=true for testing)")

    _exit_if_missing(missing, "for the sign-in journey")
    return missing


def validate_core() -> None:
    """Validates the settings the app needs at startup. Email settings are not
    required here — alert.py logs and skips sending if they're unset, since a
    missing app password shouldn't take the whole monitor down."""
    missing = _collect_missing(
        (not TARGET_NAME, "TARGET_NAME"),
        (not TARGET_URL, "TARGET_URL"),
        (not REQUIRED_TEXT and not (REQUIRED_ROLE and REQUIRED_NAME),
         "REQUIRED_TEXT (or REQUIRED_ROLE + REQUIRED_NAME)"),
        (not DASHBOARD_USER, "DASHBOARD_USER"),
        (not DASHBOARD_PASSWORD, "DASHBOARD_PASSWORD"),
    )
    _exit_if_missing(missing)
