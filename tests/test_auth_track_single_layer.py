"""B50 + B45: the auth track keeps ONE layer, and its burst never spends a login.

The defect these pin is the 2026-09-04 incident. A recovery login that died at the login page
was filed on the auth track's `render` layer. Nothing on that track can ever record a `render`
PASS -- every ok=True return in journey.py is `authed`, and run_authed_check only ever reports
`authed` -- so the DOWN it opened could not be closed by any probe the track was capable of
taking. It stood for 12h 36m while the authed check passed every single minute, and needed a
hand-edited database to clear.

Measured over the whole history at the time: 22 auth-track `render` rows, every one a failure,
zero passes.

Three behaviours are pinned here:

  relabel        every outcome of run_journey reports layer="authed", at every stage
  recovery rule  a DOWN whose evidence includes a failed login only STARTS recovering on a
                 successful login -- the gap the relabel opens, since one shared layer means a
                 cheap check could otherwise close an outage raised by failed logins
  burst          the auth burst calls the cheap check directly and stops when there is no
                 usable session, instead of quietly becoming a credentialed login (Rule 5)
"""
import asyncio
from contextlib import asynccontextmanager

import pytest

import config
from monitor import db, journey, main
from monitor.check import CheckResult
from monitor.state import LayerEvidence, MonitorState, apply_check
from monitor.timeutil import now_iso
from patchright.async_api import Error as PatchrightError

LOGIN = "https://bank.example/dbank/live/app/login/consumer"


def T(seconds: int) -> str:
    return f"2026-09-05T03:{seconds // 60:02d}:{seconds % 60:02d}+00:00"


def _step(state, ok, reason, ts, *, is_login=False, passes=3):
    return apply_check(state, ok, reason, ts, "authed", down_confidence=4, min_failed_probes=4,
                       stale_after_s=600, recovery_passes=passes, is_login=is_login)


# --- the relabel ---------------------------------------------------------------------

def _fake_login_browser(monkeypatch, *, goto_raises=False):
    """A browser where the login page is reachable or not, and the username box never shows."""
    class FakePage:
        url = LOGIN
        async def goto(self, *a, **k):
            if goto_raises:
                raise PatchrightError("net::ERR_CONNECTION_REFUSED")
            return None
        async def close(self): pass
        def get_by_role(self, *a, **k): return self
        def nth(self, _i): return self
        @property
        def first(self): return self
        async def wait_for(self, *a, **k):
            raise PatchrightError("locator never became visible")

    class FakeContext:
        async def new_page(self): return FakePage()

    @asynccontextmanager
    async def fake_browser(*a, **k):
        yield FakeContext()

    monkeypatch.setattr(journey, "open_journey_browser", fake_browser)
    monkeypatch.setattr(journey, "capture_masked_screenshot",
                        lambda *a, **k: asyncio.sleep(0, result=None))


def _run_journey():
    return asyncio.run(journey.run_journey(
        login_url=LOGIN, login_user="u", login_password="p", totp_secret=None,
        error_banner_text="err", authed_text=None, authed_role="link", authed_name="Logout",
        browser_channel="chrome", browser_timeout_ms=100, challenge_timeout_ms=100,
        artifacts_dir="/tmp", mask_patterns=[], masking_enabled=False))


def test_a_login_that_cannot_reach_the_page_reports_the_authed_layer(monkeypatch):
    """Was layer="render" -- a layer this track can never pass, so the DOWN it opened stuck."""
    _fake_login_browser(monkeypatch, goto_raises=True)
    result = _run_journey()
    assert result.fail_reason == "nav_error"
    assert result.layer == "authed", "the auth track must not file evidence on `render`"


def test_a_login_that_cannot_find_the_form_reports_the_authed_layer(monkeypatch):
    """The 2026-09-04 failure exactly: five of these, all filed on `render`."""
    _fake_login_browser(monkeypatch)
    result = _run_journey()
    assert result.fail_reason == "element_missing"
    assert result.layer == "authed"


# --- the invariant the 2026-09-04 incident violated -----------------------------------

