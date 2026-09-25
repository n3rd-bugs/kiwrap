"""kw_project (create, status, retarget, upgrade_preview) and kw_project_upgrade.

Actions (docs/DECISIONS.md D21 as amended by D24, requests/kiwrap-feature-requests.review.md item
1 / B1, SAFETY contract (c)):
- `create(dir, name, target_kicad?, board_size_mm?)`: the files are written in the target's
  format and the project is pinned to it (`target_source: created`). A directory that already
  holds a `.kicad_sch` / `.kicad_pcb` is refused before anything is written, and the three files
  are written with exclusive create (fixer finding 1).
- `status(project)`: stage, hashes, epochs, external edits, plus which KiCad the project runs on
  (`target_effective`, `target_source`, `epoch_match`, `warnings`: `_base.effective_target`) and,
  whenever a write would need a format upgrade, `upgrade_args` (the exact `kw_project_upgrade`
  arguments), `install_hint` and `retarget_candidates` (D24).
- `retarget(project, target_kicad)`: pin an install (or `'auto'`: unpin, follow the files'
  format) without touching any design file. Pinning a KiCad that writes a newer format than the
  files is allowed; every write then needs the upgrade first.
- `upgrade_preview(project)`: a READ (D24): converts a copy under `<storage>/tmp/upgrade`, runs
  ERC/DRC on it before and after and reports; no lock, no state, no snapshot, nothing written in
  the project (it also runs on a read-only server).

`kw_project_upgrade(project, from_kicad, to_kicad, files)` is the ONLY sanctioned up-conversion
(D24). It is published with `_meta["anthropic/requiresUserInteraction"]: true` and destructive
annotations, and has no `persist` / `confirm` / `dry_run`: Claude Code asks the user before every
call, in every permission mode, so the prompt is the consent (docs/DECISIONS.md D24: the gate order
read in claude.exe 2.1.280/2.1.281). The server rebuilds the arguments with `upgrade_args()` under
the project lock and refuses any difference (E_BAD_ARGS carrying the true ones), so the card the
user approved shows exactly what gets converted. `run_upgrade` is the one conversion core (the
tool, and the elicitation shortcut's accept): the listed files are upgraded on a copy with
`kicad-cli sch/pcb upgrade --force` per file (the root's upgrade leaves children alone,
`kiwrap.backends.cli.upgrade`), ERC/DRC run on the copy before and after, the pre-upgrade state is
a snapshot, then only the files that changed are replaced (all or nothing: a failed replacement puts
the replaced files back and removes its temp file, fixer finding 5) and the install is pinned.
Sub-sheets in subdirectories keep their relative paths (fixer finding 2). `.kicad_pro`,
`.kicad_prl`, `.kicad_dru`, the library tables and project libraries are never converted.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import shutil
import stat
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from kiwrap.core.state import STAGES, KiwrapError, Receipt, ValidatorDelta, readonly
from kiwrap.core.state.project import Project, ProjectState, file_epoch
from kiwrap.core.state.snapshots import create as snapshot_create
from kiwrap.core.state.snapshots import head, is_repo_root
from kiwrap.core.state.stages import downgrade_below, record_stage, stage_at_least
from kiwrap.core.tools import templates
from kiwrap.core.tools._base import (
    DESIGN_KINDS,
    DESTRUCTIVE,
    MUTATING,
    REQUIRES_USER_INTERACTION,
    SOURCE_CREATED,
    SOURCE_PINNED,
    Mutation,
    TargetChoice,
    ToolContext,
    ToolSpec,
    _conversion_text,
    _install_epochs,
    bound_request,
    consent_source,
    format_newer_error,
    mutation,
    read_state,
    refuse_unsupported_client,
    resolve_target,
    suggest_next,
    to_kiwrap_error,
)

_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$")
#: `retarget` values that remove the pin (the project then follows its files' format)
UNPIN = ("", "auto", "none", "file", "files")
#: the tool that converts design files to a newer KiCad format (D24)
UPGRADE_TOOL = "kw_project_upgrade"
#: the `upgrade_args` keys that are `kw_project_upgrade` arguments (`to_format` is informational)
UPGRADE_CALL_KEYS = ("project", "from_kicad", "to_kicad", "files")

__all__ = [
    "SPEC",
    "UPGRADE_SPEC",
    "ProjectArgs",
    "UpgradeArgs",
    "file_epoch",
    "perform_upgrade",
    "run",
    "run_upgrade",
    "status_data",
    "upgrade_args",
]


class ProjectArgs(BaseModel):
    action: Literal["create", "status", "retarget", "upgrade_preview"] = Field(
        description="create | status | retarget | upgrade_preview (converting the files is kw_project_upgrade)"
    )
    project: str | None = Field(
        default=None, description="status/retarget/upgrade_preview: project directory or .kicad_pro path"
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
        "root/kicad-cli path of an install kw_env lists; default newest installed. retarget: the install to pin "
        "(same forms), or 'auto' to unpin and follow the files' format",
    )
    board_size_mm: list[float] | None = Field(
        default=None,
        description="create: optional [width, height] in mm; writes a rectangular Edge.Cuts outline so DRC has a board",
    )


class UpgradeArgs(BaseModel):
    """`kw_project_upgrade`'s arguments (D24): all required, passed verbatim from `upgrade_args`."""

    project: str = Field(
        description="data.upgrade_args.project: the project's .kicad_pro path, exactly as given"
    )
    from_kicad: str = Field(
        description="data.upgrade_args.from_kicad: the KiCad line the files are in ('N.M'), or "
        "'format sch=<stamp>,pcb=<stamp>' when no single KiCad line wrote them"
    )
    to_kicad: str = Field(
        description="data.upgrade_args.to_kicad: the installed KiCad whose format the files move to (a version or "
        "an install root, as kw_env lists it)"
    )
    files: list[str] = Field(
        description="data.upgrade_args.files: the design files to convert, project-relative posix paths, exactly "
        "as given"
    )


# ---- the single source of the upgrade arguments (D24) --------------------------------------------------


def _install_spec(version: str, root: str, formats: dict[str, int], candidates: list[dict[str, Any]]) -> str:
    """How an install is named in `to_kicad` and in the pin: its version, or its root when another
    install sharing that version writes different formats (nightly and bench builds all report the
    plain semver, installation.py `version`; D24)."""
    same = [c for c in candidates if str(c.get("version") or "") == version]
    if root and any(dict(c.get("formats") or {}) != dict(formats) for c in same):
        return root
    return version


