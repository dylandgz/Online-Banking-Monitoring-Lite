"""Composition root: runs the unified 60s cycle loop and uvicorn together in one asyncio
process. [v3.8 / Stage R] One cycle = one platform verdict: the main track (pulse/render)
and the auth track (cheap authed-session check, budgeted recovery login) both run inside
a single tick, sharing a cycle_id, and are combined into one `cycles` summary row. See
CLAUDE.md's v3.8 amendment and Rule 16 for the reasoning."""
import asyncio
import itertools
import random
import uuid
from datetime import datetime, timezone

import uvicorn

import config
from monitor import check, db, journey, session
from monitor.channels import build_channels, dispatch
from monitor.state import ConfigErrorEvent, DownEvent, MonitorState, RecoveryEvent, apply_check
from monitor.verdict import severity, unified_verdict


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _process_probe(
    conn, channels, prev_state: MonitorState, result, ts: str, burst_id: str | None,
    browser_mode: str | None = None,
    track: str = "main",
    down_confidence: int | None = None,
    burst_window_s: int | None = None,
    min_failed_probes: int | None = None,
    precursor_down: bool = False,
    cycle_id: str | None = None,
) -> MonitorState:
    """Writes the check row (Rule 6: every probe writes its own row, pass or fail), advances
    the pure state machine, persists it, and dispatches any resulting alert.

    browser_mode defaults to config.BROWSER_MODE (the pulse/render track's mode) --
    callers for other layers (the Stage 6 sign-in journey, which always runs headed per
    v3.3) must pass their own, so the audit trail doesn't misreport how the check
    actually ran.

    track/down_confidence/burst_window_s/min_failed_probes default to the main track's
    config values -- the auth-track caller passes its own track name and thresholds.

    precursor_down [v3.8 Rule 16]: only meaningful for the auth track, when the main
    track's incident is already open this cycle -- suppresses a DownEvent (still logs
    the check row and keeps accumulating evidence) so a login-route outage doesn't page
    twice for the same underlying cause.

    cycle_id [v3.8 / Stage R]: links this probe's checks row back to the cycles summary
    row for the tick it belongs to."""
    db.append_check(
        conn,
        ts=ts,
        ok=result.ok,
        http_status=result.http_status,
        latency_ms=result.latency_ms,
        fail_reason=result.fail_reason,
        browser_mode=browser_mode or config.BROWSER_MODE,
        layer=result.layer,
        burst_id=burst_id,
        cycle_id=cycle_id,
    )
    status = "OK" if result.ok else f"FAIL ({result.fail_reason}, layer={result.layer})"
    shot = f" screenshot={result.screenshot_path}" if result.screenshot_path else ""
    print(f"[{ts}] [{track}] {status}{shot}")

    new_state, events = apply_check(
        prev_state, result.ok, result.fail_reason, ts, result.layer,
        down_confidence if down_confidence is not None else config.DOWN_CONFIDENCE,
        burst_window_s if burst_window_s is not None else config.BURST_WINDOW_S,
        min_failed_probes if min_failed_probes is not None else config.MIN_FAILED_PROBES,
        precursor_down=precursor_down,
    )
    db.set_state(conn, new_state, track=track)

    for event in events:
        if isinstance(event, DownEvent):
            db.open_incident(
                conn, event.since_ts, event.confidence, event.trigger_layer,
                len(event.fail_reasons), result.screenshot_path, track=track,
            )
        elif isinstance(event, RecoveryEvent):
            db.close_incident(conn, event.ended_at, event.duration_s, event.confidence,
                               len(event.fail_reasons), track=track)
        elif isinstance(event, ConfigErrorEvent):
            pass  # not an outage -- no incident row, just the CONFIG alert below
        print(f"[alert] [{track}] {event!r}")
        await dispatch(event, channels)

    return new_state


