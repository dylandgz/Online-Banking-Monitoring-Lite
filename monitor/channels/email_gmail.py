"""Email channel: one send on DOWN, one on RECOVERY. No re-emailing mid-incident
(state.py enforces that by only emitting events on transitions)."""
import smtplib
from email.mime.text import MIMEText

import config
from monitor.channels.base import AlertChannel, AlertEvent
from monitor.state import ConfigErrorEvent, DownEvent, RecoveryEvent
from monitor.timeutil import to_eastern
from monitor.verdict import layer_wording


def _format_duration(duration_s: int) -> str:
    minutes, seconds = divmod(duration_s, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def down_message(event: DownEvent) -> str:
    """CLAUDE.md's v3.8 locked DOWN copy, verbatim:

        [MONITOR] {name} DOWN since {eastern} — {layer wording} (confidence {n}: {reasons}).
        Dashboard: {url}

    where the layer wording is Rule 10's exact operator phrasing for the failed layer --
    "login screen unreachable / not rendering" for a precursor (pulse/render) failure,
    "online banking behind login not rendering" for an authed one. Before this, the email
    said "{trigger_layer} failed" plus a hand-written hint, so an operator paged at 3am read
    "render failed / page loads, content missing" while the dashboard (which they'd have to
    open) showed Rule 10's wording. Rule 10 "every DOWN names its layer" exists precisely so they don't need the browser.

    The fallback covers a trigger_layer this vocabulary doesn't know: name it plainly and
    still send, rather than let an unmapped layer suppress the page entirely."""
    dashboard_url = f"http://localhost:{config.PORT}"
    reasons = ", ".join(event.fail_reasons)
    wording = layer_wording(event.trigger_layer) or f"{event.trigger_layer} layer failed"
    return (
        f"[MONITOR] {config.TARGET_NAME} DOWN since {to_eastern(event.since_ts)} — "
        f"{wording} (confidence {event.confidence}: {reasons}). "
        f"Dashboard: {dashboard_url}"
    )


def recovery_message(event: RecoveryEvent) -> str:
    return f"[MONITOR] {config.TARGET_NAME} RECOVERED after {_format_duration(event.duration_s)}."


def config_message(event: ConfigErrorEvent) -> str:
    # "Sign-in checks paused" per v3.8's locked copy -- and it's also the accurate
    # statement: a CONFIG_ERROR halts the auth track (Rule 4 "never retry a credential rejection"), while the pulse/render
    # precursor keeps checking every cycle. The old "Checks paused" read as though the
    # whole monitor had stopped.
    return (
        f"[MONITOR-CONFIG] {config.TARGET_NAME} needs attention — {event.fail_reason}. "
        f"Sign-in checks paused."
    )


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
