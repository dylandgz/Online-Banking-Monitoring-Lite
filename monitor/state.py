"""Pure state machine: (previous_state, check_result) -> (new_state, events). No I/O."""
from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass(frozen=True)
class MonitorState:
    status: str  # "UP" or "DOWN"
    consecutive_fails: int
    since_ts: Optional[str]  # UTC ISO-8601; when the current status began


@dataclass(frozen=True)
class DownEvent:
    since_ts: str
    consecutive_fails: int
    fail_reason: Optional[str]


@dataclass(frozen=True)
class RecoveryEvent:
    since_ts: str
    ended_at: str
    duration_s: int
    checks_failed: int


def apply_check(
    state: MonitorState,
    ok: bool,
    fail_reason: Optional[str],
    ts: str,
    fails_to_down: int,
) -> tuple[MonitorState, list]:
    """Advances the state machine by one check result. Emits an event only on a
    status transition (UP->DOWN or DOWN->UP) — never re-emits during an ongoing incident.
    """
    if ok:
        if state.status == "DOWN":
            duration_s = round((datetime.fromisoformat(ts) - datetime.fromisoformat(state.since_ts)).total_seconds())
            event = RecoveryEvent(
                since_ts=state.since_ts,
                ended_at=ts,
                duration_s=duration_s,
                checks_failed=state.consecutive_fails,
            )
            return MonitorState(status="UP", consecutive_fails=0, since_ts=ts), [event]
        return MonitorState(status="UP", consecutive_fails=0, since_ts=state.since_ts), []

    new_fails = state.consecutive_fails + 1
    if state.status == "UP" and new_fails >= fails_to_down:
        event = DownEvent(since_ts=ts, consecutive_fails=new_fails, fail_reason=fail_reason)
        return MonitorState(status="DOWN", consecutive_fails=new_fails, since_ts=ts), [event]
    return MonitorState(status=state.status, consecutive_fails=new_fails, since_ts=state.since_ts), []
