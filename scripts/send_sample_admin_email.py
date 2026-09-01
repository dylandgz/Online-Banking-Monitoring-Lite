#!/usr/bin/env python
"""Send a sample CONFIG_ERROR email to ADMIN_EMAIL.

This script demonstrates what admin alerts look like by sending a realistic
sample config error email. CONFIG_ERROR emails are sent only to ADMIN_EMAIL,
separate from regular platform alerts (DOWN/RECOVERY) which go to RECIPIENTS_EMAIL.

Usage:
    python -m scripts.send_sample_admin_email

Requirements:
    - ADMIN_EMAIL must be configured in .env
    - GMAIL_USER and GMAIL_APP_PASSWORD must be configured
"""
import sys

import config
from monitor.channels.email_gmail import EmailGmailChannel
from monitor.state import ConfigErrorEvent


def main() -> None:
    # Validate config
    if not config.ADMIN_EMAIL:
        print("[admin-email] ADMIN_EMAIL not configured in .env")
        print("Admin email notifications are disabled. To enable them, set ADMIN_EMAIL and try again.")
        sys.exit(1)

    if not (config.GMAIL_USER and config.GMAIL_APP_PASSWORD):
        print("[admin-email] GMAIL_USER and GMAIL_APP_PASSWORD not configured in .env")
        print("To send emails, configure Gmail credentials and try again.")
        sys.exit(1)

    channel = EmailGmailChannel()

    try:
        print("[admin-email] Sending sample CONFIG_ERROR email to admin...\n")

        # Sample CONFIG_ERROR event
        config_error_event = ConfigErrorEvent(
            ts="2026-09-01T14:32:15-04:00",
            fail_reason="auth_rejected",
        )
        channel.send(config_error_event)

        print(f"  → Sent to: {config.ADMIN_EMAIL}")
        from monitor.channels.email_gmail import _build_config_subject
        print(f"  ✓ Subject: {_build_config_subject(config_error_event)}")
        print(f"  ✓ Reason: auth_rejected (credentials rejected by the platform)\n")

        print("[admin-email] Admin email sent successfully! ✓")

    except Exception as exc:
        print(f"\n[admin-email] Error sending email: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
