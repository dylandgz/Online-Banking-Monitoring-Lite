"""Presentation-layer time formatting (CLAUDE.md v3.1). Storage stays UTC ISO-8601
(Rule 6) everywhere in SQLite -- this module only converts for display: dashboard,
email bodies, /api responses, CSV export."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")


def to_eastern(ts_utc_iso: str) -> str:
    """Converts a UTC ISO-8601 timestamp to a human-readable America/New_York string
    with an explicit UTC offset, e.g. '2026-08-04 10:32:05 EDT (UTC-04:00)'. Using the
    zone (not a fixed EST offset) means DST is handled automatically."""
    dt = datetime.fromisoformat(ts_utc_iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    eastern = dt.astimezone(EASTERN)
    offset = eastern.strftime("%z")  # e.g. '-0400'
    offset_fmt = f"{offset[:3]}:{offset[3:]}"
    return f"{eastern.strftime('%Y-%m-%d %H:%M:%S')} {eastern.tzname()} (UTC{offset_fmt})"
