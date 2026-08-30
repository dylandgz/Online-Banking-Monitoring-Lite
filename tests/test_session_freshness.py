"""Tests for is_session_fresh -- specifically that a damaged session file can never
become platform evidence.

This is a false-positive guard, which is why it gets its own file. A corrupt
session_state.json used to satisfy the mtime-only freshness check, get handed to
new_context(), and raise before any page existed. Pre-P0 that killed the auth track
silently; post-P0 the outer guard turned it into nav_error every cycle, and four Soft
probes reach AUTH_DOWN_CONFIDENCE -- so a damaged local file would have paged a false
DOWN with authed wording while the bank was healthy. CLAUDE.md ranks low false positives
above every other concern, so the condition is pinned here.
"""
import json
import os
import time

import config
from monitor.session import is_session_fresh
from monitor.state import MonitorState, apply_check


def _write(tmp_path, name, content, age_s=0):
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    if age_s:
        stamp = time.time() - age_s
        os.utime(path, (stamp, stamp))
    return str(path)


def test_valid_recent_session_is_fresh(tmp_path):
    path = _write(tmp_path, "s.json", json.dumps({"cookies": [], "origins": []}))
    assert is_session_fresh(path, 1800) is True


def test_missing_session_is_not_fresh(tmp_path):
    assert is_session_fresh(str(tmp_path / "absent.json"), 1800) is False


def test_valid_but_stale_session_is_not_fresh(tmp_path):
    """SESSION_MAX_AGE_S exists to force a periodic real login, so age still wins."""
    path = _write(tmp_path, "s.json", json.dumps({"cookies": []}), age_s=9999)
    assert is_session_fresh(path, 1800) is False


def test_truncated_session_is_not_fresh(tmp_path):
    """The process killed mid-write leaves a recent mtime and unparseable content."""
    path = _write(tmp_path, "s.json", '{"cookies": [{"name": "cf_cl')
    assert is_session_fresh(path, 1800) is False


def test_empty_session_file_is_not_fresh(tmp_path):
    path = _write(tmp_path, "s.json", "")
    assert is_session_fresh(path, 1800) is False


def test_a_damaged_session_cannot_reach_the_probe_that_would_page(tmp_path):
    """The point of all the above: because is_session_fresh says False, _run_auth_probe
    treats it as 'no session' and routes to the budgeted recovery login (which rewrites
    the file and self-heals) instead of handing the damaged file to new_context() and
    producing a scoring nav_error on every cycle."""
    path = _write(tmp_path, "s.json", "not json at all")
    assert is_session_fresh(path, 1800) is False


def test_four_repeated_soft_failures_would_have_paged(tmp_path):
    """Documents WHY the above matters, and guards the floor that makes it dangerous: four
    identical Soft probes on the auth track do reach DOWN. If this ever stops being true the
    guard above is less critical -- but so is the probe floor, so asserting it is a feature,
    not redundancy.

    [2026-08-30] Updated for the consecutive model: the probes no longer need to land inside
    a window, they need to be consecutive on the same layer with no pass between them."""
    state = MonitorState(status="UP", since_ts="2026-08-24T10:00:00+00:00")
    events_seen = []
    for offset in (0, 25, 50, 55):
        state, events = apply_check(
            state, False, "nav_error", f"2026-08-24T10:00:{offset:02d}+00:00", "authed",
            config.AUTH_DOWN_CONFIDENCE, config.AUTH_MIN_FAILED_PROBES,
            config.EVIDENCE_STALE_AFTER_S,
        )
        events_seen.extend(events)

    assert state.status == "DOWN"
    assert len(events_seen) == 1
    # ...and nothing paged before the floor was met.
    assert config.AUTH_MIN_FAILED_PROBES == 4
