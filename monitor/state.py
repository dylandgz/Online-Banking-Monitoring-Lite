"""Pure state machine: (previous_state, probe_result) -> (new_state, events). No I/O.

Stage 5 rework: replaces the old "N consecutive fails" rule with a confirmation-burst /
confidence-score model (CLAUDE.md Rule 2 "alert only on transitions", Stage 5). Each failing probe is weighted by how
strong its evidence is; DOWN fires once the accumulated score within a burst reaches
DOWN_CONFIDENCE. A passing probe at any point clears the burst with no alert (a flap).
Config-class reasons (auth_rejected, bot_challenge, mfa_failed, rate_limited) bypass
scoring entirely and route straight to CONFIG_ERROR — see Rule 4 "never retry a credential rejection".

[v3.1] DOWN additionally requires at least MIN_FAILED_PROBES distinct failed probes in
the burst, not just a high enough score — a burst of two hard failures (score 4) no
longer pages on its own; it needs a third failed probe. This stops a single pair of
severe-but-brief probes from paging as fast as a slower, better-corroborated outage.

[v3.8 / Stage R] Two additions, both still pure:
- `session_expired` is its own class (weight 0, like config) but does NOT route to
  CONFIG_ERROR -- Rule 3 "session_expired never scores" says it "is not platform evidence" at all. It leaves the state
  completely untouched and emits no event; the recovery login CLAUDE.md describes as its
  follow-up is a separate, later apply_check() call carrying its own real fail_reason
  (or a pass), not something this function orchestrates.
- `precursor_down` (the "Cross-track suppression" section): when True, a failing call on the auth track that would
  otherwise cross into DOWN is suppressed -- confidence/fail_reasons/burst_started_ts
  still update normally (the evidence is real and keeps accumulating), but status is
  held at UP and no DownEvent fires, so the login-screen-down incident doesn't get a
  second, redundant page for a symptom it already explains. The suppressed evidence
  isn't lost: the next unsuppressed failing call re-evaluates threshold/floor against
  the accumulated state and will fire immediately if they're already met.
"""
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Optional

# Rule: only bad_status in the 5xx range counts as Hard evidence; other non-5xx status
# codes aren't explicitly classified in CLAUDE.md's table, so they're treated as Soft
# (ambiguous evidence stays cautious) rather than assumed to be Hard.
#
# The data-plane/API reasons (data_plane_missing, api_shape_mismatch, api_bad_status:) that
# used to appear here are gone: v3.8 removed data-plane/API probing from scope entirely, so
# nothing can ever produce them. Anything unrecognized still falls through to Soft below.
_HARD_REASONS = {"conn_refused", "dns", "auth_unavailable"}
_SOFT_REASONS = {"timeout", "element_missing", "nav_error"}
_CONFIG_REASONS = {"auth_rejected", "bot_challenge", "mfa_failed", "rate_limited"}
_SESSION_REASONS = {"session_expired"}

HARD_WEIGHT = 2
SOFT_WEIGHT = 1
CONFIG_WEIGHT = 0
SESSION_WEIGHT = 0


def classify(fail_reason: str) -> tuple[str, int]:
    """Returns (class_name, weight) for a fail_reason: 'hard' (2), 'soft' (1), 'config' (0),
    or 'session' (0, [v3.8] never scores and never routes to CONFIG_ERROR -- see Rule 3 "session_expired never scores")."""
    if fail_reason in _SESSION_REASONS:
        return "session", SESSION_WEIGHT
    if fail_reason in _CONFIG_REASONS:
        return "config", CONFIG_WEIGHT
    if fail_reason.startswith("bad_status:"):
        code = fail_reason.rsplit(":", 1)[-1]
        if code.isdigit() and 500 <= int(code) <= 599:
            return "hard", HARD_WEIGHT
        return "soft", SOFT_WEIGHT
    if fail_reason in _HARD_REASONS:
        return "hard", HARD_WEIGHT
    if fail_reason in _SOFT_REASONS:
        return "soft", SOFT_WEIGHT
    # Unrecognized reason: fail safe as ambiguous evidence rather than paging on it.
    return "soft", SOFT_WEIGHT


