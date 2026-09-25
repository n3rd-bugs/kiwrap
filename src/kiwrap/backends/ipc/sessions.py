"""`HeadlessSession`: one sandboxed `kicad-cli api-server` per session, with guaranteed teardown.

What is verified (10.99.0-4083-g6dd893f7d1 nightly, Windows, 2026-09-19; probes in
scratchpad/nightly-probe/probe11b.py, probe11c.py and scratchpad/ipc-backend/probe1.py):
- `kicad-cli api-server --socket <path> [file]` runs headless (no GUI, no bundled python) and
  pre-loads a `.kicad_pcb`, `.kicad_sch` or `.kicad_pro`; it prints
  `KiCad API server listening at <path>` on stdout once it answers requests (before that the
  server replies AS_NOT_READY) and a load failure goes to stderr with exit code ERR_ARGS.
- Two concurrent servers (board + schematic) start in ~0.5 s and shut down cleanly.
- The server ignores `api.enable_server` in kicad_common.json (a sandbox root seeded with it false
  works), so a headless root needs no special seed.
- The socket is an nng `ipc://` endpoint; on Windows nng maps it to a named pipe and rejects long
  paths (docs/VERIFICATION.md claim 10: 126 chars OK, 146 fails). The socket therefore lives under
  `ConfigRoot.temp_dir()` (`<Platform.temp_dir>/k<digits>-<6 hex>/`, ~53 chars here) as
  `ipc-<pid>-<6 hex>.sock` (~75 chars) and `start()` refuses anything over `MAX_SOCKET_PATH`.
- Only `kicad-cli api-server` is started, never a GUI; the process runs with `ConfigRoot.env()`
  (KICAD_CONFIG_HOME / DOCUMENTS / CACHE under kiwrap's storage, short TEMP), so the user's real
  `%APPDATA%/kicad` is never seen; `tests/test_ipc_live.py` hashes it before and after.

Shape of the implementation:
- `_ApiServerProcess` mirrors `kipy.server.KiCadServer` (git f79cebd) and upstream's own
  `qa/tests/api/api_e2e_utils.h::API_SERVER_MANAGER` (connect + Ping until AS_OK, retry on
  AS_NOT_READY / connection refused, fail as soon as the process exits), but with kiwrap's env, cwd,
  socket location, process group and process-tree kill. It is composed, not subclassed: the upstream
  class hard-codes `subprocess.Popen(cmd, ...)` without `env`, and its `stop()` has no tree kill.
- Teardown: `terminate()` (SIGTERM on POSIX, which the server handles and shuts down cleanly;
  TerminateProcess on Windows, the same thing kipy and KiCad's QA fixture do) then, if the process
  is still alive, a process-tree kill (`taskkill /PID <pid> /T /F` on Windows via argv — never
  through a shell, Git Bash rewrites `/F` — or `killpg` of the server's own session on POSIX).
  `close()` runs from `__exit__`, `__del__`, and an `atexit` hook over the module registry, so a
  test or tool that forgets cannot leak a `kicad-cli.exe`.
- The sandbox root is NOT locked for the session's lifetime (sessions must run concurrently:
  board + schematic). The api-server rewrites `kicad_common.json` on exit like every kicad-cli
  process (claim 10), so `close()` restores the seed under a short lock instead.
- Capability gate: `ipc.headless` (capability.py docstring; matrix 11.0.0 / 10.99.0). Anything
  else raises `Unsupported(fallback="cli")` (KiCad 10 and older only offer IPC from a GUI, D2/D6).
- Orphans (reviewer finding 4, 2026-09-19: a child killed with `Popen.kill()` left its api-server
  and `~<name>.kicad_pro.lck` behind; `atexit` / `__del__` only run on an orderly interpreter
  exit, not on TerminateProcess from an MCP host or pytest-timeout's `os._exit(1)`): on Windows
  every server is assigned to a Job Object with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` held by this
  module, so the OS kills it when the last handle goes away with the parent; on Linux the child
  sets `prctl(PR_SET_PDEATHSIG, SIGTERM)`. Belt and braces, every server is recorded in
  `<Platform.temp_dir>/api-servers/<pid>.json` (pid, argv, socket, file, parent pid) and
  `sweep_orphans()` - run at every `start()` - kills recorded servers whose parent is gone
  (identified by kiwrap's own record and the `--socket <kiwrap temp dir>` argv, never by image
  name) and removes the stale `~*.lck` of that project after the kill is confirmed. The
  `CloseAllDocuments` in `close()` still matters: it is what releases the project lock on an
  orderly teardown; a server `close()` had to kill has its lock removed by `stop()` once the
  process is confirmed dead, and `release_project()` (the mutating tools' prologue) runs the
  sweep first so a kiwrap process that died with a server open does not leave the project
  refused with E_FILE_LOCKED.
- `SessionPool` (finding 14): `pooled_session()` keeps one server per (install, file) alive
  across tool calls instead of paying the start-up + load on every read (the jetson baseboard
  demo: 22 s per kw_pcb_get), closes it after `KIWRAP_IPC_IDLE_S` seconds idle (default 120),
  when the file changes on disk (mtime/size), on `release_project()` (the mutating tools call it
  before touching the project - the pooled server holds KiCad's project lock, finding 19) and at
  interpreter exit. Calls on one pooled server are serialised by a lock.
- Bound GUI sessions (Phase 2, `kiwrap.backends.ipc.gui_sessions`): while a sandboxed KiCad GUI
  launched by `kw_live` is alive for a project, it holds that project's files and its API server
  is the ONLY KiCad allowed on them ("one KiCad per project", docs/DISPLAY.md §7.4). The GUI
  registers itself with `bind_gui_session(project_dir, session)`; `pooled_session()` for a file
  under that directory then hands out a `_BoundHandle` on the GUI session instead of starting a
  headless server (a commit made through it repaints in the live view), `release_project()`
  leaves it alone and reports it under `bound` (the mutation prologue must then exempt the
  `~*.lck` files the GUI holds - they are listed there), and `close_all()` never closes it (the
  GUI's own supervisor does, on `kw_live detach` or at interpreter exit). Two rules keep the
  GUI's document and the disk in step (reviewer findings 2 and 9, 2026-09-20): the handle is
  only handed out for the document kind the session holds (`bound_serves_kind`; the other kind
  raises `BoundElsewhere`, an `Unsupported` the planner turns into "ipc11 unavailable", so the
  file engine answers), and a file written on disk behind the editor (the G16-guarded file
  writer, a SES import, a saved refill) is reloaded into it first (`resync_bound_session`:
  the disk stamp recorded at bind / after every handle differs -> `RevertDocument`).
"""

from __future__ import annotations

import atexit
import contextlib
import ctypes
import json
import logging
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

from kiwrap.backends.ipc import ensure_kipy_importable
from kiwrap.capability import Capabilities, Unsupported, capabilities_for, load_matrix
from kiwrap.installation import KiCadInstallation, version_tuple
from kiwrap.platform import current
from kiwrap.sandbox import ConfigRoot, ensure

__all__ = [
    "MAX_SOCKET_PATH",
    "BoundElsewhere",
    "BoundStale",
    "HeadlessSession",
    "bind_gui_session",
    "bound_serves_kind",
    "bound_session_for",
    "bound_sessions",
    "resync_bound_session",
    "unbind_gui_session",
    "IpcError",
    "SessionError",
    "SessionPool",
    "SessionStartError",
    "close_all",
    "live_sessions",
    "oldest_kicad_with",
    "pooled_session",
    "release_project",
    "require_capability",
    "sweep_orphans",
]

log = logging.getLogger(__name__)

#: Kept for callers that import it; the live limit is `kiwrap.platform.current().max_socket_path`
#: (120 on Windows named pipes, 107 on Linux AF_UNIX).
MAX_SOCKET_PATH = 120
READY_PREFIX = "KiCad API server listening at "
SERVER_RECORDS_DIRNAME = "api-servers"
DEFAULT_IDLE_TIMEOUT_S = 120.0
DEFAULT_READY_TIMEOUT_S = 30.0
DEFAULT_REQUEST_TIMEOUT_MS = 10_000
STOP_GRACE_S = 5.0
#: request timeout for the CloseAllDocuments sent by `close()` (verified: ~17 ms on the nightly)
RELEASE_TIMEOUT_MS = 2_000
CLIENT_NAME_PREFIX = "kiwrap"

