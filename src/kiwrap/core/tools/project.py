"""kw_project: create a minimal valid project, report its status, pin its KiCad, upgrade its format.

Actions (docs/DECISIONS.md D21, requests/kiwrap-feature-requests.review.md item 1 / B1, SAFETY
contract (c)):
- `create(dir, name, target_kicad?, board_size_mm?)`: the files are written in the target's
  format and the project is pinned to it (`target_source: created`). A directory that already
  holds a `.kicad_sch` / `.kicad_pcb` is refused before anything is written, and the three files
  are written with exclusive create (fixer finding 1).
- `status(project)`: stage, hashes, epochs, external edits, plus which KiCad the project runs on
  (`target_effective`, `target_source`, `epoch_match`, `warnings`: `_base.effective_target`).
- `retarget(project, target_kicad)`: pin an install (or `'auto'`: unpin, follow the files'
  format) without touching any design file. Pinning a KiCad that writes a newer format than the
  files is allowed; every write then asks for the upgrade first.
- `upgrade(project, to_kicad?, confirm, dry_run)`: the ONLY sanctioned up-conversion. The project
  is copied under the storage home, every sheet and the board are upgraded there with
  `kicad-cli sch/pcb upgrade --force` (per sheet: the root's upgrade leaves children alone,
  `kiwrap.backends.cli.upgrade`), ERC/DRC run on the copy before and after, the pre-upgrade state
  is a snapshot, then the files are replaced (all or nothing: a failed replacement puts the
  replaced files back and removes its temp file, fixer finding 5) and the install is pinned.
  Sub-sheets in subdirectories are part of it (fixer finding 2). `confirm=true` means the
  user agreed; without it the user is asked (MCP elicitation) or the call explains what to ask.
  `dry_run=true` does everything on the copy and reports, never replacing anything.
"""

from __future__ import annotations

import contextlib
import hashlib
import re
import shutil
import stat
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from kiwrap.core.state import STAGES, KiwrapError, Receipt
from kiwrap.core.state.project import Project, ProjectState, file_epoch
from kiwrap.core.state.snapshots import create as snapshot_create
from kiwrap.core.state.snapshots import head, is_repo_root
from kiwrap.core.state.stages import downgrade_below, record_stage, stage_at_least
from kiwrap.core.tools import templates
from kiwrap.core.tools._base import (
    DESTRUCTIVE,
    SOURCE_CREATED,
    SOURCE_PINNED,
    Mutation,
    TargetChoice,
    ToolContext,
    ToolSpec,
    ask_upgrade_consent,
    consent_message,
    format_newer_error,
    format_upgrade_error,
    mutation,
    read_state,
    resolve_target,
    suggest_next,
    to_kiwrap_error,
)

_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$")
#: `retarget` values that remove the pin (the project then follows its files' format)
UNPIN = ("", "auto", "none", "file", "files")

__all__ = ["ProjectArgs", "SPEC", "file_epoch", "perform_upgrade", "run", "status_data"]


