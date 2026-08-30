"""Composition root: runs the unified 60s cycle loop and uvicorn together in one asyncio
process. [v3.8 / Stage R] One cycle = one platform verdict: the main track (pulse/render)
and the auth track (cheap authed-session check, budgeted recovery login) both run inside
a single tick, sharing a cycle_id, and are combined into one `cycles` summary row. See
CLAUDE.md's v3.8 amendment and its "Cross-track suppression" section for the reasoning."""
import asyncio
import random
import traceback
import uuid
from datetime import datetime, timedelta, timezone

import uvicorn

import config
from monitor import check, db, journey, session
from monitor.channels import build_channels, dispatch
from monitor.state import ConfigErrorEvent, DownEvent, MonitorState, RecoveryEvent, apply_check
from monitor.timeutil import now_iso
from monitor.verdict import severity, unified_verdict


async def _process_probe(
    conn, channels, prev_state: MonitorState, result, ts: str, burst_id: str | None,
    browser_mode: str | None = None,
    track: str = "main",
    down_confidence: int | None = None,
    min_failed_probes: int | None = None,
    precursor_down: bool = False,
    cycle_id: str | None = None,
    scoring: bool = True,
) -> MonitorState:
    """Writes the check row (Rule 15 "every probe writes a checks row" -- pass or fail), advances
    the pure state machine, persists it, and dispatches any resulting alert.

    browser_mode defaults to config.BROWSER_MODE (the pulse/render track's mode) --
    callers for other layers (the Stage 6 sign-in journey, which always runs headed per
    v3.3) must pass their own, so the audit trail doesn't misreport how the check
    actually ran.

    track/down_confidence/min_failed_probes default to the main track's
    config values -- the auth-track caller passes its own track name and thresholds.

    precursor_down [v3.8, the "Cross-track suppression" section]: only meaningful for the auth track, when the main
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
        # [B16/B35] The evidence this probe produced, recorded on the probe's own row.
        # screenshot_path used to survive only via open_incident(), i.e. only for the one
        # probe that crossed the DOWN threshold -- every other failure's image was written
        # to disk and its location discarded here.
        page_url=result.page_url,
        screenshot_path=result.screenshot_path,
    )
    status = "OK" if result.ok else f"FAIL ({result.fail_reason}, layer={result.layer})"
    shot = f" screenshot={result.screenshot_path}" if result.screenshot_path else ""
    print(f"[{ts}] [{track}] {status}{shot}")

    return await _advance_state(
        conn, channels, prev_state, result.ok, result.fail_reason, ts, result.layer,
        track=track, down_confidence=down_confidence, min_failed_probes=min_failed_probes,
        precursor_down=precursor_down, scoring=scoring, screenshot_path=result.screenshot_path,
    )


async def _advance_state(
    conn, channels, prev_state: MonitorState, ok: bool, fail_reason, ts: str, layer: str,
    track: str = "main",
    down_confidence: int | None = None,
    min_failed_probes: int | None = None,
    precursor_down: bool = False,
    scoring: bool = True,
    screenshot_path: str | None = None,
) -> MonitorState:
    """Advances the pure state machine, persists it, and dispatches any resulting alert.

    Split out of _process_probe [2026-08-30] because one `checks` row can represent two
    layer observations. `perform_check()` runs the pulse and then the render and reports
    layer="pulse" ONLY when the pulse failed -- so once the pulse is healthy it always
    reports "render", and under per-layer evidence (B37) the pulse layer would never receive
    a passing observation again. A pulse-caused incident could then never recover. run_cycle
    folds that implicit pulse pass in through this function, without writing a second row.
    Same insight as B9's _fold_combined_probe, applied to state rather than the cycles row."""
    new_state, events = apply_check(
        prev_state, ok, fail_reason, ts, layer,
        down_confidence if down_confidence is not None else config.DOWN_CONFIDENCE,
        min_failed_probes if min_failed_probes is not None else config.MIN_FAILED_PROBES,
        stale_after_s=config.EVIDENCE_STALE_AFTER_S,
        recovery_passes=config.RECOVERY_PASSES,
        precursor_down=precursor_down,
        scoring=scoring,
    )
    db.set_state(conn, new_state, track=track)

    for event in events:
        if isinstance(event, DownEvent):
            db.open_incident(
                conn, event.since_ts, event.confidence, event.trigger_layer,
                len(event.fail_reasons), screenshot_path, track=track,
            )
        elif isinstance(event, RecoveryEvent):
            db.close_incident(conn, event.ended_at, event.duration_s, event.confidence,
                               len(event.fail_reasons), track=track)
        elif isinstance(event, ConfigErrorEvent):
            pass  # not an outage -- no incident row, just the CONFIG alert below
        print(f"[alert] [{track}] {event!r}")
        await dispatch(event, channels)

    return new_state


