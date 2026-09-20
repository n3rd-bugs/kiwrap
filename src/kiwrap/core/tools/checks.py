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
from kiwrap.core.state.stages import (
    STAGE_INPUTS,
    STAGES,
    downgrade_below,
    record_stage,
    sha256_file,
    stage_index,
)
from kiwrap.core.tools._base import MUTATING, ToolContext, ToolSpec, mutation, to_kiwrap_error
from kiwrap.core.tools.intent import attach_intent_diff

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


def board_has_copper_zones(ctx: ToolContext, project: Project) -> bool:
    """True when the board has a copper zone (a net, not a rule area) - then a DRC is only
    meaningful against a fresh fill (reviewer finding, 2026-09-20). Unknown counts as False."""
    try:
        model = ctx.services.file_read_board(project.board)
    except KiwrapError as err:
        if err.code == "E_UNSUPPORTED":
            return False
        raise
    for zone in getattr(model, "zones", None) or []:
        if getattr(zone, "is_rule_area", False) or getattr(zone, "keepout", False):
            continue
        net = getattr(zone, "net", None)
        name = net if isinstance(net, str) else getattr(net, "name", "")
        if name:
            return True
    return False


def involves_zone(violation: dict[str, Any]) -> bool:
    return any(str(i.get("description") or "").startswith("Zone") for i in violation.get("items", []))


def rehash_board_stages_upto(project: Project, state: ProjectState, keep: str) -> None:
    """Re-record the input hashes of every board-derived stage up to `keep` after kiwrap itself
    (here: kicad-cli's `--refill-zones --save-board`) rewrote the board, so `check_external_edit`
    keeps the stage. Same rule as `kiwrap.core.tools._author.rehash_board_stages`, which cannot
    be imported here (that module imports this one)."""
    for stage in STAGES[: stage_index(keep) + 1]:
        if stage in state.hashes and "pcb" in STAGE_INPUTS[stage]:
            record_stage(project, state, stage, advance=False)


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


def drc_type_limit(vtype: str, caps: Any) -> int:
    """KiCad's per-type reporting limit for a DRC violation type (capability keys
    `cli.drc_error_limit` / `cli.drc_extended_error_limit` / `cli.drc_extended_error_types`;
    DRC_ENGINE ERROR_LIMIT 199 / EXTENDED_ERROR_LIMIT 499 when the record has none)."""
    extended = _table(caps, "cli.drc_extended_error_types", ("clearance", "unconnected_items"))
    key = "cli.drc_extended_error_limit" if vtype in extended else "cli.drc_error_limit"
    default = 499 if vtype in extended else 199
    try:
        val = caps[key]
    except (KeyError, TypeError):
        return default
    return int(val) if isinstance(val, (int, float)) and val > 0 else default


def drc_capped_types(report: Any, caps: Any) -> list[str]:
    """Violation types whose count reached KiCad's reporting limit (a lower bound, not a total).
    Prefers what the backend recorded (`report.raw['_cli']['capped']`, computed by
    `kiwrap.backends.cli.drc.capped_types`); recomputed here for reports without it."""
    raw = getattr(report, "raw", None)
    if isinstance(raw, dict) and isinstance(raw.get("_cli"), dict) and "capped" in raw["_cli"]:
        return sorted(str(t) for t in raw["_cli"]["capped"])
    counts: dict[str, int] = {}
    for v in (
        *getattr(report, "violations", ()),
        *getattr(report, "unconnected", ()),
        *getattr(report, "schematic_parity", ()),
    ):
        counts[v.type] = counts.get(v.type, 0) + 1
    return sorted(t for t, n in counts.items() if n >= drc_type_limit(t, caps))


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


# ---- what to run after a failing validator ---------------------------------------------------------

