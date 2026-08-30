"""Stage 6 manual drill runner: runs the full sign-in journey once against LOGIN_URL,
including logout, and reports what happened. This is how Stage 6's acceptance test gets
exercised by hand (real sign-in succeeds and logs out cleanly, wrong-password drill ->
CONFIG alert, etc.), independent of the automatic auth_scheduler() loop in main.py
(which runs continuously, reuses sessions, and deliberately skips logout to keep the
session alive -- see CLAUDE.md v3.7). This script always logs out, since proving logout
works is part of Stage 6's own acceptance test, at the cost of the session it just
created not being reusable afterward.

If TOTP_SECRET isn't set, this script falls back to a temporary fully-manual MFA mode:
it stops right before the point where automated code would click "Enter code" or touch
any code field, and instead pauses so you can complete MFA entirely by hand in the
visible browser -- including factors this codebase doesn't automate at all (e.g. a
phone-call code, not a TOTP app code). That's testing-only -- the automated production
loop (main.py) never does this and still requires a real TOTP_SECRET (CLAUDE.md's
captured TOTP_SECRET requirement isn't relaxed, just deferred for this script).

--keep-session: saves the resulting storageState to SESSION_STATE_PATH and skips logout,
instead of the default (always logs out, never saves) -- for capturing a real session by
hand (e.g. via manual MFA above) so it can be reused by the cheap authed check /
run_cycle() afterward, without needing a real TOTP_SECRET at all. Testing-only, same as
manual MFA -- the automated loop always saves+never-logs-out on its own, real, budgeted
logins (see main.py's _run_full_login), this flag just lets a manual run behave the same
way for drilling purposes.

This script always runs a genuinely fresh login (never seeds its browser from an old
session file, even if one exists at SESSION_STATE_PATH) -- fix, 2026-08-11: seeding a
fresh login with a stale session was found to redirect the bank's server to the public
marketing homepage instead of the login form. You no longer need to manually delete
SESSION_STATE_PATH before running this script.

Usage: .venv/bin/python -m scripts.run_signin_drill [--keep-session]
"""
import asyncio
import sys
import config
from monitor import db, journey
from monitor.channels import build_channels
from monitor.main import _process_probe
from monitor.timeutil import now_iso


async def _manual_mfa_pause() -> None:
    print("No TOTP_SECRET configured -- manual MFA mode. The script will not click "
          "'Enter code' or touch any code field.")
    # input() blocks the whole interpreter thread waiting on stdin; called directly inside
    # an async function it would freeze the event loop (and, with it, the browser's own
    # background tasks) until Enter is pressed. asyncio.to_thread runs the blocking read on
    # a worker thread instead, so the event loop -- and the visible browser page the human
    # is completing MFA in -- stays responsive while this coroutine waits.
    await asyncio.to_thread(
        input, "Complete MFA yourself in the browser window (any factor -- phone call, "
               "TOTP app, etc.), then press Enter here to continue: "
    )


async def main() -> None:
    config.validate_stage6(require_totp=False, require_authed_url=False)
    manual_mfa = not config.TOTP_SECRET
    keep_session = "--keep-session" in sys.argv

    conn = db.get_connection(config.DB_PATH)
    db.init_db(conn)
    channels = build_channels(config.ALERT_CHANNELS)
    prev_state = db.get_state(conn, track="auth")

    print(f"Running sign-in journey against {config.LOGIN_URL} ...")
    if keep_session:
        print(f"--keep-session: will save storageState to {config.SESSION_STATE_PATH} and skip logout.")
    result = await journey.run_journey(
        login_url=config.LOGIN_URL,
        login_user=config.LOGIN_USER,
        login_password=config.LOGIN_PASSWORD,
        totp_secret=config.TOTP_SECRET,
        error_banner_text=config.ERROR_BANNER_TEXT,
        authed_text=config.AUTHED_REQUIRED_TEXT,
        authed_role=config.AUTHED_REQUIRED_ROLE,
        authed_name=config.AUTHED_REQUIRED_NAME,
        browser_channel=config.BROWSER_CHANNEL,
        browser_timeout_ms=config.BROWSER_TIMEOUT_MS,
        challenge_timeout_ms=config.CHALLENGE_TIMEOUT_MS,
        artifacts_dir=config.ARTIFACTS_DIR,
        mask_patterns=config.MASK_TEXT,
        masking_enabled=config.MASKING_ENABLED,
        session_state_path=config.SESSION_STATE_PATH if keep_session else None,
        seed_from_existing_session=False,  # always a genuinely fresh login -- see run_journey's docstring
        should_logout=not keep_session,
        manual_mfa_pause=_manual_mfa_pause if manual_mfa else None,
    )

    ts = now_iso()

    # Rule 15 "every probe writes a checks row" / Stage 6 spec: every sign-in attempt is its own audit row, independent of
    # the checks table (which _process_probe writes below in the same call).
    db.append_login_event(
        conn, ts=ts, ok=result.ok, latency_ms=result.latency_ms,
        reason=result.fail_reason, http_status=result.http_status, session_reused=False,
    )

    await _process_probe(conn, channels, prev_state, result, ts, burst_id=None,
                          browser_mode=config.JOURNEY_BROWSER_MODE, track="auth",
                          down_confidence=config.AUTH_DOWN_CONFIDENCE,
                          min_failed_probes=config.AUTH_MIN_FAILED_PROBES)

    if result.ok:
        if keep_session:
            print(f"OK -- signed in, session saved to {config.SESSION_STATE_PATH} (not logged out) ({result.latency_ms:.0f}ms)")
        else:
            print(f"OK -- signed in and logged out cleanly ({result.latency_ms:.0f}ms)")
    else:
        shot = f" screenshot={result.screenshot_path}" if result.screenshot_path else ""
        print(f"FAIL -- {result.fail_reason} (layer={result.layer}, {result.latency_ms:.0f}ms){shot}")

    conn.close()


if __name__ == "__main__":
    asyncio.run(main())
