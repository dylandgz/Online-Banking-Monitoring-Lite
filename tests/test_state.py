"""The DOWN decision, after the 2026-08-30 rework (B37 / B38 / B39 / B7).

The model changed, so these tests changed with it. What replaced what:

  old: a weighted score accumulated inside a 90s window, per TRACK, cleared by any pass
  new: consecutive failures counted per LAYER, no clock, cleared only by a pass of that
       same layer or by the staleness guard

Every assertion below either pins a rule CLAUDE.md publishes, or pins a defect that was
reproduced against the old machine before the rework. The three headline ones are
test_b37_*, test_b38_* and test_b7_*.
"""
from monitor.state import (
    ConfigErrorEvent, DownEvent, LayerEvidence, MonitorState, RecoveryEvent,
    apply_check, classify,
)

DOWN_CONFIDENCE = 4
FLOOR = 4
STALE = 600
RECOVERY = 3

UP = MonitorState(status="UP", since_ts=None)


def step(state, ok, reason, ts, layer="render", **kw):
    kw.setdefault("recovery_passes", 1)
    return apply_check(state, ok, reason, ts, layer,
                       DOWN_CONFIDENCE, FLOOR, STALE, **kw)


def run(state, probes, **kw):
    """probes: list of (ok, reason, ts, layer)"""
    events = []
    for ok, reason, ts, layer in probes:
        state, ev = step(state, ok, reason, ts, layer, **kw)
        events += ev
    return state, events


def T(seconds):
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    return f"2026-01-01T{h:02d}:{m:02d}:{s:02d}+00:00"


def downs(events):
    return [e for e in events if isinstance(e, DownEvent)]


# --- classification (unchanged by the rework) ---------------------------------------

def test_classify_hard_reasons():
    for r in ("dns", "conn_refused", "bad_status:503", "auth_unavailable"):
        assert classify(r) == ("hard", 2), r


def test_classify_soft_reasons():
    for r in ("timeout", "element_missing", "nav_error", "bad_status:404"):
        assert classify(r) == ("soft", 1), r


def test_classify_config_reasons():
    for r in ("auth_rejected", "bot_challenge", "mfa_failed", "rate_limited"):
        assert classify(r) == ("config", 0), r


def test_classify_unknown_reason_defaults_soft():
    # Ambiguous evidence stays cautious rather than paging.
    assert classify("something_new_from_a_future_probe") == ("soft", 1)


def test_classify_session_expired_is_its_own_zero_weight_class():
    assert classify("session_expired") == ("session", 0)


def test_logout_failed_is_explicitly_soft_not_a_fallback():
    # [B21] It used to reach Soft only via the "anything unrecognized" catch-all.
    assert classify("logout_failed") == ("soft", 1)


# --- the DOWN rule ------------------------------------------------------------------

def test_four_consecutive_failures_page_on_the_fourth():
    state, events = run(UP, [(False, "element_missing", T(i * 25), "render") for i in range(4)])
    assert state.status == "DOWN"
    assert len(downs(events)) == 1
    assert downs(events)[0].trigger_layer == "render"
    assert state.evidence("render").consecutive == 4


def test_three_consecutive_failures_do_not_page():
    state, events = run(UP, [(False, "dns", T(i * 25), "pulse") for i in range(3)])
    assert state.status == "UP", "the floor is 4 probes, however severe the evidence"
    assert downs(events) == []


def test_hard_evidence_does_not_page_faster_than_soft():
    """Deliberate. Hard-class is `dns`, and `dns` is exactly what a laptop resuming from
    suspend produces (B7). Letting severity shortcut the floor would reopen that."""
    hard, _ = run(UP, [(False, "dns", T(i * 25), "pulse") for i in range(3)])
    soft, _ = run(UP, [(False, "timeout", T(i * 25), "pulse") for i in range(3)])
    assert hard.status == soft.status == "UP"


