"""D1: Config-class is an allowlist, not a default. Plus Rule 5's credential breaker.

The acceptance test is the replay. Every CONFIG_ERROR latch this monitor has ever produced
came from a blind fallback, and three of them happened in the week of 2026-08-31 for a total
of ~21 hours blind:

  2026-08-31 22:35   spinner still turning, a merely slow login  -> bot_challenge, 15 min blind
  2026-09-03 00:36   "We are temporarily experiencing technical   -> bot_challenge,  7.3 h blind
                      difficulties" -- the bank's own outage banner
  2026-09-06 22:57   "Login is currently unavailable. Please try  -> mfa_failed,    13.7 h blind
                      again later." -- likewise

None was a configuration problem. All-time, `auth_rejected` -- the one Config reason that
genuinely means a human must look -- has never fired.

So `test_no_recorded_latch_would_latch_again` feeds all three shapes through the new
classifiers and asserts none produces a Config-class reason. The rest pin the allowlist that
replaces the guessing, and the breaker that replaces the protection it removed.
"""
import asyncio

import pytest

import config
from monitor import db, journey, main
from monitor.state import classify

REJECT = ["does not match our records"]


# --- fake page ------------------------------------------------------------------------

class FakeLocator:
    """Supports the three things the classifiers do: .or_(), .first, .is_visible()."""
    def __init__(self, visible: bool, name: str = ""):
        self._visible, self.name = visible, name
        self._alts = [self]

    def or_(self, other):
        combined = FakeLocator(False, f"{self.name}|{other.name}")
        combined._alts = self._alts + other._alts
        return combined

    @property
    def first(self):
        return self

    async def is_visible(self):
        # An or_-combined locator resolves to whichever alternative matches, so visibility
        # is ANY, not the combination's own flag. Getting this wrong made the mfa path look
        # broken when it was the harness that was.
        return self.any_visible

    @property
    def any_visible(self):
        return any(a._visible for a in self._alts)


class FakeAlert:
    def __init__(self, text): self._text = text
    async def is_visible(self): return True
    async def text_content(self): return self._text


class FakeAlerts:
    def __init__(self, texts): self._a = [FakeAlert(t) for t in texts]
    async def count(self): return len(self._a)
    def nth(self, i): return self._a[i]


class FakePage:
    """`visible` names the markers on screen; `alerts` are role=alert texts."""
    def __init__(self, visible=(), alerts=()):
        self._visible, self._alerts = set(visible), list(alerts)

    def get_by_role(self, role, name=None, **k):
        if role == "alert":
            return FakeAlerts(self._alerts)
        key = f"{role}:{name}"
        return FakeLocator(key in self._visible, key)

    def get_by_text(self, text, **k):
        return FakeLocator(text in self._visible, f"text:{text}")


def _after_submit(page, reject=REJECT):
    return asyncio.run(journey.classify_after_submit(
        page, authed_text=None, authed_role="link", authed_name="Logout",
        reject_patterns=reject, challenge_timeout_ms=10))


def _after_totp(page, reject=()):
    return asyncio.run(journey.classify_after_totp(
        page, reject_patterns=reject, challenge_timeout_ms=10, browser_timeout_ms=10))


@pytest.fixture(autouse=True)
def _fake_expect(monkeypatch):
    def fake_expect(locator):
        class Expectation:
            async def to_be_visible(self, timeout=None):
                if not locator.any_visible:
                    raise AssertionError("not visible")
            async def to_be_hidden(self, timeout=None):
                if locator.any_visible:
                    raise AssertionError("still visible")
        return Expectation()
    monkeypatch.setattr(journey, "expect", fake_expect)
    monkeypatch.setattr(journey, "_settle", lambda *a, **k: asyncio.sleep(0))


# --- THE ACCEPTANCE TEST --------------------------------------------------------------

def test_no_recorded_latch_would_latch_again():
    """All three real latches, replayed. Not one may produce a Config-class reason."""
    cases = [
        # 2026-08-31: a merely slow login. Nothing on screen yet, no message.
        ("2026-08-31 spinner", _after_submit(FakePage())),
        # 2026-09-03: the bank's outage banner, after credentials.
        ("2026-09-03 technical difficulties",
         _after_submit(FakePage(alerts=["We are temporarily experiencing technical difficulties"]))),
        # 2026-09-06: the bank's outage banner, after the code. MFA heading still up.
        ("2026-09-06 login unavailable",
         _after_totp(FakePage(visible=["heading:Login Security"],
                              alerts=["Login is currently unavailable. Please try again later."]))),
    ]
    for label, reason in cases:
        assert reason not in ("auth_rejected", "bot_challenge", "mfa_failed", "rate_limited"), (
            f"{label} produced Config-class {reason!r} -- the monitor would go blind again")
        assert classify(reason)[0] in ("hard", "soft"), (
            f"{label} produced {reason!r}, which does not score toward DOWN")