def _fold_layer(outcomes: dict, layer: str, ok: bool) -> None:
    """[B9] Folds one probe's outcome into the cycle's per-layer summary.

    `cycles.pulse_ok` / `render_ok` answer "did every check of this kind pass this minute?"
    -- None when no check of that kind ran, False if any failed, True when at least one ran
    and all passed. A minute is the unit here, not a probe: a burst runs more probes after
    the one that opened it, and all of them belong to the same minute.

    Before this, both columns were derived from the cycle's FIRST probe and every burst
    probe was discarded from the summary. Worked example from the live DB, cycle fc32039c:
    the opening pulse failed (so render was correctly not attempted, render_ok NULL), the
    burst then ran a render probe that PASSED, and the row still reported render_ok NULL --
    "never measured" -- for a minute in which render was measured and passed."""
    previous = outcomes[layer]
    outcomes[layer] = ok if previous is None else (previous and ok)


def _fold_combined_probe(outcomes: dict, result: "check.CheckResult") -> None:
    """Folds perform_check()'s result, which evaluates two layers in one call. layer=="pulse"
    is returned only when the pulse itself failed -- render is then deliberately skipped, so
    it records no render outcome at all. layer=="render" means the pulse passed and render
    ran, whatever its verdict."""
    if result.layer == "pulse":
        _fold_layer(outcomes, "pulse", result.ok)
        return
    _fold_layer(outcomes, "pulse", True)
    _fold_layer(outcomes, "render", result.ok)


# [B7] Probes taken while the monitor demonstrably was not running are recorded but do not
# score. A laptop resuming from suspend produces `dns` failures that are evidence about the
# host, not the bank -- and all three main-track DOWN pages of the Stage R era were exactly
# that. Process-level, not per-layer: during a long burst one layer legitimately goes
# unprobed for minutes, which is the monitor working, not sleeping.
_GRACE: dict = {"until": None}


def _open_grace_if_process_gap(conn) -> None:
    """Called once per cycle. If more wall-clock passed since the newest recorded probe than
    the monitor could possibly have taken while running, it wasn't running -- a suspend, a
    restart, or a hung cycle (B42). Open a grace period."""
    last_ts = db.get_last_check_ts(conn)
    if last_ts is None:
        return
    gap = (datetime.now(timezone.utc) - datetime.fromisoformat(last_ts)).total_seconds()
    if gap > 2 * config.CHECK_INTERVAL_S:
        _GRACE["until"] = datetime.now(timezone.utc) + timedelta(seconds=config.WAKE_GRACE_S)
        print(f"[{now_iso()}] gap of {gap:.0f}s since the last probe -- the monitor was not "
              f"running. Probes are recorded but do not score for {config.WAKE_GRACE_S}s.")


def _scoring_now() -> bool:
    until = _GRACE["until"]
    return not (until is not None and datetime.now(timezone.utc) < until)


def _lift_grace_on_pass(ok: bool) -> None:
    """A passing probe is positive proof the host is healthy again, so the grace can end
    early. The WAKE_GRACE_S bound exists for the opposite case: if the monitor wakes INTO a
    real outage no probe ever passes, and 'suppress until a pass' would leave it blind
    indefinitely. Both halves are load-bearing; simulated."""
    if ok and _GRACE["until"] is not None:
        _GRACE["until"] = None


