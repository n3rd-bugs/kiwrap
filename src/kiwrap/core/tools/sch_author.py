"""Schematic authoring: kw_sch_add_symbol, kw_sch_connect, kw_sch_add_item, kw_sch_edit,
kw_sch_annotate, kw_sch_assign_footprints (docs/ARCHITECTURE.md §7 writer rules, §10 catalog,
§11 receipts).

Every tool is a mutation (`_base.mutation`: lock, editor-lock refusal, external-edit check,
snapshot) that writes through the planner operation `schematic.write` (ipc11 -> file, see
`_author`), then runs ERC (`validate=true`, the default) and moves the stage: the board is no
longer in sync, the schematic is `sch_written`, zero ERC errors make it `erc_clean`. The receipt
carries `data.erc` (ok / errors / warnings / the first violations with mm positions),
`data.sheets` (sha256 per sheet file - the `if_match` values), the plan and the provider.

Geometry the agent never computes (§7): `kw_sch_add_symbol` returns the placed symbol's pins
with ABSOLUTE sheet coordinates (the verified 12-orientation transform, docs/VERIFICATION.md
claim 3), and `kw_sch_connect` resolves `(ref, pin)` endpoints through the writer's `pins_of`,
so wires start exactly on pin ends (pins connect only at wire END points; the writer splits
wires at pins and junction sites and snaps every coordinate to the 1.27 mm grid).
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from kiwrap.core.state import KiwrapError, Receipt
from kiwrap.core.state.project import Project
from kiwrap.core.state.receipts import ReceiptOut
from kiwrap.core.tools._author import (
    after_schematic_write,
    apply_outcome,
    backend,
    call_with_accepted,
    check_if_match,
    find_symbol,
    g13_guard,
    hierarchy,
    ipc_batch,
    ipc_sheet,
    ipc_write,
    lib_resolver,
    multi_unit_present,
    note_saved,
    run_write,
    schematic_writer,
    symbols_of,
)
from kiwrap.core.tools._base import DESTRUCTIVE, MUTATING, Mutation, ToolContext, ToolSpec, mutation
from kiwrap.core.tools._data import PIN_TYPES, canonical, mm, pick, rec, ref_key

ProviderArg = Literal["auto", "ipc11", "file"]
Rotation = Literal[0, 90, 180, 270]
Mirror = Literal["x", "y"]

GRID_MM = 1.27


def _pt(value: Any, name: str) -> tuple[float, float]:
    if value is None or len(value) != 2:
        raise KiwrapError(
            "E_BAD_ARGS",
            f"{name} must be [x, y] in millimetres",
            fix=f"pass {name}=[x_mm, y_mm]",
            example=f"{name}=[50.8, 63.5]",
        )
    return float(value[0]), float(value[1])


def _snap(v: float) -> float:
    return round(round(v / GRID_MM) * GRID_MM, 4)


def pin_dict(p: Any) -> dict[str, Any]:
    r = rec(p)
    return {
        "number": str(pick(r, "number", "num", default="")),
        "name": pick(r, "name", default=""),
        "x_mm": mm(pick(r, "x_mm", "x")),
        "y_mm": mm(pick(r, "y_mm", "y")),
        "type": canonical(pick(r, "electrical_type", "type", default=None), PIN_TYPES),
    }


def placement_dict(p: Any) -> dict[str, Any]:
    r = rec(p)
    pins = [pin_dict(x) for x in (pick(r, "pins", default=[]) or [])]
    pins.sort(key=lambda x: ref_key(x["number"]))
    return {"uuid": pick(r, "uuid", "id"), "ref": pick(r, "ref", "reference"), "pins": pins}


def _install(ctx: ToolContext, m: Mutation) -> tuple[Any, Any, Any]:
    install = ctx.services.install(m.state.target_kicad)
    return install, ctx.services.caps(install), ctx.services.config_root(install)


def _need_root(project: Project) -> None:
    if not project.root_sch.exists():
        raise KiwrapError(
            "E_PROJECT_NOT_FOUND",
            f"root schematic {project.root_sch.name} is missing",
            fix="kw_project(action='create') writes it",
        )


def _saved(m: Mutation, paths: Any) -> None:
    note_saved(m, paths)


# ---- kw_sch_add_symbol -----------------------------------------------------------------------------------


class SchAddSymbolArgs(BaseModel):
    project: str = Field(description="project directory or .kicad_pro path")
    lib_id: str = Field(description="`Nickname:Name` from the library tables (search first; never guess)")
    at_mm: list[float] = Field(description="[x, y] symbol origin in mm (snapped to the 1.27 mm grid)")
    ref: str | None = Field(
        default=None, description="reference designator (R1); None = R? until kw_sch_annotate"
    )
    value: str | None = Field(default=None, description="Value property (10k); default the library's")
    rotation: Rotation = Field(default=0, description="0 | 90 | 180 | 270 degrees")
    mirror: Mirror | None = Field(default=None, description="mirror axis x | y")
    unit: int = Field(default=1, ge=1, description="unit of a multi-unit symbol (1-based)")
    fields: dict[str, str] | None = Field(default=None, description="extra properties {name: value}")
    footprint: str | None = Field(default=None, description="Footprint property (`Lib:Name`)")
    sheet: str = Field(
        default="",
        description="child sheet by file stem ('blinker'), file name or instance path ('/blinker/'); '' = root",
    )
    erc: bool = Field(default=True, description="run ERC after the write (stage erc_clean when clean)")
    provider: ProviderArg = Field(default="auto", description="auto | ipc11 | file")


class SchAddSymbolData(BaseModel):
    ok: bool
    uuid: str | None
    ref: str | None
    lib_id: str
    unit: int
    at_mm: list[float]
    rotation: int
    mirror: str | None
    sheet: str
    pins: list[dict[str, Any]] = Field(
        description="absolute sheet coordinates: number, name, x_mm, y_mm, type"
    )
    erc: dict[str, Any] | None = None
    sheets: dict[str, str] = Field(
        default_factory=dict, description="sha256 per sheet file (if_match values)"
    )
    plan: dict[str, Any] | None = None


class SchAddSymbolOut(ReceiptOut):
    data: SchAddSymbolData  # type: ignore[assignment]


def sch_add_symbol(ctx: ToolContext, args: SchAddSymbolArgs) -> Receipt:
    project = Project.from_path(args.project)
    at = _pt(args.at_mm, "at_mm")
    at = (_snap(at[0]), _snap(at[1]))
    with mutation(ctx, project, "kw_sch_add_symbol", gate=False) as m:
        _need_root(project)
        install, caps, config = _install(ctx, m)
        prefer = None if args.provider == "auto" else args.provider
        kwargs: dict[str, Any] = {
            "lib_id": args.lib_id,
            "ref": args.ref,
            "value": args.value,
            "at_mm": at,
            "rotation": int(args.rotation),
            "mirror": args.mirror,
            "unit": args.unit,
            "fields": args.fields,
            "footprint": args.footprint,
            "sheet": args.sheet,
        }

        def via_file() -> Any:
            w = schematic_writer(ctx, install, caps, config, project)
            placement = call_with_accepted(w.add_symbol, **kwargs)
            _saved(m, w.save())
            return placement

        def via_ipc() -> Any:
            # the session is always the ROOT document; the twin scopes the sheet instance itself
            return ipc_write(
                ctx,
                install,
                config,
                project,
                project.root_sch,
                "ipc_sch_add_symbol",
                **{**kwargs, "sheet": ipc_sheet(ctx, project, args.sheet)},
            )

        outcome = run_write(
            "schematic.write",
            caps,
            install,
            ipc=via_ipc,
            file=via_file,
            prefer=prefer,
            skip_ipc_reason=g13_guard(caps, multi_unit=args.unit > 1),
        )
        placed = placement_dict(outcome.value)
        m.receipt.changed["added"].append(placed["ref"] or placed["uuid"] or args.lib_id)
        apply_outcome(m, outcome, install, ctx)
        m.receipt.data.update(
            {
                "ok": True,
                "uuid": placed["uuid"],
                "ref": placed["ref"],
                "lib_id": args.lib_id,
                "unit": args.unit,
                "at_mm": [at[0], at[1]],
                "rotation": int(args.rotation),
                "mirror": args.mirror,
                "sheet": args.sheet or project.root_sch.name,
                "pins": placed["pins"],
            }
        )
        m.summary = f"{placed['ref'] or '?'} {args.lib_id} at {at}"
        after_schematic_write(ctx, m, install, caps, config, project, validate=args.erc)
        return m.receipt


SCH_ADD_SYMBOL = ToolSpec(
    name="kw_sch_add_symbol",
    title="Add schematic symbol",
    description=(
        "Place a library symbol (lib_id `Nickname:Name`) at at_mm with rotation/mirror/unit, reference, value, "
        "fields and footprint. Returns the symbol uuid and its pins with ABSOLUTE mm endpoints (use them for "
        "kw_sch_connect; never compute pin geometry). Runs ERC after the write (erc=false skips it while "
        "batch-placing); the stage becomes sch_written / erc_clean."
    ),
    input_model=SchAddSymbolArgs,
    output_model=SchAddSymbolOut,
    handler=sch_add_symbol,
    annotations=MUTATING,
)


# ---- kw_sch_connect -------------------------------------------------------------------------------------


class Endpoint(BaseModel):
    ref: str | None = Field(default=None, description="symbol reference (with `pin`)")
    pin: str | None = Field(default=None, description="pin number (or name) of `ref`")
    at_mm: list[float] | None = Field(
        default=None, description="absolute [x, y] mm instead of ref+pin (a corner or a wire point)"
    )


Style = Literal["wire", "label", "global_label", "power"]
Route = Literal["hv", "vh", "direct"]


class SchConnectArgs(BaseModel):
    project: str = Field(description="project directory or .kicad_pro path")
    endpoints: list[Endpoint] = Field(
        min_length=1, description="pins (ref+pin) and/or absolute points, in order"
    )
    style: Style = Field(
        default="wire",
        description=(
            "wire: wires between consecutive endpoints (+ a local label `net` on the first one when given); "
            "label / global_label: a label `net` on every endpoint; power: a power symbol `net` on every endpoint"
        ),
    )
    net: str | None = Field(
        default=None, description="net / label text (required for label, global_label, power)"
    )
    route: Route = Field(
        default="hv",
        description=(
            "wire style: L-shaped horizontal-then-vertical, vertical-then-horizontal, or direct. The corner "
            "is NOT obstacle-aware: a route that passes over another pin is refused (E_BAD_ARGS names the "
            "pin); route the other way or give the corners as at_mm endpoints"
        ),
    )
    sheet: str = Field(
        default="",
        description="child sheet by file stem ('blinker'), file name or instance path ('/blinker/'); '' = root",
    )
    erc: bool = Field(default=True, description="run ERC after the write")
    provider: ProviderArg = Field(default="auto", description="auto | ipc11 | file")


def resolve_endpoint(w: Any, e: Endpoint, pins_cache: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    if e.at_mm is not None:
        x, y = _pt(e.at_mm, "at_mm")
        return {"ref": e.ref, "pin": e.pin, "x_mm": _snap(x), "y_mm": _snap(y), "given": "at_mm"}
    if not e.ref or e.pin is None:
        raise KiwrapError(
            "E_BAD_ARGS",
            "an endpoint needs ref + pin, or at_mm",
            fix="pass {ref: 'R1', pin: '1'} or {at_mm: [x, y]}",
            example='endpoints=[{"ref": "R1", "pin": "1"}, {"ref": "U1", "pin": "3"}]',
        )
    if e.ref not in pins_cache:
        try:
            pins_cache[e.ref] = [pin_dict(p) for p in (w.pins_of(e.ref) or [])]
        except KeyError:
            pins_cache[e.ref] = []
    pins = pins_cache[e.ref]
    if not pins:
        raise KiwrapError(
            "E_BAD_ARGS",
            f"no symbol {e.ref!r} (or it has no pins) in the schematic",
            fix="place it with kw_sch_add_symbol first, or use a reference kw_sch_get lists",
        )
    want = str(e.pin)
    hit = next((p for p in pins if p["number"] == want), None) or next(
        (p for p in pins if p["name"] == want), None
    )
    if hit is None:
        raise KiwrapError(
            "E_BAD_ARGS",
            f"{e.ref} has no pin {want!r}",
            fix="use one of the pin numbers (kw_sch_get lists them with positions)",
            candidates=[f"{p['number']} ({p['name']})" for p in pins][:50],
        )
    return {"ref": e.ref, "pin": hit["number"], "x_mm": hit["x_mm"], "y_mm": hit["y_mm"], "given": "pin"}


def wire_points(a: dict[str, Any], b: dict[str, Any], route: str) -> list[tuple[float, float]]:
    p, q = (a["x_mm"], a["y_mm"]), (b["x_mm"], b["y_mm"])
    if route == "direct" or p[0] == q[0] or p[1] == q[1]:
        return [p, q]
    corner = (q[0], p[1]) if route == "hv" else (p[0], q[1])
    return [p, corner, q]


_ON_SEGMENT_EPS = 1e-3


def _on_segment(pt: tuple[float, float], a: tuple[float, float], b: tuple[float, float]) -> bool:
    """True when `pt` lies on segment a-b (ends included), within a micrometre."""
    cross = (b[0] - a[0]) * (pt[1] - a[1]) - (b[1] - a[1]) * (pt[0] - a[0])
    if abs(cross) > _ON_SEGMENT_EPS:
        return False
    return (
        min(a[0], b[0]) - _ON_SEGMENT_EPS <= pt[0] <= max(a[0], b[0]) + _ON_SEGMENT_EPS
        and min(a[1], b[1]) - _ON_SEGMENT_EPS <= pt[1] <= max(a[1], b[1]) + _ON_SEGMENT_EPS
    )


def pins_on_route(
    symbols: Any, polylines: list[list[tuple[float, float]]], resolved: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Every visible pin of `symbols` (the sheet's `SymbolPlacement`s) that the planned wires
    touch, other than the requested (ref, pin) endpoints: the writer splits a wire at every pin
    it passes over, so such a pin is CONNECTED - an L-route through a capacitor's other pin
    shorts it (reviewer finding, 2026-09-20). `endpoint` is True when the pin sits on a polyline
    end the caller gave as `at_mm` (deliberate), False for a pin hit by a corner or an interior."""
    wanted = {(e["ref"], str(e["pin"])) for e in resolved if e.get("ref") and e.get("pin") is not None}
    ends = {(round(pl[0][0], 4), round(pl[0][1], 4)) for pl in polylines} | {
        (round(pl[-1][0], 4), round(pl[-1][1], 4)) for pl in polylines
    }
    hits: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for sym in symbols or []:
        r = rec(sym)
        ref = pick(r, "ref", "reference", default="") or ""
        for pin in pick(r, "pins", default=[]) or []:
            pr = rec(pin)
            if pick(pr, "hidden", default=False):
                continue
            number = str(pick(pr, "number", "num", default=""))
            if (ref, number) in wanted or (ref, number) in seen:
                continue
            px, py = mm(pick(pr, "x_mm", "x")), mm(pick(pr, "y_mm", "y"))
            if px is None or py is None:
                continue
            pt = (px, py)
            if any(_on_segment(pt, a, b) for pl in polylines for a, b in zip(pl, pl[1:], strict=False)):
                seen.add((ref, number))
                hits.append(
                    {
                        "ref": ref,
                        "pin": number,
                        "x_mm": pt[0],
                        "y_mm": pt[1],
                        "endpoint": (round(pt[0], 4), round(pt[1], 4)) in ends,
                    }
                )
    return hits


