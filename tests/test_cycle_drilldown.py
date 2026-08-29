"""B25: the drill-down shows one line per probe actually run.

Two defects, one cause. `perform_check()` runs the pulse and then the render and writes a
SINGLE `checks` row carrying a single latency -- the pulse's -- labelled `render` when the
pulse passed. So the drill-down showed two lines where three probes ran, and the line it
labelled `render` reported the pulse's timing. Measured on the live DB: a render line reading
165 ms for a page load that actually took 1,702 ms, understated 10.5x.

Nothing here summarises a minute. Each emitted line carries one real measurement, taken from
the column that recorded that specific leg.
"""
import sys

import pytest

from monitor import db

web_app = sys.modules.get("monitor.web.app")
if web_app is None:
    import importlib
    web_app = importlib.import_module("monitor.web.app")


@pytest.fixture
def conn(tmp_path):
    c = db.get_connection(str(tmp_path / "b25.db"))
    db.init_db(c)
    yield c
    c.close()


def _seed_healthy_cycle(conn):
    """The real shape of cycle caf2a56b: pulse 164.9ms, render 1701.9ms, both passing, one
    combined checks row carrying only the pulse's latency."""
    db.append_cycle(
        conn, cycle_id="c1", ts="2026-08-29T22:00:10+00:00",
        pulse_ok=True, render_ok=True, authed_ok=True, verdict="UP",
        pulse_latency_ms=164.87, render_latency_ms=1701.93, authed_latency_ms=3715.28,
    )
    db.append_check(conn, ts="2026-08-29T22:00:12+00:00", ok=True, http_status=200,
                    latency_ms=164.87, fail_reason=None, browser_mode="headless",
                    layer="render", burst_id=None, cycle_id="c1")
    db.append_check(conn, ts="2026-08-29T22:00:16+00:00", ok=True, http_status=None,
                    latency_ms=3715.28, fail_reason=None, browser_mode="headed-xvfb",
                    layer="authed", burst_id=None, cycle_id="c1")


def _lines(conn, cycle_id="c1"):
    return web_app.api_cycle_probes(cycle_id, conn=conn)["rows"]


# --- the split ------------------------------------------------------------------------

def test_one_line_per_layer(conn):
    _seed_healthy_cycle(conn)
    assert [r["layer"] for r in _lines(conn)] == ["pulse", "render", "authed"]


def test_render_line_reports_the_render_not_the_pulse(conn):
    """The headline defect: the render line used to show 164.87 -- the pulse's number."""
    _seed_healthy_cycle(conn)
    by_layer = {r["layer"]: r for r in _lines(conn)}
    assert round(by_layer["pulse"]["latency_ms"], 2) == 164.87
    assert round(by_layer["render"]["latency_ms"], 2) == 1701.93
    assert by_layer["render"]["latency_ms"] != by_layer["pulse"]["latency_ms"]


def test_http_status_stays_with_the_pulse(conn):
    """The status came from the pulse's own GET; Playwright's navigation is not what produced
    it, so it must not appear on the render line."""
    _seed_healthy_cycle(conn)
    by_layer = {r["layer"]: r for r in _lines(conn)}
    assert by_layer["pulse"]["http_status"] == 200
    assert by_layer["render"]["http_status"] is None


def test_failed_pulse_produces_no_render_line(conn):
    """When the pulse fails, render is deliberately never attempted -- so there is no render
    probe to show. Inventing a line here would be reporting a measurement nobody took."""
    db.append_cycle(conn, cycle_id="c2", ts="2026-08-27T12:14:07+00:00",
                    pulse_ok=False, render_ok=None, authed_ok=None, verdict="DOWN",
                    pulse_latency_ms=10009.0, render_latency_ms=None)
    db.append_check(conn, ts="2026-08-27T12:14:07+00:00", ok=False, http_status=None,
                    latency_ms=10009.0, fail_reason="timeout", browser_mode="headless",
                    layer="pulse", burst_id=None, cycle_id="c2")
    assert [r["layer"] for r in _lines(conn, "c2")] == ["pulse"]


def test_burst_probes_are_left_alone(conn):
    """Burst re-probes are already single-layer with their own measured latency. Splitting
    them would duplicate evidence, and bursts are first-class and unhideable."""
    _seed_healthy_cycle(conn)
    db.append_check(conn, ts="2026-08-29T22:00:30+00:00", ok=False, http_status=None,
                    latency_ms=8940.0, fail_reason="element_missing", browser_mode="headless",
                    layer="render", burst_id="b1", cycle_id="c1")
    lines = _lines(conn)
    assert [r["layer"] for r in lines] == ["pulse", "render", "authed", "render"]
    burst = [r for r in lines if r["burst_id"]][0]
    assert burst["latency_ms"] == 8940.0, "the burst probe keeps its own measurement"


def test_orphaned_checks_are_returned_unchanged(conn):
    """Rows from a cycle with no cycles row have no per-leg timings to read, so there is
    nothing to split -- return what was recorded rather than guessing."""
    db.append_check(conn, ts="2026-08-01T00:00:00+00:00", ok=True, http_status=200,
                    latency_ms=120.0, fail_reason=None, browser_mode="headless",
                    layer="render", burst_id=None, cycle_id="ghost")
    lines = _lines(conn, "ghost")
    assert len(lines) == 1 and lines[0]["layer"] == "render"


def test_auth_layer_is_shown_under_its_layer_name(conn):
    """3 early rows stored `auth`, the TRACK name. CLAUDE.md splits the spelling on purpose:
    `authed` is the layer. Display-side only -- the CSV export still reads the table."""
    db.append_cycle(conn, cycle_id="c3", ts="2026-08-11T19:16:45+00:00",
                    pulse_ok=True, render_ok=True, authed_ok=False, verdict="UP")
    db.append_check(conn, ts="2026-08-11T19:17:22+00:00", ok=False, http_status=None,
                    latency_ms=1.0, fail_reason="session_expired", browser_mode="headed-xvfb",
                    layer="auth", burst_id=None, cycle_id="c3")
    assert [r["layer"] for r in _lines(conn, "c3")] == ["authed"]


# --- the timing fix -------------------------------------------------------------------

def test_render_only_probe_measures_its_own_latency(monkeypatch):
    """B25's other half: render_only_probe hardcoded latency_ms=0.0, so every burst render
    probe went untimed -- 102 rows on the live DB, all of them in the minutes worth studying.
    A literal zero is not a fast probe, it is no measurement at all."""
    import asyncio
    from monitor import check

    async def slow_browser_check(*a, **k):
        await asyncio.sleep(0.05)
        return True, None, None, "https://example.org/"

    monkeypatch.setattr(check, "browser_check", slow_browser_check)
    result = asyncio.run(check.render_only_probe(
        "https://example.org/", "hello", None, None, 15000, "/tmp", headless=True))

    assert result.latency_ms >= 45, f"must reflect real elapsed time, got {result.latency_ms}"
    assert result.latency_ms != 0.0