#: ERC violation type -> the schematic tool that fixes it (first match in this order wins)
ERC_FIX_TOOLS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("power_pin_not_driven",), "kw_sch_add_item"),  # a pwr_flag item on the undriven net
    (
        (
            "pin_not_connected",
            "unconnected_wire_endpoint",
            "wire_dangling",
            "label_dangling",
            "isolated_pin_label",
            "pin_not_driven",
        ),
        "kw_sch_connect",
    ),
    (("no_connect_connected", "no_connect_dangling"), "kw_sch_edit"),
    (
        (
            "unannotated",
            "duplicate_reference",
            "extra_units",
            "different_unit_footprint",
            "different_unit_net",
        ),
        "kw_sch_annotate",
    ),
    (
        ("lib_symbol_issues", "lib_symbol_mismatch", "footprint_link_issues", "missing_footprint"),
        "kw_sch_assign_footprints",
    ),
)
#: DRC violation type -> the board tool that fixes it
DRC_FIX_TOOLS: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        (
            "schematic_parity",
            "footprint",
            "net_conflict",
            "missing_footprint",
            "extra_footprint",
            "footprint_type_mismatch",
        ),
        "kw_pcb_sync",
    ),
    (
        (
            "courtyards_overlap",
            "copper_edge_clearance",
            "footprints_overlap",
            "silk_overlap",
            "silk_over_copper",
        ),
        "kw_pcb_place",
    ),
    (
        (
            "unconnected_items",
            "clearance",
            "shorting_items",
            "tracks_crossing",
            "track_width",
            "via_dangling",
            "track_dangling",
            "hole_clearance",
            "via_hole_larger_than_pad",
            "drill_out_of_range",
            "via_diameter",
            "annular_width",
        ),
        "kw_pcb_route",
    ),
    (("starved_thermal", "isolated_copper", "zone_has_empty_net", "copper_sliver"), "kw_pcb_zones"),
)


