"""kw_fab_export: fabrication outputs through kicad-cli, gated on `drc_clean`.

Targets map onto `kiwrap.backends.cli.export`: board targets (gerbers, drill, pos, pdf, step, svg,
dxf, stats, ipc2581, odb, glb) go to `export_pcb`, `bom` and `netlist` to `export_sch`.
Outputs land in `<project>/fab/<target>/` (never snapshotted) or in any caller `out_dir`.

Profiles (`kiwrap.core.fab`, docs/DECISIONS.md D9): `profile="generic"` keeps kicad-cli's defaults;
`profile="jlcpcb"` gives gerbers / drill / pos / bom the vendor's options (filtered against the
KiCad's --help, dropped flags reported), writes the CPL with the rotation corrections applied
(`data.positions.corrected` lists every changed row), the BOM with `LCSC Part #` (through
`kw_fab_bom`'s builder) and zips gerbers + drill into `<out_dir>/<name>-jlcpcb-gerbers.zip`. A
directory kiwrap wrote carries a `.kiwrap-export` marker; only such directories are ever deleted by
`clean`. The gate (docs/ARCHITECTURE.md §11: `kw_fab_export` requires must_fix == 0, i.e. stage
`drc_clean`) can be overridden with `force={"reason": ...}`, which is logged in the snapshot
message. Long runs can be started with `run_async=true` and polled with `kw_job`.

For reviewers (requests/kiwrap-feature-requests.review.md item 4):
- `out_dir` may be anywhere (outside the project), validated by `kiwrap.core.fab.outputs`;
- `layers` filters the gerber set (and the pdf/svg/dxf plots), in KiCad's canonical names, its
  standard names (`F.Silkscreen`) or the board's own layer names;
- `dry_run=true` returns the file plan without running KiCad or writing anything (no snapshot,
  no state, no gate enforcement: the gate is reported); for `pos` it adds `positions_preview`:
  the refs the CPL would hold, the excluded ones with the reason (`exclude_from_pos`,
  `attr_unspecified`, `tht`, `dnp`, `side`), flags (unspecified type, board-only) and a diff
  against the schematic BOM (`kiwrap.core.fab.assembly`);
- a real `pos` run reports the same `excluded` / `flags` / `bom_diff` for the rows KiCad wrote;
- read-only mode (D23: `serve --readonly`, `KIWRAP_READONLY=1`, `persist=false`): `mutation()`
  runs detached (`kiwrap.core.state.readonly`: state in memory, no snapshot); outputs go to the
  caller's `out_dir` or the call's results directory under the storage home; intermediate files
  (the BOM's kicad-cli CSV) always go under the storage home, never into the project.

Zone fills (D22: `kw_pcb_drc` no longer saves refilled zones): every target that plots copper
(gerbers, pdf, svg, dxf, odb) passes `--check-zones` where this KiCad's `--help` lists it (10.0+:
"Check and refill zones if required", in memory - the board file stays byte-identical, verified
on 10.0.6 / 10.99); where it does not (7-9, and ipc2581 on every version) the receipt carries
`degraded: zones_stale` when the board has zones.
"""

from __future__ import annotations

import shutil
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from kiwrap.core.fab import FabProfile, get_profile
from kiwrap.core.fab.assembly import PosOptions, bom_diff, plan_positions, schematic_parts
from kiwrap.core.fab.outputs import is_readonly, resolve_out_dir, scratch_dir
from kiwrap.core.fab.plan import (
    SINGLE_FILE_EXT,
    board_layer_names,
    drill_files,
    gerber_files,
    normalise_layers,
    pos_file,
)
from kiwrap.core.fab.profiles import PROFILE_NAMES
from kiwrap.core.state import KiwrapError, Receipt
from kiwrap.core.state import readonly as ro
from kiwrap.core.state.gates import GATES, Force, check_gate
from kiwrap.core.state.jobs import JobHandle
from kiwrap.core.state.project import Project, ProjectState
from kiwrap.core.state.receipts import receipt_to_dict
from kiwrap.core.state.stages import check_external_edit, record_stage
from kiwrap.core.tools._base import (
    MUTATING,
    ToolContext,
    ToolSpec,
    effective_target,
    mutation,
    to_kiwrap_error,
)
from kiwrap.core.tools.fab_bom import bom_table
from kiwrap.formats.reports.position import parse_pos

