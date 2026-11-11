"""Start a command so that it and every process it starts can be stopped together, because a child
left running keeps files open in a folder gdmutant is about to delete. POSIX uses a process group.
Windows uses a Job Object, which still holds a child after its parent has exited."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from functools import cache
from typing import Any, Final

#: `CreateProcess` flag: the command runs no code until it is in its job, so all it starts is too.
_CREATE_SUSPENDED: Final = 0x00000004

#: How long `taskkill` may take, so a hung one cannot hang the run.
_TASKKILL_TIMEOUT: Final = 5.0


class _Win32:  # pragma: no cover - Windows-only bindings, exercised by the real tests there
    """The few Win32 calls a Job Object needs, with handle-sized argument and return types."""

    _kernel32: Any
    _ntdll: Any

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise OSError("Job Objects exist only on Windows")
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        ntdll = ctypes.WinDLL("ntdll")
        kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        # Undocumented but stable since Windows XP, and what psutil resumes a process with.
        ntdll.NtResumeProcess.argtypes = (wintypes.HANDLE,)
        ntdll.NtResumeProcess.restype = ctypes.c_long
        self._kernel32 = kernel32
        self._ntdll = ntdll

    def new_job(self) -> int | None:
        job: int | None = self._kernel32.CreateJobObjectW(None, None)
        return job or None

    def assign(self, job: int, process: int) -> bool:
        return bool(self._kernel32.AssignProcessToJobObject(job, process))

    def resume(self, process: int) -> None:
        self._ntdll.NtResumeProcess(process)

    def terminate(self, job: int) -> bool:
        return bool(self._kernel32.TerminateJobObject(job, 1))

    def close(self, job: int) -> None:
        self._kernel32.CloseHandle(job)


@cache
def _windows() -> _Win32:
    return _Win32()  # pragma: no cover - replaced by a fake in tests off Windows


@dataclass
class ProcessTree:
    """A started command, and the Windows Job Object holding it and its descendants, if any."""

    process: subprocess.Popen[bytes]
    job: int | None = None

    def kill(self) -> None:
        """Stop the command and everything it started, then the command itself in any case."""
        try:
            if sys.platform == "win32":
                if self.job is None or not _windows().terminate(self.job):
                    _taskkill(self.process.pid)
            else:
                with contextlib.suppress(ProcessLookupError):  # the whole group already exited
                    os.killpg(self.process.pid, signal.SIGKILL)
        finally:
            self.process.kill()

    def close(self) -> None:
        """Release the job handle. The processes in the job keep running."""
        if self.job is not None:
            _windows().close(self.job)
            self.job = None


def _taskkill(pid: int) -> None:
    # Without a job: finds the tree only through a command that is still alive.
    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=_TASKKILL_TIMEOUT,
        )


def start(command: Sequence[str], cwd: str) -> ProcessTree:
    """`subprocess.Popen` with both output streams piped, as a tree `ProcessTree.kill` can stop."""
    if sys.platform != "win32":
        process = subprocess.Popen(
            list(command),
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        return ProcessTree(process)
    api = _windows()
    job = api.new_job()
    try:
        process = subprocess.Popen(
            list(command),
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=0 if job is None else _CREATE_SUSPENDED,
        )
    except BaseException:
        if job is not None:
            api.close(job)
        raise
    if job is not None:
        handle = int(getattr(process, "_handle"))  # noqa: B009 - private, so typeshed omits it
        try:
            if not api.assign(job, handle):  # e.g. a parent job that forbids nesting
                api.close(job)
                job = None
        finally:
            api.resume(handle)  # never leave the command suspended
    return ProcessTree(process, job)
