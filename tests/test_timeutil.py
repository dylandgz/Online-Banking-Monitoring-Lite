from datetime import datetime, timedelta, timezone

from monitor.timeutil import artifact_stamp, now_iso, to_eastern, to_eastern_without_offset


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


# --- to_eastern_without_offset: the alert-email stamp (B45) ---------------------------
# Same conversion as to_eastern(), minus the parenthetical. It is a field in a
# pipe-delimited subject line, so the exact shape is a parsing contract, not copy.

def test_without_offset_drops_the_parenthetical_but_keeps_the_zone():
    assert to_eastern_without_offset("2026-09-01T18:32:15+00:00") == "2026-09-01 14:32:15 EDT"


def test_without_offset_still_follows_dst():
    # The zone abbreviation is read from the zone, so the same call yields EST in winter.
    assert to_eastern_without_offset("2026-01-15T12:00:00+00:00") == "2026-01-15 07:00:00 EST"


def test_without_offset_treats_naive_iso_as_utc():
    # Matches to_eastern(): SQLite round-trips some stamps without an explicit offset.
    assert to_eastern_without_offset("2026-01-15T12:00:00") == "2026-01-15 07:00:00 EST"


def test_without_offset_never_emits_a_pipe():
    # It is the last field of "[OLB MONITOR LITE]|DOWN|<name>|<stamp>" -- a pipe in the
    # stamp would add a fifth field and break the Power Automate split.
    assert "|" not in to_eastern_without_offset("2026-09-01T18:32:15+00:00")


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

def test_artifact_stamp_is_eastern_and_matches_the_dashboard():
    # [B26] The whole point: the filename must name the same wall-clock time the dashboard,
    # CSV and email show for that failure. 14:03 UTC is 10:03 EDT.
    t = datetime(2026, 8, 27, 14, 3, 32, tzinfo=timezone.utc)
    assert artifact_stamp(t) == "20260827T100332-0400"
    assert to_eastern(t.isoformat()).startswith("2026-08-27 10:03:32")


def test_artifact_stamp_shifts_the_date_not_just_the_hour():
    # An evening-Eastern failure used to be filed under the NEXT day's date -- the part of
    # B26 that actually misleads, since you look for the wrong day entirely.
    t = datetime(2026, 1, 15, 2, 10, 5, tzinfo=timezone.utc)   # 9:10pm Jan 14 Eastern
    assert artifact_stamp(t).startswith("20260114T211005")


def test_no_filename_collision_on_the_dst_fallback():
    # B26 hazard 2, the one that silently destroys evidence: 01:30 EDT and 01:30 EST are 60
    # minutes apart and both render '20261101T013000'. Without the offset the second
    # screenshot overwrites the first.
    edt = artifact_stamp(datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc))
    est = artifact_stamp(datetime(2026, 11, 1, 6, 30, tzinfo=timezone.utc))
    assert edt != est, "two distinct failures must never share a filename"
    assert edt.endswith("-0400") and est.endswith("-0500")


def test_lexical_sort_still_matches_chronological_sort():
    # B26 hazard 1. With `checks` holding no screenshot_path (B35), `ls data/artifacts` is
    # how most of these files are found, so directory order has to stay truthful -- including
    # across the repeated hour, where '-0400' must sort before '-0500'.
    moments = [
        datetime(2026, 11, 1, 4, 30, tzinfo=timezone.utc),   # 00:30 EDT
        datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc),   # 01:30 EDT
        datetime(2026, 11, 1, 6, 30, tzinfo=timezone.utc),   # 01:30 EST -- clock repeated
        datetime(2026, 11, 1, 7, 30, tzinfo=timezone.utc),   # 02:30 EST
    ]
    stamps = [artifact_stamp(m) for m in moments]
    assert stamps == sorted(stamps), "directory order must not lie about what happened first"


def test_artifact_stamp_is_filename_safe():
    # ':' is illegal in a filename on Windows and awkward everywhere -- which is why this
    # is a strftime and not an isoformat().
    assert ":" not in artifact_stamp()
    assert "/" not in artifact_stamp()
