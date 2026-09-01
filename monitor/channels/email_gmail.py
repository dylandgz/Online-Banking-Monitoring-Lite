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
from monitor.timeutil import to_eastern
from monitor.verdict import email_layer_body, email_layer_subject, email_reason_text


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


def _format_time_ago(ts: str) -> str:
    """Convert ISO timestamp to human-readable 'X minutes ago' format."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    started = datetime.fromisoformat(ts)
    diff = (now - started).total_seconds()

    if diff < 60:
        return f"{int(diff)} seconds ago"
    minutes, _ = divmod(int(diff), 60)
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} hour{'s' if hours != 1 else ''}, {minutes} minute{'s' if minutes != 1 else ''} ago"


def _build_down_subject(event: DownEvent) -> str:
    """Build the DOWN email subject line."""
    target = event.target_name or config.TARGET_NAME
    layer_desc = email_layer_subject(event.trigger_layer)
    return f"[MONITOR] {target} Online Banking DOWN — {layer_desc}"


def _build_down_body(event: DownEvent) -> str:
    """Build the DOWN email body text (plain text part of multipart)."""
    target = event.target_name or config.TARGET_NAME
    layer_desc = email_layer_body(event.trigger_layer)
    time_ago = _format_time_ago(event.since_ts)
    started_eastern = to_eastern(event.since_ts)

    # Reason: if there's only one fail_reason repeated, say "checked X times";
    # otherwise list the distinct reasons
    if event.fail_reasons:
        first_reason = event.fail_reasons[0]
        reason_text = email_reason_text(first_reason)
        check_count = len(event.fail_reasons)
    else:
        reason_text = "unknown"
        check_count = 0

    lines = [
        "This is Online Banking Monitor Lite — a monitor built by Francisco and Dylan from Teachers RPA team.",
        "",
        f"{target}'s {layer_desc}",
        "",
        f"Started {started_eastern}, {time_ago}.",
        "",
        f"Checked {check_count} times in a row — {reason_text}.",
        "",
        f"URL: {event.page_url or config.TARGET_URL}",
    ]

    return "\n".join(lines)


def _build_recovery_subject(event: RecoveryEvent) -> str:
    """Build the RECOVERY email subject line."""
    target = event.target_name or config.TARGET_NAME
    return f"[MONITOR] {target} Online Banking Recovered"


def _build_recovery_body(event: RecoveryEvent) -> str:
    """Build the RECOVERY email body text."""
    target = event.target_name or config.TARGET_NAME
    duration_text = _format_duration(event.duration_s)

    lines = [
        "This is Online Banking Monitor Lite — a monitor built by Francisco and Dylan from Teachers RPA team.",
        "",
        f"{target}'s online banking has recovered and is now loading normally.",
        "",
        f"The outage lasted {duration_text}.",
        "",
        f"URL: {event.page_url or config.TARGET_URL}",
    ]

    return "\n".join(lines)


def _build_config_subject() -> str:
    """Build the CONFIG_ERROR email subject line."""
    return f"[MONITOR-CONFIG] {config.TARGET_NAME} needs attention"


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
            subject = _build_config_subject()
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
    subject = _build_config_subject()
    body = _build_config_body(event)
    return f"{subject}\n\n{body}"
