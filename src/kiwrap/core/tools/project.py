"""kw_project: create a minimal valid project, report its status, (Phase 1) upgrade it."""

from __future__ import annotations

import contextlib
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from kiwrap.core.state import KiwrapError, Receipt
from kiwrap.core.state.project import Project, ProjectState
from kiwrap.core.state.snapshots import head, is_repo_root
from kiwrap.core.tools import templates
from kiwrap.core.tools._base import MUTATING, ToolContext, ToolSpec, mutation, read_state, suggest_next

_VERSION_RE = re.compile(rb"\(version\s+(\d+)\s*\)")
_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$")


class ProjectArgs(BaseModel):
    action: Literal["create", "status", "upgrade"] = Field(description="create | status | upgrade")
    project: str | None = Field(
        default=None, description="status/upgrade: project directory or .kicad_pro path"
    )
    dir: str | None = Field(
        default=None, description="create: directory to create the project in (created if missing)"
    )
    name: str | None = Field(
        default=None, description="create: project name (stem of the .kicad_pro/.kicad_sch/.kicad_pcb)"
    )
    target_kicad: str | None = Field(
        default=None,
        description="create: KiCad to target - a major ('N'), major.minor ('N.M'), a full semver, or the "
        "root/kicad-cli path of an install kw_env lists; default newest installed",
    )
    board_size_mm: list[float] | None = Field(
        default=None,
        description="create: optional [width, height] in mm; writes a rectangular Edge.Cuts outline so DRC has a board",
    )
    to_kicad: str | None = Field(default=None, description="upgrade: target KiCad (not available in Phase 0)")
    dry_run: bool = Field(default=False, description="upgrade: report the semantic diff without writing")


def _refuse_unsafe_project_dir(directory: Path) -> None:
    """A project directory gets a snapshot store that walks it on every mutation: never the user's
    home directory, one of its parents, or a filesystem / drive root (the walk would take minutes
    and the store would index every KiCad file under it)."""
    home = Path.home().resolve()
    if directory == home or directory in home.parents or directory == Path(directory.anchor):
        raise KiwrapError(
            "E_BAD_ARGS",
            f"refusing to create a project directly in {directory.as_posix()}",
            why="it is your home directory, a parent of it or a drive root; kiwrap snapshots scan the project directory",
            fix="pass a dedicated directory for the project",
            example=f'kw_project(action="create", dir="{(home / "kicad" / "blinky").as_posix()}", name="blinky")',
        )


def file_epoch(path: Path) -> int | None:
    """The `(version N)` stamp of a `.kicad_sch` / `.kicad_pcb` (first 4 KB is enough)."""
    if not path.exists():
        return None
    with path.open("rb") as fh:
        m = _VERSION_RE.search(fh.read(4096))
    return int(m.group(1)) if m else None


def _create(ctx: ToolContext, args: ProjectArgs) -> Receipt:
    if not args.dir or not args.name:
        raise KiwrapError(
            "E_BAD_ARGS",
            "create needs `dir` and `name`",
            fix="pass dir (directory) and name (project stem)",
            example='kw_project(action="create", dir="C:/work/blinky", name="blinky", target_kicad="<major>")',
        )
    if not _NAME_RE.match(args.name):
        raise KiwrapError(
            "E_BAD_ARGS",
            f"invalid project name {args.name!r}",
            why="the name becomes a file stem; KiCad and git dislike spaces and path separators",
            fix="use letters, digits, '_', '-' or '.', up to 64 characters",
        )
    if args.board_size_mm is not None and (len(args.board_size_mm) != 2 or min(args.board_size_mm) <= 0):
        raise KiwrapError(
            "E_BAD_ARGS",
            "board_size_mm must be [width, height] with both > 0",
            example="board_size_mm=[50, 30]",
        )

    install = ctx.services.install(args.target_kicad)
    caps = ctx.services.caps(install)
    try:
        sch_epoch, pcb_epoch = int(caps["formats.sch"]), int(caps["formats.pcb"])
    except (KeyError, TypeError, ValueError) as exc:
        raise KiwrapError(
            "E_UNSUPPORTED",
            f"the capability record of KiCad {install.version} has no format epochs",
            why="kw_project needs formats.sch / formats.pcb to stamp the new files",
            fix="run kw_env(refresh=true); if the keys stay missing the version matrix needs an entry for this version",
        ) from exc

    directory = Path(args.dir).expanduser().resolve()
    _refuse_unsafe_project_dir(directory)
    directory.mkdir(parents=True, exist_ok=True)
    existing = sorted(directory.glob("*.kicad_pro"))
    if existing:
        raise KiwrapError(
            "E_BAD_ARGS",
            f"{directory} already holds a project ({existing[0].name})",
            why="kw_project never overwrites a project",
            fix="use kw_project(action='status', project=...) on it, or choose another dir",
            candidates=[p.name for p in existing],
        )

    project = Project(dir=directory, name=args.name, pro_path=directory / f"{args.name}.kicad_pro")
    template_dir = Path(install.share) / "template"

    outline = (float(args.board_size_mm[0]), float(args.board_size_mm[1])) if args.board_size_mm else None
    gv = templates.generator_version_for(caps, install)
    summary = f"create {args.name} (KiCad {install.version})"
    with mutation(ctx, project, "kw_project", gate=False) as m:
        m.summary = summary
        project.pro_path.write_text(templates.project_text(args.name, template_dir), encoding="utf-8")
        project.root_sch.write_text(
            templates.schematic_text(epoch=sch_epoch, generator_version=gv), encoding="utf-8"
        )
        project.board.write_text(
            templates.board_text(
                epoch=pcb_epoch,
                generator_version=gv,
                layer_ids_v2=templates.layer_ids_v2_for(caps),
                outline_mm=outline,
            ),
            encoding="utf-8",
        )
        m.state = ProjectState(stage="created", target_kicad=install.version)
        m.receipt = Receipt(
            changed={
                "added": [project.pro_path.name, project.root_sch.name, project.board.name],
                "updated": [],
                "removed": [],
            },
            provider_used="file",
            next_suggested="kw_sch_check",
            warnings=[]
            if outline
            else ["no board outline yet: kw_pcb_drc will report invalid_outline until one is drawn"],
            data={
                "ok": True,
                "project": project.pro_path.as_posix(),
                "dir": project.dir.as_posix(),
                "name": project.name,
                "target_kicad": install.version,
                "epochs": {"sch": sch_epoch, "pcb": pcb_epoch},
                "files": {
                    "pro": project.pro_path.name,
                    "sch": project.root_sch.name,
                    "pcb": project.board.name,
                },
                "board_size_mm": list(outline) if outline else None,
            },
        )
        return m.receipt


