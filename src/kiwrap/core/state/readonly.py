"""Read-only mode: calls that write nothing into the project (docs/DECISIONS.md D23).

requests/kiwrap-feature-requests.review.md item 2 (and theme T2, the `detached()` context): a
reviewer running kiwrap against somebody else's project found a pre-call snapshot, a `.kiwrap/`
directory and `<board>.drc.json` in it afterwards (review §5 item 2: only `mutation()` tools write
state and snapshots; `kw_sch_stats` / `kw_pcb_render` / `kw_fab_bom` left an empty `.kiwrap/tmp`;
§3 "smaller defects": `kw_sch_get(kinds=['netlist'])` left a 319 KB `<root>.net`).

A call is **non-persisting** when the server runs read-only (`kiwrap serve --readonly` or
`KIWRAP_READONLY=1`; `ToolContext.readonly` in tests) or the call passed `persist=false` (the common
argument `make_callable` publishes on every tool). Such a call runs inside a `DetachedCall`:

- **refusal**: a tool (or an action / argument) that changes design files, the snapshot store or
  `.kiwrap/` state fails with `E_READONLY` and a teaching fix (`refusal()`); read tools, the
  validators (`kw_sch_check`, `kw_pcb_drc` without `save_board`), `kw_fab_bom`, `kw_fab_export`,
  `kw_project status` / `upgrade(dry_run)` and the listing actions run;
- **state in memory**: `mutation()` / `read_state()` load the project state from `.kiwrap/state.json`
  (or the defaults) and keep what the call changes in a per-process store (`load_state` /
  `store_state`), so a read-only session still has a stage (a clean `kw_pcb_drc` opens the
  `kw_fab_export` gate) and validator deltas; nothing is written to disk, no snapshot is taken
  (`snapshots.create` / `restore` / `ensure_repo` refuse inside a detached call);
- **lock**: the shared `<project>/.kiwrap/lock` when `.kiwrap/` exists (the extension and other
  agents use it, EXTENSION.md §6; review decision 3 "recommended"), else a lock file under the
  storage home, so no `.kiwrap/` is ever created;
- **outputs**: every kicad-cli output that would land in the project (`<board>.drc.json`,
  `<root>.erc.json`, `<root>.net`, an `export_*` into the project) is redirected into a per-call
  results directory `<storage>/results/<name>-<sha8(dir)>/<utc stamp>-<rand>/`
  (`redirect_context`); a tool's own `out_dir` defaults there (`kw_fab_export`);
  `resolve_out_path()` is the helper tools use for any output path;
- **project guard** (the layer that catches the unknown, like the stamp guard of D21): the project
  tree is scanned before and after the call; a file the call created is moved into the results
  directory (receipt paths are rewritten to the new location), a changed or deleted file is put
  back byte-for-byte (mtime included) from the copy taken before the call, and new empty
  directories are removed. KiCad-owned volatile files (`~*.lck`, `_autosave-*`, `*-backups/`,
  `.history/`) are never touched, and when another writer was active (a KiCad editor held a lock
  before the call, or another kiwrap process changed `.kiwrap/state.json` / holds `.kiwrap/lock`)
  changed files are left alone and reported instead.

The `.kicad_prl` rewrite by kicad-cli (review B2) is undone by `backends.cli.runner` itself; the
guard is the belt behind those braces.
"""

from __future__ import annotations

import contextlib
import contextvars
import copy
import datetime as _dt
import hashlib
import os
import re
import shutil
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kiwrap.core.state import KiwrapError
from kiwrap.core.state.locks import ProjectLock
from kiwrap.core.state.project import STATE_DIRNAME, STATE_FILENAME, Project, ProjectState

#: the environment switch `kiwrap serve --readonly` sets (also honoured when set by the user)
READONLY_ENV = "KIWRAP_READONLY"
#: the common argument `make_callable` adds to every tool's inputSchema
PERSIST_ARG = "persist"
PERSIST_DESCRIPTION = (
    "false = read-only call (D23): nothing is written into the project - no snapshot, no .kiwrap state, no "
    "saved refill; outputs (reports, exports, the BOM CSV) go to a temp directory under kiwrap's storage home "
    "(or your out_dir) and the receipt says where; tools that change design files refuse with E_READONLY. "
    "Default (null): persist, unless the server runs read-only (kiwrap serve --readonly / KIWRAP_READONLY=1)"
)

