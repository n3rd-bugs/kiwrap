"""`Router`: one Freerouting 2.4.1 run per `route()` call, headless, under the managed JRE.

The invocation (every flag verified on the installed 2.4.1 jar, spikes/autorouting,
docs/VERIFICATION.md claim 8, and the probe runs of 2026-09-20 in the scratchpad):

    java -Djava.awt.headless=true -jar freerouting-2.4.1.jar
         --user_data_path=<run dir>            # a per-run directory holding freerouting.json (the
                                               # telemetry-off profile copied from bootstrap's, see
                                               # `write_profile`) and freerouting.log; Freerouting
                                               # rewrites the profile on exit - never the shared one
         --gui.enabled=false -l en             # headless, English log lines (the progress parser)
         -de <design.dsn> -do <session.ses>    # -do writes the SES at the very end only (no partial
                                               # output on a timeout / cancel)
         -mp <passes> -mt 1 --router.optimizer.max_threads=1   # determinism: `-mt` sets the
                                               # optimizer pool in 2.4.1 (settings.md) and the flat
                                               # max_threads; the nested optimizer key is what the
                                               # result manifest reports (probe: 23 -> 1)
         --router.fanout.enabled=false         # unless the board has a BGA (rules.fanout)
         --router.copper_to_edge_clearance_um=<n>   # the board rule; KiCad's DSN omits it (#558)
         --router.automatic_neckdown=false     # harmless (claim 8), keeps widths at the rule
         --router.result_json=<run dir>/result.json   # 2.4.1 writes the manifest: settings
                                               # snapshot, board_statistics.connections.incomplete_count,
                                               # clearance_violations, final_state, exit_code
         --usage_and_diagnostic_data.disable_analytics=true --profile.allow_telemetry=false
         [-inc <plane-only netclasses>]        # netclasses made only of plane nets; 2.4.1
                                               # headless IGNORES this flag (Phase 1c finding),
                                               # so a `nets=` subset is enforced by the DSN hook
                                               # (dsn_post.restrict_nets removes the other nets
                                               # from the network), never by -inc

Progress comes from the log lines (`Auto-routing pass #N … score S (U unrouted and V
violations)`, the optimizer passes, the stop reason, the "could not be routed" list with the
net and both ends); the manifest is read afterwards. Cancel: the `cancel_event` is polled four
times a second and the process TREE is killed (`taskkill /PID /T /F` through argv on Windows,
`killpg` elsewhere); the JVM is in its own process group so a Ctrl+C in the parent console does
not reach it, and it is bound to this process's lifetime (`kiwrap.platform.lifetime`: a
kill-on-close Job Object on Windows, PDEATHSIG on POSIX) so a kiwrap server the host terminates
takes its router with it instead of leaving a multi-GB orphan. Timeouts kill the same way and
raise `RouterTimeout` (a `TimeoutError`, which the tools map to E_TIMEOUT) - unless the log
already says the session was saved: then the JVM gets `STOP_GRACE_S` to finish its shutdown and
the finished session is returned (a run that completes inside the last poll interval is a
result, not a timeout). Exit code 0 with no SES written is a `RouterError`, as is any non-zero
exit; the log tail travels with both. Run directories of failed runs are kept for inspection
(`run_dir` in the error data) but pruned to the newest `KEEP_FAILED_RUN_DIRS` per temp dir.

REST mode (`--api_server.enabled=true`, spikes/autorouting/api_client_test.py) is not used: the
CLI gives the same SES, the progress lines and the manifest without a listening socket.
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

from kiwrap.backends.router.dsn_post import postprocess_dsn
from kiwrap.backends.router.jre import Engines
from kiwrap.backends.router.rules import RouteRules
from kiwrap.platform import current
from kiwrap.platform.lifetime import bind_child_lifetime, popen_lifetime_kwargs

DEFAULT_PASSES = 100
DEFAULT_TIMEOUT_S = 900.0
STOP_GRACE_S = 10.0
KEEP_FAILED_RUN_DIRS = 5  # older failed-run directories under the temp dir are pruned
LOG_TAIL_CHARS = 20_000
RESULT_JSON_NAME = "result.json"
PROFILE_NAME = "freerouting.json"
LOG_NAME = "freerouting.log"

#: The profile keys bootstrap enforces (scripts/bootstrap.ps1 §5), re-applied to every per-run
#: copy: analytics / telemetry / contact off, GUI off, the GitHub version check blackholed through
#: the JVM's https proxy (localhost stays direct), no local servers, logging into the run dir.
ENFORCED_PROFILE: dict[str, Any] = {
    "profile.email": "",
    "profile.allow_telemetry": False,
    "profile.allow_contact": False,
    "usage_and_diagnostic_data.disable_analytics": True,
    "usage_and_diagnostic_data.track_window_changed": False,
    "usage_and_diagnostic_data.track_button_clicked": False,
    "gui.enabled": False,
    "gui.show_routing_summary": False,
    "feature_flags.save_jobs": False,
    "api_server.enabled": False,
    "mcp_server.enabled": False,
    "network.proxy_url": "http://127.0.0.1:9",
    "network.no_proxy": "localhost,127.0.0.1",
    "logging.console.enabled": True,
    "logging.file.enabled": True,
    "logging.file.level": "INFO",
}

_RE_STAGE_START = re.compile(r"Auto-routing stage started .* for (\d+) unrouted items")
_RE_PASS = re.compile(
    r"Auto-routing pass #(\d+) .*?score (?:of )?([\d.]+) \((\d+) unrouted and (\d+) violations\)"
)
_RE_OPT_PASS = re.compile(
    r"Optimizer pass #(\d+) .*?score (?:of )?([\d.]+) \((\d+) unrouted and (\d+) violations\)"
)
_RE_FANOUT = re.compile(r"Fanout stage (started|completed)")
_RE_STAGE_DONE = re.compile(
    r"Auto-routing stage completed: .*final score: ([\d.]+) \((\d+) unrouted and (\d+) violations\)"
)
_RE_STOP = re.compile(r"(The router's score .*?Stopping the auto-router\.|Stopping optimizer because .*)")
_RE_STATE = re.compile(r"finished with state: ([A-Z_]+)")
_RE_UNROUTED_NET = re.compile(r"^\s*Net '(.+)' \((\d+) unrouted connection")
_RE_UNROUTED_CONN = re.compile(r"^\s*-\s+(\S+)\s+->\s+(\S+)")
_RE_SAVED = re.compile(r"Successfully saved output file")
_RE_VERSION = re.compile(r"Freerouting v([\w.\-]+)")
_RE_LEVEL = re.compile(r"^\S+ \S+ (ERROR|WARN|FATAL)\s+(.*)$")
_RE_JOB_TIMEOUT = re.compile(r"timed out|timeout", re.IGNORECASE)


class RouterError(RuntimeError):
    """Freerouting failed (non-zero exit, no SES written, or the JVM could not start)."""

    def __init__(self, message: str, *, argv: list[str], exit_code: int | None, log: str):
        super().__init__(message)
        self.argv, self.exit_code, self.log = list(argv), exit_code, log


class RouterTimeout(TimeoutError):
    """The run exceeded `timeout_s` and was killed (the tools map a TimeoutError to E_TIMEOUT)."""

    def __init__(self, message: str, *, argv: list[str], log: str, elapsed_s: float):
        super().__init__(message)
        self.argv, self.log, self.elapsed_s = list(argv), log, elapsed_s


class RouterCancelled(RuntimeError):
    """`cancel_event` was set; the process tree was killed."""


@dataclass
class RouteProgress:
    stage: str  # starting | fanout | autoroute | optimize | saving | done
    pass_no: int = 0
    max_passes: int = 0
    unrouted: int | None = None
    violations: int | None = None
    score: float | None = None
    message: str = ""
    elapsed_s: float = 0.0

    @property
    def fraction(self) -> float:
        """A monotone-ish estimate: autoroute passes count for 90 %, the optimizer for the rest."""
        if self.stage == "done":
            return 1.0
        if self.stage in ("optimize", "saving"):
            return 0.95
        if self.stage == "autoroute" and self.max_passes > 0:
            return min(0.9, 0.05 + 0.85 * self.pass_no / self.max_passes)
        return 0.02

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "pass": self.pass_no,
            "max_passes": self.max_passes,
            "unrouted": self.unrouted,
            "violations": self.violations,
            "score": self.score,
            "message": self.message,
            "elapsed_s": round(self.elapsed_s, 1),
            "fraction": round(self.fraction, 3),
        }


@dataclass
class RouteResult:
    passes: int
    unrouted: int
    log: str
    duration_s: float
    exit_code: int | None
    ses_path: Path
    ses_written: bool = False
    violations: int | None = None  # Freerouting's own clearance-violation count (KiCad DRC is the judge)
    score: float | None = None
    state: str = ""  # Freerouting job state: COMPLETED / FAILED / …
    stop_reason: str = ""
    unrouted_at_start: int | None = None
    unrouted_connections: list[dict[str, str]] = field(default_factory=list)
    result_json: dict[str, Any] | None = None
    argv: list[str] = field(default_factory=list)
    run_dir: Path | None = None
    dsn_used: Path | None = None
    ignored_nets: list[str] = field(default_factory=list)
    ignore_classes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    cancelled: bool = False
    #: the unrouted / violations / score after every autorouter and optimizer pass, in order
    #: (`{"stage", "pass", "unrouted", "violations", "score", "elapsed_s"}`): the history a
    #: receipt keeps so a plateau ("the same 2 unrouted for 30 passes") is visible without the log
    history: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and self.ses_written and not self.cancelled

    def to_dict(self) -> dict[str, Any]:
        stats = (self.result_json or {}).get("board_statistics") or {}
        return {
            "ok": self.ok,
            "passes": self.passes,
            "unrouted": self.unrouted,
            "unrouted_at_start": self.unrouted_at_start,
            "unrouted_connections": list(self.unrouted_connections),
            "router_violations": self.violations,
            "score": self.score,
            "state": self.state,
            "stop_reason": self.stop_reason,
            "duration_s": round(self.duration_s, 2),
            "exit_code": self.exit_code,
            "ses": str(self.ses_path),
            "ses_written": self.ses_written,
            "ignored_nets": list(self.ignored_nets),
            "ignore_classes": list(self.ignore_classes),
            "traces": (stats.get("traces") or {}).get("total_count"),
            "vias": (stats.get("vias") or {}).get("total_count"),
            "warnings": list(self.warnings),
            "cancelled": self.cancelled,
            "run_dir": str(self.run_dir) if self.run_dir else None,
            "history": [dict(h) for h in self.history],
        }


def _set_path(doc: dict[str, Any], dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    cur = doc
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def write_profile(run_dir: Path, template: Path | None, *, version: str = "") -> Path:
    """`<run_dir>/freerouting.json`: bootstrap's profile (when present) with `ENFORCED_PROFILE`
    re-applied, a stable profile id (a fresh uuid when the template has none) and the log file
    inside the run dir. Freerouting creates a default profile with `allow_telemetry: true` in an
    empty user_data_path (probe run 2026-09-20), so the copy is written BEFORE the JVM starts."""
    doc: dict[str, Any] = {}
    if template is not None and template.is_file():
        with contextlib.suppress(OSError, ValueError):
            loaded = json.loads(template.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                doc = loaded
    profile = doc.get("profile")
    if not isinstance(profile, dict) or not profile.get("id"):
        _set_path(doc, "profile.id", str(uuid.uuid4()))
    if version:
        doc["version"] = version
    for key, value in ENFORCED_PROFILE.items():
        _set_path(doc, key, value)
    _set_path(doc, "logging.file.location", str(run_dir / LOG_NAME))
    _set_path(doc, "api_server.endpoints", ["http://127.0.0.1:37864"])
    path = run_dir / PROFILE_NAME
    path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return path


def _drain(stream: IO[str] | None, sink: queue.Queue[str | None]) -> None:
    try:
        if stream is not None:
            for line in stream:
                sink.put(line.rstrip("\r\n"))
    except (ValueError, OSError):
        pass
    finally:
        sink.put(None)


def kill_tree(proc: subprocess.Popen[str]) -> None:
    """Kill the JVM and anything it spawned; never raises."""
    if proc.poll() is not None:
        return
    if current().is_windows:
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=STOP_GRACE_S,
                check=False,
            )
    else:  # pragma: no cover - Linux is designed-only (D18)
        import signal

        with contextlib.suppress(OSError, ProcessLookupError, AttributeError):
            killpg, getpgid = getattr(os, "killpg"), getattr(os, "getpgid")  # noqa: B009 - POSIX only (mypy on Windows)
            killpg(getpgid(proc.pid), getattr(signal, "SIGKILL"))  # noqa: B009
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        proc.kill()
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        proc.wait(timeout=STOP_GRACE_S)


class _LogParser:
    """Turns Freerouting's log lines into `RouteProgress` updates and the final facts."""

    def __init__(self, max_passes: int, started: float):
        self.max_passes, self.started = max_passes, started
        self.stage = "starting"
        self.passes = 0
        self.unrouted: int | None = None
        self.violations: int | None = None
        self.score: float | None = None
        self.unrouted_at_start: int | None = None
        self.stop_reason = ""
        self.state = ""
        self.saved = False
        self.connections: list[dict[str, str]] = []
        self.warnings: list[str] = []
        self.errors: list[str] = []
        self._current_net: str | None = None
        self.lines: list[str] = []
        self.version = ""
        self.history: list[dict[str, Any]] = []

    def feed(self, line: str) -> RouteProgress | None:
        self.lines.append(line)
        if len(self.lines) > 5000:
            del self.lines[: len(self.lines) - 4000]
        elapsed = time.perf_counter() - self.started
        m = _RE_VERSION.search(line)
        if m and not self.version:
            self.version = m.group(1)
        lvl = _RE_LEVEL.match(line)
        if lvl:
            text = lvl.group(2).strip()
            (self.errors if lvl.group(1) in ("ERROR", "FATAL") else self.warnings).append(text[:300])
        if m := _RE_STAGE_START.search(line):
            self.stage = "autoroute"
            self.unrouted_at_start = int(m.group(1))
            self.unrouted = self.unrouted_at_start
            return self._progress(elapsed, f"autorouting {self.unrouted_at_start} unrouted item(s)")
        if m := _RE_FANOUT.search(line):
            self.stage = "fanout" if m.group(1) == "started" else "autoroute"
            return self._progress(elapsed, line.split("]")[-1].strip()[:120])
        if m := _RE_PASS.search(line):
            self.stage = "autoroute"
            self.passes = int(m.group(1))
            self.score, self.unrouted, self.violations = float(m.group(2)), int(m.group(3)), int(m.group(4))
            self._record(elapsed, self.passes)
            return self._progress(
                elapsed, f"pass {self.passes}: {self.unrouted} unrouted, {self.violations} violations"
            )
        if m := _RE_STAGE_DONE.search(line):
            self.score, self.unrouted, self.violations = float(m.group(1)), int(m.group(2)), int(m.group(3))
            self.stage = "optimize"
            return self._progress(elapsed, "autorouting done; optimizing")
        if m := _RE_OPT_PASS.search(line):
            self.stage = "optimize"
            self.score, self.unrouted, self.violations = float(m.group(2)), int(m.group(3)), int(m.group(4))
            self._record(elapsed, int(m.group(1)))
            return self._progress(elapsed, f"optimizer pass {m.group(1)}")
        if m := _RE_STOP.search(line):
            if not self.stop_reason or "auto-router" in m.group(1):
                self.stop_reason = m.group(1).strip()
            return None
        if m := _RE_STATE.search(line):
            self.state = m.group(1)
            self.stage = "saving"
            return self._progress(elapsed, f"job {self.state.lower()}; saving the session")
        if _RE_SAVED.search(line):
            self.saved = True
            self.stage = "done"
            return self._progress(elapsed, "session written")
        if m := _RE_UNROUTED_NET.match(line):
            self._current_net = m.group(1)
            return None
        if self._current_net is not None and (m := _RE_UNROUTED_CONN.match(line)):
            self.connections.append({"net": self._current_net, "from": m.group(1), "to": m.group(2)})
            return None
        if line.strip() and not line.startswith(" ") and not line.startswith("-"):
            self._current_net = None
        return None

    def _record(self, elapsed: float, pass_no: int) -> None:
        if len(self.history) < 2000:
            self.history.append(
                {
                    "stage": self.stage,
                    "pass": pass_no,
                    "unrouted": self.unrouted,
                    "violations": self.violations,
                    "score": self.score,
                    "elapsed_s": round(elapsed, 1),
                }
            )

    def _progress(self, elapsed: float, message: str) -> RouteProgress:
        return RouteProgress(
            stage=self.stage,
            pass_no=self.passes,
            max_passes=self.max_passes,
            unrouted=self.unrouted,
            violations=self.violations,
            score=self.score,
            message=message,
            elapsed_s=elapsed,
        )

    @property
    def log(self) -> str:
        text = "\n".join(self.lines)
        return text[-LOG_TAIL_CHARS:]