def fix_tool_for(types: list[str], table: tuple[tuple[tuple[str, ...], str], ...], fallback: str) -> str:
    """The tool that fixes the first listed violation type (a failing validator must never suggest
    itself: an agent following `next_suggested` would loop on the check - reviewer finding,
    2026-09-20)."""
    seen = {str(t) for t in types}
    for names, tool in table:
        if seen & set(names):
            return tool
    return fallback


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
            "intent_diff": None,
        }
        receipt_warnings: list[str] = []
        if args.netlist:
            try:
                nl = ctx.services.export_netlist(install, config, project.root_sch)
                data["netlist"] = netlist_summary(nl)
            except Exception as exc:  # noqa: BLE001 - ERC is the primary result; a missing netlist only degrades
                err = to_kiwrap_error(exc)
                if err.code not in ("E_UNSUPPORTED", "E_KICAD_FAILED"):
                    raise err from exc
                receipt_warnings.append(f"netlist export skipped: {err.code}: {err.message}")
                m.receipt.degraded.append("netlist_unavailable")
            else:
                # the intent diff (kiwrap.core.tools.intent): validators.intent_diff + data.intent_diff
                # when the project declared nets with kw_intent; nothing otherwise
                data["intent_diff"] = attach_intent_diff(project, m, nl)
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
            error_types = [v["type"] for v in violations if v["severity"] == "error"]
            m.receipt.next_suggested = fix_tool_for(error_types, ERC_FIX_TOOLS, "kw_sch_connect")
        elif (data.get("intent_diff") or {}).get("count"):
            # ERC clean but the wiring differs from the declared nets: fix the wires before syncing
            m.receipt.next_suggested = "kw_sch_connect"
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
    refill_zones: bool | None = Field(
        default=None,
        description=(
            "refill copper zones before checking (needs the cli.drc_refill_flags capability; else E_UNSUPPORTED). "
            "Default (null): refill when the board has copper zones and this KiCad can - a DRC against a stale "
            "fill reports tracks added since the last fill as clearance errors against the pour"
        ),
    )
    save_board: bool | None = Field(
        default=None,
        description="with refill_zones: write the refilled zones back to the board (default: yes when refilling by default)",
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
        refill = args.refill_zones
        save = args.save_board
        stale_zones = False
        if refill is None:
            zones = board_has_copper_zones(ctx, project)
            refill = zones and caps.has("cli.drc_refill_flags")
            if zones and not refill:
                stale_zones = True
                m.receipt.degraded.append("zones_stale")
                m.receipt.warnings.append(
                    "the board has copper zones but this KiCad cannot refill them headless (cli.drc_refill_flags): "
                    "the check runs against the fill on disk; track-vs-zone clearance items are listed as review"
                )
            if save is None:
                save = refill
        if save and m.bound is not None and args.save_board is None:
            # a live GUI (kw_live) holds this board: kicad-cli must not rewrite the file under it
            # (the editor would not reload; its next save would undo the refill) - check only
            save = False
            m.receipt.warnings.append(
                "the refilled zones were not written back: a live KiCad session (kw_live) holds the board; "
                "pass save_board=true to force it, or refill in the GUI"
            )
        save = bool(save) and bool(refill)
        board_sha_before = sha256_file(project.board)
        report = ctx.services.run_drc(
            install,
            config,
            project.board,
            schematic_parity=args.schematic_parity,
            refill_zones=bool(refill),
            save_board=save,
            severity=args.severity,
        )
        if sha256_file(project.board) != board_sha_before:
            # kicad-cli rewrote the board (refill_zones + save_board): re-hash the board-derived
            # stages up to the current one so the next call does not take kiwrap's own save for
            # an external edit and drop the project back to erc_clean (kw_pcb_author does the
            # same after its refills; reviewer finding, 2026-09-20).
            rehash_board_stages_upto(project, m.state, m.state.stage)
            m.receipt.changed["updated"].append(project.board.name)
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
            if stale_zones and d["class"] == "must_fix" and v.type == "clearance" and involves_zone(d):
                d["class"] = "review"
                d["note"] = "against a stale zone fill; re-check with refill_zones=true on a KiCad 10+"
            buckets[d["class"]].append(d)
        must_fix = len(buckets["must_fix"])
        ok = must_fix == 0
        capped = drc_capped_types(report, caps)
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
            "capped": capped,
            "violations": [*buckets["must_fix"], *buckets["review"], *buckets["cosmetic"]][:MAX_VIOLATIONS],
            "violations_truncated": max(0, len(all_v) - MAX_VIOLATIONS),
            "options": {
                "schematic_parity": args.schematic_parity,
                "refill_zones": bool(refill),
                "save_board": save,
            },
        }
        m.validator_delta(
            "drc",
            must_fix,
            {"review": data["review"], "cosmetic": data["cosmetic"], "unconnected": data["unconnected"]},
        )
        m.history_extra["board_sha256"] = sha256_file(project.board)  # kw_pcb_check: is this DRC fresh?
        if capped:
            m.history_extra["drc_capped"] = list(capped)
            m.receipt.degraded.append("drc_counts_capped")
            limits = ", ".join(f"{t} after {drc_type_limit(t, caps)}" for t in capped)
            m.receipt.warnings.append(
                f"KiCad stops reporting a violation type once its limit is reached ({limits}): the counts for "
                f"{', '.join(capped)} are LOWER BOUNDS. Fix the listed items and re-run kw_pcb_drc"
                + (
                    "; for the complete unrouted count use kw_pcb_check (its connectivity providers are uncapped)"
                    if "unconnected_items" in capped
                    else ""
                )
            )
        if ok:
            record_stage(project, m.state, "drc_clean")
        else:
            downgrade_below(m.state, "drc_clean")
        m.summary = f"{'ok' if ok else 'FAIL'} must_fix={must_fix} review={data['review']} cosmetic={data['cosmetic']}"
        m.receipt.provider_used = ctx.services.provider(install)
        m.receipt.data = data
        if not ok:
            must_types = [v["type"] for v in buckets["must_fix"]]
            if any(not getattr(x, "excluded", False) for x in report.schematic_parity):
                must_types = ["schematic_parity", *must_types]
            m.receipt.next_suggested = fix_tool_for(must_types, DRC_FIX_TOOLS, "kw_pcb_route")
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
