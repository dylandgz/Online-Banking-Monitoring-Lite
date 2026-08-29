from monitor.state import (
    MonitorState, DownEvent, RecoveryEvent, ConfigErrorEvent, apply_check, classify,
)

# [v3.1] DOWN requires score >= DOWN_CONFIDENCE AND >= MIN_FAILED_PROBES distinct failed
# probes within BURST_WINDOW_S, no intervening pass.
DOWN_CONFIDENCE = 4
MIN_FAILED_PROBES = 3
BURST_WINDOW_S = 90

TS = [f"2026-01-01T00:{i:02d}:00+00:00" for i in range(20)]
# Sub-minute offsets for burst-timing tests (0s, 15s, 35s, 55s -- matches BURST_DELAYS_S).
BTS = [
    "2026-01-01T00:00:00+00:00",
    "2026-01-01T00:00:15+00:00",
    "2026-01-01T00:00:35+00:00",
    "2026-01-01T00:00:55+00:00",
    "2026-01-01T00:01:35+00:00",  # 95s after BTS[0] -- outside the 90s burst window
]

UP_INITIAL = MonitorState(status="UP", since_ts=None)


def run(state, results, precursor_down=False):
    """results: list of (ok, fail_reason, ts, layer). Returns (final_state, all_events)."""
    all_events = []
    for ok, fail_reason, ts, layer in results:
        state, events = apply_check(
            state, ok, fail_reason, ts, layer, DOWN_CONFIDENCE, BURST_WINDOW_S, MIN_FAILED_PROBES,
            precursor_down=precursor_down,
        )
        all_events.extend(events)
    return state, all_events


# --- classify() ---

def test_classify_hard_reasons():
    for reason in ("conn_refused", "dns", "auth_unavailable", "bad_status:500", "bad_status:503"):
        assert classify(reason) == ("hard", 2), reason


def test_classify_soft_reasons():
    for reason in ("timeout", "element_missing", "nav_error", "bad_status:404", "bad_status:403"):
        assert classify(reason) == ("soft", 1), reason


def test_classify_retired_data_plane_reasons_are_merely_unrecognized():
    """v3.8 removed data-plane/API probing from scope, so nothing can emit these anymore
    and classify() no longer names them. They must still land on the cautious side of the
    fence via the unrecognized-reason fallback -- never Hard, so a stray legacy row in an
    old DB can't score double toward a page."""
    for reason in ("data_plane_missing", "api_shape_mismatch", "api_bad_status:502"):
        assert classify(reason) == ("soft", 1), reason


def test_classify_config_reasons():
    for reason in ("auth_rejected", "bot_challenge", "mfa_failed", "rate_limited"):
        assert classify(reason) == ("config", 0), reason


def test_classify_unknown_reason_defaults_soft():
    assert classify("some_new_reason_nobody_added_yet") == ("soft", 1)


# --- burst + confidence scoring + probe floor (Stage 5 v3.1 acceptance a-e) ---

def test_a_three_hard_failures_page_on_third_probe():
    # hard+hard+hard: score 6, 3 probes -> pages, ~35s in (BTS[2]).
    state, events = run(UP_INITIAL, [
        (False, "conn_refused", BTS[0], "pulse"),
        (False, "conn_refused", BTS[1], "pulse"),
        (False, "conn_refused", BTS[2], "pulse"),
    ])
    assert len(events) == 1
    down = events[0]
    assert isinstance(down, DownEvent)
    assert down.confidence == 6
    assert down.since_ts == BTS[2]
    assert state.status == "DOWN"


def test_a_hard_hard_soft_pages_on_third_probe():
    # score 5, 3 probes -> pages.
    state, events = run(UP_INITIAL, [
        (False, "conn_refused", BTS[0], "pulse"),
        (False, "conn_refused", BTS[1], "pulse"),
        (False, "timeout", BTS[2], "render"),
    ])
    assert len(events) == 1
    assert events[0].confidence == 5
    assert state.status == "DOWN"


def test_a_hard_soft_soft_pages_on_third_probe():
    # score 4, 3 probes -> pages.
    state, events = run(UP_INITIAL, [
        (False, "conn_refused", BTS[0], "pulse"),
        (False, "timeout", BTS[1], "render"),
        (False, "nav_error", BTS[2], "render"),
    ])
    assert len(events) == 1
    down = events[0]
    assert down.confidence == 4
    assert down.trigger_layer == "render"
    assert state.status == "DOWN"


