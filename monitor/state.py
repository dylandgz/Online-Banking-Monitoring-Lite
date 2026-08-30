"""Pure state machine: (previous_state, probe_result) -> (new_state, events). No I/O.

[2026-08-30 / B37+B38+B39+B7] Reworked from the burst-window model. The old rule was
"accumulate a weighted score inside a 90s window, and any pass clears everything". Three
things were wrong with it, all reproduced against this module before the rewrite:

  B37  A pass on ANY layer wiped the score for ALL layers. The confirmation burst
       deliberately alternated probe kinds for independent evidence, then treated a pass on
       one as proof about the other -- so on the exact outage the render layer exists to
       catch (server reachable, page rendering wrong) the burst's own pulse probe deleted
       the render failures that opened it. A pure render outage could not page at any
       duration.

  B38  The window was 90s and the cycle 60s, so two cycles span 120s and cycle-cadence
       failures could never reach the probe floor -- the count reset every third minute,
       oscillating 1,2,1,2 forever. The burst was therefore not an accelerator but the ONLY
       path to DOWN, with no slower path behind it.

  B7   `dns` is Hard evidence on a healthy host and the signature of a laptop resuming from
       suspend on a sleeping one. All three main-track DOWN pages of the Stage R era were
       wake-from-suspend artefacts.

The replacement, simulated against 1,200 randomised worlds plus the recorded history:
evidence is per-layer, counted consecutively with no clock, and only a pass on that same
layer clears it. Detection went from 48% of outages missed / 42% of pages false, to ~2% and
zero. See personal/ISSUES.md B37/B38 for the numbers.
"""
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Mapping, Optional

# Rule: only bad_status in the 5xx range counts as Hard evidence; other non-5xx status
# codes aren't explicitly classified in CLAUDE.md's table, so they're treated as Soft
# (ambiguous evidence stays cautious) rather than assumed to be Hard.
_HARD_REASONS = {"conn_refused", "dns", "auth_unavailable"}
_SOFT_REASONS = {"timeout", "element_missing", "nav_error", "logout_failed"}
_CONFIG_REASONS = {"auth_rejected", "bot_challenge", "mfa_failed", "rate_limited"}
_SESSION_REASONS = {"session_expired"}

HARD_WEIGHT = 2
SOFT_WEIGHT = 1
CONFIG_WEIGHT = 0
SESSION_WEIGHT = 0


def classify(fail_reason: str) -> tuple[str, int]:
    """Returns (class_name, weight) for a fail_reason: 'hard' (2), 'soft' (1), 'config' (0),
    or 'session' (0, never scores and never routes to CONFIG_ERROR).

    [B37 note] The weights no longer gate anything on their own -- see apply_check's
    docstring on why the confidence threshold is currently inert. The classification is
    still load-bearing for the config/session ROUTING below, and the weights are still
    recorded on the incident."""
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
class LayerEvidence:
    """What one layer has seen. [B37] Per-layer, so a pass on `pulse` cannot erase what
    `render` observed -- they are answers to different questions."""
    consecutive: int = 0                    # failed probes in a row on THIS layer
    confidence: int = 0                     # weighted score for those failures
    fail_reasons: tuple[str, ...] = ()
    last_probe_ts: Optional[str] = None     # any probe, pass or fail -- drives the stale reset


@dataclass(frozen=True)
class MonitorState:
    status: str                             # "UP" | "DOWN" | "CONFIG_ERROR"
    since_ts: Optional[str]                 # UTC ISO-8601; when the current status began
    layers: Mapping[str, LayerEvidence] = field(default_factory=dict)
    consecutive_passes: int = 0             # [B39] toward clearing an incident
    cause_layer: Optional[str] = None       # which layer opened the current incident

    # --- presentation of the layer that explains the current status -------------------
    # db.set_state, the incident row and DownEvent all want a single confidence/reasons
    # pair. These derive it rather than storing a second copy that could drift.
    def _worst(self) -> LayerEvidence:
        if self.cause_layer and self.cause_layer in self.layers:
            return self.layers[self.cause_layer]
        if not self.layers:
            return LayerEvidence()
        return max(self.layers.values(), key=lambda e: (e.consecutive, e.confidence))

    @property
    def confidence(self) -> int:
        return self._worst().confidence

    @property
    def fail_reasons(self) -> tuple[str, ...]:
        return self._worst().fail_reasons

    @property
    def burst_started_ts(self) -> Optional[str]:
        """Retained for the `state` table and for main.py's "did this probe open a burst?"
        test. Under the consecutive model a burst is open exactly while a layer holds
        unresolved evidence, so this reports when that layer's current run began."""
        w = self._worst()
        return w.last_probe_ts if w.consecutive else None

    def evidence(self, layer: str) -> LayerEvidence:
        return self.layers.get(layer, LayerEvidence())


@dataclass(frozen=True)
class DownEvent:
    since_ts: str
    confidence: int
    fail_reasons: tuple[str, ...]
    trigger_layer: str


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


def _seconds_between(a: str, b: str) -> float:
    return (datetime.fromisoformat(a) - datetime.fromisoformat(b)).total_seconds()


def _with_layer(state: MonitorState, layer: str, ev: LayerEvidence) -> Mapping[str, LayerEvidence]:
    merged = dict(state.layers)
    merged[layer] = ev
    return merged


