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

# [B45] The DOWN/RECOVERED email vocabulary, for the fixed-key format Power Automate parses
# into a Teams card. Two facts per layer: the SERVICE line (what stopped working, named the
# way a business reader would name it) and the DESCRIPTION line (what a customer experiences).
# Neither ever names a probe, a layer or a fail_reason -- the screenshot carries the detail.
# The CONFIG_ERROR email predates this and deliberately does not use these tables.
_EMAIL_SERVICE = {
    "pulse": "{name} Online Banking Website",
    "render": "{name} Online Banking Login Page",
    "authed": "{name} Online Banking Account Access",
}

_EMAIL_DOWN_DESCRIPTION = {
    "pulse": "{name}'s website is not responding. Customers may be unable to access Online Banking.",
    "render": "{name}'s sign-in page is not loading. Customers can reach the website, but the login form is not appearing, so they cannot sign in.",
    "authed": "{name}'s Online Banking is not loading after sign-in. Customers can sign in, but their accounts are not appearing.",
}

_EMAIL_RECOVERY_DESCRIPTION = {
    "pulse": "{name}'s website is responding again. Customers can reach Online Banking.",
    "render": "{name}'s sign-in page is loading again. Customers can sign in normally.",
    "authed": "{name}'s Online Banking is loading again after sign-in. Customers can see their accounts.",
}

# Fallbacks for an unmapped layer. An alert must still send with a vaguer description rather
# than not send at all -- and RecoveryEvent.trigger_layer is Optional, so None lands here too.
_EMAIL_SERVICE_FALLBACK = "{name} Online Banking"
_EMAIL_DOWN_FALLBACK = "{name}'s Online Banking is not available."
_EMAIL_RECOVERY_FALLBACK = "{name}'s Online Banking is available again."


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


def email_service_name(fail_layer: Optional[str], target_name: str) -> str:
    """The SERVICE line of a DOWN/RECOVERED email (B45)."""
    return _EMAIL_SERVICE.get(fail_layer or "", _EMAIL_SERVICE_FALLBACK).format(name=target_name)


def email_description(fail_layer: Optional[str], target_name: str, *, recovered: bool = False) -> str:
    """The DESCRIPTION line of a DOWN/RECOVERED email (B45), in customer terms."""
    if recovered:
        table, fallback = _EMAIL_RECOVERY_DESCRIPTION, _EMAIL_RECOVERY_FALLBACK
    else:
        table, fallback = _EMAIL_DOWN_DESCRIPTION, _EMAIL_DOWN_FALLBACK
    return table.get(fail_layer or "", fallback).format(name=target_name)
