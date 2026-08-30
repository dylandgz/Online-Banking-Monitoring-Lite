"""Stage 2 of the B37/B38 rework: how the confirmation burst probes, and when a probe counts.

Three behaviours, each pinned because each closes a defect that reached production:

  B37  the burst re-probes THE LAYER THAT FAILED. It used to alternate render/pulse for
       "independent evidence", which combined with per-layer scoring is worse than either
       change alone -- simulated at 69% of outages missed against the old design's 48%,
       because no single layer accumulates.

  B38  probes are spaced by a gap after the previous one FINISHED, not at absolute offsets
       from the burst's start. Offsets only held while probes were fast enough to leave idle
       time; past that the sleep clamped to zero and the schedule became fiction.

  B7   after the monitor notices it was not running, probes are recorded but do not score.
       All three main-track DOWN pages of the Stage R era were a laptop resuming from
       suspend.
"""
import asyncio

import pytest

import config
from monitor import check, db, main
from monitor.check import CheckResult


@pytest.fixture
def conn(tmp_path):
    c = db.get_connection(str(tmp_path / "burst.db"))
    db.init_db(c)
    yield c
    c.close()


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch):
    monkeypatch.setattr(config, "BURST_GAP_S", 0)
    monkeypatch.setattr(config, "BURST_JITTER_S", 0)
    main._GRACE["until"] = None
    yield
    main._GRACE["until"] = None


def _probes(conn):
    return [(r["layer"], bool(r["ok"])) for r in
            conn.execute("SELECT layer, ok FROM checks ORDER BY id")]


def _fail(layer, reason="element_missing"):
    async def probe(*a, **k):
        return CheckResult(ok=False, http_status=None, latency_ms=1.0, fail_reason=reason,
                           layer=layer, pulse_latency_ms=1.0,
                           render_latency_ms=9.0 if layer == "render" else None)
    return probe


async def _pass_pulse(*a, **k):
    return CheckResult(ok=True, http_status=200, latency_ms=1.0, fail_reason=None, layer="pulse")


async def _pass_all(*a, **k):
    return CheckResult(ok=True, http_status=200, latency_ms=1.0, fail_reason=None,
                       layer="render", pulse_latency_ms=1.0, render_latency_ms=9.0)


# --- B37: the burst confirms the layer under suspicion --------------------------------

def test_a_render_failure_bursts_with_render_probes(conn, monkeypatch):
    monkeypatch.setattr(check, "perform_check", _fail("render"))
    monkeypatch.setattr(check, "render_only_probe", _fail("render"))
    monkeypatch.setattr(check, "pulse_only_probe", _pass_pulse)

    asyncio.run(main.run_cycle(conn, [], auth_enabled=False))

    assert all(layer == "render" for layer, _ in _probes(conn)), \
        "the pulse is not what is in doubt; probing it would only invite a pass that clears nothing"


def test_a_pulse_failure_bursts_with_pulse_probes(conn, monkeypatch):
    monkeypatch.setattr(check, "perform_check", _fail("pulse", "dns"))
    monkeypatch.setattr(check, "pulse_only_probe", _fail("pulse", "dns"))
    monkeypatch.setattr(check, "render_only_probe", _fail("render"))

    asyncio.run(main.run_cycle(conn, [], auth_enabled=False))

    assert all(layer == "pulse" for layer, _ in _probes(conn))


def test_a_pure_render_outage_pages_within_one_cycle(conn, monkeypatch):
    """THE defect this whole rework exists for. Under the old design this outage -- server
    reachable, page rendering wrong -- could not page at any duration, because the burst's
    own pulse probe passed and wiped the render evidence that opened it."""
    monkeypatch.setattr(check, "perform_check", _fail("render"))
    monkeypatch.setattr(check, "render_only_probe", _fail("render"))
    monkeypatch.setattr(check, "pulse_only_probe", _pass_pulse)

    asyncio.run(main.run_cycle(conn, [], auth_enabled=False))

    state = db.get_state(conn, track="main")
    assert state.status == "DOWN"
    assert state.cause_layer == "render"
    row = conn.execute("SELECT verdict, fail_layer FROM cycles ORDER BY ts DESC LIMIT 1").fetchone()
    assert (row["verdict"], row["fail_layer"]) == ("DOWN", "render")


def test_a_passing_probe_ends_the_burst_early(conn, monkeypatch):
    calls = {"n": 0}

    async def fails_then_passes(*a, **k):
        calls["n"] += 1
        ok = calls["n"] > 1
        return CheckResult(ok=ok, http_status=None, latency_ms=1.0,
                           fail_reason=None if ok else "element_missing", layer="render")

    monkeypatch.setattr(check, "perform_check", _fail("render"))
    monkeypatch.setattr(check, "render_only_probe", fails_then_passes)
    monkeypatch.setattr(check, "pulse_only_probe", _pass_pulse)

    asyncio.run(main.run_cycle(conn, [], auth_enabled=False))

    state = db.get_state(conn, track="main")
    assert state.status == "UP", "one failure then a pass is a flap"
    assert len(_probes(conn)) < 5, "the burst stopped once the run was cleared"


