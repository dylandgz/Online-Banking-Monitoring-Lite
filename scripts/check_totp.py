"""Stage 6 acceptance drill: cross-check the captured TOTP_SECRET against the phone
authenticator app, offline. Prints the code this codebase would submit, alongside the
seconds left in the current window, and refreshes as windows roll over -- so you can
watch it agree (or disagree) with the app on your phone across a window boundary.

Touches nothing: no browser, no network, no database, no login_events row, zero logins
consumed. It exercises exactly the code path submit_totp() uses (journey.get_fresh_totp_code)
rather than a reimplementation, so agreement here means agreement there.

Note it deliberately prints the live code -- that is the entire point of the drill, and
it is the one place Rule 7's "never rendered" gives way to a human verifying the factor
by eye. Run it in a terminal nobody is looking over, and don't paste the output anywhere.
The SECRET itself is never printed.

Usage: .venv/bin/python -m scripts.check_totp [--windows N]   (default 2 windows)
"""
import asyncio
import sys

import config
from monitor import journey


async def main() -> None:
    if not config.TOTP_SECRET:
        raise SystemExit(
            "TOTP_SECRET is not set in .env -- capture it from the authenticator "
            "enrollment screen's manual-entry ('can't scan?') option first."
        )

    windows = 2
    if "--windows" in sys.argv:
        windows = int(sys.argv[sys.argv.index("--windows") + 1])

    print(f"Open the authenticator app on your phone. Comparing {windows} code window(s).")
    print("The codes below must match the app's, and must roll over at the same moment.\n")

    seen = 0
    last = None
    while seen < windows:
        # Same call submit_totp() makes -- including its roll-over wait, so a code is
        # never printed with only a second or two of validity left on it.
        code = await journey.get_fresh_totp_code(config.TOTP_SECRET)
        if code != last:
            seen += 1
            last = code
            print(f"  code {code}   ({journey._totp_seconds_remaining()}s left in this window)")
        await asyncio.sleep(1)

    print("\nIf every code matched the phone app, the secret is correct and "
          "unattended logins can answer MFA on their own.")


if __name__ == "__main__":
    asyncio.run(main())