def _from_kicad(choice: TargetChoice) -> str:
    """The project's KiCad line, else `format sch=N,pcb=M` (its newest stamp per kind)."""
    if choice.project_kicad:
        return choice.project_kicad
    parts = [f"{k}={choice.project_epoch[k]}" for k in DESIGN_KINDS if k in choice.project_epoch]
    return "format " + ",".join(parts)


def upgrade_args(choice: TargetChoice, project: Project) -> dict[str, Any]:
    """The `kw_project_upgrade` arguments for `project` as `choice` resolves it (D24) - the single
    source of the E_FORMAT_UPGRADE_REQUIRED refusal (`data.upgrade_args`), `kw_project status`,
    `upgrade_preview` and the tool's own recompute under the project lock. Independent of which
    kinds a refused write touched, so a board-only refusal on a mixed-generation project carries
    arguments the tool accepts (D24: the kinds-dependent `from_kicad` loop).

    `{project, from_kicad, to_kicad, to_format {sch, pcb}, files}`: `from_kicad` = the files' KiCad
    line, else `format sch=N,pcb=M`; `to_kicad` = the install spec the target pins (version, or
    root when installs sharing that version write different formats); `to_format` = that
    install's formats; `files` = the sorted project-relative posix paths `choice.upgrade_files()`
    lists (empty: no write needs an upgrade)."""
    root = str(getattr(choice.install, "root", "") or "")
    return {
        "project": project.pro_path.as_posix(),
        "from_kicad": _from_kicad(choice),
        "to_kicad": _install_spec(choice.version, root, dict(choice.install_epoch), choice.candidates),
        "to_format": {k: choice.install_epoch.get(k) for k in DESIGN_KINDS},
        "files": sorted(f.path for f in choice.upgrade_files()),
    }


def _needs_upgrade(choice: TargetChoice) -> bool:
    return bool(choice.readable and not choice.writable and choice.upgrade_files())


def _call_args(args: dict[str, Any]) -> dict[str, Any]:
    return {k: args[k] for k in UPGRADE_CALL_KEYS}


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


def _need_project(args: ProjectArgs) -> Project:
    if not args.project:
        raise KiwrapError(
            "E_BAD_ARGS",
            f"{args.action} needs `project`",
            example=f'kw_project(action="{args.action}", project="C:/work/blinky")',
        )
    return Project.from_path(args.project)


def _refuse_existing_design(directory: Path, project: Project) -> None:
    """`kw_project create` never writes over a design (fixer finding 1: a directory holding a
    10.0 pic_programmer without its `.kicad_pro` had its 243 KB schematic and 665 KB board replaced
    by the 221-byte / 645-byte templates, and the receipt listed them as `added`). Any
    `.kicad_sch` / `.kicad_pcb` in `dir` - the new project's own names or any other - is refused
    before anything is written; `status` / `retarget` adopt an existing design once it has its
    `.kicad_pro`."""
    found = sorted(
        {p.name for p in (project.root_sch, project.board) if p.exists()}
        | {p.name for pattern in ("*.kicad_sch", "*.kicad_pcb") for p in directory.glob(pattern)}
    )
    if not found:
        return
    raise KiwrapError(
        "E_BAD_ARGS",
        f"{directory.as_posix()} already holds KiCad design files ({', '.join(found[:4])}"
        + (", ..." if len(found) > 4 else "")
        + "); kw_project create never overwrites them",
        why="create writes a new empty schematic and board; the files in this directory are someone's design",
        fix=(
            "to work on this design, give it its .kicad_pro (open it once in KiCad, or copy the project file that "
            "belongs to it) and use kw_project(action='status', project=<dir>) - retarget pins the KiCad it runs on; "
            "to start a new project, choose an empty directory"
        ),
        candidates=found,
        example=f'kw_project(action="status", project="{directory.as_posix()}")',
    )


def _write_new_files(project: Project, files: list[tuple[Path, str]]) -> None:
    """Write each file with exclusive create (`open(..., 'x')`), so a file that appeared after the
    existence check (a race with KiCad, the user or another agent) is never clobbered; on any
    failure the files this call created are removed again (fixer findings 1 and 5)."""
    created: list[Path] = []
    try:
        for path, text in files:
            with path.open("x", encoding="utf-8") as fh:
                created.append(path)
                fh.write(text)
    except BaseException as exc:
        for path in created:
            with contextlib.suppress(OSError):
                path.unlink()
        if isinstance(exc, FileExistsError):
            raise KiwrapError(
                "E_BAD_ARGS",
                f"{Path(exc.filename or '').name or 'a project file'} appeared in {project.dir.as_posix()} while the "
                "project was being created; nothing was overwritten",
                why="kw_project create writes its files with exclusive create and never replaces an existing file",
                fix="check what wrote the file; choose an empty directory for a new project",
                candidates=[Path(exc.filename).name] if exc.filename else [],
            ) from exc
        raise


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
    _refuse_existing_design(directory, project)
    template_dir = Path(install.share) / "template"

    outline = (float(args.board_size_mm[0]), float(args.board_size_mm[1])) if args.board_size_mm else None
    gv = templates.generator_version_for(caps, install)
    summary = f"create {args.name} (KiCad {install.version})"
    with mutation(ctx, project, "kw_project", gate=False) as m:
        m.summary = summary
        _write_new_files(
            project,
            [
                (project.pro_path, templates.project_text(args.name, template_dir)),
                (project.root_sch, templates.schematic_text(epoch=sch_epoch, generator_version=gv)),
                (
                    project.board,
                    templates.board_text(
                        epoch=pcb_epoch,
                        generator_version=gv,
                        layer_ids_v2=templates.layer_ids_v2_for(caps),
                        outline_mm=outline,
                    ),
                ),
            ],
        )
        m.state = ProjectState(stage="created", target_kicad=install.version, target_source=SOURCE_CREATED)
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
                "target_source": SOURCE_CREATED,
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


# ---- status ------------------------------------------------------------------------------------------


def _retarget_candidates(choice: TargetChoice) -> list[str]:
    """The installed KiCads that write the files' own format (kw_project retarget keeps it)."""
    return [str(c["version"]) for c in choice.candidates if c.get("matches")]