async def _run_burst_reprobes(
    conn, channels, state: MonitorState, burst_start_ts: str, cycle_id: str,
) -> tuple[MonitorState, "check.CheckResult", str]:
    """Runs the rest of the main-track confirmation burst inline (Stage 5): re-probes at
    the remaining BURST_DELAYS_S offsets, varying the probe (render, then pulse) so each
    is independent evidence rather than a correlated retry. Stops the moment the burst
    resolves -- DOWN fires, or a pass clears it. Running this inline means the cycle's
    overlap-guard lock naturally absorbs the next tick instead of double-probing while a
    burst is in flight -- the human's call: reliability over a strict 60s cadence.
    Returns the final state, the last probe's CheckResult (for the cycles row's
    fail_layer), and the burst_id every probe in this burst was tagged with (so the cycles
    row can carry it too -- see run_cycle)."""
    burst_id = str(uuid.uuid4())
    last_result = None
    probe_kinds = list(itertools.islice(itertools.cycle(["render", "pulse"]), len(config.BURST_DELAYS_S) - 1))
    start = datetime.fromisoformat(burst_start_ts)

    for kind, delay_s in zip(probe_kinds, config.BURST_DELAYS_S[1:]):
        if state.status != "UP" or state.burst_started_ts != burst_start_ts:
            break  # already resolved: DOWN fired, or a pass cleared the burst

        jitter = random.uniform(-config.BURST_JITTER_S, config.BURST_JITTER_S)
        target_offset = max(0.0, delay_s + jitter)
        elapsed = (datetime.now(timezone.utc) - start).total_seconds()
        sleep_s = max(0.0, target_offset - elapsed)
        if sleep_s:
            await asyncio.sleep(sleep_s)

        if kind == "render":
            result = await check.render_only_probe(
                config.TARGET_URL, config.REQUIRED_TEXT, config.REQUIRED_ROLE, config.REQUIRED_NAME,
                config.BROWSER_TIMEOUT_MS, config.ARTIFACTS_DIR, headless=config.HEADLESS,
            )
        else:
            result = await check.pulse_only_probe(config.TARGET_URL)

        last_result = result
        state = await _process_probe(conn, channels, state, result, _now_iso(), burst_id, cycle_id=cycle_id)

    return state, last_result, burst_id


async def _run_full_login(conn, *, should_logout: bool) -> "check.CheckResult":
    """One real login attempt (credentials + TOTP), saving the refreshed session on
    success. Always logged to login_events -- this is the real, budgeted action."""
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
        session_state_path=config.SESSION_STATE_PATH,
        # A fresh login must NOT be seeded with the old (possibly stale) session -- found
        # live to cause the bank's server to redirect to the marketing homepage instead
        # of the login form (see journey.run_journey's docstring, fix 2026-08-11).
        seed_from_existing_session=False,
        should_logout=should_logout,
    )
    db.append_login_event(
        conn, ts=_now_iso(), ok=result.ok, latency_ms=result.latency_ms,
        reason=result.fail_reason, http_status=result.http_status, session_reused=False,
    )
    return result


def _login_budget_allows(conn) -> bool:
    """Rule 11's hard limit, applied to the recovery path below: a minimum gap since the
    last attempt (success or failure) so a session that keeps expiring immediately can't
    trigger a login storm.

    [v3.9] The old MAX_LOGINS_PER_DAY cap was removed. LOGIN_INTERVAL_S already bounds
    the daily total by arithmetic (see .env.example's worked example), so the cap only
    ever mattered as a second ceiling below that bound -- and reaching it was a dead end
    until UTC midnight: the auth track stopped probing while `session_expired` kept
    scoring 0, so the dashboard read UP with no authed evidence behind it and nothing
    alerted. A throttle that recovers on its own is the right shape here; a breaker that
    silently blinds the monitor for the rest of the day is not. config.py now enforces a
    minimum LOGIN_INTERVAL_S at startup, since it is the only login rate limit left."""
    recent = db.get_recent_login_events(conn, limit=1)
    if recent:
        last_ts = datetime.fromisoformat(recent[0]["ts"])
        if (datetime.now(timezone.utc) - last_ts).total_seconds() < config.LOGIN_INTERVAL_S:
            return False
    return True


def _session_expired_result() -> "check.CheckResult":
    return check.CheckResult(ok=False, http_status=None, latency_ms=0.0, fail_reason="session_expired", layer="authed")


