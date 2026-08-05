"""Loads and validates settings from .env. Fails fast at startup if required values are missing."""
import os
import sys

from dotenv import load_dotenv

load_dotenv()

TARGET_NAME = os.getenv("TARGET_NAME")
TARGET_URL = os.getenv("TARGET_URL")
REQUIRED_TEXT = os.getenv("REQUIRED_TEXT")
REQUIRED_ROLE = os.getenv("REQUIRED_ROLE")
REQUIRED_NAME = os.getenv("REQUIRED_NAME")

CHECK_INTERVAL_S = int(os.getenv("CHECK_INTERVAL_S", "60"))
BROWSER_TIMEOUT_MS = int(os.getenv("BROWSER_TIMEOUT_MS", "15000"))

# Stage 5: confirmation burst + confidence scoring (replaces the old FAILS_TO_DOWN rule).
# [v3.1] defaults updated: DOWN requires score >= DOWN_CONFIDENCE AND >= MIN_FAILED_PROBES
# distinct failed probes in the burst window -- see CLAUDE.md's confidence-scoring section.
BURST_DELAYS_S = [int(x) for x in os.getenv("BURST_DELAYS_S", "0,15,35,55").split(",") if x.strip()]
BURST_JITTER_S = int(os.getenv("BURST_JITTER_S", "5"))
BURST_WINDOW_S = int(os.getenv("BURST_WINDOW_S", "90"))
DOWN_CONFIDENCE = int(os.getenv("DOWN_CONFIDENCE", "4"))
MIN_FAILED_PROBES = int(os.getenv("MIN_FAILED_PROBES", "3"))

RECIPIENTS_EMAIL = os.getenv("RECIPIENTS_EMAIL")
GMAIL_USER = os.getenv("GMAIL_USER")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")

DASHBOARD_USER = os.getenv("DASHBOARD_USER")
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD")
PORT = int(os.getenv("PORT", "8080"))
DB_PATH = os.getenv("DB_PATH", "./data/monitor.db")

ARTIFACTS_DIR = "./data/artifacts"

ALERT_CHANNELS = [c.strip() for c in os.getenv("ALERT_CHANNELS", "email").split(",") if c.strip()]

# Rule 14: headless is the only mode the scheduler may run; HEADLESS=false is a local-debug
# escape hatch only, never scheduled. Recorded on every check row (checks.browser_mode).
HEADLESS = os.getenv("HEADLESS", "true").lower() != "false"
BROWSER_MODE = "headless" if HEADLESS else "headed"

# Rule 12: only a real branded browser binary + a polite UA are permitted bot-challenge mitigations.
BROWSER_CHANNEL = os.getenv("BROWSER_CHANNEL", "chrome")


def validate_core():
    """Validates the settings the app needs at startup. Email settings are not
    required here — alert.py logs and skips sending if they're unset, since a
    missing app password shouldn't take the whole monitor down."""
    missing = []
    if not TARGET_NAME:
        missing.append("TARGET_NAME")
    if not TARGET_URL:
        missing.append("TARGET_URL")
    if not REQUIRED_TEXT and not (REQUIRED_ROLE and REQUIRED_NAME):
        missing.append("REQUIRED_TEXT (or REQUIRED_ROLE + REQUIRED_NAME)")
    if not DASHBOARD_USER:
        missing.append("DASHBOARD_USER")
    if not DASHBOARD_PASSWORD:
        missing.append("DASHBOARD_PASSWORD")

    if missing:
        sys.exit(
            "Missing required .env values: " + ", ".join(missing) +
            "\nCopy .env.example to .env and fill these in."
        )
