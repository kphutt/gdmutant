"""Smoke tests for scripts/dev.py: pin that each subcommand is wired to the real commands it
claims to dispatch, and that a chain keeps running (and reports) even when one link fails.

This does not re-verify ruff/mypy/pytest/`uv build` themselves -- those tools already have their
own gates. What matters here is only that `dev.py` is a faithful dispatcher: it must never grow
logic of its own that could drift from `uv run ruff check .` / `uv run ruff format --check .` /
`uv run mypy gdmutant` / `uv run pytest` / `uv build`, so every check below asserts against the
exact argv `dev.py` hands to `subprocess.run`, not a paraphrase of it.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location("dev", REPO_ROOT / "scripts" / "dev.py")
assert _spec and _spec.loader
dev = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dev)


def _run(
    monkeypatch: pytest.MonkeyPatch, task: str, returncodes: list[int] | None = None
) -> tuple[int, list[list[str]]]:
    """Run `dev.main()` for `task` with subprocess.run faked out; return (exit code, calls made)."""
    calls: list[list[str]] = []
    codes = iter(returncodes or [])

    class _FakeResult:
        def __init__(self, returncode: int) -> None:
            self.returncode = returncode

    def fake_run(command: list[str], **_kwargs: object) -> _FakeResult:
        calls.append(command)
        return _FakeResult(next(codes, 0))

    monkeypatch.setattr(dev.subprocess, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["dev.py", task])
    return dev.main(), calls


@pytest.mark.parametrize("task", sorted(dev.COMMANDS))
def test_every_task_is_wired_to_at_least_one_command(task: str) -> None:
    assert dev.COMMANDS[task], (
        f"{task!r} has an empty command chain -- it would pass, having run nothing"
    )


def test_lint_dispatches_ruff_check_ruff_format_and_mypy_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exit_code, calls = _run(monkeypatch, "lint")
    assert calls == [
        ["uv", "run", "ruff", "check", "."],
        ["uv", "run", "ruff", "format", "--check", "."],
        ["uv", "run", "mypy", "gdmutant"],
    ]
    assert exit_code == 0


def test_test_dispatches_pytest(monkeypatch: pytest.MonkeyPatch) -> None:
    exit_code, calls = _run(monkeypatch, "test")
    assert calls == [["uv", "run", "pytest"]]
    assert exit_code == 0


def test_build_dispatches_uv_build(monkeypatch: pytest.MonkeyPatch) -> None:
    exit_code, calls = _run(monkeypatch, "build")
    assert calls == [["uv", "build"]]
    assert exit_code == 0


def test_a_failing_command_does_not_short_circuit_the_rest_of_the_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ruff check fails; ruff format --check and mypy must still run, and the overall exit must be
    non-zero -- matching verify_local.py's "report everything broken in one pass" behaviour."""
    exit_code, calls = _run(monkeypatch, "lint", returncodes=[1, 0, 0])
    assert len(calls) == 3, "a failed command must not stop the remaining ones from running"
    assert exit_code == 1


def test_unknown_task_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["dev.py", "nonexistent-task"])
    with pytest.raises(SystemExit):
        dev.main()