#: tools that run under `mutation()` but can be served without persisting (validators and exports);
#: every other tool not annotated read-only is refused (`refusal`)
DETACHABLE_TOOLS = frozenset({"kw_sch_check", "kw_pcb_drc", "kw_fab_export", "kw_fab_bom", "kw_job"})
#: per-tool actions a non-persisting call may run (the rest of the tool's actions write)
READ_ACTIONS: dict[str, frozenset[str]] = {
    "kw_project": frozenset({"status"}),
    "kw_snapshot": frozenset({"list", "diff"}),
    "kw_intent": frozenset({"get", "diff"}),
    "kw_live": frozenset({"status", "screenshot", "raise", "detach"}),
}
#: what a refused tool can do instead in read-only mode (the E_READONLY `candidates`)
READ_ALTERNATIVES: dict[str, tuple[str, ...]] = {
    "kw_project": ("kw_project(action='status')", "kw_project(action='upgrade', dry_run=true)"),
    "kw_snapshot": ("kw_snapshot(action='list')", "kw_snapshot(action='diff', ref=...)"),
    "kw_intent": ("kw_intent(action='get')", "kw_intent(action='diff')"),
    "kw_live": ("kw_live(action='status')", "kw_live(action='screenshot')"),
    "kw_pcb_drc": ("kw_pcb_drc(save_board=false)",),
}
_DEFAULT_ALTERNATIVES = (
    "kw_sch_get",
    "kw_sch_stats",
    "kw_sch_check",
    "kw_pcb_get",
    "kw_pcb_check",
    "kw_pcb_drc",
    "kw_pcb_render",
    "kw_fab_bom",
    "kw_fab_export(out_dir=...)",
)

#: `<storage>/results/<project key>/<call dir>` retention (review B4's rule, applied to results):
#: keep the newest `RESULTS_KEEP` per project, drop anything older than `RESULTS_MAX_AGE_S`, never
#: a directory younger than `RESULTS_MIN_AGE_S` (a concurrent call may still be writing it)
RESULTS_DIRNAME = "results"
RESULTS_KEEP = 20
RESULTS_MAX_AGE_S = 24 * 3600.0
RESULTS_MIN_AGE_S = 60.0
#: the name of a call directory (`new_results_dir`); nothing else under a results folder is swept
_CALL_DIR = re.compile(r"^\d{8}T\d{6}-[0-9a-f]{6}$")

#: directories the project guard never walks (VCS stores, caches)
_SKIP_DIRS = frozenset({".git", "snapshots.git", "node_modules", "__pycache__", ".venv", ".svn", ".hg"})
#: stop scanning a project tree after this many files (a project pointed at a home directory)
_MAX_SCAN_FILES = 20000
#: the guard keeps a copy of files it may have to put back, up to these sizes
_BACKUP_BUDGET = 256 * 1024 * 1024
_BACKUP_MAX_FILE = 64 * 1024 * 1024


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def enable_server_readonly() -> None:
    """`kiwrap serve --readonly`: every call of this process (and of any kiwrap it spawns) is
    non-persisting."""
    os.environ[READONLY_ENV] = "1"


def server_readonly(ctx: Any = None) -> bool:
    """The server-wide switch: `ctx.readonly` when a caller set it (tests, embedders), else
    `KIWRAP_READONLY`."""
    flag = getattr(ctx, "readonly", None)
    if isinstance(flag, bool):
        return flag
    return _truthy(os.environ.get(READONLY_ENV))


def _persist_of(args: Any) -> bool | None:
    if isinstance(args, bool) or args is None:
        return args
    if isinstance(args, dict):
        value = args.get(PERSIST_ARG)
    else:
        value = getattr(args, PERSIST_ARG, None)
    return value if isinstance(value, bool) else None


def is_readonly(ctx: Any, args: Any = None) -> bool:
    """True when a call must not persist: the server runs read-only, the call passed
    `persist=false` (`args`: the call's arguments as a model / dict, or the bare `persist`
    value), or the calling thread already runs inside a non-persisting call."""
    if server_readonly(ctx) or current() is not None:
        return True
    return _persist_of(args) is False


# ---- refusal ------------------------------------------------------------------------------------


def refusal(tool: str, args: Any, *, read_only_hint: bool) -> str | None:
    """Why `tool` with `args` would write the project (None: it may run non-persisting)."""
    action = getattr(args, "action", None)
    if tool in READ_ACTIONS:
        if action in READ_ACTIONS[tool]:
            return None
        if tool == "kw_project" and action == "upgrade" and bool(getattr(args, "dry_run", False)):
            return None  # converts a copy under the storage home and reports; never replaces files
        what = {
            "kw_project": "creates, pins or converts project files",
            "kw_snapshot": "writes the snapshot store or restores files",
            "kw_intent": "writes <project>/.kiwrap/intent.json",
            "kw_live": "opens the project in a KiCad GUI (which saves .kicad_prl and whatever is edited there)",
        }[tool]
        return f"action='{action}' {what}"
    if tool == "kw_pcb_drc":
        if bool(getattr(args, "save_board", False)) and getattr(args, "refill_zones", None) is not False:
            return "save_board=true writes the refilled zones back into the board"
        return None
    if tool in DETACHABLE_TOOLS or read_only_hint:
        return None
    return "changes design files (and takes a snapshot)"


