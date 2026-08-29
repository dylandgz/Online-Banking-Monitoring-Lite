"""Tests for the failure-path guarantees added to main.py/journey.py (the "P0" hardening).

These cover monitor/main.py, which had no automated coverage at all despite carrying the
Stage R orchestration -- and the three defects tested here were all found by review, not
by the suite, which is exactly why they are pinned now. Each test asserts an invariant
that a future refactor could plausibly break silently:

  1. a crashed cycle still writes an audit row, and writes it as a NON-paging verdict;
  2. a crashed cycle's row reuses the cycle_id its probe rows were tagged with;
  3. a login attempt is recorded even when the journey raises (Rule 5's budget is
     derived from that table, so an unrecorded attempt makes the budget unenforceable);
  4. an unexpected browser exception maps onto the closed fail_reason taxonomy.

No browser, no network: journey.run_journey / check.perform_check are monkeypatched, and
the DB is a real sqlite file in tmp_path so the schema and writes are genuinely exercised.
"""
import asyncio

import pytest

import config
from monitor import db, journey, main
from monitor.timeutil import now_iso


def _conn(tmp_path):
    conn = db.get_connection(str(tmp_path / "test.db"))
    db.init_db(conn)
    return conn


def _cycles(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM cycles ORDER BY ts")]


def _states(conn):
    return {r["track"]: dict(r) for r in conn.execute("SELECT * FROM state")}


# --- 1/2: the cycle-level self-health net ------------------------------------------

def test_crashed_cycle_writes_degraded_row_and_never_pages(tmp_path, monkeypatch):
    """A raising run_cycle must leave evidence, not a hole -- and must not page.

    Before the guard in guarded_cycle, the exception escaped into asyncio's
    "Task exception was never retrieved" and the minute vanished entirely.
    """
    conn = _conn(tmp_path)
    sent = []

    async def boom(*args, **kwargs):
        raise RuntimeError("simulated unmapped probe failure")

    monkeypatch.setattr(main, "run_cycle", boom)

    asyncio.run(main.guarded_cycle(conn, sent, asyncio.Lock(), auth_enabled=False))

    rows = _cycles(conn)
    assert len(rows) == 1, "a crashed cycle must still write exactly one cycles row"
    assert rows[0]["verdict"] == "DEGRADED", "must not be DOWN -- Rule 7 'only DOWN pages': DEGRADED never pages"
    assert rows[0]["fail_layer"] == "monitor"
    assert rows[0]["fail_reason"] == "internal_error"
    # Rule 15 "every probe writes a checks row": the row exists, but the layer columns are honestly NULL -- nothing was probed.
    assert rows[0]["pulse_ok"] is None
    assert rows[0]["render_ok"] is None
    assert rows[0]["authed_ok"] is None
    assert sent == [], "a monitor-internal error must never dispatch an alert"


def test_crashed_cycle_leaves_state_untouched(tmp_path, monkeypatch):
    """The DEGRADED row is written directly, not via apply_check, so neither track's
    state machine may move -- otherwise a monitor bug could accumulate confidence toward
    a DOWN page, the exact false positive this project ranks above everything else."""
    conn = _conn(tmp_path)
    before = _states(conn)

    async def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(main, "run_cycle", boom)
    asyncio.run(main.guarded_cycle(conn, [], asyncio.Lock(), auth_enabled=False))

    assert _states(conn) == before, "a crashed cycle must not advance any track's state"


def test_crashed_cycle_row_reuses_the_cycle_id_given_to_run_cycle(tmp_path, monkeypatch):
    """guarded_cycle owns the cycle_id and passes it in, so probe rows already written
    under it before the crash stay reachable from the cycles row (no orphaned evidence)."""
    conn = _conn(tmp_path)
    seen = {}

    async def crash_after_writing_a_probe(conn_, channels, auth_enabled, cycle_id=None):
        seen["cycle_id"] = cycle_id
        db.append_check(
            conn_, ts=now_iso(), ok=False, http_status=None, latency_ms=1.0,
            fail_reason="timeout", browser_mode="headless", layer="render",
            burst_id=None, cycle_id=cycle_id,
        )
        raise RuntimeError("crash after the probe row was committed")

    monkeypatch.setattr(main, "run_cycle", crash_after_writing_a_probe)
    asyncio.run(main.guarded_cycle(conn, [], asyncio.Lock(), auth_enabled=False))

    rows = _cycles(conn)
    assert len(rows) == 1
    assert seen["cycle_id"] is not None, "guarded_cycle must supply the cycle_id"
    assert rows[0]["cycle_id"] == seen["cycle_id"]

    checks = [dict(r) for r in conn.execute("SELECT cycle_id FROM checks")]
    assert len(checks) == 1
    assert checks[0]["cycle_id"] == rows[0]["cycle_id"], "probe evidence must not be orphaned"


def test_lock_is_released_after_a_crashed_cycle(tmp_path, monkeypatch):
    """The overlap guard must not latch shut on a crash -- otherwise one bad cycle
    silences the monitor forever ("skip cycle -- previous cycle still running")."""
    conn = _conn(tmp_path)
    lock = asyncio.Lock()

    async def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(main, "run_cycle", boom)
    asyncio.run(main.guarded_cycle(conn, [], lock, auth_enabled=False))

    assert not lock.locked()


# --- 3: Rule 5's ledger must be written even when the journey raises ---------------

def test_login_attempt_is_recorded_even_when_the_journey_raises(tmp_path, monkeypatch):
    """Rule 5's budget is computed from login_events. If a raising journey writes no
    row, the budget reads as untouched and the next cycle logs in again -- unbounded
    credentialed attempts against a real account. The finally: clause is what stops that.
    """
    conn = _conn(tmp_path)

    async def raising_journey(**kwargs):
        raise RuntimeError("unmapped failure mid-journey")

    monkeypatch.setattr(journey, "run_journey", raising_journey)
    monkeypatch.setattr(config, "SESSION_STATE_PATH", str(tmp_path / "s.json"))

    with pytest.raises(RuntimeError):
        asyncio.run(main._run_full_login(conn, should_logout=False))

    events = [dict(r) for r in conn.execute("SELECT * FROM login_events")]
    assert len(events) == 1, "the attempt must be on the ledger even though it raised"
    assert events[0]["ok"] == 0
    assert events[0]["reason"] == "internal_error"


def test_recorded_failed_login_actually_consumes_the_budget(tmp_path, monkeypatch):
    """The point of the row: _login_budget_allows must now refuse the next attempt,
    which is what converts 'recorded' into 'the loop cannot run away'."""
    conn = _conn(tmp_path)

    async def raising_journey(**kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(journey, "run_journey", raising_journey)
    monkeypatch.setattr(config, "SESSION_STATE_PATH", str(tmp_path / "s.json"))
    monkeypatch.setattr(config, "LOGIN_INTERVAL_S", 1800)

    assert main._login_budget_allows(conn) is True
    with pytest.raises(RuntimeError):
        asyncio.run(main._run_full_login(conn, should_logout=False))
    assert main._login_budget_allows(conn) is False, "the failed attempt must close the gap"


def test_successful_login_is_recorded_once(tmp_path, monkeypatch):
    """Regression guard on the try/finally rewrite: the success path must still write
    exactly one row, not two (a duplicate would double-charge the daily cap)."""
    conn = _conn(tmp_path)
    from monitor.check import CheckResult

    async def ok_journey(**kwargs):
        return CheckResult(ok=True, http_status=None, latency_ms=12.5, fail_reason=None, layer="authed")

    monkeypatch.setattr(journey, "run_journey", ok_journey)
    monkeypatch.setattr(config, "SESSION_STATE_PATH", str(tmp_path / "s.json"))

    result = asyncio.run(main._run_full_login(conn, should_logout=False))

    assert result.ok is True
    events = [dict(r) for r in conn.execute("SELECT * FROM login_events")]
    assert len(events) == 1
    assert events[0]["ok"] == 1
    assert events[0]["reason"] is None


# --- 4: unexpected browser exceptions map onto the closed taxonomy -----------------

def test_unexpected_fail_reason_maps_into_the_closed_taxonomy():
    """Whatever escapes must be a reason state.classify() already understands, and must
    NOT be a config-class reason (which would latch CONFIG_ERROR and halt the track on a
    transient browser hiccup) nor a hard-weight one (which would page twice as fast)."""
    from patchright.async_api import Error as PatchrightError, TimeoutError as PatchrightTimeoutError
    from monitor.state import classify

    assert journey.unexpected_fail_reason(PatchrightTimeoutError("t")) == "timeout"
    assert journey.unexpected_fail_reason(PatchrightError("e")) == "nav_error"

    for exc in (PatchrightTimeoutError("t"), PatchrightError("e")):
        cls, weight = classify(journey.unexpected_fail_reason(exc))
        assert cls == "soft", "an unexplained browser error is ambiguous evidence, not hard"
        assert weight == 1


def test_malformed_totp_secret_raises_a_type_submit_totp_catches():
    """A mistyped TOTP_SECRET makes pyotp raise binascii.Error from deep inside the probe.

    submit_totp guards the code-generation call with `except (ValueError, TypeError)` and
    returns "mfa_failed" (-> CONFIG_ERROR, a human must fix the .env) instead of letting
    it escape. Driving submit_totp itself would need a live patchright page and expect(),
    so this pins the two halves of that contract that are actually unit-testable: the
    exception really is raised, and its type really is inside the guarded tuple.
    """
    with pytest.raises((ValueError, TypeError)) as excinfo:
        asyncio.run(journey.get_fresh_totp_code("not!valid!base32"))

    # binascii.Error subclasses ValueError; assert the relationship the guard relies on
    # rather than the concrete class, which is a pyotp/stdlib implementation detail.
    assert isinstance(excinfo.value, (ValueError, TypeError))


def test_failing_screenshot_returns_none_instead_of_raising(tmp_path):
    """capture_masked_screenshot must never raise. It runs from _fail(), so an exception
    here would destroy the CheckResult describing the actual failure and replace it with
    a crash -- and the likeliest trigger is a page that timed out mid-navigation, i.e.
    precisely the most important failure to report."""
    from patchright.async_api import Error as PatchrightError

    class _DeadPage:
        """Stands in for a page whose target crashed or is mid-navigation."""
        async def screenshot(self, **_kwargs):
            raise PatchrightError("Target page, context or browser has been closed")

    path = asyncio.run(journey.capture_masked_screenshot(
        _DeadPage(), str(tmp_path / "artifacts"), "authed_check", "nav_error",
        mask_patterns=[], masking_enabled=True,
    ))

    assert path is None


def test_screenshot_returns_path_on_success(tmp_path):
    """Companion to the above: the happy path must still return a usable path, so the
    guard can't be 'fixed' by swallowing everything."""
    written = {}

    class _LivePage:
        async def screenshot(self, *, path, mask, full_page):
            written["path"] = path
            written["full_page"] = full_page

    path = asyncio.run(journey.capture_masked_screenshot(
        _LivePage(), str(tmp_path / "artifacts"), "authed_check", "session_expired",
        mask_patterns=[], masking_enabled=True,
    ))

    assert path is not None
    assert path == written["path"]
    assert path.endswith("_authed_check_session_expired.png")