_REGISTRY: dict[int, HeadlessSession] = {}
_REGISTRY_LOCK = threading.Lock()
#: project directory (resolved) -> the live GUI session bound to it (module docstring)
_BOUND: dict[Path, Any] = {}
_BOUND_LOCK = threading.Lock()


class IpcError(RuntimeError):
    """Teaching error shape shared by this backend: `code`, `why`, `fix` (core maps it to KiwrapError)."""

    code = "E_IPC"

    def __init__(self, message: str, *, why: str = "", fix: str = "", details: str = ""):
        super().__init__(message)
        self.why, self.fix, self.details = why, fix, details

    def to_json(self) -> dict[str, str]:
        return {
            "code": self.code,
            "message": str(self),
            "why": self.why,
            "fix": self.fix,
            "details": self.details,
        }


class SessionError(IpcError):
    code = "E_IPC_SESSION"


class SessionStartError(SessionError):
    """The api-server did not become ready: exited early (stderr captured) or timed out."""

    code = "E_IPC_START"


def oldest_kicad_with(key: str) -> str | None:
    """Lowest matrix version whose record has `key` truthy (for `Unsupported.oldest_kicad_with_it`).

    Nightlies (`x.99.y`) are skipped: they are previews of the next major, not a release a user
    can be told to install.
    """
    matrix = load_matrix()
    candidates = [v for v in matrix.known() if matrix.versions[v].get(key) and version_tuple(v)[1:2] != (99,)]
    return candidates[0] if candidates else None


def require_capability(caps: Capabilities, key: str, *, fallback: str) -> None:
    if not caps.has(key):
        raise Unsupported(key, caps.install, oldest_kicad_with_it=oldest_kicad_with(key), fallback=fallback)


def _drain(stream: IO[str] | None, sink: queue.Queue[str]) -> None:
    if stream is None:
        return
    with contextlib.suppress(ValueError, OSError):
        for line in stream:
            sink.put(line.rstrip("\r\n"))


def _kill_tree(pid: int) -> None:
    """Kill `pid` and every descendant. Windows: `taskkill /PID /T /F` through argv (no shell);
    POSIX: SIGKILL the process group the server was started in (`start_new_session=True`).
    Never raises: `subprocess.TimeoutExpired` (a wedged taskkill) is a `SubprocessError`, not an
    `OSError` (finding 21)."""
    if sys.platform.startswith("win"):
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=STOP_GRACE_S,
                check=False,
            )
        return
    with contextlib.suppress(OSError, ProcessLookupError):
        group = os.getpgid(pid)
        if group == os.getpgrp():
            # the child shares OUR process group (not started with start_new_session=True): a
            # killpg would take this server down too - and under pytest the whole test session
            os.kill(pid, signal.SIGKILL)
        else:
            os.killpg(group, signal.SIGKILL)


# --- parent-death safety net ------------------------------------------------------------------------

_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_PR_SET_PDEATHSIG = 1
_job_handle: Any = None
_job_lock = threading.Lock()


def _windows_job() -> Any:
    """The process-wide Job Object (created once) whose close kills every assigned server:
    `CreateJobObjectW` + `SetInformationJobObject(JobObjectExtendedLimitInformation,
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE)`. The handle is never closed by kiwrap; the OS closes it
    when this process dies, orderly or not, and takes the servers with it."""
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
        except Exception as exc:  # noqa: BLE001 - the sweep still covers orphans
            log.debug("Job Object unavailable: %s", exc)
            _job_handle = 0
        return _job_handle or None


def _assign_to_job(proc: subprocess.Popen[str]) -> bool:
    """Windows: put the server into the kill-on-close job (nested jobs are fine on Windows 8+,
    so a host that already runs kiwrap inside a job does not break this)."""
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


def _linux_preexec() -> None:  # pragma: no cover - runs in the child
    """`prctl(PR_SET_PDEATHSIG, SIGTERM)`: the kernel signals the server when its parent thread
    dies, whatever killed it. Then the usual new session for `killpg`."""
    with contextlib.suppress(Exception):
        libc = ctypes.CDLL(None, use_errno=True)
        libc.prctl(_PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0)
    getattr(os, "setsid", lambda: None)()  # POSIX only


def _records_dir(temp_dir: Path | None = None) -> Path:
    base = temp_dir if temp_dir is not None else current().temp_dir
    return base / SERVER_RECORDS_DIRNAME


def _pid_alive(pid: int) -> bool:
    if sys.platform.startswith("win"):
        kernel32 = getattr(ctypes, "windll").kernel32  # noqa: B009 - Windows only (mypy on Linux)
        handle = kernel32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == 259  # STILL_ACTIVE
            return True
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, OverflowError):
        # OverflowError: a pid beyond pid_t (a stale or foreign record) is no live process
        return False
    except PermissionError:
        return True
    return not _is_zombie(pid)


def _is_zombie(pid: int) -> bool:
    """A killed child its parent has not reaped yet still answers `kill(pid, 0)` on POSIX; it runs
    nothing and holds nothing, so it is not alive (`/proc/<pid>/stat` state `Z`; False wherever
    there is no procfs). Seen when the process that kills a GUI is also its parent (the ubuntu unit
    leg's stand-in child in tests/test_live_remote_unit.py)."""
    try:
        stat_line = Path(f"/proc/{int(pid)}/stat").read_text(encoding="ascii", errors="replace")
    except (OSError, ValueError):
        return False
    fields = stat_line.rpartition(")")[2].split()
    return bool(fields) and fields[0] == "Z"


def _command_line(pid: int) -> str:
    """The process's command line, to confirm a recorded pid still is OUR api-server / GUI before
    it is killed or bound (pids are recycled): `/proc/<pid>/cmdline` on Linux; on Windows the
    PEB's `ProcessParameters.CommandLine` read in-process (`NtQueryInformationProcess` +
    `ReadProcessMemory`, ~1 ms; reviewer finding 12/23, 2026-09-20: the PowerShell + WMI spawn
    cost 0.7-0.8 s per record on every discovery pass), falling back to
    `Win32_Process.CommandLine` via PowerShell when the process cannot be opened (an elevated
    KiCad, another user's process)."""
    if not sys.platform.startswith("win"):
        try:
            return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
        except OSError:
            return ""
    native = _command_line_native(pid)
    if native is not None:
        return native
    try:
        out = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f'(Get-CimInstance Win32_Process -Filter "ProcessId={int(pid)}").CommandLine',
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


class _ProcessBasicInformation(ctypes.Structure):  # pragma: no cover - Windows only
    _fields_ = [
        ("ExitStatus", ctypes.c_void_p),
        ("PebBaseAddress", ctypes.c_void_p),
        ("AffinityMask", ctypes.c_void_p),
        ("BasePriority", ctypes.c_void_p),
        ("UniqueProcessId", ctypes.c_void_p),
        ("InheritedFromUniqueProcessId", ctypes.c_void_p),
    ]


