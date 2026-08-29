"""Pulse (curl_cffi) + browser (Playwright) checks. Returns a CheckResult; no DB or email I/O here."""
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from curl_cffi import requests as curl_requests
from curl_cffi.requests.exceptions import DNSError, ConnectionError as CurlConnectionError, Timeout as CurlTimeout, RequestException as CurlRequestException
from playwright.async_api import async_playwright, Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError

from monitor.timeutil import artifact_stamp


@dataclass
class CheckResult:
    ok: bool
    http_status: Optional[int]
    latency_ms: float
    fail_reason: Optional[str]
    screenshot_path: Optional[str] = None
    # [B16] Where the browser actually ENDED UP, recorded on browser-layer failures only
    # (the pulse probe has no browser, so it stays None there). Without it, two very
    # different failures are indistinguishable in the record: the bank's marketing site
    # rendered inside a frame at the correct authed URL, versus a cross-origin redirect
    # away to it. Both report element_missing, and telling them apart cost a multi-hour
    # diagnosis on 2026-08-27 because nothing wrote down the URL.
    page_url: Optional[str] = None
    layer: str = "render"  # "pulse" | "render" -- the strongest layer this probe actually evaluated
    # [v3.8 / Stage R] populated only by perform_check() (the combined pulse+render probe
    # that opens a cycle) -- None on burst re-probes and the auth journey, which each only
    # ever measure one thing. Lets the cycles row report both legs' timing separately even
    # though they run as one call.
    pulse_latency_ms: Optional[float] = None
    render_latency_ms: Optional[float] = None


async def pulse_check(url: str, timeout_s: float = 10.0) -> tuple[bool, Optional[int], float, Optional[str]]:
    """GET via curl_cffi impersonating Chrome's TLS/HTTP2 fingerprint. Plain httpx gets fingerprinted
    and 403'd by this target's WAF even with correct headers; curl_cffi clears it. Returns
    (ok, http_status, latency_ms, fail_reason)."""
    start = time.monotonic()
    try:
        resp = await curl_requests.AsyncSession().get(url, impersonate="chrome", timeout=timeout_s, allow_redirects=True)
        latency_ms = (time.monotonic() - start) * 1000
        if resp.status_code >= 400:
            return False, resp.status_code, latency_ms, f"bad_status:{resp.status_code}"
        return True, resp.status_code, latency_ms, None
    except CurlTimeout:
        return False, None, (time.monotonic() - start) * 1000, "timeout"
    except DNSError:
        return False, None, (time.monotonic() - start) * 1000, "dns"
    except CurlConnectionError:
        return False, None, (time.monotonic() - start) * 1000, "conn_refused"
    except CurlRequestException:
        return False, None, (time.monotonic() - start) * 1000, "nav_error"