def test_an_incident_recovers_from_the_layer_that_opened_it():
    """The property that was missing. Mixed cheap-check and login failures now share one
    layer, so the passes that follow can close what they opened. Replays 2026-09-04: three
    cheap-check failures, then a login failure that crosses the floor."""
    state = MonitorState(status="UP", since_ts=None)
    for i in range(3):
        state, events = _step(state, False, "element_missing", T(i * 30))
    assert state.status == "UP" and not events, "three failures is below the floor of 4"

    state, events = _step(state, False, "element_missing", T(120), is_login=True)
    assert state.status == "DOWN", "the fourth failure crosses the floor"
    assert state.cause_layer == "authed", "and it opens on a layer this track can pass"

    state, _ = _step(state, True, None, T(180), is_login=True)     # a login must start it
    state, _ = _step(state, True, None, T(240))
    state, events = _step(state, True, None, T(300))
    assert state.status == "UP", "three consecutive passes must close it"
    assert any(type(e).__name__ == "RecoveryEvent" for e in events)


# --- the recovery rule ----------------------------------------------------------------

def test_a_login_caused_down_is_not_cleared_by_cheap_checks_alone():
    """Otherwise the monitor declares "sign-in works again" on a cached cookie, while
    customers who are not already signed in still cannot get in."""
    state = MonitorState(status="UP", since_ts=None)
    for i in range(4):
        state, _ = _step(state, False, "element_missing", T(i * 30), is_login=True)
    assert state.status == "DOWN"

    for i in range(10):
        state, events = _step(state, True, None, T(300 + i * 30))
        assert not events
    assert state.status == "DOWN", "ten cheap passes must not close a login-caused outage"
    assert state.consecutive_passes == 0, "and none of them may count toward recovery"


def test_a_successful_login_starts_recovery_and_cheap_checks_finish_it():
    """Only the FIRST pass is gated. Once a login has proved sign-in works, the cheap check
    is enough corroboration -- and the auth track deliberately stops logging in once it has
    a fresh session, so demanding three logins would stall recovery for 20+ minutes."""
    state = MonitorState(status="UP", since_ts=None)
    for i in range(4):
        state, _ = _step(state, False, "element_missing", T(i * 30), is_login=True)

    state, _ = _step(state, True, None, T(300), is_login=True)
    assert state.status == "DOWN" and state.consecutive_passes == 1

    state, _ = _step(state, True, None, T(330))
    state, events = _step(state, True, None, T(360))
    assert state.status == "UP"
    assert any(type(e).__name__ == "RecoveryEvent" for e in events)


def test_a_down_with_no_login_evidence_still_recovers_on_cheap_checks():
    """No regression: an outage the cheap check found is one the cheap check can clear."""
    state = MonitorState(status="UP", since_ts=None)
    for i in range(4):
        state, _ = _step(state, False, "element_missing", T(i * 30))
    assert state.status == "DOWN"

    for i in range(2):
        state, _ = _step(state, True, None, T(300 + i * 30))
    state, events = _step(state, True, None, T(360))
    assert state.status == "UP"
    assert any(type(e).__name__ == "RecoveryEvent" for e in events)


def test_the_login_flag_survives_a_round_trip_through_the_database(tmp_path):
    """The rule is useless if the flag is lost the moment the state row is written."""
    conn = db.get_connection(str(tmp_path / "flag.db"))
    db.init_db(conn)
    state = MonitorState(status="DOWN", since_ts=T(0), cause_layer="authed",
                         layers={"authed": LayerEvidence(consecutive=4, confidence=4,
                                                         fail_reasons=("element_missing",) * 4,
                                                         last_probe_ts=T(0), run_started_ts=T(0),
                                                         from_login=True)})
    db.set_state(conn, state, track="auth")
    assert db.get_state(conn, track="auth").evidence("authed").from_login is True
    conn.close()


# --- the burst ------------------------------------------------------------------------

@pytest.fixture
def conn(tmp_path):
    c = db.get_connection(str(tmp_path / "burst.db"))
    db.init_db(c)
    yield c
    c.close()


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch):
    monkeypatch.setattr(config, "MAIN_BURST_GAP_S", 0)
    monkeypatch.setattr(config, "AUTH_BURST_GAP_S", 0)
    monkeypatch.setattr(config, "BURST_JITTER_S", 0)
    main._GRACE["until"] = None
    yield
    main._GRACE["until"] = None