async def _wait_burst_gap() -> None:
    """[2026-08-30 / B38] Idle time between confirmation probes, measured from when the
    PREVIOUS probe finished -- not from an absolute offset within the burst.

    The old scheduler slept until `burst_start + BURST_DELAYS_S[i]`, which held the burst to
    a fixed footprint only while probes were fast enough to leave idle time. Once a probe
    ran longer than its slot the sleep clamped to zero, probes ran back to back, and the
    schedule became fiction -- and because the window was measured from the same origin, the
    last probe timestamped itself outside the window it was scheduled in. That is why the
    auth track could never page for a slow failure (B13).

    With a gap there is no schedule to fall behind: a slow probe delays the burst, it cannot
    corrupt it. The burst's footprint is now BURST_PROBES x (gap + probe duration), which is
    bounded by the probe's own timeout -- see B43 for the fact that nothing currently bounds
    that, which is what makes a worst-case burst long rather than infinite.

    Jitter is retained so that many monitors (or a restarted one) do not synchronise their
    re-probes onto the same instants."""
    jitter = random.uniform(-config.BURST_JITTER_S, config.BURST_JITTER_S)
    await asyncio.sleep(max(0.0, config.BURST_GAP_S + jitter))


async def _run_burst_reprobes(
    conn, channels, state: MonitorState, failed_layer: str, cycle_id: str, outcomes: dict,
) -> tuple[MonitorState, "check.CheckResult", str]:
    """Runs the confirmation burst inline: BURST_PROBES re-probes of THE LAYER THAT FAILED,
    each starting BURST_GAP_S after the previous one finished. Stops the moment the burst
    resolves -- DOWN fires, or a pass of that layer clears the run.

    [2026-08-30 / B37] The burst used to alternate render/pulse "so each is independent
    evidence rather than a correlated retry". That intent was sound and the implementation
    inverted it: the scorer then treated a pass on one layer as proof about the other, so on
    a "server reachable, page rendering wrong" outage the burst's own pulse probe deleted
    the render failures that opened it. Independence now comes from repetition over time on
    the layer under suspicion, which is the thing actually in doubt. Simulated: alternating
    with per-layer evidence is WORSE than either alone -- 69% of outages missed against the
    old design's 48% -- because no single layer accumulates. These two changes ship together
    or not at all.

    Running inline means the cycle's overlap-guard lock absorbs the next tick rather than
    double-probing mid-burst -- reliability over a strict 60s cadence.

    Returns the final state, the last probe's CheckResult (for the cycles row's fail_layer),
    and the burst_id every probe in this burst was tagged with."""
    burst_id = str(uuid.uuid4())
    last_result = None
    run_started = state.evidence(failed_layer).run_started_ts

    for _ in range(config.BURST_PROBES):
        if state.status != "UP" or state.evidence(failed_layer).run_started_ts != run_started:
            break  # already resolved: DOWN fired, or a pass cleared the run

        await _wait_burst_gap()

        if failed_layer == "pulse":
            result = await check.pulse_only_probe(config.TARGET_URL)
        else:
            result = await check.render_only_probe(
                config.TARGET_URL, config.REQUIRED_TEXT, config.REQUIRED_ROLE, config.REQUIRED_NAME,
                config.BROWSER_TIMEOUT_MS, config.ARTIFACTS_DIR, headless=config.HEADLESS,
            )

        last_result = result
        _fold_layer(outcomes, result.layer, result.ok)  # single-layer probe: its own layer only
        state = await _process_probe(conn, channels, state, result, now_iso(), burst_id,
                                     cycle_id=cycle_id, scoring=_scoring_now())

    return state, last_result, burst_id