async def browser_check(
    url: str,
    required_text: Optional[str],
    required_role: Optional[str],
    required_name: Optional[str],
    timeout_ms: int,
    artifacts_dir: str,
    headless: bool = True,
) -> tuple[bool, Optional[str], Optional[str], Optional[str]]:
    """Loads the page and asserts the required element is visible.
    Returns (ok, fail_reason, screenshot_path, page_url).

    [B16] page_url is reported on failures for the same reason the authed layer reports it:
    'element_missing' alone cannot distinguish the expected page rendering wrongly from a
    redirect somewhere else entirely."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        page = await browser.new_page()
        try:
            try:
                await page.goto(url, timeout=timeout_ms)
            except PlaywrightError:
                # PlaywrightTimeoutError is a subclass, so this one clause covers both; they
                # produced byte-identical handling.
                screenshot_path = await _save_screenshot(page, artifacts_dir)
                return False, "nav_error", screenshot_path, _safe_url(page)

            if required_text:
                locator = page.get_by_text(required_text)
            else:
                locator = page.get_by_role(required_role, name=required_name)

            try:
                await locator.first.wait_for(state="visible", timeout=timeout_ms)
                return True, None, None, None
            except PlaywrightTimeoutError:
                screenshot_path = await _save_screenshot(page, artifacts_dir)
                return False, "element_missing", screenshot_path, _safe_url(page)
        finally:
            await browser.close()


def _safe_url(page) -> Optional[str]:
    """[B16] page.url for the record, never raising -- see journey.safe_page_url."""
    try:
        return page.url
    except PlaywrightError:
        return None


async def _save_screenshot(page, artifacts_dir: str) -> Optional[str]:
    try:
        Path(artifacts_dir).mkdir(parents=True, exist_ok=True)
        ts = artifact_stamp()
        path = str(Path(artifacts_dir) / f"{ts}.png")
        await page.screenshot(path=path)
        return path
    except PlaywrightError:
        return None


async def perform_check(
    url: str,
    required_text: Optional[str],
    required_role: Optional[str],
    required_name: Optional[str],
    browser_timeout_ms: int,
    artifacts_dir: str,
    pulse_timeout_s: float = 10.0,
    headless: bool = True,
) -> CheckResult:
    """Full check: pulse first, then browser render -- skips the browser check if the pulse
    already failed. This is the normal 60s-cadence probe and also burst probe index 0 (the
    failure that starts a burst is always this combined check)."""
    pulse_ok, http_status, pulse_latency_ms, fail_reason = await pulse_check(url, pulse_timeout_s)
    if not pulse_ok:
        return CheckResult(
            ok=False, http_status=http_status, latency_ms=pulse_latency_ms, fail_reason=fail_reason,
            layer="pulse", pulse_latency_ms=pulse_latency_ms, render_latency_ms=None,
        )

    render_start = time.monotonic()
    browser_ok, browser_fail_reason, screenshot_path, page_url = await browser_check(
        url, required_text, required_role, required_name, browser_timeout_ms, artifacts_dir, headless=headless
    )
    render_latency_ms = (time.monotonic() - render_start) * 1000
    if not browser_ok:
        return CheckResult(
            ok=False,
            http_status=http_status,
            latency_ms=pulse_latency_ms,
            fail_reason=browser_fail_reason,
            screenshot_path=screenshot_path,
            page_url=page_url,
            layer="render",
            pulse_latency_ms=pulse_latency_ms,
            render_latency_ms=render_latency_ms,
        )

    return CheckResult(
        ok=True, http_status=http_status, latency_ms=pulse_latency_ms, fail_reason=None, layer="render",
        pulse_latency_ms=pulse_latency_ms, render_latency_ms=render_latency_ms,
    )


async def pulse_only_probe(url: str, pulse_timeout_s: float = 10.0) -> CheckResult:
    """A burst re-probe that only re-checks network reachability -- independent evidence
    from a render probe, per Stage 5's 'vary the probe' rule."""
    ok, http_status, latency_ms, fail_reason = await pulse_check(url, pulse_timeout_s)
    return CheckResult(ok=ok, http_status=http_status, latency_ms=latency_ms, fail_reason=fail_reason, layer="pulse")


async def render_only_probe(
    url: str,
    required_text: Optional[str],
    required_role: Optional[str],
    required_name: Optional[str],
    browser_timeout_ms: int,
    artifacts_dir: str,
    headless: bool = True,
) -> CheckResult:
    """A burst re-probe that only re-checks the page render, in a fresh browser context --
    independent evidence from a pulse probe, per Stage 5's 'vary the probe' rule.

    [B25] latency_ms is measured, not hardcoded. It used to be a literal 0.0, which is not a
    fast probe -- it is no measurement at all, written into a column that reads as one. 102 of
    the 3,843 render rows on the live DB carry that zero, and every one of them is a burst
    re-probe: the minutes an operator actually studies were the only minutes with no render
    timing. The measurement spans exactly what browser_check does (launch, navigate, assert),
    which is the same span perform_check times for its own render leg, so burst and opening
    render latencies are directly comparable."""
    start = time.monotonic()
    ok, fail_reason, screenshot_path, page_url = await browser_check(
        url, required_text, required_role, required_name, browser_timeout_ms, artifacts_dir, headless=headless
    )
    latency_ms = (time.monotonic() - start) * 1000
    return CheckResult(ok=ok, http_status=None, latency_ms=latency_ms, fail_reason=fail_reason,
                       screenshot_path=screenshot_path, page_url=page_url, layer="render")