def _refuse_pins_on_route(hits: list[dict[str, Any]], route: str) -> None:
    through = [h for h in hits if not h["endpoint"]]
    if not through:
        return
    names = ", ".join(f"{h['ref']} pin {h['pin']} at ({h['x_mm']}, {h['y_mm']})" for h in through[:10])
    other = "vh" if route == "hv" else "hv"
    raise KiwrapError(
        "E_BAD_ARGS",
        f"the {route} route passes through {names}: the wire would connect that pin too",
        why="hv / vh corners are not obstacle-aware; the writer splits a wire at every pin it passes over",
        fix=f"use route='{other}' or route='direct', or give the corners explicitly as at_mm endpoints",
        candidates=[
            f"route='{other}'",
            "route='direct'",
            "endpoints=[{ref, pin}, {at_mm: [x, y]}, {ref, pin}]",
        ],
        data={"connected_pins": hits},
    )


def drop_missing_ids(ctx: ToolContext, project: Project, out: dict[str, Any]) -> dict[str, Any]:
    """Filter `wires` / `junctions` / `split_wires` of a connect result down to the uuids that
    exist in the saved files (KiCad merges collinear halves on save); unknown when the file
    reader is unavailable, then the lists are returned as they are."""
    hier = hierarchy(ctx, project)
    if hier is None:
        return out
    present: set[str] = set()
    for inst in getattr(hier, "instances", []) or []:
        f = rec(pick(rec(inst), "file"))
        for kind in ("wires", "buses", "junctions"):
            for item in pick(f, kind, default=[]) or []:
                uid = pick(rec(item), "uuid", "id")
                if uid:
                    present.add(str(uid))
    if not present:
        return out
    for key in ("wires", "junctions", "split_wires"):
        out[key] = [i for i in out.get(key, []) if str(i) in present]
    return out


