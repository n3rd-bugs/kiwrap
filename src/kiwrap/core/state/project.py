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
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from kiwrap.core.state import STAGES, KiwrapError

STATE_DIRNAME = ".kiwrap"
STATE_FILENAME = "state.json"
STATE_FORMAT_VERSION = 1


@dataclass
class ProjectState:
    """What kiwrap remembers about a project between tool calls.

    `hashes` maps a stage name to `{relative file path: sha256}` of the files that stage's result
    was computed from (§11: "content hashes stored with every stage result"). `target_kicad` is
    the full semver of the install the project was created for / last validated with.
    """

    stage: str = "created"
    hashes: dict[str, dict[str, str]] = field(default_factory=dict)
    target_kicad: str | None = None
    history: list[dict[str, Any]] = field(default_factory=list)  # [{tool, stage, snapshot, at, force?}]
    format_version: int = STATE_FORMAT_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ProjectState:
        state = cls(
            stage=str(raw.get("stage", "created")),
            hashes={str(k): dict(v) for k, v in dict(raw.get("hashes", {})).items()},
            target_kicad=raw.get("target_kicad"),
            history=list(raw.get("history", [])),
            format_version=int(raw.get("format_version", STATE_FORMAT_VERSION)),
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
        """Every `.kicad_sch` in the project directory (root first, then sorted)."""
        others = sorted(p for p in self.dir.glob("*.kicad_sch") if p != self.root_sch)
        return ([self.root_sch] if self.root_sch.exists() else []) + others

    def board_files(self) -> list[Path]:
        return [self.board] if self.board.exists() else []

    def design_files(self) -> list[Path]:
        return self.schematic_files() + self.board_files()

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
