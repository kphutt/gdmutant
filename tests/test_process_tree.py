"""Tests for starting and stopping a command's whole process tree, with both platforms faked.

The real-process tests, which run whichever platform this is, are in test_bounded_output.py."""

from __future__ import annotations

import subprocess
import threading
from dataclasses import dataclass, field

import pytest

from gdmutant.engine import process_tree
from gdmutant.engine.process_tree import ProcessTree


@dataclass
class _FakeProcess:
    pid: int = 4242
    _handle: int = 77
    killed: bool = False

    def kill(self) -> None:
        self.killed = True


@dataclass
class _FakeWin32:
    """Records every Job Object call, in order, with whether the process was killed yet."""

    job: int | None = 9
    assigns: bool = True
    terminates: bool = True
    calls: list[str] = field(default_factory=list)

    def new_job(self) -> int | None:
        self.calls.append("new_job")
        return self.job

    def assign(self, job: int, process: int) -> bool:
        self.calls.append(f"assign {job} {process}")
        return self.assigns

    def resume(self, process: int) -> None:
        self.calls.append(f"resume {process}")

    def terminate(self, job: int) -> bool:
        self.calls.append(f"terminate {job}")
        return self.terminates

    def close(self, job: int) -> None:
        self.calls.append(f"close {job}")


@pytest.fixture
def windows(monkeypatch: pytest.MonkeyPatch) -> _FakeWin32:
    api = _FakeWin32()
    monkeypatch.setattr(process_tree.sys, "platform", "win32")
    monkeypatch.setattr(process_tree, "_windows", lambda: api)
    return api


def _fake_popen(
    monkeypatch: pytest.MonkeyPatch, seen: dict[str, object], api: _FakeWin32 | None = None
) -> _FakeProcess:
    process = _FakeProcess()

    def popen(command: list[str], **kwargs: object) -> _FakeProcess:
        seen.update(kwargs)
        if api is not None:
            api.calls.append("popen")
        return process

    monkeypatch.setattr(process_tree.subprocess, "Popen", popen)
    return process