class Router:
    """Freerouting under the managed JRE. One `route()` = one JVM process = one run directory
    under `temp_dir` (default `Platform.temp_dir/freerouting/<id>`), removed on success unless
    `keep_run_dir`."""

    def __init__(
        self,
        engines: Engines,
        *,
        temp_dir: Path | None = None,
        env: Mapping[str, str] | None = None,
        keep_run_dir: bool = False,
        jvm_args: Iterable[str] = ("-Djava.awt.headless=true",),
    ) -> None:
        self.engines = engines.require()
        self.temp_dir = Path(temp_dir) if temp_dir is not None else current().temp_dir / "freerouting"
        self.env = dict(env) if env is not None else None
        self.keep_run_dir = keep_run_dir
        self.jvm_args = list(jvm_args)

    # ---- process plumbing -----------------------------------------------------------------------

    def _new_run_dir(self) -> Path:
        run = self.temp_dir / f"r{int(time.time())}-{uuid.uuid4().hex[:6]}"
        run.mkdir(parents=True, exist_ok=True)
        self._prune_run_dirs(keep=KEEP_FAILED_RUN_DIRS, current=run)
        return run

    def _prune_run_dirs(self, *, keep: int, current: Path) -> None:
        """Keep the newest `keep` run directories (failed runs leave theirs behind for the log
        and the processed DSN; nothing else ever removed them)."""
        try:
            dirs = [d for d in self.temp_dir.glob("r*-*") if d.is_dir() and d != current]
        except OSError:
            return
        dirs.sort(key=lambda d: d.stat().st_mtime if d.exists() else 0.0, reverse=True)
        for old in dirs[max(0, keep - 1) :]:
            shutil.rmtree(old, ignore_errors=True)

    def _env(self) -> dict[str, str]:
        env = dict(os.environ if self.env is None else self.env)
        # never let a JAVA_TOOL_OPTIONS / _JAVA_OPTIONS of the host inject flags or agents
        env.pop("JAVA_TOOL_OPTIONS", None)
        env.pop("_JAVA_OPTIONS", None)
        return env

    def base_argv(self, run_dir: Path) -> list[str]:
        assert self.engines.java is not None and self.engines.jar is not None
        return [
            str(self.engines.java),
            *self.jvm_args,
            "-jar",
            str(self.engines.jar),
            f"--user_data_path={run_dir}",
            "--gui.enabled=false",
            "-l",
            "en",
            "--usage_and_diagnostic_data.disable_analytics=true",
            "--profile.allow_telemetry=false",
            f"--logging.file.location={run_dir / LOG_NAME}",
        ]

    def _popen(self, argv: list[str], run_dir: Path) -> subprocess.Popen[str]:
        proc = subprocess.Popen(
            argv,
            cwd=str(run_dir),
            env=self._env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            **popen_lifetime_kwargs(),
        )
        # the JVM dies with this process (Job Object / PDEATHSIG); the explicit kill paths stay
        bind_child_lifetime(proc)
        return proc

    def version(self, timeout_s: float = 60.0) -> str:
        """`Freerouting vX.Y.Z` from the banner of a `-help` run (starts the JVM once)."""
        run_dir = self._new_run_dir()
        try:
            write_profile(run_dir, self.engines.profile, version=self.engines.freerouting_version)
            proc = subprocess.run(
                [*self.base_argv(run_dir), "-help"],
                cwd=str(run_dir),
                env=self._env(),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_s,
                check=False,
            )
            m = _RE_VERSION.search((proc.stdout or "") + (proc.stderr or ""))
            if not m:
                raise RouterError(
                    "Freerouting did not print its banner",
                    argv=[*self.base_argv(run_dir), "-help"],
                    exit_code=proc.returncode,
                    log=((proc.stdout or "") + (proc.stderr or ""))[-LOG_TAIL_CHARS:],
                )
            return m.group(1)
        finally:
            if not self.keep_run_dir:
                shutil.rmtree(run_dir, ignore_errors=True)

    # ---- routing --------------------------------------------------------------------------------

    def route(
        self,
        dsn: Path,
        ses_out: Path,
        *,
        rules: RouteRules,
        nets: Iterable[str] | None = None,
        passes: int = DEFAULT_PASSES,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        progress_cb: Callable[[RouteProgress], None] | None = None,
        cancel_event: threading.Event | None = None,
        extra_args: Iterable[str] = (),
    ) -> RouteResult:
        """Route `dsn` into `ses_out` (see the module docstring for the invocation). `nets`
        restricts the router to those nets (the DSN hook removes the others from the network);
        `rules` supplies the flags."""
        dsn = Path(dsn)
        ses_out = Path(ses_out)
        if not dsn.is_file():
            raise FileNotFoundError(f"DSN not found: {dsn}")
        passes = max(1, int(passes))
        run_dir = self._new_run_dir()
        started = time.perf_counter()
        parser = _LogParser(passes, started)
        argv: list[str] = []
        # the run dir is kept on every failure path (log, profile, processed DSN)
        write_profile(run_dir, self.engines.profile, version=self.engines.freerouting_version)
        dsn_used, ignore_classes, ignored_nets, warnings = self._prepare_dsn(dsn, run_dir, nets, rules)
        ses_out.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            ses_out.unlink()
        result_json = run_dir / RESULT_JSON_NAME
        argv = [
            *self.base_argv(run_dir),
            "-de",
            str(dsn_used),
            "-do",
            str(ses_out),
            "-mp",
            str(passes),
            "-mt",
            "1",
            "--router.optimizer.max_threads=1",
            *rules.cli_settings(),
            f"--router.result_json={result_json}",
        ]
        if ignore_classes:
            argv += ["-inc", ",".join(ignore_classes)]
        argv += list(extra_args)
        proc = self._popen(argv, run_dir)
        sink: queue.Queue[str | None] = queue.Queue()
        reader = threading.Thread(target=_drain, args=(proc.stdout, sink), daemon=True)
        reader.start()
        cancelled = timed_out = saved_late = False
        eof = False
        while True:
            try:
                line = sink.get(timeout=0.25)
            except queue.Empty:
                line = ""
            else:
                if line is None:
                    eof = True
                else:
                    prog = parser.feed(line)
                    if prog is not None and progress_cb is not None:
                        with contextlib.suppress(Exception):
                            progress_cb(prog)
            # cancel / timeout are checked on EVERY iteration, also while lines keep coming
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                kill_tree(proc)
                break
            # a run that finished (SES saved, JVM gone) is a result even when the last poll
            # interval crossed the budget: the exit check comes before the budget check
            if eof and proc.poll() is not None:
                break
            if not saved_late and time.perf_counter() - started > timeout_s:
                if parser.saved:
                    # the session is on disk: give the JVM its shutdown, never discard the result
                    saved_late = True
                    with contextlib.suppress(OSError, subprocess.SubprocessError):
                        proc.wait(timeout=STOP_GRACE_S)
                    if proc.poll() is None:
                        kill_tree(proc)
                    continue
                timed_out = True
                kill_tree(proc)
                break
        reader.join(timeout=STOP_GRACE_S)
        with contextlib.suppress(queue.Empty):
            while True:
                line = sink.get_nowait()
                if line is None:
                    break
                parser.feed(line)
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            proc.wait(timeout=STOP_GRACE_S)
        duration = time.perf_counter() - started
        if timed_out:
            raise RouterTimeout(
                f"Freerouting exceeded {timeout_s:.0f} s after {parser.passes} pass(es) "
                f"({parser.unrouted if parser.unrouted is not None else '?'} unrouted); killed",
                argv=argv,
                log=parser.log,
                elapsed_s=duration,
            )
        manifest = self._read_manifest(result_json)
        result = self._result(parser, manifest, argv, run_dir, dsn_used, ses_out, duration, proc.returncode)
        result.ignore_classes = ignore_classes
        result.ignored_nets = ignored_nets
        result.warnings = [*warnings, *result.warnings]
        result.cancelled = cancelled
        if cancelled:
            raise RouterCancelled(f"cancelled after {parser.passes} pass(es), {duration:.0f} s")
        if saved_late and result.ses_written:
            result.warnings.append(
                f"the router finished as the {timeout_s:.0f} s budget ran out ({duration:.0f} s elapsed); "
                "the saved session was kept"
            )
            if proc.returncode != 0:
                result.exit_code = 0  # the JVM was stopped during its shutdown, after the save
        if result.exit_code != 0 or not result.ses_written:
            why = f"exit status {proc.returncode}" if proc.returncode else "no session file was written"
            detail = "; ".join(parser.errors[-3:]) if parser.errors else ""
            raise RouterError(
                f"Freerouting failed ({why}{': ' + detail if detail else ''})",
                argv=argv,
                exit_code=proc.returncode,
                log=parser.log,
            )
        if not self.keep_run_dir:
            shutil.rmtree(run_dir, ignore_errors=True)
            result.run_dir = None
        return result

    def _prepare_dsn(
        self, dsn: Path, run_dir: Path, nets: Iterable[str] | None, rules: RouteRules
    ) -> tuple[Path, list[str], list[str], list[str]]:
        """Copy the DSN into the run dir (Freerouting's `-drc` mode writes into the cwd and the
        original stays untouched), apply the net restriction hook, collect the `-inc` classes."""
        warnings: list[str] = []
        text = dsn.read_text(encoding="utf-8", errors="replace")
        route_nets = [str(n) for n in nets] if nets is not None else None
        post = postprocess_dsn(text, route_nets=route_nets)
        target = run_dir / dsn.name
        target.write_text(post.text, encoding="utf-8")
        ignore_classes: list[str] = list(post.ignore_classes)
        ignored: list[str] = list(post.restrict.ignored_nets) if post.restrict is not None else []
        if post.restrict is not None and post.restrict.unknown_nets:
            warnings.append(
                "nets not in the DSN (not routable): " + ", ".join(post.restrict.unknown_nets[:20])
            )
        for cls in rules.ignore_net_classes:
            if cls in post.summary.get("classes", {}) and cls not in ignore_classes:
                ignore_classes.append(cls)
        if not post.summary.get("boundary"):
            warnings.append("the DSN has no (boundary …): the board outline is missing or open")
        return target, ignore_classes, ignored, warnings

    @staticmethod
    def _read_manifest(path: Path) -> dict[str, Any] | None:
        if not path.is_file():
            return None
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return doc if isinstance(doc, dict) else None

    @staticmethod
    def _result(
        parser: _LogParser,
        manifest: dict[str, Any] | None,
        argv: list[str],
        run_dir: Path,
        dsn_used: Path,
        ses_out: Path,
        duration: float,
        exit_code: int | None,
    ) -> RouteResult:
        unrouted = parser.unrouted
        violations = parser.violations
        if manifest is not None:
            stats = manifest.get("board_statistics") or {}
            conn = stats.get("connections") or {}
            if isinstance(conn.get("incomplete_count"), int):
                unrouted = int(conn["incomplete_count"])
            cv = stats.get("clearance_violations") or {}
            if isinstance(cv.get("total_count"), int):
                violations = int(cv["total_count"])
        if unrouted is None:
            unrouted = len(parser.connections)
        ses_written = ses_out.is_file() and ses_out.stat().st_size > 0
        return RouteResult(
            passes=parser.passes,
            unrouted=unrouted,
            log=parser.log,
            duration_s=duration,
            exit_code=exit_code,
            ses_path=ses_out,
            ses_written=ses_written,
            violations=violations,
            score=parser.score,
            state=parser.state or str((manifest or {}).get("final_state") or ""),
            stop_reason=parser.stop_reason,
            unrouted_at_start=parser.unrouted_at_start,
            unrouted_connections=list(parser.connections),
            result_json=manifest,
            argv=list(argv),
            run_dir=run_dir,
            dsn_used=dsn_used,
            warnings=[
                w for w in parser.warnings if "Unknown settings property" in w or "Failed to apply" in w
            ],
            history=list(parser.history),
        )


__all__ = [
    "DEFAULT_PASSES",
    "DEFAULT_TIMEOUT_S",
    "ENFORCED_PROFILE",
    "KEEP_FAILED_RUN_DIRS",
    "RouteProgress",
    "RouteResult",
    "Router",
    "RouterCancelled",
    "RouterError",
    "RouterTimeout",
    "kill_tree",
    "write_profile",
]
