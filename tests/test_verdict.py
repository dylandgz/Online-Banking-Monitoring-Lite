"""Locks CLAUDE.md v3.8's verdict rule and Rule 10's operator wording.

These assertions exist because the wording had already drifted once: the dashboard used
Rule 10's phrasing while the alert email -- the surface that actually pages a human -- still
used pre-v3.8 text. Rule 10's promise is that an operator never needs a browser to know
which wall failed, so the exact strings are part of the contract, not cosmetic.
"""
import pytest

from monitor.state import ConfigErrorEvent, DownEvent, RecoveryEvent
from monitor.verdict import AUTHED_WORDING, PRECURSOR_WORDING, layer_wording, severity, unified_verdict


# --- Rule 10 "every DOWN names its layer" wording ---

def test_precursor_layers_share_the_login_screen_wording():
    for layer in ("pulse", "render"):
        assert layer_wording(layer) == "login screen unreachable / not rendering", layer


def test_authed_layer_wording():
    assert layer_wording("authed") == "online banking behind login not rendering"


def test_unknown_or_absent_layer_has_no_wording():
    # None, not a guess: callers supply their own fallback so an unmapped layer degrades
    # the description instead of suppressing the alert.
    assert layer_wording(None) is None
    assert layer_wording("data") is None


# --- worst_of(main, auth) ---

@pytest.mark.parametrize("main_status,auth_status,expected", [
    ("UP", "UP", "UP"),
    ("UP", "DOWN", "DOWN"),            # authed-layer outage drives the platform verdict
    ("DOWN", "UP", "DOWN"),            # precursor outage does too
    ("DOWN", "CONFIG_ERROR", "DOWN"),  # DOWN outranks CONFIG_ERROR
    ("CONFIG_ERROR", "UP", "CONFIG_ERROR"),
    ("UP", "DEGRADED", "DEGRADED"),
    ("DOWN", "DOWN", "DOWN"),
])
def test_unified_verdict_is_worst_of(main_status, auth_status, expected):
    assert unified_verdict(main_status, auth_status) == expected


def test_auth_track_absent_leaves_main_deciding():
    """auth_status=None means the auth track didn't participate (unconfigured, or its own
    CONFIG_ERROR per Rule 4 "never retry a credential rejection") -- the base pulse/render monitor still works standalone."""
    assert unified_verdict("UP", None) == "UP"
    assert unified_verdict("DOWN", None) == "DOWN"


def test_severity_ladder_puts_down_on_top_and_never_raises():
    assert severity("DOWN") > severity("CONFIG_ERROR") > severity("DEGRADED") > severity("UP")
    # An unrecognized status must not be able to crash a cycle or blank the dashboard.
    assert severity("SOMETHING_NEW") == 0


# --- v3.8's locked email copy ---
# email_gmail imports config at module scope, so these run against whatever .env is
# present; only the wording fragments are asserted, never the target name.

def test_down_email_names_the_layer_in_business_terms():
    from monitor.channels.email_gmail import down_message

    # [B45] Rule 10 is satisfied by the SERVICE/DESCRIPTION lines, in customer terms --
    # the email never prints a layer name, a fail_reason, or a probe count.
    precursor = down_message(DownEvent(
        since_ts="2026-08-11T14:00:00+00:00", confidence=4,
        fail_reasons=("dns", "dns"), trigger_layer="pulse",
    ))
    assert "Online Banking Website" in precursor
    assert "website is not responding" in precursor
    assert "pulse" not in precursor          # no layer name leaks to a business reader
    assert "dns" not in precursor            # nor a fail_reason -- the screenshot carries detail
    assert "Checked 2 times" not in precursor  # probe count removed

    authed = down_message(DownEvent(
        since_ts="2026-08-11T14:00:00+00:00", confidence=4,
        fail_reasons=("nav_error",) * 4, trigger_layer="authed",
    ))
    assert "Online Banking Account Access" in authed
    assert "not loading after sign-in" in authed


def test_down_email_subject_is_pipe_delimited_and_eastern():
    """[B45] The Power Automate contract: five subject fields, and a time in Eastern rather
    than the raw UTC slice the pre-B45 subject printed."""
    from monitor.channels.email_gmail import _build_down_subject

    subject = _build_down_subject(DownEvent(
        since_ts="2026-09-01T18:32:15+00:00", confidence=4,
        fail_reasons=("dns",) * 4, trigger_layer="pulse", target_name="Example FCU",
    ))
    tag, status, service, stamp = subject.split("|")
    assert tag == "[OLB MONITOR LITE]"
    assert status == "DOWN"
    assert service == "Example FCU Online Banking"
    assert stamp == "2026-09-01 14:32:15 EDT"   # 18:32 UTC is 14:32 EDT


def test_down_and_recovery_bodies_carry_the_same_fixed_keys():
    """[B45] One Power Automate parser reads both, so both emit every key in one order --
    N/A where the key does not apply rather than the key being omitted."""
    from monitor.channels.email_gmail import _build_down_body, _build_recovery_body

    keys = ["MONITOR:", "STATUS:", "SERVICE:", "DESCRIPTION:",
            "START_TIME:", "END_TIME:", "DURATION:", "URL:", "SOURCE:"]

    down = _build_down_body(DownEvent(
        since_ts="2026-09-01T18:32:15+00:00", confidence=4,
        fail_reasons=("dns",) * 4, trigger_layer="pulse",
    ))
    recovery = _build_recovery_body(RecoveryEvent(
        since_ts="2026-09-01T18:32:15+00:00", ended_at="2026-09-01T18:47:45+00:00",
        duration_s=930, confidence=4, fail_reasons=("dns",), trigger_layer="pulse",
    ))
    for body in (down, recovery):
        positions = [body.index(k) for k in keys]      # raises if a key is missing
        assert positions == sorted(positions)          # and the order is the contract

    assert "END_TIME: N/A" in down and "DURATION: N/A" in down   # incident still open
    assert "END_TIME: 2026-09-01 14:47:45 EDT" in recovery
    assert "DURATION: 15m 30s" in recovery