def target_status(ctx: ToolContext, project: Project, state: ProjectState) -> dict[str, Any]:
    """The D21 fields of `kw_project status`: which KiCad this project runs on and whether that
    KiCad writes the files' own format (`epoch_match`); when a write would need an upgrade, the
    D24 `upgrade_args` plus the alternatives (`install_hint`, `retarget_candidates`)."""
    try:
        choice = resolve_target(ctx, project, state)
    except KiwrapError as err:
        return {
            "target_effective": None,
            "target_kicad_effective": None,
            "target_source": None,
            "epoch_match": None,
            "target": None,
            "upgrade_args": None,
            "target_warnings": [f"{err.code}: {err.message}"],
        }
    warnings = list(choice.notes)
    needs = _needs_upgrade(choice)
    if state.target_kicad and not choice.writable and choice.readable:
        matching = _retarget_candidates(choice)
        if matching:
            warnings.append(
                f"the project is pinned to KiCad {choice.version}, which writes a newer format than its files; "
                f"KiCad {matching[0]} writes theirs: kw_project(action='retarget', target_kicad='{matching[0]}') "
                "keeps the format; kw_project_upgrade with data.upgrade_args converts the files (the user is asked)"
            )
    return {
        "target_effective": choice.version,
        "target_kicad_effective": choice.version,
        "target_source": choice.source,
        "epoch_match": choice.writable,
        "epoch_match_kinds": {k: k in choice.writable_kinds for k in sorted(choice.project_epoch)},
        "project_kicad": choice.project_kicad,
        "install_epochs": dict(choice.install_epoch),
        "file_epochs": [f.to_dict() for f in choice.files],
        "install_hint": None if choice.writable else choice.install_hint,
        "candidates": choice.candidates,
        "target": choice.to_dict(),
        "upgrade_args": upgrade_args(choice, project) if needs else None,
        "retarget_candidates": _retarget_candidates(choice) if needs else [],
        "target_warnings": warnings,
        "_choice": choice,
    }


def _target_installed(ctx: ToolContext, spec: str | None) -> bool | None:
    """The pinned install (a version or a root) is still installed; None without a pin."""
    if not spec:
        return None
    try:
        ctx.services.install(spec)
    except KiwrapError:
        return False
    return True


def status_data(ctx: ToolContext, project: Project) -> dict[str, Any]:
    state, external = read_state(project)
    target_ok = _target_installed(ctx, state.target_kicad)
    target = target_status(ctx, project, state)
    choice = target.pop("_choice", None)
    warnings = target.pop("target_warnings")
    data = {
        "ok": True,
        "project": project.pro_path.as_posix(),
        "dir": project.dir.as_posix(),
        "name": project.name,
        "stage": state.stage,
        "next_suggested": suggest_next(ctx, state, target=choice),
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
        **target,
        "warnings": warnings,
    }
    return data


def _status(ctx: ToolContext, args: ProjectArgs) -> Receipt:
    project = _need_project(args)
    data = status_data(ctx, project)
    degraded = ["external_edit"] if data["external_edit"] else []
    warnings = [f"external_edit: {', '.join(data['external_edit']['files'])}"] if degraded else []
    warnings.extend(data.get("warnings") or [])
    if data.get("upgrade_args"):
        warnings.append(
            "a write needs a file-format upgrade first: tell the user, then kw_project_upgrade with "
            "data.upgrade_args (the client asks them); kw_project(action='upgrade_preview') previews it"
        )
    return Receipt(
        provider_used="file",
        degraded=degraded,
        stage=data["stage"],
        next_suggested=data["next_suggested"],
        warnings=warnings,
        data=data,
    )


# ---- retarget ----------------------------------------------------------------------------------------


def _retarget(ctx: ToolContext, args: ProjectArgs) -> Receipt:
    project = _need_project(args)
    spec = (args.target_kicad or "").strip()
    with mutation(ctx, project, "kw_project", gate=False) as m:
        before = m.state.target_kicad
        if spec.lower() in UNPIN:
            m.state.target_kicad, m.state.target_source = None, None
            choice = resolve_target(ctx, project, m.state)
            m.summary = "retarget auto (follow the files' format)"
        else:
            choice = resolve_target(ctx, project, m.state, prefer=spec)
            if not choice.readable:
                raise format_newer_error(choice, project)
            root = str(getattr(choice.install, "root", "") or "")
            m.state.target_kicad = _install_spec(
                choice.version, root, dict(choice.install_epoch), choice.candidates
            )
            m.state.target_source = SOURCE_PINNED
            choice.source = SOURCE_PINNED
            m.summary = f"retarget {choice.version}"
        m.target = choice
        warnings = list(choice.notes)
        needs = _needs_upgrade(choice)
        if needs:
            warnings.append(
                f"KiCad {choice.version} writes a newer format than the files: every write needs the upgrade first "
                "(tell the user; kw_project_upgrade with data.upgrade_args converts them once the user approves)"
            )
        m.receipt = Receipt(
            provider_used="file",
            warnings=warnings,
            data={
                "ok": True,
                "project": project.pro_path.as_posix(),
                "target_kicad": m.state.target_kicad,
                "target_kicad_before": before,
                "target_source": m.state.target_source,
                "target_effective": choice.version,
                "epoch_match": choice.writable,
                "project_epoch": dict(choice.project_epoch),
                "install_epoch": dict(choice.install_epoch),
                "upgrade_args": upgrade_args(choice, project) if needs else None,
            },
        )
        return m.receipt


# ---- the conversion (shared by upgrade_preview and kw_project_upgrade) ------------------------------


def _work_dir(ctx: ToolContext, project: Project) -> Path:
    digest = hashlib.sha1(str(project.dir.resolve()).lower().encode("utf-8")).hexdigest()[:8]  # noqa: S324 - a name
    return Path(ctx.services.storage_home) / "tmp" / "upgrade" / f"{project.name}-{digest}"


def _checks(ctx: ToolContext, install: Any, caps: Any, config: Any, sch: Path, board: Path) -> dict[str, Any]:
    """ERC / DRC counts of one copy (no refill, no save): the before/after numbers of an upgrade."""
    out: dict[str, Any] = {}
    if sch.exists() and caps.has("cli.has_erc"):
        try:
            rep = ctx.services.run_erc(install, config, sch, severity="all", units="mm")
            out["erc"] = {"errors": int(rep.errors), "warnings": int(rep.warnings)}
        except Exception as exc:  # noqa: BLE001 - a check that cannot run is reported, not fatal
            err = to_kiwrap_error(exc)
            out["erc"] = {"error": f"{err.code}: {err.message}"}
    if board.exists() and caps.has("cli.has_drc"):
        try:
            rep = ctx.services.run_drc(
                install,
                config,
                board,
                schematic_parity=False,
                refill_zones=False,
                save_board=False,
                severity="all",
            )
            out["drc"] = {
                "errors": int(rep.errors),
                "warnings": int(rep.warnings),
                "unconnected": len([v for v in rep.unconnected if not getattr(v, "excluded", False)]),
            }
        except Exception as exc:  # noqa: BLE001
            err = to_kiwrap_error(exc)
            out["drc"] = {"error": f"{err.code}: {err.message}"}
    return out