PCB_TARGETS = ("gerbers", "drill", "pos", "pdf", "step", "svg", "dxf", "stats", "ipc2581", "odb", "glb")
SCH_TARGETS = ("bom", "netlist")
DEFAULT_TARGETS = ["gerbers", "drill", "pos", "bom"]
KNOWN_TARGETS = (*PCB_TARGETS, *SCH_TARGETS)
#: targets whose kicad-cli subcommand takes `--layers`
LAYER_TARGETS = ("gerbers", "pdf", "svg", "dxf")
#: targets that plot copper zones (D22: refill in memory with --check-zones, else zones_stale)
ZONE_TARGETS = ("gerbers", "pdf", "svg", "dxf", "odb", "ipc2581")


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
    layers: list[str] | None = Field(
        default=None,
        description=(
            "only these layers for gerbers (and pdf/svg/dxf), e.g. ['F.Paste', 'F.Mask', 'In1.Cu']; KiCad names, "
            "standard names (F.Silkscreen) or the board's own layer names. Default: the profile's set / every layer"
        ),
    )
    dry_run: bool = Field(
        default=False,
        description=(
            "return the file plan without running KiCad or writing anything (gate reported, not enforced); "
            "with target pos: positions_preview = included / excluded refs with the reason + diff against the BOM"
        ),
    )
    out_dir: str | None = Field(
        default=None,
        description="absolute output directory, anywhere (outside the project allowed); default <project>/fab "
        "(read-only mode: a per-call directory under the storage home)",
    )
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
PROFILE_TARGETS = ("gerbers", "drill", "pos", "bom")
_COPPER_KINDS = ("signal", "power", "mixed", "jumper")


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


def _layer_is_copper(layer: Any, in_leading_block: bool) -> bool:
    """A layer record's copper flag (`is_copper` / `copper` on the file and IPC models); a record
    without one is copper only inside the leading block of copper-typed rows, the way KiCad's
    parser reads the `(layers …)` table (`kiwrap.formats.board.copper_layer_rows`)."""
    for attr in ("is_copper", "copper"):
        val = getattr(layer, attr, None)
        if isinstance(val, bool):
            return val
    return in_leading_block and getattr(layer, "kind", "") in _COPPER_KINDS


def _read_board(ctx: ToolContext, project: Project) -> Any | None:
    try:
        return ctx.services.file_read_board(project.board)
    except Exception:  # noqa: BLE001 - optional reader
        return None


def _copper_of(model: Any | None) -> list[str]:
    if model is None:
        return ["F.Cu", "B.Cu"]
    names: list[str] = []
    leading = True
    for layer in getattr(model, "layers", ()):
        if _layer_is_copper(layer, leading):
            names.append(str(layer.name))
        else:
            leading = False
    return names or ["F.Cu", "B.Cu"]


def copper_layers(ctx: ToolContext, project: Project) -> list[str]:
    """The board's copper layer names (F.Cu, In1.Cu, ..., B.Cu) through the file reader; the
    two-layer default when the reader is not wired or the board does not parse. A `User.N` row
    typed `signal` after the user rows is NOT copper (KiCad takes the leading block only), so it
    never becomes a copper gerber in the fab zip."""
    return _copper_of(_read_board(ctx, project))


def dnp_refs(ctx: ToolContext, project: Project) -> list[str]:
    """References of DNP footprints on the board (file reader; [] when unavailable)."""
    model = _read_board(ctx, project)
    return sorted(fp.reference for fp in getattr(model, "footprints", ()) if getattr(fp, "dnp", False))


def _schematic_parts(ctx: ToolContext, project: Project) -> dict[str, Any] | None:
    try:
        return schematic_parts(ctx.services.file_read_schematic(project.root_sch))
    except Exception:  # noqa: BLE001 - optional reader: the diff is left out, the export is not
        return None


