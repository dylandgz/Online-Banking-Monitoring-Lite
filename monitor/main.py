"""Composition root: runs the 60s check loop and uvicorn together in one asyncio process."""
import asyncio
from datetime import datetime, timezone

import uvicorn

import config
from monitor import check, db
from monitor.channels import build_channels, dispatch
from monitor.state import DownEvent, RecoveryEvent, apply_check


async def run_check_cycle(conn, channels) -> None:
    result = await check.perform_check(
        url=config.TARGET_URL,
        required_text=config.REQUIRED_TEXT,
        required_role=config.REQUIRED_ROLE,
        required_name=config.REQUIRED_NAME,
        browser_timeout_ms=config.BROWSER_TIMEOUT_MS,
        artifacts_dir=config.ARTIFACTS_DIR,
        headless=config.HEADLESS,
    )
    ts = datetime.now(timezone.utc).isoformat()
    db.append_check(
        conn,
        ts=ts,
        ok=result.ok,
        http_status=result.http_status,
        latency_ms=result.latency_ms,
        fail_reason=result.fail_reason,
        browser_mode=config.BROWSER_MODE,
    )
    status = "OK" if result.ok else f"FAIL ({result.fail_reason})"
    shot = f" screenshot={result.screenshot_path}" if result.screenshot_path else ""
    print(f"[{ts}] {status} status={result.http_status} latency={result.latency_ms:.0f}ms{shot}")

    prev_state = db.get_state(conn)
    new_state, events = apply_check(prev_state, result.ok, result.fail_reason, ts, config.FAILS_TO_DOWN)
    db.set_state(conn, new_state)

    for event in events:
        if isinstance(event, DownEvent):
            db.open_incident(conn, event.since_ts, event.consecutive_fails, result.screenshot_path)
        elif isinstance(event, RecoveryEvent):
            db.close_incident(conn, event.ended_at, event.duration_s, event.checks_failed)
        print(f"[alert] {event!r}")
        await dispatch(event, channels)


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
            print(f"[{datetime.now(timezone.utc).isoformat()}] skip cycle — previous check still running")
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