def readonly_error(tool: str, reason: str, *, mode: str) -> KiwrapError:
    """E_READONLY: `tool` would write the project in a non-persisting call (`mode`: 'server' when
    the server runs read-only, 'call' for `persist=false`)."""
    if mode == "server":
        where = "this kiwrap server runs read-only (kiwrap serve --readonly / KIWRAP_READONLY=1)"
        fix = (
            "this session only reads: tell the user the change needs a writable kiwrap (restart it without "
            "--readonly / KIWRAP_READONLY, or use a second server) - do not work around it by editing files. "
            "Reads, checks and exports still work: "
            + ", ".join(READ_ALTERNATIVES.get(tool, _DEFAULT_ALTERNATIVES))
        )
    else:
        where = "the call passed persist=false"
        fix = (
            f"call {tool} again without persist=false to make the change (it is snapshotted; kw_snapshot restore "
            "undoes it), or keep persist=false with a read: "
            + ", ".join(READ_ALTERNATIVES.get(tool, _DEFAULT_ALTERNATIVES))
        )
    return KiwrapError(
        "E_READONLY",
        f"{tool}: {reason}; {where}",
        why="a read-only call writes nothing into the project: no design file, no snapshot, no .kiwrap state (D23)",
        fix=fix,
        candidates=list(READ_ALTERNATIVES.get(tool, _DEFAULT_ALTERNATIVES)),
        example=READ_ALTERNATIVES.get(tool, ("kw_pcb_drc(project=...)",))[0],
        data={"tool": tool, "mode": mode, "reason": reason},
    )


# ---- tool registry (for withholding suggestions a read-only session cannot follow) ---------------------

_READ_ONLY_TOOLS: dict[str, bool] = {}


def register_tool(name: str, *, read_only_hint: bool) -> None:
    """`make_callable` records every tool's annotation (next_suggested filtering)."""
    _READ_ONLY_TOOLS[name] = bool(read_only_hint)


def runs_readonly(tool: str) -> bool:
    """Whether `tool` can run in a read-only session at all (some of its actions at least)."""
    return tool in READ_ACTIONS or tool in DETACHABLE_TOOLS or _READ_ONLY_TOOLS.get(tool, False)


# ---- the in-memory project state -----------------------------------------------------------------------


@dataclass
class _Held:
    state: ProjectState
    disk: tuple[int, int] | None  # (mtime_ns, size) of .kiwrap/state.json the state derives from


_STATES: dict[str, _Held] = {}
_STATES_LOCK = threading.Lock()


def _state_key(project: Project) -> str:
    try:
        return str(project.dir.resolve())
    except OSError:
        return str(project.dir)


def _disk_signature(project: Project) -> tuple[int, int] | None:
    try:
        st = project.state_path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def load_state(project: Project) -> ProjectState:
    """The state a non-persisting call works on: what earlier read-only calls of this process left
    (while `.kiwrap/state.json` is unchanged since), else the file (never written), else defaults."""
    key, disk = _state_key(project), _disk_signature(project)
    with _STATES_LOCK:
        held = _STATES.get(key)
        if held is not None and held.disk == disk:
            return copy.deepcopy(held.state)
        _STATES.pop(key, None)
    return project.load_state()


def store_state(project: Project, state: ProjectState) -> None:
    """Keep a non-persisting call's state in memory (never on disk)."""
    with _STATES_LOCK:
        _STATES[_state_key(project)] = _Held(copy.deepcopy(state), _disk_signature(project))


def forget_state(project: Project | None = None) -> None:
    """Drop the in-memory state of `project` (every project when None)."""
    with _STATES_LOCK:
        if project is None:
            _STATES.clear()
        else:
            _STATES.pop(_state_key(project), None)


# ---- lock -------------------------------------------------------------------------------------------------


def project_key(project_dir: Path, name: str = "") -> str:
    """`<name>-<sha1(normcase(resolved dir))[:12]>`: the per-project folder name under the storage
    home - the same key as `core.tools._data.project_results_dir` (the DRC result store), so a
    project's results share one folder."""
    try:
        folder = Path(project_dir).resolve()
    except OSError:
        folder = Path(project_dir)
    digest = hashlib.sha1(os.path.normcase(str(folder)).encode("utf-8")).hexdigest()[:12]  # noqa: S324 - a name
    label = re.sub(r"[^A-Za-z0-9_.-]+", "_", name or folder.name)[:40] or "project"
    return f"{label}-{digest}"


@contextlib.contextmanager
def detached_lock(storage_home: Path, project: Project, *, timeout_s: float = 60.0) -> Iterator[Path]:
    """The mutation lock of a non-persisting call: the project's own `.kiwrap/lock` when `.kiwrap/`
    already exists (shared with the extension and writable kiwrap servers), else one under the
    storage home - so a read-only call never creates `.kiwrap/`. Yields the lock path."""
    if project.state_dir.is_dir():
        path = project.lock_path
    else:
        path = Path(storage_home) / "locks" / f"{project_key(project.dir, project.name)}.lock"
    with ProjectLock(path, timeout_s=timeout_s):
        yield path