def _command_line_native(pid: int) -> str | None:  # pragma: no cover - Windows only
    """`PEB.ProcessParameters.CommandLine` of a 64-bit process read from a 64-bit Python (KiCad
    and kiwrap's interpreter are both x64 here, D18). None when anything fails (no access, a
    32-bit target, an exited process, a 32-bit interpreter): the caller falls back to WMI."""
    if not sys.platform.startswith("win") or ctypes.sizeof(ctypes.c_void_p) != 8:
        return None
    try:
        kernel32 = getattr(ctypes, "windll").kernel32  # noqa: B009
        ntdll = getattr(ctypes, "windll").ntdll  # noqa: B009
    except AttributeError:
        return None
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.ReadProcessMemory.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    ntdll.NtQueryInformationProcess.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
    ]
    handle = kernel32.OpenProcess(0x0400 | 0x0010, False, int(pid))  # QUERY_INFORMATION | VM_READ
    if not handle:
        return None
    try:
        pbi = _ProcessBasicInformation()
        got = ctypes.c_uint32()
        status = ntdll.NtQueryInformationProcess(
            handle, 0, ctypes.byref(pbi), ctypes.sizeof(pbi), ctypes.byref(got)
        )
        if status != 0 or not pbi.PebBaseAddress:
            return None

        def read(address: int, size: int) -> bytes | None:
            buf = ctypes.create_string_buffer(size)
            n = ctypes.c_size_t()
            if not kernel32.ReadProcessMemory(handle, ctypes.c_void_p(address), buf, size, ctypes.byref(n)):
                return None
            return buf.raw[: n.value]

        # PEB64.ProcessParameters at 0x20; RTL_USER_PROCESS_PARAMETERS64.CommandLine (a
        # UNICODE_STRING: USHORT Length, USHORT MaximumLength, 4 pad, PWSTR Buffer) at 0x70
        raw = read(int(pbi.PebBaseAddress) + 0x20, 8)
        if raw is None or len(raw) < 8:
            return None
        params = int.from_bytes(raw, "little")
        if not params:
            return None
        ustr = read(params + 0x70, 16)
        if ustr is None or len(ustr) < 16:
            return None
        length = int.from_bytes(ustr[0:2], "little")
        buffer = int.from_bytes(ustr[8:16], "little")
        if not buffer or length == 0:
            return ""
        data = read(buffer, min(length, 32 * 1024))
        if data is None:
            return None
        return data.decode("utf-16-le", "replace")
    except Exception:  # noqa: BLE001 - any failure means "take the slow path"
        return None
    finally:
        kernel32.CloseHandle(handle)


def _write_record(
    temp_dir: Path,
    pid: int,
    argv: list[str],
    socket_path: Path,
    file: Path | None,
    *,
    started: float | None = None,
) -> Path:
    d = _records_dir(temp_dir)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{pid}.json"
    payload = {
        "pid": pid,
        "parent_pid": os.getpid(),
        "argv": argv,
        "socket": str(socket_path),
        "file": str(file) if file is not None else None,
        "started": time.time() if started is None else started,
    }
    with contextlib.suppress(OSError):
        path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def sweep_orphans(temp_dir: Path | None = None) -> list[int]:
    """Kill api-servers recorded under `<temp_dir>/api-servers/` whose parent process is gone
    (or is this process but the session is no longer registered), when their command line still
    is `kicad-cli api-server --socket <a socket under kiwrap's temp dir>`; then remove the stale
    `~*.lck` of the project the record names. Returns the pids killed. Records of dead servers
    are deleted. Never raises."""
    killed: list[int] = []
    try:
        d = _records_dir(temp_dir)
        if not d.is_dir():
            return killed
        base = str(d.parent)
        live_pids = {s.pid for s in live_sessions() if s.pid is not None}
        for rec in sorted(d.glob("*.json")):
            try:
                info = json.loads(rec.read_text(encoding="utf-8"))
                pid, parent = int(info["pid"]), int(info.get("parent_pid", 0))
            except (OSError, ValueError, KeyError, TypeError):
                with contextlib.suppress(OSError):
                    rec.unlink()
                continue
            if not _pid_alive(pid):
                # died without a stop() (the Job Object / PDEATHSIG took it with its parent):
                # its KiCad lock was never released
                _remove_stale_lock(info, pid)
                with contextlib.suppress(OSError):
                    rec.unlink()
                continue
            orphan = not _pid_alive(parent) or (parent == os.getpid() and pid not in live_pids)
            if not orphan:
                continue
            cmd = _command_line(pid)
            if "api-server" not in cmd or base.lower() not in cmd.lower():
                continue  # the pid was recycled by something else: leave it alone, drop the record
            _kill_tree(pid)
            for _ in range(50):
                if not _pid_alive(pid):
                    break
                time.sleep(0.1)
            if _pid_alive(pid):
                continue
            killed.append(pid)
            _remove_stale_lock(info, pid)
            with contextlib.suppress(OSError):
                rec.unlink()
    except Exception as exc:  # noqa: BLE001 - housekeeping must never break a session start
        log.debug("orphan sweep failed: %s", exc)
    return killed


def _remove_stale_lock(info: dict[str, Any], pid: int) -> None:
    """Delete the `~<name>.kicad_pro.lck` a dead kiwrap server left in the project directory of
    the file it had open - only when the lock was written around that server's start (the server
    takes the lock while it pre-loads the file): a KiCad GUI that took the project over
    afterwards rewrites the lock, one that held the project before the server started predates
    it; both stay."""
    file = info.get("file")
    started = float(info.get("started") or 0)
    if not file:
        return
    for lck in Path(file).parent.glob("~*.lck"):
        try:
            mtime = lck.stat().st_mtime
            if started and not (started - 5 <= mtime <= started + 60):
                continue
            lck.unlink()
            log.info("removed stale %s left by dead api-server %s", lck.name, pid)
        except OSError:
            continue


