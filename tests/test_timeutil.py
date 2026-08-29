from datetime import datetime, timedelta, timezone

from monitor.timeutil import artifact_stamp, now_iso, to_eastern


def test_winter_utc_converts_to_est():
    # 2026-01-15 12:00:00 UTC -> 07:00:00 EST (UTC-05:00), no DST in January.
    assert to_eastern("2026-01-15T12:00:00+00:00") == "2026-01-15 07:00:00 EST (UTC-05:00)"


def test_summer_utc_converts_to_edt():
    # 2026-07-15 12:00:00 UTC -> 08:00:00 EDT (UTC-04:00), DST active in July.
    assert to_eastern("2026-07-15T12:00:00+00:00") == "2026-07-15 08:00:00 EDT (UTC-04:00)"


def test_naive_iso_assumed_utc():
    # Timestamps stored without an explicit offset (as SQLite round-trips them) are
    # treated as UTC, matching how they were written (datetime.now(timezone.utc).isoformat()).
    assert to_eastern("2026-01-15T12:00:00") == "2026-01-15 07:00:00 EST (UTC-05:00)"


def test_offset_always_present_and_unambiguous():
    result = to_eastern("2026-03-08T12:00:00+00:00")
    assert "UTC" in result
    assert result.count(":") >= 3  # H:M:S plus the UTC offset's own colon


# --- now_iso: the storage stamp -------------------------------------------------------
# Every timestamp written to SQLite comes from here. These pin the properties the rest of
# the system relies on: state.apply_check and db.uptime_pct both parse these strings back
# with fromisoformat and do arithmetic on them, so a naive or non-UTC value would corrupt
# burst timing and incident durations rather than fail loudly.

def test_now_iso_is_utc_and_offset_aware():
    ts = now_iso()
    parsed = datetime.fromisoformat(ts)
    assert parsed.tzinfo is not None, "a naive stamp would break comparisons against aware nows"
    assert parsed.utcoffset() == timedelta(0)


def test_now_iso_round_trips_through_fromisoformat():
    # main.py, state.py and db.py all read these back with fromisoformat.
    t = datetime(2026, 8, 27, 14, 3, 32, tzinfo=timezone.utc)
    assert datetime.fromisoformat(now_iso(t)) == t


# --- artifact_stamp: the screenshot filename stamp ------------------------------------

def test_artifact_stamp_matches_the_historical_format():
    # Pins the format the 204 existing files in data/artifacts already use, so the
    # consolidation into timeutil provably changed no output. B26 will change this
    # deliberately; this assertion is what makes that change visible rather than silent.
    t = datetime(2026, 8, 27, 14, 3, 32, tzinfo=timezone.utc)
    assert artifact_stamp(t) == "20260827T140332Z"


def test_artifact_stamp_is_filename_safe():
    # ':' is illegal in a filename on Windows and awkward everywhere -- which is why this
    # is a strftime and not an isoformat().
    assert ":" not in artifact_stamp()
    assert "/" not in artifact_stamp()