class ProjectArgs(BaseModel):
    action: Literal["create", "status", "retarget", "upgrade"] = Field(
        description="create | status | retarget | upgrade"
    )
    project: str | None = Field(
        default=None, description="status/retarget/upgrade: project directory or .kicad_pro path"
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
    to_kicad: str | None = Field(
        default=None,
        description="upgrade: the installed KiCad whose format the files are converted to (default: the install the "
        "project runs on when it reads but does not write the files' format)",
    )
    confirm: bool = Field(
        default=False,
        description="upgrade: true = the user agreed to convert the files (the older KiCad can no longer open "
        "them); without it the user is asked, or the call explains what to ask",
    )
    dry_run: bool = Field(
        default=False,
        description="upgrade: convert a copy and report ERC/DRC before/after; never replace files",
    )


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


def target_status(ctx: ToolContext, project: Project, state: ProjectState) -> dict[str, Any]:
    """The D21 fields of `kw_project status`: which KiCad this project runs on and whether that
    KiCad writes the files' own format (`epoch_match`)."""
    try:
        choice = resolve_target(ctx, project, state)
    except KiwrapError as err:
        return {
            "target_effective": None,
            "target_kicad_effective": None,
            "target_source": None,
            "epoch_match": None,
            "target": None,
            "target_warnings": [f"{err.code}: {err.message}"],
        }
    warnings = list(choice.notes)
    if state.target_kicad and not choice.writable and choice.readable:
        matching = [c["version"] for c in choice.candidates if c.get("matches")]
        if matching:
            warnings.append(
                f"the project is pinned to KiCad {choice.version}, which writes a newer format than its files; "
                f"KiCad {matching[0]} writes theirs: kw_project(action='retarget', target_kicad='{matching[0]}') "
                "keeps the format, kw_project(action='upgrade', confirm=true) converts the files"
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
        "target_warnings": warnings,
        "_choice": choice,
    }


def status_data(ctx: ToolContext, project: Project) -> dict[str, Any]:
    state, external = read_state(project)
    installed = None
    with contextlib.suppress(KiwrapError):
        installed = [i.version for i in ctx.services.installs()]
    target_ok = state.target_kicad in installed if (installed and state.target_kicad) else None
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
            m.state.target_kicad, m.state.target_source = choice.version, SOURCE_PINNED
            choice.source = SOURCE_PINNED
            m.summary = f"retarget {choice.version}"
        m.target = choice
        warnings = list(choice.notes)
        if choice.readable and not choice.writable:
            warnings.append(
                f"KiCad {choice.version} writes a newer format than the files: every write will ask to upgrade the "
                "project first (kw_project(action='upgrade', confirm=true) does it now)"
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
            },
        )
        return m.receipt


# ---- upgrade -----------------------------------------------------------------------------------------


def _work_dir(ctx: ToolContext, project: Project) -> Path:
    digest = hashlib.sha1(str(project.dir.resolve()).lower().encode("utf-8")).hexdigest()[:8]
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