def sch_connect(ctx: ToolContext, args: SchConnectArgs) -> Receipt:
    project = Project.from_path(args.project)
    if args.style != "wire" and not args.net:
        raise KiwrapError(
            "E_BAD_ARGS",
            f"style={args.style!r} needs `net` (the label text / power net name)",
            fix="pass net='GND' (power) or net='SDA' (label)",
        )
    if args.style == "wire" and len(args.endpoints) < 2:
        raise KiwrapError(
            "E_BAD_ARGS", "style='wire' needs at least two endpoints", fix="pass two or more endpoints"
        )
    with mutation(ctx, project, "kw_sch_connect", gate=False) as m:
        _need_root(project)
        install, caps, config = _install(ctx, m)
        prefer = None if args.provider == "auto" else args.provider
        resolved: list[dict[str, Any]] = []

        def do(w: Any, adders: dict[str, Any]) -> dict[str, Any]:
            cache: dict[str, list[dict[str, Any]]] = {}
            resolved.clear()
            resolved.extend(resolve_endpoint(w, e, cache) for e in args.endpoints)
            out: dict[str, Any] = {
                "wires": [],
                "junctions": [],
                "split_wires": [],
                "labels": [],
                "power": [],
                "junction_sites": [],
                "connected_pins": [],
            }
            if args.style == "wire":
                polylines = [
                    wire_points(a, b, args.route) for a, b in zip(resolved, resolved[1:], strict=False)
                ]
                list_symbols = getattr(w, "symbols", None)
                if callable(list_symbols):
                    hits = pins_on_route(list_symbols(args.sheet), polylines, resolved)
                    _refuse_pins_on_route(hits, args.route)  # before anything is written
                    out["connected_pins"] = hits
                for pts in polylines:
                    ids = adders["wire"](points_mm=pts, sheet=args.sheet) or []
                    out["wires"].extend(str(i) for i in ids)
                    out["junctions"].extend(str(i) for i in getattr(ids, "junctions", []) or [])
                    out["split_wires"].extend(str(i) for i in getattr(ids, "split_wires", []) or [])
                if args.net:
                    first = resolved[0]
                    out["labels"].append(
                        str(
                            adders["label"](
                                text=args.net,
                                at_mm=(first["x_mm"], first["y_mm"]),
                                kind="local",
                                rotation=0,
                                sheet=args.sheet,
                            )
                        )
                    )
            elif args.style in ("label", "global_label"):
                kind = "local" if args.style == "label" else "global"
                for e in resolved:
                    out["labels"].append(
                        str(
                            adders["label"](
                                text=args.net,
                                at_mm=(e["x_mm"], e["y_mm"]),
                                kind=kind,
                                rotation=0,
                                sheet=args.sheet,
                            )
                        )
                    )
            else:
                for e in resolved:
                    placed = adders["power"](
                        net=args.net, at_mm=(e["x_mm"], e["y_mm"]), rotation=0, sheet=args.sheet, flag=False
                    )
                    out["power"].append(placement_dict(placed))
            return out

        def via_file() -> Any:
            w = schematic_writer(ctx, install, caps, config, project)
            adders = {
                "wire": lambda **kw: call_with_accepted(w.add_wire, **kw),
                "label": lambda **kw: call_with_accepted(w.add_label, **kw),
                "power": lambda **kw: call_with_accepted(w.add_power, **kw),
            }
            out = do(w, adders)
            _saved(m, w.save())
            return out

        def via_ipc() -> Any:
            # pins are resolved through the file writer (the verified transform); the items are
            # created over IPC by the twins in one session of the ROOT document (the twins scope
            # the sheet instance), saved once at the end
            key = ipc_sheet(ctx, project, args.sheet)
            w = schematic_writer(ctx, install, caps, config, project)
            with ipc_batch(ctx, install, config, project, project.root_sch, save="ipc_sch_save") as call:
                adders = {
                    "wire": lambda **kw: call("ipc_sch_add_wire", **{**kw, "sheet": key}),
                    "label": lambda **kw: call("ipc_sch_add_label", **{**kw, "sheet": key}),
                    "power": lambda **kw: call("ipc_sch_add_power", **{**kw, "sheet": key}),
                }
                result = do(w, adders)
            # KiCad merges collinear wire halves when it saves: only ids that survived are reported
            return drop_missing_ids(ctx, project, result)

        outcome = run_write("schematic.write", caps, install, ipc=via_ipc, file=via_file, prefer=prefer)
        out = outcome.value
        added = [
            *out["wires"],
            *out.get("junctions", []),
            *out.get("split_wires", []),
            *out["labels"],
            *[p["ref"] or p["uuid"] for p in out["power"]],
        ]
        m.receipt.changed["added"].extend(str(a) for a in added if a)
        apply_outcome(m, outcome, install, ctx)
        m.receipt.data.update(
            {
                "ok": True,
                "style": args.style,
                "net": args.net,
                "endpoints": resolved,
                "wires": out["wires"],
                "junctions": out.get("junctions", []),
                "split_wires": out.get("split_wires", []),
                "labels": out["labels"],
                "power": out["power"],
                "connected_pins": out.get("connected_pins", []),
                "sheet": args.sheet or project.root_sch.name,
            }
        )
        for hit in out.get("connected_pins", []):
            m.receipt.warnings.append(
                f"the wire also ends on {hit['ref']} pin {hit['pin']} at ({hit['x_mm']}, {hit['y_mm']}), "
                "which is now connected"
            )
        names = [f"{e['ref']}.{e['pin']}" if e["ref"] else f"({e['x_mm']},{e['y_mm']})" for e in resolved]
        m.summary = f"{args.style} {' -> '.join(names)}" + (f" net={args.net}" if args.net else "")
        after_schematic_write(ctx, m, install, caps, config, project, validate=args.erc)
        return m.receipt