def _open_run(conn):
    """A run already holding one failure, as a burst always finds it.

    The timestamps must be CURRENT, not the fixed T() stamps used by the pure state tests:
    the burst's probes carry a real now_iso(), and evidence older than EVIDENCE_STALE_AFTER_S
    is discarded on the next failure -- which resets run_started_ts and ends the burst after
    one probe. That is the staleness rule working correctly; it just makes a stale fixture
    look like a burst bug."""
    now = now_iso()
    state = MonitorState(status="UP", since_ts=None,
                         layers={"authed": LayerEvidence(consecutive=1, confidence=1,
                                                         fail_reasons=("element_missing",),
                                                         last_probe_ts=now, run_started_ts=now)})
    db.set_state(conn, state, track="auth")
    return state


def test_the_burst_never_reaches_the_login_path(conn, monkeypatch):
    """Rule 5: a burst consumes zero logins. It used to call _run_auth_probe, which contains
    the recovery login, so a burst probe became a credentialed attempt whenever the session
    went stale mid-burst -- observed 2026-08-31 and 2026-09-04."""
    def _boom(*a, **k):
        raise AssertionError("the burst must never enter the recovery-login path")
    monkeypatch.setattr(main, "_run_full_login", _boom)
    monkeypatch.setattr(main, "_run_auth_probe", _boom)
    monkeypatch.setattr(main, "_session_is_usable", lambda: True)

    async def cheap():
        return CheckResult(ok=False, http_status=None, latency_ms=1.0,
                           fail_reason="element_missing", layer="authed")
    monkeypatch.setattr(main, "_cheap_authed_check", cheap)

    state, _last, _bid = asyncio.run(main._run_auth_burst_reprobes(
        conn, [], _open_run(conn), cycle_id="c1", main_down=False))
    assert state.status == "DOWN", "four cheap re-probes still reach the floor"


def test_the_burst_stops_instead_of_spinning_without_a_session(conn, monkeypatch):
    """Every probe in that state returned an inert synthetic session_expired: it could not
    confirm the run and could not clear it, so the burst learned nothing while holding the
    cycle lock. On 2026-09-06 that made single cycles 3h 19m and 4h 25m long, because each
    gap spanned a host suspend."""
    calls = []
    async def cheap():
        calls.append(1)
        return CheckResult(ok=False, http_status=None, latency_ms=1.0,
                           fail_reason="element_missing", layer="authed")
    monkeypatch.setattr(main, "_cheap_authed_check", cheap)
    monkeypatch.setattr(main, "_session_is_usable", lambda: False)

    state, last, _bid = asyncio.run(main._run_auth_burst_reprobes(
        conn, [], _open_run(conn), cycle_id="c1", main_down=False))

    assert calls == [], "no probe should run without a usable session"
    assert last is None
    assert state.status == "UP", "the run is left open for the next cycle, not resolved"


def test_the_auth_burst_waits_the_auth_gap(conn, monkeypatch):
    """[2026-09-15] The auth track has its own gap. Sharing the main track's 25s put this
    track's burst span at 267s against pulse's 77s -- the layer that DEFINES up was the
    slowest to confirm, because its failing probe already costs the whole frame budget and
    the gap was piled on top of that.

    The assertion is which CONSTANT reaches this burst, not what it is set to, so retuning
    either dial leaves this test alone."""
    gaps = []

    async def _record(gap_s):
        gaps.append(gap_s)

    monkeypatch.setattr(main, "_wait_burst_gap", _record)
    monkeypatch.setattr(config, "MAIN_BURST_GAP_S", 25)
    monkeypatch.setattr(config, "AUTH_BURST_GAP_S", 10)
    monkeypatch.setattr(main, "_session_is_usable", lambda: True)

    async def cheap():
        return CheckResult(ok=False, http_status=None, latency_ms=1.0,
                           fail_reason="element_missing", layer="authed")
    monkeypatch.setattr(main, "_cheap_authed_check", cheap)

    asyncio.run(main._run_auth_burst_reprobes(
        conn, [], _open_run(conn), cycle_id="c1", main_down=False))

    assert gaps, "the burst must have waited at least once"
    assert set(gaps) == {10}, f"the auth burst must use AUTH_BURST_GAP_S, got {gaps}"