#: the suffix of the temp file `_replace` renames over a project file
REPLACE_TMP_SUFFIX = ".kiwrap-upgrade.tmp"


def _replace(source: Path, upgraded: Path) -> None:
    """Put an upgraded copy in place of a project file (temp file + atomic rename). The temp file
    never outlives the call: a rename that fails (a read-only or locked project file) removes it
    (fixer finding 5: a 646 KB `<board>.kiwrap-upgrade.tmp` in 10.99 format was left in the
    project and committed into the FAILED snapshot)."""
    tmp = source.with_name(source.name + REPLACE_TMP_SUFFIX)
    try:
        shutil.copyfile(upgraded, tmp)
        tmp.replace(source)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)


def _clear_dir(path: Path) -> None:
    """Remove a work directory, read-only files included (Python 3.11 has no `rmtree(onexc=)`)."""
    if not path.exists():
        return
    for p in path.rglob("*"):
        if p.is_file():
            with contextlib.suppress(OSError):
                p.chmod(stat.S_IREAD | stat.S_IWRITE)
    shutil.rmtree(path, ignore_errors=True)


def _replace_all(
    project: Project, files: list[Any], originals: Path, snapshot_before: str | None
) -> list[str]:
    """Replace every changed project file with its upgraded copy, all or nothing: each original is
    kept under `originals` first, and when one replacement fails the files already replaced are put
    back before the error is re-raised, as a KiwrapError whose `data.upgrade_rollback` says what
    was restored (fixer finding 5). Returns the replaced relative paths."""
    done: list[tuple[Path, Path, str]] = []
    # a previous run's originals may be read-only copies of a read-only file: start empty, and copy
    # the bytes only (never the mode bits), so a retry after the user fixed the file works
    _clear_dir(originals)
    try:
        for f in files:
            if not f.changed:
                continue
            source, rel = Path(f.source), _rel_of(project, f)
            keep = originals / rel
            keep.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, keep)
            _replace(source, Path(f.copy))
            done.append((source, keep, rel))
    except BaseException as exc:
        restored: list[str] = []
        not_restored: list[dict[str, str]] = []
        for source, keep, rel in reversed(done):
            try:
                _replace(source, keep)
                restored.append(rel)
            except OSError as undo_exc:
                not_restored.append({"file": rel, "error": str(undo_exc)})
        leftovers = []
        for f in files:
            tmp = Path(f.source).with_name(Path(f.source).name + REPLACE_TMP_SUFFIX)
            if tmp.exists():
                with contextlib.suppress(OSError):
                    tmp.unlink()
                leftovers.append(project.relpath(tmp))
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        err = to_kiwrap_error(exc)
        err.data.setdefault(
            "upgrade_rollback",
            {
                "restored": sorted(restored),
                "not_restored": not_restored,
                "removed_temp_files": leftovers,
                "originals": originals.as_posix(),
                "snapshot_before": snapshot_before,
            },
        )
        note = (
            "the upgrade was undone: the files replaced before the failure were put back"
            if not not_restored
            else "the upgrade could NOT be fully undone: restore the snapshot named in data.upgrade_rollback"
        )
        err.fix = f"{err.fix}; {note}" if err.fix else note
        if err is exc:
            raise
        raise err from exc
    return [rel for _s, _k, rel in done]


def _require_upgrade_capability(ctx: ToolContext, install: Any, caps: Any) -> None:
    """E_UNSUPPORTED (with the installs that have it as candidates) when `install` has no
    `kicad-cli sch/pcb upgrade` (`cli.has_sch_pcb_upgrade`)."""
    if caps.has("cli.has_sch_pcb_upgrade"):
        return
    able: list[str] = []
    with contextlib.suppress(KiwrapError):
        able = [
            i.version for i in ctx.services.installs() if ctx.services.caps(i).has("cli.has_sch_pcb_upgrade")
        ]
    raise KiwrapError(
        "E_UNSUPPORTED",
        f"KiCad {install.version} cannot upgrade schematics and boards (cli.has_sch_pcb_upgrade)",
        why="kw_project_upgrade converts the files with kicad-cli sch/pcb upgrade",
        fix=(
            "pin an install that has it (kw_project(action='retarget', target_kicad=...)), then use the "
            "upgrade_args kw_project(action='status') returns"
            if able
            else "install a KiCad with sch/pcb upgrade"
        ),
        candidates=able,
        data={"capability": "cli.has_sch_pcb_upgrade", "kicad": install.version},
    )


def _convert_copy(
    ctx: ToolContext,
    project: Project,
    install: Any,
    caps: Any,
    convert: list[str],
    work: Path,
    *,
    checks: bool,
) -> tuple[Any, dict[str, Any] | None, dict[str, Any] | None]:
    """Copy the whole project (every design file, keeping relative paths) and upgrade exactly
    `convert` (project-relative posix paths) in the copy; ERC/DRC on a plain copy before and on the
    upgraded copy after when `checks`. Returns (the backend's `ProjectCopy`, before, after)."""
    config = ctx.services.config_root(install)
    files = project.design_files()
    # the project's own order (root sheet, sub-sheets, board last): the replacement follows it
    order = {os.path.normcase(project.relpath(p)): n for n, p in enumerate(files)}
    convert = sorted(convert, key=lambda rel: (order.get(os.path.normcase(rel), len(order)), rel))
    root_rel, board_rel = project.relpath(project.root_sch), project.relpath(project.board)
    before_counts: dict[str, Any] | None = None
    if checks:
        before_dir = Path(ctx.services.copy_project(project.dir, files, work / "before"))
        before_counts = _checks(ctx, install, caps, config, before_dir / root_rel, before_dir / board_rel)
    result = ctx.services.upgrade_project(
        install,
        config,
        project.dir,
        files,
        work / "after",
        caps=caps,
        convert=[project.dir / rel for rel in convert],
    )
    after_dir = Path(result.work_dir)
    after_counts = (
        _checks(ctx, install, caps, config, after_dir / root_rel, after_dir / board_rel) if checks else None
    )
    return result, before_counts, after_counts


def _file_entry(project: Project, f: Any) -> dict[str, Any]:
    return {
        "path": _rel_of(project, f),
        "kind": f.kind,
        "stamp_before": f.stamp_before,
        "stamp_after": f.stamp_after,
        "changed": bool(f.changed),
    }


