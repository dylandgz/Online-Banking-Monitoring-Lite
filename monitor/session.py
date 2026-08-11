"""Stage 8: session persistence for the sign-in journey. A single storageState file
(cookies + localStorage, including Cloudflare's cf_clearance) is saved after a
successful full login and reused by cheap checks so they don't need to submit
credentials/TOTP again. Rule 7: this file is a real secret, same tier as TOTP_SECRET --
gitignored, chmod 600, never logged or screenshotted.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from patchright.async_api import BrowserContext


async def save_session_state(context: BrowserContext, path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    await context.storage_state(path=path)
    os.chmod(path, 0o600)


def is_session_fresh(path: str, max_age_s: int) -> bool:
    """A cookie can still be individually valid past this, but the whole point of
    SESSION_MAX_AGE_S is to force a periodic real login rather than trusting a cached
    session indefinitely -- age is measured from when the file was last written, not
    from any cookie's own expiry."""
    p = Path(path)
    if not p.exists():
        return False
    age_s = time.time() - p.stat().st_mtime
    return age_s < max_age_s