def test_the_three_latches_each_reach_the_floor_together():
    """Scoring, not just classing: four such probes in a row must cross the DOWN floor."""
    reason = _after_submit(FakePage(alerts=["Login is currently unavailable."]))
    weight = classify(reason)[1]
    assert weight * config.AUTH_MIN_FAILED_PROBES >= config.AUTH_DOWN_CONFIDENCE


# --- the allowlist --------------------------------------------------------------------

def test_a_configured_rejection_is_still_config_class():
    """The allowlist's whole point: a real credential rejection still halts logins (Rule 4).
    Pattern confirmed against captured markup 2026-09-08 -- the live banner reads "The
    Username and/or Password you entered does not match our records. Try again." """
    page = FakePage(visible=["does not match our records"])
    assert _after_submit(page) == "auth_rejected"
    assert classify("auth_rejected")[0] == "config"


def test_an_unrecognised_banner_is_not_a_rejection():
    """The bank saying something we cannot name is the platform's problem, not ours."""
    page = FakePage(alerts=["Your session could not be established at this time"])
    assert _after_submit(page) == "auth_unavailable"


def test_a_silent_unrecognised_screen_is_a_timeout():
    assert _after_submit(FakePage()) == "timeout"


def test_no_configured_patterns_means_nothing_can_latch():
    """MFA_REJECTED_TEXT ships empty because no capture of a refused code exists (Rule 12).
    Until one does, every post-code failure is platform evidence -- the safe direction."""
    page = FakePage(visible=["heading:Verify Information"], alerts=["Invalid code"])
    assert _after_totp(page, reject=()) == "auth_unavailable"


# --- B27: the success path that never existed ------------------------------------------

def test_a_login_with_no_step_up_is_recognised_as_success():
    """Was the fallback's worst case: a completely successful sign-in recorded as a bot
    challenge, latching the track."""
    assert _after_submit(FakePage(visible=["link:Logout"])) == "authed"


def test_the_mfa_screen_is_still_recognised():
    assert _after_submit(FakePage(visible=["heading:Login Security"])) == "mfa"


def test_a_marker_that_vanishes_between_the_wait_and_the_read_is_not_config_class():
    """The 2026-09-03 shape: something matched during the wait and was gone by the re-query
    -- a transient screen, or a host suspend mid-probe (B57). The old code returned
    bot_challenge here."""
    page = FakePage(alerts=["something the bank said"])
    for locator_name in ("link:Logout", "heading:Login Security"):
        page._visible.add(locator_name)
    real_is_visible = FakeLocator.is_visible

    async def vanished(self):
        return False
    FakeLocator.is_visible = vanished
    try:
        reason = _after_submit(page)
    finally:
        FakeLocator.is_visible = real_is_visible
    assert reason not in ("auth_rejected", "bot_challenge", "mfa_failed")


def test_after_totp_success_is_the_heading_disappearing():
    assert _after_totp(FakePage()) == "success"


# --- the breaker ----------------------------------------------------------------------

@pytest.fixture
def conn(tmp_path):
    c = db.get_connection(str(tmp_path / "breaker.db"))
    db.init_db(c)
    yield c
    c.close()


def _breaker(conn, channels=None):
    """The predicate is async now -- it dispatches the admin notification on the first
    decline, so it needs the channel list."""
    main._BREAKER["notified"] = False
    return asyncio.run(main._login_breaker_allows(conn, channels if channels is not None else []))


def _fail_logins(conn, n, minutes_ago_start=60):
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    for i in range(n):
        db.append_login_event(conn, ts=(now - timedelta(minutes=minutes_ago_start - i)).isoformat(),
                              ok=False, latency_ms=1000.0, reason="timeout")


def test_the_breaker_allows_attempts_below_the_threshold(conn):
    _fail_logins(conn, config.MAX_CONSECUTIVE_LOGIN_FAILURES - 1)
    assert _breaker(conn) is True


def test_the_breaker_halts_at_the_threshold(conn):
    """What replaces the protection D1 removed -- and it is screen-independent, so it covers
    screens nobody has captured. Four new ones appeared in one week."""
    from datetime import datetime, timezone
    _fail_logins(conn, config.MAX_CONSECUTIVE_LOGIN_FAILURES - 1)
    db.append_login_event(conn, ts=datetime.now(timezone.utc).isoformat(), ok=False,
                          latency_ms=1000.0, reason="timeout")
    assert _breaker(conn) is False