SCH_CONNECT = ToolSpec(
    name="kw_sch_connect",
    title="Connect schematic pins",
    description=(
        "Connect endpoints given as (ref, pin) or absolute mm: style wire draws L-routed wires between "
        "consecutive endpoints (split at pins, snapped to 1.27 mm, junctions added; `net` adds a local label), "
        "label / global_label put a label `net` on each endpoint, power puts a power symbol `net` on each. "
        "`wires` lists the NEW wire segments only (`junctions` / `split_wires` the rest of the commit). "
        "Pin positions come from the writer (never guessed). hv/vh corners are not obstacle-aware: a wire "
        "that would pass over another pin is refused with the pin named (use the other route or explicit "
        "at_mm corners); `data.connected_pins` lists pins the wire ends on besides the endpoints. "
        "ERC runs after the write."
    ),
    input_model=SchConnectArgs,
    handler=sch_connect,
    annotations=MUTATING,
)


# ---- kw_sch_add_item ---------------------------------------------------------------------------------------

ItemType = Literal[
    "power",
    "pwr_flag",
    "no_connect",
    "label",
    "global_label",
    "hierarchical_label",
    "sheet",
    "sheet_pin",
    "text",
]


class SheetPinSpec(BaseModel):
    name: str
    side: Literal["left", "right", "top", "bottom"] = "left"
    kind: Literal["input", "output", "bidirectional", "tri_state", "passive"] = "passive"


class SchItem(BaseModel):
    type: ItemType
    at_mm: list[float] | None = Field(
        default=None, description="[x, y] mm (or ref+pin for power / pwr_flag / no_connect)"
    )
    ref: str | None = Field(default=None, description="with `pin`: place the item on that pin's endpoint")
    pin: str | None = None
    net: str | None = Field(default=None, description="power / pwr_flag: net name (GND, +3V3)")
    text: str | None = Field(
        default=None, description="label text / text content / sheet name / sheet pin name"
    )
    rotation: Rotation = 0
    file: str | None = Field(default=None, description="sheet: the sheet file (`power.kicad_sch`)")
    size_mm: list[float] | None = Field(default=None, description="sheet: [w, h]")
    pins: list[SheetPinSpec] | None = Field(default=None, description="sheet: its sheet pins")
    sheet_uuid: str | None = Field(default=None, description="sheet_pin: the sheet symbol to add the pin to")
    side: Literal["left", "right", "top", "bottom"] | None = None
    kind: Literal["input", "output", "bidirectional", "tri_state", "passive"] | None = Field(
        default=None,
        description="sheet_pin: its kind; global_label / hierarchical_label: the label's ERC shape",
    )


class SchAddItemArgs(BaseModel):
    project: str = Field(description="project directory or .kicad_pro path")
    item: SchItem
    sheet: str = Field(
        default="",
        description="child sheet by file stem ('blinker'), file name or instance path ('/blinker/'); '' = root",
    )
    erc: bool = Field(default=True, description="run ERC after the write")
    provider: ProviderArg = Field(default="auto", description="auto | ipc11 | file")


def _item_point(w: Any, item: SchItem) -> tuple[float, float]:
    if item.ref and item.pin is not None:
        e = resolve_endpoint(w, Endpoint(ref=item.ref, pin=item.pin), {})
        return e["x_mm"], e["y_mm"]
    x, y = _pt(item.at_mm, "item.at_mm")
    return _snap(x), _snap(y)


def _need(item: SchItem, field: str) -> Any:
    val = getattr(item, field)
    if val is None or val == "":
        raise KiwrapError(
            "E_BAD_ARGS",
            f"item type {item.type!r} needs `{field}`",
            fix=f"pass item.{field}",
            example='item={"type": "power", "net": "GND", "ref": "C1", "pin": "2"}',
        )
    return val


