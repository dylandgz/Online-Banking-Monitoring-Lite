"""Stage 6/8: authenticated sign-in journey -- locators, TOTP, the submit_*/classify_*
step functions, run_journey() (full login -> TOTP -> authed -> optional logout), and
run_authed_check() (Stage 8: a cheap check reusing a saved session, zero logins
consumed). Originally ported from the `signin_lab` prototype and converted from
patchright's sync API to its async API to match this project's asyncio architecture
(Rule 17 "single asyncio process"). run_journey() is new -- the prototype's own run.py
(the only place this sequencing previously lived) was explicitly not shipped because it
assumed a human was watching; this rebuilds that control flow without any
print()/input() calls, reporting through CheckResult like every other check layer.

NOTE ON PROVENANCE: comments here used to cite "handoff section N" of the prototype's
MONITOR_LITE_HANDOFF.md. That document is not in this repository, so those pointers were
unfollowable and have been removed -- the reasoning they carried is inlined instead.
Nothing in this file should be treated as verified because the prototype did it that way;
the live-target evidence lives in data/dom_dumps/ and personal/ISSUES.md.

Browser automation uses patchright (not vanilla Playwright), always headed under xvfb
(CLAUDE.md v3.2/v3.3, named exceptions to Rules 6 "bot challenges: detect, never defeat" and 14 "headless is the scheduler's mode"). Session persistence uses a
single storageState file (v3.4/v3.6/Stage 8) -- the earlier persistent-profile-directory
approach (USER_DATA_DIR) has been retired, per Stage 8's acceptance criteria: it carried
cf_clearance just as effectively (confirmed in the prototype's own testing) and
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
from urllib.parse import urlsplit

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
# trusting it. Kept here as named factories per Rule 11 "locators".

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
    # .first, not the bare locator: ERROR_BANNER_TEXT is free-form .env text, and a short
    # string ("error", "incorrect") routinely matches several nodes on a banking page.
    # Playwright's strict mode raises Error -- NOT AssertionError -- when a locator used by
    # expect()/is_visible() resolves to more than one element, and none of the classify_*
    # helpers catch Error, so an ambiguous banner string used to crash the probe outright
    # instead of reporting a fail_reason. Same fix as username_field's existing .first.
    return page.get_by_text(text).first


def mfa_heading(page: Page) -> Locator:
    # [2026-08-25] The step-up flow has TWO screens, each with its own heading, and this
    # locator is what tells classify_after_submit "we reached MFA" and classify_after_totp
    # "we left MFA" -- so it has to mean the whole step, not one screen of it. "Login
    # Security" is the factor picker (h1); "Verify Information" is the code-entry screen
    # that follows (h3). Both are served from the SAME url (/nxg-olb/beta/mfaVerification)
    # as two render states of one route -- the heading is the only thing distinguishing
    # them, so the union is load-bearing, not caution. Matching only one of them is
    # exactly the bug that fell through to the bot_challenge fallback and parked the auth
    # track in CONFIG_ERROR. Both names confirmed against captured DOM (data/dom_dumps,
    # 2026-08-25).
    return page.get_by_role("heading", name="Login Security", exact=True).or_(
        page.get_by_role("heading", name="Verify Information", exact=True)
    )


def enter_code_button(page: Page) -> Locator:
    # Confirmed against captured DOM (data/dom_dumps, 2026-08-25). The picker also offers
    # "send a text to phone number ending in NNNN" and two "call phone number ..." rows;
    # this is the authenticator-app one.
    return page.get_by_role("button", name="send code to authenticator", exact=True)


def totp_field(page: Page) -> Locator:
    # [2026-08-25] Confirmed against captured DOM (data/dom_dumps, 03_verification_code_screen):
    #   <label for=":r8k:">Verification code</label><input id=":r8k:" type="text">
    # The name comes from a real <label for>, not a placeholder fallback, so exact=True is
    # safe. The same string also appears in a <legend> inside an aria-hidden fieldset,
    # which contributes nothing to the accessible name -- no double-match risk.
    # Do NOT key off id or data-testid: the id (":r8k:") is React-generated and changes
    # between renders, and data-testid is outside Rule 11 "locators".
    return page.get_by_role("textbox", name="Verification code", exact=True)


def authed_marker(page: Page, *, text: Optional[str], role: Optional[str], name: Optional[str]) -> Locator:
    # .first on both branches, for the same strict-mode reason as error_banner above --
    # AUTHED_REQUIRED_TEXT/ROLE+NAME are also free-form .env values with no arity
    # validation, and this locator is read by expect() in both classify_authed and
    # classify_logout, so an ambiguous match crashed two separate code paths.
    if role and name:
        return page.get_by_role(role, name=name, exact=True).first
    return page.get_by_text(text).first


def logout_link(page: Page) -> Locator:
    return page.get_by_role("link", name="Logout", exact=True)


# Deliberately not ported: the prototype's register_device_button/public_device_button/
# dismiss_device_prompt, which assumed the post-TOTP screen was a device-trust prompt.
#
# [2026-08-29] That assumption is still unresolved, and the picture has moved: the
# 2026-08-25 capture (data/dom_dumps/20260825T194512Z/03_verification_code_screen/) shows
# the CODE-ENTRY screen carries a real device-registration choice -- "Yes, register my
# private device" / "No, this is a public device", both type=submit -- which submit_totp()
# never clicks. Whether that is the same screen the prototype meant is unknown. Do not
# resolve it from this comment: read the capture, and see B27 in personal/ISSUES.md first
# -- registering the device plausibly suppresses future step-ups, which would make
# classify_after_submit()'s bot_challenge fallback fire on every subsequent login.


# --- submit / classify pairs --------------------------------------------------
# [v3.5] "unknown" must not ship. Every fallback below resolves to a specific reason
# from the closed taxonomy, with a comment explaining the judgment call -- these are
# best-effort mappings made without a live site to drill against, and they have NOT all
# survived contact with one: classify_after_submit's fallback was confirmed wrong live on
# 2026-08-25. Tracked as B1/B6/B27 in personal/ISSUES.md -- read those before trusting any
# fallback below.

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
        # Neither marker showed up. This is exactly what bot_challenge, auth_unavailable
        # and rate_limited look like undifferentiated -- and, per B27, what a login that
        # simply succeeded without a step-up looks like too.
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
        # In the prototype this was an unguarded wait that crashed the whole script.
        # element_missing is the literal, honest description -- the expected field
        # genuinely never appeared.
        return "element_missing"

    # No way to answer the code prompt (no human providing one, no captured secret) --
    # this is a config problem (a human needs to set TOTP_SECRET), not evidence about
    # the platform. Reported the same way any other "MFA could not be completed" case
    # is (mfa_failed -> CONFIG_ERROR, Rule 4 "never retry a credential rejection"), instead of crashing pyotp.TOTP(None)
    # deep in a background task where it would silently drop the whole probe (Rule 15 "every probe writes a checks row"
    # requires every probe to still write a row).
    if not code_provider and not totp_secret:
        return "mfa_failed"

    # pyotp.TOTP(secret).now() raises binascii.Error (a ValueError subclass) on a secret
    # that isn't valid base32 -- a mistyped or padded TOTP_SECRET in .env. That's a config
    # problem a human must fix, so it reports mfa_failed -> CONFIG_ERROR (Rule 4 "never retry a credential rejection") exactly
    # like the missing-secret case above, rather than raising out of the probe and taking
    # the whole cycle down unrecorded. The secret itself is never included in any message.
    try:
        code = await code_provider() if code_provider else await get_fresh_totp_code(totp_secret)
    except (ValueError, TypeError):
        return "mfa_failed"

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
    # authed state wasn't established either way. Never observed in the prototype; revisit
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
# Rule 13 "screenshots on failure only": post-login screenshots must mask PII locators. Masking works by regex pattern
# over visible text, not by label -- a label locator only covers the label element
# itself, not the sibling element holding the actual (dynamic) value.

async def capture_masked_screenshot(
    page: Page,
    artifacts_dir: str,
    step: str,
    fail_reason: str,
    mask_patterns: Sequence[str],
    masking_enabled: bool,
) -> Optional[str]:
    """Returns the screenshot path, or None if the capture itself failed.

    Never raises: this runs on the failure path, from _fail(), so an exception here would
    destroy the CheckResult that describes the actual failure and replace it with a crash.
    The likeliest trigger is precisely the most important failure to report -- a page that
    timed out mid-navigation or whose target crashed can't be screenshotted, so the
    'nav_error' result would be lost. Mirrors check.py's _save_screenshot, which already
    swallows PlaywrightError for the same reason. Losing the evidence image is acceptable;
    losing the fail_reason is not."""
    Path(artifacts_dir).mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = str(Path(artifacts_dir) / f"{ts}_{step}_{fail_reason}.png")

    try:
        if masking_enabled:
            # A pattern that matches nothing on this page is safe, not silently wrong:
            # Playwright paints a mask rectangle only over elements a locator actually
            # resolves to, so a zero-match locator just contributes no rectangle rather
            # than erroring or masking the wrong thing. That means an outdated/typo'd
            # pattern fails open (no mask, PII exposed) with no error to catch it -- and
            # note masking cannot cover <input> values at all, since get_by_text matches
            # text nodes, so a filled username field is never redacted by this. Rule 13's "screenshots on failure only"
            # real safety net is the review step, not this list being provably correct.
            if not mask_patterns:
                # [v3.9 / Stage H P2] The genuinely dangerous configuration, and until now
                # the silent one: MASK_TEXT is optional and defaults to empty, and
                # validate_stage6() doesn't require it, so the DEFAULT setup captures a
                # full-page authenticated screenshot with an empty mask list and no
                # warning at all. The loud warning below only ever covered
                # MASKING_ENABLED=false -- the case someone deliberately chose. Warn on
                # the accidental case too; it is the one that ships by mistake.
                print("WARNING: MASKING_ENABLED is on but MASK_TEXT is empty -- this "
                      "authenticated screenshot has NO redactions. Set MASK_TEXT in .env "
                      "(semicolon-separated regexes) per Rule 13 'screenshots on failure only'.", flush=True)
            mask_locators = [page.get_by_text(re.compile(pattern)) for pattern in mask_patterns]
        else:
            # Explicit opt-out only, never a silent default -- printed loudly so it's never
            # in effect against a real account by accident.
            print("WARNING: masking disabled -- screenshot is NOT masked.", flush=True)
            mask_locators = []

        await page.screenshot(path=path, mask=mask_locators, full_page=True)
    except (PatchrightTimeoutError, PatchrightError, OSError, re.error):
        # re.error: an invalid regex in MASK_TEXT (unvalidated .env free text), raised by
        # re.compile above. OSError: unwritable artifacts_dir.
        return None
    return path


# --- orchestrator --------------------------------------------------------------
# The shape this follows:
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

def _same_route(url_a: str, url_b: str) -> bool:
    """Do two URLs point at the same scheme+host+path? Query and fragment are ignored --
    banking apps append session/nonce params freely and those don't change which page you
    are on. Trailing-slash difference is not a difference."""
    a, b = urlsplit(url_a), urlsplit(url_b)
    return (
        a.scheme == b.scheme
        and a.netloc.lower() == b.netloc.lower()
        and a.path.rstrip("/") == b.path.rstrip("/")
    )


async def bounced_to_login(page: Page, authed_url: str, timeout_ms: int) -> bool:
    """Did the authed route bounce us back to a login screen (expired session), as opposed
    to serving something broken (platform failure)?

    [v3.9 / Stage H P2] This distinction is the whole point. run_authed_check used to map
    *every* "page loaded but the authed marker is missing" to session_expired, which
    Rule 3 "session_expired never scores" makes completely inert -- no score, no probe-floor credit, and it never even
    sets burst_started_ts, so the auth burst never started. The consequence was that
    "online banking behind login not rendering" -- Rule 10's exact authed wording, and
    v3.8's literal definition of the platform being DOWN -- could not be reached by the
    check whose entire job is to detect it. A 500 error page or a blank authed home
    scored zero and merely spent a recovery login.

    The signature of an expired session is specifically a *redirect away* from the authed
    route to a login form. Still being on the authed route with no marker means the
    platform served us something wrong, which is real evidence. Both conditions are
    required here, deliberately: a redirect on its own is not enough (a redirect to a
    maintenance or error page is platform evidence, not a session problem), so the login
    form must also actually be present."""
    if _same_route(page.url, authed_url):
        return False
    try:
        await username_field(page).first.wait_for(state="visible", timeout=timeout_ms)
        return True
    except (PatchrightTimeoutError, PatchrightError):
        # Redirected somewhere that isn't a login form -- not a session problem.
        return False


def unexpected_fail_reason(exc: BaseException) -> str:
    """Maps a browser exception that escaped every specific handler onto the closed
    fail_reason taxonomy, so an unforeseen failure still reports evidence instead of
    raising out of the probe.

    Why this exists: before it, only page.goto and the username-field wait were guarded,
    and the only exception type the classify_* helpers caught was AssertionError (from
    expect()). Every other Playwright call -- fill, click, press, is_visible, logout --
    could raise PatchrightError/PatchrightTimeoutError and take the whole cycle down with
    no checks row, no cycles row, and no alert (Rule 15 "every probe writes a checks row" violated, and a silent monitoring
    blind spot on exactly the probes most likely to fail during a real outage).

    Mapping rationale, both deliberately Soft (weight 1) per CLAUDE.md's scoring table:
      - a timeout is genuinely indistinguishable from a hung site, and 'timeout' is the
        honest word for it;
      - anything else (strict-mode violation, target crashed, context closed) is a failed
        browser operation rather than an observation that an element was absent, so
        'nav_error' fits better than 'element_missing'.

    Known residual risk, deliberately accepted for now: a *persistent* monitor-side bug
    (say a locator that always matches two elements) looks identical to a real soft
    outage and can therefore eventually page, since 4 soft probes reach DOWN_CONFIDENCE.
    The 3-probe floor stops a single flake, not a systematic defect. Properly fixing that
    needs a distinct non-scoring 'monitor error' class in the taxonomy, which is a
    CLAUDE.md amendment, not a code change -- flagged rather than silently assumed safe."""
    if isinstance(exc, PatchrightTimeoutError):
        return "timeout"
    return "nav_error"


async def _fail(
    page: Page,
    layer: str,
    fail_reason: str,
    step: str,
    latency_ms: float,
    artifacts_dir: str,
    mask_patterns: Sequence[str],
    masking_enabled: bool,
    http_status: Optional[int] = None,
) -> CheckResult:
    """http_status [v3.9]: the authed route's real HTTP status when we have it. Every
    CheckResult from this module used to report None, which is why bad_status:5xx -- the
    only Hard-weight evidence the authed layer could plausibly produce -- was unreachable."""
    screenshot_path = await capture_masked_screenshot(
        page, artifacts_dir, step, fail_reason, mask_patterns, masking_enabled
    )
    return CheckResult(
        ok=False,
        http_status=http_status,
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
    Never retries anything itself (Rule 4 "never retry a credential rejection" is the caller's job -- this function just
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
    # The outer guard covers everything the inner per-step handlers don't, INCLUDING
    # open_journey_browser itself -- browser launch, and new_context() raising on a
    # truncated/corrupt storage_state file (a real possibility after the process is killed
    # mid-write), both happen before any page exists to screenshot. See
    # unexpected_fail_reason for the mapping and its accepted residual risk.
    try:
        async with open_journey_browser(browser_channel, browser_timeout_ms, seed_path) as context:
            page = await context.new_page()
            try:
                try:
                    await page.goto(login_url, timeout=browser_timeout_ms)
                except (PatchrightTimeoutError, PatchrightError):
                    latency_ms = (time.monotonic() - start) * 1000
                    return await _fail(page, "render", "nav_error", "navigate", latency_ms,
                                        artifacts_dir, mask_patterns, masking_enabled)

                # "Wait for real content" is folded into waiting for the login form
                # itself -- there is no site-specific "real content" locator yet
                # (deferred; there is no separate locators module), and wait_for(state="visible")
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
                # Guarded: the login itself already succeeded, and a failure to cache the
                # session (unwritable path, disk full) must not turn that success into a
                # reported platform failure. The cost is only that the next cycle needs a
                # fresh login; main.py's budget still governs that.
                if session_state_path:
                    try:
                        await save_session_state(context, session_state_path)
                    except OSError as exc:
                        print(f"[journey] WARNING: could not save session state: {exc.strerror}", flush=True)

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
    except (PatchrightTimeoutError, PatchrightError) as exc:
        # layer="authed": anything reaching here got past the two explicitly-guarded
        # pre-credential steps above (both of which report layer="render" themselves), so
        # the authed layer was already engaged. No screenshot -- the page may not exist or
        # may be the very thing that failed.
        latency_ms = (time.monotonic() - start) * 1000
        return CheckResult(ok=False, http_status=None, latency_ms=latency_ms,
                           fail_reason=unexpected_fail_reason(exc), layer="authed")


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
    exactly what CLAUDE.md's "Cross-track suppression" section wants (a login-route outage can leave existing
    sessions still served, and that distinction belongs in the incident record).

    A failure to even navigate/load the page (nav_error) is real platform evidence, same
    as any other reachability failure -- it's not a session problem. Nor is an HTTP error
    status: a >=400 authed route reports bad_status:<code> (5xx being the only Hard-weight
    evidence this layer can produce), read explicitly off goto's Response because Playwright
    does not raise on error statuses.

    [v3.9 / Stage H P2] A missing authed marker is NOT automatically session_expired -- that
    mapping is what made Rule 10's "online banking behind login not rendering" unreachable by
    the very check whose job is to detect it (session_expired is inert under Rule 3 "session_expired never scores": no
    score, no probe-floor credit, and it never opens a burst). session_expired now requires
    the full expired-session signature via bounced_to_login(): redirected AWAY from the
    authed route AND a login form actually present. Still on the authed route with no marker
    is element_missing -- Soft, scoring, burst-opening -- because that is the platform
    serving us something wrong."""
    start = time.monotonic()

    # Outer guard, same rationale as run_journey's: this is the probe that runs every 60s,
    # and open_journey_browser's new_context() raises on a corrupt/truncated session file
    # -- outside any page context, so it cannot be reported from inside the block. Before
    # this guard, that condition killed the auth track silently on every cycle until a
    # human deleted the file, because is_session_fresh only stats mtime and never validates
    # the JSON, and main.py's recovery path keys off a result that was never produced.
    try:
        async with open_journey_browser(browser_channel, browser_timeout_ms, session_state_path) as context:
            page = await context.new_page()
            try:
                try:
                    response = await page.goto(authed_url, timeout=browser_timeout_ms)
                except (PatchrightTimeoutError, PatchrightError):
                    latency_ms = (time.monotonic() - start) * 1000
                    return await _fail(page, "authed", "nav_error", "authed_check_nav", latency_ms,
                                        artifacts_dir, mask_patterns, masking_enabled)

                # [v3.9] Playwright does NOT raise on an HTTP error status -- goto only
                # raises on network-level failures -- so this status has to be read
                # explicitly. It was previously discarded, which is why bad_status:5xx was
                # unreachable on this layer and a 500 behind login produced no evidence at
                # all. A 5xx here is Hard evidence (weight 2) via state.classify; it is
                # emphatically not a session problem, so it must not route to recovery.
                http_status = response.status if response is not None else None
                if http_status is not None and http_status >= 400:
                    latency_ms = (time.monotonic() - start) * 1000
                    return await _fail(page, "authed", f"bad_status:{http_status}", "authed_check_status",
                                        latency_ms, artifacts_dir, mask_patterns, masking_enabled,
                                        http_status=http_status)

                await _settle(page, browser_timeout_ms)
                authed_result = await classify_authed(
                    page, authed_text, authed_role, authed_name, error_banner_text, challenge_timeout_ms
                )
                latency_ms = (time.monotonic() - start) * 1000
                if authed_result == "element_missing":
                    # Two very different things look identical at this point; see
                    # bounced_to_login for why telling them apart is the difference
                    # between the authed layer being able to report an outage and not.
                    if await bounced_to_login(page, authed_url, challenge_timeout_ms):
                        return await _fail(page, "authed", "session_expired", "authed_check",
                                            latency_ms, artifacts_dir, mask_patterns, masking_enabled,
                                            http_status=http_status)
                    return await _fail(page, "authed", "element_missing", "authed_check_content",
                                        latency_ms, artifacts_dir, mask_patterns, masking_enabled,
                                        http_status=http_status)

                return CheckResult(ok=True, http_status=http_status, latency_ms=latency_ms,
                                   fail_reason=None, layer="authed")
            finally:
                await page.close()
    except (PatchrightTimeoutError, PatchrightError) as exc:
        latency_ms = (time.monotonic() - start) * 1000
        return CheckResult(ok=False, http_status=None, latency_ms=latency_ms,
                           fail_reason=unexpected_fail_reason(exc), layer="authed")