@dataclass
class _ApiServerProcess:
    """The `kicad-cli api-server` subprocess plus its stdout/stderr drains (see module docstring)."""

    kicad_cli: Path
    socket_path: Path
    env: dict[str, str]
    cwd: Path
    file: Path | None = None
    process: subprocess.Popen[str] | None = None
    stdout_lines: list[str] = field(default_factory=list)
    stderr_lines: list[str] = field(default_factory=list)
    ready_line: str | None = None
    in_job: bool = False  # Windows: assigned to the kill-on-close Job Object
    record: Path | None = None  # `<temp>/api-servers/<pid>.json`, for sweep_orphans()
    started_epoch: float = 0.0  # `time.time()` at start(), the record's `started`
    _stdout_q: queue.Queue[str] = field(default_factory=queue.Queue, repr=False)
    _stderr_q: queue.Queue[str] = field(default_factory=queue.Queue, repr=False)

    @property
    def socket_url(self) -> str:
        return f"ipc://{self.socket_path}"

    @property
    def argv(self) -> list[str]:
        argv = [str(self.kicad_cli), "api-server", "--socket", str(self.socket_path)]
        if self.file is not None:
            argv.append(str(self.file))
        return argv

    @property
    def alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    @property
    def pid(self) -> int | None:
        return self.process.pid if self.process is not None else None

    def start(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            self.socket_path.unlink()
        popen_kw: dict[str, Any] = {}
        if sys.platform.startswith("win"):
            # Own process group: a Ctrl+C in the parent console does not reach the server, and
            # taskkill /T can address the tree by the server's pid.
            popen_kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kw["preexec_fn"] = _linux_preexec  # PDEATHSIG + setsid (= start_new_session)
        self.process = subprocess.Popen(
            self.argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=self.env,
            cwd=str(self.cwd),
            **popen_kw,
        )
        if sys.platform.startswith("win"):
            self.in_job = _assign_to_job(self.process)
        self.started_epoch = time.time()
        self.record = _write_record(
            self.socket_path.parent.parent,
            self.process.pid,
            self.argv,
            self.socket_path,
            self.file,
            started=self.started_epoch,
        )
        for stream, sink in ((self.process.stdout, self._stdout_q), (self.process.stderr, self._stderr_q)):
            threading.Thread(target=_drain, args=(stream, sink), daemon=True).start()

    def pump(self) -> None:
        """Move drained lines into the lists; note the ready line (and the socket it announces)."""
        for q, lines in ((self._stdout_q, self.stdout_lines), (self._stderr_q, self.stderr_lines)):
            while True:
                try:
                    line = q.get_nowait()
                except queue.Empty:
                    break
                lines.append(line)
                if line.startswith(READY_PREFIX):
                    self.ready_line = line
                    self.socket_path = Path(line[len(READY_PREFIX) :].strip())

    def wait_ready(self, timeout_s: float, *, request_timeout_ms: int) -> None:
        """Connect + Ping until AS_OK. `AS_NOT_READY` and connection failures are retried; process
        exit or the deadline raise `SessionStartError` with both streams captured."""
        from google.protobuf.empty_pb2 import Empty
        from kipy.client import KiCadClient
        from kipy.errors import ApiError, ConnectionError
        from kipy.proto.common import commands

        deadline = time.monotonic() + timeout_s
        last_error = ""
        while time.monotonic() < deadline:
            self.pump()
            if not self.alive:
                self.pump()
                raise SessionStartError(
                    f"kicad-cli api-server exited with status {self.process.returncode if self.process else '?'} "
                    "before it was ready",
                    why="the server could not start or could not load the requested file",
                    fix="check the file path and that it is a KiCad board/schematic/project of this version; "
                    "the captured stderr says what failed",
                    details=self._transcript(last_error),
                )
            probe = KiCadClient(
                self.socket_url, f"{CLIENT_NAME_PREFIX}.ready-probe", "", min(request_timeout_ms, 1000)
            )
            try:
                probe.send(commands.Ping(), Empty)
                probe.close()
                self.pump()
                return
            except (ConnectionError, ApiError) as e:  # AS_NOT_READY comes back as ApiError
                last_error = str(e)
                probe.close()
            time.sleep(0.05)
        self.stop()
        raise SessionStartError(
            f"timed out after {timeout_s:.0f} s waiting for kicad-cli api-server readiness",
            why="the server process is alive but never answered Ping with AS_OK",
            fix="raise `ready_timeout_s` for very large projects, or check the transcript for a stuck load",
            details=self._transcript(last_error),
        )

    def _transcript(self, last_error: str = "") -> str:
        parts = [f"argv: {self.argv}"]
        if self.stdout_lines:
            parts += ["stdout:", *self.stdout_lines[-40:]]
        if self.stderr_lines:
            parts += ["stderr:", *self.stderr_lines[-40:]]
        if last_error:
            parts += ["last probe error:", last_error]
        return "\n".join(parts)

    def stop(self) -> None:
        """Terminate, then tree-kill, then `kill()`. Never raises. `self.process` is dropped only
        once the process is confirmed dead: a server that survives every attempt stays known
        (`alive` is still true) so `close_all()` / the orphan sweep can retry (finding 21)."""
        proc = self.process
        if proc is None:
            return
        try:
            if proc.poll() is None:
                with contextlib.suppress(OSError):
                    proc.terminate()  # SIGTERM (handled: clean shutdown) / TerminateProcess
                with contextlib.suppress(subprocess.SubprocessError):
                    proc.wait(timeout=STOP_GRACE_S)
            if proc.poll() is None:
                _kill_tree(proc.pid)
                with contextlib.suppress(subprocess.SubprocessError):
                    proc.wait(timeout=STOP_GRACE_S)
            if proc.poll() is None:  # pragma: no cover - last resort
                with contextlib.suppress(OSError):
                    proc.kill()
                with contextlib.suppress(subprocess.SubprocessError):
                    proc.wait(timeout=STOP_GRACE_S)
        finally:
            self.pump()
            if proc.poll() is not None:
                self.process = None
                for stream in (proc.stdout, proc.stderr):
                    with contextlib.suppress(OSError, ValueError):
                        if stream is not None:
                            stream.close()
                with contextlib.suppress(OSError):
                    self.socket_path.unlink()
                # A server that was killed instead of closed (CloseAllDocuments failed or timed
                # out: the wedged-server case) never released KiCad's `~<name>.kicad_pro.lck`;
                # it is removed here, while the record still exists, so that no E_FILE_LOCKED
                # asks a human to delete a file (reviewer finding, 2026-09-20).
                _remove_stale_lock(
                    {"file": str(self.file) if self.file else None, "started": self.started_epoch}, proc.pid
                )
                if self.record is not None:
                    with contextlib.suppress(OSError):
                        self.record.unlink()
                    self.record = None
            else:
                log.warning("kicad-cli api-server pid %s survived stop(); it stays registered", proc.pid)


class HeadlessSession:
    """Context manager around one headless api-server bound to one file (or none).

    >>> with HeadlessSession(install, board_path, config_root=root) as s:
    ...     board = s.board()          # kipy.board.Board
    ...     model = read_board(s)      # kiwrap dataclasses, millimetres

    `install` must have `ipc.headless` (else `Unsupported`, fallback "cli"); `file` may be a
    `.kicad_pcb`, `.kicad_sch` or `.kicad_pro` (the server pre-loads it) or None (open documents
    later with `open_document`). `config_root` defaults to `kiwrap.sandbox.ensure(install)`.
    """

    def __init__(
        self,
        install: KiCadInstallation,
        file: str | Path | None = None,
        *,
        config_root: ConfigRoot | None = None,
        capabilities: Capabilities | None = None,
        ready_timeout_s: float = DEFAULT_READY_TIMEOUT_S,
        request_timeout_ms: int = DEFAULT_REQUEST_TIMEOUT_MS,
        client_name: str | None = None,
    ):
        self.install = install
        self.file = Path(file).resolve() if file is not None else None
        self._config = config_root
        self._caps = capabilities
        self.ready_timeout_s = ready_timeout_s
        self.request_timeout_ms = request_timeout_ms
        self.client_name = client_name or f"{CLIENT_NAME_PREFIX}-{os.getpid()}-{os.urandom(3).hex()}"
        self._server: _ApiServerProcess | None = None
        self._kicad: Any = None  # kipy.KiCad
        self.started_at: float | None = None
        self._presence_probed = False  # docs/UPSTREAM.md §3.3 probes ran (once per session)

    # -- lifecycle ------------------------------------------------------------------------------

    @property
    def config(self) -> ConfigRoot:
        if self._config is None:
            self._config = ensure(self.install)
        return self._config

    @property
    def capabilities(self) -> Capabilities:
        if self._caps is None:
            self._caps = capabilities_for(self.install)
        return self._caps

    @property
    def running(self) -> bool:
        return self._server is not None and self._server.alive

    @property
    def pid(self) -> int | None:
        return self._server.pid if self._server else None

    @property
    def socket_path(self) -> Path | None:
        return self._server.socket_path if self._server else None

    @property
    def socket_url(self) -> str | None:
        return self._server.socket_url if self._server else None

    def transcript(self) -> str:
        """stdout/stderr captured so far (for teaching errors and test diagnostics)."""
        if self._server is None:
            return ""
        self._server.pump()
        return self._server._transcript()

    def _socket_for(self) -> Path:
        platform = current()
        temp = self.config.temp_dir(platform)
        path = temp / f"ipc-{os.getpid()}-{os.urandom(3).hex()}.sock"
        limit = platform.max_socket_path
        if len(str(path)) > limit:
            raise SessionError(
                f"IPC socket path is {len(str(path))} chars, over the {limit}-char limit of this OS: {path}",
                why=(
                    "nng named pipes on Windows reject long paths (verified: 126 OK, 146 fails)"
                    if platform.is_windows
                    else "an AF_UNIX sun_path holds 108 bytes including the NUL"
                ),
                fix="set KIWRAP_HOME (or the platform temp dir) to a shorter path",
            )
        return path

    def start(self) -> HeadlessSession:
        if self.running:
            return self
        ensure_kipy_importable()
        _register_atexit()
        require_capability(self.capabilities, "ipc.headless", fallback="cli")
        if self.file is not None and not self.file.is_file():
            raise SessionStartError(
                f"file not found: {self.file}",
                why="the api-server pre-loads the file at start-up",
                fix="pass an existing .kicad_pcb / .kicad_sch / .kicad_pro path",
            )
        config = self.config
        env = config.env()
        sweep_orphans(current().temp_dir)
        server = _ApiServerProcess(
            kicad_cli=self.install.kicad_cli,
            socket_path=self._socket_for(),
            env=env,
            cwd=config.temp_dir(current()),
            file=self.file,
        )
        self._server = server
        with _REGISTRY_LOCK:
            _REGISTRY[id(self)] = self
        try:
            server.start()
            server.wait_ready(self.ready_timeout_s, request_timeout_ms=self.request_timeout_ms)
        except BaseException:
            self.close()
            raise
        self.started_at = time.monotonic()
        return self

    def close(self) -> None:
        """Stop the server (idempotent). Never raises.

        Before the process is stopped the session sends `CloseAllDocuments` (best effort, short
        timeout): the headless server takes KiCad's project lock (`~<name>.kicad_pro.lck` next to
        the project, JSON with hostname/username/token) when it pre-loads a document, and
        `TerminateProcess` on Windows gives it no chance to release it, so without this step a
        stale lock stays behind and every later kicad-cli/edit tool refuses the project with
        E_FILE_LOCKED. Verified 2026-09-19 on 10.99.0-4083 (scratchpad integrator/probe_close.py):
        the lock exists while a .kicad_sch, .kicad_pcb or .kicad_pro is open, `CloseAllDocuments`
        answers AS_OK in ~17 ms and removes it, and nothing is left after `stop()`. When the
        release fails (a wedged server that has to be killed), `stop()` deletes the lock the dead
        process left, provided it is not newer than the server's start (a GUI that took the
        project over meanwhile keeps its own lock).
        """
        server, self._server = self._server, None
        kicad, self._kicad = self._kicad, None
        with _REGISTRY_LOCK:
            _REGISTRY.pop(id(self), None)
        if server is not None and server.alive:
            self._release_documents(server)
        if kicad is not None:
            with contextlib.suppress(Exception):
                kicad._client.close()
        if server is None:
            return
        try:
            server.stop()
        finally:
            if self._config is not None:
                # kicad-cli rewrites kicad_common.json on exit (claim 10): put the seed back.
                with contextlib.suppress(Exception), self._config.lock(timeout_s=STOP_GRACE_S):
                    self._config.restore_seed()

    @staticmethod
    def _release_documents(server: _ApiServerProcess) -> None:
        """`CloseAllDocuments` over a throw-away client with its own short timeout (never the
        session's client: a wedged server - the UpdateItems hang recorded in the capability
        matrix - would otherwise hold `close()` for the full request timeout) so the server
        drops its project lock file. Failures are ignored: the process is terminated right after
        and a lock that survives is reported by the tools as E_FILE_LOCKED."""
        from google.protobuf.empty_pb2 import Empty
        from kipy.client import KiCadClient
        from kipy.proto.common.commands import project_commands_pb2

        client: Any = None
        try:
            client = KiCadClient(server.socket_url, f"{CLIENT_NAME_PREFIX}.close", "", RELEASE_TIMEOUT_MS)
            client.send(project_commands_pb2.CloseAllDocuments(), Empty)
        except Exception as exc:  # noqa: BLE001 - best effort; the process is stopped next
            log.debug("CloseAllDocuments before stop failed: %s", exc)
        finally:
            if client is not None:
                with contextlib.suppress(Exception):
                    client.close()

    def __enter__(self) -> HeadlessSession:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __del__(self) -> None:  # pragma: no cover - safety net
        with contextlib.suppress(Exception):
            self.close()

    # -- client ---------------------------------------------------------------------------------

    def kicad(self) -> Any:
        """The `kipy.KiCad` facade bound to this server (its socket, never the global default)."""
        if not self.running:
            raise SessionError(
                "session is not running", fix="use `with HeadlessSession(...) as s:` or call start()"
            )
        if self._kicad is None:
            from kipy.client import KiCadClient
            from kipy.kicad import KiCad

            assert self._server is not None
            client = KiCadClient(self._server.socket_url, self.client_name, "", self.request_timeout_ms)
            self._kicad = KiCad.from_client(client)
        return self._kicad

    def client(self) -> Any:
        """The raw `kipy.client.KiCadClient` (`send(command, ResponseType)`)."""
        return self.kicad()._client

    def board(self) -> Any:
        """`kipy.board.Board` for the open board (raises kipy `ApiError` when none is open).

        The first call of a session also runs the pending IPC presence probes of the patch
        manifest (`kiwrap.backends.ipc.presence`, docs/UPSTREAM.md §3.3): a derived `ipc.*` key
        such as `ipc.has_get_ratsnest` is confirmed or flipped by one live request and cached, so
        a build carrying only the CLI half of a patch stops advertising the IPC half after its
        first headless use."""
        board = self.kicad().get_board()
        if not self._presence_probed:
            self._presence_probed = True
            from kiwrap.backends.ipc import presence

            presence.run_pending(self)
        return board

    def schematic(self) -> Any:
        """`kipy.schematic.Schematic` for the open schematic."""
        return self.kicad().get_schematic()

    def open_document(self, path: str | Path, kind: str) -> Any:
        """`OpenDocument` (11-only command family): `kind` is "pcb" | "schematic" | "project".
        Returns the `DocumentSpecifier`. One document per type per server (upstream
        `command_api_server.cpp`: "a document of this type is already open")."""
        from kipy.proto.common.types import DocumentType

        types = {
            "pcb": DocumentType.DOCTYPE_PCB,
            "schematic": DocumentType.DOCTYPE_SCHEMATIC,
            "project": DocumentType.DOCTYPE_PROJECT,
        }
        if kind not in types:
            raise SessionError(f"unknown document kind {kind!r}", fix=f"one of {sorted(types)}")
        return self.kicad().open_document(str(Path(path).resolve()), types[kind])

    def version(self) -> str:
        return str(self.kicad().get_version().full_version)

    def load_libraries(
        self, kinds: Iterable[str] = ("symbol", "footprint"), *, timeout_s: float = 120.0
    ) -> dict[str, str]:
        """`LoadAllLibraries` + `GetLibraryStatuses` polling (kipy `load_all_libraries` /
        `wait_for_libraries`). Headless servers load NO library until asked (kipy docstring, and
        verified: `PlaceFootprintFromLibrary` / `PlaceSymbolFromLibrary` answer "'X' not found"
        before this call and succeed after it). Returns {nickname: status} for the loaded tables
        (global + project); statuses other than "loaded" carry KiCad's error text after " - ".
        Loading pulls in the eeschema/pcbnew faces, so the first call can take seconds."""
        from kipy.proto.common.types.library_types_pb2 import LibraryLoadStatus, LibraryType

        types = {"symbol": LibraryType.LT_SYMBOL, "footprint": LibraryType.LT_FOOTPRINT}
        wanted = [types[k] for k in kinds]
        k = self.kicad()
        k.load_all_libraries(wanted)
        result: dict[str, str] = {}
        for entry in k.wait_for_libraries(wanted, timeout_s=timeout_s):
            status = LibraryLoadStatus.Name(entry.status).removeprefix("LLS_").lower()
            if entry.error_message:
                status += " - " + entry.error_message
            result[entry.nickname] = status
        return result


def live_sessions() -> list[HeadlessSession]:
    with _REGISTRY_LOCK:
        return list(_REGISTRY.values())


def close_all() -> int:
    """Close every session still registered - pooled ones included (atexit hook; tests call it in
    teardown). Returns the count."""
    SESSION_POOL.close_all()
    sessions = live_sessions()
    for session in sessions:
        session.close()
    return len(sessions)


# --- bound GUI sessions ---------------------------------------------------------------------------


def _bound_key(directory: str | Path) -> Path:
    return Path(directory).resolve()


def _bound_writes_through(session: Any) -> bool:
    """True when the mutating tools reach this GUI over IPC (its KiCad has the headless IPC
    provider, capability `ipc.headless` - the planner's ipc11 predicate); a session without a
    capability record (test fakes) counts as reachable."""
    caps = getattr(session, "capabilities", None)
    if caps is None or not hasattr(caps, "has"):
        return True
    try:
        return bool(caps.has("ipc.headless"))
    except Exception:  # noqa: BLE001 - a broken record never blocks the prologue
        return True


def bind_gui_session(directory: str | Path, session: Any) -> None:
    """Register `session` (a `gui_sessions.GuiSession`: `running`, `file`, `pid`, `socket_url`,
    `kicad()`, `board()`, `schematic()`, `capabilities`, `install`, `locks_taken`, `serves()`) as
    THE KiCad for every file under `directory`. Replaces a dead binding; a live one for another
    session raises `SessionError`. The disk stamp of the GUI's files is recorded now (the editor
    just loaded them): see `resync_bound_session`."""
    key = _bound_key(directory)
    with _BOUND_LOCK:
        existing = _BOUND.get(key)
        if existing is not None and existing is not session and getattr(existing, "running", False):
            raise SessionError(
                f"a GUI session is already bound to {key}",
                why="one KiCad per project: its API server owns the project's files",
                fix="detach the existing session first (kw_live detach)",
            )
        _BOUND[key] = session
        _BOUND_STAMPS[key] = _session_stamp(session)
    # the pooled headless servers on that project would hold KiCad's lock next to the GUI's
    SESSION_POOL.close_project(key)


def unbind_gui_session(directory: str | Path, session: Any | None = None) -> None:
    key = _bound_key(directory)
    with _BOUND_LOCK:
        if session is None or _BOUND.get(key) is session:
            _BOUND.pop(key, None)
            _BOUND_STAMPS.pop(key, None)


# --- the bound GUI's document vs the file on disk ------------------------------------------------


#: per bound project directory: the disk stamp of the GUI's files as of its last known save /
#: load / revert (what its in-memory document corresponds to)
_BOUND_STAMPS: dict[Path, tuple[Any, ...]] = {}


def _session_files(session: Any) -> list[Path]:
    files = [Path(f) for f in (getattr(session, "files", None) or [])]
    if not files:
        one = getattr(session, "file", None)
        if one is not None:
            files = [Path(one)]
    return files


def _session_stamp(session: Any) -> tuple[Any, ...]:
    """(name, mtime_ns, size, [sheet tree]) of every file the GUI holds (`_file_stamp` per file)."""
    return tuple((p.name, _file_stamp(p)) for p in _session_files(session))


def _stamp_bound(session: Any) -> None:
    key = _bound_key(getattr(session, "project_dir", "") or ".")
    with _BOUND_LOCK:
        _BOUND_STAMPS[key] = _session_stamp(session)


class BoundStale(Unsupported):
    """The live GUI's document could not be brought back in line with the file on disk (a
    `RevertDocument` the editor refused - a commit still open - or the API not answering): the
    planner takes it as "ipc11 unavailable" and the file engine, which reads and writes the
    truth on disk, answers instead."""

    def __init__(self, session: Any, reason: str):
        # a session without an install (a test double) still yields the error; typed Any because
        # Unsupported.install is a KiCadInstallation (mypy; no behaviour change)
        install: Any = getattr(session, "install", None)
        self.capability, self.install = "ipc.bound_document_in_sync", install
        self.oldest_kicad_with_it, self.fallback = None, "file"
        self.fix = (
            "the file engine answers this call from the file on disk; the live view catches up on the "
            "next successful call, or kw_live(action='detach') releases the GUI"
        )
        RuntimeError.__init__(
            self,
            f"the live KiCad session (kw_live, pid {getattr(session, 'pid', '?')}) holds a document that is "
            f"older than the file on disk and could not revert it: {reason}",
        )


class BoundElsewhere(Unsupported):
    """A GUI session is bound to the project but holds a document of ANOTHER kind (a pcbnew
    session and a `.kicad_sch` request, or the reverse): its API server has no handler for that
    kind (verified 10.99: `GetOpenDocuments` answers "no handler available"), and a headless
    server next to it would fight it for the project lock. The planner takes this as "ipc11
    unavailable" and the file engine answers (reviewer finding 9, 2026-09-20)."""

    def __init__(self, session: Any, kind: str, editor: str):
        # a session without an install (a test double) still yields the error; typed Any because
        # Unsupported.install is a KiCadInstallation (mypy; no behaviour change)
        install: Any = getattr(session, "install", None)
        self.capability, self.install = f"ipc.bound_{editor}_session_serves_{kind}", install
        self.oldest_kicad_with_it, self.fallback = None, "file"
        names = {"pcb": "board", "sch": "schematic"}
        self.fix = (
            f"{names.get(kind, kind)} reads and writes come from the file engine while the live "
            f"{names.get(editor, editor)} session lives (degraded: ipc11_unavailable); "
            "kw_live(action='detach') releases the project"
        )
        RuntimeError.__init__(
            self,
            f"a live KiCad {names.get(editor, editor)} session (kw_live, pid {getattr(session, 'pid', '?')}) is "
            f"bound to this project and holds no {names.get(kind, kind)} document",
        )


def _document_kind(path: Path | None) -> str | None:
    if path is None:
        return None
    suffix = path.suffix.lower()
    if suffix == ".kicad_pcb":
        return "pcb"
    if suffix == ".kicad_sch":
        return "sch"
    return None


def bound_serves_kind(session: Any, kind: str | None) -> bool:
    """Whether the bound GUI can answer requests for a document of `kind` (pcb | sch | None =
    anything): a pcbnew session serves boards only, an eeschema one schematics only; a project
    manager session (or one without an editor attribute, a test fake) counts as serving both."""
    editor = getattr(session, "editor", None)
    if kind is None or editor in (None, "", "project"):
        return True
    return str(editor) == kind


def _revert_bound_document(session: Any) -> list[str]:
    """`RevertDocument` (kipy `Board.revert()` / `Schematic.revert()`, handled by
    `API_HANDLER_PCB::handleRevertDocument` on 10.99, refused only while a commit is open) for
    the documents the session's editor holds. Returns the kinds reverted."""
    editor = str(getattr(session, "editor", "") or "pcb")
    kinds = {"pcb": ("board",), "sch": ("schematic",), "project": ("board", "schematic")}.get(
        editor, ("board",)
    )
    reverted: list[str] = []
    errors: list[str] = []
    for kind in kinds:
        try:
            getattr(session, kind)().revert()
            reverted.append(kind)
        except Exception as exc:  # noqa: BLE001 - a manager without that editor open is fine
            errors.append(f"{kind}: {type(exc).__name__}: {exc}")
    if not reverted and errors:
        raise SessionError("; ".join(errors))
    return reverted


def resync_bound_session(path: str | Path | None, *, session: Any | None = None) -> dict[str, Any] | None:
    """Bring the bound GUI's in-memory document back in line with the files on disk when
    something wrote them behind the editor (reviewer finding 2, 2026-09-20: a rotating
    `kw_pcb_place` through the G16-guarded file writer, `kw_pcb_route_auto` / `kw_pcb_fix`
    importing a SES on disk, a `--save-board` refill; pcbnew never reloads a board on its own -
    its wxFileSystemWatcher watches the footprint library only - and its next `SaveDocument`
    would silently overwrite those edits while every ipc11 read answered from the stale board).

    Compares the disk stamp of the session's files with the one recorded at its last known
    load / save / revert (`_BOUND_STAMPS`); on a difference sends `RevertDocument` and re-stamps.
    Returns None without a bound session, else `{"changed": bool, "reverted": [kinds]}`; raises
    `BoundStale` (an `Unsupported`: the planner falls through to the file engine) when the
    revert failed."""
    if session is None:
        session = bound_session_for(path)
    if session is None:
        return None
    key = _bound_key(getattr(session, "project_dir", None) or Path(path or ".").parent)
    now = _session_stamp(session)
    with _BOUND_LOCK:
        last = _BOUND_STAMPS.get(key)
    if last is None or last == now:
        with _BOUND_LOCK:
            _BOUND_STAMPS[key] = now
        return {"changed": False, "reverted": []}
    try:
        reverted = _revert_bound_document(session)
    except Exception as exc:  # noqa: BLE001 - classified for the planner
        raise BoundStale(session, str(exc)) from exc
    log.info(
        "live GUI session pid %s reverted %s: %s changed on disk behind the editor",
        getattr(session, "pid", "?"),
        ", ".join(reverted),
        ", ".join(p.name for p in _session_files(session)),
    )
    with _BOUND_LOCK:
        _BOUND_STAMPS[key] = _session_stamp(session)
    SESSION_POOL.bound_reverts += 1
    return {"changed": True, "reverted": reverted}


def bound_session_for(path: str | Path | None) -> Any | None:
    """The live GUI session bound to the project directory containing `path` (or to `path`
    itself when it is a directory), else None. Dead bindings are dropped on the way.

    A GUI another kiwrap process launched (the VS Code extension's server, while this is the
    agent's - docs/EXTENSION.md §6) counts too: its record under the shared temp dir is found by
    `gui_sessions.discover_remote_session()` and bound here as a `RemoteGuiSession`, so this
    process's ipc11 provider writes into the window the user is watching."""
    if path is None:
        return None
    target = Path(path).resolve()
    with _BOUND_LOCK:
        for key, session in list(_BOUND.items()):
            if not getattr(session, "running", False):
                _BOUND.pop(key, None)
                continue
            if target == key or _is_under(target, key):
                return session
    remote = _discover_remote(target)
    if remote is None:
        return None
    key = _bound_key(remote.project_dir)
    with _BOUND_LOCK:
        existing = _BOUND.get(key)
        if existing is not None and getattr(existing, "running", False):
            return existing
        _BOUND[key] = remote
        # this process has no history of that GUI's saves: the disk is taken as its state now
        _BOUND_STAMPS[key] = _session_stamp(remote)
    SESSION_POOL.close_project(key)  # a pooled headless server on that project would fight the GUI
    return remote


def _discover_remote(target: Path) -> Any | None:
    """`gui_sessions.discover_remote_session(target)` (lazy: gui_sessions imports this module),
    swallowing every failure - discovery is a convenience on top of the in-process binding."""
    try:
        from kiwrap.backends.ipc.gui_sessions import discover_remote_session
    except ImportError:  # pragma: no cover - the component is part of the same distribution
        return None
    try:
        return discover_remote_session(target)
    except Exception as exc:  # noqa: BLE001 - never break a tool call over a stale record
        log.debug("remote GUI session discovery failed for %s: %s", target, exc)
        return None


def bound_sessions() -> list[Any]:
    with _BOUND_LOCK:
        return [s for s in _BOUND.values() if getattr(s, "running", False)]


class _BoundHandle:
    """What `pooled_session()` returns for a file a GUI session is bound to: the same protocol
    as `_PooledHandle`, over the GUI's API server; `close()` / `__exit__` never stop the GUI.
    On the way out the disk stamp is re-recorded: whatever the GUI saved through the handle
    (`ipc_pcb_save` / `ipc_sch_save`) is now what its document corresponds to."""

    def __init__(self, session: Any):
        self._session = session
        self.bound = True

    def __enter__(self) -> Any:
        return self._session

    def __exit__(self, *exc: object) -> None:
        _stamp_bound(self._session)

    def close(self) -> None:
        _stamp_bound(self._session)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)


