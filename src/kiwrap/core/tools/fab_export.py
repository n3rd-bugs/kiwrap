"""kw_fab_export: fabrication outputs through kicad-cli, gated on `drc_clean`.

Targets map onto `kiwrap.backends.cli.export`: board targets (gerbers, drill, pos, pdf, step, svg,
dxf, stats, ipc2581, odb, glb) go to `export_pcb`, `bom` and `netlist` to `export_sch`.
Outputs land in `<project>/fab/<target>/` (never snapshotted).

Profiles (`kiwrap.core.fab`, docs/DECISIONS.md D9): `profile="generic"` keeps kicad-cli's defaults;
`profile="jlcpcb"` gives gerbers / drill / pos / bom the vendor's options (filtered against the
KiCad's --help, dropped flags reported), writes the CPL with the rotation corrections applied
(`data.positions.corrected` lists every changed row), the BOM with `LCSC Part #` (through
`kw_fab_bom`'s builder) and zips gerbers + drill into `<project>/fab/<name>-jlcpcb-gerbers.zip`. A directory kiwrap wrote carries a
`.kiwrap-export` marker; only such directories are ever deleted by `clean`. The gate
(docs/ARCHITECTURE.md §11: `kw_fab_export` requires must_fix == 0, i.e. stage `drc_clean`) can be
overridden with `force={"reason": ...}`, which is logged in the snapshot message. Long runs can
be started with `run_async=true` and polled with `kw_job`.
"""

from __future__ import annotations