def test_b_all_soft_outage_does_not_page_until_fourth_probe():
    # soft+soft+soft: score 3, 3 probes -> below DOWN_CONFIDENCE, no page yet.
    state, events = run(UP_INITIAL, [
        (False, "timeout", BTS[0], "pulse"),
        (False, "element_missing", BTS[1], "render"),
        (False, "nav_error", BTS[2], "render"),
    ])
    assert events == []
    assert state.status == "UP"
    assert state.confidence == 3
    assert len(state.fail_reasons) == 3

    # 4th soft failure (still inside the 90s window): score 4, 4 probes -> pages.
    state, events = run(state, [(False, "timeout", BTS[3], "pulse")])
    assert len(events) == 1
    down = events[0]
    assert down.confidence == 4
    assert len(down.fail_reasons) == 4
    assert state.status == "DOWN"


def test_c_two_failures_then_a_pass_never_alerts():
    state, events = run(UP_INITIAL, [
        (False, "conn_refused", BTS[0], "pulse"),
        (False, "conn_refused", BTS[1], "pulse"),
        (True, None, BTS[2], "render"),
    ])
    assert events == []
    assert state.status == "UP"
    assert state.confidence == 0
    assert state.burst_started_ts is None


def test_d_hard_hard_two_probes_does_not_page_until_third_fails():
    # Explicit floor test: score already >= DOWN_CONFIDENCE (4) after 2 hard failures,
    # but only 2 distinct failed probes -- must NOT page yet.
    state, events = run(UP_INITIAL, [
        (False, "conn_refused", BTS[0], "pulse"),
        (False, "conn_refused", BTS[1], "pulse"),
    ])
    assert events == []
    assert state.status == "UP"
    assert state.confidence == 4
    assert len(state.fail_reasons) == 2

    # A 3rd failed probe (any evidence) finally satisfies the floor -> pages.
    state, events = run(state, [(False, "conn_refused", BTS[2], "pulse")])
    assert len(events) == 1
    down = events[0]
    assert isinstance(down, DownEvent)
    assert down.confidence == 6
    assert len(down.fail_reasons) == 3
    assert state.status == "DOWN"


def test_e_config_reason_never_counts_toward_score_or_probe_floor():
    state, events = run(UP_INITIAL, [(False, "bot_challenge", BTS[0], "render")])
    assert len(events) == 1
    assert isinstance(events[0], ConfigErrorEvent)
    assert state.status == "CONFIG_ERROR"
    assert state.confidence == 0


# --- below-threshold / flap ---

def test_single_hard_failure_below_threshold_no_event():
    state, events = run(UP_INITIAL, [(False, "dns", BTS[0], "pulse")])
    assert events == []
    assert state.status == "UP"
    assert state.confidence == 2
    assert len(state.fail_reasons) == 1


def test_two_soft_failures_below_threshold_no_event():
    state, events = run(UP_INITIAL, [
        (False, "timeout", BTS[0], "pulse"),
        (False, "timeout", BTS[1], "pulse"),
    ])
    assert events == []
    assert state.status == "UP"
    assert state.confidence == 2


def test_flap_sequence_never_alerts():
    # F, S, F, F, S -- a pass always clears the burst before it reaches threshold.
    state, events = run(UP_INITIAL, [
        (False, "timeout", BTS[0], "pulse"),
        (True, None, BTS[1], "render"),
        (False, "timeout", BTS[2], "pulse"),
        (False, "timeout", BTS[3], "render"),
        (True, None, BTS[4], "render"),
    ])
    assert events == []
    assert state.status == "UP"
    assert state.confidence == 0


def test_passing_probe_clears_burst_and_resets_confidence():
    state, _ = run(UP_INITIAL, [(False, "timeout", BTS[0], "pulse")])
    assert state.confidence == 1
    state, events = run(state, [(True, None, BTS[1], "render")])
    assert events == []
    assert state.confidence == 0
    assert state.burst_started_ts is None


def test_stale_failure_outside_burst_window_starts_a_fresh_burst():
    # A soft fail, then another soft fail 95s later (outside the 90s window) should NOT
    # accumulate with the first -- it starts a brand-new burst instead of compounding a
    # stale one, so confidence is 1 (not 2) and no DOWN fires.
    state, _ = run(UP_INITIAL, [(False, "timeout", BTS[0], "pulse")])
    state, events = run(state, [(False, "timeout", BTS[4], "pulse")])
    assert events == []
    assert state.confidence == 1
    assert state.burst_started_ts == BTS[4]


# --- sustained incident / recovery / restart ---

