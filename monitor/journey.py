"""Stage 6/8: authenticated sign-in journey -- locators, TOTP, the submit_*/classify_*
step functions, run_journey() (full login -> TOTP -> authed -> optional logout), and
run_authed_check() (Stage 8: a cheap check reusing a saved session, zero logins
consumed). Ported from the signin_lab sandbox handoff (MONITOR_LITE_HANDOFF.md) and
converted from patchright's sync API to its async API to match this project's asyncio
architecture (Rule 8). run_journey() is new -- the handoff's own run.py (the only place
this sequencing previously lived) was explicitly not shipped because it assumed a human
was watching (see handoff section 3); this rebuilds that control flow without any
print()/input() calls, reporting through CheckResult like every other check layer.

Browser automation uses patchright (not vanilla Playwright), always headed under xvfb
(CLAUDE.md v3.2/v3.3, named exceptions to Rules 12/14). Session persistence uses a
single storageState file (v3.4/v3.6/Stage 8) -- the earlier persistent-profile-directory
approach (USER_DATA_DIR) has been retired, per Stage 8's acceptance criteria: it carried
cf_clearance just as effectively (confirmed in the signin_lab sandbox's own testing) and
this way there's exactly one persisted, chmod-600 secret file for the whole journey, not
two different mechanisms.
"""
from __future__ import annotations

import asyncio
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator, Awaitable, Callable, Optional, Sequence

import pyotp
from patchright.async_api import (
    BrowserContext,
    Error as PatchrightError,
    Locator,
    Page,
    TimeoutError as PatchrightTimeoutError,
    async_playwright,
    expect,
)

from monitor.check import CheckResult
from monitor.session import save_session_state

TOTP_INTERVAL_S = 30


# --- browser lifecycle -------------------------------------------------------

@asynccontextmanager
async def open_journey_browser(
    browser_channel: str,
    browser_timeout_ms: int,
    storage_state_path: Optional[str] = None,
) -> AsyncIterator[BrowserContext]:
    """Regular (non-persistent) context, seeded from a saved storageState file if one
    exists -- carries cf_clearance and any still-valid login session cookies into a
    fresh browser, without the overhead/secrecy surface of a whole profile directory.
    Always headed (v3.3): patchright was confirmed unable to clear the real target's
    Cloudflare challenge headless, so this runs under xvfb in production rather than
    honoring config.HEADLESS."""
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            channel=browser_channel, headless=False, timeout=browser_timeout_ms
        )
        context_kwargs: dict = {"no_viewport": True}
        if storage_state_path and Path(storage_state_path).exists():
            context_kwargs["storage_state"] = storage_state_path
        context = await browser.new_context(**context_kwargs)
        try:
            yield context
        finally:
            await context.close()
            await browser.close()


# --- TOTP --------------------------------------------------------------------

def _totp_seconds_remaining(now: Optional[float] = None) -> int:
    now = time.time() if now is None else now
    return TOTP_INTERVAL_S - int(now) % TOTP_INTERVAL_S


async def get_fresh_totp_code(secret: str, min_remaining_seconds: int = 5) -> str:
    """Waits out the TOTP window if it's about to roll over, so the code submitted stays
    valid for however long the page takes to accept it. A bounded wait on a known
    wall-clock deadline, not a guess at page-render timing."""
    remaining = _totp_seconds_remaining()
    if remaining < min_remaining_seconds:
        await asyncio.sleep(remaining + 0.5)
    return pyotp.TOTP(secret).now()


# --- locators ------------------------------------------------------------
# All site-specific -- rediscover every one of these against the real target before
# trusting it (handoff section 6). Kept here as named factories per Rule 3.

def username_field(page: Page) -> Locator:
    # [v3.5] Positional exception: role/text/label locators were tried against the real
    # target and none worked (a <label for="..."> pointing at a nonexistent id makes
    # name-based lookup return zero matches). Scoped to exactly these two fields.
    return page.get_by_role("textbox").nth(0)


def password_field(page: Page) -> Locator:
    return page.get_by_role("textbox").nth(1)


def submit_button(page: Page) -> Locator:
    return page.get_by_role("button", name="Login", exact=True)