def test_one_pass_clears_the_run():
    state, events = run(UP, [
        (False, "element_missing", T(0), "render"),
        (False, "element_missing", T(25), "render"),
        (True, None, T(50), "render"),
        (False, "element_missing", T(75), "render"),
    ])
    assert state.evidence("render").consecutive == 1, "the pass reset the run to zero"
    assert downs(events) == []


def test_confidence_threshold_is_inert_at_this_floor():
    """With floor 4 the weakest possible evidence -- four soft failures worth 1 each -- already
    totals 4, so a threshold of 4 can never block. Verified exhaustively over every
    4-failure combination before the rework."""
    state, _ = run(UP, [(False, "timeout", T(i * 25), "render") for i in range(4)])
    assert state.status == "DOWN", "four of the weakest failures still page"


def test_confidence_threshold_re_arms_if_the_floor_is_lowered():
    """Why the check is retained rather than deleted. At floor 3, three soft failures total
    3 and the threshold of 4 blocks them -- so the score becomes a live guard again the
    moment someone weakens the floor."""
    state = MonitorState(status="UP", since_ts=None)
    for i in range(3):
        state, _ = apply_check(state, False, "timeout", T(i * 25), "render",
                               4, 3, STALE)          # threshold 4, floor 3
    assert state.status == "UP", "floor met at 3 probes, but 3 points < threshold 4"

    state = MonitorState(status="UP", since_ts=None)
    for i in range(3):
        state, _ = apply_check(state, False, "dns", T(i * 25), "pulse",
                               4, 3, STALE)          # same floor, harder evidence
    assert state.status == "DOWN", "3 hard failures total 6 and clear the threshold"


# --- B37: evidence is per layer ------------------------------------------------------

def test_b37_a_passing_pulse_does_not_erase_render_evidence():
    """THE defect. The burst alternates probe kinds for independent evidence, then the old
    scorer treated a pass on one as proof about the other -- so on a "server reachable, page
    broken" outage the burst's own pulse probe deleted the render failures that opened it,
    and a pure render outage could not page at any duration."""
    state, events = run(UP, [
        (False, "element_missing", T(0), "render"),
        (False, "element_missing", T(25), "render"),
        (True, None, T(50), "pulse"),            # the server is fine -- of course it is
        (False, "element_missing", T(75), "render"),
        (False, "element_missing", T(100), "render"),
    ])
    assert state.status == "DOWN"
    assert downs(events)[0].trigger_layer == "render"


def test_b37_layers_accumulate_independently():
    state, _ = run(UP, [
        (False, "dns", T(0), "pulse"),
        (False, "element_missing", T(25), "render"),
        (False, "dns", T(50), "pulse"),
    ])
    assert state.evidence("pulse").consecutive == 2
    assert state.evidence("render").consecutive == 1
    assert state.status == "UP", "neither layer alone has reached the floor"


def test_b37_only_the_causing_layer_can_recover_the_incident():
    state, _ = run(UP, [(False, "element_missing", T(i * 25), "render") for i in range(4)])
    assert state.status == "DOWN" and state.cause_layer == "render"
    state, events = step(state, True, None, T(200), "pulse")
    assert state.status == "DOWN", "a passing pulse says nothing about whether the page renders"
    assert not [e for e in events if isinstance(e, RecoveryEvent)]


# --- B38: no window ------------------------------------------------------------------

def test_b38_slow_probes_still_reach_the_floor():
    """The old 90s window reset the count whenever probes were slower than their slots, so
    the auth track could never page (B13). Four failures 400s apart now still count."""
    state, events = run(UP, [(False, "element_missing", T(i * 400), "authed") for i in range(4)])
    assert state.status == "DOWN"
    assert len(downs(events)) == 1


def test_b38_cycle_cadence_failures_accumulate():
    """Two cycles span 120s, wider than the old 90s window, so cycle-cadence failures used
    to oscillate 1,2,1,2 forever and never reach the floor."""
    state, _ = run(UP, [(False, "nav_error", T(i * 60), "render") for i in range(4)])
    assert state.status == "DOWN"


# --- the staleness guard -------------------------------------------------------------

