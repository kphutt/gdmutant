"""Tests for action.yml's "Install gdmutant" step: which of PyPI vs git it installs from.

Extracts the step's actual embedded Python script straight from action.yml (so these can never
validate a copy that's drifted from what's really shipped) and executes it directly in-process,
with subprocess.run monkeypatched so no real pip install or network call ever happens. Testing the
real script by exec()-ing its extracted source, rather than shelling out to bash, sidesteps
cross-platform subprocess/argv fragility entirely -- there is no shell boundary left to get wrong.

What this does NOT exercise: GitHub's own `${{ }}` composite-action expression evaluation --
env: values here are plain env vars, not resolved through the Actions runner. `action-smoke.yml`
consumes the real action end to end, but it always sets `ref:` explicitly (it has to, since it's
testing this branch's own unreleased code), so it only ever exercises the pre-existing
escape-hatch path -- never this PR's new tag-derivation or SHA/branch-fallback logic through a
genuine Actions-runner evaluation. That gap is real and untracked as of this writing.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent

_ENV_KEYS = ("GDMUTANT_VERSION", "GDMUTANT_EXPLICIT_REF", "GDMUTANT_ACTION_REF")


def _install_step_python() -> str:
    """The exact Python source inside action.yml's "Install gdmutant" step's `python - <<'PY'`
    heredoc."""
    action = yaml.safe_load((REPO / "action.yml").read_text(encoding="utf-8"))
    steps = action["runs"]["steps"]
    install_step = next(step for step in steps if step["name"] == "Install gdmutant")
    run = install_step["run"]
    assert isinstance(run, str)
    match = re.search(r"<<'PY'\n(.*)\nPY", run, re.DOTALL)
    assert match, "could not find a python - <<'PY' ... PY heredoc in the install step"
    return match.group(1)


def _resolve(monkeypatch: pytest.MonkeyPatch, **env: str) -> tuple[list[str] | None, int]:
    """Run the extracted install-step script in-process. Returns the pip argv it would have
    invoked (None if the error path fired before ever reaching it) and the script's exit code."""
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    captured: dict[str, list[str]] = {}

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(SystemExit) as excinfo:
        exec(compile(_install_step_python(), "<install-step>", "exec"), {})
    return captured.get("cmd"), excinfo.value.code


def test_explicit_version_installs_from_pypi_regardless_of_ref(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cmd, code = _resolve(
        monkeypatch,
        GDMUTANT_VERSION="0.1.1",
        GDMUTANT_EXPLICIT_REF="some-branch",
        GDMUTANT_ACTION_REF="v9.9.9",
    )
    assert code == 0
    assert cmd == ["python", "-m", "pip", "install", "gdmutant==0.1.1"]


def test_explicit_version_strips_a_leading_v(monkeypatch: pytest.MonkeyPatch) -> None:
    cmd, code = _resolve(monkeypatch, GDMUTANT_VERSION="v0.1.1")
    assert code == 0
    assert cmd == ["python", "-m", "pip", "install", "gdmutant==0.1.1"]


def test_explicit_ref_installs_from_git_and_ignores_action_ref(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The escape hatch: an explicitly-set ref wins even when the Action's own invocation ref
    # looks like a perfectly good version tag -- explicit intent is never overridden.
    cmd, code = _resolve(
        monkeypatch, GDMUTANT_EXPLICIT_REF="my-test-branch", GDMUTANT_ACTION_REF="v0.1.1"
    )
    assert code == 0
    assert cmd == [
        "python",
        "-m",
        "pip",
        "install",
        "git+https://github.com/kphutt/gdmutant@my-test-branch",
    ]


def test_a_real_version_tag_derives_the_matching_pypi_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The common case: a consumer pinned `@v0.1.1` in their own uses: line.
    cmd, code = _resolve(monkeypatch, GDMUTANT_ACTION_REF="v0.1.1")
    assert code == 0
    assert cmd == ["python", "-m", "pip", "install", "gdmutant==0.1.1"]


def test_a_sha_pin_falls_back_to_git_install(monkeypatch: pytest.MonkeyPatch) -> None:
    # The documented, recommended way to pin (README/guide both show a commit SHA). PyPI has no
    # notion of an arbitrary commit, so this must still work via git-install, with no config.
    sha = "05728864a1c9330d632e2aab2348ff4442f3d61d"
    cmd, code = _resolve(monkeypatch, GDMUTANT_ACTION_REF=sha)
    assert code == 0
    assert cmd == [
        "python",
        "-m",
        "pip",
        "install",
        f"git+https://github.com/kphutt/gdmutant@{sha}",
    ]


def test_a_branch_ref_falls_back_to_git_install(monkeypatch: pytest.MonkeyPatch) -> None:
    cmd, code = _resolve(monkeypatch, GDMUTANT_ACTION_REF="main")
    assert code == 0
    assert cmd == ["python", "-m", "pip", "install", "git+https://github.com/kphutt/gdmutant@main"]


def test_no_ref_at_all_errors_clearly_without_ever_calling_pip_install(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A local `uses: ./` or otherwise-unversioned invocation: github.action_ref resolves empty,
    # and nothing else was set either. Must fail loudly, never fall through to some default.
    cmd, code = _resolve(monkeypatch)
    assert code == 1
    assert cmd is None
    assert "could not resolve a gdmutant ref to install" in capsys.readouterr().err