def error_banner(page: Page, text: str) -> Locator:
    return page.get_by_text(text)


def mfa_heading(page: Page) -> Locator:
    # [2026-08-25] The step-up flow has TWO screens, each with its own heading, and this
    # locator is what tells classify_after_submit "we reached MFA" and classify_after_totp
    # "we left MFA" -- so it has to mean the whole step, not one screen of it. "Login
    # Security" is the factor picker (Text / Call / Authenticator rows, live-confirmed
    # from the failure screenshot); "Verify Information" is the code-entry screen that
    # follows. Matching only one of them is exactly the bug that fell through to the
    # bot_challenge fallback and parked the auth track in CONFIG_ERROR.
    return page.get_by_role("heading", name="Login Security", exact=True).or_(
        page.get_by_role("heading", name="Verify Information", exact=True)
    )


def enter_code_button(page: Page) -> Locator:
    return page.get_by_role("button", name="send code to authenticator")


def totp_field(page: Page) -> Locator:
    # The picker's Authenticator button is confirmed to be named "Enter code"
    # (enter_code_button, above -- live screenshot). The accessible name of the FIELD
    # revealed after clicking it has never been captured, and the SMS variant of this
    # screen labels its own field "Verification code" -- so a single guessed string here
    # would fail the entire login on a wrong word. Accept the plausible wordings instead
    # (still Rule 3: get_by_role only, no CSS/XPath). or_() is a union, so an unmatched
    # alternative costs nothing. Collapse this to the one real name once a live run
    # confirms it.
    return (
        page.get_by_role("textbox", name="Enter code", exact=True)
        .or_(page.get_by_role("textbox", name="Verification code", exact=True))
        .or_(page.get_by_role("textbox", name="Authenticator code", exact=True))
    )


def authed_marker(page: Page, *, text: Optional[str], role: Optional[str], name: Optional[str]) -> Locator:
    if role and name:
        return page.get_by_role(role, name=name, exact=True)
    return page.get_by_text(text)


def logout_link(page: Page) -> Locator:
    return page.get_by_role("link", name="Logout", exact=True)


# Deliberately not ported: the sandbox's register_device_button/public_device_button/
# dismiss_device_prompt. They were built on a wrong assumption (that the post-TOTP
# screen was a device-trust prompt; it's actually a promotional offer -- handoff
# section 7). Resolving what that screen really is, against the real target, is an
# open Stage 6 task, not something to carry over guessed-at.


# --- submit / classify pairs --------------------------------------------------
# [v3.5] "unknown" must not ship. Every fallback below resolves to a specific reason
# from the closed taxonomy, with a comment explaining the judgment call -- these are
# best-effort mappings made without a live site to drill against (handoff section 8)
# and should be revisited once Stage 6's real-target drilling (integration checklist
# step 7) confirms or corrects them.

async def _settle(page: Page, timeout_ms: int) -> None:
    """Best-effort 'let the destination render' pause between an action and the
    classify_* step that follows it. Uses domcontentloaded, not networkidle -- an
    authenticated banking page can run background network chatter (keepalive pings,
    analytics, chat widgets) indefinitely, so 'the network goes quiet' may never
    happen even though the page is genuinely ready. Confirmed live: logout() hung a
    full 30s on networkidle despite "load" having already fired. The classify_* call
    that follows every use of this is the real source of truth (it polls for the
    actual expected element), so a timeout here is swallowed, not fatal."""
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
    except (PatchrightTimeoutError, PatchrightError):
        pass


async def submit_credentials(page: Page, login_user: str, login_password: str, browser_timeout_ms: int) -> None:
    await username_field(page).fill(login_user)
    await password_field(page).fill(login_password)
    await submit_button(page).click()
    await _settle(page, browser_timeout_ms)