def test_stale_evidence_is_discarded_before_counting():
    """Without a window, only a pass clears evidence -- and a pass cannot happen while the
    monitor is not looking. Three failures, then a long silence, then one more must NOT
    complete the run."""
    state, _ = run(UP, [(False, "element_missing", T(i * 25), "render") for i in range(3)])
    assert state.evidence("render").consecutive == 3
    state, events = step(state, False, "element_missing", T(7200), "render")
    assert state.status == "UP", "must not page on two-hour-old evidence"
    assert state.evidence("render").consecutive == 1


def test_evidence_just_inside_the_stale_bound_still_counts():
    state, _ = run(UP, [(False, "element_missing", T(i * 25), "render") for i in range(3)])
    state, events = step(state, False, "element_missing", T(50 + STALE - 10), "render")
    assert state.status == "DOWN", "still observing, just slowly"


# --- B7: probes taken while the monitor was not running ------------------------------

def test_b7_non_scoring_probes_cannot_page():
    """A laptop resuming from suspend produces `dns` failures that are evidence about the
    host, not the bank. All three main-track DOWN pages of the Stage R era were this."""
    state, events = run(UP, [(False, "dns", T(i * 25), "pulse") for i in range(6)],
                        scoring=False)
    assert state.status == "UP"
    assert downs(events) == []


def test_b7_non_scoring_probes_still_prove_we_were_looking():
    """They must update last_probe_ts, or the staleness guard would fire spuriously the
    moment scoring resumes."""
    state, _ = step(UP, False, "dns", T(0), "pulse", scoring=False)
    assert state.evidence("pulse").last_probe_ts == T(0)
    assert state.evidence("pulse").consecutive == 0


def test_b7_scoring_resumes_cleanly_after_the_grace_period():
    """A real outage that begins at a wake is delayed, never lost."""
    state, _ = run(UP, [(False, "dns", T(i * 25), "pulse") for i in range(3)], scoring=False)
    assert state.status == "UP"
    state, events = run(state, [(False, "dns", T(100 + i * 25), "pulse") for i in range(4)])
    assert state.status == "DOWN", "once the grace lifts, the outage is caught normally"


# --- B39: recovery hysteresis --------------------------------------------------------

def test_b39_one_pass_does_not_clear_an_incident():
    state, _ = run(UP, [(False, "element_missing", T(i * 25), "render") for i in range(4)])
    state, events = step(state, True, None, T(200), "render", recovery_passes=RECOVERY)
    assert state.status == "DOWN"
    assert not [e for e in events if isinstance(e, RecoveryEvent)]


def test_b39_three_passes_clear_it_and_emit_exactly_one_recovery():
    state, _ = run(UP, [(False, "element_missing", T(i * 25), "render") for i in range(4)])
    events = []
    for i in range(3):
        state, ev = step(state, True, None, T(200 + i * 25), "render", recovery_passes=RECOVERY)
        events += ev
    assert state.status == "UP"
    rec = [e for e in events if isinstance(e, RecoveryEvent)]
    assert len(rec) == 1
    assert rec[0].duration_s > 0


def test_b39_a_failure_mid_recovery_restarts_the_count():
    state, _ = run(UP, [(False, "element_missing", T(i * 25), "render") for i in range(4)])
    state, _ = step(state, True, None, T(200), "render", recovery_passes=RECOVERY)
    state, _ = step(state, False, "element_missing", T(225), "render", recovery_passes=RECOVERY)
    state, events = step(state, True, None, T(250), "render", recovery_passes=RECOVERY)
    assert state.status == "DOWN", "the recovery run restarted"
    assert not [e for e in events if isinstance(e, RecoveryEvent)]


# --- no duplicate alerts -------------------------------------------------------------

def test_no_duplicate_down_alerts_during_an_incident():
    state, events = run(UP, [(False, "element_missing", T(i * 25), "render") for i in range(8)])
    assert len(downs(events)) == 1
    assert state.evidence("render").consecutive == 8, "still tallying for the record"


# --- config-class routing (unchanged) ------------------------------------------------

