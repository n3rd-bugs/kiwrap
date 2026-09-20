"""kw_fab_bom: the bill of materials as rows (grouped by value + footprint + vendor part + DNP),
with the `LCSC Part #` field the JLCPCB profile needs and cost / stock hooks left `None` for
the parts component (docs/DECISIONS.md D9; docs/ARCHITECTURE.md §10 `kw_fab_bom`).

Source, in order: kicad-cli's own `sch export bom --fields ... --group-by ...` (KiCad 8+: KiCad
resolves fields, variants and text variables itself) and, where the subcommand does not exist,
the kicadsexpr netlist kicad-cli exports on every version, grouped by `kiwrap.core.fab.bom`. Both
give the same rows. Symbols the schematic excludes from the BOM (`in_bom no`) are dropped by
KiCad's exporter on the CLI path and, on the netlist path, by reading the schematic files
(`file_read_schematic`) when that reader is wired.

Not gated (a BOM is useful long before DRC is clean) and never a mutation: with `write=true`
the profile's CSV lands in `<project>/fab/bom/` (never snapshotted), and the intermediate
kicad-cli output goes to `<project>/.kiwrap/tmp/`.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from kiwrap.core.fab import BomTable, FabProfile, bom_from_cli_csv, bom_from_netlist, get_profile
from kiwrap.core.fab.profiles import PROFILE_NAMES
from kiwrap.core.state import KiwrapError, Receipt
from kiwrap.core.state.project import Project
from kiwrap.core.tools._base import ToolContext, ToolSpec, read_state, to_kiwrap_error


class FabBomArgs(BaseModel):
    project: str = Field(description="project directory or .kicad_pro path")
    profile: str = Field(
        default="generic",
        description=f"fabrication profile ({' | '.join(PROFILE_NAMES)}): columns, vendor part field, DNP handling",
    )
    include_dnp: bool | None = Field(
        default=None,
        description="keep DNP parts in the rows / CSV (default: the profile's choice - jlcpcb drops them, generic keeps them flagged)",
    )
    fields: list[str] = Field(
        default_factory=list,
        description="extra symbol fields to carry as columns (e.g. ['Datasheet', 'Tolerance'])",
    )
    write: bool = Field(default=False, description="also write the profile's CSV to <project>/fab/bom/")


def has_subcommand(caps: Any, sub: str) -> bool:
    """True when the capability record lists `sub` in cli.subcommands, or has no list at all."""
    try:
        listed = caps.get("cli.subcommands")
    except Exception:  # noqa: BLE001 - a hand-built record
        listed = None
    return True if not listed else sub in listed


def excluded_from_bom(ctx: ToolContext, project: Project) -> list[str]:
    """References of symbols the schematic marks `in_bom no` (per instance), through the file
    reader; [] when it is not wired or the schematic does not parse."""
    try:
        hier = ctx.services.file_read_schematic(project.root_sch)
    except Exception:  # noqa: BLE001 - optional reader: the BOM is still right for every other part
        return []
    refs: set[str] = set()
    try:
        for inst, sym in hier.symbols():
            if getattr(sym, "in_bom", True):
                continue
            by_path = {i.path: i.reference for i in getattr(sym, "instances", ())}
            ref = by_path.get(getattr(inst, "path", "")) or getattr(sym, "reference", "")
            if ref:
                refs.add(ref)
    except Exception:  # noqa: BLE001
        return []
    return sorted(refs)


def bom_table(
    ctx: ToolContext,
    install: Any,
    config: Any,
    caps: Any,
    project: Project,
    *,
    profile: FabProfile,
    extra_fields: list[str],
    workdir: Path,
) -> tuple[BomTable, list[str]]:
    """The BOM through kicad-cli's exporter when the KiCad has it, else from the netlist.
    Returns (table, degraded) where degraded names the fallback taken."""
    workdir.mkdir(parents=True, exist_ok=True)
    degraded: list[str] = []
    if has_subcommand(caps, "sch export bom"):
        opts = profile.options_for("bom", caps, extra_fields=extra_fields)
        out = workdir / f"{project.name}-bom-kicad.csv"
        try:
            produced = ctx.services.export_sch(install, config, project.root_sch, "bom", out, **opts.options)
            text = Path(produced[0]).read_text(encoding="utf-8", errors="replace")
            table = bom_from_cli_csv(
                text,
                lcsc_fields=profile.lcsc_fields,
                extra_fields=extra_fields,
                kicad_version=getattr(install, "version", ""),
            )
            table.warnings.extend(f"option dropped on this KiCad: {d}" for d in opts.dropped)
            return table, degraded
        except Exception as exc:  # noqa: BLE001 - fall back to the netlist on a missing/failed exporter
            err = to_kiwrap_error(exc)
            if err.code not in ("E_UNSUPPORTED", "E_KICAD_FAILED"):
                raise err from exc
            degraded.append("bom_from_netlist")
            note = f"kicad-cli bom export unavailable ({err.code}): rows grouped from the netlist"
    else:
        degraded.append("bom_from_netlist")
        note = "this KiCad has no `sch export bom`: rows grouped from the netlist"
    nl = ctx.services.export_netlist(install, config, project.root_sch, workdir / f"{project.name}-bom.net")
    table = bom_from_netlist(
        nl,
        lcsc_fields=profile.lcsc_fields,
        extra_fields=extra_fields,
        exclude_refs=excluded_from_bom(ctx, project),
    )
    table.warnings.append(note)
    return table, degraded


def run(ctx: ToolContext, args: FabBomArgs) -> Receipt:
    project = Project.from_path(args.project)
    profile = get_profile(args.profile)  # ValueError -> E_BAD_ARGS
    if not project.root_sch.exists():
        raise KiwrapError(
            "E_PROJECT_NOT_FOUND",
            f"root schematic {project.root_sch.name} is missing",
            fix="kw_project create writes it; if you renamed files, keep the .kicad_pro stem",
        )
    state, external = read_state(project)
    install = ctx.services.install(state.target_kicad)
    caps = ctx.services.caps(install)
    config = ctx.services.config_root(install)
    workdir = project.state_dir / "tmp" / "bom"
    table, degraded = bom_table(
        ctx, install, config, caps, project, profile=profile, extra_fields=list(args.fields), workdir=workdir
    )
    rows = profile.bom_rows(table, include_dnp=args.include_dnp)
    receipt = Receipt(provider_used=ctx.services.provider(install), stage=state.stage)
    receipt.degraded.extend(degraded)
    if external.external_edit:
        receipt.degraded.append("external_edit")
    receipt.warnings.extend(table.warnings)
    missing = [ref for r in rows if not r.dnp and not r.lcsc for ref in r.refs]
    if missing and profile.name == "jlcpcb":
        receipt.warnings.append(
            f"{len(missing)} fitted part(s) have no LCSC Part # field ({', '.join(missing[:12])}"
            + (", ..." if len(missing) > 12 else "")
            + "): JLCPCB cannot assemble them - set the field with kw_sch_edit or kw_sch_add_symbol(fields=...)"
        )
    file: str | None = None
    if args.write:
        out_dir = project.dir / "fab" / "bom"
        out_dir.mkdir(parents=True, exist_ok=True)
        name = profile.file_name("bom", project.name) or f"{project.name}-bom.csv"
        path = out_dir / name
        path.write_text(
            profile.bom_csv(table, include_dnp=args.include_dnp, extra_fields=list(args.fields)),
            encoding="utf-8",
        )
        file = path.as_posix()
        receipt.changed["added"].append(file)
    shutil.rmtree(workdir, ignore_errors=True)
    receipt.data = {
        "ok": True,
        "profile": profile.name,
        "source": table.source,
        "kicad_version": table.kicad_version,
        "rows": [r.to_dict() for r in rows],
        "groups": len(rows),
        "components": sum(r.qty for r in rows),
        "dnp_refs": [ref for r in table.dnp for ref in r.refs],
        "dnp_included": any(r.dnp for r in rows),
        "missing_lcsc": missing,
        "columns": [label for label, _ in profile.bom_columns] + list(args.fields),
        "file": file,
    }
    receipt.next_suggested = "kw_fab_export"
    return receipt


SPEC = ToolSpec(
    name="kw_fab_bom",
    title="Bill of materials",
    description=(
        "BOM rows grouped by value + footprint + vendor part (+ DNP) from kicad-cli's own bom export (else the "
        "netlist): refs, qty, value, footprint, lcsc ('LCSC Part #' and aliases), dnp, mpn, manufacturer, extra "
        "fields; cost / stock are hooks (null). profile='jlcpcb' drops DNP parts and flags missing LCSC numbers; "
        "write=true also writes the profile's CSV to <project>/fab/bom/."
    ),
    input_model=FabBomArgs,
    handler=run,
    # it may write <project>/fab/bom/<name>.csv (write=true), so not read-only; never destructive,
    # idempotent, no design file and no snapshot touched
    annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
    ),
)
