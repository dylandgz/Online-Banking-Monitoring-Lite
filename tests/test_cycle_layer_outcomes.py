"""B9: cycles.pulse_ok / render_ok describe the whole minute, not its first probe.

The defect was not bad arithmetic -- for a single-probe minute the old inference was sound.
It was scope. Both columns were derived from the cycle's FIRST probe and every burst
re-probe was discarded from the summary, so on exactly the minutes bad enough to trigger a
burst -- the ones an operator scrutinises -- the one-row-per-minute audit view was least
complete.

The headline test here replays a real cycle from the live database (fc32039c): the opening
pulse failed, so render was correctly skipped and recorded NULL; the burst then ran a render
probe that PASSED, and the row still said NULL. "We never measured render" about a minute in
which render was measured and passed -- and it is precisely the fact worth having during a
DOWN incident, because it says the login page itself was fine.
"""
import asyncio

import pytest

import config
from monitor import check, db, main
from monitor.check import CheckResult


@pytest.fixture
def conn(tmp_path):
    c = db.get_connection(str(tmp_path / "b9.db"))
    db.init_db(c)
    yield c
    c.close()


def _cycle(conn):
    return dict(conn.execute("SELECT * FROM cycles ORDER BY ts DESC LIMIT 1").fetchone())


# --- the folding rules ---------------------------------------------------------------

def test_fold_layer_records_first_outcome_then_ands():
    o = {"pulse": None, "render": None}
    assert o["render"] is None, "nothing measured yet"
    main._fold_layer(o, "render", True)
    assert o["render"] is True
    main._fold_layer(o, "render", False)
    assert o["render"] is False, "any failure in the minute makes the layer not-ok"
    main._fold_layer(o, "render", True)
    assert o["render"] is False, "a later pass does not erase a failure that happened"


def test_combined_probe_pulse_failure_leaves_render_unmeasured():
    # perform_check short-circuits: a failed pulse means render is never attempted, so the
    # minute genuinely has no render measurement *yet*.
    o = {"pulse": None, "render": None}
    main._fold_combined_probe(o, CheckResult(ok=False, http_status=None, latency_ms=1.0,
                                             fail_reason="timeout", layer="pulse"))
    assert o == {"pulse": False, "render": None}


def test_combined_probe_render_result_implies_the_pulse_passed():
    o = {"pulse": None, "render": None}
    main._fold_combined_probe(o, CheckResult(ok=False, http_status=200, latency_ms=1.0,
                                             fail_reason="element_missing", layer="render"))
    assert o == {"pulse": True, "render": False}, "reaching render at all means the pulse passed"


# --- the real cycle this bug was found in --------------------------------------------

def test_burst_probe_outcomes_reach_the_summary_row(conn, monkeypatch):
    """Live cycle fc32039c is what opened B9: the opening pulse failed, the burst's re-probe
    passed, and the summary row still reported that layer as never measured.

    [2026-08-30] The original replay had the burst running a RENDER probe after a pulse
    failure, because the burst alternated layers. Under the same-layer burst (B37) a pulse
    failure is confirmed with pulse probes, so that exact sequence can no longer occur. The
    defect being pinned is unchanged: whatever the burst learns must reach the summary."""
    monkeypatch.setattr(config, "BURST_GAP_S", 0)
    monkeypatch.setattr(config, "BURST_JITTER_S", 0)

    calls = {"n": 0}

    async def failing_pulse(*a, **k):
        return CheckResult(ok=False, http_status=None, latency_ms=5.0,
                           fail_reason="timeout", layer="pulse", pulse_latency_ms=5.0)

    async def pulse_recovers(*a, **k):
        calls["n"] += 1
        return CheckResult(ok=True, http_status=200, latency_ms=4.0, fail_reason=None, layer="pulse")

    monkeypatch.setattr(check, "perform_check", failing_pulse)
    monkeypatch.setattr(check, "pulse_only_probe", pulse_recovers)

    asyncio.run(main.run_cycle(conn, [], auth_enabled=False))

    row = _cycle(conn)
    assert calls["n"] >= 1, "the burst must re-probe the layer that failed"
    assert row["pulse_ok"] == 0, (
        "the opening pulse failed this minute, and a later pass does not erase that it "
        "happened -- the column answers 'did every pulse check pass?'"
    )
    assert row["render_ok"] is None, "render was never attempted, so it is not measured"


def test_render_failure_during_a_burst_is_not_hidden_by_a_later_pass(conn, monkeypatch):
    """The inverse guard: a failure that happened must survive into the summary even though
    the burst went on to clear and the cycle's verdict is UP."""
    monkeypatch.setattr(config, "BURST_GAP_S", 0)
    monkeypatch.setattr(config, "BURST_JITTER_S", 0)

    async def failing_render_probe(*a, **k):
        return CheckResult(ok=False, http_status=200, latency_ms=1.0,
                           fail_reason="element_missing", layer="render",
                           pulse_latency_ms=2.0, render_latency_ms=9.0)

    async def passing_render(*a, **k):
        return CheckResult(ok=True, http_status=None, latency_ms=0.0, fail_reason=None, layer="render")

    async def passing_pulse(*a, **k):
        return CheckResult(ok=True, http_status=200, latency_ms=4.0, fail_reason=None, layer="pulse")

    monkeypatch.setattr(check, "perform_check", failing_render_probe)
    monkeypatch.setattr(check, "render_only_probe", passing_render)
    monkeypatch.setattr(check, "pulse_only_probe", passing_pulse)

    asyncio.run(main.run_cycle(conn, [], auth_enabled=False))

    row = _cycle(conn)
    assert row["verdict"] == "UP", "one failure then a pass is a flap, not an outage"
    assert row["render_ok"] == 0, "the flap still happened -- the audit row must not erase it"
    assert row["pulse_ok"] == 1


def test_clean_cycle_reports_both_layers_passing(conn, monkeypatch):
    async def all_good(*a, **k):
        return CheckResult(ok=True, http_status=200, latency_ms=3.0, fail_reason=None,
                           layer="render", pulse_latency_ms=3.0, render_latency_ms=40.0)

    monkeypatch.setattr(check, "perform_check", all_good)
    asyncio.run(main.run_cycle(conn, [], auth_enabled=False))

    row = _cycle(conn)
    assert (row["pulse_ok"], row["render_ok"], row["verdict"]) == (1, 1, "UP")


def test_cycle_fail_layer_names_the_layer_that_opened_the_incident(conn, monkeypatch):
    """[B37] Once the pulse recovers, perform_check reports layer="render" again -- so
    taking fail_layer from the last probe logged a pulse-caused incident as "render: dns",
    naming a layer that was passing alongside a reason it never produced."""
    monkeypatch.setattr(config, "BURST_GAP_S", 0)
    monkeypatch.setattr(config, "BURST_JITTER_S", 0)

    async def pulse_down(*a, **k):
        return CheckResult(ok=False, http_status=None, latency_ms=1.0, fail_reason="dns",
                           layer="pulse", pulse_latency_ms=1.0)
    monkeypatch.setattr(check, "perform_check", pulse_down)
    monkeypatch.setattr(check, "pulse_only_probe", pulse_down)
    monkeypatch.setattr(check, "render_only_probe", pulse_down)
    for _ in range(2):
        asyncio.run(main.run_cycle(conn, [], auth_enabled=False))

    row = _cycle(conn)
    assert row["verdict"] == "DOWN"
    assert row["fail_layer"] == "pulse", "the layer that opened the incident, not the last probed"
    assert row["fail_reason"] == "dns"