def perform_upgrade(
    ctx: ToolContext,
    m: Mutation,
    install: Any,
    *,
    checks: bool = True,
    reason: str = "",
    dry_run: bool = False,
) -> dict[str, Any]:
    """Convert every design file of `m.project` to `install`'s format (the caller holds the
    project lock inside `mutation()` and has the user's consent). Converted on a copy under the
    storage home; ERC/DRC counts before and after when `checks`; the pre-upgrade state is a
    snapshot; then the files are replaced, the install is pinned and the stages re-hashed (a
    check that got worse drops the stage it backed). Returns the report."""
    project = m.project
    caps = ctx.services.caps(install)
    if not caps.has("cli.has_sch_pcb_upgrade"):
        able = []
        with contextlib.suppress(KiwrapError):
            able = [
                i.version
                for i in ctx.services.installs()
                if ctx.services.caps(i).has("cli.has_sch_pcb_upgrade")
            ]
        raise KiwrapError(
            "E_UNSUPPORTED",
            f"KiCad {install.version} cannot upgrade schematics and boards (cli.has_sch_pcb_upgrade)",
            why="kw_project upgrade converts the files with kicad-cli sch/pcb upgrade",
            fix="pass to_kicad naming an install that has it"
            if able
            else "install a KiCad with sch/pcb upgrade",
            candidates=able,
            data={"capability": "cli.has_sch_pcb_upgrade", "kicad": install.version},
        )
    config = ctx.services.config_root(install)
    files = project.design_files()
    if not files:
        raise KiwrapError(
            "E_PROJECT_NOT_FOUND",
            f"{project.name} has no .kicad_sch / .kicad_pcb to upgrade",
            fix="kw_project create writes them",
        )
    work = _work_dir(ctx, project)
    before_counts: dict[str, Any] | None = None
    if checks:
        before_dir = ctx.services.copy_project(project.dir, files, work / "before")
        before_counts = _checks(
            ctx,
            install,
            caps,
            config,
            Path(before_dir) / project.root_sch.name,
            Path(before_dir) / project.board.name,
        )
    result = ctx.services.upgrade_project(install, config, project.dir, files, work / "after", caps=caps)
    after_counts = (
        _checks(
            ctx,
            install,
            caps,
            config,
            Path(result.work_dir) / project.root_sch.name,
            Path(result.work_dir) / project.board.name,
        )
        if checks
        else None
    )
    report: dict[str, Any] = {
        "kicad": install.version,
        "reason": reason,
        "files": [f.to_dict() for f in result.files],
        "from": {_rel_of(project, f): f.stamp_before for f in result.files},
        "to": {_rel_of(project, f): f.stamp_after for f in result.files},
        "checks": {"before": before_counts, "after": after_counts} if checks else None,
        "dry_run": dry_run,
        "work_dir": Path(result.work_dir).as_posix(),
    }
    if dry_run:
        return report
    snapshot_before = m.baseline
    if snapshot_before is None:
        with contextlib.suppress(KiwrapError):
            snapshot_before = snapshot_create(project.dir, f"[kiwrap] pre-upgrade to KiCad {install.version}")
    report["snapshot_before"] = snapshot_before
    replaced = _replace_all(project, list(result.files), work / "originals", snapshot_before)
    report["replaced"] = replaced
    m.state.target_kicad, m.state.target_source = install.version, SOURCE_PINNED
    for s in STAGES:
        if s in m.state.hashes:
            record_stage(project, m.state, s, advance=False)
    if checks and before_counts and after_counts:
        for kind, gate_stage in (("erc", "erc_clean"), ("drc", "drc_clean")):
            b, a = before_counts.get(kind) or {}, after_counts.get(kind) or {}
            if (
                "errors" in a
                and int(a["errors"]) > int(b.get("errors", 0))
                and stage_at_least(m.state.stage, gate_stage)
            ):
                downgrade_below(m.state, gate_stage)
                report.setdefault("stage_dropped", []).append(gate_stage)
    # the stamp guard compares against the upgraded stamps from now on (it stays armed for the
    # rest of the call: only this sanctioned conversion may raise them)
    report["epochs"] = {"before": dict(m.stamps_before), "after": _stamps(project)}
    m.stamps_before = {**m.stamps_before, **_stamps(project)}
    for name in replaced:
        if name not in m.receipt.changed["updated"]:
            m.receipt.changed["updated"].append(name)
    return report


def _stamps(project: Project) -> dict[str, int | None]:
    return {f.path: f.stamp for f in project.format_stamps()}


def _rel_of(project: Project, f: Any) -> str:
    """An upgraded file's path relative to the project (`sch/x.kicad_sch` for a sub-sheet)."""
    rel = str(getattr(f, "rel", "") or "")
    return rel or project.relpath(Path(f.source))


def _upgrade_target(
    ctx: ToolContext, project: Project, m: Mutation, to_kicad: str | None
) -> tuple[Any, TargetChoice]:
    """(install to convert to, the resolution with that install): `to_kicad`, else the install
    the project runs on (pinned, or the nearest newer reader)."""
    choice = resolve_target(ctx, project, m.state, prefer=to_kicad or None)
    if not choice.readable:
        raise format_newer_error(choice, project)
    return choice.install, choice


