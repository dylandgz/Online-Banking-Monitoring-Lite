from monitor.state import MonitorState, DownEvent, RecoveryEvent, apply_check

FAILS_TO_DOWN = 3

TS = [f"2026-01-01T00:{i:02d}:00+00:00" for i in range(20)]

UP_INITIAL = MonitorState(status="UP", consecutive_fails=0, since_ts=None)


def run(state, results):
    """results: list of (ok, fail_reason, ts). Returns (final_state, all_events)."""
    all_events = []
    for ok, fail_reason, ts in results:
        state, events = apply_check(state, ok, fail_reason, ts, FAILS_TO_DOWN)
        all_events.extend(events)
    return state, all_events


def test_flap_never_alerts():
    # F, F, S, F, F, S -- never reaches 3 consecutive fails, so never alerts.
    results = [
        (False, "timeout", TS[0]),
        (False, "timeout", TS[1]),
        (True, None, TS[2]),
        (False, "timeout", TS[3]),
        (False, "timeout", TS[4]),
        (True, None, TS[5]),
    ]
    final_state, events = run(UP_INITIAL, results)
    assert events == []
    assert final_state.status == "UP"
    assert final_state.consecutive_fails == 0


def test_exact_threshold_no_event_below_threshold():
    state, events = run(UP_INITIAL, [
        (False, "timeout", TS[0]),
        (False, "timeout", TS[1]),
    ])
    assert events == []
    assert state.status == "UP"
    assert state.consecutive_fails == 2


def test_exact_threshold_fires_on_third_fail():
    state, events = run(UP_INITIAL, [
        (False, "timeout", TS[0]),
        (False, "timeout", TS[1]),
        (False, "conn_refused", TS[2]),
    ])
    assert len(events) == 1
    event = events[0]
    assert isinstance(event, DownEvent)
    assert event.consecutive_fails == 3
    assert event.fail_reason == "conn_refused"
    assert event.since_ts == TS[2]
    assert state.status == "DOWN"
    assert state.consecutive_fails == 3
    assert state.since_ts == TS[2]


def test_recovery_after_down():
    state, events = run(UP_INITIAL, [
        (False, "timeout", TS[0]),
        (False, "timeout", TS[1]),
        (False, "timeout", TS[2]),
        (True, None, TS[3]),
    ])
    assert len(events) == 2
    down, recovery = events
    assert isinstance(down, DownEvent)
    assert isinstance(recovery, RecoveryEvent)
    assert recovery.since_ts == TS[2]
    assert recovery.ended_at == TS[3]
    assert recovery.checks_failed == 3
    assert state.status == "UP"
    assert state.consecutive_fails == 0
    assert state.since_ts == TS[3]


def test_no_duplicate_down_alerts_during_incident():
    # Fails continue well past the threshold -- only one DOWN event, ever.
    results = [(False, "timeout", TS[i]) for i in range(6)]
    state, events = run(UP_INITIAL, results)
    down_events = [e for e in events if isinstance(e, DownEvent)]
    assert len(down_events) == 1
    assert state.status == "DOWN"
    assert state.consecutive_fails == 6  # keeps counting for the eventual checks_failed tally


def test_restart_keeps_state_no_duplicate_down():
    # Simulates a restart mid-incident: state reloaded from DB as already DOWN.
    persisted = MonitorState(status="DOWN", consecutive_fails=3, since_ts=TS[2])
    state, events = apply_check(persisted, False, "timeout", TS[3], FAILS_TO_DOWN)
    assert events == []
    assert state.status == "DOWN"
    assert state.consecutive_fails == 4


def test_restart_keeps_state_recovery_uses_persisted_since_ts():
    persisted = MonitorState(status="DOWN", consecutive_fails=5, since_ts=TS[0])
    state, events = apply_check(persisted, True, None, TS[10], FAILS_TO_DOWN)
    assert len(events) == 1
    recovery = events[0]
    assert isinstance(recovery, RecoveryEvent)
    assert recovery.since_ts == TS[0]
    assert recovery.checks_failed == 5
    assert recovery.duration_s == 600  # TS[10] - TS[0] == 10 minutes
    assert state.status == "UP"
    assert state.consecutive_fails == 0


def test_single_fail_below_threshold_no_event():
    state, events = apply_check(UP_INITIAL, False, "dns", TS[0], FAILS_TO_DOWN)
    assert events == []
    assert state.consecutive_fails == 1
    assert state.status == "UP"


def test_success_while_up_resets_fails_no_event():
    state = MonitorState(status="UP", consecutive_fails=2, since_ts=None)
    state, events = apply_check(state, True, None, TS[0], FAILS_TO_DOWN)
    assert events == []
    assert state.consecutive_fails == 0
    assert state.status == "UP"
