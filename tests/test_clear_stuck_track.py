"""B64: the repair tool for a track that cannot recover on its own.

The load-bearing test here is `test_a_stuck_down_is_closed_at_the_derived_time`. It rebuilds
the exact shape of the 2026-09-04 incident -- an auth-track DOWN whose `cause_layer` is
`render`, a layer that track can never produce a pass on (B50) -- and asserts the tool closes
it at the moment recovery actually happened rather than at repair time. Closing at "now" would
have written 12 h 36 m of outage for a platform that recovered after ~9 minutes, which is the
audit-record lie this tool exists to avoid.

The dry-run tests matter for a different reason: unlike its predecessor, this script can clear
a DOWN, so a careless invocation can silence a real outage. "Writes nothing without --confirm"
is a safety property, not a convenience.
"""
import runpy
import sys

import pytest

import config
from monitor import db
from monitor.state import LayerEvidence, MonitorState

SCRIPT = "scripts.clear_config_error_and_stuck_down"

T0 = "2026-09-05T03:25:28+00:00"   # incident opens (2026-09-04 23:25:28 ET)
T_PASSES = [
    "2026-09-05T03:33:12+00:00",   # pass 1
    "2026-09-05T03:34:11+00:00",   # pass 2
    "2026-09-05T03:35:10+00:00",   # pass 3 -> recovery would have fired here
    "2026-09-05T03:36:10+00:00",
]


@pytest.fixture
def dbpath(tmp_path, monkeypatch):
    path = str(tmp_path / "stuck.db")
    monkeypatch.setattr(config, "DB_PATH", path)
    conn = db.get_connection(path)
    db.init_db(conn)
    conn.close()
    return path


def _run(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", [SCRIPT, *argv])
    runpy.run_module(SCRIPT, run_name="__main__")


def _state(path, track="auth"):
    conn = db.get_connection(path)
    try:
        return db.get_state(conn, track=track)
    finally:
        conn.close()


def _incident(path, incident_id=1):
    conn = db.get_connection(path)
    try:
        return db.get_incident(conn, incident_id)
    finally:
        conn.close()


def _make_stuck_down(path, *, with_passes=True):
    """The 2026-09-04 shape: auth DOWN, cause_layer=render, open incident, and (optionally)
    a run of passing `authed` probes afterwards that the state machine could never count."""
    conn = db.get_connection(path)
    db.set_state(conn, MonitorState(
        status="DOWN", since_ts=T0, cause_layer="render",
        layers={"render": LayerEvidence(consecutive=4, confidence=4,
                                        fail_reasons=("element_missing",) * 4,
                                        last_probe_ts=T0, run_started_ts=T0)},
    ), track="auth")
    db.open_incident(conn, T0, 4, "render", 4, None, page_url=None, track="auth")
    if with_passes:
        for ts in T_PASSES:
            db.append_check(conn, ts=ts, ok=True, http_status=None, latency_ms=5000.0,
                            fail_reason=None, browser_mode="headed-xvfb", layer="authed")
    conn.commit()
    conn.close()


# --- nothing to do -------------------------------------------------------------------

def test_an_up_track_is_left_alone(dbpath, monkeypatch, capsys):
    _run(monkeypatch, "--track", "auth")
    assert "nothing to clear" in capsys.readouterr().out
    assert _state(dbpath).status == "UP"


def test_the_track_is_never_guessed(dbpath, monkeypatch):
    """It can clear a DOWN, so it must not act on a default. argparse exits 2."""
    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch)
    assert exc.value.code == 2


# --- dry run is the default ----------------------------------------------------------

def test_dry_run_writes_nothing_for_a_config_error(dbpath, monkeypatch, capsys):
    conn = db.get_connection(dbpath)
    db.set_state(conn, MonitorState(status="CONFIG_ERROR", since_ts=T0, cause_layer="authed",
                                    layers={"authed": LayerEvidence(fail_reasons=("mfa_failed",))}),
                 track="auth")
    conn.close()

    _run(monkeypatch, "--track", "auth")

    out = capsys.readouterr().out
    assert "DRY RUN" in out and "mfa_failed" in out
    assert _state(dbpath).status == "CONFIG_ERROR", "dry run must not write"