def _flag_listed(caps: Any, subcommand: str, flag: str) -> bool:
    """`backends.cli.runner.has_flag` semantics: listed, or the record has no list (unknown)."""
    try:
        listed = list(caps.flags(subcommand))
    except Exception:  # noqa: BLE001 - a hand-built record
        listed = []
    return not listed or flag in listed


# ---- per-call context ---------------------------------------------------------------------------------------


@dataclass
class _Run:
    """What the target runners need; `m` is the `Mutation` or a `_Detached` stand-in."""

    ctx: ToolContext
    m: Any
    install: Any
    caps: Any
    config: Any
    project: Project
    profile: FabProfile
    args: FabExportArgs
    board: Any | None  # file-reader model (None when the reader is missing / the board does not parse)
    layers: list[str] | None  # canonical names of the caller's `layers` filter
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class TargetOptions:
    options: dict[str, Any]
    dropped: list[str]
    zones: str  # "refill" (--check-zones) | "stale" (no flag) | "n/a"


def target_options(
    caps: Any,
    profile: FabProfile,
    target: str,
    *,
    copper: list[str],
    layers: list[str] | None,
    include_dnp: bool | None,
) -> TargetOptions:
    """The kicad-cli options of one target: the profile's (vendor targets), the `layers` filter,
    and D22's `--check-zones` on copper plots where this KiCad lists the flag."""
    vendor = profile.name != "generic" and target in PROFILE_TARGETS
    opts: dict[str, Any] = {}
    dropped: list[str] = []
    if vendor and target != "bom":
        filtered = profile.options_for(target, caps, copper_layers=copper if target == "gerbers" else None)
        opts, dropped = dict(filtered.options), list(filtered.dropped)
    if target in LAYER_TARGETS and layers:
        opts["layers"] = list(layers)
    if target == "pos" and include_dnp is True:
        opts.pop("exclude_dnp", None)  # the caller wants DNP rows in the CPL
    zones = "n/a"
    if target in ZONE_TARGETS:
        if _flag_listed(caps, f"pcb export {target}", "--check-zones"):
            opts["check_zones"] = True
            zones = "refill"
        else:
            opts.pop("check_zones", None)
            dropped = [d for d in dropped if not d.startswith("check_zones")]
            zones = "stale"
    return TargetOptions(opts, dropped, zones)


def _vendor_skip_dnp(profile: FabProfile, args: FabExportArgs) -> bool:
    return profile.name != "generic" and (
        args.include_dnp is False or (args.include_dnp is None and profile.exclude_dnp)
    )


# ---- target runners -----------------------------------------------------------------------------------------