# --- pool ------------------------------------------------------------------------------------------


#: a pooled server on one of these pre-loads the whole sheet tree, so its stamp covers every
#: `.kicad_sch` next to the file (a child sheet edited on disk must drop the entry too)
_TREE_SUFFIXES = (".kicad_sch", ".kicad_pro")


def _file_stamp(file: Path | None) -> tuple[Any, ...] | None:
    """(mtime_ns, size) of `file`; for a schematic / project also of every `*.kicad_sch` in its
    directory (the server holds the hierarchy: a child sheet changed outside this process while
    the root is unchanged must not be answered from the stale load - reviewer finding, 2026-09-20)."""
    if file is None:
        return None
    try:
        st = file.stat()
    except OSError:
        return None
    stamp: tuple[Any, ...] = (st.st_mtime_ns, st.st_size)
    if file.suffix.lower() in _TREE_SUFFIXES:
        siblings: list[tuple[str, int, int]] = []
        try:
            for sheet in sorted(file.parent.glob("*.kicad_sch")):
                try:
                    sst = sheet.stat()
                except OSError:
                    continue
                siblings.append((sheet.name, sst.st_mtime_ns, sst.st_size))
        except OSError:
            pass
        stamp = (*stamp, tuple(siblings))
    return stamp


@dataclass
class _PoolEntry:
    key: tuple[str, str]
    session: HeadlessSession
    stamp: tuple[Any, ...] | None
    lock: threading.RLock = field(default_factory=threading.RLock)  # re-entrant: nested use by one thread
    timer: threading.Timer | None = None
    last_used: float = field(default_factory=time.monotonic)
    uses: int = 0
    busy: int = 0  # acquisitions not yet released (RLock has no portable `locked()`)