def sch_add_item(ctx: ToolContext, args: SchAddItemArgs) -> Receipt:
    project = Project.from_path(args.project)
    item = args.item
    with mutation(ctx, project, "kw_sch_add_item", gate=False) as m:
        _need_root(project)
        install, caps, config = _install(ctx, m)
        prefer = None if args.provider == "auto" else args.provider
        twin = {
            "power": "ipc_sch_add_power",
            "pwr_flag": "ipc_sch_add_power",
            "label": "ipc_sch_add_label",
            "global_label": "ipc_sch_add_label",
            "hierarchical_label": "ipc_sch_add_label",
        }.get(item.type)

        def do(w: Any, call: Any) -> dict[str, Any]:
            t = item.type
            if t in ("power", "pwr_flag"):
                net = _need(item, "net")
                at = _item_point(w, item)
                placed = call(
                    "add_power",
                    net=net,
                    at_mm=at,
                    rotation=int(item.rotation),
                    sheet=args.sheet,
                    flag=t == "pwr_flag",
                )
                d = placement_dict(placed)
                return {"uuid": d["uuid"], "ref": d["ref"], "at_mm": list(at), "pins": d["pins"]}
            if t == "no_connect":
                at = _item_point(w, item)
                uid = call("add_no_connect", at_mm=at, sheet=args.sheet)
                return {"uuid": str(uid), "ref": None, "at_mm": list(at)}
            if t in ("label", "global_label", "hierarchical_label"):
                text = _need(item, "text")
                at = _item_point(w, item)
                kind = {"label": "local", "global_label": "global", "hierarchical_label": "hierarchical"}[t]
                extra: dict[str, Any] = {}
                if item.kind and t != "label":
                    # the ERC shape of a global / hierarchical label (input, output, ...): it must
                    # match the sheet pin's kind or ERC reports the pair as mismatched
                    extra["shape"] = item.kind
                uid = call(
                    "add_label",
                    text=text,
                    at_mm=at,
                    kind=kind,
                    rotation=int(item.rotation),
                    sheet=args.sheet,
                    **extra,
                )
                return {"uuid": str(uid), "ref": None, "at_mm": list(at), "kind": kind, **extra}
            if t == "sheet":
                name, file = _need(item, "text"), _need(item, "file")
                file_name = file if file.endswith(".kicad_sch") else f"{file}.kicad_sch"
                if (
                    not file_name.removesuffix(".kicad_sch")
                    or Path(file_name).name != file_name
                    or Path(file_name).is_absolute()
                    or file_name.startswith(("~", "."))
                    or ":" in file_name
                ):
                    raise KiwrapError(
                        "E_BAD_ARGS",
                        f"item.file {file!r} must be a bare file name inside the project directory",
                        why="a sheet file lives next to the root schematic; a path would create a file outside "
                        "the project (not owned, not snapshotted)",
                        fix="pass the file name only, e.g. file='blinker' or 'blinker.kicad_sch'",
                        example='item={"type": "sheet", "text": "blinker", "file": "blinker", ...}',
                    )
                at = _item_point(w, item)
                size = _pt(item.size_mm, "item.size_mm")
                pins = [(p.name, p.side, p.kind) for p in (item.pins or [])]
                placed = call(
                    "add_sheet", name=name, file=file, at_mm=at, size_mm=size, pins=pins, sheet=args.sheet
                )
                r = rec(placed)
                sheet_pins = []
                for sp in pick(r, "pins", default=[]) or []:
                    spr = rec(sp)
                    sheet_pins.append(
                        {
                            "name": pick(spr, "name"),
                            "kind": pick(spr, "kind", "shape"),
                            "side": pick(spr, "side"),
                            "x_mm": mm(pick(spr, "x_mm", "x")),
                            "y_mm": mm(pick(spr, "y_mm", "y")),
                            "uuid": pick(spr, "uuid", "id"),
                        }
                    )
                return {
                    "uuid": pick(r, "uuid", "id"),
                    "ref": None,
                    "at_mm": list(at),
                    "file": file,
                    "name": name,
                    "pins": sheet_pins,
                }
            if t == "sheet_pin":
                fn = getattr(w, "add_sheet_pin", None)
                if fn is None:
                    raise KiwrapError(
                        "E_UNSUPPORTED",
                        "the schematic writer has no add_sheet_pin",
                        why="sheet pins are declared with the sheet in this build",
                        fix="pass `pins` to item type 'sheet' when creating the sheet",
                    )
                uid = call_with_accepted(
                    fn,
                    _need(item, "sheet_uuid"),
                    name=_need(item, "text"),
                    side=item.side or "left",
                    kind=item.kind or "passive",
                    sheet=args.sheet,
                )
                return {"uuid": str(uid), "ref": None}
            fn = getattr(w, "add_text", None)
            if fn is None:
                raise KiwrapError(
                    "E_UNSUPPORTED",
                    "the schematic writer has no add_text",
                    fix="use a label for net-bearing text; free text is not supported by this build",
                )
            at = _item_point(w, item)
            uid = call_with_accepted(
                fn, _need(item, "text"), at_mm=at, rotation=int(item.rotation), sheet=args.sheet
            )
            return {"uuid": str(uid), "ref": None, "at_mm": list(at)}

        def via_file() -> Any:
            w = schematic_writer(ctx, install, caps, config, project)
            out = do(w, lambda method, **kw: call_with_accepted(getattr(w, method), **kw))
            _saved(m, w.save())
            return out

        def via_ipc() -> Any:
            if twin is None:
                raise KiwrapError(
                    "E_UNSUPPORTED",
                    f"no IPC twin for item type {item.type!r}",
                    data={"capability": "ipc.headless"},
                )
            key = ipc_sheet(ctx, project, args.sheet)
            w = schematic_writer(ctx, install, caps, config, project)
            return do(
                w,
                lambda method, **kw: ipc_write(
                    ctx, install, config, project, project.root_sch, twin, **{**kw, "sheet": key}
                ),
            )

        outcome = run_write("schematic.write", caps, install, ipc=via_ipc, file=via_file, prefer=prefer)
        out = outcome.value
        m.receipt.changed["added"].append(out.get("ref") or out.get("uuid") or item.type)
        apply_outcome(m, outcome, install, ctx)
        m.receipt.data.update(
            {"ok": True, "type": item.type, **out, "sheet": args.sheet or project.root_sch.name}
        )
        m.summary = f"{item.type} {item.net or item.text or ''}".rstrip()
        after_schematic_write(ctx, m, install, caps, config, project, validate=args.erc)
        return m.receipt


SCH_ADD_ITEM = ToolSpec(
    name="kw_sch_add_item",
    title="Add schematic item",
    description=(
        "Add one item: power (a power symbol `net` at at_mm or on ref+pin), pwr_flag (PWR_FLAG on a power-input "
        "net), no_connect (on an unused pin), label / global_label / hierarchical_label (`text`), sheet (name "
        "`text`, `file`, at_mm, size_mm, pins[{name, side, kind}] - the file is created with its own instance "
        "path) or text. ERC runs after the write."
    ),
    input_model=SchAddItemArgs,
    handler=sch_add_item,
    annotations=MUTATING,
)


# ---- kw_sch_edit -----------------------------------------------------------------------------------------

EditOp = Literal["move", "rotate", "mirror", "property", "value", "footprint", "dnp", "delete"]


class SchEditOp(BaseModel):
    op: EditOp
    ref: str | None = Field(default=None, description="symbol reference (resolved to its uuid)")
    uuid: str | None = Field(default=None, description="any item's uuid (wires, labels, sheets, symbols)")
    at_mm: list[float] | None = Field(default=None, description="move: new [x, y] mm")
    rotation: Rotation | None = Field(default=None, description="rotate: absolute 0 | 90 | 180 | 270")
    mirror: Mirror | None = Field(default=None, description="mirror: x | y (null clears)")
    name: str | None = Field(default=None, description="property: the property name")
    value: str | None = Field(default=None, description="property / value / footprint: the new text")
    dnp: bool | None = Field(default=None, description="dnp: true | false")


class SchEditArgs(BaseModel):
    project: str = Field(description="project directory or .kicad_pro path")
    ops: list[SchEditOp] = Field(min_length=1)
    if_match: str | None = Field(
        default=None, description="sha256 (prefix ok) of the sheet file the ops were planned against"
    )
    sheet: str = Field(
        default="",
        description="child sheet by file stem ('blinker'), file name or instance path ('/blinker/'); '' = root",
    )
    erc: bool = Field(default=True, description="run ERC after the write")
    provider: ProviderArg = Field(default="auto", description="auto | ipc11 | file")


def changes_for(op: SchEditOp) -> dict[str, Any]:
    """The writer's `edit(uuid, changes)` dict for one op (keys: at_mm, rotation, mirror,
    fields, value, footprint, dnp - the vocabulary the file writer and the IPC twin share)."""
    if op.op == "move":
        x, y = _pt(op.at_mm, "at_mm")
        return {"at_mm": (_snap(x), _snap(y))}
    if op.op == "rotate":
        if op.rotation is None:
            raise KiwrapError("E_BAD_ARGS", "rotate needs `rotation`", fix="pass rotation=0|90|180|270")
        return {"rotation": int(op.rotation)}
    if op.op == "mirror":
        return {"mirror": op.mirror}
    if op.op == "property":
        if not op.name or op.value is None:
            raise KiwrapError(
                "E_BAD_ARGS", "property needs `name` and `value`", fix="pass name='Datasheet', value='...'"
            )
        return {"fields": {op.name: op.value}}  # the key both the file writer and the IPC twin accept
    if op.op in ("value", "footprint"):
        if op.value is None:
            raise KiwrapError("E_BAD_ARGS", f"{op.op} needs `value`", fix=f"pass value='<new {op.op}>'")
        return {op.op: op.value}
    if op.op == "dnp":
        if op.dnp is None:
            raise KiwrapError("E_BAD_ARGS", "dnp needs `dnp`", fix="pass dnp=true|false")
        return {"dnp": bool(op.dnp)}
    return {}