# ---- outputs ----------------------------------------------------------------------------------------------


def results_root(storage_home: Path, project_dir: Path | None, name: str = "") -> Path:
    key = project_key(project_dir, name) if project_dir is not None else "_"
    return Path(storage_home) / RESULTS_DIRNAME / key


def sweep_results(root: Path, *, now: float | None = None) -> list[Path]:
    """Delete old call directories under one project's results root (keep the newest
    `RESULTS_KEEP`, drop anything older than `RESULTS_MAX_AGE_S`, never one younger than
    `RESULTS_MIN_AGE_S`). Returns what was deleted."""
    now = time.time() if now is None else now
    try:
        # only this module's call directories: the folder also holds other stores (`drc/`)
        entries = sorted(
            (p for p in Path(root).iterdir() if p.is_dir() and _CALL_DIR.match(p.name)),
            key=lambda p: p.name,
            reverse=True,
        )
    except OSError:
        return []
    gone: list[Path] = []
    for i, d in enumerate(entries):
        try:
            age = now - d.stat().st_mtime
        except OSError:
            continue
        if age < RESULTS_MIN_AGE_S:
            continue
        if i >= RESULTS_KEEP or age > RESULTS_MAX_AGE_S:
            shutil.rmtree(d, ignore_errors=True)
            gone.append(d)
    return gone


def new_results_dir(storage_home: Path, project_dir: Path | None, name: str = "") -> Path:
    """A fresh `<storage>/results/<project key>/<UTC stamp>-<rand>/` (older ones swept)."""
    root = results_root(storage_home, project_dir, name)
    sweep_results(root)
    stamp = _dt.datetime.now(_dt.UTC).strftime("%Y%m%dT%H%M%S")
    d = root / f"{stamp}-{uuid.uuid4().hex[:6]}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        return False
    return True


def _user_config_dirs() -> list[Path]:
    try:
        from kiwrap.platform import current as current_platform

        plat = current_platform()
    except Exception:  # noqa: BLE001 - an unknown platform has no config dirs to protect here
        return []
    dirs = [plat.user_kicad_config_home]
    if plat.user_kicad_documents_home is not None:
        dirs.append(plat.user_kicad_documents_home)
    return dirs


def resolve_out_path(
    ctx: Any, project: Project | None, requested: str | None, kind: str, *, default: Path | None = None
) -> Path:
    """Where a tool writes an output of `kind` (review theme T2's one helper).

    - `requested` (an absolute path, anywhere the caller chooses): used as given. Refused
      (E_BAD_ARGS): a relative path, a `.kicad_*` target (a design file is never an output), a
      path inside KiCad's user config or documents directories, a missing parent directory.
      Inside a non-persisting call it is exempt from the project guard (the caller asked for it).
    - `requested` 'auto', or unset in a non-persisting call, or unset without a `default`: the
      call's results directory under the storage home (`<storage>/results/<project key>/<call>/<kind>`).
    - unset otherwise: `default` (a persisting tool's usual place, e.g. `<project>/fab`).
    """
    call = current()
    if requested and requested.strip().lower() != "auto":
        raw = Path(requested).expanduser()
        if not raw.is_absolute():
            raise KiwrapError(
                "E_BAD_ARGS",
                f"output path {requested!r} is not absolute",
                why="a relative path would be resolved against the server's working directory, not the project",
                fix="pass an absolute directory, or omit it (or 'auto') for a temp directory under kiwrap's storage home",
            )
        path = raw.resolve()
        if ".kicad_" in path.name:
            raise KiwrapError(
                "E_BAD_ARGS",
                f"output path {path.as_posix()} names a KiCad design file",
                why="an output never overwrites a .kicad_* file",
                fix="pass a directory (or a report file name that is not .kicad_*)",
            )
        for cfg in _user_config_dirs():
            if _inside(path, cfg):
                raise KiwrapError(
                    "E_BAD_ARGS",
                    f"output path {path.as_posix()} is inside KiCad's user directory {cfg.as_posix()}",
                    why="kiwrap never writes to the user's KiCad configuration or documents directories",
                    fix="choose another directory",
                )
        if not path.parent.exists():
            raise KiwrapError(
                "E_BAD_ARGS",
                f"the parent of {path.as_posix()} does not exist",
                why="kiwrap creates the output directory, not a whole tree of missing parents",
                fix="create the parent directory first, or pass an existing directory",
            )
        if call is not None:
            call.allow(path)
        return path
    if call is None and default is not None and not (requested or "").strip():
        return default
    if call is not None:
        out = call.results(kind)
    else:
        storage = Path(getattr(getattr(ctx, "services", None), "storage_home", None) or Path.cwd())
        out = (
            new_results_dir(storage, project.dir, project.name)
            if project is not None
            else new_results_dir(storage, None)
        ) / kind
    out.mkdir(parents=True, exist_ok=True)
    return out


# ---- the project guard -----------------------------------------------------------------------------------