async def classify_after_submit(page: Page, error_banner_text: str, challenge_timeout_ms: int) -> str:
    """Returns "mfa", "auth_rejected", or "bot_challenge"."""
    mfa_marker = mfa_heading(page)
    error_marker = error_banner(page, error_banner_text)

    try:
        await expect(mfa_marker.or_(error_marker)).to_be_visible(timeout=challenge_timeout_ms)
    except AssertionError:
        # Neither marker showed up. Per the handoff, this is exactly what
        # bot_challenge/auth_unavailable/rate_limited look like undifferentiated.
        # bot_challenge is the closest single reason given this is the one check
        # layer deliberately built to contend with a Cloudflare challenge -- not
        # confirmed correct until drilled against the real target.
        return "bot_challenge"

    if await mfa_marker.is_visible():
        return "mfa"
    if await error_marker.is_visible():
        return "auth_rejected"
    return "bot_challenge"


async def submit_totp(
    page: Page, totp_secret: Optional[str], challenge_timeout_ms: int, browser_timeout_ms: int,
    code_provider: Optional[Callable[[], Awaitable[str]]] = None,
) -> Optional[str]:
    """Returns None on success, or a fail_reason if the code-entry field never appears.

    code_provider, if given, is awaited for the code instead of generating one from
    totp_secret -- the temporary manual-entry path for testing without a captured
    TOTP_SECRET (a human reads the code off their phone's authenticator app). Not used
    by the automated production loop (main.py never passes one), which still requires a
    real totp_secret -- unattended monitoring can't pause for a human to type a code."""
    await enter_code_button(page).click()
    field = totp_field(page)
    try:
        await expect(field).to_be_visible(timeout=challenge_timeout_ms)
    except AssertionError:
        # handoff section 5: this was an unguarded wait that crashed the whole script.
        # element_missing is the literal, honest description -- the expected field
        # genuinely never appeared.
        return "element_missing"

    # No way to answer the code prompt (no human providing one, no captured secret) --
    # this is a config problem (a human needs to set TOTP_SECRET), not evidence about
    # the platform. Reported the same way any other "MFA could not be completed" case
    # is (mfa_failed -> CONFIG_ERROR, Rule 10), instead of crashing pyotp.TOTP(None)
    # deep in a background task where it would silently drop the whole probe (Rule 6
    # requires every probe to still write a row).
    if not code_provider and not totp_secret:
        return "mfa_failed"

    code = await code_provider() if code_provider else await get_fresh_totp_code(totp_secret)
    await field.fill(code)

    # Some OTP inputs auto-submit on the input event fill() dispatches; only press
    # Enter if the field is still there to receive it.
    if await field.count() and await field.is_visible():
        await field.press("Enter")

    await _settle(page, browser_timeout_ms)
    return None


async def classify_after_totp(page: Page, challenge_timeout_ms: int, browser_timeout_ms: int) -> str:
    """Returns "success" or "mfa_failed"."""
    heading = mfa_heading(page)
    try:
        await expect(heading).to_be_hidden(timeout=challenge_timeout_ms)
        # The heading disappearing only means the old page started unloading, not that
        # the destination has finished rendering -- let it settle before reading state.
        await _settle(page, browser_timeout_ms)
        return "success"
    except AssertionError:
        pass

    # Full timeout elapsed with the heading still visible -- MFA did not succeed,
    # regardless of whether a specific rejection message is findable. Ignore
    # "Verifying..." (may just be mid-retry) and hidden alerts (not what the user sees).
    alerts = page.get_by_role("alert")
    count = await alerts.count()
    for i in range(count):
        el = alerts.nth(i)
        if await el.is_visible():
            text = (await el.text_content()) or ""
            if text.strip() and "verifying" not in text.lower():
                return "mfa_failed"
    return "mfa_failed"


async def classify_authed(page: Page, authed_text: Optional[str], authed_role: Optional[str],
                           authed_name: Optional[str], error_banner_text: str,
                           challenge_timeout_ms: int) -> str:
    """Returns "authed" or "element_missing"."""
    marker = authed_marker(page, text=authed_text, role=authed_role, name=authed_name)
    error_marker = error_banner(page, error_banner_text)

    try:
        await expect(marker).to_be_visible(timeout=challenge_timeout_ms)
    except AssertionError:
        return "element_missing"

    # CLAUDE.md's Stage 6 spec: authed requires the marker visible AND the error banner
    # absent. If both are visible at once, the overall assertion still fails -- reported
    # the same way as the marker missing, since from the check's perspective a valid
    # authed state wasn't established either way. Never observed in the sandbox; revisit
    # if a real drill shows this needs its own distinct reason instead.
    if await error_marker.is_visible():
        return "element_missing"

    return "authed"