def perform_upgrade(
    ctx: ToolContext,
    m: Mutation,
    install: Any,
    *,
    convert: Iterable[str],
    checks: bool = True,
    reason: str = "",
) -> dict[str, Any]:
    """Convert exactly `convert` (project-relative posix paths of design files) of `m.project` to
    `install`'s format (the caller holds the project lock inside `mutation()` and has the user's
    consent: `run_upgrade`, D24). Converted on a copy under the storage home; ERC/DRC counts before
    and after when `checks`; the pre-upgrade state is a snapshot; then the files that changed are
    replaced, the install is pinned and the stages re-hashed (a check that got worse drops the
    stage it backed). Returns the report."""
    project = m.project
    caps = ctx.services.caps(install)
    _require_upgrade_capability(ctx, install, caps)
    rels = sorted(set(convert))
    if not rels:
        raise KiwrapError(
            "E_BAD_ARGS",
            f"nothing to convert in {project.name}",
            fix="kw_project(action='status') says whether a write needs an upgrade (upgrade_args)",
        )
    # the undo point comes first: nothing is converted without one (D24 "snapshots the project first";
    # review finding 4 - a failed snapshot used to be swallowed and the files replaced anyway)
    snapshot_before = m.baseline or _snapshot_before_upgrade(project, install)
    work = _work_dir(ctx, project)
    result, before_counts, after_counts = _convert_copy(
        ctx, project, install, caps, rels, work, checks=checks
    )
    report: dict[str, Any] = {
        "kicad": install.version,
        "reason": reason,
        "files": [_file_entry(project, f) for f in result.files],
        "checks": {"before": before_counts, "after": after_counts} if checks else None,
        "work_dir": Path(result.work_dir).as_posix(),
    }
    report["snapshot_before"] = snapshot_before
    replaced = _replace_all(project, list(result.files), work / "originals", snapshot_before)
    report["replaced"] = replaced
    root = str(getattr(install, "root", "") or "")
    candidates = [{"version": i.version, "formats": _install_epochs(ctx, i)} for i in ctx.services.installs()]
    m.state.target_kicad = _install_spec(install.version, root, _install_epochs(ctx, install), candidates)
    m.state.target_source = SOURCE_PINNED
    for s in STAGES:
        if s in m.state.hashes:
            record_stage(project, m.state, s, advance=False)
    stage_dropped: list[str] = []
    if checks and before_counts and after_counts:
        for kind, gate_stage in (("erc", "erc_clean"), ("drc", "drc_clean")):
            b, a = before_counts.get(kind) or {}, after_counts.get(kind) or {}
            if (
                "errors" in a
                and int(a["errors"]) > int(b.get("errors", 0))
                and stage_at_least(m.state.stage, gate_stage)
            ):
                downgrade_below(m.state, gate_stage)
                stage_dropped.append(gate_stage)
    report["stage_dropped"] = stage_dropped
    # the stamp guard compares against the upgraded stamps from now on (it stays armed for the
    # rest of the call: only this sanctioned conversion may raise them)
    report["epochs"] = {"before": dict(m.stamps_before), "after": _stamps(project)}
    m.stamps_before = {**m.stamps_before, **_stamps(project)}
    for name in replaced:
        if name not in m.receipt.changed["updated"]:
            m.receipt.changed["updated"].append(name)
    return report


def _snapshot_before_upgrade(project: Project, install: Any) -> str:
    """The pre-upgrade snapshot when the call's prologue could not take one, else E_INTERNAL before
    any file is replaced: an irreversible conversion never runs without an undo point (review
    finding 4 - a git failure such as `Filename too long` was swallowed, and the receipt said
    'kw_snapshot restore  undoes it' with an empty ref)."""
    try:
        sha = snapshot_create(project.dir, f"[kiwrap] pre-upgrade to KiCad {install.version}")
    except KiwrapError as exc:
        raise _no_undo_point(project, exc) from exc
    if not sha:
        raise _no_undo_point(project, None)
    return sha


def _no_undo_point(project: Project, cause: KiwrapError | None) -> KiwrapError:
    detail = cause.message if cause is not None else "the snapshot store returned no commit"
    fix = (
        "fix the snapshot store first - the git error above says what failed (git missing from PATH: install "
        "git; a disk or permission problem: free space / make the project writable); kw_snapshot(action='create', "
        f"project='{project.pro_path.as_posix()}') shows when it works again, then call kw_project_upgrade again"
    )
    if "too long" in detail.lower():
        fix = (
            "the project path is too long for git on this machine: move the project to a shorter path, or enable "
            "long paths (git config --global core.longpaths true, and the Windows LongPathsEnabled policy); then "
            "call kw_project_upgrade again"
        )
    return KiwrapError(
        "E_INTERNAL",
        f"no undo point for {project.name}: the pre-upgrade snapshot failed ({detail}); nothing was converted",
        why="kw_project_upgrade replaces the design files with a format the older KiCad cannot open; it runs only "
        "when kw_snapshot restore can undo it (D24)",
        fix=fix,
        candidates=["kw_snapshot(action='create')"],
        data={"snapshot_error": detail, **({"git": cause.data} if cause is not None and cause.data else {})},
    )


def _stamps(project: Project) -> dict[str, int | None]:
    return {f.path: f.stamp for f in project.format_stamps()}


def _rel_of(project: Project, f: Any) -> str:
    """An upgraded file's path relative to the project (`sch/x.kicad_sch` for a sub-sheet)."""
    rel = str(getattr(f, "rel", "") or "")
    return rel or project.relpath(Path(f.source))


#: never converted by kw_project_upgrade (D24): project settings, per-user state, rules, library
#: tables and project libraries
_NEVER_CONVERTED_SUFFIXES = (".kicad_pro", ".kicad_prl", ".kicad_dru", ".kicad_sym", ".pretty")
_LIB_TABLES = ("sym-lib-table", "fp-lib-table", "design-block-lib-table")
_SKIP_DIRS = frozenset({".kiwrap", ".git", ".history", "node_modules", "__pycache__"})


def _not_converted(project: Project, converted: Iterable[str]) -> list[str]:
    """What a conversion leaves alone, as project-relative posix paths: the design files not in
    `converted`, plus the project file, its per-user state, the rules, the library tables and the
    project libraries (`.kicad_sym` files, `.pretty` folders) - D24."""
    done = {os.path.normcase(c) for c in converted}
    out = [
        rel
        for rel in (project.relpath(p) for p in project.design_files())
        if os.path.normcase(rel) not in done
    ]
    for dirpath, dirnames, filenames in os.walk(project.dir):
        base = Path(dirpath)
        dirnames[:] = sorted(
            d
            for d in dirnames
            if d not in _SKIP_DIRS and not d.endswith("-backups") and not d.startswith(".")
        )
        names = list(filenames) + [d for d in dirnames if d.lower().endswith(".pretty")]
        for name in names:
            if name.lower().endswith(_NEVER_CONVERTED_SUFFIXES) or name in _LIB_TABLES:
                out.append((base / name).relative_to(project.dir).as_posix())
        dirnames[:] = [d for d in dirnames if not d.lower().endswith(".pretty")]
        if len(out) > 500:
            break
    return sorted(set(out))


