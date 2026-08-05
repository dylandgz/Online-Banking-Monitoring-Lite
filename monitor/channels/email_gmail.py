"""Email channel: one send on DOWN, one on RECOVERY. No re-emailing mid-incident
(state.py enforces that by only emitting events on transitions)."""
import smtplib
from email.mime.text import MIMEText

import config
from monitor.channels.base import AlertChannel, AlertEvent
from monitor.state import ConfigErrorEvent, DownEvent, RecoveryEvent
from monitor.timeutil import to_eastern

# layers whose evidence implies the pulse (plain HTTP) itself failed -- the site is unreachable.
PULSE_LAYERS = {"pulse"}


def _layer_hint(trigger_layer: str) -> str:
    if trigger_layer in PULSE_LAYERS:
        return "site unreachable"
    return "page loads, content missing — possible site change, verify before escalating"


def _format_duration(duration_s: int) -> str:
    minutes, seconds = divmod(duration_s, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def down_message(event: DownEvent) -> str:
    dashboard_url = f"http://localhost:{config.PORT}"
    reasons = ", ".join(event.fail_reasons)
    return (
        f"[MONITOR] {config.TARGET_NAME} DOWN since {to_eastern(event.since_ts)} — "
        f"{event.trigger_layer} failed (confidence {event.confidence}: {reasons}). "
        f"{_layer_hint(event.trigger_layer)}. Dashboard: {dashboard_url}"
    )


def recovery_message(event: RecoveryEvent) -> str:
    return f"[MONITOR] {config.TARGET_NAME} RECOVERED after {_format_duration(event.duration_s)}."


def config_message(event: ConfigErrorEvent) -> str:
    return f"[MONITOR-CONFIG] {config.TARGET_NAME} needs attention — {event.fail_reason}. Checks paused."


class EmailGmailChannel(AlertChannel):
    name = "email"

    def send(self, event: AlertEvent) -> None:
        if isinstance(event, DownEvent):
            body = down_message(event)
        elif isinstance(event, RecoveryEvent):
            body = recovery_message(event)
        elif isinstance(event, ConfigErrorEvent):
            body = config_message(event)
        else:
            raise TypeError(f"unknown event type: {event!r}")

        self._send_email(subject=body, body=body)

    @staticmethod
    def _send_email(subject: str, body: str) -> None:
        if not (config.GMAIL_USER and config.GMAIL_APP_PASSWORD and config.RECIPIENTS_EMAIL):
            print(f"[email] not configured, skipping send. subject={subject!r}")
            return

        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = config.GMAIL_USER
        msg["To"] = config.RECIPIENTS_EMAIL

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(config.GMAIL_USER, config.GMAIL_APP_PASSWORD)
            server.sendmail(config.GMAIL_USER, [config.RECIPIENTS_EMAIL], msg.as_string())