async def logout(page: Page, browser_timeout_ms: int) -> None:
    await logout_link(page).click()
    await _settle(page, browser_timeout_ms)


async def classify_logout(page: Page, authed_text: Optional[str], authed_role: Optional[str],
                           authed_name: Optional[str], challenge_timeout_ms: int) -> str:
    """Returns "logged_out" or "logout_failed"."""
    marker = authed_marker(page, text=authed_text, role=authed_role, name=authed_name)
    try:
        await expect(marker).to_be_hidden(timeout=challenge_timeout_ms)
        return "logged_out"
    except AssertionError:
        return "logout_failed"


# --- masked screenshots --------------------------------------------------------
# Rule 5: post-login screenshots must mask PII locators. Masking works by regex pattern
# over visible text, not by label -- a label locator only covers the label element
# itself, not the sibling element holding the actual (dynamic) value.

async def capture_masked_screenshot(
    page: Page,
    artifacts_dir: str,
    step: str,
    fail_reason: str,
    mask_patterns: Sequence[str],
    masking_enabled: bool,
) -> str:
    Path(artifacts_dir).mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = str(Path(artifacts_dir) / f"{ts}_{step}_{fail_reason}.png")

    if masking_enabled:
        mask_locators = [page.get_by_text(re.compile(pattern)) for pattern in mask_patterns]
    else:
        # Explicit opt-out only, never a silent default -- printed loudly so it's never
        # in effect against a real account by accident.
        print("WARNING: masking disabled -- screenshot is NOT masked.", flush=True)
        mask_locators = []

    await page.screenshot(path=path, mask=mask_locators, full_page=True)
    return path


# --- orchestrator --------------------------------------------------------------
# The shape this follows (handoff section 3):
#   navigate -> (nav_error)
#   wait for login form -> (element_missing)
#   submit credentials -> classify_after_submit -> mfa | auth_rejected | bot_challenge
#     if mfa:
#       submit_totp -> classify_after_totp -> success | mfa_failed
#       if success:
#         classify_authed -> authed | element_missing
#         if authed:
#           logout -> classify_logout -> logged_out | logout_failed
#
# Every non-success arrow becomes one specific fail_reason, reported the same way every
# other check layer reports failure: a CheckResult, no I/O beyond the screenshot.
#
# layer="render" for anything that fails before credentials are ever submitted (only the
# login page's render was actually exercised); layer="authed" from submit_credentials
# onward (the authed layer was engaged, pass or fail) -- matches CheckResult.layer's
# meaning of "the strongest layer this probe actually evaluated," used elsewhere in
# check.py. [v3.8] "authed" is the layer name; "auth" remains the track name (state/
# incidents/login_events) -- the two are spelled differently on purpose so log lines
# and dashboard columns are never ambiguous about which one they mean.

async def _fail(
    page: Page,
    layer: str,
    fail_reason: str,
    step: str,
    latency_ms: float,
    artifacts_dir: str,
    mask_patterns: Sequence[str],
    masking_enabled: bool,
) -> CheckResult:
    screenshot_path = await capture_masked_screenshot(
        page, artifacts_dir, step, fail_reason, mask_patterns, masking_enabled
    )
    return CheckResult(
        ok=False,
        http_status=None,
        latency_ms=latency_ms,
        fail_reason=fail_reason,
        screenshot_path=screenshot_path,
        layer=layer,
    )