def _volatile(rel: str) -> bool:
    """KiCad's own transient files (GUI locks, autosaves, backups, local history) and kiwrap's lock:
    never moved, restored or reported."""
    parts = rel.split("/")
    name = parts[-1]
    if name.endswith(".lck") or name.startswith("_autosave-"):
        return True
    if any(p.endswith("-backups") or p == ".history" for p in parts[:-1]):
        return True
    return rel.startswith(f"{STATE_DIRNAME}/lock")


def _backup_worthy(rel: str) -> bool:
    parts = rel.split("/")
    if ".kicad_" in parts[-1] or parts[-1].endswith("-lib-table") or parts[-1] == "fp-info-cache":
        return True
    if len(parts) == 1:
        return True
    return len(parts) == 2 and parts[0] == STATE_DIRNAME


@dataclass
class _Scan:
    files: dict[str, tuple[int, int]] = field(default_factory=dict)  # rel -> (size, mtime_ns)
    dirs: set[str] = field(default_factory=set)
    truncated: bool = False


def _scan(root: Path) -> _Scan:
    out = _Scan()
    root = Path(root)
    for dirpath, dirnames, filenames in os.walk(root):
        base = Path(dirpath)
        rel_dir = base.relative_to(root).as_posix()
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
        for d in dirnames:
            out.dirs.add(d if rel_dir == "." else f"{rel_dir}/{d}")
        for name in filenames:
            rel = name if rel_dir == "." else f"{rel_dir}/{name}"
            try:
                st = (base / name).stat()
            except OSError:
                continue
            out.files[rel] = (st.st_size, st.st_mtime_ns)
        if len(out.files) > _MAX_SCAN_FILES:
            out.truncated = True
            break
    return out


@dataclass
class GuardReport:
    """What the project guard found after a non-persisting call."""

    relocated: list[tuple[Path, Path]] = field(default_factory=list)  # (was in the project, now in results)
    restored: list[str] = field(default_factory=list)  # changed / deleted files put back
    removed_dirs: list[str] = field(default_factory=list)  # new empty directories removed
    left: list[dict[str, str]] = field(default_factory=list)  # {path, why}: changes the guard did not undo
    errors: list[str] = field(default_factory=list)

    def empty(self) -> bool:
        return not (self.relocated or self.restored or self.removed_dirs or self.left or self.errors)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.relocated:
            out["relocated"] = [{"from": a.as_posix(), "to": b.as_posix()} for a, b in self.relocated]
        if self.restored:
            out["restored"] = list(self.restored)
        if self.removed_dirs:
            out["removed_dirs"] = list(self.removed_dirs)
        if self.left:
            out["left"] = list(self.left)
        if self.errors:
            out["errors"] = list(self.errors)
        return out


