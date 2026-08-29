"""B16 + B35: the evidence a failing probe produced is recorded on the probe's own row.

Both defects had the same shape -- the value existed in memory at the moment of failure and
was then dropped on the floor:

  B35  a screenshot is written on EVERY browser-layer failure, but the path survived only
       when open_incident() ran, i.e. only for the one probe that crossed the DOWN
       threshold. Measured on the live DB before this change: 204 files on disk, 9 reachable
       from any surface. The other 195 were flaps, burst re-probes 2-4, CONFIG_ERROR
       failures and suppressed auth failures -- exactly the ambiguous cases where the image
       is worth most.

  B16  nothing recorded where the browser actually ended up, so "the bank's marketing site
       rendered in a frame at the correct authed URL" and "a cross-origin redirect away to
       it" were indistinguishable in the record. Both report element_missing. Telling them
       apart cost a multi-hour diagnosis on 2026-08-27.

The load-bearing test here is test_process_probe_persists_the_evidence: that is the exact
seam where both values used to be discarded.
"""
import asyncio
import sqlite3

import pytest

from monitor import db, main
from monitor.check import CheckResult
from monitor.state import MonitorState


@pytest.fixture
def conn(tmp_path):
    c = db.get_connection(str(tmp_path / "evidence.db"))
    db.init_db(c)
    yield c
    c.close()


def _checks(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM checks ORDER BY id")]


# --- the columns exist and persist ---------------------------------------------------

def test_append_check_persists_both_evidence_columns(conn):
    db.append_check(
        conn, ts="2026-08-29T12:00:00+00:00", ok=False, http_status=200, latency_ms=12.0,
        fail_reason="element_missing", browser_mode="headed-xvfb", layer="authed",
        burst_id=None, cycle_id="c1",
        page_url="https://www.teachersfcu.org/", screenshot_path="data/artifacts/x.png",
    )
    row = _checks(conn)[0]
    assert row["page_url"] == "https://www.teachersfcu.org/"
    assert row["screenshot_path"] == "data/artifacts/x.png"


def test_migration_is_additive_on_a_pre_existing_database(tmp_path):
    """Rule: migrations are additive with no backfill. A database written before these
    columns existed must gain them without losing or rewriting a single row."""
    path = str(tmp_path / "old.db")
    old = sqlite3.connect(path)
    old.execute("""CREATE TABLE checks (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, ok INTEGER NOT NULL,
        http_status INTEGER, latency_ms REAL, fail_reason TEXT)""")
    old.execute("INSERT INTO checks (ts, ok, fail_reason) VALUES ('2026-01-01T00:00:00+00:00', 0, 'timeout')")
    old.commit(); old.close()

    conn = db.get_connection(path)
    db.init_db(conn)

    rows = _checks(conn)
    assert len(rows) == 1, "migration must not drop rows"
    assert rows[0]["fail_reason"] == "timeout", "migration must not rewrite rows"
    assert rows[0]["page_url"] is None, "no backfill -- the value was never captured"
    assert rows[0]["screenshot_path"] is None
    conn.close()


# --- the seam where both values used to be lost --------------------------------------

def test_process_probe_persists_the_evidence(conn):
    """B35's actual defect. This probe never becomes an incident -- it is a single failure,
    nowhere near the DOWN threshold -- which before this change meant its screenshot path
    was discarded the moment _process_probe returned, leaving the image orphaned on disk."""
    result = CheckResult(
        ok=False, http_status=200, latency_ms=30.0, fail_reason="element_missing",
        screenshot_path="data/artifacts/20260829T120000-0400_authed_check_content.png",
        page_url="https://www.teachersfcu.org/online-banking-login",
        layer="authed",
    )
    asyncio.run(main._process_probe(
        conn, [], MonitorState(status="UP", since_ts=None), result,
        "2026-08-29T16:00:00+00:00", burst_id=None, track="auth", cycle_id="c1",
    ))

    row = _checks(conn)[0]
    assert row["screenshot_path"] == result.screenshot_path
    assert row["page_url"] == result.page_url
    assert conn.execute("SELECT COUNT(*) c FROM incidents").fetchone()["c"] == 0, (
        "no incident opened -- which is precisely why the path used to be lost"
    )


def test_burst_reprobes_also_record_their_evidence(conn):
    """The 2nd/3rd/4th probes of a burst are the bulk of the orphaned files: only the probe
    that trips DOWN ever reached open_incident()."""
    state = MonitorState(status="UP", since_ts=None)
    for i in range(3):
        state = asyncio.run(main._process_probe(
            conn, [], state,
            CheckResult(ok=False, http_status=None, latency_ms=1.0, fail_reason="element_missing",
                        screenshot_path=f"data/artifacts/burst_{i}.png",
                        page_url=f"https://example.org/{i}", layer="render"),
            f"2026-08-29T16:00:0{i}+00:00", burst_id="b1", cycle_id="c1",
        ))

    paths = [r["screenshot_path"] for r in _checks(conn)]
    assert paths == ["data/artifacts/burst_0.png", "data/artifacts/burst_1.png",
                     "data/artifacts/burst_2.png"], "every burst probe keeps its own image"


def test_a_passing_probe_records_no_evidence(conn):
    """No screenshot is taken on success, so both columns stay NULL rather than carrying a
    stale path from an earlier failure."""
    asyncio.run(main._process_probe(
        conn, [], MonitorState(status="UP", since_ts=None),
        CheckResult(ok=True, http_status=200, latency_ms=5.0, fail_reason=None, layer="render"),
        "2026-08-29T16:00:00+00:00", burst_id=None, cycle_id="c1",
    ))
    row = _checks(conn)[0]
    assert row["page_url"] is None and row["screenshot_path"] is None
