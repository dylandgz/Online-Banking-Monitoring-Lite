#!/usr/bin/env python
"""Send sample DOWN and RECOVERY emails to RECIPIENTS_EMAIL.

This script demonstrates what monitor alerts look like by sending realistic
sample emails. The DOWN email carries a generated placeholder screenshot clearly
marked TEST SCREENSHOT, so a recipient can never mistake a sample for a real
outage artifact. Temporary files are cleaned up afterwards.

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


# Fallback if Chromium is unavailable: a 1x1 red PNG, so the script can still demonstrate
# the email even on a host where `playwright install` has not been run.
MINIMAL_PNG = (
    b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01'
    b'\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00'
    b'\x00\x01\x01\x00\x05\x1b\xf1\xde\x00\x00\x00\x00IEND\xaeB`\x82'
)

# The placeholder is rendered by Playwright rather than drawn by hand: Playwright already
# takes every real screenshot this monitor attaches, so the sample lands in the inbox as
# the same kind of artifact -- same encoder, same dimensions, same viewport -- instead of a
# 1x1 pixel that tells a reviewer nothing about how a real attachment will look.
PLACEHOLDER_HTML = """<!doctype html>
<html><body style="margin:0;height:100vh;display:flex;align-items:center;
justify-content:center;background:#f4f4f5;font-family:Helvetica,Arial,sans-serif;">
  <div style="text-align:center;border:6px dashed #9ca3af;border-radius:16px;padding:64px 96px;">
    <div style="font-size:76px;font-weight:700;letter-spacing:2px;color:#111827;">TEST SCREENSHOT</div>
    <div style="font-size:28px;margin-top:24px;color:#4b5563;">Online Banking Monitor Lite &mdash; sample alert</div>
    <div style="font-size:24px;margin-top:8px;color:#6b7280;">Not a real outage. No action required.</div>
  </div>
</body></html>"""


def _create_temp_screenshot() -> str:
    """Render the TEST SCREENSHOT placeholder to a temporary PNG and return its path."""
    path = tempfile.NamedTemporaryFile(suffix=".png", delete=False).name
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page(viewport={"width": 1280, "height": 800})
                page.set_content(PLACEHOLDER_HTML)
                page.screenshot(path=path)
            finally:
                browser.close()
    except Exception as exc:
        # Never let the placeholder block the thing being demonstrated -- the emails.
        print(f"     ! could not render placeholder ({exc}); falling back to a 1x1 PNG")
        Path(path).write_bytes(MINIMAL_PNG)
    return path


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
        print(f"     ✓ Attachment: placeholder marked TEST SCREENSHOT\n")

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
