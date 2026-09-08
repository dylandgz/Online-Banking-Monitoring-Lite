"""B61 + B59: two ways the alert channel could hurt more than help.

  B61  smtplib.SMTP_SSL had no timeout, so the socket inherited the global default of None.
       send() runs via asyncio.to_thread from dispatch(), awaited inside run_cycle, inside
       guarded_cycle's `async with lock` -- so a stalled send holds the cycle lock for as long
       as the socket hangs while the scheduler prints "skip cycle" and the dashboard keeps
       serving the last verdict. That is B42 exactly: 13h 34m on 2026-08-29, reporting UP
       throughout. A timeout bounds it; it does not fix B42, which needs dispatch off the lock.

  B59  A cleared CONFIG_ERROR left through the same path as an outage recovery, so every
       recipient was told "...has recovered and is now loading normally. The outage lasted
       5m 0s." They were never told of an outage (CONFIG_ERROR is admin-only), and there was
       no outage -- Rule 7, and uptime_pct excludes it from both sides. The duration measured
       how long a human took to notice.
"""
import smtplib

import pytest

import config
from monitor.channels import email_gmail as eg
from monitor.state import ConfigErrorEvent, DownEvent, RecoveryEvent

TS0 = "2026-09-08T12:00:00+00:00"
TS1 = "2026-09-08T12:05:00+00:00"


@pytest.fixture
def sent(monkeypatch):
    """Captures the SMTP conversation instead of holding one."""
    box = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            box["timeout"] = timeout
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def login(self, *a): pass
        def sendmail(self, frm, to, msg):
            box["to"], box["msg"] = to, msg

    monkeypatch.setattr(eg.smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setattr(config, "GMAIL_USER", "monitor@example.com")
    monkeypatch.setattr(config, "GMAIL_APP_PASSWORD", "x")
    monkeypatch.setattr(config, "RECIPIENTS_EMAIL", "ops1@example.com,ops2@example.com")
    monkeypatch.setattr(config, "RECIPIENTS_CC", "")
    monkeypatch.setattr(config, "RECIPIENTS_BCC", "")
    monkeypatch.setattr(config, "ADMIN_EMAIL", "admin@example.com")
    return box


def _down():
    return DownEvent(since_ts=TS0, confidence=4, fail_reasons=("element_missing",) * 4,
                     trigger_layer="authed", page_url="https://x/app/home",
                     track="auth", target_name="Teachers FCU")


def _recovery(from_status):
    return RecoveryEvent(since_ts=TS0, ended_at=TS1, duration_s=300, confidence=4,
                         fail_reasons=("bot_challenge",), trigger_layer="authed",
                         page_url="https://x/app/home", track="auth",
                         target_name="Teachers FCU", from_status=from_status)


# --- B61 ------------------------------------------------------------------------------

def test_the_smtp_socket_has_a_timeout(sent):
    """Without it, a stalled send holds the cycle lock indefinitely -- the monitor stops
    probing and keeps reporting the last verdict."""
    eg.EmailGmailChannel().send(_down())
    assert sent["timeout"] is not None, "an unbounded SMTP socket can hang the whole monitor"
    assert 0 < sent["timeout"] <= 60, "must expire well inside a cycle, not after several"


def test_a_hanging_send_raises_instead_of_blocking_forever(monkeypatch):
    """What the timeout buys: dispatch() catches the error per channel and the cycle carries
    on, rather than the lock being held until someone notices."""
    class HangingSMTP:
        def __init__(self, *a, **k): raise smtplib.SMTPServerDisconnected("timed out")
    monkeypatch.setattr(eg.smtplib, "SMTP_SSL", HangingSMTP)
    monkeypatch.setattr(config, "GMAIL_USER", "m@example.com")
    monkeypatch.setattr(config, "GMAIL_APP_PASSWORD", "x")
    monkeypatch.setattr(config, "RECIPIENTS_EMAIL", "ops@example.com")

    with pytest.raises(smtplib.SMTPServerDisconnected):
        eg.EmailGmailChannel().send(_down())


# --- B59 ------------------------------------------------------------------------------

def test_a_real_outage_recovery_still_goes_to_every_recipient(sent):
    eg.EmailGmailChannel().send(_recovery(from_status="DOWN"))
    assert sent["to"] == ["ops1@example.com", "ops2@example.com"]
    assert "|RECOVERED|" in sent["msg"], "the machine-parsed format the Teams flow reads"


def test_a_cleared_config_error_does_not_reach_the_outage_recipients(sent):
    """They were never told of an outage, so they must not be told one ended."""
    eg.EmailGmailChannel().send(_recovery(from_status="CONFIG_ERROR"))
    assert sent["to"] == ["admin@example.com"]
    assert "ops1@example.com" not in sent["to"]


def test_a_cleared_config_error_is_not_dressed_as_an_outage(sent):
    """Two lies to avoid: that there was an outage, and that its duration means downtime."""
    eg.EmailGmailChannel().send(_recovery(from_status="CONFIG_ERROR"))
    msg = sent["msg"]
    assert "|RECOVERED|" not in msg, "must not enter the machine-parsed format at all"
    assert "MONITOR-CONFIG" in msg
    assert "NOT a platform outage" in msg


def test_a_cleared_config_error_is_silent_without_an_admin_address(sent, monkeypatch):
    """Same routing as the CONFIG_ERROR alert it closes -- it must not fall back to the
    outage recipients. (That an unset ADMIN_EMAIL silences it entirely is B58, still open.)"""
    monkeypatch.setattr(config, "ADMIN_EMAIL", "")
    eg.EmailGmailChannel().send(_recovery(from_status="CONFIG_ERROR"))
    assert "to" not in sent, "nothing should have been sent"


def test_an_unset_from_status_is_treated_as_an_outage(sent):
    """Fail toward telling people. A RecoveryEvent built without the field -- an older row,
    or a caller that predates it -- is more safely over-reported than silently dropped."""
    eg.EmailGmailChannel().send(_recovery(from_status=None))
    assert sent["to"] == ["ops1@example.com", "ops2@example.com"]