def test_dry_run_writes_nothing_for_a_stuck_down(dbpath, monkeypatch, capsys):
    _make_stuck_down(dbpath)
    _run(monkeypatch, "--track", "auth")

    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert _state(dbpath).status == "DOWN"
    assert _incident(dbpath)["ended_at"] is None


# --- the repairs ---------------------------------------------------------------------

def test_a_config_error_is_cleared_to_a_clean_slate(dbpath, monkeypatch, capsys):
    conn = db.get_connection(dbpath)
    db.set_state(conn, MonitorState(status="CONFIG_ERROR", since_ts=T0, cause_layer="authed",
                                    layers={"authed": LayerEvidence(consecutive=1, confidence=0,
                                                                    fail_reasons=("bot_challenge",))}),
                 track="auth")
    conn.close()

    _run(monkeypatch, "--track", "auth", "--confirm")

    state = _state(dbpath)
    assert state.status == "UP"
    assert state.cause_layer is None
    assert state.fail_reasons == (), "stale evidence must not survive into a future DOWN"


def test_a_stuck_down_is_closed_at_the_derived_time(dbpath, monkeypatch, capsys):
    """The 2026-09-04 regression. Recovery is derived from the probes, not from the clock."""
    _make_stuck_down(dbpath)

    _run(monkeypatch, "--track", "auth", "--confirm")

    state = _state(dbpath)
    assert state.status == "UP" and state.cause_layer is None

    incident = _incident(dbpath)
    assert incident["ended_at"] == T_PASSES[2], "must close at the 3rd consecutive pass"
    assert incident["duration_s"] == 582, "9m42s -- not the hours until a human noticed"


def test_an_explicit_ended_at_overrides_the_derivation(dbpath, monkeypatch, capsys):
    _make_stuck_down(dbpath)
    chosen = "2026-09-05T03:40:00+00:00"

    _run(monkeypatch, "--track", "auth", "--confirm", "--ended-at", chosen)

    assert _incident(dbpath)["ended_at"] == chosen


# --- the refusal ---------------------------------------------------------------------

def test_it_refuses_to_close_an_incident_it_cannot_date(dbpath, monkeypatch, capsys):
    """No passing probes means no honest end time. Closing at "now" is exactly the lie this
    tool exists to avoid, so it declines and says how to proceed."""
    _make_stuck_down(dbpath, with_passes=False)

    _run(monkeypatch, "--track", "auth", "--confirm")

    out = capsys.readouterr().out
    assert "refusing to clear" in out and "--ended-at" in out
    assert _state(dbpath).status == "DOWN", "state must not be cleared either"
    assert _incident(dbpath)["ended_at"] is None


def test_session_expired_neither_counts_nor_breaks_a_recovery_run(dbpath, monkeypatch):
    """Rule 3: session_expired is inert. It must not count as a pass, and must not reset the
    streak either -- otherwise the derived end time drifts to whenever the sessions settled."""
    _make_stuck_down(dbpath, with_passes=False)
    conn = db.get_connection(dbpath)
    for ts, ok, reason in (
        (T_PASSES[0], True, None),
        ("2026-09-05T03:33:40+00:00", False, "session_expired"),   # inert
        (T_PASSES[1], True, None),
        (T_PASSES[2], True, None),
    ):
        db.append_check(conn, ts=ts, ok=ok, http_status=None, latency_ms=0.0,
                        fail_reason=reason, browser_mode="headed-xvfb", layer="authed")
    conn.commit()
    conn.close()

    _run(monkeypatch, "--track", "auth", "--confirm")

    assert _incident(dbpath)["ended_at"] == T_PASSES[2]
