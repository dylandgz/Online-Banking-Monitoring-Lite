"""Email channel: one send on DOWN, one on RECOVERY. No re-emitting mid-incident
(state.py enforces that by only emitting events on transitions)."""
import smtplib
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import config
from monitor.channels.base import AlertChannel, AlertEvent
from monitor.state import ConfigErrorEvent, DownEvent, RecoveryEvent
from monitor.timeutil import to_eastern, to_eastern_without_offset
from monitor.verdict import email_description, email_service_name


def _parse_email_list(emails_str: str) -> list:
    """Parse comma-separated email list from env var. Returns empty list if not set."""
    return [e.strip() for e in (emails_str or "").split(",") if e.strip()]


def _format_duration(duration_s: int) -> str:
    minutes, seconds = divmod(duration_s, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


# The fixed body keys, in emission order. Every DOWN and RECOVERED email carries all of
# them so one Power Automate parser reads both without branching on STATUS; a key that does
# not apply to the event carries NOT_APPLICABLE rather than being omitted.
MONITOR_NAME = "Online Banking Monitor Lite"
ALERT_SOURCE = "Teachers RPA Team"
NOT_APPLICABLE = "N/A"


def _default_url(fail_layer) -> str:
    """The URL the failed layer was actually looking at. The authed check navigates
    AUTHED_URL directly (never derived from LOGIN_URL), so an authed alert that printed
    TARGET_URL would name a page that was never probed."""
    if fail_layer == "authed" and config.AUTHED_URL:
        return config.AUTHED_URL
    return config.TARGET_URL


def _build_subject(status: str, target_name: str, stamp_eastern: str) -> str:
    """Pipe-delimited so a Power Automate trigger can split the subject into fields
    without parsing prose. Field 2 is the machine-readable status."""
    return f"[OLB MONITOR LITE]|{status}|{target_name} Online Banking|{stamp_eastern}"


def _build_body(*, status: str, service: str, description: str,
                start_time: str, end_time: str, duration: str, url: str) -> str:
    """The fixed nine-key body. Order and key names are the contract the Teams flow parses
    -- changing either breaks the card, so treat this like a schema, not like copy."""
    return "\n".join([
        f"MONITOR: {MONITOR_NAME}", "",
        f"STATUS: {status}", "",
        f"SERVICE: {service}", "",
        "DESCRIPTION:", description, "",
        f"START_TIME: {start_time}", "",
        f"END_TIME: {end_time}", "",
        f"DURATION: {duration}", "",
        f"URL: {url}", "",
        f"SOURCE: {ALERT_SOURCE}",
    ])


def _build_down_subject(event: DownEvent) -> str:
    target = event.target_name or config.TARGET_NAME
    return _build_subject("DOWN", target, to_eastern_without_offset(event.since_ts))


def _build_down_body(event: DownEvent) -> str:
    target = event.target_name or config.TARGET_NAME
    return _build_body(
        status="DOWN",
        service=email_service_name(event.trigger_layer, target),
        description=email_description(event.trigger_layer, target),
        start_time=to_eastern_without_offset(event.since_ts),
        end_time=NOT_APPLICABLE,   # the incident is open; it has no end yet
        duration=NOT_APPLICABLE,
        url=event.page_url or _default_url(event.trigger_layer),
    )


def _build_recovery_subject(event: RecoveryEvent) -> str:
    target = event.target_name or config.TARGET_NAME
    return _build_subject("RECOVERED", target, to_eastern_without_offset(event.ended_at))


def _build_recovery_body(event: RecoveryEvent) -> str:
    target = event.target_name or config.TARGET_NAME
    return _build_body(
        status="RECOVERED",
        service=email_service_name(event.trigger_layer, target),
        description=email_description(event.trigger_layer, target, recovered=True),
        start_time=to_eastern_without_offset(event.since_ts),
        end_time=to_eastern_without_offset(event.ended_at),
        duration=_format_duration(event.duration_s),
        url=event.page_url or _default_url(event.trigger_layer),
    )


def _build_config_subject(event: ConfigErrorEvent) -> str:
    """Build the CONFIG_ERROR email subject line."""
    # Extract time from ISO timestamp (e.g., "2026-08-11T14:00:00+00:00" -> "14:00")
    time_part = event.ts[11:16] if len(event.ts) > 10 else ""
    return f"[MONITOR-CONFIG] {config.TARGET_NAME} needs attention {time_part}".strip()


def _build_config_body(event: ConfigErrorEvent) -> str:
    """Build the CONFIG_ERROR email body text."""
    ts_eastern = to_eastern(event.ts)
    return (
        f"This is Online Banking Monitor Lite — a monitor built by Francisco and Dylan from Teachers RPA team.\n"
        f"\n"
        f"{config.TARGET_NAME} configuration error at {ts_eastern}: {event.fail_reason}\n"
        f"\n"
        f"Sign-in checks are paused until this is resolved."
    )


class EmailGmailChannel(AlertChannel):
    name = "email"

    def send(self, event: AlertEvent) -> None:
        if isinstance(event, DownEvent):
            subject = _build_down_subject(event)
            body = _build_down_body(event)
            screenshot = event.screenshot_path
            recipients = _parse_email_list(config.RECIPIENTS_EMAIL)
        elif isinstance(event, RecoveryEvent):
            subject = _build_recovery_subject(event)
            body = _build_recovery_body(event)
            screenshot = None  # no screenshot for recovery
            recipients = _parse_email_list(config.RECIPIENTS_EMAIL)
        elif isinstance(event, ConfigErrorEvent):
            if not config.ADMIN_EMAIL:
                return  # ADMIN_EMAIL not configured; CONFIG_ERROR notifications disabled
            subject = _build_config_subject(event)
            body = _build_config_body(event)
            screenshot = None
            recipients = [config.ADMIN_EMAIL.strip()]
        else:
            raise TypeError(f"unknown event type: {event!r}")

        self._send_email(subject=subject, body=body, screenshot_path=screenshot, recipients=recipients)

    @staticmethod
    def _send_email(subject: str, body: str, screenshot_path=None, recipients=None) -> None:
        # Parse recipient lists; if recipients is provided (CONFIG_ERROR case), use only that
        if recipients is None:
            to_list = _parse_email_list(config.RECIPIENTS_EMAIL)
            cc_list = _parse_email_list(config.RECIPIENTS_CC)
            bcc_list = _parse_email_list(config.RECIPIENTS_BCC)
        else:
            # CONFIG_ERROR: send only to admin, no CC/BCC
            to_list = recipients
            cc_list = []
            bcc_list = []

        if not (config.GMAIL_USER and config.GMAIL_APP_PASSWORD and to_list):
            print(f"[email] not configured, skipping send. subject={subject!r}")
            return

        # Build multipart message
        msg = MIMEMultipart("mixed")
        msg["Subject"] = subject
        msg["From"] = config.GMAIL_USER
        msg["To"] = ", ".join(to_list)
        if cc_list:
            msg["Cc"] = ", ".join(cc_list)
        # Note: BCC is NOT added to headers (stays hidden)

        # Attach text body
        msg.attach(MIMEText(body, "plain"))

        # Attach screenshot if present and file exists
        if screenshot_path:
            try:
                path = Path(screenshot_path)
                if path.exists() and path.is_file():
                    with open(path, "rb") as f:
                        img_data = f.read()
                    img = MIMEImage(img_data, name=path.name)
                    img.add_header("Content-Disposition", "attachment", filename=path.name)
                    msg.attach(img)
            except Exception as exc:
                print(f"[email] failed to attach screenshot {screenshot_path}: {exc}")

        # Send via SMTP to all recipients (To + Cc + Bcc)
        all_recipients = to_list + cc_list + bcc_list
        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
                server.login(config.GMAIL_USER, config.GMAIL_APP_PASSWORD)
                server.sendmail(config.GMAIL_USER, all_recipients, msg.as_string())
        except Exception as exc:
            print(f"[email] SMTP send failed: {exc}")
            raise


# Backward-compat wrappers for tests that expect the old combined format
def down_message(event: DownEvent) -> str:
    """Backward-compat wrapper combining subject + body for tests."""
    subject = _build_down_subject(event)
    body = _build_down_body(event)
    return f"{subject}\n\n{body}"


def recovery_message(event: RecoveryEvent) -> str:
    """Backward-compat wrapper combining subject + body for tests."""
    subject = _build_recovery_subject(event)
    body = _build_recovery_body(event)
    return f"{subject}\n\n{body}"


def config_message(event: ConfigErrorEvent) -> str:
    """Backward-compat wrapper combining subject + body for tests."""
    subject = _build_config_subject(event)
    body = _build_config_body(event)
    return f"{subject}\n\n{body}"