def test_down_email_still_sends_for_an_unmapped_layer():
    from monitor.channels.email_gmail import down_message

    # [B45] Unmapped layers still produce an email (not suppressed), with a fallback description.
    body = down_message(DownEvent(
        since_ts="2026-08-11T14:00:00+00:00", confidence=4,
        fail_reasons=("timeout",), trigger_layer="something_new",
    ))
    assert "Online Banking is not available." in body   # fallback description
    assert "|DOWN|" in body                             # subject still machine-readable


def test_recovery_email_copy():
    from monitor.channels.email_gmail import recovery_message

    # trigger_layer is Optional on RecoveryEvent; the fallback wording must still render.
    body = recovery_message(RecoveryEvent(
        since_ts="2026-08-11T14:00:00+00:00", ended_at="2026-08-11T14:07:30+00:00",
        duration_s=450, confidence=4, fail_reasons=("dns",),
    ))
    assert "|RECOVERED|" in body
    assert "DURATION: 7m 30s" in body
    assert "Online Banking is available again." in body


def test_config_error_goes_to_admin_email(monkeypatch):
    """CONFIG_ERROR emails go to ADMIN_EMAIL only, not to regular RECIPIENTS_EMAIL."""
    import monitor.channels.email_gmail as email_module
    from monitor.channels.email_gmail import EmailGmailChannel

    monkeypatch.setattr(email_module.config, "ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setattr(email_module.config, "GMAIL_USER", "sender@gmail.com")
    monkeypatch.setattr(email_module.config, "GMAIL_APP_PASSWORD", "app_pass")

    # Track what _send_email was called with
    calls = []
    original_send = email_module.EmailGmailChannel._send_email

    @staticmethod
    def mock_send_email(subject: str, body: str, screenshot_path=None, recipients=None) -> None:
        calls.append({"subject": subject, "recipients": recipients})

    monkeypatch.setattr(EmailGmailChannel, "_send_email", mock_send_email)

    event = ConfigErrorEvent(ts="2026-01-01T00:00:00+00:00", fail_reason="auth_rejected")
    EmailGmailChannel().send(event)

    assert len(calls) == 1
    assert calls[0]["recipients"] == ["admin@example.com"]
    assert "MONITOR-CONFIG" in calls[0]["subject"]


def test_config_error_skips_if_admin_email_blank(monkeypatch):
    """If ADMIN_EMAIL is blank, CONFIG_ERROR notifications are disabled."""
    import monitor.channels.email_gmail as email_module
    from monitor.channels.email_gmail import EmailGmailChannel

    monkeypatch.setattr(email_module.config, "ADMIN_EMAIL", "")
    monkeypatch.setattr(email_module.config, "GMAIL_USER", "sender@gmail.com")
    monkeypatch.setattr(email_module.config, "GMAIL_APP_PASSWORD", "app_pass")

    # Track calls to _send_email
    calls = []
    original_send = email_module.EmailGmailChannel._send_email

    @staticmethod
    def mock_send_email(subject: str, body: str, screenshot_path=None, recipients=None) -> None:
        calls.append({"subject": subject, "recipients": recipients})

    monkeypatch.setattr(EmailGmailChannel, "_send_email", mock_send_email)

    event = ConfigErrorEvent(ts="2026-01-01T00:00:00+00:00", fail_reason="auth_rejected")
    EmailGmailChannel().send(event)

    # Should not send anything
    assert len(calls) == 0


def test_down_email_still_goes_to_recipients_email(monkeypatch):
    """DOWN events still go to RECIPIENTS_EMAIL, not ADMIN_EMAIL."""
    import monitor.channels.email_gmail as email_module
    from monitor.channels.email_gmail import EmailGmailChannel

    monkeypatch.setattr(email_module.config, "RECIPIENTS_EMAIL", "ops@example.com")
    monkeypatch.setattr(email_module.config, "RECIPIENTS_CC", "")
    monkeypatch.setattr(email_module.config, "RECIPIENTS_BCC", "")
    monkeypatch.setattr(email_module.config, "ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setattr(email_module.config, "GMAIL_USER", "sender@gmail.com")
    monkeypatch.setattr(email_module.config, "GMAIL_APP_PASSWORD", "app_pass")

    # Track calls
    calls = []

    @staticmethod
    def mock_send_email(subject: str, body: str, screenshot_path=None, recipients=None) -> None:
        calls.append({"subject": subject, "recipients": recipients})

    monkeypatch.setattr(EmailGmailChannel, "_send_email", mock_send_email)

    event = DownEvent(
        since_ts="2026-01-01T00:00:00+00:00", confidence=4,
        fail_reasons=("timeout",), trigger_layer="pulse",
    )
    EmailGmailChannel().send(event)

    assert len(calls) == 1
    # Should go to RECIPIENTS_EMAIL, not ADMIN_EMAIL
    assert calls[0]["recipients"] == ["ops@example.com"]