class ProjectGuard:
    """Snapshot of a project tree before a non-persisting call, and the undo after it (module
    docstring). Nothing here raises: a guard failure is reported, never masks the tool's result."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.before = _Scan()
        self.backup: dict[str, bytes] = {}
        self.gui_locks: list[str] = []
        self.state_sig: tuple[int, int] | None = None
        self.started = False

    def start(self) -> None:
        self.before = _scan(self.root)
        self.gui_locks = sorted(
            p for p in self.before.files if p.split("/")[-1].startswith("~") and p.endswith(".lck")
        )
        self.state_sig = self._state_signature()
        budget = _BACKUP_BUDGET
        for rel, (size, _mtime) in sorted(self.before.files.items()):
            if _volatile(rel) or not _backup_worthy(rel) or size > _BACKUP_MAX_FILE or size > budget:
                continue
            try:
                self.backup[rel] = (self.root / rel).read_bytes()
            except OSError:
                continue
            budget -= size
        self.started = True

    def _state_signature(self) -> tuple[int, int] | None:
        try:
            st = (self.root / STATE_DIRNAME / STATE_FILENAME).stat()
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def finish(self, results: Callable[[], Path], *, exempt: list[Path]) -> GuardReport:
        report = GuardReport()
        if not self.started:
            return report
        try:
            self._finish(report, results, exempt)
        except Exception as exc:  # noqa: BLE001 - the guard never masks the tool's own result
            report.errors.append(f"{type(exc).__name__}: {exc}")
        return report

    def _exempt(self, rel: str, exempt: list[Path]) -> bool:
        path = self.root / rel
        return any(_inside(path, e) for e in exempt)

    def _finish(self, report: GuardReport, results: Callable[[], Path], exempt: list[Path]) -> None:
        after = _scan(self.root)
        foreign: list[str] = []
        if self.gui_locks:
            foreign.append("a KiCad editor has the project open (" + ", ".join(self.gui_locks[:3]) + ")")
        if self._state_signature() != self.state_sig:
            foreign.append("another kiwrap process changed .kiwrap/state.json during the call")
        if (self.root / STATE_DIRNAME / "lock").exists():
            foreign.append("another kiwrap call holds .kiwrap/lock")
        # files the call created: moved out of the project (while nobody else writes it)
        for rel in sorted(set(after.files) - set(self.before.files)):
            if _volatile(rel) or self._exempt(rel, exempt):
                continue
            if foreign:
                report.left.append(
                    {"path": rel, "why": "created during the call; left because " + foreign[0]}
                )
                continue
            src = self.root / rel
            dst = results() / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            report.relocated.append((src, dst))
        # files the call changed or deleted: put back from the copy taken before the call
        for rel, sig in sorted(self.before.files.items()):
            if _volatile(rel) or self._exempt(rel, exempt):
                continue
            now = after.files.get(rel)
            if now == sig:
                continue
            path = self.root / rel
            old = self.backup.get(rel)
            if old is not None and now is not None and now[0] == sig[0]:
                with contextlib.suppress(OSError):
                    if path.read_bytes() == old:
                        os.utime(path, ns=(sig[1], sig[1]))  # touched, not changed: keep the old mtime
                        continue
            if foreign:
                report.left.append(
                    {"path": rel, "why": ("deleted" if now is None else "changed") + "; " + foreign[0]}
                )
                continue
            if old is None:
                report.left.append(
                    {
                        "path": rel,
                        "why": ("deleted" if now is None else "changed") + " (no copy was kept: too large)",
                    }
                )
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(old)
            os.utime(path, ns=(sig[1], sig[1]))
            report.restored.append(rel)
        # directories the call created (now empty): removed, deepest first
        for rel in (
            []
            if foreign
            else sorted(set(after.dirs) - set(self.before.dirs), key=lambda r: r.count("/"), reverse=True)
        ):
            if _volatile(rel + "/x") or self._exempt(rel, exempt):
                continue
            path = self.root / rel
            try:
                path.rmdir()
            except OSError:
                continue
            report.removed_dirs.append(rel)
        if after.truncated or self.before.truncated:
            report.errors.append(
                f"the project tree holds more than {_MAX_SCAN_FILES} files: only part of it was checked"
            )


# ---- the detached call -----------------------------------------------------------------------------------

_CURRENT: contextvars.ContextVar[DetachedCall | None] = contextvars.ContextVar(
    "kiwrap_detached", default=None
)

#: kicad-cli backends whose output would land beside their input (or in a caller-given path):
#: `{service name: (positional index of the output argument or None, its keyword, default suffix)}`.
#: A default suffix means "None = beside the input" (`<board>.drc.json`, `<root>.erc.json`,
#: `<root>.net` - review item 2 and §3), which a non-persisting call sends to its results directory.
OUTPUT_ARGS: dict[str, tuple[int | None, str, str | None]] = {
    "run_erc": (None, "out_json", ".erc.json"),
    "run_drc": (None, "out_json", ".drc.json"),
    "export_netlist": (3, "out", ".net"),
    "export_sch": (4, "out", None),
    "export_pcb": (4, "out_dir", None),
    "sch_diff": (None, "out", None),
    "export_stats": (None, "out_dir", None),
    "export_stackup": (None, "out_dir", None),
    "export_ratsnest": (None, "out_dir", None),
}


_GUARD_LOCKS: dict[str, Any] = {}
_GUARD_LOCKS_LOCK = threading.Lock()


def _guard_lock(project: Project) -> Any:
    """The in-process lock serialising the guarded windows of one project (re-entrant)."""
    key = _state_key(project)
    with _GUARD_LOCKS_LOCK:
        lock = _GUARD_LOCKS.get(key)
        if lock is None:
            lock = _GUARD_LOCKS[key] = threading.RLock()
        return lock


def current() -> DetachedCall | None:
    """The non-persisting call running in this thread (None: a normal, persisting call)."""
    return _CURRENT.get()


class DetachedCall:
    """One non-persisting tool call: the mode, the per-call results directory, the project guard
    and the redirected services. Use as a context manager around the handler; `annotate()` then
    stamps the receipt."""

    def __init__(
        self, ctx: Any, tool: str, *, mode: str, project: Project | None, allowed: list[Path] | None = None
    ):
        self.tool = tool
        self.mode = mode
        self.project = project
        self.storage_home = Path(getattr(getattr(ctx, "services", None), "storage_home", None) or Path.cwd())
        self.notes: list[str] = []
        self.allowed: list[Path] = list(allowed or [])
        self.report = GuardReport()
        self._results: Path | None = None
        self._token: contextvars.Token[DetachedCall | None] | None = None
        self._guard_lock: threading.RLock | None = None
        self.guard = ProjectGuard(project.dir) if project is not None and project.dir.is_dir() else None
        self.ctx = redirect_context(ctx, self)

    # -- outputs --
    def results(self, kind: str | None = None) -> Path:
        """This call's results directory (created on first use), or its `kind` subdirectory."""
        if self._results is None:
            p = self.project
            self._results = new_results_dir(self.storage_home, p.dir if p else None, p.name if p else "")
        if not kind:
            return self._results
        out = self._results / kind
        out.mkdir(parents=True, exist_ok=True)
        return out

    @property
    def results_dir(self) -> Path | None:
        return self._results

    def allow(self, path: Path) -> None:
        """A caller-chosen output location: the guard leaves what the call writes there."""
        self.allowed.append(Path(path))

    def redirect(self, path: Any) -> Any:
        """`path` unchanged unless it lies inside the project: then the same relative path under
        the results directory."""
        if path is None or self.project is None:
            return path
        p = Path(path)
        try:
            rel = p.resolve().relative_to(self.project.dir.resolve())
        except (ValueError, OSError):
            return path
        if any(_inside(p, a) for a in self.allowed):
            return path
        out = self.results() / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        return out if isinstance(path, Path) else str(out)

    # -- context --
    def __enter__(self) -> DetachedCall:
        self._token = _CURRENT.set(self)
        if self.guard is not None and self.project is not None:
            # one guarded window per project at a time in this process: a second read-only call
            # must not see the first one's in-flight files as "created by me" (or vice versa)
            self._guard_lock = _guard_lock(self.project)
            self._guard_lock.acquire()
            try:
                self.guard.start()
            except Exception as exc:  # noqa: BLE001 - reported; the call still runs
                self.report.errors.append(f"guard start: {type(exc).__name__}: {exc}")
        return self

    def __exit__(self, *exc: object) -> None:
        try:
            if self.guard is not None and self.guard.started:
                self._release_own_servers()
                self.report = self.guard.finish(lambda: self.results("project"), exempt=self.allowed)
        finally:
            if self._guard_lock is not None:
                self._guard_lock.release()
                self._guard_lock = None
            if self._token is not None:
                _CURRENT.reset(self._token)
                self._token = None

    def _release_own_servers(self) -> None:
        """A kiwrap api-server a read pooled on this project holds `~*.lck` (and rewrites
        `.kicad_prl` on close): close it now when the call created such a lock, so the project is
        left as it was found."""
        guard = self.guard
        if guard is None or self.project is None:
            return
        try:
            fresh = [
                p
                for p in self.project.dir.glob("~*.lck")
                if self.project.relpath(p) not in guard.before.files
            ]
        except OSError:
            return
        release = getattr(getattr(self.ctx, "services", None), "ipc_release_project", None)
        if fresh and callable(release):
            with contextlib.suppress(Exception):
                release(self.project.dir)

    # -- receipt --
    def info(self) -> dict[str, Any]:
        out: dict[str, Any] = {"mode": self.mode, "persisted": False}
        if self._results is not None:
            out["results_dir"] = self._results.as_posix()
        out.update(self.report.to_dict())
        return out

    def warnings(self) -> list[str]:
        w = list(self.notes)
        r = self.report
        if r.relocated:
            w.append(
                "read-only: "
                + ", ".join(a.name for a, _ in r.relocated[:6])
                + (" ..." if len(r.relocated) > 6 else "")
                + f" would have been written into the project; moved to {self.results().as_posix()}"
            )
        if r.restored:
            w.append("read-only: put back " + ", ".join(r.restored[:6]) + " (changed during the call)")
        if r.left:
            w.append("read-only: not undone - " + "; ".join(f"{x['path']}: {x['why']}" for x in r.left[:4]))
        w.extend(f"read-only guard: {e}" for e in r.errors)
        return w

    def mapping(self) -> list[tuple[str, str]]:
        pairs: list[tuple[str, str]] = []
        for a, b in self.report.relocated:
            pairs.append((a.as_posix(), b.as_posix()))
            if str(a) != a.as_posix():
                pairs.append((str(a), str(b)))
        return pairs

    def annotate(self, result: Any) -> Any:
        """Stamp a receipt (or a CallToolResult's structured content) with `data.readonly`, the
        guard's warnings, rewritten output paths and no snapshot; withhold a `next_suggested` a
        read-only session cannot run."""
        pairs = self.mapping()
        data = getattr(result, "data", None)
        if isinstance(data, dict):  # a Receipt
            if pairs:
                result.data = _rewrite(data, pairs)
                result.changed = _rewrite(result.changed, pairs)
            result.snapshot = None
            nxt = result.next_suggested
            if self.mode == "server" and nxt and not runs_readonly(nxt):
                result.next_suggested = None
            info = self.info()
            if self.mode == "server" and nxt and result.next_suggested is None:
                info["next_suggested_withheld"] = nxt
            result.data.setdefault("readonly", info)
            result.warnings.extend(x for x in self.warnings() if x not in result.warnings)
            return result
        structured = getattr(result, "structured_content", None)
        if isinstance(structured, dict):
            if pairs:
                structured = dict(_rewrite(structured, pairs))
            inner = structured.get("data")
            target: dict[str, Any] = inner if isinstance(inner, dict) else structured
            target.setdefault("readonly", self.info())
            result.structured_content = structured
        return result