async def _run_full_login(conn, *, should_logout: bool) -> "check.CheckResult":
    """One real login attempt (credentials + TOTP), saving the refreshed session on
    success. Always logged to login_events -- this is the real, budgeted action.

    "Always" is enforced by try/finally, and that matters more than it looks. Rule 5's
    login budget is a HARD limit, but _login_budget_allows() derives it entirely from the
    login_events table -- as of v3.9 that is the LOGIN_INTERVAL_S gap and nothing else
    (MAX_LOGINS_PER_DAY was removed), which makes this ledger the ONLY thing standing
    between a crashing journey and an unbounded login loop. Recording the
    attempt only on the success path therefore made the budget unenforceable in exactly
    the case it exists for: if run_journey raised (before the guards added alongside this,
    any unhandled Playwright error mid-journey did), no row was written, the ledger showed
    no attempt, and because the crash also happened before save_session_state the session
    never became fresh either -- so the next cycle, 60s later, judged the budget untouched
    and logged in again. Unbounded credentialed attempts against a real bank account,
    invisible in the audit trail. The ledger has to be written on the failure path
    precisely because that is the path that can loop."""
    result = None
    try:
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
        return result
    finally:
        # Nested try: a failure to WRITE the ledger must not mask whatever the login
        # actually did (and must not replace the original exception on the failure path).
        try:
            db.append_login_event(
                conn, ts=now_iso(),
                ok=bool(result and result.ok),
                latency_ms=result.latency_ms if result else 0.0,
                # No result at all means run_journey raised rather than reporting -- with
                # the journey-level guards in place that should now be unreachable, so
                # record it as its own distinct reason rather than a plausible-looking
                # browser fail_reason. It never reaches classify()/apply_check from here.
                reason=result.fail_reason if result else "internal_error",
                http_status=result.http_status if result else None,
                session_reused=False,
            )
        except Exception as exc:  # noqa: BLE001 -- last-resort audit write, never fatal
            print(f"[login] CRITICAL: could not record login attempt in login_events: {exc!r}", flush=True)


def _login_budget_allows(conn) -> bool:
    """Rule 5's hard limit, applied to the recovery path below: a minimum gap since the
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


async def _run_auth_probe(
    conn, main_down: bool, login_budget_state: dict
) -> tuple["check.CheckResult", bool]:
    """One auth-track probe attempt for this cycle. Cheap session-reuse check first
    (zero logins). session_expired (or no session at all) triggers exactly one budgeted
    recovery login per cycle (Rule 5 "the login budget is a hard limit") -- UNLESS the main track's precursor is already
    DOWN this cycle, in which case the recovery-login path is paused entirely: no budget
    is spent chasing a possibly-down site, and the session_expired result is reported
    as-is (harmless either way -- Rule 3 "session_expired never scores" makes session_expired inert to scoring on any
    track). Any *other* cheap-check failure (e.g. nav_error) is real evidence and is
    returned untouched -- no login involved, it feeds the burst directly.

    Returns (result, probed): `probed` is False when nothing actually contacted the
    platform this cycle -- no usable session AND the recovery login was either paused
    (the "Cross-track suppression" section) or refused by the budget (Rule 5 "the login budget is a hard limit"). The result in that case is a synthetic
    session_expired, which is inert by Rule 3 "session_expired never scores", so it is honest as a `checks` row (it
    records that we wanted to look and could not) but it is NOT an observation of the
    platform. [v3.9 / Stage H P2] Callers use `probed` to decide `cycles.authed_ok`:
    writing False there conflated "the authed layer failed" with "we never looked",
    contradicting the "Cross-track suppression" section's own wording that NULL means the track didn't run, and painting
    the dashboard's authed badge red on a completely healthy platform."""
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

    # The cheap check ran and produced a real observation if we got a result at all.
    probed = result is not None
    needs_recovery = result is None or result.fail_reason == "session_expired"

    if needs_recovery and main_down:
        # the "Cross-track suppression" section: recovery logins are paused while the precursor is DOWN. If the cheap
        # check itself ran and said session_expired, that IS a real observation (probed
        # stays True); if there was no session to check, nothing was observed.
        return (result, probed) if result is not None else (_session_expired_result(), False)

    if needs_recovery and not login_budget_state["used"]:
        if _login_budget_allows(conn):
            login_budget_state["used"] = True
            result = await _run_full_login(conn, should_logout=False)
            probed = True  # a real login attempt is very much an observation
        elif result is None:
            result = _session_expired_result()
        # else: budget says wait -- keep the cheap check's own session_expired result.
    elif result is None:
        # Budget already spent elsewhere this cycle and still no session -- report
        # plainly rather than silently doing nothing (Rule 15 "every probe writes a checks row").
        result = _session_expired_result()

    return result, probed


