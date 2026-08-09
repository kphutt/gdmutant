"""Tests for action.yml's "Run gdmutant" step: how the Action's inputs become gdmutant's argv.

Extracts the step's actual embedded Python script straight from action.yml (so these can never
validate a copy that's drifted from what's really shipped) and executes it in-process, with
subprocess.run monkeypatched so gdmutant is never actually invoked. Same technique as
test_action_install_step.py -- see that file's docstring for why exec()-ing the extracted source
sidesteps cross-platform subprocess/argv fragility entirely.

What this does NOT exercise: GitHub's own `${{ }}` composite-action expression evaluation -- env:
values here are plain env vars, not resolved through the Actions runner.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent

_ENV_KEYS = (
    "INPUT_PATHS",
    "INPUT_PROJECT",
    "INPUT_RUNNER",
    "INPUT_TESTS",
    "INPUT_COMMAND",
    "INPUT_SINCE",
    "INPUT_ARGS",
    "INPUT_JOB_SUMMARY",
    "REPORT_JSON",
    "GITHUB_OUTPUT",
)

_REQUIRED = {
    "INPUT_PROJECT": "./",
    "INPUT_RUNNER": "gdunit4",
    "REPORT_JSON": "/tmp/gdmutant-report.json",
}


def _run_step_python() -> str:
    """The exact Python source inside action.yml's "Run gdmutant" step's `python - <<'PY'`
    heredoc."""
    action = yaml.safe_load((REPO / "action.yml").read_text(encoding="utf-8"))
    steps = action["runs"]["steps"]
    run_step = next(step for step in steps if step["name"] == "Run gdmutant")
    run = run_step["run"]
    assert isinstance(run, str)
    match = re.search(r"<<'PY'\n(.*)\nPY", run, re.DOTALL)
    assert match, "could not find a python - <<'PY' ... PY heredoc in the run step"
    return match.group(1)


def _assemble(monkeypatch: pytest.MonkeyPatch, **env: str) -> tuple[list[str], int]:
    """Run the extracted run-step script in-process. Returns the gdmutant argv it would have
    invoked and the script's exit code (always 0 here, since subprocess.run is faked to
    succeed)."""
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    for key, value in {**_REQUIRED, **env}.items():
        monkeypatch.setenv(key, value)

    captured: dict[str, list[str]] = {}

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(SystemExit) as excinfo:
        exec(compile(_run_step_python(), "<run-step>", "exec"), {})
    return captured["cmd"], excinfo.value.code


def test_minimal_inputs_default_to_the_whole_project(monkeypatch: pytest.MonkeyPatch) -> None:
    cmd, code = _assemble(monkeypatch)
    assert code == 0
    assert cmd == [
        "gdmutant",
        "run",
        "./",
        "--project",
        "./",
        "--runner",
        "gdunit4",
        "--json",
        "/tmp/gdmutant-report.json",
        "--report",
        "step-summary",
    ]


def test_command_input_adds_the_command_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    cmd, code = _assemble(
        monkeypatch,
        INPUT_RUNNER="command",
        INPUT_COMMAND="godot --headless --script res://tests/run_tests.gd",
    )
    assert code == 0
    assert "--command" in cmd
    idx = cmd.index("--command")
    assert cmd[idx + 1] == "godot --headless --script res://tests/run_tests.gd"


def test_empty_command_input_adds_no_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    cmd, code = _assemble(monkeypatch, INPUT_COMMAND="  ")
    assert code == 0
    assert "--command" not in cmd


def test_command_input_is_stripped(monkeypatch: pytest.MonkeyPatch) -> None:
    cmd, code = _assemble(monkeypatch, INPUT_RUNNER="command", INPUT_COMMAND="  godot --headless  ")
    assert code == 0
    idx = cmd.index("--command")
    assert cmd[idx + 1] == "godot --headless"


def test_args_can_still_override_after_command(monkeypatch: pytest.MonkeyPatch) -> None:
    # `command` is a first-class input now, but `args` stays the raw escape hatch: it's appended
    # last, so a consumer who needs to override or extend past what `command` expresses still can.
    cmd, code = _assemble(
        monkeypatch,
        INPUT_RUNNER="command",
        INPUT_COMMAND="godot --headless",
        INPUT_ARGS='--command "godot --headless --verbose"',
    )
    assert code == 0
    # Both --command flags are present, in order; argparse keeps the last one it sees.
    positions = [i for i, part in enumerate(cmd) if part == "--command"]
    assert len(positions) == 2
    assert positions[0] < positions[1]
    assert cmd[positions[1] + 1] == "godot --headless --verbose"


def test_tests_and_since_still_work_alongside_command(monkeypatch: pytest.MonkeyPatch) -> None:
    cmd, code = _assemble(
        monkeypatch,
        INPUT_TESTS="res://test/unit",
        INPUT_SINCE="origin/main",
    )
    assert code == 0
    assert "--tests" in cmd
    assert cmd[cmd.index("--tests") + 1] == "res://test/unit"
    assert "--since" in cmd
    assert cmd[cmd.index("--since") + 1] == "origin/main"
    assert "--command" not in cmd


def test_job_summary_false_skips_the_report_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    cmd, code = _assemble(monkeypatch, INPUT_JOB_SUMMARY="false")
    assert code == 0
    assert "--report" not in cmd
