"""Stage H P2: the uptime denominator, authed_ok's NULL semantics, and -- the substantive
one -- the authed layer regaining the ability to report an outage.

The authed-detection tests are the important ones. Before this change run_authed_check
mapped every "loaded but the marker is missing" to session_expired, which Rule 3 "session_expired never scores" makes
completely inert: no score, no probe-floor credit, and it never set burst_started_ts so
the auth burst never even began. The effect was that Rule 10's authed wording -- "online
banking behind login not rendering", which IS v3.8's definition of the platform being
down -- could not be produced by the check whose whole job is to detect it. These pin both
halves: a real platform failure now scores, and a genuinely expired session still doesn't.
"""
import sqlite3

import pytest

import config
from monitor import db, journey
from monitor.state import classify


# --- uptime denominator ------------------------------------------------------------

@pytest.fixture
def conn(tmp_path):
    c = db.get_connection(str(tmp_path / "p2.db"))
    db.init_db(c)
    yield c
    c.close()


def _cycle(conn, n, verdict):
    db.append_cycle(conn, cycle_id=f"c{n:04d}", ts=f"2026-08-24T10:{n:02d}:00+00:00",
                    pulse_ok=True, render_ok=True, authed_ok=None, verdict=verdict)


def test_uptime_ignores_config_error_cycles(conn):
    """The headline bug: one stale password latches auth CONFIG_ERROR, every cycle then
    writes verdict='CONFIG_ERROR' (worst_of), and the dashboard reported 0.0% uptime for a
    platform whose pulse and render passed every single minute."""
    for n in range(10):
        _cycle(conn, n, "CONFIG_ERROR")
    conn.commit()

    # Not 0.0 -- there is simply no platform evidence in this window.
    assert db.uptime_pct(conn, "2026-08-24T00:00:00+00:00") is None


def test_uptime_ignores_degraded_cycles(conn):
    """Rule 9's "no minute may vanish" self-health rows mean 'we could not measure', not 'the platform was down'."""
    for n in range(5):
        _cycle(conn, n, "DEGRADED")
    for n in range(5, 10):
        _cycle(conn, n, "UP")
    conn.commit()

    # 5 UP out of 5 measured -- the 5 unmeasurable minutes are excluded, not counted against.
    assert db.uptime_pct(conn, "2026-08-24T00:00:00+00:00") == 100.0


def test_uptime_still_counts_real_downtime(conn):
    """The exclusions must not become a way to hide outages: DOWN still divides."""
    for n in range(8):
        _cycle(conn, n, "UP")
    for n in range(8, 10):
        _cycle(conn, n, "DOWN")
    conn.commit()

    assert db.uptime_pct(conn, "2026-08-24T00:00:00+00:00") == 80.0


def test_uptime_mixed_window_excludes_only_the_unmeasurable(conn):
    for n in range(6):
        _cycle(conn, n, "UP")
    for n in range(6, 8):
        _cycle(conn, n, "DOWN")
    for n in range(8, 10):
        _cycle(conn, n, "CONFIG_ERROR")
    conn.commit()

    assert db.uptime_pct(conn, "2026-08-24T00:00:00+00:00") == 75.0  # 6 of 8 measured


def test_uptime_is_none_on_an_empty_window(conn):
    """Unchanged behaviour, and the SUM-over-zero-rows NULL must not become a crash."""
    assert db.uptime_pct(conn, "2026-08-24T00:00:00+00:00") is None


# --- authed_ok NULL semantics ------------------------------------------------------

def test_cycles_authed_ok_accepts_null(conn):
    """the "Cross-track suppression" section: NULL means the auth track didn't run. Pinning that the column really is
    nullable, since main.py now writes None for 'never looked'."""
    db.append_cycle(conn, cycle_id="x", ts="2026-08-24T10:00:00+00:00",
                    pulse_ok=True, render_ok=True, authed_ok=None, verdict="UP")
    conn.commit()
    row = conn.execute("SELECT authed_ok FROM cycles WHERE cycle_id='x'").fetchone()
    assert row["authed_ok"] is None


# --- the authed layer can report an outage again -----------------------------------

def test_same_route_ignores_query_and_trailing_slash():
    """Banking apps append session/nonce params; those don't mean you were redirected."""
    assert journey._same_route("https://b.com/home", "https://b.com/home") is True
    assert journey._same_route("https://b.com/home?sid=abc", "https://b.com/home") is True
    assert journey._same_route("https://b.com/home/", "https://b.com/home") is True
    assert journey._same_route("https://B.com/home", "https://b.com/home") is True
    assert journey._same_route("https://b.com/login", "https://b.com/home") is False
    assert journey._same_route("http://b.com/home", "https://b.com/home") is False


def test_a_5xx_behind_login_is_hard_evidence_not_a_session_problem():
    """The reason capturing goto's Response matters. Playwright does not raise on HTTP
    error status, so this status used to be discarded and a 500 error page behind login
    classified as element_missing -> rewritten to session_expired -> weight 0 -> a spent
    recovery login and no evidence at all."""
    cls, weight = classify("bad_status:500")
    assert (cls, weight) == ("hard", 2)

    # Two hard probes reach DOWN_CONFIDENCE, so the 3-probe floor is what still governs
    # timing -- exactly as CLAUDE.md's outcome table describes for a hard-heavy burst.
    assert weight * 2 >= config.AUTH_DOWN_CONFIDENCE
    assert config.AUTH_MIN_FAILED_PROBES == 3


def test_missing_authed_content_now_scores_instead_of_being_inert():
    """The core of the fix: still-on-the-authed-route-but-not-rendering must be able to
    accumulate toward DOWN, where session_expired provably cannot."""
    missing_cls, missing_weight = classify("element_missing")
    session_cls, session_weight = classify("session_expired")

    assert (missing_cls, missing_weight) == ("soft", 1)
    assert (session_cls, session_weight) == ("session", 0)

    # Four soft probes reach the auth track's threshold; session_expired never would.
    assert missing_weight * 4 >= config.AUTH_DOWN_CONFIDENCE
    assert session_weight * 99 < config.AUTH_DOWN_CONFIDENCE


@pytest.mark.parametrize(
    "page_url, login_form_visible, expected",
    [
        # Still on the authed route -> platform problem, never a session problem, and the
        # login form is not even consulted.
        ("https://bank.com/home", True, False),
        ("https://bank.com/home", False, False),
        ("https://bank.com/home?sid=xyz", True, False),
        # Bounced away AND a login form is there -> the classic expired-session signature.
        ("https://bank.com/login", True, True),
        # Bounced away to something that is NOT a login form (maintenance/error page) ->
        # platform evidence, so it must not be excused as a session problem.
        ("https://bank.com/maintenance", False, False),
    ],
)
def test_bounced_to_login_requires_both_a_redirect_and_a_login_form(
    page_url, login_form_visible, expected, monkeypatch
):
    from patchright.async_api import Error as PatchrightError
    import asyncio

    class _Locator:
        @property
        def first(self):
            return self

        async def wait_for(self, **_kwargs):
            if not login_form_visible:
                raise PatchrightError("not found")

    class _Page:
        url = page_url

    monkeypatch.setattr(journey, "username_field", lambda page: _Locator())

    got = asyncio.run(journey.bounced_to_login(_Page(), "https://bank.com/home", 100))
    assert got is expected
