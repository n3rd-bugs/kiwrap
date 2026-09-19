"""kw_sch_stats: schematic health as numbers the agent can act on - component and net counts,
dangling wire ends, unconnected pins, label-only nets (a label with no pin on its net) and
per-sheet counts. It is the structured "look" after every schematic edit, before the slower
kw_sch_check (ERC by KiCad).

Providers: the schematic model comes from `schematic.read` (ipc11 -> file); the netlist from the
`netlist` operation (ipc11 -> cli -> file); dangling ends / unconnected pins / label-only nets
come from `kiwrap.formats.analysis.pin_connectivity` on the FILE model (kiwrap's own geometry
walk - the reader contract: `dangling_ends[] {sheet, x_mm, y_mm, uuid}`, `unconnected_pins[]
{ref, pin, sheet, x_mm, y_mm}`, `label_only_nets[] str`). When that analysis is missing the
counts are still returned and `degraded` says `connectivity_unavailable`.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from kiwrap.core import planner
from kiwrap.core.state import KiwrapError, Receipt
from kiwrap.core.state.project import Project
from kiwrap.core.state.receipts import ReceiptOut
from kiwrap.core.tools._base import READ_ONLY, ToolContext, ToolSpec, read_state
from kiwrap.core.tools._data import external_tags, mm, mm_pair, pick, rec, ref_key
from kiwrap.core.tools.pcb_get import ProviderArg
from kiwrap.core.tools.sch_get import read_netlist, read_schematic, schematic_items

MAX_LISTED = 200


def _xy(r: dict[str, Any]) -> tuple[float | None, float | None]:
    pos = mm_pair(pick(r, "position", "at"))
    if pos is not None:
        return pos[0], pos[1]
    return mm(pick(r, "x_mm", "x")), mm(pick(r, "y_mm", "y"))


def norm_connectivity(c: Any) -> dict[str, Any]:
    r = rec(c)
    dangling = []
    for d in pick(r, "dangling_ends", "dangling_wire_ends", "dangling", default=[]) or []:
        dr = rec(d)
        x, y = _xy(dr)
        dangling.append(
            {
                "sheet": pick(dr, "sheet", "names_path", "sheet_path", "path", default="/") or "/",
                "x_mm": x,
                "y_mm": y,
                "uuid": pick(dr, "uuid", "wire_uuid", "id") or None,
            }
        )
    dangling.sort(key=lambda d: (d["sheet"], d["x_mm"] or 0.0, d["y_mm"] or 0.0, d["uuid"] or ""))
    pins = []
    for p in pick(r, "unconnected_pins", default=[]) or []:
        pr = rec(p)
        raw_pin = pick(pr, "pin", "number")
        number = raw_pin if isinstance(raw_pin, (str, int)) else pick(rec(raw_pin), "number", default="")
        x, y = _xy(pr)
        pins.append(
            {
                "ref": pick(pr, "ref", "reference"),
                "pin": str(number if number is not None else ""),
                "name": pick(pr, "name"),
                "type": pick(pr, "type", "electrical_type"),
                "sheet": pick(pr, "sheet", "names_path", "sheet_path", default="/"),
                "x_mm": x,
                "y_mm": y,
            }
        )
    pins.sort(key=lambda p: (p["sheet"], ref_key(p["ref"]), ref_key(p["pin"])))
    label_only = sorted(
        str(n) if isinstance(n, str) else str(pick(rec(n), "name", default=n))
        for n in (pick(r, "label_only_nets", default=[]) or [])
    )
    return {"dangling_ends": dangling, "unconnected_pins": pins, "label_only_nets": label_only}


class SchStatsArgs(BaseModel):
    project: str = Field(description="project directory or .kicad_pro path")
    provider: ProviderArg = Field(default="auto", description="schematic provider: auto | ipc11 | file")
    list_limit: int = Field(
        default=50,
        ge=0,
        le=MAX_LISTED,
        description="how many dangling ends / unconnected pins / nets to list",
    )


class SchStatsData(BaseModel):
    ok: bool = Field(
        description="no dangling ends, no unconnected pins and no label-only nets (when analysed)"
    )
    schematic: str
    components: int
    power_symbols: int
    symbols_total: int
    sheets: int
    nets: int | None = None
    single_node_nets: list[str] = Field(default_factory=list)
    dangling_ends: int | None = None
    unconnected_pins: int | None = None
    label_only_nets: int | None = None
    missing_footprint: list[str] = Field(default_factory=list)
    per_sheet: dict[str, dict[str, int]]
    lists: dict[str, list[Any]]
    providers: dict[str, str]
    plan: dict[str, Any]


class SchStatsOut(ReceiptOut):
    data: SchStatsData  # type: ignore[assignment]


def sch_stats(ctx: ToolContext, args: SchStatsArgs) -> Receipt:
    project = Project.from_path(args.project)
    state, external = read_state(project)
    if not project.root_sch.exists():
        raise KiwrapError(
            "E_PROJECT_NOT_FOUND",
            f"root schematic {project.root_sch.name} is missing",
            fix="kw_project create writes it",
        )
    install = ctx.services.install(state.target_kicad)
    caps = ctx.services.caps(install)
    config = ctx.services.config_root(install)
    prefer = None if args.provider == "auto" else args.provider

    outcome = read_schematic(ctx, install, caps, config, project.root_sch, prefer=prefer)
    lists = schematic_items(outcome.value)
    degraded, warnings = external_tags(external) + list(outcome.degraded), list(outcome.warnings)
    providers = {"schematic": outcome.provider_used(install)}

    symbols = lists["symbols"]
    comps = [s for s in symbols if not s["power"]]
    per_sheet: dict[str, dict[str, int]] = {}
    for kind, entries in lists.items():
        for it in entries:
            sheet = str(it.get("sheet") or "/")
            per_sheet.setdefault(sheet, {})
            per_sheet[sheet][kind] = per_sheet[sheet].get(kind, 0) + 1
    per_sheet = {k: dict(sorted(v.items())) for k, v in sorted(per_sheet.items())}

    data: dict[str, Any] = {
        "ok": True,
        "schematic": project.root_sch.name,
        "components": len({(c["ref"]) for c in comps}),
        "power_symbols": len(symbols) - len(comps),
        "symbols_total": len(symbols),
        "sheets": len(lists["sheets"]),
        "nets": None,
        "single_node_nets": [],
        "dangling_ends": None,
        "unconnected_pins": None,
        "label_only_nets": None,
        "missing_footprint": sorted(
            {c["ref"] for c in comps if not c["footprint"] and c["ref"]}, key=ref_key
        )[: args.list_limit],
        "per_sheet": per_sheet,
        "lists": {},
        "providers": providers,
        "plan": outcome.plan.to_dict(),
    }

    try:
        nl = read_netlist(
            ctx,
            install,
            caps,
            config,
            project.root_sch,
            ipc_model=outcome.value if outcome.provider_name == planner.IPC11 else None,
            prefer=prefer,
        )
    except KiwrapError as err:
        if err.code != "E_UNSUPPORTED":
            raise
        warnings.append(f"netlist unavailable: {err.message}")
        degraded.append("netlist_unavailable")
    else:
        providers["netlist"] = nl.provider_used(install)
        nets = nl.value["nets"]
        data["nets"] = len(nets)
        data["single_node_nets"] = [n["name"] for n in nets if len(n["nodes"]) < 2][: args.list_limit]
        degraded.extend(d for d in nl.degraded if d not in degraded)
        warnings.extend(nl.warnings)

    try:
        conn = norm_connectivity(
            ctx.services.pin_connectivity(ctx.services.file_read_schematic(project.root_sch))
        )
    except KiwrapError as err:
        if err.code != "E_UNSUPPORTED":
            raise
        warnings.append(f"pin connectivity analysis unavailable: {err.message}")
        degraded.append("connectivity_unavailable")
    else:
        providers["connectivity"] = planner.provider_label(planner.FILE, install)
        data["dangling_ends"] = len(conn["dangling_ends"])
        data["unconnected_pins"] = len(conn["unconnected_pins"])
        data["label_only_nets"] = len(conn["label_only_nets"])
        data["lists"] = {
            "dangling_ends": conn["dangling_ends"][: args.list_limit],
            "unconnected_pins": conn["unconnected_pins"][: args.list_limit],
            "label_only_nets": conn["label_only_nets"][: args.list_limit],
        }
        data["ok"] = not (conn["dangling_ends"] or conn["unconnected_pins"] or conn["label_only_nets"])

    if data.get("dangling_ends") or data.get("unconnected_pins"):
        # a wire to nowhere is deleted / extended with kw_sch_edit or kw_sch_connect; an
        # unconnected pin needs a wire, a label, a power symbol or a no_connect item
        next_tool = "kw_sch_edit" if data.get("dangling_ends") else "kw_sch_connect"
    elif data.get("label_only_nets"):
        next_tool = "kw_sch_connect"
    else:
        next_tool = "kw_sch_check"
    return Receipt(
        provider_used=outcome.provider_used(install),
        degraded=degraded,
        warnings=warnings,
        stage=state.stage,
        next_suggested=next_tool,
        data=data,
    )


SPEC = ToolSpec(
    name="kw_sch_stats",
    title="Schematic stats",
    description=(
        "Schematic health as numbers: components, power symbols, sheets, nets, single-node nets, dangling wire "
        "ends, unconnected pins, label-only nets, missing footprints and per-sheet counts (with mm positions of "
        "the offenders). Cheap; call after every schematic edit, before kw_sch_check."
    ),
    input_model=SchStatsArgs,
    output_model=SchStatsOut,
    handler=sch_stats,
    annotations=READ_ONLY,
)
