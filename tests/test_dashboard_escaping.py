"""[AppScan XSS findings, 2026-09-22] Enforces dashboard.js's escaping rule.

AppScan rated four `innerHTML` assignments in dashboard.js as Critical reflected XSS. The
rating was wrong twice over -- nothing from the request reaches those sinks, so it is not
reflected, and every field actually rendered is a number, a code-defined enum, a server
timestamp or a UUID, so it was not exploitable. What was true is that the safety was a
*coincidence*: it held because every rendered field happened to contain no markup, and
nothing anywhere enforced that it would stay that way.

The trap was one token deep. `/api/cycle/{id}` already ships the whole `checks` row, so
`page_url` and `evidence_text` are sitting inside the probe-detail template right now,
unrendered. `evidence_text` is the live text of the monitored bank's role="alert" banner
(journey.py `_visible_alert_text`) -- verbatim remote content. Rendering it with
`${p.evidence_text}` would look like a one-line diagnostic improvement and would be real
stored XSS against an authenticated operator.

So this file exists to make the next person's mistake loud. It is a lint, not a behavioural
test: it reads the source and checks every `${...}` interpolation against the rule stated at
the top of dashboard.js. It deliberately does NOT need a browser, so it runs with the rest
of the suite in milliseconds.

The escaping itself is additionally proven end-to-end against a live browser with a real
payload -- see the 2026-09-22 PROGRESS.md entry. That check is not in this suite because it
would make the whole suite depend on a working Chromium.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

DASHBOARD_JS = Path(__file__).resolve().parents[1] / "monitor" / "web" / "static" / "dashboard.js"

# Helpers that RETURN MARKUP. Their callers must not esc() them (it would render the tags as
# visible text); each owns the escaping of anything it interpolates. probeLayer is the only
# one that passes a server value through, and it calls esc() itself -- asserted below.
MARKUP_HELPERS = {
    "probeLayer", "probeStatus", "probeLatency", "layerBadge", "sessionBadge", "verdictClass",
}

# Raw interpolations that never reach innerHTML. Keeping them as an explicit allowlist rather
# than trying to detect the sink means a NEW raw interpolation fails this test wherever it is
# added, and whoever added it has to come here and say which case it falls under.
ALLOWED_RAW = {
    "h", "m", "sec",              # fmtDuration -- returns a string its callers esc()
    "state.page", "totalPages",   # the page label, assigned via textContent
}


def _strip_comments(src: str) -> str:
    """Blank out whole-line `//` comments, preserving line numbering.

    Needed because dashboard.js's own header comment quotes `${p.evidence_text}` as the
    example of the mistake this file guards against -- without this, the lint flags the
    documentation explaining the lint. (It found exactly that when first run, which is a
    reassuring way to learn the scanner works.)

    Only whole-line comments are stripped. A trailing `//` after code would need real
    tokenising to tell from a `//` inside a string or URL, and no line in this file both
    interpolates and carries a trailing comment. If one ever does, this lint will flag it
    and the fix is to move the comment to its own line, not to loosen the scan."""
    return "\n".join("" if line.lstrip().startswith("//") else line
                     for line in src.splitlines())


def _interpolations(src: str) -> list[tuple[int, str]]:
    """Every `${...}` in the file as (line number, expression), brace-balanced.

    A regex cannot do this: the templates nest, e.g.
    `${cond ? `<a href="/x/${esc(id)}">v</a>` : "-"}`. Walking the braces handles that, and
    the nested interpolation is returned as its own entry so it gets checked on its own."""
    src = _strip_comments(src)
    out = []
    for i in range(len(src) - 1):
        if src[i] == "$" and src[i + 1] == "{":
            depth, j = 1, i + 2
            while j < len(src) and depth:
                if src[j] == "{":
                    depth += 1
                elif src[j] == "}":
                    depth -= 1
                j += 1
            out.append((src.count("\n", 0, i) + 1, src[i + 2:j - 1]))
    return out


def test_every_interpolation_is_escaped_or_a_known_markup_helper():
    """The rule from the top of dashboard.js, mechanically. An expression is acceptable if it
    is esc()-wrapped, is a call to a markup-returning helper, itself builds markup (contains a
    backtick -- its own inner interpolations are separate entries and are checked too), or is
    on the small allowlist of values that never reach innerHTML."""
    src = DASHBOARD_JS.read_text()
    offenders = []

    for line, expr in _interpolations(src):
        s = expr.strip()
        lead = re.match(r"[A-Za-z_$][\w$]*", s)
        ok = (
            s.startswith("esc(")
            or (lead and lead.group(0) in MARKUP_HELPERS)
            or "`" in s                     # builds markup; inner interpolations checked separately
            or s in ALLOWED_RAW
        )
        if not ok:
            offenders.append(f"  dashboard.js:{line}  ${{{s}}}")

    assert not offenders, (
        "Unescaped interpolation(s) in dashboard.js:\n" + "\n".join(offenders) + "\n\n"
        "Wrap the value in esc(). If it is a helper that returns markup, add it to "
        "MARKUP_HELPERS here; if it never reaches innerHTML, add it to ALLOWED_RAW. "
        "Do not widen this test to make a new sink pass without deciding which it is."
    )


def test_probe_layer_escapes_its_own_server_value():
    """probeLayer returns markup, so callers cannot esc() it -- which puts the escaping of
    p.layer inside the function. 461 early rows carry a NULL/empty layer (B25), so this path
    is exercised in production, and `layer` is a server-written column."""
    src = DASHBOARD_JS.read_text()
    body = src.split("function probeLayer(p) {", 1)[1].split("}", 1)[0]
    assert "esc(p.layer)" in body, (
        "probeLayer must escape p.layer itself: it returns markup, so its callers are "
        "required NOT to escape its result."
    )


def test_esc_covers_every_character_that_can_break_out():
    """The five characters that matter, and the ampersand ordering.

    & must be replaced first or the later replacements' own entities get double-escaped
    ('<' -> '&lt;' -> '&amp;lt;'), which renders as visible '&lt;' instead of '<'."""
    src = DASHBOARD_JS.read_text()
    body = src.split("function esc(v) {", 1)[1].split("\n}", 1)[0]

    order = [c for c in ("&", "<", ">", '"', "'") if f'/{c}/g' in body or f"/{c}/g" in body]
    assert order and order[0] == "&", "esc() must replace & first, or it double-escapes"

    for char, entity in (("&", "&amp;"), ("<", "&lt;"), (">", "&gt;"),
                         ('"', "&quot;"), ("'", "&#39;")):
        assert entity in body, f"esc() does not encode {char!r} as {entity}"

    assert "null" in body and "undefined" in body, (
        "esc() must return '' for null/undefined -- SQLite columns arrive as null and "
        "String(null) would render the literal text 'null' in the table."
    )


@pytest.mark.parametrize("field", ["page_url", "evidence_text", "screenshot_path"])
def test_the_unrendered_remote_fields_are_still_not_interpolated_raw(field):
    """These ship in the /api/cycle payload but are not rendered. If someone renders one, it
    must go through esc() -- evidence_text in particular is the monitored site's own banner
    text, so it is the one field in the payload an attacker could directly author."""
    src = DASHBOARD_JS.read_text()
    for line, expr in _interpolations(src):
        s = expr.strip()
        if field in s and not (s.startswith("esc(") or "`" in s):
            pytest.fail(
                f"dashboard.js:{line} interpolates {field} without esc(): ${{{s}}}\n"
                f"{field} is remote-influenced -- evidence_text is verbatim text from the "
                f"monitored site's role=\"alert\" banner."
            )


# --- the second wall: CSP [Option D alongside the escaping above] -------------------

def test_security_headers_are_on_every_response_including_healthz():
    """The header must not be per-route. /healthz is the one unauthenticated route, so it is
    the one most worth asserting -- and a middleware that skipped it would show that someone
    had started making the policy conditional."""
    from fastapi.testclient import TestClient
    from monitor.web import app as fastapi_app

    client = TestClient(fastapi_app)
    for path, auth in (("/healthz", None), ("/api/status", ("user", "pass"))):
        r = client.get(path, auth=auth)
        csp = r.headers.get("Content-Security-Policy", "")
        assert "default-src 'self'" in csp, f"{path} has no CSP"
        assert "unsafe-inline" not in csp, (
            f"{path} CSP allows unsafe-inline, which forfeits most of its XSS value -- "
            "move inline code into dashboard.js/.css instead of weakening this."
        )
        assert "frame-ancestors 'none'" in csp
        assert r.headers.get("X-Content-Type-Options") == "nosniff"


def test_the_dashboard_has_nothing_the_csp_would_break():
    """The CSP forbids 'unsafe-inline', so an inline handler or style attribute would break
    the page at runtime while every test still passed. Assert the precondition directly."""
    html = (Path(__file__).resolve().parents[1] / "monitor" / "web" / "templates"
            / "dashboard.html").read_text()
    css_js_dir = Path(__file__).resolve().parents[1] / "monitor" / "web" / "static"

    assert not re.search(r"<script(?![^>]*\bsrc=)", html), "inline <script> breaks the CSP"
    assert not re.search(r"<style\b", html), "inline <style> breaks the CSP"
    assert not re.search(r"\sstyle\s*=", html), "style= attribute breaks the CSP"
    assert not re.search(r"\son(click|load|error|mouse\w+)\s*=", html), \
        "inline event handler breaks the CSP -- assign it in dashboard.js instead"
    assert not re.search(r"https?://", (css_js_dir / "dashboard.css").read_text()), \
        "external URL in CSS breaks default-src 'self' -- fonts are self-hosted on purpose"