def _no_taskkill(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise AssertionError("taskkill ran although the job stopped the tree")

    monkeypatch.setattr(process_tree.subprocess, "run", fail)


# --- Windows: a Job Object ----------------------------------------------------------------------


def test_on_windows_the_command_is_in_its_job_before_it_runs(
    monkeypatch: pytest.MonkeyPatch, windows: _FakeWin32
) -> None:
    seen: dict[str, object] = {}
    _fake_popen(monkeypatch, seen, windows)
    tree = process_tree.start(["x"], ".")
    # Suspended until assigned, so nothing it starts can escape the job.
    assert seen["creationflags"] == process_tree._CREATE_SUSPENDED
    assert windows.calls == ["new_job", "popen", "assign 9 77", "resume 77"]
    assert tree.job == 9
    assert (seen["stdout"], seen["stderr"]) == (subprocess.PIPE, subprocess.PIPE)


def test_on_windows_a_job_the_command_cannot_join_falls_back_to_none(
    monkeypatch: pytest.MonkeyPatch, windows: _FakeWin32
) -> None:
    windows.assigns = False
    _fake_popen(monkeypatch, {}, windows)
    tree = process_tree.start(["x"], ".")
    assert windows.calls == ["new_job", "popen", "assign 9 77", "close 9", "resume 77"]
    assert tree.job is None


def test_on_windows_an_interrupted_assign_stops_the_command_and_releases_the_job(
    monkeypatch: pytest.MonkeyPatch, windows: _FakeWin32
) -> None:
    def interrupted(job: int, process: int) -> bool:
        windows.calls.append("assign")
        raise KeyboardInterrupt

    monkeypatch.setattr(windows, "assign", interrupted)
    _no_taskkill(monkeypatch)
    process = _fake_popen(monkeypatch, {}, windows)
    with pytest.raises(KeyboardInterrupt):
        process_tree.start(["x"], ".")
    assert windows.calls == ["new_job", "popen", "assign", "terminate 9", "close 9"]
    assert process.killed


def test_on_windows_a_failed_stop_after_a_failed_start_still_releases_the_job(
    monkeypatch: pytest.MonkeyPatch, windows: _FakeWin32
) -> None:
    def fails(*args: object) -> None:
        raise OSError("failed")

    monkeypatch.setattr(windows, "resume", fails)
    monkeypatch.setattr(windows, "terminate", fails)
    _fake_popen(monkeypatch, {}, windows)
    with pytest.raises(OSError, match="failed"):
        process_tree.start(["x"], ".")
    assert windows.calls[-1] == "close 9"


def test_on_windows_a_command_that_cannot_resume_is_stopped(
    monkeypatch: pytest.MonkeyPatch, windows: _FakeWin32
) -> None:
    def fails(process: int) -> None:
        windows.calls.append("resume")
        raise OSError("could not resume the test command")

    monkeypatch.setattr(windows, "resume", fails)
    _no_taskkill(monkeypatch)
    process = _fake_popen(monkeypatch, {}, windows)
    with pytest.raises(OSError, match="resume"):
        process_tree.start(["x"], ".")
    assert windows.calls[-3:] == ["resume", "terminate 9", "close 9"]
    assert process.killed


@pytest.mark.parametrize("failure", [OSError("no kernel32"), AttributeError("NtResumeProcess")])
def test_on_windows_without_the_win32_bindings_the_command_starts_without_a_job(
    monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    def unavailable() -> None:
        raise failure

    monkeypatch.setattr(process_tree.sys, "platform", "win32")
    monkeypatch.setattr(process_tree, "_windows", unavailable)
    seen: dict[str, object] = {}
    _fake_popen(monkeypatch, seen)
    tree = process_tree.start(["x"], ".")
    assert seen["creationflags"] == 0
    assert tree.job is None


def test_on_windows_no_job_starts_the_command_normally(
    monkeypatch: pytest.MonkeyPatch, windows: _FakeWin32
) -> None:
    windows.job = None
    seen: dict[str, object] = {}
    _fake_popen(monkeypatch, seen, windows)
    tree = process_tree.start(["x"], ".")
    assert seen["creationflags"] == 0
    assert windows.calls == ["new_job", "popen"]
    assert tree.job is None


@pytest.mark.parametrize(("job", "calls"), [(9, ["new_job", "close 9"]), (None, ["new_job"])])
def test_on_windows_a_command_that_cannot_start_releases_its_job(
    monkeypatch: pytest.MonkeyPatch, windows: _FakeWin32, job: int | None, calls: list[str]
) -> None:
    def missing(command: list[str], **kwargs: object) -> None:
        raise FileNotFoundError(command[0])

    windows.job = job
    monkeypatch.setattr(process_tree.subprocess, "Popen", missing)
    with pytest.raises(FileNotFoundError):
        process_tree.start(["x"], ".")
    assert windows.calls == calls


def test_on_windows_a_kill_terminates_the_job_then_the_command(
    monkeypatch: pytest.MonkeyPatch, windows: _FakeWin32
) -> None:
    _no_taskkill(monkeypatch)
    process = _FakeProcess()
    ProcessTree(process, job=9).kill()  # type: ignore[arg-type]
    assert windows.calls == ["terminate 9"]
    assert process.killed


@pytest.mark.parametrize(("job", "terminates"), [(None, True), (9, False)])
def test_on_windows_without_a_working_job_a_kill_uses_taskkill(
    monkeypatch: pytest.MonkeyPatch, windows: _FakeWin32, job: int | None, terminates: bool
) -> None:
    windows.terminates = terminates
    calls: list[object] = []
    process = _FakeProcess()
    monkeypatch.setattr(
        process_tree.subprocess,
        "run",
        lambda command, **kwargs: calls.append((command, kwargs["timeout"], process.killed)),
    )
    ProcessTree(process, job).kill()  # type: ignore[arg-type]
    # Before the kill: taskkill walks the tree from a process that is still alive.
    assert calls == [(["taskkill", "/T", "/F", "/PID", "4242"], 5.0, False)]
    assert process.killed


@pytest.mark.parametrize(
    "failure",
    [FileNotFoundError("taskkill"), subprocess.TimeoutExpired("taskkill", 5.0)],
    ids=["missing", "hung"],
)
def test_on_windows_a_failed_taskkill_still_kills_the_command(
    monkeypatch: pytest.MonkeyPatch, windows: _FakeWin32, failure: Exception
) -> None:
    def failing(*args: object, **kwargs: object) -> None:
        raise failure

    monkeypatch.setattr(process_tree.subprocess, "run", failing)
    process = _FakeProcess()
    ProcessTree(process).kill()  # type: ignore[arg-type]
    assert process.killed


def test_on_windows_a_ctrl_c_during_the_kill_still_kills_the_command(
    monkeypatch: pytest.MonkeyPatch, windows: _FakeWin32
) -> None:
    def interrupted(job: int) -> bool:
        raise KeyboardInterrupt

    monkeypatch.setattr(windows, "terminate", interrupted)
    process = _FakeProcess()
    with pytest.raises(KeyboardInterrupt):
        ProcessTree(process, job=9).kill()  # type: ignore[arg-type]
    assert process.killed


def test_closing_releases_the_job_once(windows: _FakeWin32) -> None:
    tree = ProcessTree(_FakeProcess(), job=9)  # type: ignore[arg-type]
    tree.close()
    tree.close()
    assert windows.calls == ["close 9"]
    assert tree.job is None


# --- elsewhere: a process group ------------------------------------------------------------------


def test_elsewhere_the_command_leads_a_process_group_of_its_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}
    monkeypatch.setattr(process_tree.sys, "platform", "linux")
    _fake_popen(monkeypatch, seen)
    tree = process_tree.start(["x"], ".")
    assert seen["start_new_session"] is True
    assert (seen["stdout"], seen["stderr"]) == (subprocess.PIPE, subprocess.PIPE)
    assert tree.job is None


@pytest.mark.parametrize(
    "failure", [None, ProcessLookupError(4242), PermissionError(1, "Operation not permitted")]
)
def test_elsewhere_a_kill_stops_the_process_group(
    monkeypatch: pytest.MonkeyPatch, failure: OSError | None
) -> None:
    # A group that already exited, or one with a member we may not signal, still kills the command.
    groups: list[tuple[int, int]] = []

    def killpg(pid: int, sig: int) -> None:
        groups.append((pid, sig))
        if failure is not None:
            raise failure

    process = _FakeProcess()
    monkeypatch.setattr(process_tree.sys, "platform", "linux")
    monkeypatch.setattr(process_tree.os, "killpg", killpg, raising=False)
    monkeypatch.setattr(process_tree.signal, "SIGKILL", 9, raising=False)
    ProcessTree(process).kill()  # type: ignore[arg-type]
    assert groups == [(4242, 9)]
    assert process.killed


def test_on_windows_closing_waits_for_a_kill_that_is_using_the_job(
    monkeypatch: pytest.MonkeyPatch, windows: _FakeWin32
) -> None:
    # Ctrl-C can kill on the main thread while the worker closes the same tree.
    terminating = threading.Event()
    finish = threading.Event()

    def slow_terminate(job: int) -> bool:
        windows.calls.append(f"terminate {job}")
        terminating.set()
        finish.wait(10)
        windows.calls.append("terminated")
        return True

    monkeypatch.setattr(windows, "terminate", slow_terminate)
    tree = ProcessTree(_FakeProcess(), job=9)  # type: ignore[arg-type]
    killer = threading.Thread(target=tree.kill)
    killer.start()
    terminating.wait(10)
    closer = threading.Thread(target=tree.close)
    closer.start()
    closer.join(0.3)
    assert closer.is_alive(), "the job was closed while a kill was using it"
    finish.set()
    killer.join(10)
    closer.join(10)
    assert windows.calls == ["terminate 9", "terminated", "close 9"]
