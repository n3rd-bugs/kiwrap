"""kw_sch_check and kw_pcb_drc: KiCad is the judge.

Both run kicad-cli through `kiwrap.backends.cli` (which already applies the verified quirks:
ERC JSON positions x`cli.erc_json_pos_scale`, docs/VERIFICATION.md claim 1) and turn the
normalised `Report` into JSON with millimetre positions. `ok` = zero errors; warnings are listed
and never gate (docs/ARCHITECTURE.md §11).

DRC classification (§11): must_fix = clearance, shorting_items, tracks_crossing,
unconnected_items, hole_clearance, net_conflict, track_width; review = courtyards_overlap,
solder_mask_bridge, lib_footprint_issues; cosmetic = silk_*. The tables live in the capability
record per version (`rules.drc_must_fix` / `rules.drc_review` / `rules.drc_cosmetic_prefix`,
seeded from kiwrap/data/kicad_version_matrix.json); the constants below are the §11 defaults used
when a record lacks them. Any other
error-severity violation is must_fix (an error KiCad refuses to plot is not something to ship),
any other warning is review.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from kiwrap.core.state import KiwrapError, Receipt
from kiwrap.core.state.project import Project, ProjectState
from kiwrap.core.state.stages import downgrade_below, record_stage
from kiwrap.core.tools._base import MUTATING, ToolContext, ToolSpec, mutation, to_kiwrap_error

DRC_MUST_FIX = (
    "clearance",
    "shorting_items",
    "tracks_crossing",
    "unconnected_items",
    "hole_clearance",
    "net_conflict",
    "track_width",
)
DRC_REVIEW = ("courtyards_overlap", "solder_mask_bridge", "lib_footprint_issues")
DRC_COSMETIC_PREFIX = "silk_"

VALIDATOR_ANNOTATIONS = MUTATING  # validators write state + a snapshot but never touch design files
MAX_VIOLATIONS = 500


def install_for(ctx: ToolContext, state: ProjectState) -> Any:
    return ctx.services.install(state.target_kicad)


def pos_dict(pos: Any) -> dict[str, float] | None:
    if pos is None:
        return None
    return {"x_mm": round(float(pos.x_mm), 4), "y_mm": round(float(pos.y_mm), 4)}


def violation_dict(v: Any) -> dict[str, Any]:
    return {
        "type": v.type,
        "severity": v.severity,
        "description": v.description,
        "sheet": getattr(v, "sheet", None),
        "excluded": bool(getattr(v, "excluded", False)),
        "pos": pos_dict(v.pos),
        "items": [{"uuid": i.uuid, "description": i.description, "pos": pos_dict(i.pos)} for i in v.items],
    }


def _table(caps: Any, key: str, default: tuple[str, ...]) -> tuple[str, ...]:
    try:
        val = caps[key]
    except (KeyError, TypeError):
        return default
    return tuple(val) if isinstance(val, (list, tuple)) else default


def _text(caps: Any, key: str, default: str) -> str:
    try:
        val = caps[key]
    except (KeyError, TypeError):
        return default
    return val if isinstance(val, str) and val else default


def classify_drc(v: Any, *, must_fix: tuple[str, ...], review: tuple[str, ...], cosmetic_prefix: str) -> str:
    if v.type.startswith(cosmetic_prefix):
        return "cosmetic"
    if v.type in review:
        return "review"
    if v.severity == "error":
        return "must_fix"
    return "review" if v.type in must_fix or v.severity == "warning" else "cosmetic"


# ---- kw_sch_check ---------------------------------------------------------------------------------


#: The kicad-cli `--severity-*` filters (`backends.cli.erc.severity_flags`); a Literal so the enum
#: lands in the published inputSchema and a typo is E_BAD_ARGS instead of a backend ValueError.
Severity = Literal["all", "error", "warning", "exclusions"]


class SchCheckArgs(BaseModel):
    project: str = Field(description="project directory or .kicad_pro path")
    severity: Severity = Field(
        default="all",
        description="ERC severity filter passed to kicad-cli (all = errors + warnings; exclusions = excluded markers)",
    )
    netlist: bool = Field(default=True, description="also export and summarise the netlist")


def netlist_summary(nl: Any) -> dict[str, Any]:
    comps = list(nl.components)
    nets = list(nl.nets)
    single = [n.name for n in nets if len(n.nodes) < 2]
    return {
        "source": nl.source,
        "kicad_version": nl.kicad_version,
        "components": len(comps),
        "nets": len(nets),
        "single_node_nets": single[:50],
        "missing_footprint": [c.ref for c in comps if not c.footprint][:100],
        "component_list": [
            {"ref": c.ref, "value": c.value, "footprint": c.footprint, "lib_id": c.lib_id}
            for c in comps[:200]
        ],
    }


def sch_check(ctx: ToolContext, args: SchCheckArgs) -> Receipt:
    project = Project.from_path(args.project)
    with mutation(ctx, project, "kw_sch_check", gate=False) as m:
        if not project.root_sch.exists():
            raise KiwrapError(
                "E_PROJECT_NOT_FOUND",
                f"root schematic {project.root_sch.name} is missing",
                fix="kw_project create writes it; if you renamed files, keep the .kicad_pro stem",
            )
        install = install_for(ctx, m.state)
        config = ctx.services.config_root(install)
        report = ctx.services.run_erc(install, config, project.root_sch, severity=args.severity, units="mm")
        violations = [violation_dict(v) for v in report.violations]
        errors, warnings = report.errors, report.warnings
        ok = report.ok
        data: dict[str, Any] = {
            "ok": ok,
            "kicad_version": report.kicad_version,
            "errors": errors,
            "warnings": warnings,
            "violations": violations[:MAX_VIOLATIONS],
            "violations_truncated": max(0, len(violations) - MAX_VIOLATIONS),
            "netlist": None,
        }
        receipt_warnings: list[str] = []
        if args.netlist:
            try:
                data["netlist"] = netlist_summary(
                    ctx.services.export_netlist(install, config, project.root_sch)
                )
            except Exception as exc:  # noqa: BLE001 - ERC is the primary result; a missing netlist only degrades
                err = to_kiwrap_error(exc)
                if err.code not in ("E_UNSUPPORTED", "E_KICAD_FAILED"):
                    raise err from exc
                receipt_warnings.append(f"netlist export skipped: {err.code}: {err.message}")
                m.receipt.degraded.append("netlist_unavailable")
        m.validator_delta("erc", errors, {"warnings": warnings})
        if ok:
            record_stage(project, m.state, "erc_clean")
        else:
            downgrade_below(m.state, "erc_clean")
        m.summary = f"{'ok' if ok else 'FAIL'} errors={errors} warnings={warnings}"
        m.receipt.provider_used = ctx.services.provider(install)
        m.receipt.warnings.extend(receipt_warnings)
        m.receipt.data = data
        if not ok:
            m.receipt.next_suggested = "kw_sch_check"
        return m.receipt


SCH_CHECK = ToolSpec(
    name="kw_sch_check",
    title="Schematic check",
    description=(
        "Run KiCad ERC on the root schematic (positions in mm) and export the netlist. ok means zero errors; "
        "warnings are listed but never gate. Passing moves the project to stage erc_clean."
    ),
    input_model=SchCheckArgs,
    handler=sch_check,
    annotations=VALIDATOR_ANNOTATIONS,
)


# ---- kw_pcb_drc -----------------------------------------------------------------------------------


class PcbDrcArgs(BaseModel):
    project: str = Field(description="project directory or .kicad_pro path")
    schematic_parity: bool = Field(
        default=False, description="also check footprint/net parity against the schematic"
    )
    refill_zones: bool = Field(
        default=False,
        description="refill copper zones before checking (needs the cli.drc_refill_flags capability; else E_UNSUPPORTED)",
    )
    save_board: bool = Field(
        default=False, description="with refill_zones: write the refilled zones back to the board"
    )
    severity: Severity = Field(
        default="all",
        description="DRC severity filter passed to kicad-cli (all = errors + warnings; exclusions = excluded markers)",
    )


def pcb_drc(ctx: ToolContext, args: PcbDrcArgs) -> Receipt:
    project = Project.from_path(args.project)
    with mutation(ctx, project, "kw_pcb_drc", gate=False) as m:
        if not project.board.exists():
            raise KiwrapError(
                "E_PROJECT_NOT_FOUND",
                f"board {project.board.name} is missing",
                fix="kw_project create writes it",
            )
        install = install_for(ctx, m.state)
        caps = ctx.services.caps(install)
        config = ctx.services.config_root(install)
        report = ctx.services.run_drc(
            install,
            config,
            project.board,
            schematic_parity=args.schematic_parity,
            refill_zones=args.refill_zones,
            save_board=args.save_board and args.refill_zones,
            severity=args.severity,
        )
        must_fix_t = _table(caps, "rules.drc_must_fix", DRC_MUST_FIX)
        review_t = _table(caps, "rules.drc_review", DRC_REVIEW)
        cosmetic_prefix = _text(caps, "rules.drc_cosmetic_prefix", DRC_COSMETIC_PREFIX)
        buckets: dict[str, list[dict[str, Any]]] = {"must_fix": [], "review": [], "cosmetic": []}
        all_v = [*report.violations, *report.unconnected, *report.schematic_parity]
        for v in all_v:
            if getattr(v, "excluded", False):
                continue
            d = violation_dict(v)
            d["class"] = classify_drc(
                v, must_fix=must_fix_t, review=review_t, cosmetic_prefix=cosmetic_prefix
            )
            buckets[d["class"]].append(d)
        must_fix = len(buckets["must_fix"])
        ok = must_fix == 0
        data: dict[str, Any] = {
            "ok": ok,
            "kicad_version": report.kicad_version,
            "errors": report.errors,
            "warnings": report.warnings,
            "must_fix": must_fix,
            "review": len(buckets["review"]),
            "cosmetic": len(buckets["cosmetic"]),
            "unconnected": len([v for v in report.unconnected if not getattr(v, "excluded", False)]),
            "parity_violations": len(report.schematic_parity),
            "violations": [*buckets["must_fix"], *buckets["review"], *buckets["cosmetic"]][:MAX_VIOLATIONS],
            "violations_truncated": max(0, len(all_v) - MAX_VIOLATIONS),
            "options": {
                "schematic_parity": args.schematic_parity,
                "refill_zones": args.refill_zones,
                "save_board": args.save_board,
            },
        }
        m.validator_delta(
            "drc",
            must_fix,
            {"review": data["review"], "cosmetic": data["cosmetic"], "unconnected": data["unconnected"]},
        )
        if args.refill_zones and args.save_board:
            m.receipt.changed["updated"].append(project.board.name)
        if ok:
            record_stage(project, m.state, "drc_clean")
        else:
            downgrade_below(m.state, "drc_clean")
        m.summary = f"{'ok' if ok else 'FAIL'} must_fix={must_fix} review={data['review']} cosmetic={data['cosmetic']}"
        m.receipt.provider_used = ctx.services.provider(install)
        m.receipt.data = data
        if not ok:
            m.receipt.next_suggested = "kw_pcb_drc"
        return m.receipt


PCB_DRC = ToolSpec(
    name="kw_pcb_drc",
    title="Board DRC",
    description=(
        "Run KiCad DRC on the board (optionally schematic parity and zone refill). Violations are classified "
        "must_fix / review / cosmetic with mm positions; ok means must_fix == 0 and moves the project to drc_clean."
    ),
    input_model=PcbDrcArgs,
    handler=pcb_drc,
    annotations=VALIDATOR_ANNOTATIONS,
)
