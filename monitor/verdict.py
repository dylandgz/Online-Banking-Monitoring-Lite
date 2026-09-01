"""[v3.8] The platform verdict vocabulary: status severity ordering, worst_of(), and the
exact operator wording Rule 10 "every DOWN names its layer" mandates for each failed layer. Pure -- no I/O, no config.

This module exists because the same two facts (which status is worse, and what an
operator is told about a failed layer) were previously written out three times -- in
monitor/main.py, monitor/web.py, and monitor/channels/email_gmail.py -- and had already
drifted apart: the dashboard used Rule 10's locked wording while the alert email, the
thing that actually pages a human, still used pre-v3.8 phrasing. Rule 10's promise ("the
operator must never need a browser to know which wall failed") only holds if every
surface reads the wording from one place, so it lives here.

Note this is the *vocabulary*, not a message formatter -- CONTRIBUTING.md deliberately
requires each alert channel to format its own medium's message. Channels compose these
phrases into their own strings; they don't share a formatter.
"""
from typing import Optional

# Severity ladder for worst_of(). DEGRADED and CONFIG_ERROR both sit below DOWN because
# neither pages (Rule 7 "only DOWN pages") -- the ordering is about which status explains the platform
# verdict, not about which is more urgent to a human.
STATUS_ORDER = {"UP": 0, "DEGRADED": 1, "CONFIG_ERROR": 2, "DOWN": 3}

# Rule 10 "every DOWN names its layer": exact operator wording, per layer. Locked by CLAUDE.md's v3.8 email-copy block --
# do not reword these without amending CLAUDE.md.
PRECURSOR_WORDING = "login screen unreachable / not rendering"
AUTHED_WORDING = "online banking behind login not rendering"

_LAYER_WORDING = {
    "pulse": PRECURSOR_WORDING,
    "render": PRECURSOR_WORDING,
    "authed": AUTHED_WORDING,
}

# Plain-English descriptions for email notifications (B44)
_EMAIL_LAYER_DESCRIPTION = {
    "pulse": {
        "subject": "website not responding",
        "body": "website is not responding. Customers cannot reach the login page.",
    },
    "render": {
        "subject": "sign-in page not loading",
        "body": "sign-in page is not loading correctly. Customers can reach the website, but the login form is not appearing.",
    },
    "authed": {
        "subject": "not loading after sign-in",
        "body": "online banking is not loading after sign-in. Customers can sign in, but their accounts are not appearing.",
    },
}

# Reason codes mapped to plain English for email
_EMAIL_REASON_TEXT = {
    "dns": "the website's address could not be looked up",
    "conn_refused": "the server refused the connection",
    "timeout": "the server did not respond in time",
    "nav_error": "the page failed to load",
    "element_missing": "the page loaded, but the expected content never appeared",
    "bad_status:500": "the server returned an error (HTTP 500)",
    "bad_status:502": "the server returned an error (HTTP 502)",
    "bad_status:503": "the server returned an error (HTTP 503)",
}


def severity(status: str) -> int:
    """Position on the severity ladder. Unknown statuses sort as UP (0) rather than
    raising -- a status string this module doesn't recognize must never be able to crash
    the scheduler mid-cycle or blank the dashboard."""
    return STATUS_ORDER.get(status, 0)


def unified_verdict(main_status: str, auth_status: Optional[str]) -> str:
    """platform_status = worst_of(main, auth). auth_status=None means the auth track
    didn't participate (not configured, or its own CONFIG_ERROR per Rule 4 "never retry a credential rejection"), so the main
    track alone decides."""
    if auth_status is None:
        return main_status
    return auth_status if severity(auth_status) > severity(main_status) else main_status


def layer_wording(fail_layer: Optional[str]) -> Optional[str]:
    """Rule 10's ("every DOWN names its layer") operator wording for the layer that failed, or None if the layer is
    unknown/absent. Callers decide their own fallback -- an alert should still send with
    a degraded description rather than not send at all."""
    return _LAYER_WORDING.get(fail_layer) if fail_layer else None


def email_layer_subject(fail_layer: Optional[str]) -> str:
    """Plain-English subject line fragment for email alerts (B44)."""
    if not fail_layer:
        return "online banking not available"
    desc = _EMAIL_LAYER_DESCRIPTION.get(fail_layer)
    return desc["subject"] if desc else "online banking not available"


def email_layer_body(fail_layer: Optional[str]) -> str:
    """Plain-English body text for email alerts (B44)."""
    if not fail_layer:
        return "online banking is not available."
    desc = _EMAIL_LAYER_DESCRIPTION.get(fail_layer)
    return desc["body"] if desc else "online banking is not available."


def email_reason_text(fail_reason: str) -> str:
    """Convert a fail_reason code to plain English for email (B44)."""
    # Try exact match first
    if fail_reason in _EMAIL_REASON_TEXT:
        return _EMAIL_REASON_TEXT[fail_reason]
    # Try prefix match for bad_status codes
    if fail_reason.startswith("bad_status:"):
        code = fail_reason.rsplit(":", 1)[-1]
        return f"the server returned an error (HTTP {code})"
    # Fallback
    return fail_reason