def _check_changes(checks: dict[str, Any] | None) -> list[str]:
    """Warnings for ERC/DRC counts that changed with the conversion (counted on copies)."""
    out: list[str] = []
    for kind in ("erc", "drc"):
        b = ((checks or {}).get("before") or {}).get(kind) or {}
        a = ((checks or {}).get("after") or {}).get(kind) or {}
        if "errors" in a and "errors" in b and (a["errors"], a["warnings"]) != (b["errors"], b["warnings"]):
            out.append(
                f"{kind.upper()} changed with the conversion: errors {b['errors']} -> {a['errors']}, "
                f"warnings {b['warnings']} -> {a['warnings']} (counted on copies)"
            )
    return out


def _check_validators(checks: dict[str, Any] | None) -> list[ValidatorDelta]:
    """The receipt's validators `erc` and `drc`: error counts on the copy before and after (D24)."""
    out: list[ValidatorDelta] = []
    for kind in ("erc", "drc"):
        b = ((checks or {}).get("before") or {}).get(kind)
        a = ((checks or {}).get("after") or {}).get(kind)
        if b is None and a is None:
            continue
        b, a = b or {}, a or {}
        detail: dict[str, Any] = {"counted_on": "copies", "before": b, "after": a}
        out.append(
            ValidatorDelta(
                name=kind,
                before=int(b["errors"]) if "errors" in b else None,
                after=int(a["errors"]) if "errors" in a else None,
                detail=detail,
            )
        )
    return out


def _files_text(files: list[str], limit: int = 6) -> str:
    shown = files[:limit] + ([f"{len(files) - limit} more"] if len(files) > limit else [])
    return ", ".join(shown)


# ---- upgrade_preview (a read, D24) ---------------------------------------------------------------------


def _alternatives(choice: TargetChoice, project: Project) -> list[str]:
    """What the user can do instead of converting (the preview's and the refusal's alternatives)."""
    pro = project.pro_path.as_posix()
    out = [
        f"kw_project(action='retarget', project='{pro}', target_kicad='{v}') - KiCad {v} writes the files' own "
        "format"
        for v in _retarget_candidates(choice)
    ]
    line, hint = choice.release_for()
    if not out and line:
        out.append(
            f"install KiCad {line}" + (f" ({hint})" if hint else "") + " - it writes the files' own format"
        )
    if choice.mixed:
        out.append("kw_snapshot(action='restore', ref=...) a snapshot from before the formats were mixed")
    return out


def _upgrade_preview(ctx: ToolContext, args: ProjectArgs) -> Receipt:
    """`kw_project(action='upgrade_preview')` (D24): the conversion `kw_project_upgrade` would do,
    run on a copy under `<storage>/tmp/upgrade` - outside `mutation()`: no lock, no state written,
    no snapshot, nothing in the project changes (also on a read-only server, D23)."""
    project = _need_project(args)
    state = readonly.load_state(project)
    choice = resolve_target(ctx, project, state)
    if not choice.readable:
        raise format_newer_error(choice, project)
    base: dict[str, Any] = {
        "ok": True,
        "project": project.pro_path.as_posix(),
        "target_effective": choice.version,
        "target_source": choice.source,
        "epoch_match": choice.writable,
        "project_kicad": choice.project_kicad,
    }
    if not _needs_upgrade(choice):
        return Receipt(
            provider_used="file",
            warnings=[
                f"nothing to convert: KiCad {choice.version} (the KiCad {project.name} runs on) writes its files' "
                "own format; to move the project to a newer KiCad, pin it first (kw_project(action='retarget'))"
            ],
            data={**base, "upgrade_args": None, "files": [], "checks": None, "alternatives": []},
        )
    ua = upgrade_args(choice, project)
    install = choice.install
    caps = ctx.services.caps(install)
    _require_upgrade_capability(ctx, install, caps)
    work = _work_dir(ctx, project).with_name(f"{_work_dir(ctx, project).name}-preview-{uuid.uuid4().hex[:8]}")
    try:
        result, before, after = _convert_copy(
            ctx, project, install, caps, list(ua["files"]), work, checks=True
        )
        files = [_file_entry(project, f) for f in result.files]
    finally:
        _clear_dir(work)
    checks = {"before": before, "after": after}
    warnings = [
        "preview: nothing in the project was changed; converting needs the user's approval - tell the user "
        f"(from {ua['from_kicad']} to KiCad {choice.version}: {_files_text(ua['files'])}), then kw_project_upgrade "
        "with data.upgrade_args",
        *_check_changes(checks),
    ]
    return Receipt(
        provider_used=ctx.services.provider(install),
        next_suggested=UPGRADE_TOOL,
        warnings=warnings,
        data={
            **base,
            "upgrade_args": ua,
            "files": files,
            "checks": checks,
            "not_converted": _not_converted(project, ua["files"]),
            "alternatives": _alternatives(choice, project),
            "install_hint": choice.install_hint,
            "retarget_candidates": _retarget_candidates(choice),
        },
    )


# ---- kw_project_upgrade: the one conversion core (D24) ----------------------------------------------------


