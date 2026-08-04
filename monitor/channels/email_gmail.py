"""Email channel: one send on DOWN, one on RECOVERY. No re-emailing mid-incident
(state.py enforces that by only emitting events on transitions)."""
import smtplib
from email.mime.text import MIMEText

import config
from monitor.channels.base import AlertChannel, AlertEvent
from monitor.state import DownEvent, RecoveryEvent

# fail_reason values where the pulse (plain HTTP) already failed -- the site itself is unreachable.
PULSE_FAILURE_REASONS = {"timeout", "dns", "conn_refused"}


def _pulse_hint(fail_reason: str) -> str:
    if fail_reason in PULSE_FAILURE_REASONS or fail_reason.startswith("bad_status:"):
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


def down_message(since_ts: str, consecutive_fails: int, fail_reason: str) -> str:
    dashboard_url = f"http://localhost:{config.PORT}"
    return (
        f"[MONITOR] {config.TARGET_NAME} DOWN since {since_ts} "
        f"({consecutive_fails} fails: {fail_reason}) — {_pulse_hint(fail_reason)}. "
        f"Dashboard: {dashboard_url}"
    )


def recovery_message(duration_s: int) -> str:
    return f"[MONITOR] {config.TARGET_NAME} RECOVERED after {_format_duration(duration_s)}."


class EmailGmailChannel(AlertChannel):
    name = "email"

    def send(self, event: AlertEvent) -> None:
        if isinstance(event, DownEvent):
            body = down_message(event.since_ts, event.consecutive_fails, event.fail_reason)
        elif isinstance(event, RecoveryEvent):
            body = recovery_message(event.duration_s)
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