def _rewrite(obj: Any, pairs: list[tuple[str, str]]) -> Any:
    """Replace every relocated path inside the strings of a JSON-like value."""
    if isinstance(obj, str):
        for old, new in pairs:
            if old in obj:
                obj = obj.replace(old, new)
        return obj
    if isinstance(obj, dict):
        return {k: _rewrite(v, pairs) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_rewrite(v, pairs) for v in obj]
    if isinstance(obj, tuple):
        return tuple(_rewrite(v, pairs) for v in obj)
    return obj


def _wrap_output(
    fn: Callable[..., Any], call: DetachedCall, pos: int | None, kw: str, suffix: str | None
) -> Callable[..., Any]:
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        args_l = list(args)
        if pos is not None and len(args_l) > pos:
            args_l[pos] = call.redirect(args_l[pos])
            if args_l[pos] is None and suffix and len(args_l) > 2:
                args_l[pos] = call.results() / (Path(args_l[2]).stem + suffix)
        elif kw in kwargs and kwargs[kw] is not None:
            kwargs[kw] = call.redirect(kwargs[kw])
        elif suffix and len(args_l) > 2:
            kwargs[kw] = call.results() / (Path(args_l[2]).stem + suffix)
        return fn(*args_l, **kwargs)

    wrapped.__name__ = getattr(fn, "__name__", "wrapped")
    wrapped.__wrapped__ = fn  # type: ignore[attr-defined]
    return wrapped