@dataclass(frozen=True)
class MonitorState:
    status: str  # "UP" | "DOWN" | "CONFIG_ERROR"
    since_ts: Optional[str]  # UTC ISO-8601; when the current status began
    burst_started_ts: Optional[str] = None  # None when no burst is in progress
    confidence: int = 0  # accumulated score of the in-progress (or most recently resolved) burst
    fail_reasons: tuple[str, ...] = ()  # fail reasons seen so far in the current burst/incident


@dataclass(frozen=True)
class DownEvent:
    since_ts: str
    confidence: int
    fail_reasons: tuple[str, ...]
    trigger_layer: str  # layer of the probe that pushed confidence over the threshold


@dataclass(frozen=True)
class RecoveryEvent:
    since_ts: str
    ended_at: str
    duration_s: int
    confidence: int
    fail_reasons: tuple[str, ...]


@dataclass(frozen=True)
class ConfigErrorEvent:
    ts: str
    fail_reason: str


Event = DownEvent | RecoveryEvent | ConfigErrorEvent


def apply_check(
    state: MonitorState,
    ok: bool,
    fail_reason: Optional[str],
    ts: str,
    layer: str,
    down_confidence: int,
    burst_window_s: int,
    min_failed_probes: int,
    precursor_down: bool = False,
) -> tuple[MonitorState, list[Event]]:
    """Advances the state machine by one probe result. Emits an event only on a status
    transition — never re-emits while an incident (DOWN or CONFIG_ERROR) is ongoing, and
    never mid-burst before the confidence threshold is crossed.

    precursor_down [v3.8, the "Cross-track suppression" section]: only meaningful for the auth track, when the main
    track's incident is already open and explains the symptom. Defaults to False, which
    keeps every existing (main-track) call site's behavior unchanged."""
    if ok:
        if state.status in ("DOWN", "CONFIG_ERROR"):
            duration_s = round((datetime.fromisoformat(ts) - datetime.fromisoformat(state.since_ts)).total_seconds())
            event = RecoveryEvent(
                since_ts=state.since_ts,
                ended_at=ts,
                duration_s=duration_s,
                confidence=state.confidence,
                fail_reasons=state.fail_reasons,
            )
            return MonitorState(status="UP", since_ts=ts), [event]
        # UP and passing: clears any in-progress burst (a flap) -- logged via the check
        # row itself, no event/alert.
        return MonitorState(status="UP", since_ts=state.since_ts), []

    # Failure path.
    cls, weight = classify(fail_reason)

    if cls == "session":
        # [v3.8 Rule 3 "session_expired never scores"] Not platform evidence -- leaves state and burst untouched, no
        # event, on any track and in any status. The follow-up recovery login (or its
        # absence, per the login budget) is a separate apply_check() call with its own
        # real fail_reason.
        return state, []

    if cls == "config":
        if state.status == "CONFIG_ERROR":
            return state, []  # already alerted; needs a human, never re-alerts
        event = ConfigErrorEvent(ts=ts, fail_reason=fail_reason)
        return MonitorState(status="CONFIG_ERROR", since_ts=ts, confidence=0, fail_reasons=(fail_reason,)), [event]

    if state.status == "CONFIG_ERROR":
        return state, []  # a human must clear this; ordinary failures don't override it

    if state.status == "DOWN":
        # Keep tallying total evidence for the incident record; never re-alert mid-incident.
        return replace(state, confidence=state.confidence + weight, fail_reasons=state.fail_reasons + (fail_reason,)), []

    # status == UP, failing: burst scoring.
    burst_active = state.burst_started_ts is not None and (
        (datetime.fromisoformat(ts) - datetime.fromisoformat(state.burst_started_ts)).total_seconds() <= burst_window_s
    )
    if burst_active:
        new_confidence = state.confidence + weight
        new_reasons = state.fail_reasons + (fail_reason,)
        burst_started_ts = state.burst_started_ts
    else:
        new_confidence = weight
        new_reasons = (fail_reason,)
        burst_started_ts = ts

    if new_confidence >= down_confidence and len(new_reasons) >= min_failed_probes and not precursor_down:
        event = DownEvent(since_ts=ts, confidence=new_confidence, fail_reasons=new_reasons, trigger_layer=layer)
        return MonitorState(status="DOWN", since_ts=ts, confidence=new_confidence, fail_reasons=new_reasons), [event]

    return MonitorState(
        status="UP", since_ts=state.since_ts,
        burst_started_ts=burst_started_ts, confidence=new_confidence, fail_reasons=new_reasons,
    ), []