async def _run_auth_burst_reprobes(
    conn, channels, state: MonitorState, cycle_id: str,
    main_down: bool, login_budget_state: dict,
) -> tuple[MonitorState, "check.CheckResult", str]:
    """[v3.8 / Stage R] Auth-track confirmation burst: re-probes with the cheap
    session-reuse check, spaced by BURST_GAP_S like the main track -- zero logins per probe
    (Rule 5 "the login budget is a hard limit"). A session_expired probe mid-burst is inert (Rule 3 "session_expired never scores": doesn't count, burst
    stays open) and, per the "Cross-track suppression" section, doesn't get its own recovery login if main is down or
    if this cycle's one budgeted login was already spent."""
    burst_id = str(uuid.uuid4())
    last_result = None
    run_started = state.evidence("authed").run_started_ts

    for _ in range(config.BURST_PROBES):
        if state.status != "UP" or state.evidence("authed").run_started_ts != run_started:
            break  # already resolved: DOWN fired, or a pass cleared the run

        await _wait_burst_gap()

        # The burst only needs the result; whether it counted as a real observation
        # matters to the cycles row, which the initiating probe already decided.
        result, _probed = await _run_auth_probe(conn, main_down, login_budget_state)
        last_result = result
        state = await _process_probe(
            conn, channels, state, result, now_iso(), burst_id,
            browser_mode=config.JOURNEY_BROWSER_MODE, track="auth",
            down_confidence=config.AUTH_DOWN_CONFIDENCE,
            min_failed_probes=config.AUTH_MIN_FAILED_PROBES, precursor_down=main_down,
            cycle_id=cycle_id, scoring=_scoring_now(),
        )

    return state, last_result, burst_id


def _cycle_fail_info(
    verdict: str, main_state: MonitorState, last_main_result, auth_state: MonitorState | None, last_auth_result,
) -> tuple[str | None, str | None]:
    """Rule 10 "every DOWN names its layer": name the layer responsible for a non-UP verdict, with exact wording owned
    by the email/dashboard layer -- this just picks which track's evidence explains it.

    On an equal-severity tie (e.g. both tracks DOWN) this favors main -- it's the
    precursor, so its evidence is what an operator should look at first. Same tie-break,
    same rationale, as monitor/web/app.py's dashboard-badge logic; kept in both places
    since each derives fail_layer/fail_reason from different inputs (this one also needs
    the last CheckResult as a fallback when a track has no fail_reasons of its own yet)."""
    if verdict == "UP":
        return None, None
    if main_state.status != "UP" and severity(main_state.status) >= severity(auth_state.status if auth_state else "UP"):
        fail_reason = main_state.fail_reasons[-1] if main_state.fail_reasons else (last_main_result.fail_reason if last_main_result else None)
        # [B37] Prefer the layer that actually opened the incident. Taking it from the last
        # probe's layer mis-attributed every cycle during recovery: once the pulse is healthy
        # the combined probe reports layer="render", so a pulse-caused incident was logged as
        # "render: dns" -- a layer that was passing, paired with a reason it never produced.
        fail_layer = main_state.cause_layer or (last_main_result.layer if last_main_result else "pulse")
        return fail_layer, fail_reason
    fail_reason = auth_state.fail_reasons[-1] if auth_state and auth_state.fail_reasons else (last_auth_result.fail_reason if last_auth_result else None)
    return "authed", fail_reason