class _PooledHandle:
    """What `pooled_session()` returns: a context manager that yields the live `HeadlessSession`
    and, on exit, hands it back to the pool instead of closing it. `close()` does the same so the
    tools' `ipc_session()` helper (which accepts either protocol) works unchanged."""

    def __init__(self, pool: SessionPool, entry: _PoolEntry):
        self._pool, self._entry = pool, entry

    def __enter__(self) -> HeadlessSession:
        return self._entry.session

    def __exit__(self, *exc: object) -> None:
        self._pool.release(self._entry)

    def close(self) -> None:
        self._pool.release(self._entry)

    def __getattr__(self, name: str) -> Any:  # transparent for callers that poke the session
        return getattr(self._entry.session, name)


class SessionPool:
    """One headless server per (install, file), kept between tool calls (module docstring)."""

    def __init__(self, idle_timeout_s: float | None = None):
        env = os.environ.get("KIWRAP_IPC_IDLE_S")
        self.idle_timeout_s = float(env) if env else (idle_timeout_s or DEFAULT_IDLE_TIMEOUT_S)
        self._entries: dict[tuple[str, str], _PoolEntry] = {}
        self._lock = threading.Lock()
        self.reused = 0  # statistics for tests / kw_env
        self.bound_hits = 0  # requests answered by a bound GUI session instead of a headless server
        self.bound_reverts = 0  # times a bound GUI had to reload a file written behind it

    @staticmethod
    def _key(install: KiCadInstallation, file: str | Path | None) -> tuple[str, str]:
        return (install.key(), str(Path(file).resolve()) if file is not None else "")

    def acquire(self, install: KiCadInstallation, file: str | Path | None = None, **kw: Any) -> _PooledHandle:
        """A handle on a running session for `file`; started now, or reused when the same file
        (unchanged on disk) is already open. Blocks while another call uses that session."""
        key = self._key(install, file)
        path = Path(file).resolve() if file is not None else None
        bound = bound_session_for(path)
        if bound is not None:
            # the live GUI is THE KiCad for this project: no headless server next to it - and
            # only for the document kind it holds (a pcbnew session has no schematic handlers;
            # a headless server beside it would fight it for the project lock): the other kind
            # is "ipc11 unavailable" for the planner, so the file engine answers
            kind = _document_kind(path)
            if not bound_serves_kind(bound, kind):
                raise BoundElsewhere(bound, kind or "", str(getattr(bound, "editor", "")))
            self.bound_hits += 1
            # a file written on disk behind the editor (file writer, SES import) is reloaded
            # into the GUI first so neither a read nor the next save answers from a stale board
            resync_bound_session(path, session=bound)
            return _BoundHandle(bound)  # type: ignore[return-value]
        stamp = _file_stamp(path)
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and (not entry.session.running or entry.stamp != stamp):
                self._drop(entry)  # dead, or the file changed on disk: never answer from a stale load
                entry = None
            if entry is None:
                session = HeadlessSession(install, file, **kw)
                entry = _PoolEntry(key, session, stamp)
                self._entries[key] = entry
                fresh = True
            else:
                fresh = False
                self.reused += 1
            if entry.timer is not None:
                entry.timer.cancel()
                entry.timer = None
        entry.lock.acquire()
        entry.busy += 1
        try:
            if fresh or not entry.session.running:
                entry.session.start()
                entry.stamp = _file_stamp(path)
        except BaseException:
            entry.busy -= 1
            entry.lock.release()
            with self._lock:
                self._drop(entry)
            raise
        entry.uses += 1
        entry.last_used = time.monotonic()
        return _PooledHandle(self, entry)

    def release(self, entry: _PoolEntry) -> None:
        entry.last_used = time.monotonic()
        with self._lock:
            entry.busy = max(0, entry.busy - 1)
            still_busy = entry.busy > 0
        with contextlib.suppress(RuntimeError):
            entry.lock.release()
        if still_busy:
            return
        with self._lock:
            if self._entries.get(entry.key) is not entry:
                return
            if entry.timer is not None:
                entry.timer.cancel()
            entry.timer = threading.Timer(self.idle_timeout_s, self._expire, args=(entry,))
            entry.timer.daemon = True
            entry.timer.start()

    def _expire(self, entry: _PoolEntry) -> None:
        with self._lock:
            if self._entries.get(entry.key) is not entry or entry.busy > 0:
                return
            self._drop(entry)

    def _drop(self, entry: _PoolEntry) -> None:
        """Caller holds `self._lock`. Closes the session (never raises)."""
        if entry.timer is not None:
            entry.timer.cancel()
            entry.timer = None
        self._entries.pop(entry.key, None)
        entry.session.close()

    def close_project(self, directory: Path, *, timeout_s: float = 60.0) -> list[Path]:
        """Close the pooled sessions whose file lives under `directory` (waiting for a busy one up
        to `timeout_s`), so KiCad's project lock is released before a mutating tool runs.
        Returns the files that were open."""
        directory = Path(directory).resolve()
        closed: list[Path] = []
        with self._lock:
            entries = [
                e
                for e in self._entries.values()
                if e.session.file is not None and _is_under(e.session.file, directory)
            ]
        for entry in entries:
            if entry.lock.acquire(timeout=timeout_s):
                try:
                    with self._lock:
                        self._drop(entry)
                finally:
                    entry.lock.release()
                if entry.session.file is not None:
                    closed.append(entry.session.file)
        return closed

    def close_all(self) -> int:
        with self._lock:
            entries = list(self._entries.values())
            for entry in entries:
                self._drop(entry)
        return len(entries)

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "open": [
                    {"file": str(e.session.file), "pid": e.session.pid, "uses": e.uses}
                    for e in self._entries.values()
                ],
                "reused": self.reused,
                "bound_hits": self.bound_hits,
                "bound_reverts": self.bound_reverts,
                "bound": [
                    {"project": str(getattr(s, "project_dir", "")), "pid": getattr(s, "pid", None)}
                    for s in bound_sessions()
                ],
                "idle_timeout_s": self.idle_timeout_s,
            }