def sch_edit(ctx: ToolContext, args: SchEditArgs) -> Receipt:
    project = Project.from_path(args.project)
    with mutation(ctx, project, "kw_sch_edit", gate=False) as m:
        _need_root(project)
        check_if_match(project, args.sheet, args.if_match)
        install, caps, config = _install(ctx, m)
        prefer = None if args.provider == "auto" else args.provider
        symbols = symbols_of(hierarchy(ctx, project))
        plan: list[dict[str, Any]] = []
        touches_multi_unit = False
        for i, op in enumerate(args.ops):
            uuid = op.uuid
            sym: dict[str, Any] | None = None
            if uuid is None:
                if not op.ref:
                    raise KiwrapError(
                        "E_BAD_ARGS",
                        f"ops[{i}] needs `ref` or `uuid`",
                        fix="pass the symbol reference or the item uuid",
                    )
                sym = find_symbol(symbols, op.ref)
                uuid = str(sym["uuid"])
            else:
                sym = next((s for s in symbols if s["uuid"] == uuid), None)
            if sym is not None and sym["unit_count"] > 1:
                touches_multi_unit = True
            plan.append(
                {
                    "index": i,
                    "op": op.op,
                    "ref": op.ref or (sym or {}).get("ref"),
                    "uuid": uuid,
                    "changes": changes_for(op) if op.op != "delete" else None,
                }
            )

        def run_plan(edit: Any, delete: Any) -> list[dict[str, Any]]:
            applied: list[dict[str, Any]] = []
            for p in plan:
                if p["op"] == "delete":
                    delete(p["uuid"])
                else:
                    edit(p["uuid"], p["changes"])
                applied.append({k: p[k] for k in ("index", "op", "ref", "uuid")})
            return applied

        def via_file() -> Any:
            w = schematic_writer(ctx, install, caps, config, project)
            applied = run_plan(w.edit, w.delete)
            _saved(m, w.save())
            return applied

        def via_ipc() -> Any:
            with ipc_batch(ctx, install, config, project, project.root_sch, save="ipc_sch_save") as call:
                return run_plan(
                    lambda uuid, changes: call("ipc_sch_edit", uuid=uuid, changes=changes),
                    lambda uuid: call("ipc_sch_delete", uuid=uuid),
                )

        outcome = run_write(
            "schematic.write",
            caps,
            install,
            ipc=via_ipc,
            file=via_file,
            prefer=prefer,
            skip_ipc_reason=g13_guard(caps, multi_unit=touches_multi_unit),
        )
        applied = outcome.value
        for p in applied:
            key = "removed" if p["op"] == "delete" else "updated"
            label = p["ref"] or p["uuid"]
            if label not in m.receipt.changed[key]:
                m.receipt.changed[key].append(label)
        apply_outcome(m, outcome, install, ctx)
        m.receipt.data.update(
            {"ok": True, "applied": applied, "skipped": [], "sheet": args.sheet or project.root_sch.name}
        )
        m.summary = ", ".join(f"{p['op']} {p['ref'] or p['uuid']}" for p in applied)[:120]
        after_schematic_write(ctx, m, install, caps, config, project, validate=args.erc)
        return m.receipt


SCH_EDIT = ToolSpec(
    name="kw_sch_edit",
    title="Edit schematic items",
    description=(
        "Apply ops[] to symbols (by ref) or any item (by uuid): move (at_mm), rotate, mirror, property (name + "
        "value), value, footprint, dnp, delete. `if_match` (sha256 of the sheet from a previous receipt's "
        "data.sheets) refuses to edit a sheet that changed since. Every op is applied or the call fails as a "
        "whole (the snapshot is the undo). ERC runs after the write."
    ),
    input_model=SchEditArgs,
    handler=sch_edit,
    annotations=DESTRUCTIVE,
)


# ---- kw_sch_annotate ------------------------------------------------------------------------------------


class SchAnnotateArgs(BaseModel):
    project: str = Field(description="project directory or .kicad_pro path")
    scheme: Literal["sheet", "global"] = Field(
        default="global",
        description="global (default): one sequence R1, R2...; sheet: per-sheet hundreds (R101 on sheet 1, R201...)",
    )
    start_per_sheet: bool = Field(default=False, description="sheet scheme: restart numbering on every sheet")
    reset: bool = Field(
        default=False,
        description="renumber EVERY reference (KiCad's 'reset existing annotations'); default: keep existing "
        "references and number only the `?` ones and duplicates",
    )
    erc: bool = Field(default=True, description="run ERC after the write")
    provider: ProviderArg = Field(default="auto", description="auto | ipc11 | file")


def sch_annotate(ctx: ToolContext, args: SchAnnotateArgs) -> Receipt:
    project = Project.from_path(args.project)
    with mutation(ctx, project, "kw_sch_annotate", gate=False) as m:
        _need_root(project)
        install, caps, config = _install(ctx, m)
        prefer = None if args.provider == "auto" else args.provider
        symbols = symbols_of(hierarchy(ctx, project))

        # both providers share KiCad's default: existing references are kept unless `reset`
        # (file writer: keep_existing=not reset; IPC twin: reset=reset)
        def via_file() -> Any:
            w = schematic_writer(ctx, install, caps, config, project)
            mapping = call_with_accepted(
                w.annotate,
                scheme=args.scheme,
                start_per_sheet=args.start_per_sheet,
                keep_existing=not args.reset,
            )
            _saved(m, w.save())
            return dict(mapping or {})

        def via_ipc() -> Any:
            return dict(
                ipc_write(
                    ctx,
                    install,
                    config,
                    project,
                    project.root_sch,
                    "ipc_sch_annotate",
                    scheme=args.scheme,
                    start_per_sheet=args.start_per_sheet,
                    reset=args.reset,
                )
                or {}
            )

        outcome = run_write(
            "schematic.write",
            caps,
            install,
            ipc=via_ipc,
            file=via_file,
            prefer=prefer,
            skip_ipc_reason=g13_guard(caps, multi_unit=multi_unit_present(symbols)),
        )
        mapping: dict[str, str] = {str(k): str(v) for k, v in outcome.value.items()}
        m.receipt.changed["updated"].extend(sorted(mapping.values(), key=ref_key))
        apply_outcome(m, outcome, install, ctx)
        m.receipt.data.update(
            {"ok": True, "mapping": mapping, "renamed": len(mapping), "scheme": args.scheme}
        )
        m.summary = f"{len(mapping)} references ({args.scheme})"
        after_schematic_write(ctx, m, install, caps, config, project, validate=args.erc)
        if m.receipt.data.get("erc") and m.receipt.data["erc"]["ok"]:
            m.receipt.next_suggested = "kw_sch_assign_footprints"
        return m.receipt