async def _run_auth_probe(conn, main_down: bool, login_budget_state: dict) -> "check.CheckResult":
    """One auth-track probe attempt for this cycle. Cheap session-reuse check first
    (zero logins). session_expired (or no session at all) triggers exactly one budgeted
    recovery login per cycle (Rule 11) -- UNLESS the main track's precursor is already
    DOWN this cycle, in which case the recovery-login path is paused entirely: no budget
    is spent chasing a possibly-down site, and the session_expired result is reported
    as-is (harmless either way -- Rule 17 makes session_expired inert to scoring on any
    track). Any *other* cheap-check failure (e.g. nav_error) is real evidence and is
    returned untouched -- no login involved, it feeds the burst directly."""
    result = None
    if session.is_session_fresh(config.SESSION_STATE_PATH, config.SESSION_MAX_AGE_S):
        result = await journey.run_authed_check(
            authed_url=config.AUTHED_URL,
            authed_text=config.AUTHED_REQUIRED_TEXT,
            authed_role=config.AUTHED_REQUIRED_ROLE,
            authed_name=config.AUTHED_REQUIRED_NAME,
            error_banner_text=config.ERROR_BANNER_TEXT,
            browser_channel=config.BROWSER_CHANNEL,
            session_state_path=config.SESSION_STATE_PATH,
            browser_timeout_ms=config.BROWSER_TIMEOUT_MS,
            challenge_timeout_ms=config.CHALLENGE_TIMEOUT_MS,
            artifacts_dir=config.ARTIFACTS_DIR,
            mask_patterns=config.MASK_TEXT,
            masking_enabled=config.MASKING_ENABLED,
        )

    needs_recovery = result is None or result.fail_reason == "session_expired"

    if needs_recovery and main_down:
        return result if result is not None else _session_expired_result()

    if needs_recovery and not login_budget_state["used"]:
        if _login_budget_allows(conn):
            login_budget_state["used"] = True
            result = await _run_full_login(conn, should_logout=False)
        elif result is None:
            result = _session_expired_result()
        # else: budget says wait -- keep the cheap check's own session_expired result.
    elif result is None:
        # Budget already spent elsewhere this cycle and still no session -- report
        # plainly rather than silently doing nothing (Rule 6: every probe writes a row).
        result = _session_expired_result()

    return result


async def _run_auth_burst_reprobes(
    conn, channels, state: MonitorState, burst_start_ts: str, cycle_id: str,
    main_down: bool, login_budget_state: dict,
) -> tuple[MonitorState, "check.CheckResult", str]:
    """[v3.8 / Stage R] Auth-track confirmation burst: re-probes with the cheap
    session-reuse check at the same offsets as the main track -- zero logins per probe
    (Rule 11). A session_expired probe mid-burst is inert (Rule 17: doesn't count, burst
    stays open) and, per Rule 16, doesn't get its own recovery login if main is down or
    if this cycle's one budgeted login was already spent."""
    burst_id = str(uuid.uuid4())
    last_result = None
    start = datetime.fromisoformat(burst_start_ts)

    for delay_s in config.BURST_DELAYS_S[1:]:
        if state.status != "UP" or state.burst_started_ts != burst_start_ts:
            break  # already resolved: DOWN fired, or a pass cleared the burst

        jitter = random.uniform(-config.BURST_JITTER_S, config.BURST_JITTER_S)
        target_offset = max(0.0, delay_s + jitter)
        elapsed = (datetime.now(timezone.utc) - start).total_seconds()
        sleep_s = max(0.0, target_offset - elapsed)
        if sleep_s:
            await asyncio.sleep(sleep_s)

        result = await _run_auth_probe(conn, main_down, login_budget_state)
        last_result = result
        state = await _process_probe(
            conn, channels, state, result, _now_iso(), burst_id,
            browser_mode=config.JOURNEY_BROWSER_MODE, track="auth",
            down_confidence=config.AUTH_DOWN_CONFIDENCE, burst_window_s=config.BURST_WINDOW_S,
            min_failed_probes=config.AUTH_MIN_FAILED_PROBES, precursor_down=main_down, cycle_id=cycle_id,
        )

    return state, last_result, burst_id


def _cycle_fail_info(
    verdict: str, main_state: MonitorState, last_main_result, auth_state: MonitorState | None, last_auth_result,
) -> tuple[str | None, str | None]:
    """Rule 4: name the layer responsible for a non-UP verdict, with exact wording owned
    by the email/dashboard layer -- this just picks which track's evidence explains it."""
    if verdict == "UP":
        return None, None
    if main_state.status != "UP" and severity(main_state.status) >= severity(auth_state.status if auth_state else "UP"):
        fail_reason = main_state.fail_reasons[-1] if main_state.fail_reasons else (last_main_result.fail_reason if last_main_result else None)
        fail_layer = last_main_result.layer if last_main_result else "pulse"
        return fail_layer, fail_reason
    fail_reason = auth_state.fail_reasons[-1] if auth_state and auth_state.fail_reasons else (last_auth_result.fail_reason if last_auth_result else None)
    return "authed", fail_reason