def _is_under(path: Path, directory: Path) -> bool:
    try:
        Path(path).resolve().relative_to(directory)
    except ValueError:
        return False
    return True


SESSION_POOL = SessionPool()


def pooled_session(install: KiCadInstallation, file: str | Path | None = None, **kw: Any) -> _PooledHandle:
    """Drop-in for `HeadlessSession(install, file, **kw)` that reuses a running server (module
    docstring). Use it as `with pooled_session(...) as s:`; the server stays alive afterwards."""
    return SESSION_POOL.acquire(install, file, **kw)


def release_project(directory: str | Path, *, timeout_s: float = 60.0) -> dict[str, Any]:
    """The mutating tools' prologue (finding 19): close kiwrap's own pooled sessions on the project
    so their `~<name>.kicad_pro.lck` goes away, and report the NON-pooled live sessions still bound
    to files there (a read in flight in another thread), which the caller answers with E_BUSY."""
    directory = Path(directory).resolve()
    # A kiwrap process that was hard-killed (MCP host TerminateProcess, pytest-timeout os._exit)
    # took its servers down through the Job Object / PDEATHSIG, but their `~<name>.kicad_pro.lck`
    # stayed: sweep the recorded dead servers first so the prologue's refuse_if_editor_open()
    # does not refuse the project with E_FILE_LOCKED for a lock only a human could delete.
    sweep_orphans()
    # Same for the GUI sessions of a kiwrap server that died hard (reviewer finding 10,
    # 2026-09-20: the extension host killed -> the Job Object took pcbnew, its ~*.lck stayed
    # and every mutation of the agent was refused E_FILE_LOCKED until someone launched again).
    try:
        from kiwrap.backends.ipc.gui_sessions import sweep_orphans as gui_sweep_orphans

        gui_sweep_orphans()
    except Exception as exc:  # noqa: BLE001 - housekeeping never breaks the prologue
        log.debug("gui orphan sweep failed: %s", exc)
    closed = SESSION_POOL.close_project(directory, timeout_s=timeout_s)
    busy = [
        str(s.file)
        for s in live_sessions()
        if s.file is not None and s.running and _is_under(s.file, directory)
    ]
    info: dict[str, Any] = {"closed": [str(p) for p in closed], "busy": busy}
    bound = bound_session_for(directory)
    if bound is not None:
        # The GUI stays: the tools' IPC twins commit through it (pooled_session hands it out) and
        # the `~*.lck` files it holds are ITS, not a foreign editor's - the editor-lock refusal
        # must skip exactly these (listed by name for the prologue).
        info["bound"] = {
            "session_id": getattr(bound, "session_id", None),
            "pid": getattr(bound, "pid", None),
            "socket": getattr(bound, "socket_url", None),
            "locks": [str(p) for p in getattr(bound, "locks_taken", [])],
            "files": [str(p) for p in getattr(bound, "files", [])],
            # whether the tools' ipc11 provider is eligible on this KiCad (planner predicate
            # `ipc.headless`): only then do the mutating tools write THROUGH the GUI and may treat
            # its lock files as their own; on a 10.0.x the GUI holds the files and they stay refused
            "ipc11": _bound_writes_through(bound),
            # which KiCad saves what the tools write through the GUI: the planner's ipc11 epoch
            # precondition judges THESE formats (the pool hands the GUI out whatever install a
            # call asked for - review finding 7)
            "install": _bound_install_key(bound),
            "kicad": getattr(getattr(bound, "install", None), "version", None),
            "formats": _bound_formats(bound),
        }
    return info