SCH_ANNOTATE = ToolSpec(
    name="kw_sch_annotate",
    title="Annotate schematic",
    description=(
        "Give every `R?`-style symbol (and duplicates) a unique reference (scheme sheet | global; start_per_sheet); "
        "existing references are kept unless reset=true. Returns the {old: new} mapping; references are written "
        "to the Reference property AND the instances block. ERC runs after the write."
    ),
    input_model=SchAnnotateArgs,
    handler=sch_annotate,
    annotations=MUTATING,
)


# ---- kw_sch_assign_footprints ----------------------------------------------------------------------------


class FootprintRule(BaseModel):
    fpid: str = Field(description="footprint id `Lib:Name` to assign")
    lib_id: str | None = Field(default=None, description="match symbols with this lib_id")
    value: str | None = Field(default=None, description="match symbols with this Value")
    ref_prefix: str | None = Field(default=None, description="match references starting with this (R, C, U)")


class SchAssignFootprintsArgs(BaseModel):
    project: str = Field(description="project directory or .kicad_pro path")
    mapping: dict[str, str] | None = Field(default=None, description="{ref: fpid} explicit assignments")
    rules: list[FootprintRule] | None = Field(
        default=None, description="rules applied to symbols without a footprint"
    )
    overwrite: bool = Field(default=False, description="rules also replace existing footprints")
    suggest: bool = Field(
        default=True,
        description="LibIndex suggestions (from the symbols' footprint filters) for what stays unresolved",
    )
    allow_unknown: bool = Field(
        default=False,
        description=(
            "write an fpid the fp-lib-table cannot resolve (a project-local library not yet on the table). "
            "By default an unknown fpid is not written: it comes back in unresolved with candidates"
        ),
    )
    erc: bool = Field(default=True, description="run ERC after the write")


def check_fpids(
    ctx: ToolContext, install: Any, config: Any, project: Project, fpids: Iterable[str], m: Mutation
) -> dict[str, dict[str, Any]]:
    """`{fpid: {reason, candidates}}` for every fpid the library resolver cannot load through the
    fp-lib-table (global + project rows, env vars, ${KIPRJMOD}). An fpid that only surfaces at
    kw_pcb_sync as "footprint not found" is a mistake this tool must catch (reviewer finding,
    2026-09-20). Empty when the resolver component is not available (the receipt says so)."""
    wanted = sorted({str(f) for f in fpids if f})
    if not wanted:
        return {}
    try:
        resolver = lib_resolver(ctx, install, config, project)
    except KiwrapError as err:
        if err.code != "E_UNSUPPORTED":
            raise
        m.receipt.warnings.append(f"footprint ids not checked: {err.message}")
        m.receipt.degraded.append("fpid_check_unavailable")
        return {}
    load = getattr(resolver, "footprint", None)
    if not callable(load):
        return {}
    bad: dict[str, dict[str, Any]] = {}
    for fpid in wanted:
        try:
            load(fpid)
        except Exception as exc:  # noqa: BLE001 - every resolver failure is "not resolvable" for the agent
            nick, name = (fpid.split(":", 1) + [""])[:2] if ":" in fpid else ("", fpid)
            raw = [str(c) for c in (getattr(exc, "candidates", None) or [])]
            if raw and ":" not in raw[0]:
                # the resolver names either sibling footprints of the library or sibling nicknames
                raw = [f"{c}:{name}" if "nickname" in str(exc) else f"{nick}:{c}" for c in raw]
            bad[fpid] = {"reason": str(getattr(exc, "message", None) or exc), "candidates": raw[:10]}
    return bad


def _rule_matches(rule: FootprintRule, sym: dict[str, Any]) -> bool:
    if rule.lib_id and sym["lib_id"] != rule.lib_id:
        return False
    if rule.value and sym["value"] != rule.value:
        return False
    if rule.ref_prefix:
        ref = str(sym["ref"] or "")
        if not ref.startswith(rule.ref_prefix) or not ref[len(rule.ref_prefix) :][:1].isdigit():
            return False
    return bool(rule.lib_id or rule.value or rule.ref_prefix)


def suggest_footprints(
    ctx: ToolContext,
    install: Any,
    config: Any,
    project: Project,
    symbols: list[dict[str, Any]],
    limit: int = 3,
) -> tuple[dict[str, list[dict[str, Any]]], str | None]:
    """`{ref: [{fpid, name, description, pad_count, score}]}` from the LibIndex searched with the
    symbol's footprint filters; a note when suggestions were not possible."""
    if not symbols:
        return {}, None
    try:
        resolver = lib_resolver(ctx, install, config, project)
        index = call_with_accepted(
            backend(ctx, "lib_index"),
            install,
            config,
            ctx.services.storage_home,
            project_dir=project.dir,
            resolver=resolver,
        )
    except KiwrapError as err:
        if err.code != "E_UNSUPPORTED":
            raise
        return {}, f"suggestions unavailable: {err.message}"
    ensure = getattr(index, "ensure", None)
    if callable(ensure):
        # the index is built lazily (once per install, ~7 s; a no-op afterwards) - the same call
        # kw_lib_search makes before searching
        try:
            ensure()
        except Exception as exc:  # noqa: BLE001 - suggestions are a convenience, never fatal
            return {}, f"suggestions unavailable: library index build failed: {exc}"
    out: dict[str, list[dict[str, Any]]] = {}
    by_lib: dict[str, tuple[list[str], int | None]] = {}
    for s in symbols:
        lib_id = s["lib_id"]
        if lib_id in by_lib:
            continue
        try:
            sym = resolver.symbol(lib_id)
            filters = [str(f) for f in (getattr(sym, "footprint_filters", None) or [])]
            pin_count = getattr(sym, "pin_count", None)
            by_lib[lib_id] = (filters, int(pin_count) if isinstance(pin_count, int) else None)
        except Exception as exc:  # noqa: BLE001 - a symbol the resolver cannot load simply gets no suggestion
            by_lib[lib_id] = ([], None)
            out.setdefault("_errors", []).append({"lib_id": lib_id, "error": str(exc)})
    cache: dict[str, list[dict[str, Any]]] = {}
    for s in symbols:
        filters, pin_count = by_lib.get(s["lib_id"]) or ([], None)
        if s["lib_id"] not in cache:
            cache[s["lib_id"]] = _footprint_hits(index, filters, s["value"], pin_count, limit)
        out[str(s["ref"])] = cache[s["lib_id"]]
    return out, None