def _mismatch(ctx: ToolContext, args: UpgradeArgs, truth: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    """(the install `args.to_kicad` resolves to or None, the differences from `truth`). `to_kicad`
    may be a version prefix or an install root as kw_env lists it (`find(probe_paths=False)` via
    `services.install`); what is compared is the resolved install's formats, because every 10.99
    nightly and bench build reports the same version string (D24). Files are compared as a set
    after normcase."""
    mismatch: dict[str, Any] = {}
    if (args.from_kicad or "").strip() != truth["from_kicad"]:
        mismatch["from_kicad"] = {"given": args.from_kicad, "expected": truth["from_kicad"]}
    install: Any = None
    spec = (args.to_kicad or "").strip()
    if spec:
        with contextlib.suppress(KiwrapError):
            install = ctx.services.install(spec)
    formats = _install_epochs(ctx, install) if install is not None else None
    resolved = {k: formats.get(k) for k in DESIGN_KINDS} if formats is not None else None
    if resolved is None or resolved != truth["to_format"]:
        mismatch["to_kicad"] = {
            "given": args.to_kicad,
            "expected": truth["to_kicad"],
            "resolved": getattr(install, "version", None),
            "resolved_format": resolved,
            "expected_format": truth["to_format"],
        }

    def key(path: str) -> str:
        return os.path.normcase(path.strip().replace("\\", "/").removeprefix("./"))

    given = {key(f): f for f in args.files}
    want = {key(f): f for f in truth["files"]}
    missing = [want[k] for k in sorted(want.keys() - given.keys())]
    extra = [given[k] for k in sorted(given.keys() - want.keys())]
    if missing or extra:
        mismatch["files"] = {"missing": missing, "extra": extra}
    return install, mismatch


def _mismatch_error(project: Project, truth: dict[str, Any], mismatch: dict[str, Any]) -> KiwrapError:
    return KiwrapError(
        "E_BAD_ARGS",
        f"kw_project_upgrade: {', '.join(mismatch)} do not match {project.name} as it is now; nothing was converted",
        why=(
            "the server rebuilds the arguments under the project lock and converts only what the user approved: "
            "the call's arguments must equal data.upgrade_args (D24)"
        ),
        fix=(
            "tell the user the project changed (or the arguments were not passed verbatim); if they still want the "
            "upgrade, call kw_project_upgrade with data.upgrade_args exactly as given - they will be asked again"
        ),
        candidates=[UPGRADE_TOOL, "kw_project(action='status')"],
        example=f"kw_project_upgrade(project={truth['project']!r}, from_kicad={truth['from_kicad']!r}, "
        f"to_kicad={truth['to_kicad']!r}, files={truth['files']!r})",
        data={"mismatch": mismatch, "upgrade_args": truth},
    )


def _nothing_to_convert(
    project: Project, choice: TargetChoice, truth: dict[str, Any], mismatch: dict[str, Any]
) -> KiwrapError:
    pro = project.pro_path.as_posix()
    return KiwrapError(
        "E_BAD_ARGS",
        f"nothing to convert: every design file of {project.name} is in the format KiCad {choice.version} "
        "(the KiCad it runs on) writes",
        why="kw_project_upgrade only moves files to the format of the KiCad the project runs on (D24)",
        fix=(
            "no upgrade is needed. To move the project to a newer installed KiCad's format, pin that KiCad first: "
            f"kw_project(action='retarget', project='{pro}', target_kicad=...); kw_project(action='status') then "
            "returns the upgrade_args to show the user"
        ),
        candidates=["kw_project(action='retarget')", "kw_project(action='status')"],
        example=f'kw_project(action="retarget", project="{pro}", target_kicad="<version>")',
        data={"upgrade_args": truth, "mismatch": mismatch or None},
    )


def _bound_refusal(project: Project, bound: dict[str, Any]) -> KiwrapError:
    pro = project.pro_path.as_posix()
    return KiwrapError(
        "E_FILE_LOCKED",
        f"a live KiCad session (kw_live) holds {project.name}; kw_project_upgrade replaces its files on disk",
        why="the GUI keeps the old files in memory and would write them back (or save over the converted ones)",
        fix=f"kw_live(action='detach', project='{pro}') first, then call kw_project_upgrade again with the same "
        "arguments (the user is asked again)",
        candidates=["kw_live(action='detach')"],
        example=f'kw_live(action="detach", project="{pro}")',
        data={"live_session": {k: bound.get(k) for k in ("session_id", "pid") if k in bound}},
    )


def _upgrade_under(
    ctx: ToolContext,
    m: Mutation,
    project: Project,
    args: UpgradeArgs,
    consent: dict[str, Any],
    *,
    prefer: str | None,
) -> tuple[dict[str, Any], Any]:
    """Inside the project lock: the kw_live refusal, the recompute, the mismatch / nothing-to-convert
    / capability refusals, then `perform_upgrade(convert=files, checks=True)`. Returns (the report
    with the D24 receipt fields, the install converted to)."""
    if m.bound is not None:
        raise _bound_refusal(project, m.bound)
    choice = resolve_target(ctx, project, m.state, prefer=prefer)
    if not choice.readable:
        raise format_newer_error(choice, project)
    truth = upgrade_args(choice, project)
    # the KiCad line(s) that open the listed files now and no longer will (the receipt's warning)
    olds = _conversion_text(choice)[1]
    install, mismatch = _mismatch(ctx, args, truth)
    if not truth["files"]:
        raise _nothing_to_convert(project, choice, truth, mismatch)
    if mismatch:
        raise _mismatch_error(project, truth, mismatch)
    _require_upgrade_capability(ctx, install, ctx.services.caps(install))
    m.target = choice
    report = perform_upgrade(
        ctx, m, install, convert=truth["files"], checks=True, reason=f"{UPGRADE_TOOL} ({consent.get('via')})"
    )
    m.history_extra["consent"] = dict(consent)
    report.update(
        {
            "from_kicad": truth["from_kicad"],
            "to_kicad": m.state.target_kicad,
            "to_format": truth["to_format"],
            "not_converted": _not_converted(project, truth["files"]),
            "consent": dict(consent),
            "older_kicads": olds,
        }
    )
    return report, install


def run_upgrade(
    ctx: ToolContext,
    args: UpgradeArgs,
    consent: dict[str, Any] | None = None,
    *,
    running: Mutation | None = None,
    prefer: str | None = None,
) -> Receipt:
    """The ONE conversion core (D24): `kw_project_upgrade`'s handler, and the elicitation
    shortcut's accept (`_base.effective_target`, `consent` gate/via 'elicitation').

    Order: the client gate (a Claude Code older than 2.1.199 does not ask the user: E_UNSUPPORTED);
    then under `mutation(ctx, project, 'kw_project_upgrade', gate=False)` - or inside `running`, the
    mutation of the call whose write needed the upgrade (its lock is held) - the kw_live bound
    refusal, the recompute of `upgrade_args` and the comparison (E_BAD_ARGS with `data.mismatch` and
    the true `data.upgrade_args`), nothing to convert (E_BAD_ARGS naming kw_project retarget), the
    `cli.has_sch_pcb_upgrade` check (E_UNSUPPORTED with candidates), then `perform_upgrade` on
    exactly the listed files with ERC/DRC before and after. The receipt names the consent record
    (`consent_source`), never a human click. `prefer` is the elicitation's per-call target
    (kw_live's `target_kicad`), never a tool argument."""
    if consent is None:
        consent = consent_source(bound_request())
        refuse_unsupported_client(consent)
    project = Project.from_path(args.project)
    if running is not None:
        report, install = _upgrade_under(ctx, running, project, args, consent, prefer=prefer)
        commit = None
        with contextlib.suppress(KiwrapError):
            commit = snapshot_create(
                project.dir,
                f"[kiwrap] {running.tool}: upgrade to KiCad {install.version} format ({consent.get('via')})",
            )
        report["snapshot_after"] = commit
        running.receipt.warnings.append(
            f"the project was upgraded to KiCad {install.version} format ({consent.get('via')}) before this change "
            f"(snapshot {str(commit or '')[:12]}; kw_snapshot restore {str(report.get('snapshot_before') or '')[:12]} "
            "undoes it)"
        )
        running.upgrade_report = report
        return Receipt(provider_used=ctx.services.provider(install), data={"ok": True, **report})
    with mutation(ctx, project, UPGRADE_TOOL, gate=False) as m:
        report, install = _upgrade_under(ctx, m, project, args, consent, prefer=prefer)
        m.target = resolve_target(ctx, project, m.state)
        m.summary = f"to KiCad {install.version} format ({len(report['replaced'])} files)"
        snapshot_before = str(report.get("snapshot_before") or "")
        converted = [f["path"] for f in report["files"]]
        warnings = [
            f"{report['older_kicads']} can no longer open {_files_text(converted)}; kw_snapshot restore "
            f"{snapshot_before[:12]} undoes it"
        ]
        warnings.extend(_check_changes(report.get("checks")))
        if report.get("stage_dropped"):
            warnings.append(
                "stage dropped below "
                + ", ".join(report["stage_dropped"])
                + ": an error count rose with the conversion"
            )
        if consent.get("via") == "unknown_client":
            client = consent.get("client") or {}
            warnings.append(
                f"the MCP client ({client.get('name') or 'unnamed'} {client.get('version') or ''}".rstrip()
                + ") is not known to ask the user before kw_project_upgrade runs; kiwrap cannot tell whether "
                "the user approved this conversion"
            )
        m.receipt.provider_used = ctx.services.provider(install)
        m.receipt.warnings.extend(warnings)
        m.receipt.validators.extend(_check_validators(report.get("checks")))
        m.receipt.next_suggested = "kw_sch_check"
        m.receipt.data = {
            "ok": True,
            "project": project.pro_path.as_posix(),
            "from_kicad": report["from_kicad"],
            "to_kicad": report["to_kicad"],
            "to_format": report["to_format"],
            "files": report["files"],
            "replaced": report["replaced"],
            "not_converted": report["not_converted"],
            "checks": report["checks"],
            "snapshot_before": report.get("snapshot_before"),
            "stage_dropped": report["stage_dropped"],
            "target": {
                "kicad": m.target.version,
                "source": m.target.source,
                "epoch_match": m.target.writable,
            },
            "consent": report["consent"],
            "epochs": report["epochs"],
            "work_dir": report["work_dir"],
        }
        return m.receipt


def _upgrade_tool(ctx: ToolContext, args: UpgradeArgs) -> Receipt:
    return run_upgrade(ctx, args)


def run(ctx: ToolContext, args: ProjectArgs) -> Receipt:
    if args.action == "create":
        return _create(ctx, args)
    if args.action == "status":
        return _status(ctx, args)
    if args.action == "retarget":
        return _retarget(ctx, args)
    return _upgrade_preview(ctx, args)


SPEC = ToolSpec(
    name="kw_project",
    title="Project",
    description=(
        "create: write a minimal valid .kicad_pro + empty root .kicad_sch + .kicad_pcb in the target KiCad's format "
        "(optionally with a rectangular outline) and start the snapshot history; refused in a directory that already "
        "holds .kicad_sch / .kicad_pcb files. status: stage, hashes, epochs, external edits, the KiCad the project "
        "runs on (target_effective, target_source, epoch_match), next suggested tool, and - whenever a write would "
        "need a file-format upgrade - upgrade_args (the exact kw_project_upgrade arguments), install_hint and "
        "retarget_candidates. retarget: pin an installed KiCad (or 'auto') without touching the files. "
        "upgrade_preview: convert a copy under kiwrap's storage home and report the ERC/DRC counts before/after; "
        "writes nothing in the project (it also runs on a read-only server). Converting the project's files is "
        "kw_project_upgrade, a separate tool the user approves call by call."
    ),
    input_model=ProjectArgs,
    handler=run,
    # D24: kw_project no longer replaces design files (that is kw_project_upgrade); create and
    # retarget write the project (new files, the pin), status and upgrade_preview are reads
    annotations=MUTATING,
)

UPGRADE_DESCRIPTION = (
    "Convert a project's design files to a newer KiCad file format - the only way kiwrap does it. The older KiCad "
    "can no longer open the converted files. The client's permission prompt for this call is the user's consent: "
    "call it only after telling the user what changes (from_kicad -> to_kicad, the files) and the alternatives "
    "(install the matching KiCad, or kw_project retarget); never call it to try something, and never convert files "
    "with kicad-cli yourself. Pass data.upgrade_args of E_FORMAT_UPGRADE_REQUIRED (or of kw_project(action="
    "'status')) verbatim: the server rebuilds them under the project lock and refuses any difference with "
    "E_BAD_ARGS (data.upgrade_args = the true ones) - after the prompt, so a mismatch costs the user a second one. "
    "It snapshots the project, converts exactly `files` on a copy with kicad-cli, runs ERC/DRC on the copy before "
    "and after, replaces the changed files, pins to_kicad and drops erc_clean/drc_clean if an error count rose. "
    ".kicad_pro, .kicad_prl, .kicad_dru, library tables and project libraries are never converted. The receipt "
    "lists files (stamps before/after), checks, snapshot_before (kw_snapshot restore undoes it) and the consent "
    "record (the client flow that approved the call). kw_project(action='upgrade_preview') previews without "
    "changing anything. Not available on a read-only server."
)

UPGRADE_SPEC = ToolSpec(
    name=UPGRADE_TOOL,
    title="Upgrade project file format",
    description=UPGRADE_DESCRIPTION,
    input_model=UpgradeArgs,
    handler=_upgrade_tool,
    # replaces design files with a newer format the older KiCad cannot open (review theme T8)
    annotations=DESTRUCTIVE,
    # Claude Code >= 2.1.199 asks the user before every call, in every permission mode (D24)
    meta={REQUIRES_USER_INTERACTION: True},
    # no `persist`: a prompted call must never be one that can only fail (D24)
    common_persist=False,
)