def test_config_reason_routes_straight_to_config_error():
    state, events = step(UP, False, "auth_rejected", T(0), "authed")
    assert state.status == "CONFIG_ERROR"
    assert isinstance(events[0], ConfigErrorEvent)


def test_config_error_never_re_alerts():
    state, _ = step(UP, False, "bot_challenge", T(0), "authed")
    state, events = step(state, False, "bot_challenge", T(60), "authed")
    assert events == []


def test_config_error_holds_through_ordinary_failures():
    state, _ = step(UP, False, "mfa_failed", T(0), "authed")
    state, events = step(state, False, "nav_error", T(60), "authed")
    assert state.status == "CONFIG_ERROR"
    assert events == []


def test_config_error_clears_on_a_pass():
    state, _ = step(UP, False, "auth_rejected", T(0), "authed")
    state, events = step(state, True, None, T(60), "authed")
    assert state.status == "UP"
    assert isinstance(events[0], RecoveryEvent)


def test_config_reason_never_counts_toward_the_floor():
    state, _ = run(UP, [
        (False, "element_missing", T(0), "authed"),
        (False, "element_missing", T(25), "authed"),
        (False, "bot_challenge", T(50), "authed"),
    ])
    assert state.status == "CONFIG_ERROR"
    assert state.evidence("authed").consecutive == 0


# --- session_expired stays inert ------------------------------------------------------

def test_session_expired_leaves_the_run_untouched():
    state, _ = run(UP, [(False, "element_missing", T(0), "authed")])
    state, events = step(state, False, "session_expired", T(25), "authed")
    assert state.evidence("authed").consecutive == 1, "unchanged by the session probe"
    assert events == []


def test_session_expired_never_reaches_the_floor():
    state, events = run(UP, [(False, "session_expired", T(i * 25), "authed") for i in range(10)])
    assert state.status == "UP"
    assert downs(events) == []


def test_session_expired_does_not_route_to_config_error():
    state, _ = step(UP, False, "session_expired", T(0), "authed")
    assert state.status == "UP"


def test_session_expired_is_inert_during_an_open_incident():
    state, _ = run(UP, [(False, "element_missing", T(i * 25), "authed") for i in range(4)])
    before = state.evidence("authed").consecutive
    state, events = step(state, False, "session_expired", T(200), "authed")
    assert state.status == "DOWN" and events == []
    assert state.evidence("authed").consecutive == before


# --- cross-track suppression ----------------------------------------------------------

def test_precursor_down_suppresses_the_auth_down_event():
    state, events = run(UP, [(False, "element_missing", T(i * 25), "authed") for i in range(4)],
                        precursor_down=True)
    assert state.status == "UP", "held back while the main incident explains the symptom"
    assert downs(events) == []
    assert state.evidence("authed").consecutive == 4, "evidence is withheld, not discarded"


def test_suppressed_evidence_fires_on_the_next_unsuppressed_failure():
    state, _ = run(UP, [(False, "element_missing", T(i * 25), "authed") for i in range(4)],
                   precursor_down=True)
    state, events = step(state, False, "element_missing", T(200), "authed")
    assert state.status == "DOWN"
    assert len(downs(events)) == 1


def test_precursor_down_defaults_off_for_the_main_track():
    state, events = run(UP, [(False, "element_missing", T(i * 25), "render") for i in range(4)])
    assert state.status == "DOWN"
    assert len(downs(events)) == 1


# --- persistence shape ----------------------------------------------------------------

def test_derived_confidence_and_reasons_describe_the_causing_layer():
    state, _ = run(UP, [
        (False, "dns", T(0), "pulse"),
        (False, "element_missing", T(25), "render"),
        (False, "element_missing", T(50), "render"),
        (False, "element_missing", T(75), "render"),
        (False, "element_missing", T(100), "render"),
    ])
    assert state.status == "DOWN" and state.cause_layer == "render"
    assert state.fail_reasons == ("element_missing",) * 4
    assert state.confidence == 4, "the render run, not the pulse failure alongside it"
