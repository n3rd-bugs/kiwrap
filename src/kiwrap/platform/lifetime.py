"""Tie a child process's lifetime to this process (the router's JVM, any long helper).

Why: a Freerouting run is a multi-GB JVM that may legitimately work for hours. When the kiwrap
server dies underneath it (the MCP host terminates the server at session end, a crash, a
`Stop-Process`), a child started in its own process group keeps running as an orphan until its
pass budget is spent and nothing ever reaps it (Phase 1c finding: `java.exe` of the managed JRE
survived its dead parent, wrote the SES into `.kiwrap/tmp` with no consumer).

* Windows: the child is assigned to a Job Object created once per process with
  `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`. The job handle is never closed by kiwrap; the OS closes
  it when this process ends, orderly or not, and kills every assigned process with it. Nested
  jobs are allowed since Windows 8, so a host that already runs kiwrap inside a job (VS Code,
  a CI runner) does not break it. `CREATE_NEW_PROCESS_GROUP` keeps a console Ctrl+C in the
  parent from reaching the child (the tools cancel through their own kill path).
* POSIX: `prctl(PR_SET_PDEATHSIG, SIGTERM)` in the child before `exec` (the kernel signals the
  child when its parent THREAD dies, whatever killed it), then `setsid()` so the caller can
  `killpg` the whole tree. Designed-only on this Windows-first codebase (docs/DECISIONS.md D18).

This module is the only code outside `kiwrap.platform.__init__` / `system` that looks at
`sys.platform` (the platform-seam rule); callers pass the results to `subprocess.Popen`.
"""

from __future__ import annotations

import contextlib
import ctypes
import logging
import os
import signal
import subprocess
import sys
import threading
from typing import Any

log = logging.getLogger(__name__)

_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_PR_SET_PDEATHSIG = 1
_job_handle: Any = None
_job_lock = threading.Lock()


def _is_windows() -> bool:
    return sys.platform.startswith("win")


def _windows_job() -> Any:
    """The process-wide kill-on-close Job Object (created on first use), or None when the OS
    refused it (the caller then relies on its explicit kill paths alone)."""
    global _job_handle
    with _job_lock:
        if _job_handle is not None:
            return _job_handle or None
        try:
            kernel32 = getattr(ctypes, "windll").kernel32  # noqa: B009 - Windows only (mypy on Linux)
            handle = kernel32.CreateJobObjectW(None, None)
            if not handle:
                _job_handle = 0
                return None

            class _IoCounters(ctypes.Structure):
                _fields_ = [(n, ctypes.c_ulonglong) for n in ("r", "w", "o", "rb", "wb", "ob")]

            class _BasicLimits(ctypes.Structure):
                _fields_ = [
                    ("PerProcessUserTimeLimit", ctypes.c_int64),
                    ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", ctypes.c_uint32),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", ctypes.c_uint32),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", ctypes.c_uint32),
                    ("SchedulingClass", ctypes.c_uint32),
                ]

            class _ExtendedLimits(ctypes.Structure):
                _fields_ = [
                    ("BasicLimitInformation", _BasicLimits),
                    ("IoInfo", _IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t),
                ]

            info = _ExtendedLimits()
            info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            ok = kernel32.SetInformationJobObject(
                handle, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(info), ctypes.sizeof(info)
            )
            _job_handle = handle if ok else 0
        except Exception as exc:  # noqa: BLE001 - the explicit kill paths still work without it
            log.debug("Job Object unavailable: %s", exc)
            _job_handle = 0
        return _job_handle or None


def _posix_preexec() -> None:  # pragma: no cover - runs in the child, Linux only
    with contextlib.suppress(Exception):
        libc = ctypes.CDLL(None, use_errno=True)
        libc.prctl(_PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0)
    getattr(os, "setsid", lambda: None)()


def popen_lifetime_kwargs() -> dict[str, Any]:
    """Extra `subprocess.Popen` keywords: a new process group on Windows (Ctrl+C isolation; the
    job binding happens after the start through `bind_child_lifetime`), the PDEATHSIG + setsid
    hook on POSIX."""
    if _is_windows():
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {"preexec_fn": _posix_preexec}


def bind_child_lifetime(proc: subprocess.Popen[Any]) -> bool:
    """Make `proc` die with this process: Windows assigns it to the kill-on-close job (False when
    the job is unavailable or the assignment failed); POSIX children were bound at exec."""
    if not _is_windows():
        return True
    job = _windows_job()
    if job is None:
        return False
    try:
        kernel32 = getattr(ctypes, "windll").kernel32  # noqa: B009 - Windows only (mypy on Linux)
        proc_handle = getattr(proc, "_handle", 0)  # Popen's Windows process handle
        return bool(kernel32.AssignProcessToJobObject(job, int(proc_handle)))
    except Exception as exc:  # noqa: BLE001
        log.debug("AssignProcessToJobObject failed: %s", exc)
        return False


__all__ = ["bind_child_lifetime", "popen_lifetime_kwargs"]
