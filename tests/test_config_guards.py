"""Startup guards in config.py: B33 (unparseable .env) and B30 (session/login headroom).

Both guards run at MODULE level -- config.py sys.exit()s on import, deliberately, the same
way its existing MIN_LOGIN_INTERVAL_S floor does. That cannot be exercised in-process
(importing config here would kill the test run), so each case spawns a subprocess with its
own cwd and its own .env and asserts on the exit code and message.

`cwd` is the lever: config resolves its .env with find_dotenv(usecwd=True), so a tmp_path
containing a .env fully isolates each case from the repo's real one.
"""
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _subprocess_pythonpath() -> str:
    """Repo root, but preserving any outer PYTHONPATH ahead of it.

    That preservation is load-bearing, not tidiness: it is what allows the
    stash-the-fix-and-rerun validation this project uses on every regression test. Point
    PYTHONPATH at a directory holding a pre-fix config.py and it shadows the repo's, so
    these tests can be run against the broken code they were written for. Hardcoding the
    repo root here would make that impossible -- and would make these tests silently
    unfalsifiable, which is the failure mode the validation step exists to catch.
    """
    parts = [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]
    if str(REPO_ROOT) not in parts:
        parts.append(str(REPO_ROOT))
    return os.pathsep.join(parts)

# The minimum a .env needs for validate_core() to be satisfiable; these tests only care
# about the two module-level guards, which run before any of it is read.
BASE_ENV = (
    "TARGET_NAME=t\n"
    "TARGET_URL=https://example.invalid\n"
    "REQUIRED_TEXT=hello\n"
    "DASHBOARD_USER=u\n"
    "DASHBOARD_PASSWORD=p\n"
)


def run_config(tmp_path: Path, env_text: str, expr: str = "pass"):
    """Import config in a subprocess whose cwd holds `env_text` as .env, then eval `expr`."""
    (tmp_path / ".env").write_text(env_text, encoding="utf-8")
    return subprocess.run(
        [sys.executable, "-c", f"import config; {expr}"],
        cwd=tmp_path,
        env={"PYTHONPATH": _subprocess_pythonpath(), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
    )


# --- B33: an unparseable .env line must stop startup, not warn -----------------------

def test_unparseable_line_refuses_to_start(tmp_path):
    result = run_config(tmp_path, BASE_ENV + "this line has no equals sign\n")
    assert result.returncode == 1
    assert "could not be parsed" in result.stderr
    assert "line 6" in result.stderr, "must name the offending line so it can be found"


def test_unparseable_line_never_echoes_its_contents(tmp_path):
    """Rule 16 'secrets from .env only': a mangled line is most likely a mangled
    assignment, so its value half may be a live credential. Line numbers only."""
    secret = "hunter2-do-not-print-me"
    result = run_config(tmp_path, BASE_ENV + f'LOGIN_PASSWORD "{secret}"\n')
    assert result.returncode == 1
    assert secret not in result.stderr and secret not in result.stdout


def test_a_clean_env_still_starts(tmp_path):
    """Companion guard: the check must not reject ordinary files (comments, blanks)."""
    result = run_config(tmp_path, BASE_ENV + "\n# a real comment\n\nPORT=9090\n",
                        expr="print(config.PORT)")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "9090"


def test_guard_and_loader_read_the_same_file(tmp_path):
    """The bug found while building B33: find_dotenv(usecwd=True) walks from the process
    cwd while a bare load_dotenv() walks from config.py's own directory, so the guard could
    validate one .env while dotenv loaded another -- a clean bill of health for a file that
    was never read. Pinned by asserting the values actually come from the cwd .env and not
    from the repo's real one, which this subprocess can also see."""
    result = run_config(
        tmp_path, BASE_ENV + "SESSION_MAX_AGE_S=900\nLOGIN_INTERVAL_S=150\n",
        expr="print(config.SESSION_MAX_AGE_S, config.LOGIN_INTERVAL_S)",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "900 150"


# --- B30: the two clocks must not converge -------------------------------------------

def test_equal_values_are_refused(tmp_path):
    """600/600 is the pairing that produced five dead minutes of false session_expired
    on a healthy session (2026-08-26, 300.1s teardown skew)."""
    result = run_config(tmp_path, BASE_ENV + "SESSION_MAX_AGE_S=600\nLOGIN_INTERVAL_S=600\n")
    assert result.returncode == 1
    assert "headroom" in result.stderr


def test_the_old_defaults_pairing_is_refused(tmp_path):
    """1800/1800 was config.py's own fallback pair until B30 -- the guard has to reject the
    exact configuration this file used to hand out silently."""
    result = run_config(tmp_path, BASE_ENV + "SESSION_MAX_AGE_S=1800\nLOGIN_INTERVAL_S=1800\n")
    assert result.returncode == 1
    assert "headroom" in result.stderr


def test_shipped_values_are_accepted(tmp_path):
    result = run_config(tmp_path, BASE_ENV + "SESSION_MAX_AGE_S=600\nLOGIN_INTERVAL_S=120\n",
                        expr="print(config.SESSION_MAX_AGE_S - config.LOGIN_INTERVAL_S)")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "480"


def test_defaults_alone_satisfy_the_guard(tmp_path):
    """The point of B30: with neither variable set, the fallbacks must be a legal pair.
    Before the fix they were 1800/1800 -- which the guard above now rejects outright."""
    result = run_config(tmp_path, BASE_ENV,
                        expr="print(config.SESSION_MAX_AGE_S, config.LOGIN_INTERVAL_S)")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "600 120"


def test_headroom_is_measured_not_a_ratio(tmp_path):
    """The invariant is SESSION_MAX_AGE_S - LOGIN_INTERVAL_S >= the worst teardown skew we
    have actually measured (300s), because that is the quantity the dead window is made of.
    3600/3000 leaves 600s and passes despite the ratio being far worse than 600/300."""
    result = run_config(tmp_path, BASE_ENV + "SESSION_MAX_AGE_S=3600\nLOGIN_INTERVAL_S=3000\n")
    assert result.returncode == 0, result.stderr


def test_the_login_floor_still_applies(tmp_path):
    """Companion guard: B30's headroom check must not have displaced the pre-existing
    MIN_LOGIN_INTERVAL_S floor. 600/30 has plenty of headroom but is below the floor."""
    result = run_config(tmp_path, BASE_ENV + "SESSION_MAX_AGE_S=600\nLOGIN_INTERVAL_S=30\n")
    assert result.returncode == 1
    assert "floor" in result.stderr