def _export_bom(r: _Run, out: Path) -> list[Path]:
    """The profile's BOM CSV through `kw_fab_bom`'s builder (kicad-cli bom export, else the netlist).
    The intermediate kicad-cli output goes to a per-call directory under the storage home (never
    into the project: D23)."""
    workdir = scratch_dir(r.ctx, r.project, "bom")
    try:
        table, degraded = bom_table(
            r.ctx,
            r.install,
            r.config,
            r.caps,
            r.project,
            profile=r.profile,
            extra_fields=list(r.args.bom_fields),
            workdir=workdir,
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    receipt = r.m.receipt
    for d in degraded:
        if d not in receipt.degraded:
            receipt.degraded.append(d)
    receipt.warnings.extend(table.warnings)
    rows = r.profile.bom_rows(table, include_dnp=r.args.include_dnp)
    path = out / (r.profile.file_name("bom", r.project.name) or f"{r.project.name}-bom.csv")
    path.write_text(
        r.profile.bom_csv(table, include_dnp=r.args.include_dnp, extra_fields=list(r.args.bom_fields)),
        encoding="utf-8",
    )
    missing = [ref for row in rows if not row.dnp and not row.lcsc for ref in row.refs]
    r.extra["bom"] = {
        "source": table.source,
        "groups": len(rows),
        "components": sum(row.qty for row in rows),
        "dnp_refs": [ref for row in table.dnp for ref in row.refs],
        "missing_lcsc": missing,
        "columns": [label for label, _ in r.profile.bom_columns] + list(r.args.bom_fields),
    }
    if missing:
        receipt.warnings.append(
            f"bom: {len(missing)} fitted part(s) without an LCSC Part # ({', '.join(missing[:12])}"
            + (", ..." if len(missing) > 12 else "")
            + ") - the vendor cannot assemble them"
        )
    return [path]


def _position_report(r: _Run, opts: dict[str, Any], cpl_refs: list[str], skip_dnp: bool) -> dict[str, Any]:
    """`excluded` / `flags` / `bom_diff` for the rows a pos export produced (review item 4)."""
    fps = list(getattr(r.board, "footprints", ()) or ())
    if not fps:
        return {"excluded": [], "flags": [], "bom_diff": None}
    plan = plan_positions(fps, PosOptions.from_options(opts, extra_dnp=skip_dnp))
    in_cpl = set(cpl_refs)
    excluded = [e.to_dict() for e in plan.excluded if e.ref not in in_cpl]
    known = {e["ref"] for e in excluded}
    for ref in (str(fp.reference) for fp in fps):
        if ref not in in_cpl and ref not in known:
            excluded.append({"ref": ref, "reason": "unknown", "reasons": ["unknown"], "why": "not modelled"})
            known.add(ref)
    parts = _schematic_parts(r.ctx, r.project)
    diff = bom_diff(cpl_refs, parts, fps, plan) if parts is not None else None
    flags = [f.to_dict() for f in plan.flags if f.ref in in_cpl]
    if flags:
        unspecified = [f["ref"] for f in flags if f["reason"] == "attr_unspecified"]
        if unspecified:
            r.m.receipt.warnings.append(
                f"pos: {len(unspecified)} part(s) have footprint type 'unspecified' ({', '.join(unspecified[:12])}"
                + (", ..." if len(unspecified) > 12 else "")
                + "): an assembler's SMD-only filter drops them - set the footprint type (SMD / through-hole)"
            )
    if diff is not None and not diff["match"]:
        r.m.receipt.warnings.append(
            f"pos: the CPL and the schematic BOM disagree ({len(diff['missing_from_cpl'])} missing, "
            f"{len(diff['extra_in_cpl'])} extra): see data.positions.bom_diff"
        )
    return {"excluded": excluded, "flags": flags, "bom_diff": diff}


def _export_pos(r: _Run, out: Path, produced: list[Path], opts: dict[str, Any], vendor: bool) -> list[Path]:
    """Vendor profile: post-process KiCad's pos CSV into the CPL with the rotation corrections
    applied. Both profiles: report the excluded refs with the reason and the BOM diff."""
    raw = next((p for p in produced if Path(p).suffix.lower() in (".csv", ".pos")), None)
    receipt = r.m.receipt
    if raw is None:
        receipt.warnings.append("pos: kicad-cli wrote no position file; nothing to report on")
        receipt.degraded.append("pos_uncorrected")
        return produced
    text = Path(raw).read_text(encoding="utf-8", errors="replace")
    try:
        pos = parse_pos(text, source=str(raw), units=str(opts.get("units", "mm")))
    except Exception as exc:  # noqa: BLE001 - the export itself succeeded; only the report is lost
        receipt.warnings.append(f"pos: {Path(raw).name} could not be parsed ({exc}); no exclusion report")
        receipt.degraded.append("pos_unparsed")
        return produced
    if not vendor:
        refs = [row.ref for row in pos.rows]
        r.extra["positions"] = {"rows": len(refs), "refs": refs, "file": Path(raw).as_posix()}
        r.extra["positions"].update(_position_report(r, opts, refs, skip_dnp=False))
        return produced
    # DNP parts: kicad-cli's --exclude-dnp did it when the flag exists; else (or when the caller
    # explicitly wants them out) drop them here from the board's dnp attributes
    skip_dnp = _vendor_skip_dnp(r.profile, r.args)
    skip = dnp_refs(r.ctx, r.project) if skip_dnp else []
    corrected = r.profile.process_positions(pos, skip_refs=skip)
    cpl = out / (r.profile.file_name("pos", r.project.name) or f"{r.project.name}-cpl.csv")
    cpl.write_text(r.profile.cpl_csv(corrected), encoding="utf-8")
    r.extra["positions"] = corrected.to_dict() | {"file": cpl.as_posix(), "kicad_file": Path(raw).as_posix()}
    r.extra["positions"].update(_position_report(r, opts, [row.ref for row in corrected.rows], skip_dnp))
    if corrected.corrections:
        receipt.warnings.append(
            f"pos: {len(corrected.corrections)} rotation correction(s) applied ({r.profile.name} table): "
            + ", ".join(
                f"{c.ref} {c.rotation_kicad:g}->{c.rotation_out:g}" for c in corrected.corrections[:10]
            )
            + (", ..." if len(corrected.corrections) > 10 else "")
            + "; check them in the vendor's order preview"
        )
    return [*produced, cpl]


def _note_zones(r: _Run, target: str, zones: str) -> None:
    if zones != "stale":
        return
    count = len(getattr(r.board, "zones", ()) or ()) if r.board is not None else None
    if count == 0:
        return
    receipt = r.m.receipt
    if "zones_stale" not in receipt.degraded:
        receipt.degraded.append("zones_stale")
    receipt.warnings.append(
        f"{target}: this KiCad cannot refill zones while plotting (no --check-zones): the "
        f"{'' if count is None else f'{count} '}zone fill(s) are plotted as saved in the board and may be stale - "
        "refill in KiCad (or kw_pcb_drc(save_board=true) on a matching KiCad) before ordering"
    )


def _export_target(r: _Run, target: str, out: Path) -> list[Path]:
    """Run one target. Generic profile: kicad-cli defaults (plus `layers` / `--check-zones`). A
    vendor profile: its options for gerbers / drill / pos / bom plus the post-processing."""
    vendor = r.profile.name != "generic" and target in PROFILE_TARGETS
    if target in SCH_TARGETS:
        if vendor:  # bom
            return _export_bom(r, out)
        ext = "csv" if target == "bom" else "net"
        return list(
            r.ctx.services.export_sch(
                r.install, r.config, r.project.root_sch, target, out / f"{r.project.name}.{ext}"
            )
        )
    topts = target_options(
        r.caps,
        r.profile,
        target,
        copper=_copper_of(r.board),
        layers=r.layers,
        include_dnp=r.args.include_dnp,
    )
    if topts.dropped:
        r.extra.setdefault("dropped_options", {})[target] = topts.dropped
        r.m.receipt.warnings.append(
            f"{target}: options this KiCad lacks were dropped: {', '.join(topts.dropped)}"
        )
    r.extra.setdefault("zones", {})[target] = topts.zones
    _note_zones(r, target, topts.zones)
    produced = list(
        r.ctx.services.export_pcb(r.install, r.config, r.project.board, target, out, **topts.options)
    )
    if target == "pos":
        return _export_pos(r, out, produced, topts.options, vendor)
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


# ---- the call -----------------------------------------------------------------------------------------------


def _layer_filter(
    r_board: Any | None, args: FabExportArgs, targets: list[str]
) -> tuple[list[str] | None, list[str]]:
    """Canonical names of `args.layers` (E_BAD_ARGS for a name the board does not have) and warnings."""
    if not args.layers:
        return None, []
    table = board_layer_names(r_board) if r_board is not None else []
    canon, unknown = normalise_layers(args.layers, table)
    if unknown:
        raise KiwrapError(
            "E_BAD_ARGS",
            f"unknown layer name(s) {', '.join(repr(u) for u in unknown)} for this board",
            why="kicad-cli --layers takes the board's layer names (untranslated)",
            fix="pick from the board's layers",
            candidates=[name for name, _ in table],
            example='kw_fab_export(project=..., targets=["gerbers"], layers=["F.Paste", "F.Mask"], dry_run=true)',
        )
    warnings: list[str] = []
    if not any(t in LAYER_TARGETS for t in targets):
        warnings.append(
            f"layers applies to {', '.join(LAYER_TARGETS)} only; ignored for {', '.join(targets)}"
        )
    return canon, warnings


def _run_exports(
    r: _Run, targets: list[str], base: Path, clean: bool, handle: JobHandle | None
) -> dict[str, Any]:
    files: dict[str, list[str]] = {}
    unsupported: dict[str, dict[str, Any]] = {}
    receipt = r.m.receipt
    for i, target in enumerate(targets):
        if handle is not None:
            handle.check()
            handle.progress(i / len(targets), f"exporting {target}")
        out = base / target
        _prepare_target_dir(out, clean=clean)
        try:
            produced = _export_target(r, target, out)
        except Exception as exc:  # noqa: BLE001 - one unsupported target must not lose the others
            err = to_kiwrap_error(exc)
            if err.code != "E_UNSUPPORTED":
                raise err from exc
            unsupported[target] = err.to_dict()
            receipt.degraded.append(f"{target}_unsupported")
            continue
        files[target] = [Path(p).as_posix() for p in produced]
        (out / EXPORT_MARKER).write_text(
            f"written by kiwrap kw_fab_export ({target}); deleted on the next export with clean=true\n",
            encoding="utf-8",
        )
    # DNP is a schematic flag: the CPL (from the board) only drops a DNP part once kw_pcb_sync
    # carried the flag over, so a BOM/CPL disagreement means the board is stale
    stale = set((r.extra.get("bom") or {}).get("dnp_refs", ())) & set(
        (r.extra.get("positions") or {}).get("refs", ())
    )
    if stale:
        receipt.degraded.append("cpl_has_dnp")
        receipt.warnings.append(
            f"pos: DNP part(s) {', '.join(sorted(stale))} are still in the CPL - the board does not carry "
            "the schematic's DNP flag yet: run kw_pcb_sync, then export again"
        )
    bundle_name = r.profile.bundle_name(r.project.name)
    if bundle_name and "gerbers" in files:
        if r.layers:
            receipt.warnings.append(
                f"bundle: not built - the layers filter ({', '.join(r.layers)}) makes the gerber set partial"
            )
        else:
            zpath = _bundle(base, bundle_name, files)
            if zpath is not None:
                files["bundle"] = [zpath.as_posix()]
                if "drill" not in files:
                    receipt.warnings.append(
                        "bundle: no drill files in the zip (target drill was not exported)"
                    )
    if handle is not None:
        handle.progress(1.0, "done")
    return {"files": files, "unsupported": unsupported}


def _finish_receipt(
    r: _Run, targets: list[str], base: Path, result: dict[str, Any], *, persisted: bool
) -> tuple[bool, bool]:
    files, unsupported = result["files"], result["unsupported"]
    receipt = r.m.receipt
    ok = bool(files)
    r.m.summary = (
        f"{','.join(files)} -> {base.as_posix()}"
        + (f" (unsupported: {','.join(unsupported)})" if unsupported else "")
        + (f" [{r.profile.name}]" if r.profile.name != "generic" else "")
    )
    receipt.provider_used = r.ctx.services.provider(r.install)
    receipt.changed["added"] = [f for lst in files.values() for f in lst]
    receipt.warnings.extend(f"{t}: {e['message']}" for t, e in unsupported.items())
    if r.m.forced_reason:
        receipt.warnings.append(f"gate overridden: {r.m.forced_reason}")
    receipt.next_suggested = "kw_fab_export" if (not ok or unsupported) else None
    receipt.data = {
        "ok": ok,
        "out_dir": base.as_posix(),
        "targets": targets,
        "files": files,
        "unsupported": unsupported,
        "profile": r.profile.name,
        "profile_notes": list(r.profile.notes),
        "forced": r.m.forced_reason,
        "layers": r.layers,
        "persisted": persisted,
        **r.extra,
    }
    return ok, bool(unsupported)


def _resolve_base(
    ctx: ToolContext, project: Project, args: FabExportArgs, readonly: bool
) -> tuple[Path, bool]:
    """(output base directory, clean default)."""
    out = resolve_out_dir(
        ctx, project, args.out_dir, "fab", readonly_call=readonly, default=project.dir / "fab"
    )
    clean = args.clean if args.clean is not None else (args.out_dir is None and not readonly)
    out.path.mkdir(parents=True, exist_ok=True)
    return out.path, clean


def export_sync(ctx: ToolContext, args: FabExportArgs, handle: JobHandle | None = None) -> Receipt:
    project = Project.from_path(args.project)
    targets = _check_targets(args.targets)
    try:
        profile = get_profile(args.profile)
    except ValueError as exc:
        raise KiwrapError(
            "E_BAD_ARGS", str(exc), fix="pick a known profile", candidates=list(PROFILE_NAMES)
        ) from exc
    readonly = is_readonly(ctx, args)
    if args.dry_run:
        return dry_run(ctx, args, project, targets, profile, readonly=readonly)
    # read-only (D23): `mutation()` runs detached - state in memory, no snapshot, outputs that
    # would land in the project redirected; the gate still applies (force to override)
    with mutation(ctx, project, "kw_fab_export", force=args.force, gate=True) as m:
        install = effective_target(ctx, project, m.state, for_write=False).install
        board = _read_board(ctx, project)
        layers, lw = _layer_filter(board, args, targets)
        m.receipt.warnings.extend(lw)
        r = _Run(
            ctx,
            m,
            install,
            ctx.services.caps(install),
            ctx.services.config_root(install),
            project,
            profile,
            args,
            board,
            layers,
        )
        base, clean = _resolve_base(ctx, project, args, readonly=readonly)
        result = _run_exports(r, targets, base, clean, handle)
        ok, partial = _finish_receipt(r, targets, base, result, persisted=bool(getattr(m, "persist", True)))
        if ok and not partial:
            record_stage(project, m.state, "exported")
        return m.receipt


# ---- dry run ------------------------------------------------------------------------------------------------


def _gate_report(state: ProjectState, args: FabExportArgs) -> dict[str, Any]:
    needed = GATES.get("kw_fab_export")
    try:
        forced = check_gate("kw_fab_export", state.stage, Force.parse(args.force))
        blocked = False
    except KiwrapError as exc:
        if exc.code != "E_GATE":
            raise
        forced, blocked = None, True
    return {"required_stage": needed, "stage": state.stage, "would_block": blocked, "forced": forced}


def _planned_files(
    target: str,
    stem: str,
    name: str,
    profile: FabProfile,
    opts: dict[str, Any],
    table: list[Any],
    layers: Any,
) -> list[str]:
    vendor = profile.name != "generic" and target in PROFILE_TARGETS
    if target == "gerbers":
        chosen = opts.get("layers") or [n for n, _ in table]
        return gerber_files(stem, chosen, table, protel=not opts.get("no_protel_ext"))
    if target == "drill":
        return drill_files(stem, opts)
    if target == "pos":
        kicad = pos_file(stem, opts)
        cpl = profile.file_name("pos", name) or f"{name}-cpl.csv"
        return [kicad, cpl] if vendor else [kicad]
    if target == "bom":
        return [(profile.file_name("bom", name) or f"{name}-bom.csv") if vendor else f"{name}.csv"]
    if target == "netlist":
        return [f"{name}.net"]
    if target == "stats":
        return [f"{stem}.json"]
    return [f"{stem}.{SINGLE_FILE_EXT.get(target, target)}"]


def dry_run(
    ctx: ToolContext,
    args: FabExportArgs,
    project: Project,
    targets: list[str],
    profile: FabProfile,
    *,
    readonly: bool,
) -> Receipt:
    """The file plan of the export without running KiCad or writing anything (review item 4).
    Reads the state and the design files only; the gate is reported, not enforced."""
    state = ro.load_state(project) if ro.current() is not None else project.load_state()
    check_external_edit(project, state)  # in memory only: a dry run writes nothing
    choice = effective_target(ctx, project, state, for_write=False)
    install = choice.install
    caps = ctx.services.caps(install)
    board = _read_board(ctx, project)
    layers, warnings = _layer_filter(board, args, targets)
    table = board_layer_names(board) if board is not None else []
    stem, name = project.board.stem, project.name
    if args.out_dir:
        base: str | None = Path(args.out_dir).expanduser().as_posix()
    elif readonly:
        base = None
    else:
        base = (project.dir / "fab").as_posix()
    receipt = Receipt(provider_used="plan", stage=state.stage)
    receipt.warnings.extend(warnings)
    plan: dict[str, Any] = {}
    stale_zones: list[str] = []
    positions_preview: dict[str, Any] | None = None
    for target in targets:
        if target in SCH_TARGETS:
            topts = TargetOptions({}, [], "n/a")
        else:
            topts = target_options(
                caps, profile, target, copper=_copper_of(board), layers=layers, include_dnp=args.include_dnp
            )
        entry: dict[str, Any] = {
            "dir": f"{base}/{target}" if base else None,
            "files": _planned_files(target, stem, name, profile, topts.options, table, layers),
            "predicted": True,
            "options": topts.options,
            "dropped_options": topts.dropped,
            "zones": topts.zones,
        }
        plan[target] = entry
        if topts.zones == "stale" and (board is None or bool(getattr(board, "zones", None))):
            stale_zones.append(target)
        if target == "pos":
            vendor = profile.name != "generic"
            skip = vendor and _vendor_skip_dnp(profile, args)
            fps = list(getattr(board, "footprints", ()) or ())
            pplan = plan_positions(fps, PosOptions.from_options(topts.options, extra_dnp=skip))
            positions_preview = pplan.to_dict()
            parts = _schematic_parts(ctx, project)
            positions_preview["bom_diff"] = (
                bom_diff(pplan.included, parts, fps, pplan) if parts is not None else None
            )
            if board is None:
                receipt.degraded.append("positions_preview_unavailable")
                receipt.warnings.append("pos: the board file could not be read; no positions preview")
    if stale_zones:
        receipt.degraded.append("zones_stale")
        receipt.warnings.append(
            f"{', '.join(stale_zones)}: this KiCad cannot refill zones while plotting; the saved fills would be used"
        )
    bundle = profile.bundle_name(name) if "gerbers" in targets and not layers else None
    receipt.data = {
        "ok": True,
        "dry_run": True,
        "out_dir": base,
        "out_dir_note": None if base else "a per-call directory under the storage home (read-only mode)",
        "targets": targets,
        "profile": profile.name,
        "layers": layers,
        "plan": plan,
        "bundle": f"{base}/{bundle}" if bundle and base else bundle,
        "gate": _gate_report(state, args),
        "target_kicad": getattr(install, "version", None),
        "persisted": False,
    }
    if positions_preview is not None:
        receipt.data["positions_preview"] = positions_preview
    receipt.next_suggested = "kw_fab_export"
    return receipt


def run(ctx: ToolContext, args: FabExportArgs) -> Receipt:
    if not args.run_async or args.dry_run:
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
        "Export fabrication outputs (gerbers, drill, pos, pdf, step, bom, ...) with kicad-cli into <project>/fab/<target>/ "
        "or any out_dir. layers=[...] filters the gerber set; dry_run=true returns the file plan (for pos: excluded refs "
        "with the reason + diff against the schematic BOM) without running KiCad. profile='jlcpcb' applies the vendor's "
        "options, CPL rotation corrections (reported), the LCSC BOM and a gerber+drill zip. Copper plots refill zones in "
        "memory where KiCad can (else degraded zones_stale). Gated on stage drc_clean unless force={'reason': ...}. "
        "run_async=true returns a job_id."
    ),
    input_model=FabExportArgs,
    handler=run,
    annotations=MUTATING,
)