def _footprint_hits(
    index: Any, filters: list[str], value: str, pin_count: int | None, limit: int
) -> list[dict[str, Any]]:
    """Footprints for a symbol's `ki_fp_filters` (KiCad's chooser semantics: anchored wildcards,
    exact pad-count matches first) through `LibIndex.footprints_for`; a free-text `search` on the
    filters / the value when the index has no such method."""
    hits: list[dict[str, Any]] = []
    fp_for = getattr(index, "footprints_for", None)
    if callable(fp_for) and filters:
        try:
            found = fp_for(filters, pin_count=pin_count, limit=limit)
        except Exception:  # noqa: BLE001 - the index is a convenience
            found = []
        for h in found or []:
            r = rec(h)
            hits.append(
                {
                    "fpid": pick(r, "id", "fpid"),
                    "name": str(pick(r, "id", "fpid") or "").split(":")[-1],
                    "description": pick(r, "description", default=""),
                    "pad_count": pick(r, "pad_count", "pin_count"),
                    "score": 1.0 if pick(r, "pin_count_match", default=True) else 0.5,
                    "filter": ", ".join(filters),
                }
            )
        if hits:
            return hits[:limit]
    for flt in filters or [value]:
        if not flt:
            continue
        query = flt.replace("*", " ").replace("?", " ").strip()
        try:
            found = index.search("footprint", query, None, limit)
        except Exception:  # noqa: BLE001 - the index is a convenience
            continue
        for h in found or []:
            r = rec(h)
            hits.append(
                {
                    "fpid": pick(r, "id", "fpid"),
                    "name": pick(r, "name"),
                    "description": pick(r, "description", default=""),
                    "pad_count": pick(r, "pad_count", "pin_count"),
                    "score": pick(r, "score"),
                    "filter": flt,
                }
            )
        if len(hits) >= limit:
            break
    return hits[:limit]


def sch_assign_footprints(ctx: ToolContext, args: SchAssignFootprintsArgs) -> Receipt:
    project = Project.from_path(args.project)
    if not args.mapping and not args.rules:
        raise KiwrapError(
            "E_BAD_ARGS",
            "pass `mapping` ({ref: fpid}) and/or `rules`",
            fix="mapping={'R1': 'Resistor_SMD:R_0603_1608Metric'} or rules=[{ref_prefix: 'R', fpid: ...}]",
        )
    with mutation(ctx, project, "kw_sch_assign_footprints", gate=False) as m:
        _need_root(project)
        install, caps, config = _install(ctx, m)
        symbols = [s for s in symbols_of(hierarchy(ctx, project)) if not s["power"]]
        mapping: dict[str, str] = dict(args.mapping or {})
        for rule in args.rules or []:
            for s in symbols:
                ref = str(s["ref"] or "")
                if not ref or ref in mapping or ref.endswith("?"):
                    continue
                if s["footprint"] and not args.overwrite:
                    continue
                if _rule_matches(rule, s):
                    mapping[ref] = rule.fpid
        known = {str(s["ref"]) for s in symbols}
        unknown = sorted(r for r in mapping if known and r not in known)
        if unknown:
            raise KiwrapError(
                "E_BAD_ARGS",
                f"unknown references: {', '.join(unknown)}",
                fix="annotate first (kw_sch_annotate) and use references kw_sch_get lists",
                candidates=sorted(known, key=ref_key)[:50],
            )
        bad_fpids = (
            {} if args.allow_unknown else check_fpids(ctx, install, config, project, mapping.values(), m)
        )
        rejected = {ref: fpid for ref, fpid in mapping.items() if fpid in bad_fpids}
        mapping = {ref: fpid for ref, fpid in mapping.items() if fpid not in bad_fpids}

        def via_file() -> Any:
            w = schematic_writer(ctx, install, caps, config, project)
            assigned = list(call_with_accepted(w.assign_footprints, mapping) or []) if mapping else []
            if mapping:
                _saved(m, w.save())
            return assigned

        outcome = run_write("schematic.write", caps, install, ipc=None, file=via_file, prefer=None)
        assigned = [str(r) for r in outcome.value]
        m.receipt.changed["updated"].extend(sorted(assigned, key=ref_key))
        apply_outcome(m, outcome, install, ctx)
        after = {**{str(s["ref"]): s["footprint"] for s in symbols}, **{r: mapping[r] for r in assigned}}
        unresolved = [
            s for s in symbols if s["ref"] and (not after.get(str(s["ref"])) or str(s["ref"]) in rejected)
        ]
        suggestions: dict[str, list[dict[str, Any]]] = {}
        note = None
        if args.suggest and unresolved:
            suggestions, note = suggest_footprints(ctx, install, config, project, unresolved)
            if note:
                m.receipt.warnings.append(note)
                m.receipt.degraded.append("suggestions_unavailable")
        unresolved_out: list[dict[str, Any]] = []
        for s in unresolved:
            ref = str(s["ref"])
            entry: dict[str, Any] = {
                "ref": s["ref"],
                "lib_id": s["lib_id"],
                "value": s["value"],
                "suggestions": suggestions.get(ref, []),
            }
            if ref in rejected:
                bad = bad_fpids[rejected[ref]]
                entry["requested_fpid"] = rejected[ref]
                entry["reason"] = bad["reason"]
                entry["suggestions"] = [
                    {"fpid": c, "name": c.split(":")[-1], "description": "", "pad_count": None, "score": 0.9}
                    for c in bad["candidates"]
                ] + [x for x in entry["suggestions"] if x.get("fpid") not in set(bad["candidates"])]
            unresolved_out.append(entry)
        m.receipt.data.update(
            {
                "ok": not unresolved,
                "assigned": {r: mapping[r] for r in assigned},
                "unresolved": unresolved_out,
                "rejected": rejected,
                "suggestion_errors": suggestions.get("_errors", []),
            }
        )
        if rejected:
            m.receipt.warnings.append(
                f"{len(rejected)} footprint id(s) are not in the library tables and were NOT written: "
                + "; ".join(
                    f"{ref} -> {fpid}: {bad_fpids[fpid]['reason']}"
                    for ref, fpid in sorted(rejected.items())[:10]
                )
                + " (allow_unknown=true writes them anyway)"
            )
        m.summary = f"{len(assigned)} assigned, {len(unresolved)} unresolved"
        after_schematic_write(ctx, m, install, caps, config, project, validate=args.erc)
        if unresolved:
            m.receipt.next_suggested = "kw_sch_assign_footprints"
        elif m.receipt.data.get("erc") and m.receipt.data["erc"]["ok"]:
            m.receipt.next_suggested = "kw_pcb_sync"
        return m.receipt


SCH_ASSIGN_FOOTPRINTS = ToolSpec(
    name="kw_sch_assign_footprints",
    title="Assign footprints",
    description=(
        "Set the Footprint property from an explicit mapping {ref: fpid} and/or rules (lib_id / value / "
        "ref_prefix -> fpid, applied to symbols without a footprint unless overwrite). Every fpid is resolved "
        "through the fp-lib-table first: an unknown one is not written and comes back in data.unresolved with "
        "its reason and candidates (allow_unknown=true overrides). Symbols still without a footprint come back "
        "in data.unresolved with LibIndex suggestions from their footprint filters. ok = nothing unresolved; "
        "then kw_pcb_sync."
    ),
    input_model=SchAssignFootprintsArgs,
    handler=sch_assign_footprints,
    annotations=MUTATING,
)


SCH_AUTHOR_TOOLS: tuple[ToolSpec, ...] = (
    SCH_ADD_SYMBOL,
    SCH_CONNECT,
    SCH_ADD_ITEM,
    SCH_EDIT,
    SCH_ANNOTATE,
    SCH_ASSIGN_FOOTPRINTS,
)

__all__ = [
    "SCH_ADD_ITEM",
    "SCH_ADD_SYMBOL",
    "SCH_ANNOTATE",
    "SCH_ASSIGN_FOOTPRINTS",
    "SCH_AUTHOR_TOOLS",
    "SCH_CONNECT",
    "SCH_EDIT",
]