def _bound_install_key(session: Any) -> Any:
    install = getattr(session, "install", None)
    try:
        return install.key() if install is not None else None
    except Exception:  # noqa: BLE001 - a test double without key()
        return None


def _bound_formats(session: Any) -> dict[str, int] | None:
    """`{sch, pcb}` format epochs of the bound GUI's KiCad (its capability record), None when the
    record has none (test fakes, a failed probe) - the precondition then cannot judge it."""
    caps = getattr(session, "capabilities", None)
    if caps is None:
        return None
    out: dict[str, int] = {}
    for kind in ("sch", "pcb"):
        try:
            value = caps[f"formats.{kind}"]
        except Exception:  # noqa: BLE001 - a missing key or a broken record: not judged
            continue
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            out[kind] = int(value)
    return out or None


_ATEXIT_REGISTERED = False


def _register_atexit() -> None:
    """Register `close_all` AFTER pynng is imported. atexit handlers run last-in-first-out and
    pynng registers `nng_fini()` at import; a `close_all` registered earlier (at this module's
    import) would run after nng is finalised and the `CloseAllDocuments` dial hangs forever
    (verified: a pooled session left open blocked interpreter exit until a faulthandler dump).
    Called from `HeadlessSession.start()`, the first place kipy is guaranteed importable."""
    global _ATEXIT_REGISTERED
    if _ATEXIT_REGISTERED:
        return
    import pynng  # noqa: F401 - imported for its atexit registration order

    atexit.register(close_all)
    _ATEXIT_REGISTERED = True


@contextlib.contextmanager
def session_for(
    install: KiCadInstallation, file: str | Path | None = None, **kw: Any
) -> Iterator[HeadlessSession]:
    """Functional alias for `with HeadlessSession(install, file, **kw) as s`."""
    with HeadlessSession(install, file, **kw) as s:
        yield s
