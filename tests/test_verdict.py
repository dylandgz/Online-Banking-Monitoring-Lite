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

def test_down_email_uses_rule_4_wording_not_the_raw_layer_name():
    from monitor.channels.email_gmail import down_message

    precursor = down_message(DownEvent(
        since_ts="2026-08-11T14:00:00+00:00", confidence=4,
        fail_reasons=("dns", "dns"), trigger_layer="pulse",
    ))
    assert PRECURSOR_WORDING in precursor
    assert "confidence 4: dns, dns" in precursor
    assert "pulse failed" not in precursor  # the pre-v3.8 phrasing

    authed = down_message(DownEvent(
        since_ts="2026-08-11T14:00:00+00:00", confidence=4,
        fail_reasons=("nav_error",) * 4, trigger_layer="authed",
    ))
    assert AUTHED_WORDING in authed


def test_down_email_still_sends_for_an_unmapped_layer():
    from monitor.channels.email_gmail import down_message

    body = down_message(DownEvent(
        since_ts="2026-08-11T14:00:00+00:00", confidence=4,
        fail_reasons=("timeout",), trigger_layer="something_new",
    ))
    assert "something_new layer failed" in body
    assert "DOWN since" in body


def test_config_email_says_sign_in_checks_paused():
    """Rule 4 "never retry a credential rejection" halts the auth track only -- the pulse/render precursor keeps checking, so
    the old "Checks paused" overstated the outage."""
    from monitor.channels.email_gmail import config_message

    body = config_message(ConfigErrorEvent(ts="2026-08-11T14:00:00+00:00", fail_reason="auth_rejected"))
    assert "Sign-in checks paused." in body
    assert "auth_rejected" in body


def test_recovery_email_copy():
    from monitor.channels.email_gmail import recovery_message

    body = recovery_message(RecoveryEvent(
        since_ts="2026-08-11T14:00:00+00:00", ended_at="2026-08-11T14:07:30+00:00",
        duration_s=450, confidence=4, fail_reasons=("dns",),
    ))
    assert "RECOVERED after 7m 30s." in body
