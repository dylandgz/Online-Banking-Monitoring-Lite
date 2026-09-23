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
    """`el()` assigns through SETTERS, one literal property name per entry. A
    `setAttribute(name, value)` with a variable name is the other way a data value could name
    its own attribute (`onclick`, `href`), so the file does not use it at all. If a future
    attribute genuinely needs setAttribute -- `colspan` does not, `colSpan` is the property --
    it must take a literal name."""
    offenders = [f"  dashboard.js:{n}  {line.strip()}"
                 for n, line in _code_lines() if "setAttribute" in line]
    assert not offenders, (
        "dashboard.js uses setAttribute:\n" + "\n".join(offenders) + "\n\n"
        "Use el()'s attrs object (a property assignment) unless the attribute has no "
        "property form, and then pass a literal name."
    )


def _setters_block_lines() -> set[int]:
    """Line numbers of the SETTERS map.

    The url lint below has to skip them: `["href", (node, value) => { node.href = value; }]`
    is the *mechanism* of assignment, and the rule it would otherwise trip is about where the
    value is CHOSEN. That happens at the el() call site -- `href: "/api/artifact/" + id` --
    which the lint still checks."""
    src = _strip_comments(DASHBOARD_JS.read_text())
    start = src.index("const SETTERS = new Map([")
    end = src.index("]);", start)
    return set(range(src.count("\n", 0, start) + 1, src.count("\n", 0, end) + 2))


def _el_attr_keys(src: str) -> list[tuple[int, str]]:
    """Every attribute key passed to an el(...) call, as (line, key).

    Brace-matched from the `{` after the tag, so multi-line attrs objects are covered. Keys
    are the identifiers that follow `{` or `,` -- a ternary's `:` follows a value, never a
    separator, so `{ className: ok ? "a" : "b" }` yields just `className`."""
    out = []
    for m in re.finditer(r"\bel\(", src):
        i = src.find(",", m.end())
        if i == -1:
            continue
        j = i + 1
        while j < len(src) and src[j] in " \n\t":
            j += 1
        if j >= len(src) or src[j] != "{":
            continue                     # `null`, or a call spread across a shape we skip
        depth, k = 1, j + 1
        while k < len(src) and depth:
            if src[k] == "{":
                depth += 1
            elif src[k] == "}":
                depth -= 1
            k += 1
        obj = src[j:k]
        for key in re.findall(r"(?:^|[{,])\s*([A-Za-z_$][\w$]*)\s*:", obj):
            out.append((src.count("\n", 0, j) + 1, key))
    return out


def test_no_property_is_written_under_a_name_held_in_a_variable():
    """[AppScan prototype-pollution finding, 2026-09-23] `obj[key] = value` with a variable
    key is the shape of a prototype-pollution bug: a key of "__proto__" or "constructor"
    writes to the object everything else inherits from. el() used to do exactly that, with
    keys that were always literals -- safe, but only by inspection of every call site.

    Now every assignment names its property in full, in SETTERS. This asserts there is no
    line capable of writing a name that is not spelled out in the source."""
    offenders = [f"  dashboard.js:{n}  {line.strip()}" for n, line in _code_lines()
                 if re.search(r"\[\s*[A-Za-z_$][\w$]*\s*\]\s*=(?!=)", line)]
    assert not offenders, (
        "dashboard.js assigns to a computed property name:\n" + "\n".join(offenders) + "\n\n"
        "Add a literal setter to SETTERS instead. A write whose property name comes from a "
        "variable cannot be shown safe by reading the line it is on."
    )


def test_every_attribute_el_is_asked_to_set_has_a_literal_setter():
    """The other half: SETTERS skips a key it does not know, so a typo would silently drop an
    attribute and the page would render subtly wrong with nothing raised. That is deliberate
    -- a throw would blank the dashboard over a typo, and a console warning is its own scanner
    finding -- so the check lives here, where it fails at build time and names the key."""
    src = _strip_comments(DASHBOARD_JS.read_text())
    block = src.split("const SETTERS = new Map([", 1)[1].split("]);", 1)[0]
    known = set(re.findall(r'\["([A-Za-z_$][\w$]*)"', block))
    assert known, "SETTERS is empty or its shape changed"

    unknown = sorted({f"{key} (dashboard.js:{n})" for n, key in _el_attr_keys(src)
                      if key not in known})
    assert not unknown, (
        "el() is passed attributes with no setter, so they are silently dropped:\n  "
        + "\n  ".join(unknown)
        + f"\n\nKnown: {sorted(known)}. Add a literal setter for it, or fix the typo."
    )


def test_every_url_is_built_from_a_literal_path():
    """textContent protects text; it does nothing for urls. A `javascript:` href executes no
    matter how it was assigned, so every href/src in this file must begin with a string
    literal starting `/` -- a same-origin path -- with anything dynamic appended after it."""
    pattern = re.compile(r"\b(href|src)\s*[:=]\s*(.+)$")
    skip = _setters_block_lines()
    offenders = []
    for n, line in _code_lines():
        if n in skip:
            continue
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


def test_every_fetch_url_is_a_literal_path_with_an_encoded_dynamic_part():
    """[AppScan "SSRF" finding, 2026-09-23] The scanner flags any fetch whose url is built by
    concatenation rather than written out in full, because it cannot see where the appended
    value came from. Same rule as href/src, one step stricter: the literal path has to come
    first, and anything appended to it has to be encoded -- encodeURIComponent for a path
    segment, URLSearchParams.toString() for a query string. Both are sanitizers a reader (and
    a scanner) can recognise without tracing the value back to its source."""
    offenders = []
    for n, line in _code_lines():
        if "fetch(" not in line:
            continue
        arg = line.split("fetch(", 1)[1].strip()
        if not re.match(r"""^["']/""", arg):
            offenders.append(f"  dashboard.js:{n}  fetch({arg}  <- url does not start with a literal path")
        elif "+" in arg and not ("encodeURIComponent(" in arg or ".toString()" in arg):
            offenders.append(f"  dashboard.js:{n}  fetch({arg}  <- appends an unencoded value")

    assert not offenders, (
        "fetch url not built safely:\n" + "\n".join(offenders) + "\n\n"
        "Write the path as a literal and append only encodeURIComponent(x) or a "
        "URLSearchParams."
    )


def test_the_cycle_id_is_validated_before_it_is_used():
    """The check has to happen *before* the fetch, not inside it -- a guard placed after the
    request has already been made would satisfy a careless reading of the finding and none of
    its substance. Asserted by position, since that is the whole property."""
    src = _strip_comments(DASHBOARD_JS.read_text())
    assert "const CYCLE_ID = /" in src, "the cycle id pattern is gone"

    body = src.split("async function toggleProbes(", 1)[1].split("\nasync function", 1)[0]
    assert "CYCLE_ID.test(" in body, "toggleProbes does not validate the id it is given"
    assert body.index("CYCLE_ID.test(") < body.index("fetch("), (
        "the id is validated after the request is already sent"
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
