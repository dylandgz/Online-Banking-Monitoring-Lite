"""Tests for the email-to-SMS gateway channel. Mocks smtplib.SMTP_SSL -- never sends a
real email/SMS."""
import email
import smtplib

import pytest

import monitor.channels.sms_email_gateway as sms_gateway
from monitor.state import ConfigErrorEvent, DownEvent, RecoveryEvent


def _decoded_body(raw_msg: str) -> str:
    return email.message_from_string(raw_msg).get_payload(decode=True).decode("utf-8")


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(sms_gateway.config, "GMAIL_USER", "test@gmail.com")
    monkeypatch.setattr(sms_gateway.config, "GMAIL_APP_PASSWORD", "app_password")
    monkeypatch.setattr(sms_gateway, "SMS_GATEWAY_ADDRESSES", ["5551234567@vtext.com"])
    monkeypatch.setattr(sms_gateway.config, "TARGET_NAME", "Test Bank")


class FakeSMTP:
    last_instance = None

    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.sent = []
        FakeSMTP.last_instance = self

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def login(self, user, password):
        self.login_args = (user, password)

    def sendmail(self, from_addr, to_addrs, msg):
        self.sent.append({"from_addr": from_addr, "to_addrs": to_addrs, "msg": msg})


@pytest.fixture
def fake_smtp(monkeypatch):
    monkeypatch.setattr(sms_gateway.smtplib, "SMTP_SSL", FakeSMTP)
    return FakeSMTP


def test_send_down_event(fake_smtp):
    event = DownEvent(
        since_ts="2026-01-01T00:00:00+00:00",
        confidence=4,
        fail_reasons=("conn_refused", "timeout"),
        trigger_layer="pulse",
    )
    sms_gateway.SmsEmailGatewayChannel().send(event)

    call = fake_smtp.last_instance.sent[0]
    assert call["to_addrs"] == ["5551234567@vtext.com"]
    body = _decoded_body(call["msg"])
    assert "Test Bank" in body
    assert "DOWN" in body


def test_send_recovery_event(fake_smtp):
    event = RecoveryEvent(
        since_ts="2026-01-01T00:00:00+00:00",
        ended_at="2026-01-01T00:05:00+00:00",
        duration_s=300,
        confidence=4,
        fail_reasons=("conn_refused",),
    )
    sms_gateway.SmsEmailGatewayChannel().send(event)

    call = fake_smtp.last_instance.sent[0]
    assert "RECOVERED" in _decoded_body(call["msg"])


def test_provider_failure_propagates(monkeypatch, fake_smtp):
    def boom(self, from_addr, to_addrs, msg):
        raise smtplib.SMTPException("gateway rejected the message")

    monkeypatch.setattr(FakeSMTP, "sendmail", boom)

    event = RecoveryEvent(
        since_ts="2026-01-01T00:00:00+00:00",
        ended_at="2026-01-01T00:05:00+00:00",
        duration_s=300,
        confidence=4,
        fail_reasons=("conn_refused",),
    )
    with pytest.raises(Exception):
        sms_gateway.SmsEmailGatewayChannel().send(event)


def test_missing_config_raises(monkeypatch, fake_smtp):
    monkeypatch.setattr(sms_gateway, "SMS_GATEWAY_ADDRESSES", [])

    event = RecoveryEvent(
        since_ts="2026-01-01T00:00:00+00:00",
        ended_at="2026-01-01T00:05:00+00:00",
        duration_s=300,
        confidence=4,
        fail_reasons=("conn_refused",),
    )
    with pytest.raises(RuntimeError):
        sms_gateway.SmsEmailGatewayChannel().send(event)


def test_send_to_multiple_recipients(monkeypatch, fake_smtp):
    monkeypatch.setattr(
        sms_gateway, "SMS_GATEWAY_ADDRESSES", ["5551234567@vtext.com", "5164390038@tmomail.net"]
    )

    event = RecoveryEvent(
        since_ts="2026-01-01T00:00:00+00:00",
        ended_at="2026-01-01T00:05:00+00:00",
        duration_s=300,
        confidence=4,
        fail_reasons=("conn_refused",),
    )
    sms_gateway.SmsEmailGatewayChannel().send(event)

    # one separate email per recipient -- not one email addressed to both -- so a
    # carrier's spam filter dropping one recipient can't silently take out the other.
    sent = fake_smtp.last_instance.sent
    assert len(sent) == 2
    assert sent[0]["to_addrs"] == ["5551234567@vtext.com"]
    assert sent[1]["to_addrs"] == ["5164390038@tmomail.net"]


def test_one_recipient_failure_does_not_block_the_other(monkeypatch, fake_smtp):
    monkeypatch.setattr(
        sms_gateway, "SMS_GATEWAY_ADDRESSES", ["bad@tmomail.net", "5164390038@tmomail.net"]
    )

    original_sendmail = FakeSMTP.sendmail

    def flaky(self, from_addr, to_addrs, msg):
        if to_addrs == ["bad@tmomail.net"]:
            raise smtplib.SMTPException("rejected")
        return original_sendmail(self, from_addr, to_addrs, msg)

    monkeypatch.setattr(FakeSMTP, "sendmail", flaky)

    event = RecoveryEvent(
        since_ts="2026-01-01T00:00:00+00:00",
        ended_at="2026-01-01T00:05:00+00:00",
        duration_s=300,
        confidence=4,
        fail_reasons=("conn_refused",),
    )
    with pytest.raises(RuntimeError):
        sms_gateway.SmsEmailGatewayChannel().send(event)

    # the good recipient still got sent despite the other one failing
    assert fake_smtp.last_instance.sent[0]["to_addrs"] == ["5164390038@tmomail.net"]
