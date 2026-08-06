"""Tests for GodotCommandRunner (ADR-0014): the exit-code contract of `CommandRunner`
(`tests/test_runner.py`), plus its own two Godot-aware behaviours — import-cache warm-up (shared
machinery, already exercised in depth by `test_gdunit_runner.py`; here just its wiring into this
class) and treating a runtime `SCRIPT ERROR` as a failure regardless of exit code.

`godot=sys.executable` stands in for a real Godot binary in most tests: `_warm_import_cache` never
checks the warm-up's exit code or output (see runner.py), so any real, fast executable satisfies it
without needing Godot installed."""

import subprocess
import sys
from pathlib import Path

import pytest

import gdmutant.adapters.gdscript.runner as runner_mod
from gdmutant.adapters.gdscript.runner import GodotCommandRunner
from gdmutant.engine.runner import Preparable, Runner, SuiteTimeout


def _exits(code: int) -> list[str]:
    return [sys.executable, "-c", f"import sys; sys.exit({code})"]


def _prints_then_exits(text: str, code: int, *, stderr: bool = False) -> list[str]:
    stream = "sys.stderr" if stderr else "sys.stdout"
    script = f"import sys; print({text!r}, file={stream}); sys.exit({code})"
    return [sys.executable, "-c", script]


def test_satisfies_the_runner_and_preparable_protocols() -> None:
    runner = GodotCommandRunner(command=_exits(0))
    assert isinstance(runner, Runner)
    assert isinstance(runner, Preparable)


def test_exit_zero_no_script_error_is_a_passing_suite(tmp_path: Path) -> None:
    result = GodotCommandRunner(command=_exits(0), godot=sys.executable).run(str(tmp_path))
    assert result.passed is True
    assert (result.tests, result.failures, result.errors) == (1, 0, 0)


def test_nonzero_exit_no_script_error_is_a_failing_suite(tmp_path: Path) -> None:
    cmd = _prints_then_exits("boom-on-stderr", 1, stderr=True)
    result = GodotCommandRunner(command=cmd, godot=sys.executable).run(str(tmp_path))
    assert result.failed is True
    assert (result.tests, result.failures, result.errors) == (1, 1, 0)
    assert "boom-on-stderr" in result.detail


def test_script_error_in_output_is_an_error_even_on_exit_zero(tmp_path: Path) -> None:
    # The one case CommandRunner can't catch (ADR-0014): a half-executed test that still exits 0.
    cmd = _prints_then_exits("SCRIPT ERROR: Nonexistent function 'foo'", 0)
    result = GodotCommandRunner(command=cmd, godot=sys.executable).run(str(tmp_path))
    assert result.failed is True
    assert (result.tests, result.failures, result.errors) == (1, 0, 1)
    assert "SCRIPT ERROR" in result.detail
    assert "GDScript has no exceptions" in result.detail


def test_script_error_on_stderr_is_also_caught(tmp_path: Path) -> None:
    cmd = _prints_then_exits("SCRIPT ERROR: boom", 0, stderr=True)
    result = GodotCommandRunner(command=cmd, godot=sys.executable).run(str(tmp_path))
    assert (result.failures, result.errors) == (0, 1)


def test_script_error_takes_precedence_over_a_nonzero_exit(tmp_path: Path) -> None:
    # A command that both errors AND exits non-zero is still reported via `errors`, not `failures`:
    # the SCRIPT ERROR is the more specific, more actionable diagnosis of the two.
    cmd = _prints_then_exits("SCRIPT ERROR: boom", 1)
    result = GodotCommandRunner(command=cmd, godot=sys.executable).run(str(tmp_path))
    assert (result.failures, result.errors) == (0, 1)


def test_timeout_raises_suite_timeout(tmp_path: Path) -> None:
    slow = [sys.executable, "-c", "import time; time.sleep(5)"]
    with pytest.raises(SuiteTimeout):
        GodotCommandRunner(command=slow, godot=sys.executable, timeout=0.2).run(str(tmp_path))


def test_missing_command_executable_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        GodotCommandRunner(command=["gdmutant-no-such-binary-xyz"], godot=sys.executable).run(
            str(tmp_path)
        )


def test_missing_godot_binary_for_warmup_raises_and_names_it(tmp_path: Path) -> None:
    # godot is only used for the import warm-up here — but a run still can't proceed without it,
    # and the error must name the actual attempted path (with_filename; see _warm_import_cache).
    with pytest.raises(FileNotFoundError) as excinfo:
        GodotCommandRunner(command=_exits(0), godot="gdmutant-no-such-godot-xyz").run(str(tmp_path))
    assert excinfo.value.filename == "gdmutant-no-such-godot-xyz"


def test_prepare_warms_the_import_cache_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"import": 0, "run": 0}

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        if "--import" in command:
            calls["import"] += 1
        else:
            calls["run"] += 1
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(runner_mod.subprocess, "run", fake_run)
    runner = GodotCommandRunner(command=["fake-test-cmd"], godot="fake-godot")
    runner.run(str(tmp_path))
    runner.run(str(tmp_path))
    assert calls["import"] == 1  # warmed once, not once per run
    assert calls["run"] == 2
