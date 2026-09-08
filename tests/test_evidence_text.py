"""B63 + B65: record what the page SAID, and stop losing screenshots silently.

Both defects cost real diagnosis time. The 2026-09-06 latch recorded `mfa_failed` and nothing
else; the screen was showing "Login is currently unavailable. Please try again later." -- the
bank announcing its own outage -- and finding that out meant locating and opening a PNG. On the
night of 2026-09-04 there was no PNG to open: eleven consecutive failures captured nothing and
said nothing about why.

The load-bearing test is `test_alert_text_never_costs_the_check_result`. This code runs on every
failure path and its only job is a nicer log line, so anything it raises would destroy the
CheckResult describing the actual failure -- and guarded_cycle would record that as DEGRADED,
writing a real outage down as a monitor bug. An AttributeError did exactly that during
development, which is why the catch here is deliberately broad.
"""
import asyncio
import sys

import pytest

from monitor import db, journey

# `from monitor.web import app` gives the FastAPI INSTANCE, not the module -- the re-export
# in monitor/web/__init__.py shadows the submodule, and that file documents the trap. Reach
# the module the way the other web tests do.
import monitor.web  # noqa: F401  -- ensures the submodule is imported before the lookup
webapp = sys.modules["monitor.web.app"]


class FakeAlert:
    def __init__(self, text, visible=True):
        self._text, self._visible = text, visible
    async def is_visible(self): return self._visible
    async def text_content(self): return self._text


class FakeAlerts:
    def __init__(self, alerts): self._alerts = alerts
    async def count(self): return len(self._alerts)
    def nth(self, i): return self._alerts[i]


class FakePage:
    def __init__(self, alerts): self._alerts = FakeAlerts(alerts)
    def get_by_role(self, role, **k):
        assert role == "alert", "role=alert is the accessibility standard for error banners"
        return self._alerts


def _text(page):
    return asyncio.run(journey._visible_alert_text(page))


# --- what it captures -----------------------------------------------------------------

def test_it_captures_the_message_the_bank_displayed():
    """The 2026-09-06 banner, verbatim."""
    page = FakePage([FakeAlert("Login is currently unavailable. Please try again later.")])
    assert _text(page) == "Login is currently unavailable. Please try again later."


def test_an_empty_live_region_is_skipped():
    """Every captured DOM dump of the authed shell contains exactly one role="alert" node:
    an EMPTY sr-only live region. Without this it would match and record nothing, crowding out
    a real message further down the page."""
    page = FakePage([FakeAlert("   "), FakeAlert("Something actually went wrong")])
    assert _text(page) == "Something actually went wrong"


def test_a_progress_message_is_skipped():
    """"Verifying..." is a transient state, not an outcome -- the same reason
    classify_after_totp already skips it."""
    page = FakePage([FakeAlert("Verifying your code..."), FakeAlert("That code has expired")])
    assert _text(page) == "That code has expired"


def test_an_invisible_alert_is_skipped():
    page = FakePage([FakeAlert("hidden banner", visible=False), FakeAlert("shown banner")])
    assert _text(page) == "shown banner"


def test_whitespace_is_collapsed_and_long_text_truncated():
    """A row in an audit table should not be able to grow without bound."""
    page = FakePage([FakeAlert("  We are   temporarily\n\n  experiencing  difficulties  ")])
    assert _text(page) == "We are temporarily experiencing difficulties"

    page = FakePage([FakeAlert("x" * 5000)])
    assert len(_text(page)) == journey._EVIDENCE_TEXT_MAX


def test_no_alert_on_the_page_is_not_an_error():
    assert _text(FakePage([])) is None


# --- the safety property --------------------------------------------------------------

def test_alert_text_never_costs_the_check_result():
    """Diagnostics must never destroy evidence. A page object that does not implement
    get_by_role raised AttributeError straight out of _fail() during development, which would
    have turned a reportable platform failure into an unhandled exception -- recorded by
    guarded_cycle as DEGRADED, i.e. a real outage written down as our own bug."""
    class PageWithoutTheMethod:
        pass

    assert _text(PageWithoutTheMethod()) is None, "must swallow, not raise"

    class ExplodingPage:
        def get_by_role(self, *a, **k): raise RuntimeError("driver went away mid-failure")

    assert _text(ExplodingPage()) is None


# --- it survives to the record --------------------------------------------------------

@pytest.fixture
def conn(tmp_path):
    c = db.get_connection(str(tmp_path / "evidence_text.db"))
    db.init_db(c)
    yield c
    c.close()


def test_the_message_is_persisted_and_read_back(conn):
    db.append_check(conn, ts="2026-09-08T12:00:00+00:00", ok=False, http_status=None,
                    latency_ms=52815.0, fail_reason="mfa_failed", browser_mode="headed-xvfb",
                    layer="authed", evidence_text="Login is currently unavailable.")
    row = conn.execute("SELECT fail_reason, evidence_text FROM checks").fetchone()
    assert row["fail_reason"] == "mfa_failed"
    assert row["evidence_text"] == "Login is currently unavailable.", (
        "the reason code alone does not say the bank announced its own outage")


def test_a_passing_check_records_no_message(conn):
    db.append_check(conn, ts="2026-09-08T12:00:00+00:00", ok=True, http_status=200,
                    latency_ms=200.0, fail_reason=None, layer="render")
    assert conn.execute("SELECT evidence_text FROM checks").fetchone()["evidence_text"] is None


def test_the_csv_export_carries_it(conn):
    """Rule 15: the export stays row-for-row faithful to the table. A new column that never
    reaches the CSV is a column the audit trail does not have."""
    assert "evidence_text" in [key for _header, key in webapp._EXPORT_COLUMNS["checks"]]


def test_the_synthesised_pulse_line_never_inherits_it():
    """_split_main_probe fabricates a pulse line from one combined row. It already blanks
    fail_reason, page_url and screenshot_path there; the message must go the same way, or the
    drill-down attributes a banner to a probe that is a plain HTTP GET and saw no page at all."""
    row = {"burst_id": None, "layer": "render", "ok": 0, "latency_ms": 9.0,
           "fail_reason": "element_missing", "http_status": 200, "page_url": "https://x",
           "screenshot_path": "/tmp/a.png", "evidence_text": "the bank said something"}
    cycle = {"pulse_ok": 1, "pulse_latency_ms": 164.0, "render_latency_ms": 1701.0}

    pulse_line, render_line = webapp._split_main_probe(row, cycle)
    assert pulse_line["evidence_text"] is None, "the pulse leg never saw a page"
    assert render_line["evidence_text"] == "the bank said something"
