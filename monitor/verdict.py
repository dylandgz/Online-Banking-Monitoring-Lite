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
