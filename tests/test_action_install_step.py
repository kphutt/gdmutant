"""Tests for action.yml's "Install gdmutant" step: which of PyPI vs git it installs from.

Extracts the step's actual bash script straight from action.yml (so these can never validate a
copy that's drifted from what's really shipped) and executes it for real, in bash, with a
`python` shell function shadowing the real one so no network call or real pip install ever
happens -- it just records the argument the script would have installed. A shell function
(rather than a stub executable on PATH) sidesteps chmod/executable-bit portability concerns
entirely, since Windows filesystems don't carry real POSIX exec bits.

What this does NOT exercise: GitHub's own `${{ }}` composite-action expression evaluation --
env: values here are plain env vars, not resolved through the Actions runner. That half is
covered by tests/test_action_pin.py (the `ref` input's own default) and the live
action-smoke.yml workflow, which consumes the real action end to end.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent


def _install_step_script() -> str:
    """The exact bash `run:` body of action.yml's "Install gdmutant" step."""
    action = yaml.safe_load((REPO / "action.yml").read_text(encoding="utf-8"))
    steps = action["runs"]["steps"]
    install_step = next(step for step in steps if step["name"] == "Install gdmutant")
    script = install_step["run"]
    assert isinstance(script, str)
    return script


@dataclass
class InstallResult:
    completed: subprocess.CompletedProcess[str]
    #: One line per `pip install <spec>` call the stub actually received (the `--upgrade pip`
    #: warm-up call is absorbed silently and never logged).
    pip_log: str


def _run_install_step(
    tmp_path: Path, *, version: str = "", explicit_ref: str = "", action_ref: str = ""
) -> InstallResult:
    log = tmp_path / "pip_log.txt"
    stub_python = f"""
python() {{
  if [ "$1" = "-m" ] && [ "$2" = "pip" ] && [ "$3" = "install" ]; then
    shift 3
    if [ "$1" = "--upgrade" ]; then
      return 0
    fi
    printf '%s\\n' "$1" >> {shlex.quote(str(log))}
    return 0
  fi
  echo "unexpected stub python invocation: $*" >&2
  return 1
}}
"""
    script = stub_python + "\n" + _install_step_script()
    env = dict(os.environ)
    env["GDMUTANT_VERSION"] = version
    env["GDMUTANT_EXPLICIT_REF"] = explicit_ref
    env["GDMUTANT_ACTION_REF"] = action_ref
    completed = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, env=env, timeout=30, check=False
    )
    pip_log = log.read_text(encoding="utf-8") if log.exists() else ""
    return InstallResult(completed=completed, pip_log=pip_log)


def test_explicit_version_installs_from_pypi_regardless_of_ref(tmp_path: Path) -> None:
    result = _run_install_step(
        tmp_path, version="0.1.1", explicit_ref="some-branch", action_ref="v9.9.9"
    )
    assert result.completed.returncode == 0, result.completed.stderr
    assert result.pip_log == "gdmutant==0.1.1\n"


def test_explicit_version_strips_a_leading_v(tmp_path: Path) -> None:
    result = _run_install_step(tmp_path, version="v0.1.1")
    assert result.completed.returncode == 0, result.completed.stderr
    assert result.pip_log == "gdmutant==0.1.1\n"


def test_explicit_ref_installs_from_git_and_ignores_action_ref(tmp_path: Path) -> None:
    # The escape hatch: an explicitly-set ref wins even when the Action's own invocation ref
    # looks like a perfectly good version tag -- explicit intent is never overridden.
    result = _run_install_step(tmp_path, explicit_ref="my-test-branch", action_ref="v0.1.1")
    assert result.completed.returncode == 0, result.completed.stderr
    assert result.pip_log == "git+https://github.com/kphutt/gdmutant@my-test-branch\n"


def test_a_real_version_tag_derives_the_matching_pypi_release(tmp_path: Path) -> None:
    # The common case: a consumer pinned `@v0.1.1` in their own uses: line.
    result = _run_install_step(tmp_path, action_ref="v0.1.1")
    assert result.completed.returncode == 0, result.completed.stderr
    assert result.pip_log == "gdmutant==0.1.1\n"


def test_a_sha_pin_falls_back_to_git_install(tmp_path: Path) -> None:
    # The documented, recommended way to pin (README/guide both show a commit SHA). PyPI has no
    # notion of an arbitrary commit, so this must still work via git-install, with no config.
    sha = "05728864a1c9330d632e2aab2348ff4442f3d61d"
    result = _run_install_step(tmp_path, action_ref=sha)
    assert result.completed.returncode == 0, result.completed.stderr
    assert result.pip_log == f"git+https://github.com/kphutt/gdmutant@{sha}\n"


def test_a_branch_ref_falls_back_to_git_install(tmp_path: Path) -> None:
    result = _run_install_step(tmp_path, action_ref="main")
    assert result.completed.returncode == 0, result.completed.stderr
    assert result.pip_log == "git+https://github.com/kphutt/gdmutant@main\n"


def test_no_ref_at_all_errors_clearly_without_ever_calling_pip_install(tmp_path: Path) -> None:
    # A local `uses: ./` or otherwise-unversioned invocation: github.action_ref resolves empty,
    # and nothing else was set either. Must fail loudly, never fall through to some default.
    result = _run_install_step(tmp_path)
    assert result.completed.returncode == 1
    assert "could not resolve a gdmutant ref to install" in result.completed.stderr
    assert result.pip_log == ""