def status_data(ctx: ToolContext, project: Project) -> dict[str, Any]:
    state, external = read_state(project)
    installed = None
    with contextlib.suppress(KiwrapError):
        installed = [i.version for i in ctx.services.installs()]
    target_ok = state.target_kicad in installed if (installed and state.target_kicad) else None
    return {
        "ok": True,
        "project": project.pro_path.as_posix(),
        "dir": project.dir.as_posix(),
        "name": project.name,
        "stage": state.stage,
        "next_suggested": suggest_next(ctx, state),
        "target_kicad": state.target_kicad,
        "target_installed": target_ok,
        "hashes": state.hashes,
        "epochs": {"sch": file_epoch(project.root_sch), "pcb": file_epoch(project.board)},
        "files": {
            "sch": [project.relpath(p) for p in project.schematic_files()],
            "pcb": [project.relpath(p) for p in project.board_files()],
        },
        "editor_locks": [p.name for p in project.editor_locks()],
        "snapshot_head": head(project.dir) if is_repo_root(project.dir) else None,
        "external_edit": {
            "files": external.changed_files,
            "from": external.stage_before,
            "to": external.stage_after,
        }
        if external.external_edit
        else None,
        "history": state.history[-10:],
    }


def _status(ctx: ToolContext, args: ProjectArgs) -> Receipt:
    if not args.project:
        raise KiwrapError(
            "E_BAD_ARGS",
            "status needs `project`",
            example='kw_project(action="status", project="C:/work/blinky")',
        )
    project = Project.from_path(args.project)
    data = status_data(ctx, project)
    degraded = ["external_edit"] if data["external_edit"] else []
    return Receipt(
        provider_used="file",
        degraded=degraded,
        stage=data["stage"],
        next_suggested=data["next_suggested"],
        warnings=[f"external_edit: {', '.join(data['external_edit']['files'])}"] if degraded else [],
        data=data,
    )


def run(ctx: ToolContext, args: ProjectArgs) -> Receipt:
    if args.action == "create":
        return _create(ctx, args)
    if args.action == "status":
        return _status(ctx, args)
    raise KiwrapError(
        "E_UNSUPPORTED",
        "kw_project upgrade is not available in Phase 0",
        why="project upgrade (kicad-cli sch/pcb upgrade on a copy + semantic diff) lands in Phase 1",
        fix="create the project with the target_kicad you intend to ship with",
        data={"oldest_kicad_with_it": None, "fallback": "kw_project create with target_kicad"},
    )


SPEC = ToolSpec(
    name="kw_project",
    title="Project",
    description=(
        "create: write a minimal valid .kicad_pro + empty root .kicad_sch + .kicad_pcb in the target KiCad's format "
        "(optionally with a rectangular outline) and start the snapshot history. status: stage, hashes, epochs, "
        "external edits, next suggested tool. upgrade: Phase 1."
    ),
    input_model=ProjectArgs,
    handler=run,
    annotations=MUTATING,
)
