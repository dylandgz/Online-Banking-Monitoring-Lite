"""[AppScan XSS findings, 2026-09-22; rewritten 2026-09-23] dashboard.js builds no HTML.

AppScan rated four `innerHTML` assignments in dashboard.js as Critical reflected XSS. The
rating was wrong twice over -- nothing from the request reaches those sinks, so it was not
reflected, and every field actually rendered was a number, a code-defined enum, a server
timestamp or a UUID, so it was not exploitable. Two things were nevertheless true. The safety
was a *coincidence* maintained by hand: it held because every rendered field happened to
contain no markup, with nothing enforcing that it would stay that way. And a static scanner
cannot verify a human escaped every field, so it flags the sink itself -- meaning the finding
would have come back on every rescan for as long as an HTML string was being assigned.

The first answer to this was an esc() call on every interpolation, and this file used to
enforce that rule. It now enforces the stronger one that replaced it: dashboard.js builds the
page with document.createElement + textContent and never assembles HTML from a string at all.
Text set that way is character data the browser never parses as markup, so there is nothing
to escape and no sink for a scanner to flag.

The trap this closes. `/api/cycle/{id}` ships the whole `checks` row, so `page_url` and
`evidence_text` sit in the probe-detail payload right now, unrendered. `evidence_text` is the
live text of the monitored bank's role="alert" banner (journey.py `_visible_alert_text`,
B63) -- verbatim remote content. Under the old design, displaying it would have looked like a
one-line diagnostic improvement and been real stored XSS against an authenticated operator.
Under textContent that same edit is simply safe, which is the whole point of the rewrite.

What is still NOT safe, and is asserted below: urls. A `javascript:` url in an href executes
regardless of how it was set, so every url in the file must be a code-literal path plus, at
most, a server-generated id -- and none of the three remote-influenced fields may reach one.

This is a lint, not a behavioural test: it reads the source, needs no browser, and runs in
milliseconds with the rest of the suite. It cannot tell you the page still *looks* right --
that needs a real browser load, which is a manual acceptance step (see PROGRESS.md).
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

DASHBOARD_JS = Path(__file__).resolve().parents[1] / "monitor" / "web" / "static" / "dashboard.js"

# Every way a string can be handed to the HTML parser. `eval` is not an HTML sink but belongs
# to the same family of "this text becomes code" mistakes and the file has no use for it.
HTML_SINKS = (
    "innerHTML",
    "outerHTML",
    "insertAdjacentHTML",
    "document.write",
    "createContextualFragment",
    "eval(",
)


def _strip_comments(src: str) -> str:
    """Blank out whole-line `//` comments, preserving line numbering.

    Needed because dashboard.js's own header comment names the sinks it refuses to use -- and
    the docstrings here do the same. Without this, the lint flags the documentation explaining
    the lint. (The previous version of this file learned that the hard way on its first run.)

    Only whole-line comments are stripped. A trailing `//` after code would need real
    tokenising to tell from a `//` inside a string or URL, and no line in this file both
    matters to the lint and carries a trailing comment. If one ever does, this lint will flag
    it and the fix is to move the comment to its own line, not to loosen the scan."""
    return "\n".join("" if line.lstrip().startswith("//") else line
                     for line in src.splitlines())


def _code_lines() -> list[tuple[int, str]]:
    return [(i, line) for i, line in enumerate(_strip_comments(DASHBOARD_JS.read_text()).splitlines(), 1)
            if line.strip()]


@pytest.mark.parametrize("sink", HTML_SINKS)
def test_dashboard_js_contains_no_html_string_sink(sink):
    """The whole finding, in one assertion per sink.

    This is what makes the AppScan result stay closed: the pattern its rule matches on is not
    in the file. Do not add one back with an escaper in front of it -- a scanner cannot see
    the escaper, and neither can the next person editing the line."""
    offenders = [f"  dashboard.js:{n}  {line.strip()}" for n, line in _code_lines() if sink in line]
    assert not offenders, (
        f"dashboard.js assigns HTML through `{sink}`:\n" + "\n".join(offenders) + "\n\n"
        "Build the node with el()/textContent instead. Every value inserted as text is "
        "character data the browser never parses as markup, which is why there is no "
        "escaping function in this file any more."
    )


def test_attributes_are_set_as_properties_not_by_computed_name():
    """`el()` assigns `node[key] = value`. A `setAttribute(name, value)` with a variable name
    is the one way a data value could name its own attribute (`onclick`, `href`), so the file
    does not use it at all. If a future attribute genuinely needs setAttribute -- `colspan`
    does not, `colSpan` is the property -- it must take a literal name."""
    offenders = [f"  dashboard.js:{n}  {line.strip()}"
                 for n, line in _code_lines() if "setAttribute" in line]
    assert not offenders, (
        "dashboard.js uses setAttribute:\n" + "\n".join(offenders) + "\n\n"
        "Use el()'s attrs object (a property assignment) unless the attribute has no "
        "property form, and then pass a literal name."
    )


def test_every_url_is_built_from_a_literal_path():
    """textContent protects text; it does nothing for urls. A `javascript:` href executes no
    matter how it was assigned, so every href/src in this file must begin with a string
    literal starting `/` -- a same-origin path -- with anything dynamic appended after it."""
    pattern = re.compile(r"\b(href|src)\s*[:=]\s*(.+)$")
    offenders = []
    for n, line in _code_lines():
        m = pattern.search(line)
        if not m:
            continue
        value = m.group(2).strip()
        if not re.match(r"""^["']/""", value):
            offenders.append(f"  dashboard.js:{n}  {m.group(1)} = {value}")

    assert not offenders, (
        "url not built from a literal same-origin path:\n" + "\n".join(offenders) + "\n\n"
        "Start the value with a quoted path (\"/api/...\") and append the dynamic part. A "
        "bare server value here could carry a javascript: scheme, which no amount of text "
        "escaping prevents."
    )


@pytest.mark.parametrize("field", ["page_url", "evidence_text", "screenshot_path"])
def test_the_remote_influenced_fields_never_reach_a_url(field):
    """These three ship in the /api/cycle and /api/status payloads and are the fields an
    attacker could most plausibly author -- `evidence_text` is verbatim text from the
    monitored site's own role="alert" banner. Rendering them as *text* is now safe and needs
    no permission from this test. Putting one in an href or src is not, and never will be."""
    for n, line in _code_lines():
        if field in line and re.search(r"\b(href|src)\s*[:=]", line):
            pytest.fail(
                f"dashboard.js:{n} puts {field} into a url:\n  {line.strip()}\n"
                f"{field} is remote-influenced. Render it as text, or link to an "
                f"/api/ route that serves it instead."
            )


def test_dashboard_js_parses():
    """The file has no other automated coverage -- everything it does is DOM construction, so
    a syntax error would only ever surface as a blank dashboard. `node --check` is a cheap
    floor under that. It proves the file parses, NOT that the page renders correctly; the
    rendered output is a manual browser check."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed -- run `node --check monitor/web/static/dashboard.js`")
    result = subprocess.run([node, "--check", str(DASHBOARD_JS)], capture_output=True, text=True)
    assert result.returncode == 0, f"dashboard.js does not parse:\n{result.stderr}"


# --- the second wall: CSP [Option D alongside the DOM building above] ---------------

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