async def run_journey(
    login_url: str,
    login_user: str,
    login_password: str,
    totp_secret: Optional[str],
    error_banner_text: str,
    authed_text: Optional[str],
    authed_role: Optional[str],
    authed_name: Optional[str],
    browser_channel: str,
    browser_timeout_ms: int,
    challenge_timeout_ms: int,
    artifacts_dir: str,
    mask_patterns: Sequence[str],
    masking_enabled: bool,
    session_state_path: Optional[str] = None,
    seed_from_existing_session: bool = False,
    should_logout: bool = True,
    totp_code_provider: Optional[Callable[[], Awaitable[str]]] = None,
    manual_mfa_pause: Optional[Callable[[], Awaitable[None]]] = None,
) -> CheckResult:
    """Runs the full login -> TOTP -> authed-assertion -> (optional) logout journey
    once and reports the outcome as a CheckResult, exactly like check.py's probes.
    Never retries anything itself (Rule 10 is the caller's job -- this function just
    reports what happened on the one attempt it made).

    session_state_path, if given, is where the fresh storageState is saved immediately
    after "authed" is confirmed -- before logout, since logging out would invalidate the
    very session just saved.

    seed_from_existing_session [fix, 2026-08-11]: whether to ALSO seed this fresh login's
    browser from any existing file at session_state_path before navigating. Defaults to
    False -- live testing found that seeding a genuinely fresh credentialed login with a
    stale/partially-expired old session caused the bank's server to redirect away from
    the login form entirely (to the public marketing homepage) instead of showing it,
    which then misreported as a generic element_missing/render failure with no obvious
    cause. The cost of not seeding: this attempt won't carry over a still-valid
    cf_clearance cookie, so it may need to re-clear Cloudflare's challenge from scratch --
    patchright has done so reliably except one observed non-deterministic miss (see
    CLAUDE.md v3.2 / PROGRESS.md 2026-08-11). Neither main.py's recovery-login path nor
    the manual drill script opt into seeding.

    should_logout=False skips the logout step entirely (used by the Stage 8 automated
    refresh path, which wants to keep the session alive for reuse); should_logout=True
    (default) is Stage 6's acceptance-test behavior (prove logout works), used by the
    manual drill script.

    totp_code_provider is a temporary manual-entry escape hatch for testing without a
    captured TOTP_SECRET -- see submit_totp()'s docstring. manual_mfa_pause is a further
    temporary escape hatch for an MFA factor this codebase doesn't automate at all (e.g.
    a phone-call code): when given, submit_totp()/classify_after_totp() are skipped
    entirely -- a human handles the whole MFA step by hand in the visible browser, and
    this just awaits their signal that it's done before checking classify_authed().
    Neither is ever passed by main.py's automated loop."""
    start = time.monotonic()

    seed_path = session_state_path if seed_from_existing_session else None
    async with open_journey_browser(browser_channel, browser_timeout_ms, seed_path) as context:
        page = await context.new_page()
        try:
            try:
                await page.goto(login_url, timeout=browser_timeout_ms)
            except (PatchrightTimeoutError, PatchrightError):
                latency_ms = (time.monotonic() - start) * 1000
                return await _fail(page, "render", "nav_error", "navigate", latency_ms,
                                    artifacts_dir, mask_patterns, masking_enabled)

            # "Wait for real content" (handoff's diagram) is folded into waiting for the
            # login form itself -- there's no site-specific "real content" locator yet
            # (deferred, like everything in locators.py), and wait_for(state="visible")
            # already retries past a blank mid-navigation page rather than reading it
            # instantly, which is the actual bug that step guards against.
            try:
                await username_field(page).first.wait_for(state="visible", timeout=browser_timeout_ms)
            except (PatchrightTimeoutError, PatchrightError):
                latency_ms = (time.monotonic() - start) * 1000
                return await _fail(page, "render", "element_missing", "login_form", latency_ms,
                                    artifacts_dir, mask_patterns, masking_enabled)

            await submit_credentials(page, login_user, login_password, browser_timeout_ms)
            after_submit = await classify_after_submit(page, error_banner_text, challenge_timeout_ms)

            if after_submit in ("auth_rejected", "bot_challenge"):
                latency_ms = (time.monotonic() - start) * 1000
                return await _fail(page, "authed", after_submit, "after_submit", latency_ms,
                                    artifacts_dir, mask_patterns, masking_enabled)

            # after_submit == "mfa"
            if manual_mfa_pause:
                # Human handles the entire MFA step (any factor) directly in the
                # browser -- nothing here clicks "Enter code" or touches a code field.
                await manual_mfa_pause()
            else:
                totp_fail_reason = await submit_totp(
                    page, totp_secret, challenge_timeout_ms, browser_timeout_ms, code_provider=totp_code_provider
                )
                if totp_fail_reason:
                    latency_ms = (time.monotonic() - start) * 1000
                    return await _fail(page, "authed", totp_fail_reason, "submit_totp", latency_ms,
                                        artifacts_dir, mask_patterns, masking_enabled)

                after_totp = await classify_after_totp(page, challenge_timeout_ms, browser_timeout_ms)
                if after_totp == "mfa_failed":
                    latency_ms = (time.monotonic() - start) * 1000
                    return await _fail(page, "authed", "mfa_failed", "after_totp", latency_ms,
                                        artifacts_dir, mask_patterns, masking_enabled)

            authed_result = await classify_authed(
                page, authed_text, authed_role, authed_name, error_banner_text, challenge_timeout_ms
            )
            if authed_result == "element_missing":
                latency_ms = (time.monotonic() - start) * 1000
                return await _fail(page, "authed", "element_missing", "authed", latency_ms,
                                    artifacts_dir, mask_patterns, masking_enabled)

            # Save before logout, not after -- logging out invalidates the session
            # server-side, which would make a post-logout save useless for reuse.
            if session_state_path:
                await save_session_state(context, session_state_path)

            if not should_logout:
                latency_ms = (time.monotonic() - start) * 1000
                return CheckResult(ok=True, http_status=None, latency_ms=latency_ms, fail_reason=None, layer="authed")

            await logout(page, browser_timeout_ms)
            logout_result = await classify_logout(
                page, authed_text, authed_role, authed_name, challenge_timeout_ms
            )
            latency_ms = (time.monotonic() - start) * 1000
            if logout_result == "logout_failed":
                return await _fail(page, "authed", "logout_failed", "logout", latency_ms,
                                    artifacts_dir, mask_patterns, masking_enabled)

            return CheckResult(ok=True, http_status=None, latency_ms=latency_ms, fail_reason=None, layer="authed")
        finally:
            await page.close()


