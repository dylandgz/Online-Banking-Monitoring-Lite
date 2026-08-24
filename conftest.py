"""Exists so the bare `pytest` command works from the repo root, not just `python -m pytest`.

Why this file is needed at all: `python -m pytest` puts the current directory on sys.path
(that's what `-m` does for any module), so `import monitor` resolves. The bare `pytest`
executable does not -- under its default "prepend" import mode it inserts each test file's
first non-package parent directory instead, which for tests/ (deliberately not a package --
no __init__.py) means `tests/` itself goes on sys.path and the repo root never does. Every
test module then fails collection with "No module named 'monitor'".

pytest always inserts the directory holding the topmost conftest.py into sys.path, so an
otherwise-empty conftest.py here is the standard, dependency-free fix: it makes the repo root
importable and both invocations behave identically. README.md and CONTRIBUTING.md both
document plain `pytest`, and that is what a CI runner or a reviewer will type -- before this,
that documented command failed at collection while the suite itself was green.

Kept intentionally free of fixtures: nothing here should influence test behavior, only
import resolution.
"""
