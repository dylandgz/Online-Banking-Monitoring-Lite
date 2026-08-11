"""SMS-via-email-gateway channel -- a free way to test SMS alerting without a paid
Twilio account. Carriers expose email-to-SMS gateways (e.g. <number>@vtext.com for
Verizon, @txt.att.net for AT&T, @tmomail.net for T-Mobile, @vmobl.com for Visible);
sending a plain email to that address is delivered to the phone as a text message.
Reuses the existing Gmail SMTP credentials (GMAIL_USER/GMAIL_APP_PASSWORD) and the
terse message formatters from sms_twilio.py.

Testing convenience only, not a production replacement for sms_twilio.py: gateway
delivery isn't guaranteed (carriers can rate-limit, delay, or silently drop it), there's
no delivery confirmation, and it depends on the recipient's carrier gateway staying up.
"""
import os
import smtplib
from email.mime.text import MIMEText

import config
from monitor.channels.base import AlertChannel, AlertEvent
from monitor.channels.sms_twilio import config_message, down_message, recovery_message
from monitor.state import ConfigErrorEvent, DownEvent, RecoveryEvent

SMS_GATEWAY_ADDRESSES = [a.strip() for a in (os.getenv("SMS_GATEWAY_ADDRESS") or "").split(",") if a.strip()]


class SmsEmailGatewayChannel(AlertChannel):
    name = "sms_gateway"

    def send(self, event: AlertEvent) -> None:
        if isinstance(event, DownEvent):
            body = down_message(event, config.TARGET_NAME)
        elif isinstance(event, RecoveryEvent):
            body = recovery_message(event, config.TARGET_NAME)
        elif isinstance(event, ConfigErrorEvent):
            body = config_message(event, config.TARGET_NAME)
        else:
            raise TypeError(f"unknown event type: {event!r}")

        self._send(body)

    @staticmethod
    def _send(body: str) -> None:
        if not (config.GMAIL_USER and config.GMAIL_APP_PASSWORD and SMS_GATEWAY_ADDRESSES):
            raise RuntimeError(
                "sms_gateway channel enabled but GMAIL_USER/GMAIL_APP_PASSWORD/"
                "SMS_GATEWAY_ADDRESS are not fully set in .env"
            )
        # One email per recipient, not one email with multiple "To" addresses: several
        # carrier gateways spam-filter (silently drop, no exception) a message that looks
        # like bulk mail to multiple recipients, even though sendmail() only raises when
        # every recipient is refused.
        errors = {}
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(config.GMAIL_USER, config.GMAIL_APP_PASSWORD)
            for address in SMS_GATEWAY_ADDRESSES:
                msg = MIMEText(body)
                msg["Subject"] = ""  # most carrier gateways prepend it to the text, wasting characters
                msg["From"] = config.GMAIL_USER
                msg["To"] = address
                try:
                    server.sendmail(config.GMAIL_USER, [address], msg.as_string())
                except smtplib.SMTPException as exc:
                    errors[address] = exc

        if errors:
            raise RuntimeError(f"sms_gateway failed for: {errors}")