async def run_cycle(conn, channels, auth_enabled: bool) -> None:
    """[v3.8 / Stage R] One unified cycle: the main track (pulse/render, with its
    confirmation burst) always runs; the auth track (cheap authed check, with its own
    burst and a budgeted recovery-login fallback) runs too, UNLESS its own status is
    already CONFIG_ERROR (Rule 10 -- needs a human) or the journey isn't configured at
    all. Both tracks' results are combined into one `cycles` row (Rule 16's unified
    verdict). A burst on either track extends this cycle's wall-clock time rather than
    running detached -- deliberate, per the human's call: reliable checks over a strict
    60s cadence."""
    cycle_id = str(uuid.uuid4())
    ts = _now_iso()

    # --- main track: pulse + render, with its confirmation burst ---
    prev_main_state = db.get_state(conn, track="main")
    prior_main_burst_ts = prev_main_state.burst_started_ts if prev_main_state.status == "UP" else None

    main_result = await check.perform_check(
        url=config.TARGET_URL,
        required_text=config.REQUIRED_TEXT,
        required_role=config.REQUIRED_ROLE,
        required_name=config.REQUIRED_NAME,
        browser_timeout_ms=config.BROWSER_TIMEOUT_MS,
        artifacts_dir=config.ARTIFACTS_DIR,
        headless=config.HEADLESS,
    )
    ts_main = _now_iso()
    new_main_state = await _process_probe(conn, channels, prev_main_state, main_result, ts_main, burst_id=None, cycle_id=cycle_id)
    last_main_result = main_result

    main_burst_id = None
    if new_main_state.status == "UP" and new_main_state.burst_started_ts == ts_main and prior_main_burst_ts != ts_main:
        new_main_state, burst_last_result, main_burst_id = await _run_burst_reprobes(
            conn, channels, new_main_state, burst_start_ts=ts_main, cycle_id=cycle_id
        )
        if burst_last_result is not None:
            last_main_result = burst_last_result

    main_down = new_main_state.status == "DOWN"

    # --- auth track: cheap authed check, with its own burst + budgeted recovery login ---
    new_auth_state = None
    last_auth_result = None
    authed_ok = None
    authed_latency_ms = None
    login_used_this_cycle = False
    auth_burst_id = None

    if auth_enabled:
        prev_auth_state = db.get_state(conn, track="auth")
        if prev_auth_state.status == "CONFIG_ERROR":
            print(f"[{_now_iso()}] [auth] skip -- track is CONFIG_ERROR ({prev_auth_state.fail_reasons}), needs a human")
            new_auth_state = prev_auth_state
        else:
            prior_auth_burst_ts = prev_auth_state.burst_started_ts if prev_auth_state.status == "UP" else None
            login_budget_state = {"used": False}

            auth_result = await _run_auth_probe(conn, main_down, login_budget_state)
            ts_auth = _now_iso()
            new_auth_state = await _process_probe(
                conn, channels, prev_auth_state, auth_result, ts_auth, burst_id=None,
                browser_mode=config.JOURNEY_BROWSER_MODE, track="auth",
                down_confidence=config.AUTH_DOWN_CONFIDENCE, burst_window_s=config.BURST_WINDOW_S,
                min_failed_probes=config.AUTH_MIN_FAILED_PROBES, precursor_down=main_down, cycle_id=cycle_id,
            )
            last_auth_result = auth_result

            if new_auth_state.status == "UP" and new_auth_state.burst_started_ts == ts_auth and prior_auth_burst_ts != ts_auth:
                new_auth_state, burst_last_auth_result, auth_burst_id = await _run_auth_burst_reprobes(
                    conn, channels, new_auth_state, burst_start_ts=ts_auth, cycle_id=cycle_id,
                    main_down=main_down, login_budget_state=login_budget_state,
                )
                if burst_last_auth_result is not None:
                    last_auth_result = burst_last_auth_result

            authed_ok = auth_result.ok
            authed_latency_ms = auth_result.latency_ms
            login_used_this_cycle = login_budget_state["used"]

    # --- unified verdict + cycles row (Rule 16) ---
    verdict = unified_verdict(new_main_state.status, new_auth_state.status if new_auth_state else None)
    fail_layer, fail_reason = _cycle_fail_info(verdict, new_main_state, last_main_result, new_auth_state, last_auth_result)

    # cycles.burst_id marks this minute as burst-confirmed so the dashboard's Burst column
    # and the CSV audit export can show it without drilling into `checks` (bursts are
    # first-class and unhideable). The column holds one id, so when both tracks bursted in
    # the same cycle it carries the one belonging to the track that explains the verdict --
    # the other track's burst is never lost, every probe in it is still tagged in `checks`
    # under this cycle_id. Until now this argument was simply never passed, so the column
    # was NULL on every row ever written and the dashboard's Burst badge could not fire.
    burst_id = auth_burst_id if fail_layer == "authed" and auth_burst_id else (main_burst_id or auth_burst_id)

    db.append_cycle(
        conn,
        cycle_id=cycle_id,
        ts=ts,
        pulse_ok=(main_result.layer != "pulse"),
        render_ok=(main_result.ok if main_result.layer == "render" else None),
        authed_ok=authed_ok,
        verdict=verdict,
        # session_reused: the authed check passed without spending this cycle's one
        # budgeted recovery login -- i.e. a cached session actually did the work.
        session_reused=bool(authed_ok and not login_used_this_cycle),
        pulse_latency_ms=main_result.pulse_latency_ms,
        render_latency_ms=main_result.render_latency_ms,
        authed_latency_ms=authed_latency_ms,
        fail_layer=fail_layer,
        fail_reason=fail_reason,
        burst_id=burst_id,
    )
    print(f"[{ts}] [cycle {cycle_id[:8]}] verdict={verdict}"
          + (f" ({fail_layer}: {fail_reason})" if fail_reason else "")
          + (f" burst={burst_id[:8]}" if burst_id else ""))


