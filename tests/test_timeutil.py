from monitor.timeutil import to_eastern


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
