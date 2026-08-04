"""Pulse (curl_cffi) + browser (Playwright) checks. Returns a CheckResult; no DB or email I/O here."""
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from curl_cffi import requests as curl_requests
from curl_cffi.requests.exceptions import DNSError, ConnectionError as CurlConnectionError, Timeout as CurlTimeout, RequestException as CurlRequestException
from playwright.async_api import async_playwright, Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError


@dataclass
class CheckResult:
    ok: bool
    http_status: Optional[int]
    latency_ms: float
    fail_reason: Optional[str]
    screenshot_path: Optional[str] = None


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
) -> tuple[bool, Optional[str], Optional[str]]:
    """Loads the page and asserts the required element is visible. Returns (ok, fail_reason, screenshot_path)."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        page = await browser.new_page()
        try:
            try:
                await page.goto(url, timeout=timeout_ms)
            except PlaywrightTimeoutError:
                screenshot_path = await _save_screenshot(page, artifacts_dir)
                return False, "nav_error", screenshot_path
            except PlaywrightError:
                screenshot_path = await _save_screenshot(page, artifacts_dir)
                return False, "nav_error", screenshot_path

            if required_text:
                locator = page.get_by_text(required_text)
            else:
                locator = page.get_by_role(required_role, name=required_name)

            try:
                await locator.first.wait_for(state="visible", timeout=timeout_ms)
                return True, None, None
            except PlaywrightTimeoutError:
                screenshot_path = await _save_screenshot(page, artifacts_dir)
                return False, "element_missing", screenshot_path
        finally:
            await browser.close()


async def _save_screenshot(page, artifacts_dir: str) -> Optional[str]:
    try:
        Path(artifacts_dir).mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
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
    """Runs the pulse check first; skips the browser check if the pulse already failed."""
    pulse_ok, http_status, latency_ms, fail_reason = await pulse_check(url, pulse_timeout_s)
    if not pulse_ok:
        return CheckResult(ok=False, http_status=http_status, latency_ms=latency_ms, fail_reason=fail_reason)

    browser_ok, browser_fail_reason, screenshot_path = await browser_check(
        url, required_text, required_role, required_name, browser_timeout_ms, artifacts_dir, headless=headless
    )
    if not browser_ok:
        return CheckResult(
            ok=False,
            http_status=http_status,
            latency_ms=latency_ms,
            fail_reason=browser_fail_reason,
            screenshot_path=screenshot_path,
        )

    return CheckResult(ok=True, http_status=http_status, latency_ms=latency_ms, fail_reason=None)