def apply_check(
    state: MonitorState,
    ok: bool,
    fail_reason: Optional[str],
    ts: str,
    layer: str,
    down_confidence: int,
    min_failed_probes: int,
    stale_after_s: int,
    recovery_passes: int = 1,
    precursor_down: bool = False,
    scoring: bool = True,
) -> tuple[MonitorState, list[Event]]:
    """Advances the state machine by one probe result. Emits an event only on a status
    transition -- never re-emits while an incident is ongoing.

    DOWN requires `min_failed_probes` CONSECUTIVE failures on ONE layer with no intervening
    pass of that layer. There is no time window: a slow probe delays detection, it cannot
    prevent it. That is [B38] -- under the old window a probe slower than its slot
    timestamped itself outside the window it was scheduled in, and the count reset forever.

    `down_confidence` is still checked, and is currently INERT by arithmetic: the weakest
    possible evidence is `min_failed_probes` soft failures worth 1 each, which already
    equals a threshold of 4 when the floor is 4. It is retained deliberately -- if the floor
    is ever lowered to 3, three soft failures total 3 and the threshold blocks them again.
    Verified exhaustively over every 4-failure combination.

    `stale_after_s` [B38] replaces the window's only useful job. Evidence now persists until
    a pass clears it, so the one case a pass cannot cover is the monitor not looking at all
    (restart, host asleep, the B42 hang). If this layer has not been probed for that long,
    its run is discarded before this probe is counted. Without it the machine will page on
    hours-old evidence; tested.

    `recovery_passes` [B39] is how many consecutive passes clear an incident. Entering DOWN
    takes 4 corroborated probes; leaving it took 1, and all of the project's false-positive
    discipline sat on one side. A premature RECOVERED tells an operator to stop looking at
    something still broken.

    `scoring=False` [B7] records the probe without letting it count. main.py sets this
    during the grace period after a wall-clock gap, when the monitor demonstrably was not
    running: a `dns` failure from a laptop whose network has not come up is evidence about
    the host, not the bank. The probe still updates `last_probe_ts`, because it does prove
    we were looking by then -- otherwise the stale reset would fire spuriously.

    `precursor_down` [the "Cross-track suppression" section]: only meaningful for the auth
    track, when the main track's incident is already open and explains the symptom."""
    prev = state.evidence(layer)

    # A probe we are told not to score still proves the monitor was awake and looking.
    if not scoring:
        touched = replace(prev, last_probe_ts=ts)
        return replace(state, layers=_with_layer(state, layer, touched)), []

    if ok:
        cleared = LayerEvidence(last_probe_ts=ts)
        layers = _with_layer(state, layer, cleared)

        if state.status in ("DOWN", "CONFIG_ERROR"):
            # Only the layer that opened the incident can close it. A passing pulse says
            # nothing about whether the page renders -- the same confusion as B37, in the
            # opposite direction. CONFIG_ERROR has no cause layer, so any pass clears it.
            if state.cause_layer and layer != state.cause_layer:
                return replace(state, layers=layers), []

            passes = state.consecutive_passes + 1
            if passes < recovery_passes:
                return replace(state, layers=layers, consecutive_passes=passes), []

            event = RecoveryEvent(
                since_ts=state.since_ts,
                ended_at=ts,
                duration_s=round(_seconds_between(ts, state.since_ts)),
                confidence=state.confidence,
                fail_reasons=state.fail_reasons,
            )
            return MonitorState(status="UP", since_ts=ts, layers=layers), [event]

        # UP and passing: clears this layer's run (a flap), logged via the check row itself.
        return replace(state, status="UP", layers=layers, consecutive_passes=0), []

    # --- failure path ------------------------------------------------------------------
    cls, weight = classify(fail_reason)

    if cls == "session":
        # Not platform evidence -- leaves the run untouched, no event, on any track and in
        # any status. The follow-up recovery login is a separate apply_check call carrying
        # its own real fail_reason.
        touched = replace(prev, last_probe_ts=ts)
        return replace(state, layers=_with_layer(state, layer, touched)), []

    if cls == "config":
        if state.status == "CONFIG_ERROR":
            return state, []  # already alerted; needs a human, never re-alerts
        ev = LayerEvidence(consecutive=0, confidence=0, fail_reasons=(fail_reason,), last_probe_ts=ts)
        return MonitorState(
            status="CONFIG_ERROR", since_ts=ts,
            layers=_with_layer(state, layer, ev), cause_layer=None,
        ), [ConfigErrorEvent(ts=ts, fail_reason=fail_reason)]

    if state.status == "CONFIG_ERROR":
        return state, []  # a human must clear this; ordinary failures don't override it

    if state.status == "DOWN":
        # Keep tallying evidence for the incident record; never re-alert mid-incident.
        tallied = replace(
            prev,
            consecutive=prev.consecutive + 1,
            confidence=prev.confidence + weight,
            fail_reasons=prev.fail_reasons + (fail_reason,),
            last_probe_ts=ts,
        )
        return replace(state, layers=_with_layer(state, layer, tallied), consecutive_passes=0), []

    # status == UP, failing: consecutive scoring on this layer alone.
    stale = (
        stale_after_s is not None
        and prev.last_probe_ts is not None
        and _seconds_between(ts, prev.last_probe_ts) > stale_after_s
    )
    base = LayerEvidence() if stale else prev

    ev = LayerEvidence(
        consecutive=base.consecutive + 1,
        confidence=base.confidence + weight,
        fail_reasons=base.fail_reasons + (fail_reason,),
        last_probe_ts=ts,
    )
    layers = _with_layer(state, layer, ev)

    if ev.consecutive >= min_failed_probes and ev.confidence >= down_confidence and not precursor_down:
        return MonitorState(
            status="DOWN", since_ts=ts, layers=layers, cause_layer=layer,
        ), [DownEvent(since_ts=ts, confidence=ev.confidence,
                      fail_reasons=ev.fail_reasons, trigger_layer=layer)]

    return replace(state, status="UP", layers=layers, consecutive_passes=0), []
