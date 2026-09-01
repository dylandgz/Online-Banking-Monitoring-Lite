#!/usr/bin/env python
"""Send sample DOWN and RECOVERY emails to RECIPIENTS_EMAIL.

This script demonstrates what monitor alerts look like by sending realistic
sample emails. It creates temporary 1x1 PNG attachments so you can see how
screenshots appear in the email, but cleans up after itself.

Usage:
    python -m scripts.send_sample_recipient_emails

Requirements:
    - RECIPIENTS_EMAIL must be configured in .env
    - GMAIL_USER and GMAIL_APP_PASSWORD must be configured
"""
import sys
import tempfile
from pathlib import Path

import config
from monitor.channels.email_gmail import EmailGmailChannel
from monitor.state import DownEvent, RecoveryEvent


# Minimal 1x1 red PNG (68 bytes)
MINIMAL_PNG = (
    b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01'
    b'\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00'
    b'\x00\x01\x01\x00\x05\x1b\xf1\xde\x00\x00\x00\x00IEND\xaeB`\x82'
)


def _create_temp_screenshot() -> str:
    """Create a temporary PNG file and return its path."""
    with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as f:
        f.write(MINIMAL_PNG)
        return f.name


def main() -> None:
    # Validate config
    if not config.RECIPIENTS_EMAIL:
        print("[sample-emails] RECIPIENTS_EMAIL not configured in .env")
        print("To test recipient emails, set RECIPIENTS_EMAIL and try again.")
        sys.exit(1)

    if not (config.GMAIL_USER and config.GMAIL_APP_PASSWORD):
        print("[sample-emails] GMAIL_USER and GMAIL_APP_PASSWORD not configured in .env")
        print("To send emails, configure Gmail credentials and try again.")
        sys.exit(1)

    channel = EmailGmailChannel()
    temp_screenshot = None

    try:
        print("[sample-emails] Sending 2 sample emails to recipients...\n")

        # 1. Sample DOWN event with screenshot
        print("  1. DOWN event")
        temp_screenshot = _create_temp_screenshot()
        down_event = DownEvent(
            since_ts="2026-09-01T14:32:15-04:00",
            confidence=4,
            fail_reasons=("timeout", "timeout", "timeout", "timeout"),
            trigger_layer="pulse",
            target_name=config.TARGET_NAME,
            page_url=config.TARGET_URL,
            screenshot_path=temp_screenshot,
        )
        channel.send(down_event)
        print(f"     → Sent to: {config.RECIPIENTS_EMAIL}")
        if config.RECIPIENTS_CC:
            print(f"     → CC: {config.RECIPIENTS_CC}")
        if config.RECIPIENTS_BCC:
            print(f"     → BCC: {config.RECIPIENTS_BCC}")
        from monitor.channels.email_gmail import _build_down_subject
        print(f"     ✓ Subject: {_build_down_subject(down_event)}")
        print(f"     ✓ Attachment: sample screenshot (1x1 PNG)\n")

        # 2. Sample RECOVERY event
        print("  2. RECOVERY event")
        recovery_event = RecoveryEvent(
            since_ts="2026-09-01T14:32:15-04:00",
            ended_at="2026-09-01T14:39:45-04:00",
            duration_s=450,
            confidence=4,
            fail_reasons=("timeout",),
            trigger_layer="pulse",
            target_name=config.TARGET_NAME,
            page_url=config.TARGET_URL,
        )
        channel.send(recovery_event)
        print(f"     → Sent to: {config.RECIPIENTS_EMAIL}")
        if config.RECIPIENTS_CC:
            print(f"     → CC: {config.RECIPIENTS_CC}")
        if config.RECIPIENTS_BCC:
            print(f"     → BCC: {config.RECIPIENTS_BCC}")
        from monitor.channels.email_gmail import _build_recovery_subject
        print(f"     ✓ Subject: {_build_recovery_subject(recovery_event)}")
        print(f"     ✓ No attachment (recovery emails don't include screenshots)\n")

        print("[sample-emails] Both emails sent successfully! ✓")

    except Exception as exc:
        print(f"\n[sample-emails] Error sending emails: {exc}", file=sys.stderr)
        sys.exit(1)

    finally:
        # Clean up temporary screenshot
        if temp_screenshot and Path(temp_screenshot).exists():
            Path(temp_screenshot).unlink()


if __name__ == "__main__":
    main()
