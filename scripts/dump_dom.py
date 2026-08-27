"""Walks the sign-in flow as far as it can and writes the DOM of every page it reaches,
so locators can be written from real markup instead of guessed from screenshots.

Changes nothing. It imports monitor/ code (the browser setup and the three login-form
locators that are known to work today) but never writes to the database, never touches
monitor state, never saves a session, and never logs out. The only thing it costs is one
real credentialed sign-in.

It deliberately does NOT use journey.py's MFA locators, because those are the ones under
suspicion -- automating past a screen with a locator that may be wrong is how you end up
with no information at all. Instead it drives what is known to work (fill credentials,
submit), then hands the browser to you: click through the flow yourself in the visible
window and press Enter here at each screen to capture it. That is what "as far as it can"
means in practice -- it gets you to the wall automatically, and you walk past the wall.

Each capture writes, per frame:
  elements.txt -- visible interactive elements with their inferred ARIA role and
                  accessible name, plus a ready-to-paste get_by_role(...) line
  page.html    -- that frame's full serialized DOM

Output goes to data/dom_dumps/<run timestamp>/NN_<label>/. Input values are stripped
from both outputs (Rule 7: a value can hold the password or a live OTP).

Usage: .venv/bin/python -m scripts.dump_dom
"""
import asyncio
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import config
from monitor import journey

# Role inference mirrors how Playwright's get_by_role resolves an element, so the
# suggested locator lines below are ones that will actually match. Accessible name is
# approximated in priority order (aria-label, then <label for>, then text, then
# placeholder) -- close enough to write a locator from, not a spec implementation.
COLLECT_JS = """
() => {
    const roleOf = (el) => {
        const explicit = el.getAttribute('role');
        if (explicit) return explicit;
        const tag = el.tagName.toLowerCase();
        if (tag === 'button') return 'button';
        if (tag === 'a') return el.hasAttribute('href') ? 'link' : null;
        if (tag === 'select') return 'combobox';
        if (tag === 'textarea') return 'textbox';
        if (/^h[1-6]$/.test(tag)) return 'heading';
        if (tag === 'input') {
            const t = (el.getAttribute('type') || 'text').toLowerCase();
            if (t === 'checkbox') return 'checkbox';
            if (t === 'radio') return 'radio';
            if (['button', 'submit', 'reset', 'image'].includes(t)) return 'button';
            if (t === 'password') return null;           // password inputs expose no role
            return 'textbox';
        }
        return null;
    };
    const nameOf = (el) => {
        const aria = el.getAttribute('aria-label');
        if (aria) return aria.trim();
        const labelledby = el.getAttribute('aria-labelledby');
        if (labelledby) {
            const ref = document.getElementById(labelledby);
            if (ref) return (ref.innerText || '').trim();
        }
        const id = el.getAttribute('id');
        if (id) {
            const lab = document.querySelector(`label[for="${CSS.escape(id)}"]`);
            if (lab) return (lab.innerText || '').trim();
        }
        const text = (el.innerText || '').trim();
        if (text) return text;
        return (el.getAttribute('placeholder') || '').trim();
    };
    return Array.from(document.querySelectorAll(
        'a, button, input, select, textarea, h1, h2, h3, h4, [role]'
    )).filter(el => {
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0;          // visible only -- hidden scaffolding is noise
    }).map(el => ({
        tag: el.tagName.toLowerCase(),
        type: el.getAttribute('type'),
        role: roleOf(el),
        name: nameOf(el).slice(0, 120),
        id: el.getAttribute('id'),
        testid: el.getAttribute('data-testid'),
    }));
}
"""

# fill() sets the value PROPERTY, so a serialized DOM normally has no value attribute to
# leak -- but a server-rendered value attribute would, so strip them unconditionally
# rather than relying on that.
VALUE_ATTR = re.compile(r'\svalue="[^"]*"', re.IGNORECASE)


def _frame_slug(frame, index: int) -> str:
    tail = (frame.url or "about:blank").rstrip("/").split("/")[-1] or "root"
    return f"{index:02d}_" + re.sub(r"[^A-Za-z0-9._-]", "_", tail)[:50]


