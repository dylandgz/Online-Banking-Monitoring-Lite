"""Contribution template for the SMS channel — see CONTRIBUTING.md.

This stub deliberately raises NotImplementedError so it's safe to leave wired into the
registry: `ALERT_CHANNELS=email` (the default) never touches it, and if a maintainer opts
into `sms` before it's built, dispatch()'s per-channel try/except logs the failure and
email still goes out — the crash-safety of the fan-out is exercised by this file, not
just described by it.

To implement: replace the body of send() with a real SMS provider call (e.g. Twilio).
Keep messages short — DownEvent/RecoveryEvent carry the same fields email uses; write a
terse formatter rather than reusing email_gmail's long-form message.
"""
from monitor.channels.base import AlertChannel, AlertEvent


class SmsStubChannel(AlertChannel):
    name = "sms"

    def send(self, event: AlertEvent) -> None:
        raise NotImplementedError("SMS channel not implemented yet — see CONTRIBUTING.md")