def test_no_duplicate_down_alerts_during_incident():
    # First 3 close-together failures cross both the score and probe floor -> DOWN.
    # Further failures at normal 60s cadence keep tallying but never re-alert.
    results = [
        (False, "conn_refused", BTS[0], "pulse"),
        (False, "conn_refused", BTS[1], "pulse"),
        (False, "conn_refused", BTS[2], "pulse"),
    ] + [(False, "conn_refused", TS[i], "pulse") for i in range(3, 6)]
    state, events = run(UP_INITIAL, results)
    down_events = [e for e in events if isinstance(e, DownEvent)]
    assert len(down_events) == 1
    assert state.status == "DOWN"
    assert state.confidence == 12  # 2 per fail x 6 fails total
    assert len(state.fail_reasons) == 6


def test_recovery_after_down():
    state, events = run(UP_INITIAL, [
        (False, "conn_refused", BTS[0], "pulse"),
        (False, "conn_refused", BTS[1], "pulse"),
        (False, "conn_refused", BTS[2], "pulse"),
        (True, None, BTS[3], "render"),
    ])
    assert len(events) == 2
    down, recovery = events
    assert isinstance(down, DownEvent)
    assert isinstance(recovery, RecoveryEvent)
    assert recovery.since_ts == BTS[2]
    assert recovery.ended_at == BTS[3]
    assert recovery.confidence == 6
    assert state.status == "UP"
    assert state.confidence == 0
    assert state.since_ts == BTS[3]


def test_restart_keeps_state_no_duplicate_down():
    persisted = MonitorState(status="DOWN", since_ts=BTS[2], confidence=6, fail_reasons=("conn_refused", "conn_refused", "conn_refused"))
    state, events = apply_check(persisted, False, "conn_refused", BTS[3], "pulse", DOWN_CONFIDENCE, BURST_WINDOW_S, MIN_FAILED_PROBES)
    assert events == []
    assert state.status == "DOWN"
    assert state.confidence == 8


def test_restart_keeps_state_recovery_uses_persisted_since_ts():
    persisted = MonitorState(status="DOWN", since_ts=TS[0], confidence=6, fail_reasons=("dns", "dns", "dns"))
    state, events = apply_check(persisted, True, None, TS[10], "render", DOWN_CONFIDENCE, BURST_WINDOW_S, MIN_FAILED_PROBES)
    assert len(events) == 1
    recovery = events[0]
    assert isinstance(recovery, RecoveryEvent)
    assert recovery.since_ts == TS[0]
    assert recovery.duration_s == 600
    assert state.status == "UP"


# --- CONFIG_ERROR routing (Rule 4 "never retry a credential rejection" -- never burst-retried, distinct from DOWN) ---

def test_config_reason_never_scored_routes_straight_to_config_error():
    state, events = run(UP_INITIAL, [(False, "bot_challenge", BTS[0], "render")])
    assert len(events) == 1
    event = events[0]
    assert isinstance(event, ConfigErrorEvent)
    assert event.fail_reason == "bot_challenge"
    assert state.status == "CONFIG_ERROR"
    assert state.confidence == 0  # config reasons never contribute to burst confidence


def test_config_error_never_re_alerts_on_repeated_config_failures():
    state, _ = run(UP_INITIAL, [(False, "auth_rejected", BTS[0], "auth")])
    state, events = run(state, [
        (False, "auth_rejected", BTS[1], "auth"),
        (False, "auth_rejected", BTS[2], "auth"),
    ])
    assert events == []
    assert state.status == "CONFIG_ERROR"


def test_config_error_holds_through_unrelated_ordinary_failures():
    # A human must clear CONFIG_ERROR -- ordinary hard/soft fails don't override it or
    # trigger a separate DOWN underneath it.
    state, _ = run(UP_INITIAL, [(False, "mfa_failed", BTS[0], "auth")])
    state, events = run(state, [(False, "conn_refused", BTS[1], "pulse")])
    assert events == []
    assert state.status == "CONFIG_ERROR"


def test_config_error_recovers_on_first_pass_like_down_does():
    state, _ = run(UP_INITIAL, [(False, "bot_challenge", BTS[0], "render")])
    state, events = run(state, [(True, None, BTS[1], "render")])
    assert len(events) == 1
    assert isinstance(events[0], RecoveryEvent)
    assert state.status == "UP"


# --- [v3.8 / Stage R] session_expired never scores (Rule 3 "session_expired never scores") ---

def test_classify_session_expired_is_its_own_zero_weight_class():
    assert classify("session_expired") == ("session", 0)


