"""B10: the second authed marker, inside the banking iframe.

These pin the four defects found reviewing that change. Three of the four were in error
handling -- what happens when the page misbehaves -- which is why the happy path passed
first time and the suite stayed green while the code was wrong.

No browser: the page/frame objects are fakes, because what is under test is the control
flow around them, not Playwright.
"""
import asyncio
import fnmatch
import time
from unittest.mock import patch

import pytest

from monitor import journey
from patchright.async_api import Error as PatchrightError


class FakeFrame:
    def __init__(self, url, detached=False):
        self._url = url
        self.detached = detached

    @property
    def url(self):
        if self.detached:
            raise PatchrightError("frame was detached")
        return self._url


class FakePage:
    def __init__(self, frames, raises=False):
        self._frames = frames
        self._raises = raises

    @property
    def frames(self):
        if self._raises:
            raise PatchrightError("page closed")
        return self._frames


GLOB = "**/nxg-olb/**"
LIVE = "https://www.teachersfcuonline.org/nxg-olb/live/#home-page"


# --- FIX 4: deterministic matching on every platform ---------------------------------

def test_frame_glob_is_case_sensitive_on_every_platform():
    """fnmatch folds case through os.path.normcase -- identity on POSIX, lowercasing on
    Windows -- so the same config matched on one OS and not another. A URL is not a path.

    This drives find_authed_frame itself under a simulated Windows normcase. An earlier
    version of this test called fnmatch directly and therefore passed against the buggy
    code, proving nothing."""
    upper = "https://www.teachersfcuonline.org/NXG-OLB/live/#home-page"
    page = FakePage([FakeFrame(upper)])

    with patch("os.path.normcase", lambda s: s.lower()):        # pretend to be Windows
        fnmatch._compile_pattern.cache_clear()                  # normcase is cached into it
        try:
            frame = asyncio.run(journey.find_authed_frame(page, GLOB, 500))
        finally:
            fnmatch._compile_pattern.cache_clear()

    assert frame is None, (
        "an upper-case frame URL must NOT match a lower-case glob, on any platform. "
        "The old fnmatch call folded the case away on Windows only, so the monitor "
        "would have behaved differently there than on the mac it was configured on."
    )


def test_the_real_frame_url_still_matches():
    assert fnmatch.fnmatchcase(LIVE, GLOB)


# --- FIX 2: a frame that detaches must not raise out of the search --------------------

def test_a_detached_frame_is_skipped_not_raised():
    """The banking app redraws itself, so a frame can detach between being listed and being
    read. That raises PatchrightError, which is NOT an AssertionError -- so before the guard
    it escaped every classify_* handler and surfaced as a generic nav_error."""
    page = FakePage([FakeFrame("about:blank", detached=True), FakeFrame(LIVE)])
    frame = asyncio.run(journey.find_authed_frame(page, GLOB, 2000))
    assert frame is not None and frame.url == LIVE, "the detached frame is skipped, not fatal"


def test_a_closed_page_returns_none_rather_than_raising():
    page = FakePage([], raises=True)
    assert asyncio.run(journey.find_authed_frame(page, GLOB, 500)) is None


def test_no_matching_frame_returns_none_within_budget():
    page = FakePage([FakeFrame("https://example.org/other")])
    t0 = time.monotonic()
    assert asyncio.run(journey.find_authed_frame(page, GLOB, 1000)) is None
    elapsed = (time.monotonic() - t0) * 1000
    assert 900 < elapsed < 2500, f"must honour its budget, took {elapsed:.0f}ms"


def test_a_present_frame_is_found_immediately():
    page = FakePage([FakeFrame(LIVE)])
    t0 = time.monotonic()
    assert asyncio.run(journey.find_authed_frame(page, GLOB, 25000)) is not None
    assert (time.monotonic() - t0) < 0.5, "must not wait when the frame is already there"


# --- FIX 1: the frame phase shares ONE budget -----------------------------------------