def _upgrade(ctx: ToolContext, args: ProjectArgs) -> Receipt:
    project = _need_project(args)
    with mutation(ctx, project, "kw_project", gate=False) as m:
        install, choice = _upgrade_target(ctx, project, m, args.to_kicad)
        m.target = choice
        base = {
            "ok": True,
            "project": project.pro_path.as_posix(),
            "to_kicad": install.version,
            "project_epoch": dict(choice.project_epoch),
            "install_epoch": dict(choice.install_epoch),
            "project_kicad": choice.project_kicad,
        }
        if choice.writable and not args.dry_run:
            # nothing to convert: the install already writes the files' format - pin it
            m.state.target_kicad, m.state.target_source = install.version, SOURCE_PINNED
            m.summary = f"upgrade: already KiCad {install.version} format (pinned)"
            m.receipt = Receipt(
                provider_used="file",
                warnings=[
                    f"the files are already in the format KiCad {install.version} writes; nothing converted"
                ],
                data={**base, "upgraded": False, "target_kicad": install.version},
            )
            return m.receipt
        if not args.dry_run and not args.confirm:
            answer = ask_upgrade_consent(consent_message(project, choice))
            if answer != "accept":
                raise format_upgrade_error(choice, project, answer=answer)
        report = perform_upgrade(
            ctx, m, install, checks=True, reason="kw_project upgrade", dry_run=args.dry_run
        )
        warnings: list[str] = []
        checks = report.get("checks") or {}
        for kind in ("erc", "drc"):
            b = (checks.get("before") or {}).get(kind) or {}
            a = (checks.get("after") or {}).get(kind) or {}
            if (
                "errors" in a
                and "errors" in b
                and (a["errors"], a["warnings"]) != (b["errors"], b["warnings"])
            ):
                warnings.append(
                    f"{kind.upper()} changed with the conversion: errors {b['errors']} -> {a['errors']}, "
                    f"warnings {b['warnings']} -> {a['warnings']} (counted on copies)"
                )
        if args.dry_run:
            m.summary = f"upgrade dry run to KiCad {install.version}"
            warnings.append(
                "dry run: nothing in the project was changed; pass confirm=true to convert the files"
            )
        else:
            m.summary = f"upgrade to KiCad {install.version} format"
            m.target = resolve_target(ctx, project, m.state)
            if choice.project_kicad:
                warnings.append(
                    f"KiCad {choice.project_kicad} can no longer open the upgraded files; the pre-upgrade state is "
                    f"snapshot {str(report.get('snapshot_before') or '')[:12]} (kw_snapshot restore)"
                )
        m.receipt.provider_used = ctx.services.provider(install)
        m.receipt.warnings.extend(warnings)
        m.receipt.next_suggested = "kw_sch_check" if not args.dry_run else None
        m.receipt.data = {
            **base,
            "upgraded": not args.dry_run,
            "target_kicad": m.state.target_kicad,
            "upgrade": report,
        }
        return m.receipt


def run(ctx: ToolContext, args: ProjectArgs) -> Receipt:
    if args.action == "create":
        return _create(ctx, args)
    if args.action == "status":
        return _status(ctx, args)
    if args.action == "retarget":
        return _retarget(ctx, args)
    return _upgrade(ctx, args)


SPEC = ToolSpec(
    name="kw_project",
    title="Project",
    description=(
        "create: write a minimal valid .kicad_pro + empty root .kicad_sch + .kicad_pcb in the target KiCad's format "
        "(optionally with a rectangular outline) and start the snapshot history; refused in a directory that already "
        "holds .kicad_sch / .kicad_pcb files. status: stage, hashes, epochs, "
        "external edits, the KiCad the project runs on (target_effective, target_source, epoch_match), next "
        "suggested tool. retarget: pin an installed KiCad (or 'auto') without touching the files. upgrade: convert "
        "every sheet (sub-sheets in subdirectories too) and the board to a newer KiCad's format - only with the "
        "user's consent (confirm=true), on a copy first, ERC/DRC before/after, snapshot; dry_run=true only reports. "
        "The tool is annotated destructive because upgrade replaces files; status and upgrade(dry_run=true) are "
        "read-only (they also run on a read-only server)."
    ),
    input_model=ProjectArgs,
    handler=run,
    # `upgrade` replaces the design files (with consent, after a snapshot): review theme T8, honest hints.
    # MCP annotations are per tool, so status / dry_run share the hint; the description and
    # kw_help(topic='readonly') say they are read-only (fixer finding 8)
    annotations=DESTRUCTIVE,
)