def _locator_line(el: dict) -> str:
    """The get_by_* call that would match this element -- paste straight into journey.py."""
    if el["role"] and el["name"]:
        return f'page.get_by_role("{el["role"]}", name="{el["name"]}", exact=True)'
    if el["role"]:
        return f'page.get_by_role("{el["role"]}")   # unnamed -- needs scoping'
    if el["name"]:
        return f'page.get_by_text("{el["name"]}")'
    return "# no role and no accessible name -- not addressable under Rule 3"


async def capture(page, run_dir: Path, step: int, label: str) -> None:
    out = run_dir / f"{step:02d}_{label}"
    out.mkdir(parents=True, exist_ok=True)
    print(f"\n=== capture {step:02d} ({label}) -> {out}")
    print(f"    page url: {page.url}")

    for i, frame in enumerate(page.frames):
        try:
            elements = await frame.evaluate(COLLECT_JS)
            html = await frame.content()
        except Exception as exc:                 # cross-origin, or detached mid-capture
            print(f"    [frame {i}] {frame.url} -- unreadable: {type(exc).__name__}")
            continue

        slug = _frame_slug(frame, i)
        (out / f"{slug}.html").write_text(VALUE_ATTR.sub("", html))

        lines = [f"# frame {i}: {frame.url}", f"# {len(elements)} visible elements", ""]
        for el in elements:
            desc = el["tag"] + (f"[{el['type']}]" if el["type"] else "")
            extra = "".join(f"  {k}={el[k]!r}" for k in ("id", "testid") if el[k])
            lines.append(f"{desc}{extra}")
            lines.append(f"    role={el['role']!r}  name={el['name']!r}")
            lines.append(f"    {_locator_line(el)}")
            lines.append("")
        (out / f"{slug}.elements.txt").write_text("\n".join(lines))

        print(f"    [frame {i}] {frame.url} -- {len(elements)} elements -> {slug}.elements.txt")


async def main() -> None:
    if not (config.LOGIN_URL and config.LOGIN_USER and config.LOGIN_PASSWORD):
        raise SystemExit("LOGIN_URL, LOGIN_USER and LOGIN_PASSWORD must be set in .env.")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = Path(config.ARTIFACTS_DIR).parent / "dom_dumps" / stamp
    run_dir.mkdir(parents=True, exist_ok=True)
    print(f"Writing captures to {run_dir}")

    # storage_state=None for the same reason run_journey defaults to not seeding: a stale
    # session makes the server redirect away from the login form to the marketing site.
    async with journey.open_journey_browser(
        config.BROWSER_CHANNEL, config.BROWSER_TIMEOUT_MS, None
    ) as context:
        page = await context.new_page()
        step = 0

        await page.goto(config.LOGIN_URL, timeout=config.BROWSER_TIMEOUT_MS)
        await journey.username_field(page).first.wait_for(
            state="visible", timeout=config.BROWSER_TIMEOUT_MS
        )
        step += 1
        await capture(page, run_dir, step, "login_form")

        print("\nSubmitting credentials ...")
        await journey.submit_credentials(
            page, config.LOGIN_USER, config.LOGIN_PASSWORD, config.BROWSER_TIMEOUT_MS
        )
        step += 1
        await capture(page, run_dir, step, "after_submit")

        print("\nThe browser is yours now. Click through the flow by hand -- the factor "
              "picker, the code screen, the authed home page -- and capture each one.")
        while True:
            answer = await asyncio.to_thread(
                input, "\n[Enter] capture current screen  |  [label] capture with a name  |  q to quit: "
            )
            answer = answer.strip()
            if answer.lower().startswith("q"):
                break
            step += 1
            label = re.sub(r"[^A-Za-z0-9._-]", "_", answer) or "step"
            await capture(page, run_dir, step, label)

    print(f"\nDone. {step} captures in {run_dir}")
    print("Nothing was written to the database and no session was saved.")


if __name__ == "__main__":
    asyncio.run(main())