def test_session_expired_from_up_leaves_state_and_burst_untouched():
    state, _ = run(UP_INITIAL, [(False, "timeout", BTS[0], "authed")])
    assert state.confidence == 1
    state2, events = run(state, [(False, "session_expired", BTS[1], "authed")])
    assert events == []
    assert state2 == state  # completely inert, not even burst_started_ts touched


def test_session_expired_does_not_route_to_config_error():
    state, events = run(UP_INITIAL, [(False, "session_expired", BTS[0], "authed")])
    assert events == []
    assert state.status == "UP"
    assert state.confidence == 0
    assert state.fail_reasons == ()


def test_session_expired_never_contributes_even_at_floor():
    # 3 real failures right at the DOWN threshold, then a session_expired probe must not
    # push it over/hold it back -- it's simply not counted either way.
    state, events = run(UP_INITIAL, [
        (False, "conn_refused", BTS[0], "authed"),
        (False, "conn_refused", BTS[1], "authed"),
    ])
    assert events == []
    state, events = run(state, [(False, "session_expired", BTS[2], "authed")])
    assert events == []
    assert state.confidence == 4  # unchanged by the session_expired probe
    assert len(state.fail_reasons) == 2


def test_session_expired_inert_during_an_open_down_incident():
    state, _ = run(UP_INITIAL, [
        (False, "conn_refused", BTS[0], "authed"),
        (False, "conn_refused", BTS[1], "authed"),
        (False, "conn_refused", BTS[2], "authed"),
    ])
    assert state.status == "DOWN"
    state2, events = run(state, [(False, "session_expired", BTS[3], "authed")])
    assert events == []
    assert state2 == state


# --- [v3.8 / Stage R] the "Cross-track suppression" section: precursor_down suppresses auth-track paging ---

def test_precursor_down_suppresses_auth_down_event():
    state, events = run(UP_INITIAL, [
        (False, "conn_refused", BTS[0], "authed"),
        (False, "conn_refused", BTS[1], "authed"),
        (False, "conn_refused", BTS[2], "authed"),
    ], precursor_down=True)
    assert events == []
    assert state.status == "UP"  # held -- the "Cross-track suppression" section: auth-track DOWN can't open while precursor is down
    assert state.confidence == 6
    assert len(state.fail_reasons) == 3


def test_precursor_down_suppressed_evidence_fires_once_precursor_recovers():
    state, _ = run(UP_INITIAL, [
        (False, "conn_refused", BTS[0], "authed"),
        (False, "conn_refused", BTS[1], "authed"),
        (False, "conn_refused", BTS[2], "authed"),
    ], precursor_down=True)
    assert state.status == "UP"

    # Precursor has now recovered -- the next failing probe re-evaluates against the
    # already-accumulated evidence and fires immediately (nothing was lost).
    state, events = run(state, [(False, "conn_refused", BTS[3], "authed")], precursor_down=False)
    assert len(events) == 1
    assert isinstance(events[0], DownEvent)
    assert state.status == "DOWN"


def test_precursor_down_does_not_affect_main_track_default():
    # Sanity check: default precursor_down=False (every existing main-track call site)
    # behaves exactly as before -- covered implicitly by every other test in this file,
    # asserted explicitly here too.
    state, events = run(UP_INITIAL, [
        (False, "conn_refused", BTS[0], "pulse"),
        (False, "conn_refused", BTS[1], "pulse"),
        (False, "conn_refused", BTS[2], "pulse"),
    ])
    assert len(events) == 1
    assert state.status == "DOWN"


def test_hard_mixed_pages_faster_than_all_soft():
    # Confirms the "hard evidence pages faster" governing property directly against each
    # other: hard-mixed resolves on the 3rd probe (~35s), all-soft needs a 4th (~55-60s).
    hard_state, hard_events = run(UP_INITIAL, [
        (False, "conn_refused", BTS[0], "pulse"),
        (False, "conn_refused", BTS[1], "pulse"),
        (False, "timeout", BTS[2], "render"),
    ])
    soft_state, soft_events = run(UP_INITIAL, [
        (False, "timeout", BTS[0], "pulse"),
        (False, "timeout", BTS[1], "render"),
        (False, "timeout", BTS[2], "render"),
    ])
    assert len(hard_events) == 1 and isinstance(hard_events[0], DownEvent)
    assert soft_events == []  # 3 soft fails (score 3) haven't paged yet -- needs a 4th

    soft_state, soft_events = run(soft_state, [(False, "timeout", BTS[3], "pulse")])
    assert len(soft_events) == 1 and isinstance(soft_events[0], DownEvent)
