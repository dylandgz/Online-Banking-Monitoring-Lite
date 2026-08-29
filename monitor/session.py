"""Stage 8: session persistence for the sign-in journey. A single storageState file
(cookies + localStorage, including Cloudflare's cf_clearance) is saved after a
successful full login and reused by cheap checks so they don't need to submit
credentials/TOTP again. Rule 16 "secrets from .env only": this file is a real secret, same tier as TOTP_SECRET --
gitignored, chmod 600, never logged or screenshotted.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from patchright.async_api import BrowserContext


async def save_session_state(context: BrowserContext, path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    await context.storage_state(path=path)
    os.chmod(path, 0o600)


def is_session_fresh(path: str, max_age_s: int) -> bool:
    """Is there a session file worth reusing? Fresh means: present, young enough, AND
    actually loadable.

    A cookie can still be individually valid past max_age_s, but the whole point of
    SESSION_MAX_AGE_S is to force a periodic real login rather than trusting a cached
    session indefinitely -- age is measured from when the file was last written, not from
    any cookie's own expiry.

    [v3.9 / Stage H P2] The JSON validity check is not cosmetic -- it closes a
    false-DOWN path. This used to stat mtime and nothing else, so a truncated or
    half-written file (the process killed mid-write, a full disk, a reboot during the
    Node driver's write) counted as "fresh". The authed check would then hand it to
    new_context(), which raises on unparseable storage_state, and because that raise
    happens before any page exists it isn't reportable from inside the probe. Originally
    that meant the auth track died silently every cycle until a human deleted the file;
    once Stage H P0 added the outer guard, the same condition instead began reporting
    nav_error every cycle -- four Soft probes reach AUTH_DOWN_CONFIDENCE, so a corrupt
    local file would page a false DOWN with authed wording ("online banking behind login
    not rendering") in about a minute while the bank was perfectly healthy. Verified by
    tracing apply_check: probes at confidence 1/2/3 then DOWN on the fourth. That is
    exactly the false positive this project ranks above every other concern.

    Returning False here routes the cycle down the already-correct path instead: no fresh
    session -> the budgeted recovery login runs (Rule 5 "the login budget is a hard limit") -> a valid session is written ->
    the condition self-heals with no page and no human. So the fix is also the cheapest
    possible one; it just has to happen here, before anything trusts the file.

    Deliberately only a parse check, not a schema check: json.load succeeding proves the
    file is intact, which is the failure mode being guarded. Validating Playwright's
    storage_state shape would couple this module to that format for no additional safety --
    a structurally-valid-but-wrong session simply fails auth and routes to session_expired,
    which Rule 3 "session_expired never scores" already handles."""
    p = Path(path)
    try:
        age_s = time.time() - p.stat().st_mtime
    except OSError:
        # Missing, or removed between the stat and now (also covers permission errors).
        return False
    if age_s >= max_age_s:
        return False

    try:
        with p.open(encoding="utf-8") as handle:
            json.load(handle)
    except (OSError, ValueError):
        # ValueError covers json.JSONDecodeError. A file we can't read or can't parse is
        # not a session -- say so plainly (no contents echoed: Rule 16 "secrets from .env only").
        print(f"[session] {path} is unreadable or not valid JSON -- treating as no session "
              f"so the recovery login can replace it.", flush=True)
        return False

    return True