def test_the_breaker_releases_one_attempt_after_the_cooldown(conn):
    """Without this the track freezes: no logins means no session, which means no cheap
    checks either, so it would sit at DOWN long after the platform recovered."""
    _fail_logins(conn, config.MAX_CONSECUTIVE_LOGIN_FAILURES,
                 minutes_ago_start=config.LOGIN_BREAKER_COOLDOWN_S // 60 + 10)
    assert _breaker(conn) is True


def test_a_success_resets_the_streak(conn):
    from datetime import datetime, timezone
    _fail_logins(conn, config.MAX_CONSECUTIVE_LOGIN_FAILURES)
    db.append_login_event(conn, ts=datetime.now(timezone.utc).isoformat(), ok=True,
                          latency_ms=16000.0, reason=None)
    assert _breaker(conn) is True
    assert main._consecutive_login_failures(conn)[0] == 0


def test_down_fires_before_the_breaker_stops_trying(conn):
    """The ordering is the design, not a coincidence: if the breaker tripped first the track
    would go quiet with nobody paged -- the failure mode it exists to replace, from the other
    direction. config.py refuses to start if this is violated."""
    assert config.MAX_CONSECUTIVE_LOGIN_FAILURES > config.AUTH_MIN_FAILED_PROBES

    _fail_logins(conn, config.AUTH_MIN_FAILED_PROBES)
    assert _breaker(conn) is True, (
        "at the probe floor the alarm has just fired; the breaker must not have tripped yet")


def test_the_admin_is_notified_once_per_episode_not_once_per_cycle(conn):
    """The predicate runs every time a login is considered, so without the latch the admin
    would be mailed every minute for as long as the breaker stays open."""
    from datetime import datetime, timezone
    _fail_logins(conn, config.MAX_CONSECUTIVE_LOGIN_FAILURES - 1)
    db.append_login_event(conn, ts=datetime.now(timezone.utc).isoformat(), ok=False,
                          latency_ms=1000.0, reason="timeout")

    seen = []

    class Recorder:
        name = "recorder"
        def send(self, event): seen.append(event)

    main._BREAKER["notified"] = False
    for _ in range(5):
        assert asyncio.run(main._login_breaker_allows(conn, [Recorder()])) is False

    assert len(seen) == 1, f"expected one notification per episode, got {len(seen)}"
    event = seen[0]
    assert event.consecutive_failures == config.MAX_CONSECUTIVE_LOGIN_FAILURES
    assert event.cooldown_s == config.LOGIN_BREAKER_COOLDOWN_S
    assert event.last_reason == "timeout"


def test_the_breaker_notification_never_reaches_the_outage_recipients(monkeypatch):
    """It is not an outage and the operator has already been paged for the real one. SMS is
    for paging, so both SMS channels skip it rather than raising TypeError on an unknown
    event -- which dispatch() would otherwise log as a channel failure every time."""
    from monitor.channels import email_gmail as eg, sms_email_gateway, sms_twilio
    from monitor.state import LoginBreakerEvent

    event = LoginBreakerEvent(ts="2026-09-08T12:00:00+00:00", consecutive_failures=5,
                              cooldown_s=600, last_reason="timeout", track_status="DOWN",
                              target_name="Teachers FCU")

    sms_twilio.SmsTwilioChannel().send(event)          # must not raise
    sms_email_gateway.SmsEmailGatewayChannel().send(event)

    box = {}
    class FakeSMTP:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def login(self, *a): pass
        def sendmail(self, frm, to, msg): box["to"], box["msg"] = to, msg
    monkeypatch.setattr(eg.smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setattr(config, "GMAIL_USER", "m@example.com")
    monkeypatch.setattr(config, "GMAIL_APP_PASSWORD", "x")
    monkeypatch.setattr(config, "RECIPIENTS_EMAIL", "ops1@example.com,ops2@example.com")
    monkeypatch.setattr(config, "RECIPIENTS_CC", "")
    monkeypatch.setattr(config, "RECIPIENTS_BCC", "")
    monkeypatch.setattr(config, "ADMIN_EMAIL", "admin@example.com")

    eg.EmailGmailChannel().send(event)
    assert box["to"] == ["admin@example.com"]
    assert "|DOWN|" not in box["msg"], "must stay out of the machine-parsed Teams format"
    assert "NOT a new outage report" in box["msg"]
