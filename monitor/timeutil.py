"""The project's time conventions, in one place.

Two directions, deliberately not symmetric:

- **Storage is UTC.** `now_iso()` produces every timestamp written to SQLite. UTC is not a
  style preference here -- it is what keeps the stored record correct. America/New_York
  repeats an hour every fall, so two probes 60 minutes apart would record an identical
  local timestamp: `cycles` has no autoincrement id (`cycle_id` is a UUID), so `ORDER BY ts`
  is the only thing establishing sequence in `query_cycles`/`get_last_cycle`; `uptime_pct`'s
  `WHERE ts >= ?` window would double-count the repeated hour; `state.apply_check` measures
  the burst window by subtracting two stored timestamps, and `RecoveryEvent.duration_s` does
  the same -- across the fall-back boundary a real 30-minute outage computes as -30.

- **Presentation is America/New_York.** `to_eastern()` is called only at the outer edges
  (dashboard JSON, CSV export, alert email bodies). Nothing between the probe and the
  database ever sees Eastern.

`artifact_stamp()` sits on the presentation side despite producing a filename: a screenshot
name is read by a human next to Eastern timestamps, and for most artifacts it is the *only*
handle that exists -- `checks` has no `screenshot_path` column, so only the screenshot
belonging to a DOWN-triggering probe is recorded anywhere (see B35).

Timezone is fixed to America/New_York in code rather than read from the host: the monitor
runs on a VM whose clock is UTC while the operator reads the dashboard from an Eastern
laptop, and "whatever local time the process happens to be in" would render the same
database differently depending on where it ran.
"""
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")


def now_iso(now: Optional[datetime] = None) -> str:
    """The UTC ISO-8601 stamp every stored timestamp uses. `now` is an injection point for
    tests only -- production always calls this with no argument."""
    return (now or datetime.now(timezone.utc)).isoformat()


def artifact_stamp(now: Optional[datetime] = None) -> str:
    """Filename-safe timestamp for a screenshot in ARTIFACTS_DIR, e.g. '20260827T140332Z'.

    Filename-safe means no ':' -- illegal on Windows and awkward everywhere -- which is why
    this is a strftime and not an isoformat()."""
    return (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")


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
