"""[v3.8 / Stage R] Live drill runner: calls the exact same monitor.main.run_cycle() the
deployed monitor calls every CHECK_INTERVAL_S, against the real target, a few times in a
row. This is how Stage R's unified pulse/render/authed cycle gets exercised for real,
independent of the automatic cycle_scheduler() loop (which runs forever) -- one-shot,
supervised, for drilling.

Requires a session already captured (e.g. via `run_signin_drill.py --keep-session`,
which supports manual MFA and doesn't need a real TOTP_SECRET) -- this script never
attempts a fresh login itself if TOTP_SECRET isn't set; if the auth track's cheap check
needs a recovery login mid-drill and TOTP_SECRET is missing, that attempt will fail
loudly (submit_totp() has no code to enter) rather than silently -- expected, not a bug,
given a fresh session shouldn't need one.

Usage: .venv/bin/python -m scripts.run_live_cycle_drill [N]   # N cycles, default 3
"""
import asyncio
import sys

import config
from monitor import db
from monitor.channels import build_channels
from monitor.main import run_cycle


async def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 3

    conn = db.get_connection(config.DB_PATH)
    db.init_db(conn)
    channels = build_channels(config.ALERT_CHANNELS)

    auth_enabled = bool(config.LOGIN_URL and config.AUTHED_URL)
    if not auth_enabled:
        print("LOGIN_URL/AUTHED_URL not both set -- running main track (pulse/render) only.")
    if not config.TOTP_SECRET:
        print("No TOTP_SECRET set -- relying on an already-captured session "
              f"({config.SESSION_STATE_PATH}). If the cheap check finds it missing/stale, "
              "the recovery-login fallback will fail loudly (no code to enter) -- expected.")

    print(f"Running {n} live cycle(s) against {config.TARGET_URL} (auth_enabled={auth_enabled}) ...\n")
    for i in range(n):
        print(f"--- cycle {i + 1}/{n} ---")
        await run_cycle(conn, channels, auth_enabled)
        if i < n - 1:
            await asyncio.sleep(config.CHECK_INTERVAL_S)

    last_cycle = db.get_last_cycle(conn)
    print("\nFinal cycle row:", dict(last_cycle) if last_cycle else None)
    conn.close()


if __name__ == "__main__":
    asyncio.run(main())
