"""Tests for the Twilio SMS channel. Mocks twilio.rest.Client -- never sends a real SMS."""
import json

import pytest

import monitor.channels.sms_twilio as sms_twilio
from monitor.state import ConfigErrorEvent, DownEvent, RecoveryEvent


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(sms_twilio, "TWILIO_AVAILABLE", True)
    monkeypatch.setattr(sms_twilio, "TWILIO_ACCOUNT_SID", "AC_test_sid")
    monkeypatch.setattr(sms_twilio, "TWILIO_AUTH_TOKEN", "test_token")
    monkeypatch.setattr(sms_twilio, "TWILIO_FROM_NUMBER", "+15551234567")
    monkeypatch.setattr(sms_twilio, "SMS_TO_NUMBERS", ["+15557654321"])
    monkeypatch.setattr(sms_twilio, "TWILIO_CONTENT_SID", None)
    monkeypatch.setattr(sms_twilio.config, "TARGET_NAME", "Test Bank")


class FakeMessages:
    def __init__(self):
        self.calls = []

    def create(self, to, from_, **kwargs):
        self.calls.append({"to": to, "from_": from_, **kwargs})


class FakeClient:
    last_instance = None

    def __init__(self, account_sid, auth_token):
        self.account_sid = account_sid
        self.auth_token = auth_token
        self.messages = FakeMessages()
        FakeClient.last_instance = self


@pytest.fixture
def fake_client(monkeypatch):
    monkeypatch.setattr(sms_twilio, "Client", FakeClient)
    return FakeClient


def test_send_down_event(fake_client):
    event = DownEvent(
        since_ts="2026-01-01T00:00:00+00:00",
        confidence=4,
        fail_reasons=("conn_refused", "timeout"),
        trigger_layer="pulse",
    )
    sms_twilio.SmsTwilioChannel().send(event)

    call = fake_client.last_instance.messages.calls[0]
    assert call["to"] == "+15557654321"
    assert call["from_"] == "+15551234567"
    assert "Test Bank" in call["body"]
    assert "DOWN" in call["body"]
    assert "pulse" in call["body"]


def test_send_recovery_event(fake_client):
    event = RecoveryEvent(
        since_ts="2026-01-01T00:00:00+00:00",
        ended_at="2026-01-01T00:05:00+00:00",
        duration_s=300,
        confidence=4,
        fail_reasons=("conn_refused",),
    )
    sms_twilio.SmsTwilioChannel().send(event)

    call = fake_client.last_instance.messages.calls[0]
    assert "RECOVERED" in call["body"]
    assert "5m0s" in call["body"]


def test_provider_failure_propagates(monkeypatch, fake_client):
    def boom(self, to, from_, body):
        raise RuntimeError("twilio rejected the request")

    monkeypatch.setattr(FakeMessages, "create", boom)

    event = RecoveryEvent(
        since_ts="2026-01-01T00:00:00+00:00",
        ended_at="2026-01-01T00:05:00+00:00",
        duration_s=300,
        confidence=4,
        fail_reasons=("conn_refused",),
    )
    with pytest.raises(RuntimeError):
        sms_twilio.SmsTwilioChannel().send(event)


def test_missing_config_raises(monkeypatch, fake_client):
    monkeypatch.setattr(sms_twilio, "TWILIO_ACCOUNT_SID", None)

    event = RecoveryEvent(
        since_ts="2026-01-01T00:00:00+00:00",
        ended_at="2026-01-01T00:05:00+00:00",
        duration_s=300,
        confidence=4,
        fail_reasons=("conn_refused",),
    )
    with pytest.raises(RuntimeError):
        sms_twilio.SmsTwilioChannel().send(event)


def test_send_to_multiple_recipients(fake_client):
    sms_twilio.SMS_TO_NUMBERS.append("+15559998888")
    try:
        event = RecoveryEvent(
            since_ts="2026-01-01T00:00:00+00:00",
            ended_at="2026-01-01T00:05:00+00:00",
            duration_s=300,
            confidence=4,
            fail_reasons=("conn_refused",),
        )
        sms_twilio.SmsTwilioChannel().send(event)

        calls = fake_client.last_instance.messages.calls
        assert [c["to"] for c in calls] == ["+15557654321", "+15559998888"]
    finally:
        sms_twilio.SMS_TO_NUMBERS.pop()


def test_one_recipient_failure_does_not_block_the_other(monkeypatch, fake_client):
    monkeypatch.setattr(sms_twilio, "SMS_TO_NUMBERS", ["+15551110000", "+15559998888"])

    def flaky(self, to, from_, body):
        if to == "+15551110000":
            raise RuntimeError("twilio rejected the request")
        self.calls.append({"to": to, "from_": from_, "body": body})

    monkeypatch.setattr(FakeMessages, "create", flaky)

    event = RecoveryEvent(
        since_ts="2026-01-01T00:00:00+00:00",
        ended_at="2026-01-01T00:05:00+00:00",
        duration_s=300,
        confidence=4,
        fail_reasons=("conn_refused",),
    )
    with pytest.raises(RuntimeError):
        sms_twilio.SmsTwilioChannel().send(event)

    assert fake_client.last_instance.messages.calls[0]["to"] == "+15559998888"


def test_send_uses_content_template_when_configured(monkeypatch, fake_client):
    monkeypatch.setattr(sms_twilio, "TWILIO_CONTENT_SID", "HX_test_template")

    event = RecoveryEvent(
        since_ts="2026-01-01T00:00:00+00:00",
        ended_at="2026-01-01T00:05:00+00:00",
        duration_s=300,
        confidence=4,
        fail_reasons=("conn_refused",),
    )
    sms_twilio.SmsTwilioChannel().send(event)

    call = fake_client.last_instance.messages.calls[0]
    assert call["content_sid"] == "HX_test_template"
    assert "body" not in call
    variables = json.loads(call["content_variables"])
    assert "RECOVERED" in variables["1"]