def redirect_context(ctx: Any, call: DetachedCall) -> Any:
    """A shallow copy of the tool context whose services send every output that would land in the
    project (`OUTPUT_ARGS`) to the call's results directory. The copy shares the caches (installs,
    capabilities, config roots) and the job queue with the original."""
    services = getattr(ctx, "services", None)
    if ctx is None or services is None:
        return ctx
    svc = copy.copy(services)
    for name, (pos, kw, suffix) in OUTPUT_ARGS.items():
        fn = getattr(services, name, None)
        if callable(fn):
            setattr(svc, name, _wrap_output(fn, call, pos, kw, suffix))
    new = copy.copy(ctx)
    new.services = svc
    new.detached = call
    return new


def begin(
    ctx: Any, tool: str, args: Any, persist: bool | None, *, read_only_hint: bool, project_spec: str | None
) -> DetachedCall | None:
    """The non-persisting call for `tool`, or None when the call persists. Raises E_READONLY when
    the call would write the project."""
    server = server_readonly(ctx)
    if not server and persist is not False and current() is None:
        return None
    mode = "server" if server else "call"
    reason = refusal(tool, args, read_only_hint=read_only_hint)
    if reason is not None:
        raise readonly_error(tool, reason, mode=mode)
    project: Project | None = None
    if project_spec:
        with contextlib.suppress(KiwrapError, OSError):
            project = Project.from_path(project_spec)
    allowed: list[Path] = []
    for name in ("out_dir", "out_path", "out"):
        value = getattr(args, name, None)
        if isinstance(value, str) and value.strip() and value.strip().lower() != "auto":
            with contextlib.suppress(OSError, ValueError):
                allowed.append(Path(value).expanduser().resolve())
    call = DetachedCall(ctx, tool, mode=mode, project=project, allowed=allowed)
    if server and persist is True:
        call.notes.append("persist=true is ignored: this kiwrap server runs read-only (D23)")
    return call


def prepare_args(call: DetachedCall, args: Any) -> Any:
    """Arguments a non-persisting call needs changed: `run_async` off (a background job would
    outlive the guard) and an unset `out_dir` pointed at the results directory (an export must
    never clean or fill `<project>/fab`)."""
    fields = getattr(type(args), "model_fields", {})
    update: dict[str, Any] = {}
    if "run_async" in fields and getattr(args, "run_async", False):
        update["run_async"] = False
        call.notes.append(
            "read-only: ran synchronously (run_async=true is not available in a read-only call)"
        )
    if "out_dir" in fields and not getattr(args, "out_dir", None):
        update["out_dir"] = str(call.results(call.tool.removeprefix("kw_")))
    return args.model_copy(update=update) if update else args


def refuse_writes_unless_detachable(tool: str) -> None:
    """The prologue check of `mutation()` inside a non-persisting call: only the tools that can
    run detached (validators, exports, the read actions of kw_project / kw_snapshot / kw_intent /
    kw_live) get a detached mutation; anything else is E_READONLY (a handler called directly,
    bypassing `begin`)."""
    call = current()
    if call is not None and tool not in DETACHABLE_TOOLS and tool not in READ_ACTIONS:
        raise readonly_error(tool, "changes design files (and takes a snapshot)", mode=call.mode)


def refuse_writes(what: str) -> None:
    """Belt and braces for the layers below the tools: raise E_READONLY when a non-persisting call
    reaches a write (`snapshots.create` / `restore` / `ensure_repo`)."""
    call = current()
    if call is not None:
        raise readonly_error(call.tool, what, mode=call.mode)
