"""Composition root: runs the 60s check loop and uvicorn together in one asyncio process."""
import asyncio
import itertools
import random
import uuid
from datetime import datetime, timezone

import uvicorn

import config
from monitor import check, db
from monitor.channels import build_channels, dispatch
from monitor.state import ConfigErrorEvent, DownEvent, MonitorState, RecoveryEvent, apply_check


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _process_probe(conn, channels, prev_state: MonitorState, result, ts: str, burst_id: str | None) -> MonitorState:
    """Writes the check row (Rule 6: every probe writes its own row, pass or fail), advances
    the pure state machine, persists it, and dispatches any resulting alert."""
    db.append_check(
        conn,
        ts=ts,
        ok=result.ok,
        http_status=result.http_status,
        latency_ms=result.latency_ms,
        fail_reason=result.fail_reason,
        browser_mode=config.BROWSER_MODE,
        layer=result.layer,
        burst_id=burst_id,
    )
    status = "OK" if result.ok else f"FAIL ({result.fail_reason}, layer={result.layer})"
    shot = f" screenshot={result.screenshot_path}" if result.screenshot_path else ""
    print(f"[{ts}] {status}{shot}")

    new_state, events = apply_check(
        prev_state, result.ok, result.fail_reason, ts, result.layer,
        config.DOWN_CONFIDENCE, config.BURST_WINDOW_S, config.MIN_FAILED_PROBES,
    )
    db.set_state(conn, new_state)

    for event in events:
        if isinstance(event, DownEvent):
            db.open_incident(
                conn, event.since_ts, event.confidence, event.trigger_layer,
                len(event.fail_reasons), result.screenshot_path,
            )
        elif isinstance(event, RecoveryEvent):
            db.close_incident(conn, event.ended_at, event.duration_s, event.confidence, len(event.fail_reasons))
        elif isinstance(event, ConfigErrorEvent):
            pass  # not an outage -- no incident row, just the CONFIG alert below
        print(f"[alert] {event!r}")
        await dispatch(event, channels)

    return new_state


async def _run_burst_reprobes(conn, channels, state: MonitorState, burst_start_ts: str) -> None:
    """Runs the rest of the confirmation burst inline (Stage 5): re-probes at the remaining
    BURST_DELAYS_S offsets, varying the probe (render, then pulse) so each is independent
    evidence rather than a correlated retry. Stops the moment the burst resolves -- DOWN
    fires, or a pass clears it. Running this inline (not as a separate task) means the
    scheduler's overlap-guard lock naturally absorbs the next 60s tick instead of
    double-probing while a burst is in flight."""
    burst_id = str(uuid.uuid4())
    # index 0 (the check that started this burst) already ran; cycle render/pulse for the
    # rest of BURST_DELAYS_S so a 4-probe burst (v3.1 default) still varies its evidence.
    probe_kinds = list(itertools.islice(itertools.cycle(["render", "pulse"]), len(config.BURST_DELAYS_S) - 1))
    start = datetime.fromisoformat(burst_start_ts)

    for kind, delay_s in zip(probe_kinds, config.BURST_DELAYS_S[1:]):
        if state.status != "UP" or state.burst_started_ts != burst_start_ts:
            return  # already resolved: DOWN fired, or a pass cleared the burst

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

        state = await _process_probe(conn, channels, state, result, _now_iso(), burst_id)


async def run_check_cycle(conn, channels) -> None:
    prev_state = db.get_state(conn)
    prior_burst_ts = prev_state.burst_started_ts if prev_state.status == "UP" else None

    result = await check.perform_check(
        url=config.TARGET_URL,
        required_text=config.REQUIRED_TEXT,
        required_role=config.REQUIRED_ROLE,
        required_name=config.REQUIRED_NAME,
        browser_timeout_ms=config.BROWSER_TIMEOUT_MS,
        artifacts_dir=config.ARTIFACTS_DIR,
        headless=config.HEADLESS,
    )
    ts = _now_iso()
    new_state = await _process_probe(conn, channels, prev_state, result, ts, burst_id=None)

    # A fresh burst just started exactly on this tick's probe -- run the rest of it inline.
    if new_state.status == "UP" and new_state.burst_started_ts == ts and prior_burst_ts != ts:
        await _run_burst_reprobes(conn, channels, new_state, burst_start_ts=ts)


async def guarded_check(conn, channels, lock: asyncio.Lock) -> None:
    async with lock:
        await run_check_cycle(conn, channels)


async def scheduler() -> None:
    conn = db.get_connection(config.DB_PATH)
    db.init_db(conn)
    channels = build_channels(config.ALERT_CHANNELS)
    lock = asyncio.Lock()

    while True:
        if lock.locked():
            print(f"[{_now_iso()}] skip cycle — previous check (or an in-progress burst) still running")
        else:
            asyncio.create_task(guarded_check(conn, channels, lock))
        await asyncio.sleep(config.CHECK_INTERVAL_S)


async def run_web() -> None:
    server_config = uvicorn.Config("monitor.web:app", host="0.0.0.0", port=config.PORT, log_level="info")
    server = uvicorn.Server(server_config)
    await server.serve()


async def run_all() -> None:
    await asyncio.gather(scheduler(), run_web())


def main() -> None:
    config.validate_core()
    print(f"Monitoring {config.TARGET_NAME} ({config.TARGET_URL}) every {config.CHECK_INTERVAL_S}s")
    print(f"Dashboard on http://0.0.0.0:{config.PORT}")
    asyncio.run(run_all())


if __name__ == "__main__":
    main()
