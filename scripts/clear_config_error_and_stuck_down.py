"""Clears a track that has stopped recovering on its own, so it resumes checking.

Two states qualify, and they are not the same repair:

  CONFIG_ERROR  The track is skipped entirely each cycle (Rule 4 "never retry a credential
                rejection"), so it cannot self-clear. Only the `state` row needs resetting.
                This is what the predecessor script, `clear_config_error.py`, handled.

  DOWN          The track holds an incident it can never close. `apply_check` only lets a
                pass of the *causing* layer count toward recovery, so a DOWN whose cause
                layer can no longer be probed waits forever -- B50, observed live on
                2026-09-04 for 12 h 36 m while the authed check passed every single minute.
                Here the `state` row AND the open `incidents` row both need repair.

Closing the incident is not optional bookkeeping. `db.close_incident` picks the newest row
with `ended_at IS NULL` for the track, so an incident left open is closed by the *next* real
incident instead of itself, stamping it with the wrong duration and overwriting its
confidence and probe count (B3). Leaving it open also shows it as "(ongoing)" on the
dashboard forever.

WHY THIS IS DRY-RUN BY DEFAULT. The predecessor could only clear a CONFIG_ERROR, which is
not an outage. This one can clear a DOWN -- so a careless invocation can silence a real
outage. It therefore prints exactly what it would change and writes nothing until `--confirm`
is passed, and it refuses to guess which track you meant.

NOTHING AUTOMATED MAY EVER CALL THIS. Rule 4's halt exists precisely so the monitor cannot
retry its way into a lockout; this script is a person saying "I looked, I fixed the cause,
resume". It is not a retry mechanism. The same rule the predecessor carried.

The state is reset to UP with a clean slate -- no confidence, no fail_reasons, no in-progress
run -- rather than to some prior status. Stored evidence belongs to the run that ended in the
latch, and carrying it forward would let stale failures count toward a future DOWN. The next
real probe re-establishes the truth within one cycle.

ON `ended_at`. Never "now". Closing at repair time would record an outage lasting until the
moment a human happened to notice -- on 2026-09-04 that would have written 12 h 36 m for a
platform that recovered after 9 m 42 s. The script computes the honest value instead: the
moment `RECOVERY_PASSES` consecutive passes completed on the track after the incident opened,
which is what the state machine would have done by itself had the incident been keyed to a
layer that can pass. Override with `--ended-at` when you know better; the derivation is always
printed so you can judge it.

Usage:
    .venv312/bin/python -m scripts.clear_config_error_and_stuck_down --track auth
    .venv312/bin/python -m scripts.clear_config_error_and_stuck_down --track auth --confirm
    .venv312/bin/python -m scripts.clear_config_error_and_stuck_down --track auth --confirm \\
        --ended-at 2026-09-05T03:35:10.847645+00:00
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone

import config
from monitor import db
from monitor.state import MonitorState
from monitor.timeutil import now_iso, to_eastern

# Which layers each track can produce a probe on. Used only to derive the suggested
# `ended_at`. `auth` includes `render` because historic rows carry it: before B50 a login
# that failed before credentials were submitted was filed on the auth track's `render`
# layer, which is the very defect that produces most stuck DOWNs.
_TRACK_LAYERS = {"main": ("pulse", "render"), "auth": ("authed", "render")}


def _fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _age_since(ts: str | None) -> str:
    if not ts:
        return "unknown"
    return _fmt_duration((datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds())


def _when(ts: str | None) -> str:
    """Eastern for a human, tolerant of a state row with no since_ts."""
    return to_eastern(ts) if ts else "(no timestamp recorded)"


def _suggest_ended_at(conn, track: str, started_at: str, recovery_passes: int):
    """When did `recovery_passes` consecutive passes complete on this track after the
    incident opened? Returns (ts, note) or (None, why-not).

    Deliberately counts passes across all of the track's layers rather than the cause layer
    alone: a DOWN that needs this script is usually one whose cause layer cannot pass at all,
    so restricting to it would always return nothing. The derivation is printed rather than
    trusted silently -- this is a suggestion for a human, not an authority.

    [AppScan SQLi finding, 2026-09-22] The IN clause is fixed at two placeholders rather
    than joined to match len(layers). Both _TRACK_LAYERS entries are 2-tuples, so nothing
    was gained by generating it -- and this is the one flagged site with a genuine taint
    source rather than an assumed one: argparse reads sys.argv, which AppScan tracks, and it
    cannot see that `choices=("auth","main")` constrains `track` or that the dict lookup
    discards the value and keeps only the arity. The guard below is what the generated
    placeholders used to provide implicitly: a third layer on a track must fail loudly here
    instead of silently mismatching the parameter count."""
    layers = _TRACK_LAYERS.get(track, ())
    if len(layers) != 2:
        raise ValueError(f"track {track!r} does not have exactly two layers: {layers!r}")
    rows = conn.execute(
        "SELECT ts, ok, fail_reason FROM checks "
        "WHERE ts > ? AND layer IN (?, ?) ORDER BY id ASC",
        (started_at, *layers),
    ).fetchall()

    streak = 0
    for row in rows:
        # session_expired is inert under Rule 3: it neither counts as a pass nor breaks one.
        if row["fail_reason"] == "session_expired":
            continue
        if not row["ok"]:
            streak = 0
            continue
        streak += 1
        if streak >= recovery_passes:
            return row["ts"], f"{recovery_passes} consecutive passes completed here"
    return None, (f"no run of {recovery_passes} consecutive passes found on the {track} track "
                  f"since the incident opened")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Clear a track stuck in CONFIG_ERROR or DOWN. Human-only; never automate.")
    parser.add_argument("--track", required=True, choices=("auth", "main"),
                        help="which track to clear -- required, never guessed")
    parser.add_argument("--confirm", action="store_true",
                        help="actually write; without it this is a dry run")
    parser.add_argument("--ended-at",
                        help="explicit incident end (UTC ISO-8601); overrides the suggestion")
    args = parser.parse_args()
    track = args.track

    conn = db.get_connection(config.DB_PATH)
    db.init_db(conn)
    state = db.get_state(conn, track=track)

    if state.status == "UP":
        print(f"[{track}] status is UP -- nothing to clear.")
        conn.close()
        return

    print(f"[{track}] status is {state.status}, held since {_when(state.since_ts)} "
          f"({_age_since(state.since_ts)} ago)")
    print(f"[{track}]   cause layer : {state.cause_layer or '(none -- CONFIG_ERROR opens no incident)'}")
    print(f"[{track}]   reasons     : {', '.join(state.fail_reasons) or '(none recorded)'}")
    print(f"[{track}]   confidence  : {state.confidence}")

    incident = db.get_open_incident(conn, track=track)
    ended_at = duration_s = None
    if incident:
        started_at = incident["started_at"]
        if args.ended_at:
            ended_at, note = args.ended_at, "supplied with --ended-at"
        else:
            ended_at, note = _suggest_ended_at(conn, track, started_at, config.RECOVERY_PASSES)
        print(f"[{track}]   open incident #{incident['id']}, opened {_when(started_at)}, "
              f"trigger layer {incident['trigger_layer']}")
        if ended_at:
            duration_s = round((datetime.fromisoformat(ended_at)
                                - datetime.fromisoformat(started_at)).total_seconds())
            print(f"[{track}]   would close it at {_when(ended_at)} "
                  f"(duration {_fmt_duration(duration_s)}) -- {note}")
        else:
            print(f"[{track}]   CANNOT close it: {note}.")
            print(f"[{track}]   Re-run with --ended-at once you know when it actually recovered. "
                  f"Closing at 'now' would record an outage that did not happen.")
    elif state.status == "DOWN":
        print(f"[{track}]   no open incident row found -- only the state row needs resetting.")

    if not args.confirm:
        print(f"\n[{track}] DRY RUN -- nothing written. Re-run with --confirm to apply.")
        conn.close()
        return

    if incident and not ended_at:
        print(f"\n[{track}] refusing to clear: the incident is open and has no end time. "
              f"Supply --ended-at.")
        conn.close()
        return

    db.set_state(conn, MonitorState(status="UP", since_ts=now_iso(), layers={}), track=track)
    if incident and ended_at:
        # Parameterised, and pinned to this incident's id rather than "newest open for the
        # track" -- the caller has already been shown exactly which row this is.
        conn.execute("UPDATE incidents SET ended_at = ?, duration_s = ? WHERE id = ?",
                     (ended_at, duration_s, incident["id"]))
        conn.commit()
        print(f"[{track}] incident #{incident['id']} closed.")

    print(f"[{track}] now UP with a clean slate. The next cycle checks for real again.")
    print(f"[{track}] If the underlying cause is still broken this will re-open within "
          f"~{config.AUTH_MIN_FAILED_PROBES if track == 'auth' else config.MIN_FAILED_PROBES} "
          f"failed probes -- watch the log rather than assuming it is fixed.")
    conn.close()


if __name__ == "__main__":
    main()
