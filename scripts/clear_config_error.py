"""Clears a stuck CONFIG_ERROR so the track resumes checking -- the "human clears it"
half of Rule 4 "never retry a credential rejection", which until now had no implementation (CLAUDE.md backlog: "the only way
to clear a stuck track is a manual DB edit or a passing manual drill run").

Rule 4 "never retry a credential rejection" is deliberate: a credential rejection, bot challenge or MFA failure halts logins
until a person has looked at it, precisely so the monitor can never retry its way into a
lockout. This script is that person saying "I looked, I fixed the cause, resume" -- it is
not a retry mechanism, and nothing automated may ever call it.

Resets the track to UP with a clean slate (no confidence, no fail_reasons, no in-progress
burst) rather than to its pre-error status: the stored confidence belongs to the burst
that ended in the config failure, and carrying it forward would let stale evidence count
toward a future DOWN. The next real probe re-establishes the truth within one cycle, and
because CONFIG_ERROR never opens an incident row there is nothing else to close.

Usage: .venv/bin/python -m scripts.clear_config_error [--track auth|main]   (default auth)
"""
import sys
from datetime import datetime, timezone

import config
from monitor import db
from monitor.state import MonitorState


def main() -> None:
    track = "auth"
    if "--track" in sys.argv:
        track = sys.argv[sys.argv.index("--track") + 1]
    if track not in ("auth", "main"):
        raise SystemExit(f"Unknown track {track!r} -- expected 'auth' or 'main'.")

    conn = db.get_connection(config.DB_PATH)
    db.init_db(conn)
    current = db.get_state(conn, track=track)

    if current.status != "CONFIG_ERROR":
        print(f"[{track}] status is {current.status}, not CONFIG_ERROR -- nothing to clear.")
        conn.close()
        return

    print(f"[{track}] clearing CONFIG_ERROR (reasons: {', '.join(current.fail_reasons) or 'none'}, "
          f"since {current.since_ts})")
    db.set_state(
        conn,
        MonitorState(
            status="UP",
            since_ts=datetime.now(timezone.utc).isoformat(),
            burst_started_ts=None,
            confidence=0,
            fail_reasons=(),
        ),
        track=track,
    )
    conn.close()
    print(f"[{track}] now UP. The next cycle will attempt a real check again -- watch the "
          f"log to confirm the underlying cause is actually fixed.")


if __name__ == "__main__":
    main()
