"""SMS channel via Twilio. Implements the AlertChannel contract from
monitor/channels/base.py -- see CONTRIBUTING.md for the contract this was built against.

Reads its own TWILIO_*/SMS_TO_NUMBER env vars directly -- config.py never reads SMS_*
(Rule 18 "alert channels are plug-ins" / CONTRIBUTING.md's "files you may touch").

TWILIO_CONTENT_SID is optional: trial Twilio accounts reject freeform `body` sends and
require an approved Content Template instead. When set, the whole formatted message is
passed as the template's single variable ("1") via `content_variables`; the template
itself just needs to render `{{1}}`. Once the account is upgraded, unset it to go back
to plain freeform `body` sends.
"""
import json
import os

try:
    from twilio.rest import Client
    TWILIO_AVAILABLE = True
except ImportError:
    TWILIO_AVAILABLE = False
    Client = None

import config
from monitor.channels.base import AlertChannel, AlertEvent
from monitor.state import ConfigErrorEvent, DownEvent, RecoveryEvent

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_FROM_NUMBER = os.getenv("TWILIO_FROM_NUMBER")
SMS_TO_NUMBERS = [n.strip() for n in (os.getenv("SMS_TO_NUMBER") or "").split(",") if n.strip()]
TWILIO_CONTENT_SID = os.getenv("TWILIO_CONTENT_SID")


def _format_duration(duration_s: int) -> str:
    minutes, seconds = divmod(duration_s, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h{minutes}m"
    if minutes:
        return f"{minutes}m{seconds}s"
    return f"{seconds}s"


def down_message(event: DownEvent, target_name: str) -> str:
    reasons = ", ".join(event.fail_reasons)
    return (
        f"[MONITOR] {target_name} DOWN — {event.trigger_layer} failed "
        f"(confidence {event.confidence}: {reasons})"
    )


def recovery_message(event: RecoveryEvent, target_name: str) -> str:
    return f"[MONITOR] {target_name} RECOVERED after {_format_duration(event.duration_s)}"


def config_message(event: ConfigErrorEvent, target_name: str) -> str:
    return f"[MONITOR-CONFIG] {target_name} needs attention — {event.fail_reason}"


class SmsTwilioChannel(AlertChannel):
    name = "sms"

    def send(self, event: AlertEvent) -> None:
        if isinstance(event, DownEvent):
            body = down_message(event, config.TARGET_NAME)
        elif isinstance(event, RecoveryEvent):
            body = recovery_message(event, config.TARGET_NAME)
        elif isinstance(event, ConfigErrorEvent):
            body = config_message(event, config.TARGET_NAME)
        else:
            raise TypeError(f"unknown event type: {event!r}")

        self._send_sms(body)

    @staticmethod
    def _send_sms(body: str) -> None:
        if not TWILIO_AVAILABLE:
            raise RuntimeError("twilio library is not installed; run `pip install twilio`")
        if not (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_FROM_NUMBER and SMS_TO_NUMBERS):
            # Raise (not skip): dispatch()'s per-channel try/except is what logs this,
            # so email still fires and the failure is visible instead of a silent no-op.
            raise RuntimeError(
                "sms channel enabled but TWILIO_ACCOUNT_SID/TWILIO_AUTH_TOKEN/"
                "TWILIO_FROM_NUMBER/SMS_TO_NUMBER are not fully set in .env"
            )
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        errors = {}
        for to_number in SMS_TO_NUMBERS:
            try:
                if TWILIO_CONTENT_SID:
                    client.messages.create(
                        to=to_number, from_=TWILIO_FROM_NUMBER,
                        content_sid=TWILIO_CONTENT_SID, content_variables=json.dumps({"1": body}),
                    )
                else:
                    client.messages.create(to=to_number, from_=TWILIO_FROM_NUMBER, body=body)
            except Exception as exc:
                errors[to_number] = exc

        if errors:
            raise RuntimeError(f"sms channel failed for: {errors}")
