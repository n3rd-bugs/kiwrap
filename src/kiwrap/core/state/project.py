"""`Project`: one KiCad project directory plus kiwrap's per-project state file.

Layout kiwrap relies on (docs/ARCHITECTURE.md §11, "the project path is the handle"):

    <dir>/<name>.kicad_pro          exactly one per project directory
    <dir>/<name>.kicad_sch          root schematic (same stem as the .kicad_pro)
    <dir>/<name>.kicad_pcb          board (same stem)
    <dir>/.kiwrap/state.json        stage + per-stage content hashes + target_kicad
    <dir>/.kiwrap/lock              per-project cross-process mutation lock
    <dir>/.kiwrap/snapshots.git     the private git store behind kw_snapshot (never a `.git` in <dir>)
    <dir>/.kiwrap/.gitignore        hides the store, lock and tmp from a user repository enclosing <dir>

The state file is plain JSON so that a human (or a git diff) can read it; it is committed with
every snapshot so `kw_snapshot restore` also restores the stage.

Format stamps (docs/DECISIONS.md D21, requests/kiwrap-feature-requests.review.md item 1 / B1): every
`.kicad_sch` / `.kicad_pcb` starts `(kicad_xxx (version N) ...)`; `file_epoch()` reads N from the
first 4 KB and `Project.format_stamps()` lists the stamp of every design file, the input of
`kiwrap.core.tools._base.effective_target` (which KiCad may write this project) and of the stamp
guard in the mutation epilogue. Which stamp belongs to which KiCad release is not known here.

The sheets of a project are the root, every top-level `.kicad_sch` AND every sheet the hierarchy
references through `(property "Sheetfile" ...)`, followed recursively relative to the referencing
sheet (KiCad resolves a relative sheet path against the parent sheet's directory). KiCad's own 10.0
demo royalblue54L_feather keeps its four sub-sheets under `sch/`; a top-level glob alone hid them
from the target resolution, the stamp guard, `kw_project status` and the upgrade (now
`kw_project_upgrade`, D24), which
then left them in the 9.0 format (fixer finding 2 on the D21 work).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from kiwrap.core.state import STAGES, KiwrapError

STATE_DIRNAME = ".kiwrap"
STATE_FILENAME = "state.json"
STATE_FORMAT_VERSION = 1

#: `ProjectState.target_source`: how `target_kicad` got there. `created` = kw_project create
#: wrote the files in that install's format; `pinned` = kw_project retarget / upgrade (or a
#: hand-edited / extension-written state.json that predates the field). None with no target.
TARGET_SOURCES = ("created", "pinned")

#: design-file suffix -> the `formats.<kind>` capability key that stamps it
DESIGN_KINDS: dict[str, str] = {".kicad_sch": "sch", ".kicad_pcb": "pcb"}
_VERSION_RE = re.compile(rb"\(version\s+(\d+)\s*\)")
_STAMP_HEAD_BYTES = 4096
#: the file a hierarchical sheet instantiates: `(property "Sheetfile" "sch/x.kicad_sch" ...)` (7+) or
#: `(property "Sheet file" ...)` (6.0); KiCad escapes `"` and `\` inside strings with a backslash
_SHEETFILE_RE = re.compile(rb'\(property\s+"(?:Sheetfile|Sheet file)"\s+"((?:[^"\\]|\\.)*)"')
_ESCAPE_RE = re.compile(r"\\(.)")
#: directories a referenced sheet is never taken from (kiwrap's store, VCS, KiCad's backups, exports)
_SKIP_SHEET_DIRS = frozenset({".kiwrap", ".git", ".history", "node_modules"})
#: `sheet_references` cache: {normcased path: (mtime_ns, size, references)} (a status call or a
#: mutation prologue re-lists the hierarchy several times; the files rarely change in between)
_REF_CACHE: dict[str, tuple[int, int, tuple[str, ...]]] = {}
_REF_CACHE_MAX = 1024


def sheet_references(path: Path) -> tuple[str, ...]:
    """The `Sheetfile` values of the hierarchical sheets placed in one `.kicad_sch` (as written:
    relative to that sheet's directory, or absolute); () when unreadable. Cached on the file's
    (mtime_ns, size)."""
    path = Path(path)
    try:
        st = path.stat()
    except OSError:
        return ()
    key = os.path.normcase(str(path))
    hit = _REF_CACHE.get(key)
    if hit is not None and hit[0] == st.st_mtime_ns and hit[1] == st.st_size:
        return hit[2]
    try:
        data = path.read_bytes()
    except OSError:
        return ()
    refs = tuple(
        _ESCAPE_RE.sub(r"\1", m.group(1).decode("utf-8", errors="replace"))
        for m in _SHEETFILE_RE.finditer(data)
    )
    if len(_REF_CACHE) >= _REF_CACHE_MAX:
        _REF_CACHE.clear()
    _REF_CACHE[key] = (st.st_mtime_ns, st.st_size, refs)
    return refs


def file_epoch(path: Path) -> int | None:
    """The `(version N)` stamp of a `.kicad_sch` / `.kicad_pcb` (the first 4 KB hold the header);
    None when the file is missing, unreadable or carries no stamp."""
    try:
        with Path(path).open("rb") as fh:
            m = _VERSION_RE.search(fh.read(_STAMP_HEAD_BYTES))
    except OSError:
        return None
    return int(m.group(1)) if m else None


def design_kind(path: Path) -> str | None:
    """'sch' / 'pcb' for a design file, else None."""
    return DESIGN_KINDS.get(Path(path).suffix.lower())


@dataclass(frozen=True)
class FileStamp:
    """One design file's format stamp: `path` relative to the project dir (posix), `kind`
    ('sch' | 'pcb'), `stamp` (None: the file has no `(version N)` header)."""

    path: str
    kind: str
    stamp: int | None

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "kind": self.kind, "stamp": self.stamp}


@dataclass
class ProjectState:
    """What kiwrap remembers about a project between tool calls.

    `hashes` maps a stage name to `{relative file path: sha256}` of the files that stage's result
    was computed from (§11: "content hashes stored with every stage result"). `target_kicad` is
    the full semver of the install the project is pinned to (None: not pinned - the install is
    chosen by the files' format stamps on every call, D21); `target_source` says how it was
    pinned (`TARGET_SOURCES`).
    """

    stage: str = "created"
    hashes: dict[str, dict[str, str]] = field(default_factory=dict)
    target_kicad: str | None = None
    history: list[dict[str, Any]] = field(default_factory=list)  # [{tool, stage, snapshot, at, force?}]
    format_version: int = STATE_FORMAT_VERSION
    target_source: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ProjectState:
        target = raw.get("target_kicad")
        # a state.json written before the field existed (or by the extension's retarget command)
        # has a target without a source: `effective_target` honours it as a pin
        source = raw.get("target_source")
        if source not in TARGET_SOURCES:
            source = None
        state = cls(
            stage=str(raw.get("stage", "created")),
            hashes={str(k): dict(v) for k, v in dict(raw.get("hashes", {})).items()},
            target_kicad=target,
            history=list(raw.get("history", [])),
            format_version=int(raw.get("format_version", STATE_FORMAT_VERSION)),
            target_source=source if target else None,
        )
        if state.stage not in STAGES:
            state.stage = "created"
        return state


@dataclass(frozen=True)
class Project:
    dir: Path
    name: str
    pro_path: Path

    @property
    def root_sch(self) -> Path:
        return self.dir / f"{self.name}.kicad_sch"

    @property
    def board(self) -> Path:
        return self.dir / f"{self.name}.kicad_pcb"

    @property
    def state_dir(self) -> Path:
        return self.dir / STATE_DIRNAME

    @property
    def state_path(self) -> Path:
        return self.state_dir / STATE_FILENAME

    @property
    def lock_path(self) -> Path:
        return self.state_dir / "lock"

    def schematic_files(self) -> list[Path]:
        """Every sheet of the project: the root first, then (sorted by relative path) every other
        top-level `.kicad_sch` and every existing sheet the hierarchy references from those files,
        followed recursively - `sch/Connectors.kicad_sch` included (module docstring). A reference
        outside the project directory, under `.kiwrap` / `.git` / `.history` / `*-backups`, or to a
        missing file is left out (nothing there can be stamped, snapshotted or upgraded)."""
        base = self.dir.resolve()
        found: dict[str, str] = {}  # normcased relpath -> relpath (posix)
        root_rel = self.relpath(self.root_sch)
        queue = ([self.root_sch] if self.root_sch.exists() else []) + sorted(
            p for p in self.dir.glob("*.kicad_sch") if p != self.root_sch and p.is_file()
        )
        while queue:
            sheet = queue.pop(0)
            rel = self.relpath(sheet)
            key = os.path.normcase(rel)
            if key in found:
                continue
            found[key] = rel
            for ref in sheet_references(sheet):
                target = Path(ref.replace("\\", "/"))
                child = target if target.is_absolute() else sheet.parent / target
                if child.suffix.lower() != ".kicad_sch":
                    continue
                try:
                    child_rel = child.resolve().relative_to(base)
                except (OSError, ValueError):
                    continue
                parts = child_rel.parts[:-1]
                if any(p in _SKIP_SHEET_DIRS or p.endswith("-backups") for p in parts):
                    continue
                if (base / child_rel).is_file() and os.path.normcase(child_rel.as_posix()) not in found:
                    queue.append(self.dir / child_rel)
        root_key = os.path.normcase(root_rel)
        others = sorted(
            (rel for key, rel in found.items() if key != root_key), key=lambda r: (r.count("/"), r)
        )
        return ([self.root_sch] if root_key in found else []) + [self.dir / rel for rel in others]

    def board_files(self) -> list[Path]:
        return [self.board] if self.board.exists() else []

    def design_files(self) -> list[Path]:
        return self.schematic_files() + self.board_files()

    def format_stamps(self) -> list[FileStamp]:
        """The `(version N)` stamp of every design file (root schematic first, sheets, board)."""
        out: list[FileStamp] = []
        for path in self.design_files():
            kind = design_kind(path)
            if kind is not None:
                out.append(FileStamp(self.relpath(path), kind, file_epoch(path)))
        return out

    def project_epochs(self) -> dict[str, int]:
        """`{kind: newest stamp}` over the design files that carry one (the newest file decides
        which KiCad can load the project)."""
        out: dict[str, int] = {}
        for fs in self.format_stamps():
            if fs.stamp is not None:
                out[fs.kind] = max(out.get(fs.kind, 0), fs.stamp)
        return out

    def relpath(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.dir.resolve()).as_posix()
        except ValueError:
            return path.as_posix()

    # ---- state -----------------------------------------------------------------------------

    def load_state(self) -> ProjectState:
        if not self.state_path.exists():
            return ProjectState()
        try:
            return ProjectState.from_dict(json.loads(self.state_path.read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            raise KiwrapError(
                "E_INTERNAL",
                f"cannot read {self.state_path}: {exc}",
                why="the kiwrap state file is unreadable or not valid JSON",
                fix="delete <project>/.kiwrap/state.json (the stage restarts at 'created') or restore a snapshot",
            ) from exc

    def save_state(self, state: ProjectState) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(self.state_path)

    # ---- editor locks --------------------------------------------------------------------------

    def editor_locks(self) -> list[Path]:
        """KiCad GUI lock files (`~<file>.lck`) present next to the design files (§11 editor-lock refusal)."""
        return sorted(p for p in self.dir.glob("~*.lck"))

    # ---- discovery -------------------------------------------------------------------------------

    @classmethod
    def from_path(cls, spec: str | Path) -> Project:
        """Resolve a project directory or a `.kicad_pro` path. Raises E_PROJECT_NOT_FOUND."""
        path = Path(spec).expanduser()
        if path.is_file() and path.suffix == ".kicad_pro":
            return cls(dir=path.parent.resolve(), name=path.stem, pro_path=path.resolve())
        if path.is_dir():
            pros = sorted(path.glob("*.kicad_pro"))
            if len(pros) == 1:
                return cls(dir=path.resolve(), name=pros[0].stem, pro_path=pros[0].resolve())
            if not pros:
                raise KiwrapError(
                    "E_PROJECT_NOT_FOUND",
                    f"no .kicad_pro file in {path}",
                    why="a kiwrap project is a directory holding exactly one .kicad_pro file",
                    fix="run kw_project(action='create', dir=..., name=...) first, or pass the .kicad_pro path",
                    example='kw_project(action="create", dir="C:/work/blinky", name="blinky")',
                )
            raise KiwrapError(
                "E_PROJECT_NOT_FOUND",
                f"{len(pros)} .kicad_pro files in {path}; pass the one you mean",
                why="the directory holds more than one project",
                fix="pass the full path of the .kicad_pro file as `project`",
                candidates=[p.as_posix() for p in pros],
            )
        raise KiwrapError(
            "E_PROJECT_NOT_FOUND",
            f"{path} does not exist",
            why="`project` must be an existing directory or .kicad_pro path",
            fix="check the path, or create the project with kw_project(action='create')",
        )