async def run_cycle(conn, channels, auth_enabled: bool, cycle_id: str | None = None) -> None:
    """[v3.8 / Stage R] One unified cycle: the main track (pulse/render, with its
    confirmation burst) always runs; the auth track (cheap authed check, with its own
    burst and a budgeted recovery-login fallback) runs too, UNLESS its own status is
    already CONFIG_ERROR (Rule 4 "never retry a credential rejection" -- needs a human) or the journey isn't configured at
    all. Both tracks' results are combined into one `cycles` row (the "Verdict" section's unified
    verdict). A burst on either track extends this cycle's wall-clock time rather than
    running detached -- deliberate, per the human's call: reliable checks over a strict
    60s cadence.

    cycle_id is normally supplied by guarded_cycle so that its failure handler can write
    the cycles row under the SAME id this cycle already tagged its checks rows with -- a
    generated-here id would leave those probe rows orphaned (a cycle_id with no cycles
    row), invisible to /api/history and the dashboard drill-down, which read cycles. It
    still defaults to generating one so run_cycle stays callable on its own."""
    cycle_id = cycle_id or str(uuid.uuid4())
    ts = now_iso()
    _open_grace_if_process_gap(conn)

    # --- main track: pulse + render, with its confirmation burst ---
    prev_main_state = db.get_state(conn, track="main")

    main_result = await check.perform_check(
        url=config.TARGET_URL,
        required_text=config.REQUIRED_TEXT,
        required_role=config.REQUIRED_ROLE,
        required_name=config.REQUIRED_NAME,
        browser_timeout_ms=config.BROWSER_TIMEOUT_MS,
        artifacts_dir=config.ARTIFACTS_DIR,
        headless=config.HEADLESS,
    )
    # [B9] Per-layer outcome for the whole minute, folded across the opening probe and every
    # burst re-probe -- not inferred from whichever probe happened to be first.
    layer_outcomes: dict = {"pulse": None, "render": None}
    _fold_combined_probe(layer_outcomes, main_result)

    ts_main = now_iso()

    # [B37] The combined probe observed TWO layers. layer=="render" means the pulse was
    # reached and passed; record that against the pulse layer before scoring the render
    # result, or the pulse layer only ever hears about its own failures and a pulse-caused
    # incident can never recover. No extra checks row -- the one row covers both legs.
    _lift_grace_on_pass(main_result.ok)
    scoring = _scoring_now()

    if main_result.layer == "render":
        prev_main_state = await _advance_state(
            conn, channels, prev_main_state, True, None, ts_main, "pulse", track="main",
            scoring=scoring)

    new_main_state = await _process_probe(conn, channels, prev_main_state, main_result, ts_main,
                                          burst_id=None, cycle_id=cycle_id, scoring=scoring)
    last_main_result = main_result

    # [B37] Confirm the layer that actually failed, and only if the failure produced
    # evidence -- session/config-class reasons open no run, so there is nothing to confirm.
    main_burst_id = None
    failed_layer = None if main_result.ok else main_result.layer
    if (failed_layer and new_main_state.status == "UP"
            and new_main_state.evidence(failed_layer).run_started_ts):
        new_main_state, burst_last_result, main_burst_id = await _run_burst_reprobes(
            conn, channels, new_main_state, failed_layer, cycle_id, outcomes=layer_outcomes,
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
            print(f"[{now_iso()}] [auth] skip -- track is CONFIG_ERROR ({prev_auth_state.fail_reasons}), needs a human")
            new_auth_state = prev_auth_state
        else:
            login_budget_state = {"used": False}

            auth_result, auth_probed = await _run_auth_probe(conn, main_down, login_budget_state)
            ts_auth = now_iso()
            new_auth_state = await _process_probe(
                conn, channels, prev_auth_state, auth_result, ts_auth, burst_id=None,
                browser_mode=config.JOURNEY_BROWSER_MODE, track="auth",
                down_confidence=config.AUTH_DOWN_CONFIDENCE,
                min_failed_probes=config.AUTH_MIN_FAILED_PROBES, precursor_down=main_down,
                cycle_id=cycle_id, scoring=scoring,
            )
            last_auth_result = auth_result

            if (new_auth_state.status == "UP"
                    and new_auth_state.evidence("authed").run_started_ts):
                new_auth_state, burst_last_auth_result, auth_burst_id = await _run_auth_burst_reprobes(
                    conn, channels, new_auth_state, cycle_id=cycle_id,
                    main_down=main_down, login_budget_state=login_budget_state,
                )
                if burst_last_auth_result is not None:
                    last_auth_result = burst_last_auth_result

            # [v3.9 / Stage H P2] NULL when nothing actually contacted the platform this
            # cycle, per the "Cross-track suppression" section -- see _run_auth_probe's `probed` return value.
            authed_ok = auth_result.ok if auth_probed else None
            authed_latency_ms = auth_result.latency_ms if auth_probed else None
            login_used_this_cycle = login_budget_state["used"]

    # --- unified verdict + cycles row (the "Verdict" section) ---
    verdict = unified_verdict(new_main_state.status, new_auth_state.status if new_auth_state else None)
    fail_layer, fail_reason = _cycle_fail_info(verdict, new_main_state, last_main_result, new_auth_state, last_auth_result)

    # cycles.burst_id marks this minute as burst-confirmed so the dashboard's Burst column
    # and the CSV audit export can show it without drilling into `checks` (bursts are
    # first-class and unhideable). The column holds one id, so when both tracks bursted in
    # the same cycle it carries the one belonging to the track that explains the verdict --
    # the other track's burst is never lost, every probe in it is still tagged in `checks`
    # under this cycle_id. Until now this argument was simply never passed, so the column
    # was NULL on every row ever written and the dashboard's Burst badge could not fire.
    # Precedence, spelled out: authed-caused verdict + an auth burst happened -> auth_burst_id;
    # otherwise prefer main_burst_id if a main burst happened; otherwise fall back to
    # auth_burst_id (covers a precursor-caused verdict where only the auth track bursted --
    # e.g. an absorbed auth failure per the "Cross-track suppression" section); otherwise None (no burst ran either track).
    burst_id = auth_burst_id if fail_layer == "authed" and auth_burst_id else (main_burst_id or auth_burst_id)

    db.append_cycle(
        conn,
        cycle_id=cycle_id,
        ts=ts,
        # [B9] Measured across every main-track probe in this cycle, not inferred from the
        # first one. Latency below deliberately still reports the opening probe's timing --
        # "the latency of a minute containing four probes" is a separate question with no
        # obvious answer (first? worst? mean?) and is not what B9 asked.
        pulse_ok=layer_outcomes["pulse"],
        render_ok=layer_outcomes["render"],
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
    """Overlap guard, plus the self-health net that keeps a crashed cycle from vanishing.

    Why the net exists: cycle_scheduler launches this with asyncio.create_task and never
    awaits it, so before this handler an unexpected exception anywhere in run_cycle
    (unmapped browser error, `database is locked` from a concurrent CSV export, corrupt
    session file) surfaced only as asyncio's "Task exception was never retrieved" on
    stderr. The minute then had no checks row, no cycles row, no state advance and no
    alert -- for a monitor, silently missing a minute during an outage is the worst
    possible failure, since a quiet dashboard reads as "the bank is fine".

    The recorded verdict is DEGRADED, deliberately: Rule 7 "only DOWN pages" guarantees DEGRADED never
    pages, and a bug in the monitor is not evidence about the bank -- inventing a DOWN
    here would manufacture exactly the false positive this project ranks above all other
    concerns. Note this row is written directly, NOT through apply_check(), so it cannot
    touch either track's state or contribute to any burst's confidence or probe floor.

    [v3.9 / Stage H P2] DEGRADED minutes do NOT count against uptime: uptime_pct's
    denominator is verdict IN ('UP','DOWN'), so CONFIG_ERROR and DEGRADED are excluded from
    both sides. A self-health row means "we could not measure", and an unmeasurable minute
    is not a minute of downtime -- reporting it as one would describe an outage that never
    happened, the same class of lie the latched-CONFIG_ERROR 0%-uptime bug produced. The
    accepted consequence is that a window containing only excluded verdicts reports "n/a"
    rather than 100%; the banner names the CONFIG_ERROR/DEGRADED state next to it."""
    async with lock:
        cycle_id = str(uuid.uuid4())
        try:
            await run_cycle(conn, channels, auth_enabled, cycle_id=cycle_id)
        except Exception:  # noqa: BLE001 -- deliberate catch-all; see docstring
            traceback.print_exc()
            try:
                db.append_cycle(
                    conn, cycle_id=cycle_id, ts=now_iso(),
                    pulse_ok=None, render_ok=None, authed_ok=None,
                    verdict="DEGRADED", fail_layer="monitor", fail_reason="internal_error",
                )
                print(f"[{now_iso()}] [cycle {cycle_id[:8]}] verdict=DEGRADED "
                      f"(monitor: internal_error) -- cycle failed, see traceback above", flush=True)
            except Exception as exc:  # noqa: BLE001 -- the DB may be the thing that broke
                print(f"[{now_iso()}] CRITICAL: cycle failed AND its DEGRADED marker row "
                      f"could not be written: {exc!r}", flush=True)


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
            print(f"[{now_iso()}] skip cycle — previous cycle (or an in-progress burst) still running")
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