def test_the_frame_phase_does_not_get_a_fresh_timeout_per_step(monkeypatch):
    """Each step used to get its own challenge_timeout_ms -- find the frame, then assert
    inside it -- so a failing probe cost up to 3x what it should. Measured live at the time:
    35.4s for a missing marker against 7.8s healthy. Not a local cost: burst length is
    BURST_PROBES x (gap + probe cost), so it made one burst run nearly 7 minutes, which in
    the log is indistinguishable from the B42 hang.

    Behavioural: the frame is found only after most of the budget has elapsed, so the assert
    that follows must be handed the REMAINDER, not a fresh copy."""
    handed = {}

    class FakeExpect:
        def __init__(self, loc): self.loc = loc
        async def to_be_visible(self, timeout):
            if self.loc == "shell":
                return                      # shell marker passes
            handed["frame_timeout"] = timeout
            raise AssertionError("frame marker not visible")

    slow_frame = {"attached_at": time.monotonic() + 1.2}

    class LateFakePage:
        @property
        def frames(self):
            return [FakeFrame(LIVE)] if time.monotonic() >= slow_frame["attached_at"] else []

    monkeypatch.setattr(journey, "expect", FakeExpect)
    monkeypatch.setattr(journey, "authed_marker", lambda *a, **k: "shell")
    monkeypatch.setattr(journey, "frame_marker", lambda *a, **k: "frame")
    monkeypatch.setattr(journey, "error_banner", lambda *a, **k: "banner")

    BUDGET = 2000
    result = asyncio.run(journey.classify_authed(
        LateFakePage(), None, "link", "Logout", "err", BUDGET,
        frame_url=GLOB, frame_role="heading", frame_name="Internal Accounts"))

    assert result == "content_missing"
    assert handed["frame_timeout"] < BUDGET, (
        f"the assert was handed {handed['frame_timeout']:.0f}ms of a {BUDGET}ms phase budget "
        "after ~1.2s was already spent finding the frame -- it must get the remainder, "
        "not a fresh full timeout"
    )


# --- FIX 3: run_authed_check must fail CLOSED -----------------------------------------

def test_an_unrecognised_classification_is_reported_as_a_failure(monkeypatch):
    """run_authed_check handled two outcomes with explicit ifs and then fell through to
    `return ok=True`. A value it did not recognise -- a fourth outcome added later -- was
    therefore reported as THE PLATFORM BEING UP. On a monitor that is the one direction a
    bug must never fail in: a false alarm annoys, a false all-clear means you never find out.

    Behavioural: classify_authed is forced to return a value nothing handles."""
    from contextlib import asynccontextmanager

    class FakeResponse:
        status = 200

    class FakePageForCheck:
        url = LIVE
        frames = [FakeFrame(LIVE)]
        async def goto(self, *a, **k): return FakeResponse()
        async def close(self): pass
        async def wait_for_load_state(self, *a, **k): pass
        async def screenshot(self, **k): pass
        def get_by_text(self, *a, **k): return self

    class FakeContext:
        async def new_page(self): return FakePageForCheck()

    @asynccontextmanager
    async def fake_browser(*a, **k):
        yield FakeContext()

    monkeypatch.setattr(journey, "open_journey_browser", fake_browser)
    monkeypatch.setattr(journey, "capture_masked_screenshot", lambda *a, **k: _none())
    async def _none(): return None
    monkeypatch.setattr(journey, "capture_masked_screenshot",
                        lambda *a, **k: asyncio.sleep(0, result=None))
    async def unknown(*a, **k): return "a_fourth_outcome_nobody_handled"
    monkeypatch.setattr(journey, "classify_authed", unknown)

    r = asyncio.run(journey.run_authed_check(
        authed_url=LIVE, authed_text=None, authed_role="link", authed_name="Logout",
        error_banner_text="err", browser_channel="chrome", session_state_path="/tmp/x.json",
        browser_timeout_ms=5000, challenge_timeout_ms=5000, artifacts_dir="/tmp",
        mask_patterns=[], masking_enabled=False, frame_url=GLOB,
        frame_role="heading", frame_name="Internal Accounts"))

    assert r.ok is False, "an unrecognised classification must NOT be reported as UP"
    assert r.fail_reason == "element_missing"


def test_run_journey_also_fails_closed():
    import inspect
    src = inspect.getsource(journey.run_journey)
    assert 'if authed_result != "authed":' in src, (
        "run_journey must treat anything that is not an explicit success as a failure"
    )