async def run_authed_check(
    authed_url: str,
    authed_text: Optional[str],
    authed_role: Optional[str],
    authed_name: Optional[str],
    error_banner_text: str,
    browser_channel: str,
    session_state_path: str,
    browser_timeout_ms: int,
    challenge_timeout_ms: int,
    artifacts_dir: str,
    mask_patterns: Sequence[str],
    masking_enabled: bool,
) -> CheckResult:
    """[v3.8 / Stage R] A cheap check reusing a saved session -- no credentials, no TOTP,
    zero logins consumed. Navigates DIRECTLY to authed_url (the authenticated home route),
    never to the login URL hoping for a post-login redirect: the login route and the
    authed-home route are different paths with different dependencies (auth service, MFA
    backend, login UI bundle, Cloudflare challenge vs. just session-cookie validation), so
    testing the home route directly gives independent evidence from the render check,
    exactly what CLAUDE.md's Rule 16 wants (a login-route outage can leave existing
    sessions still served, and that distinction belongs in the incident record).

    A failure to even navigate/load the page (nav_error) is real platform evidence, same
    as any other reachability failure -- it's not a session problem. Only "the page loaded
    but the authed marker isn't there" maps to session_expired, since that's the actual
    signature of an expired/invalid session (most often a redirect back to the login
    page)."""
    start = time.monotonic()

    async with open_journey_browser(browser_channel, browser_timeout_ms, session_state_path) as context:
        page = await context.new_page()
        try:
            try:
                await page.goto(authed_url, timeout=browser_timeout_ms)
            except (PatchrightTimeoutError, PatchrightError):
                latency_ms = (time.monotonic() - start) * 1000
                return await _fail(page, "authed", "nav_error", "authed_check_nav", latency_ms,
                                    artifacts_dir, mask_patterns, masking_enabled)

            await _settle(page, browser_timeout_ms)
            authed_result = await classify_authed(
                page, authed_text, authed_role, authed_name, error_banner_text, challenge_timeout_ms
            )
            latency_ms = (time.monotonic() - start) * 1000
            if authed_result == "element_missing":
                return await _fail(page, "authed", "session_expired", "authed_check", latency_ms,
                                    artifacts_dir, mask_patterns, masking_enabled)

            return CheckResult(ok=True, http_status=None, latency_ms=latency_ms, fail_reason=None, layer="authed")
        finally:
            await page.close()