async def guarded_cycle(conn, channels, lock: asyncio.Lock, auth_enabled: bool) -> None:
    async with lock:
        await run_cycle(conn, channels, auth_enabled)


async def cycle_scheduler() -> None:
    """[v3.8 / Stage R] Replaces the old separate scheduler()/auth_scheduler() loops with
    one tick producing one platform verdict. auth_enabled is decided once at startup
    (same as the old auth_scheduler()'s guard) -- if LOGIN_URL/AUTHED_URL or the rest of
    Stage 6's config isn't set, the auth track simply never participates in any cycle,
    and the base pulse/render monitor still works standalone."""
    conn = db.get_connection(config.DB_PATH)
    db.init_db(conn)
    channels = build_channels(config.ALERT_CHANNELS)
    lock = asyncio.Lock()

    auth_enabled = False
    if config.LOGIN_URL:
        try:
            config.validate_stage6()
            auth_enabled = True
        except SystemExit as exc:
            print(f"[auth] Stage 6 config incomplete, auth track will not run: {exc}")
    else:
        print("[auth] LOGIN_URL not configured -- auth track will not run")

    if auth_enabled and config.ALLOW_MFA_UNCONFIGURED and not config.TOTP_SECRET:
        print("[auth] WARNING: ALLOW_MFA_UNCONFIGURED=true and no TOTP_SECRET set -- "
              "session-reuse checks will run normally, but if a recovery login is ever "
              "needed it cannot complete MFA and will report mfa_failed -> CONFIG_ERROR. "
              "Testing only -- set a real TOTP_SECRET before unattended production use.")

    while True:
        if lock.locked():
            print(f"[{_now_iso()}] skip cycle — previous cycle (or an in-progress burst) still running")
        else:
            asyncio.create_task(guarded_cycle(conn, channels, lock, auth_enabled))
        await asyncio.sleep(config.CHECK_INTERVAL_S)


async def run_web() -> None:
    server_config = uvicorn.Config("monitor.web:app", host="0.0.0.0", port=config.PORT, log_level="info")
    server = uvicorn.Server(server_config)
    await server.serve()


async def run_all() -> None:
    await asyncio.gather(cycle_scheduler(), run_web())


def main() -> None:
    config.validate_core()
    print(f"Monitoring {config.TARGET_NAME} ({config.TARGET_URL}) every {config.CHECK_INTERVAL_S}s")
    print(f"Dashboard on http://0.0.0.0:{config.PORT}")
    asyncio.run(run_all())


if __name__ == "__main__":
    main()