import shutil
import zipfile
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from kiwrap.core.fab import FabProfile, get_profile
from kiwrap.core.fab.profiles import PROFILE_NAMES
from kiwrap.core.state import KiwrapError, Receipt
from kiwrap.core.state.jobs import JobHandle
from kiwrap.core.state.project import Project
from kiwrap.core.state.receipts import receipt_to_dict
from kiwrap.core.state.stages import record_stage
from kiwrap.core.tools._base import MUTATING, ToolContext, ToolSpec, mutation, to_kiwrap_error
from kiwrap.core.tools.fab_bom import bom_table
from kiwrap.formats.reports.position import parse_pos_csv

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
        default="generic",
        description=(
            f"fabrication profile ({' | '.join(PROFILE_NAMES)}): jlcpcb = vendor options for gerbers/drill/pos/bom, "
            "CPL rotation corrections, BOM with 'LCSC Part #', gerber+drill zip"
        ),
    )
    include_dnp: bool | None = Field(
        default=None, description="keep DNP parts in the profile's BOM / CPL (default: the profile's choice)"
    )
    bom_fields: list[str] = Field(
        default_factory=list, description="extra symbol fields as BOM columns (profile bom target only)"
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


PROFILE_TARGETS = ("gerbers", "drill", "pos", "bom")


_COPPER_KINDS = ("signal", "power", "mixed", "jumper")


def _layer_is_copper(layer: Any, in_leading_block: bool) -> bool:
    """A layer record's copper flag (`is_copper` / `copper` on the file and IPC models); a record
    without one is copper only inside the leading block of copper-typed rows, the way KiCad's
    parser reads the `(layers …)` table (`kiwrap.formats.board.copper_layer_rows`)."""
    for attr in ("is_copper", "copper"):
        val = getattr(layer, attr, None)
        if isinstance(val, bool):
            return val
    return in_leading_block and getattr(layer, "kind", "") in _COPPER_KINDS


def copper_layers(ctx: ToolContext, project: Project) -> list[str]:
    """The board's copper layer names (F.Cu, In1.Cu, ..., B.Cu) through the file reader; the
    two-layer default when the reader is not wired or the board does not parse. A `User.N` row
    typed `signal` after the user rows is NOT copper (KiCad takes the leading block only), so it
    never becomes a copper gerber in the fab zip."""
    try:
        model = ctx.services.file_read_board(project.board)
    except Exception:  # noqa: BLE001 - optional reader
        return ["F.Cu", "B.Cu"]
    names: list[str] = []
    leading = True
    for layer in getattr(model, "layers", ()):
        if _layer_is_copper(layer, leading):
            names.append(str(layer.name))
        else:
            leading = False
    return names or ["F.Cu", "B.Cu"]


def dnp_refs(ctx: ToolContext, project: Project) -> list[str]:
    """References of DNP footprints on the board (file reader; [] when unavailable)."""
    try:
        model = ctx.services.file_read_board(project.board)
    except Exception:  # noqa: BLE001
        return []
    return sorted(fp.reference for fp in getattr(model, "footprints", ()) if getattr(fp, "dnp", False))


def _export_bom(
    ctx: ToolContext,
    m: Any,
    install: Any,
    caps: Any,
    config: Any,
    project: Project,
    profile: FabProfile,
    out: Path,
    args: FabExportArgs,
    extra: dict[str, Any],
) -> list[Path]:
    """The profile's BOM CSV through `kw_fab_bom`'s builder (kicad-cli bom export, else the netlist)."""
    workdir = project.state_dir / "tmp" / "bom"
    table, degraded = bom_table(
        ctx,
        install,
        config,
        caps,
        project,
        profile=profile,
        extra_fields=list(args.bom_fields),
        workdir=workdir,
    )
    for d in degraded:
        if d not in m.receipt.degraded:
            m.receipt.degraded.append(d)
    m.receipt.warnings.extend(table.warnings)
    rows = profile.bom_rows(table, include_dnp=args.include_dnp)
    path = out / (profile.file_name("bom", project.name) or f"{project.name}-bom.csv")
    path.write_text(
        profile.bom_csv(table, include_dnp=args.include_dnp, extra_fields=list(args.bom_fields)),
        encoding="utf-8",
    )
    missing = [ref for r in rows if not r.dnp and not r.lcsc for ref in r.refs]
    extra["bom"] = {
        "source": table.source,
        "groups": len(rows),
        "components": sum(r.qty for r in rows),
        "dnp_refs": [ref for r in table.dnp for ref in r.refs],
        "missing_lcsc": missing,
        "columns": [label for label, _ in profile.bom_columns] + list(args.bom_fields),
    }
    if missing:
        m.receipt.warnings.append(
            f"bom: {len(missing)} fitted part(s) without an LCSC Part # ({', '.join(missing[:12])}"
            + (", ..." if len(missing) > 12 else "")
            + ") - the vendor cannot assemble them"
        )
    shutil.rmtree(workdir, ignore_errors=True)
    return [path]


def _export_pos(
    ctx: ToolContext,
    m: Any,
    project: Project,
    profile: FabProfile,
    out: Path,
    args: FabExportArgs,
    extra: dict[str, Any],
    produced: list[Path],
    options: dict[str, Any],
) -> list[Path]:
    """Post-process KiCad's pos CSV into the profile's CPL with the rotation corrections applied."""
    raw = next((p for p in produced if Path(p).suffix.lower() == ".csv"), None)
    if raw is None:
        m.receipt.warnings.append("pos: kicad-cli wrote no CSV; rotation corrections not applied")
        m.receipt.degraded.append("pos_uncorrected")
        return produced
    text = Path(raw).read_text(encoding="utf-8", errors="replace")
    pos = parse_pos_csv(text, source=str(raw), units=str(options.get("units", "mm")))
    # DNP parts: kicad-cli's --exclude-dnp did it when the flag exists; else (or when the caller
    # explicitly wants them out) drop them here from the board's dnp attributes
    skip: list[str] = []
    if args.include_dnp is False or (args.include_dnp is None and profile.exclude_dnp):
        skip = dnp_refs(ctx, project)
    corrected = profile.process_positions(pos, skip_refs=skip)
    cpl = out / (profile.file_name("pos", project.name) or f"{project.name}-cpl.csv")
    cpl.write_text(profile.cpl_csv(corrected), encoding="utf-8")
    extra["positions"] = corrected.to_dict() | {"file": cpl.as_posix(), "kicad_file": Path(raw).as_posix()}
    if corrected.corrections:
        m.receipt.warnings.append(
            f"pos: {len(corrected.corrections)} rotation correction(s) applied ({profile.name} table): "
            + ", ".join(
                f"{c.ref} {c.rotation_kicad:g}->{c.rotation_out:g}" for c in corrected.corrections[:10]
            )
            + (", ..." if len(corrected.corrections) > 10 else "")
            + "; check them in the vendor's order preview"
        )
    return [*produced, cpl]


def _export_target(
    ctx: ToolContext,
    m: Any,
    install: Any,
    caps: Any,
    config: Any,
    project: Project,
    profile: FabProfile,
    target: str,
    out: Path,
    args: FabExportArgs,
    extra: dict[str, Any],
) -> list[Path]:
    """Run one target. Generic profile: kicad-cli defaults (Phase 0 behaviour, unchanged). A
    vendor profile: its options for gerbers / drill / pos / bom plus the post-processing."""
    vendor = profile.name != "generic" and target in PROFILE_TARGETS
    if not vendor:
        if target in SCH_TARGETS:
            ext = "csv" if target == "bom" else "net"
            return list(
                ctx.services.export_sch(
                    install, config, project.root_sch, target, out / f"{project.name}.{ext}"
                )
            )
        return list(ctx.services.export_pcb(install, config, project.board, target, out))
    if target == "bom":
        return _export_bom(ctx, m, install, caps, config, project, profile, out, args, extra)
    layers = copper_layers(ctx, project) if target == "gerbers" else None
    opts = profile.options_for(target, caps, copper_layers=layers)
    if opts.dropped:
        extra.setdefault("dropped_options", {})[target] = opts.dropped
        m.receipt.warnings.append(
            f"{target}: options this KiCad lacks were dropped: {', '.join(opts.dropped)}"
        )
    if target == "pos" and args.include_dnp is True:
        opts.options.pop("exclude_dnp", None)  # the caller wants DNP rows in the CPL
    produced = list(ctx.services.export_pcb(install, config, project.board, target, out, **opts.options))
    if target == "pos":
        return _export_pos(ctx, m, project, profile, out, args, extra, produced, opts.options)
    return produced


def _bundle(base: Path, name: str, files: dict[str, list[str]]) -> Path | None:
    """Zip the gerber + drill outputs (what the vendor's upload form takes)."""
    members = [Path(f) for t in ("gerbers", "drill") for f in files.get(t, [])]
    members = [p for p in members if p.is_file() and p.name != EXPORT_MARKER]
    if not members or not files.get("gerbers"):
        return None
    zpath = base / name
    with zipfile.ZipFile(zpath, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for p in members:
            z.write(p, arcname=p.name)
    return zpath


def export_sync(ctx: ToolContext, args: FabExportArgs, handle: JobHandle | None = None) -> Receipt:
    project = Project.from_path(args.project)
    targets = _check_targets(args.targets)
    try:
        profile = get_profile(args.profile)
    except ValueError as exc:
        raise KiwrapError(
            "E_BAD_ARGS", str(exc), fix="pick a known profile", candidates=list(PROFILE_NAMES)
        ) from exc
    with mutation(ctx, project, "kw_fab_export", force=args.force, gate=True) as m:
        install = ctx.services.install(m.state.target_kicad)
        caps = ctx.services.caps(install)
        config = ctx.services.config_root(install)
        base = Path(args.out_dir).expanduser().resolve() if args.out_dir else project.dir / "fab"
        clean = args.clean if args.clean is not None else args.out_dir is None
        base.mkdir(parents=True, exist_ok=True)
        files: dict[str, list[str]] = {}
        unsupported: dict[str, dict[str, Any]] = {}
        extra: dict[str, Any] = {}
        for i, target in enumerate(targets):
            if handle is not None:
                handle.check()
                handle.progress(i / len(targets), f"exporting {target}")
            out = base / target
            _prepare_target_dir(out, clean=clean)
            try:
                produced = _export_target(
                    ctx, m, install, caps, config, project, profile, target, out, args, extra
                )
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
        # DNP is a schematic flag: the CPL (from the board) only drops a DNP part once kw_pcb_sync
        # carried the flag over, so a BOM/CPL disagreement means the board is stale
        stale = set((extra.get("bom") or {}).get("dnp_refs", ())) & set(
            (extra.get("positions") or {}).get("refs", ())
        )
        if stale:
            m.receipt.degraded.append("cpl_has_dnp")
            m.receipt.warnings.append(
                f"pos: DNP part(s) {', '.join(sorted(stale))} are still in the CPL - the board does not carry "
                "the schematic's DNP flag yet: run kw_pcb_sync, then export again"
            )
        bundle_name = profile.bundle_name(project.name)
        if bundle_name and "gerbers" in files:
            zpath = _bundle(base, bundle_name, files)
            if zpath is not None:
                files["bundle"] = [zpath.as_posix()]
                if "drill" not in files:
                    m.receipt.warnings.append(
                        "bundle: no drill files in the zip (target drill was not exported)"
                    )
        if handle is not None:
            handle.progress(1.0, "done")
        ok = bool(files)
        if ok and not unsupported:
            record_stage(project, m.state, "exported")
        m.summary = (
            f"{','.join(files)} -> {base.as_posix()}"
            + (f" (unsupported: {','.join(unsupported)})" if unsupported else "")
            + (f" [{profile.name}]" if profile.name != "generic" else "")
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
            "profile": profile.name,
            "profile_notes": list(profile.notes),
            "forced": m.forced_reason,
            **extra,
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
        "profile='jlcpcb' applies the vendor's options, CPL rotation corrections (reported), the LCSC BOM and a "
        "gerber+drill zip. Gated on stage drc_clean unless force={'reason': ...}. Returns the file list per target; "
        "run_async=true returns a job_id."
    ),
    input_model=FabExportArgs,
    handler=run,
    annotations=MUTATING,
)
