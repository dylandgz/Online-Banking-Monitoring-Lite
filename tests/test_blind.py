"""B41 / BLIND: alarm on the ABSENCE of positive evidence.

The monitor could previously say two things -- "everything is fine" and "the bank is down" --
with no way to say "I cannot currently tell you". In the week of 2026-08-31 that third state
happened repeatedly and silently:

  09-03  CONFIG_ERROR latched, auth track skipped        7.3 h, one admin email
  09-06  CONFIG_ERROR latched, auth track skipped       13.7 h, one admin email
  09-06  host asleep, no cycles ran at all               9.5 h, nothing at all

The load-bearing choice is that BLIND is a NOTIFICATION, not a verdict. It never reaches
cycles.verdict, so it stays out of the severity ladder, unified_verdict, uptime_pct, the CSV
value set and the dashboard palette. Filing "we could not measure" in the same column as "the
bank is down" is the exact conflation uptime_pct was rewritten to remove.
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

import config
from monitor import db, main
from monitor.state import BlindEvent, MonitorState


def _ago(seconds: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


@pytest.fixture
def conn(tmp_path):
    c = db.get_connection(str(tmp_path / "blind.db"))
    db.init_db(c)
    yield c
    c.close()


@pytest.fixture(autouse=True)
def _reset():
    main._BLIND.update(notified=False, escalated=False)
    main._GRACE["until"] = None
    yield
    main._BLIND.update(notified=False, escalated=False)
    main._GRACE["until"] = None


class Recorder:
    name = "recorder"
    def __init__(self): self.events = []
    def send(self, event): self.events.append(event)


def _authed(conn, seconds_ago: int, ok: bool, reason=None):
    db.append_check(conn, ts=_ago(seconds_ago), ok=ok, http_status=None, latency_ms=5000.0,
                    fail_reason=reason, browser_mode="headed-xvfb", layer="authed")


def _run(conn, channels, auth_state=None, main_status="UP"):
    state = auth_state if auth_state is not None else MonitorState(status="UP", since_ts=None)
    asyncio.run(main._check_blind(conn, channels, True, state, main_status))


# --- when it fires --------------------------------------------------------------------

def test_silence_past_the_threshold_notifies(conn):
    _authed(conn, config.BLIND_AFTER_S + 120, ok=True)
    rec = Recorder()
    _run(conn, [rec])

    assert len(rec.events) == 1
    event = rec.events[0]
    assert isinstance(event, BlindEvent)
    assert event.recovered is False and event.escalation is False
    assert event.blind_for_s >= config.BLIND_AFTER_S


def test_a_recent_pass_is_not_blind(conn):
    _authed(conn, 60, ok=True)
    rec = Recorder()
    _run(conn, [rec])
    assert rec.events == []


def test_failing_probes_do_not_count_as_evidence(conn):
    """The distinction the whole feature rests on. A failing probe -- and especially an inert
    synthetic session_expired -- is the monitor recording that it WANTED to look and could
    not. Counting those as evidence is precisely how a track goes quiet unnoticed."""
    _authed(conn, config.BLIND_AFTER_S + 300, ok=True)
    for i in range(5):
        _authed(conn, 60 * i, ok=False, reason="session_expired")
        _authed(conn, 60 * i + 5, ok=False, reason="element_missing")

    rec = Recorder()
    _run(conn, [rec])
    assert len(rec.events) == 1, "recent FAILING probes must not suppress the alarm"


def test_it_notifies_once_not_once_per_cycle(conn):
    _authed(conn, config.BLIND_AFTER_S + 120, ok=True)
    rec = Recorder()
    for _ in range(10):
        _run(conn, [rec])
    assert len(rec.events) == 1


def test_it_escalates_once_at_the_longer_threshold(conn):
    """15 minutes and 13 hours deserve different reactions; more than one escalation is noise."""
    _authed(conn, config.BLIND_ESCALATE_AFTER_S + 600, ok=True)
    rec = Recorder()
    _run(conn, [rec])                      # entry
    for _ in range(5):
        _run(conn, [rec])                  # escalation, then silence
    assert len(rec.events) == 2
    assert rec.events[0].escalation is False
    assert rec.events[1].escalation is True


def test_a_pass_ends_the_episode_and_reports_it(conn):
    _authed(conn, config.BLIND_AFTER_S + 120, ok=True)
    rec = Recorder()
    _run(conn, [rec])
    assert len(rec.events) == 1

    _authed(conn, 1, ok=True)              # the track reports again
    _run(conn, [rec])
    assert len(rec.events) == 2
    assert rec.events[1].recovered is True

    for _ in range(3):
        _run(conn, [rec])
    assert len(rec.events) == 2, "recovery must not repeat either"


# --- when it must stay quiet ----------------------------------------------------------

def test_an_unconfigured_auth_track_is_not_blind(conn):
    """Nothing is expected of a track that was never enabled."""
    _authed(conn, config.BLIND_AFTER_S + 120, ok=True)
    rec = Recorder()
    asyncio.run(main._check_blind(conn, [rec], False, None, "UP"))
    assert rec.events == []


def test_no_baseline_means_no_alarm(conn):
    """A fresh database has never had a passing authed probe. There is nothing to be blind
    relative to, and claiming otherwise would fire on every first run."""
    rec = Recorder()
    _run(conn, [rec])
    assert rec.events == []


# --- the reason line ------------------------------------------------------------------

def test_the_reason_names_a_tripped_breaker(conn):
    """What separates an alarming instance from a benign one at a glance."""
    for i in range(config.MAX_CONSECUTIVE_LOGIN_FAILURES):
        db.append_login_event(conn, ts=_ago(120 - i), ok=False, latency_ms=1000.0,
                              reason="timeout")
    reason = main._blind_reason(conn, MonitorState(status="DOWN", since_ts=None))
    assert "halted" in reason and str(config.MAX_CONSECUTIVE_LOGIN_FAILURES) in reason


def test_the_reason_names_a_config_latch(conn):
    from monitor.state import LayerEvidence
    state = MonitorState(status="CONFIG_ERROR", since_ts=None, cause_layer="authed",
                         layers={"authed": LayerEvidence(fail_reasons=("mfa_failed",))})
    assert "halted" in main._blind_reason(conn, state)
    assert "mfa_failed" in main._blind_reason(conn, state)


def test_the_reason_names_a_sleeping_host(conn):
    """It fires even when the host was asleep -- the monitor is meant to be running, so that
    is something to be told about rather than filtered out."""
    main._GRACE["until"] = datetime.now(timezone.utc) + timedelta(seconds=120)
    assert "not running" in main._blind_reason(conn, MonitorState(status="UP", since_ts=None))


# --- it is not a verdict --------------------------------------------------------------

def test_blind_never_becomes_a_verdict(conn):
    """The design decision, pinned. If BLIND ever entered cycles.verdict it would join the
    severity ladder, the CSV value set and uptime_pct's arithmetic."""
    from monitor.verdict import STATUS_ORDER, unified_verdict
    assert "BLIND" not in STATUS_ORDER
    assert unified_verdict("UP", "UP") == "UP"

    _authed(conn, config.BLIND_AFTER_S + 120, ok=True)
    _run(conn, [Recorder()])
    verdicts = {r["verdict"] for r in conn.execute("SELECT verdict FROM cycles")}
    assert "BLIND" not in verdicts
