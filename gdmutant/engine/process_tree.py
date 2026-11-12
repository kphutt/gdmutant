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

#: `JOBOBJECT_EXTENDED_LIMIT_INFORMATION`'s class, and the flag that ends every process in a job
#: when its last handle closes: after the command finishes, or if gdmutant itself dies.
_EXTENDED_LIMIT_INFORMATION: Final = 9
_KILL_ON_JOB_CLOSE: Final = 0x2000


class _Win32:  # pragma: no cover - Windows-only bindings, exercised by the real tests there
    """The few Win32 calls a Job Object needs, with handle-sized argument and return types."""

    _kernel32: Any
    _ntdll: Any
    _limits: Any
    _ctypes: Any

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise OSError("Job Objects exist only on Windows")
        import ctypes
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = (
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            )

        class ExtendedLimits(ctypes.Structure):
            _fields_ = (
                ("BasicLimitInformation", BasicLimits),
                ("IoInfo", ctypes.c_uint64 * 6),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            )

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        ntdll = ctypes.WinDLL("ntdll")
        kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        )
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
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
        self._ctypes = ctypes
        self._limits = ExtendedLimits

    def new_job(self) -> int | None:
        job: int | None = self._kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        limits = self._limits()
        limits.BasicLimitInformation.LimitFlags = _KILL_ON_JOB_CLOSE
        if not self._kernel32.SetInformationJobObject(
            job,
            _EXTENDED_LIMIT_INFORMATION,
            self._ctypes.byref(limits),
            self._ctypes.sizeof(limits),
        ):
            self._kernel32.CloseHandle(job)
            return None
        return job

    def assign(self, job: int, process: int) -> bool:
        return bool(self._kernel32.AssignProcessToJobObject(job, process))

    def resume(self, process: int) -> None:
        status = self._ntdll.NtResumeProcess(process)
        if status < 0:  # an NTSTATUS error
            raise OSError(f"could not resume the test command (NTSTATUS {status & 0xFFFFFFFF:#x})")

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
        """Release the job handle, which also ends any process still in the job."""
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


def _new_job() -> tuple[_Win32 | None, int | None]:
    """The Win32 bindings and a new job, or Nones if either is unavailable (then `taskkill`)."""
    try:
        api = _windows()
    except (OSError, AttributeError):  # ctypes could not load a DLL or find a function
        return None, None
    return api, api.new_job()


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
    api, job = _new_job()
    try:
        process = subprocess.Popen(
            list(command),
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=0 if job is None else _CREATE_SUSPENDED,
        )
    except BaseException:
        if api is not None and job is not None:
            api.close(job)
        raise
    tree = ProcessTree(process, job)
    if api is None or job is None:
        return tree
    try:
        handle = int(getattr(process, "_handle"))  # noqa: B009 - private, so typeshed omits it
        if not api.assign(job, handle):  # e.g. a parent job that forbids nesting
            api.close(job)
            tree.job = None
        api.resume(handle)
    except BaseException:  # never leave the command suspended, or running outside any kill
        tree.kill()
        tree.close()
        raise
    return tree
