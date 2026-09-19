"""kw_fab_export: fabrication outputs through kicad-cli, gated on `drc_clean`.

Targets map onto `kiwrap.backends.cli.export`: board targets (gerbers, drill, pos, pdf, step, svg,
dxf, stats, ipc2581, odb, glb) go to `export_pcb`, `bom` and `netlist` to `export_sch`.
Outputs land in `<project>/fab/<target>/` (never snapshotted). A directory kiwrap wrote carries a
`.kiwrap-export` marker; only such directories are ever deleted by `clean`. The gate
(docs/ARCHITECTURE.md §11: `kw_fab_export` requires must_fix == 0, i.e. stage `drc_clean`) can be
overridden with `force={"reason": ...}`, which is logged in the snapshot message. Long runs can
be started with `run_async=true` and polled with `kw_job`.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from kiwrap.core.state import KiwrapError, Receipt
from kiwrap.core.state.jobs import JobHandle
from kiwrap.core.state.project import Project
from kiwrap.core.state.receipts import receipt_to_dict
from kiwrap.core.state.stages import record_stage
from kiwrap.core.tools._base import MUTATING, ToolContext, ToolSpec, mutation, to_kiwrap_error

PCB_TARGETS = ("gerbers", "drill", "pos", "pdf", "step", "svg", "dxf", "stats", "ipc2581", "odb", "glb")
SCH_TARGETS = ("bom", "netlist")
DEFAULT_TARGETS = ["gerbers", "drill", "pos", "bom"]
KNOWN_TARGETS = (*PCB_TARGETS, *SCH_TARGETS)


class FabExportArgs(BaseModel):
    project: str = Field(description="project directory or .kicad_pro path")
    targets: list[str] = Field(
        default_factory=lambda: list(DEFAULT_TARGETS), description=f"any of {', '.join(KNOWN_TARGETS)}"
    )
    profile: str = Field(
        default="generic", description="fabrication profile (Phase 0: generic only; jlcpcb lands in Phase 1)"
    )
    out_dir: str | None = Field(default=None, description="output directory; default <project>/fab")
    clean: bool | None = Field(
        default=None,
        description="remove the target's previous kiwrap output first (only a <out_dir>/<target> that kiwrap "
        "wrote, recognised by its .kiwrap-export marker). Default: true under <project>/fab, false with out_dir",
    )
    force: dict[str, Any] | None = Field(
        default=None, description='override the drc_clean gate: {"reason": "..."} (logged in the snapshot)'
    )
    run_async: bool = Field(
        default=False, description="return a job_id immediately and run in the background (poll with kw_job)"
    )


EXPORT_MARKER = ".kiwrap-export"


def _prepare_target_dir(out: Path, *, clean: bool) -> None:
    """Create `<out_dir>/<target>`; with `clean`, first delete it ONLY when it carries the marker a
    previous kw_fab_export left there. A pre-existing directory that merely shares a target's name
    (`~/Documents/pdf`) is never deleted: it is refused so the caller picks another out_dir or
    `clean=false` (which writes alongside whatever is there)."""
    if clean and out.exists():
        if (out / EXPORT_MARKER).is_file():
            shutil.rmtree(out, ignore_errors=True)
        elif any(out.iterdir()):
            raise KiwrapError(
                "E_BAD_ARGS",
                f"{out.as_posix()} exists and was not written by kiwrap; refusing to delete it",
                why="clean=true removes the target's previous output, and only kiwrap's own output is ever removed",
                fix="pass clean=false to write into it, or choose an empty/new out_dir",
                example='kw_fab_export(project=..., targets=["pdf"], out_dir="C:/work/blinky-fab")',
            )
    out.mkdir(parents=True, exist_ok=True)


def _check_targets(targets: list[str]) -> list[str]:
    seen: list[str] = []
    for t in targets:
        key = t.strip().lower()
        if key not in KNOWN_TARGETS:
            raise KiwrapError(
                "E_BAD_ARGS",
                f"unknown export target {t!r}",
                fix="pick from the known targets",
                candidates=list(KNOWN_TARGETS),
                example='kw_fab_export(project=..., targets=["gerbers", "drill", "pos", "bom"])',
            )
        if key not in seen:
            seen.append(key)
    if not seen:
        raise KiwrapError("E_BAD_ARGS", "targets is empty", candidates=list(KNOWN_TARGETS))
    return seen


def export_sync(ctx: ToolContext, args: FabExportArgs, handle: JobHandle | None = None) -> Receipt:
    project = Project.from_path(args.project)
    targets = _check_targets(args.targets)
    if args.profile not in ("generic",):
        raise KiwrapError(
            "E_UNSUPPORTED",
            f"fabrication profile {args.profile!r} is not available in Phase 0",
            fix="use profile='generic'",
            data={"oldest_kicad_with_it": None, "fallback": "generic"},
        )
    with mutation(ctx, project, "kw_fab_export", force=args.force, gate=True) as m:
        install = ctx.services.install(m.state.target_kicad)
        config = ctx.services.config_root(install)
        base = Path(args.out_dir).expanduser().resolve() if args.out_dir else project.dir / "fab"
        clean = args.clean if args.clean is not None else args.out_dir is None
        base.mkdir(parents=True, exist_ok=True)
        files: dict[str, list[str]] = {}
        unsupported: dict[str, dict[str, Any]] = {}
        for i, target in enumerate(targets):
            if handle is not None:
                handle.check()
                handle.progress(i / len(targets), f"exporting {target}")
            out = base / target
            _prepare_target_dir(out, clean=clean)
            try:
                if target in SCH_TARGETS:
                    produced = ctx.services.export_sch(
                        install,
                        config,
                        project.root_sch,
                        target,
                        out / f"{project.name}.{'csv' if target == 'bom' else 'net'}",
                    )
                else:
                    produced = ctx.services.export_pcb(install, config, project.board, target, out)
            except Exception as exc:  # noqa: BLE001 - one unsupported target must not lose the others
                err = to_kiwrap_error(exc)
                if err.code != "E_UNSUPPORTED":
                    raise err from exc
                unsupported[target] = err.to_dict()
                m.receipt.degraded.append(f"{target}_unsupported")
                continue
            files[target] = [Path(p).as_posix() for p in produced]
            (out / EXPORT_MARKER).write_text(
                f"written by kiwrap kw_fab_export ({target}); deleted on the next export with clean=true\n",
                encoding="utf-8",
            )
        if handle is not None:
            handle.progress(1.0, "done")
        ok = bool(files)
        if ok and not unsupported:
            record_stage(project, m.state, "exported")
        m.summary = f"{','.join(files)} -> {base.as_posix()}" + (
            f" (unsupported: {','.join(unsupported)})" if unsupported else ""
        )
        m.receipt.provider_used = ctx.services.provider(install)
        m.receipt.changed["added"] = [f for lst in files.values() for f in lst]
        m.receipt.warnings.extend(f"{t}: {e['message']}" for t, e in unsupported.items())
        if m.forced_reason:
            m.receipt.warnings.append(f"gate overridden: {m.forced_reason}")
        # the Phase 0 flow ends here; suggest a retry only when part of the bundle was refused
        m.receipt.next_suggested = "kw_fab_export" if (not ok or unsupported) else None
        m.receipt.data = {
            "ok": ok,
            "out_dir": base.as_posix(),
            "targets": targets,
            "files": files,
            "unsupported": unsupported,
            "profile": args.profile,
            "forced": m.forced_reason,
        }
        return m.receipt


def run(ctx: ToolContext, args: FabExportArgs) -> Receipt:
    if not args.run_async:
        return export_sync(ctx, args)
    _check_targets(args.targets)
    Project.from_path(args.project)  # fail fast on a bad path before queueing

    def work(handle: JobHandle) -> dict[str, Any]:
        return receipt_to_dict(export_sync(ctx, args, handle))

    rec = ctx.jobs.submit("kw_fab_export", work)
    return Receipt(
        provider_used="jobs",
        next_suggested="kw_job",
        data={"ok": True, "job_id": rec.job_id, "state": rec.state},
    )


SPEC = ToolSpec(
    name="kw_fab_export",
    title="Fabrication export",
    description=(
        "Export fabrication outputs (gerbers, drill, pos, pdf, step, bom, ...) with kicad-cli into <project>/fab/<target>/. "
        "Gated on stage drc_clean unless force={'reason': ...}. Returns the file list per target; run_async=true returns a job_id."
    ),
    input_model=FabExportArgs,
    handler=run,
    annotations=MUTATING,
)