# --- B7: the wake grace ----------------------------------------------------------------

def _seed_stale_probe(conn, ts="2026-08-30T10:00:00+00:00"):
    conn.execute("INSERT INTO checks (ts, ok, latency_ms, layer) VALUES (?, 1, 1.0, 'render')", (ts,))
    conn.commit()


def test_probes_after_a_process_gap_are_recorded_but_do_not_score(conn, monkeypatch):
    _seed_stale_probe(conn)
    monkeypatch.setattr(check, "perform_check", _fail("pulse", "dns"))
    monkeypatch.setattr(check, "pulse_only_probe", _fail("pulse", "dns"))

    asyncio.run(main.run_cycle(conn, [], auth_enabled=False))

    state = db.get_state(conn, track="main")
    assert state.status == "UP", "a laptop waking up is not evidence about the bank"
    assert state.evidence("pulse").consecutive == 0
    assert conn.execute("SELECT COUNT(*) c FROM checks WHERE fail_reason='dns'").fetchone()["c"] >= 1, \
        "but the probe is still recorded -- every probe writes a row"


def test_the_grace_lifts_on_a_passing_probe(conn, monkeypatch):
    _seed_stale_probe(conn)
    monkeypatch.setattr(check, "perform_check", _fail("pulse", "dns"))
    monkeypatch.setattr(check, "pulse_only_probe", _fail("pulse", "dns"))
    asyncio.run(main.run_cycle(conn, [], auth_enabled=False))
    assert not main._scoring_now(), "grace is open"

    monkeypatch.setattr(check, "perform_check", _pass_all)
    asyncio.run(main.run_cycle(conn, [], auth_enabled=False))
    assert main._scoring_now(), "a pass is positive proof the host is healthy again"


def test_an_outage_beginning_at_a_wake_is_delayed_not_lost(conn, monkeypatch):
    """The reason the grace is time-bounded. 'Suppress until a pass' alone never lifts if the
    monitor wakes into a real outage, and would leave it blind indefinitely."""
    _seed_stale_probe(conn)
    monkeypatch.setattr(config, "WAKE_GRACE_S", 0)      # the bound, expired immediately
    monkeypatch.setattr(check, "perform_check", _fail("render"))
    monkeypatch.setattr(check, "render_only_probe", _fail("render"))
    monkeypatch.setattr(check, "pulse_only_probe", _pass_pulse)

    asyncio.run(main.run_cycle(conn, [], auth_enabled=False))

    assert db.get_state(conn, track="main").status == "DOWN", \
        "once the bound expires the outage scores normally"


def test_no_grace_on_a_normal_cadence(conn, monkeypatch):
    """A cycle following the previous one on schedule must not be suppressed."""
    from monitor.timeutil import now_iso
    conn.execute("INSERT INTO checks (ts, ok, latency_ms, layer) VALUES (?, 1, 1.0, 'render')",
                 (now_iso(),))
    conn.commit()
    monkeypatch.setattr(check, "perform_check", _fail("render"))
    monkeypatch.setattr(check, "render_only_probe", _fail("render"))
    monkeypatch.setattr(check, "pulse_only_probe", _pass_pulse)

    asyncio.run(main.run_cycle(conn, [], auth_enabled=False))

    assert db.get_state(conn, track="main").status == "DOWN"


def test_a_suppressed_probe_is_recorded_as_unscored(conn, monkeypatch):
    """Without this, a failing row that did not page looks identical to one that simply had
    not reached the floor. The only trace was a log line, which dies with the terminal."""
    _seed_stale_probe(conn)
    monkeypatch.setattr(check, "perform_check", _fail("pulse", "dns"))
    monkeypatch.setattr(check, "pulse_only_probe", _fail("pulse", "dns"))

    asyncio.run(main.run_cycle(conn, [], auth_enabled=False))

    row = conn.execute("SELECT ok, fail_reason, scored FROM checks WHERE fail_reason='dns' "
                       "ORDER BY id DESC LIMIT 1").fetchone()
    assert row["scored"] == 0, "the row must say it did not count"
    assert row["ok"] == 0 and row["fail_reason"] == "dns", "and still record what happened"


def test_a_normal_probe_is_recorded_as_scored(conn, monkeypatch):
    from monitor.timeutil import now_iso
    conn.execute("INSERT INTO checks (ts, ok, latency_ms, layer) VALUES (?, 1, 1.0, 'render')",
                 (now_iso(),))
    conn.commit()
    monkeypatch.setattr(check, "perform_check", _fail("render"))
    monkeypatch.setattr(check, "render_only_probe", _fail("render"))
    monkeypatch.setattr(check, "pulse_only_probe", _pass_pulse)

    asyncio.run(main.run_cycle(conn, [], auth_enabled=False))

    assert all(r["scored"] == 1 for r in conn.execute("SELECT scored FROM checks WHERE fail_reason IS NOT NULL"))
