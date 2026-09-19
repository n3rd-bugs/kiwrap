"""kw_sch_get: the schematic as structured JSON (millimetres): symbols with ABSOLUTE pin endpoints
and nets, wires, labels, junctions, no-connects, sheets, the sheet hierarchy and the netlist.

Providers (kiwrap.core.planner `schematic.read`): `ipc11` - headless `kicad-cli api-server`
(verified on the installed nightly: `get_schematic()` answers get_symbols / get_lines /
get_labels / get_junctions / get_no_connects / get_sheet_symbols / get_netlist / get_hierarchy) -
then `file` (the CST reader of the whole sheet tree). The netlist (`kinds=['netlist']`) is its
own planner operation: ipc11 -> kicad-cli `sch export netlist` -> file estimate
(`degraded: ['netlist_estimated']`).

Items (`kinds`): symbols, wires, labels, junctions, no_connects, sheets go to `data.items`
(flat, `kind` on every item, deterministic order, chunked); hierarchy and netlist are singletons.
Filters: `refs`, `nets` (a symbol matches when any pin is on the net; a wire / label / junction
by its net - filled from `GetSchematicNetlist` on ipc11 and from `pin_connectivity` on the file
provider), `sheets` (sheet path or file name), `bbox_mm`. `detail`: summary (identity +
position), normal (+ pins with absolute endpoints and nets), full (+ fields/properties).
Sheet paths are the human-readable instance paths (`/`, `/CH1/`) on both providers; a sheet file
placed N times yields N items per wire/symbol, one per instance (reviewer findings 1/12).
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from kiwrap.core import planner
from kiwrap.core.state import KiwrapError, Receipt
from kiwrap.core.state.project import Project
from kiwrap.core.state.receipts import ReceiptOut
from kiwrap.core.tools._base import READ_ONLY, ToolContext, ToolSpec, read_state
from kiwrap.core.tools._data import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    PIN_TYPES,
    Detail,
    Filters,
    canonical,
    chunk_by_budget,
    count_by,
    external_tags,
    matches,
    mm,
    mm_pair,
    net_name,
    pick,
    prune,
    rec,
    ref_key,
    sheet_key,
)
from kiwrap.core.tools.pcb_get import ProviderArg, ipc_session

SchKind = Literal["symbols", "wires", "labels", "junctions", "no_connects", "sheets", "hierarchy", "netlist"]
LIST_KINDS: tuple[str, ...] = ("symbols", "wires", "labels", "junctions", "no_connects", "sheets")

KEEP: dict[str, dict[str, tuple[str, ...]]] = {
    "symbol": {
        "summary": ("kind", "ref", "value", "lib_id", "unit", "sheet", "x_mm", "y_mm", "rotation_deg"),
        "normal": (
            "kind",
            "ref",
            "value",
            "lib_id",
            "unit",
            "sheet",
            "x_mm",
            "y_mm",
            "rotation_deg",
            "mirror",
            "footprint",
            "dnp",
            "power",
            "pins",
            "nets",
            "uuid",
        ),
    },
    "wire": {
        "summary": ("kind", "sheet", "start_mm", "end_mm"),
        "normal": ("kind", "sheet", "start_mm", "end_mm", "net", "uuid"),
    },
    "label": {
        "summary": ("kind", "label_kind", "text", "sheet", "x_mm", "y_mm"),
        "normal": ("kind", "label_kind", "text", "sheet", "x_mm", "y_mm", "net", "uuid"),
    },
    "junction": {
        "summary": ("kind", "sheet", "x_mm", "y_mm"),
        "normal": ("kind", "sheet", "x_mm", "y_mm", "net", "uuid"),
    },
    "no_connect": {
        "summary": ("kind", "sheet", "x_mm", "y_mm"),
        "normal": ("kind", "sheet", "x_mm", "y_mm", "uuid"),
    },
    "sheet": {
        "summary": ("kind", "name", "file", "sheet", "x_mm", "y_mm"),
        "normal": ("kind", "name", "file", "sheet", "path", "x_mm", "y_mm", "size_mm", "pins", "uuid"),
    },
}


def ipc_schematic(ctx: ToolContext, install: Any, config: Any, sch: Path) -> Any:
    with ipc_session(ctx, install, config, sch) as s:
        return ctx.services.ipc_read_schematic(s)


# ---- normalisation ----------------------------------------------------------------------------------


def _xy(r: dict[str, Any]) -> tuple[float | None, float | None]:
    pos = mm_pair(pick(r, "position", "at"))
    if pos is not None:
        return pos[0], pos[1]
    return mm(pick(r, "x_mm", "x")), mm(pick(r, "y_mm", "y"))


LABEL_KINDS = {
    "label": "local",
    "global_label": "global",
    "hierarchical_label": "hierarchical",
    "netclass_flag": "netclass",
    "directive_label": "directive",
    "directive": "directive",
    "sheet_pin": "sheet_pin",
}


def pin_connected(net: str | None) -> bool | None:
    """`connected`: the pin has a net that is not KiCad's `unconnected-(...)` placeholder; None
    while the net is unknown (file provider before the netlist join)."""
    if net is None:
        return None
    return not net.startswith("unconnected-")


def norm_pin(p: Any) -> dict[str, Any]:
    r = rec(p)
    x, y = _xy(r)
    net = net_name(pick(r, "net", "net_name"))
    return {
        "number": str(pick(r, "number", "num", default="")),
        "name": pick(r, "name", default=""),
        "type": canonical(pick(r, "type", "electrical_type", "etype"), PIN_TYPES),
        "x_mm": x,
        "y_mm": y,
        "net": net,
        "connected": pin_connected(net),
        "hidden": bool(pick(r, "hidden", default=False)),
    }


def _mirror(r: dict[str, Any]) -> str | None:
    m = pick(r, "mirror")
    if m is not None:
        return str(m) if m else None
    if pick(r, "mirror_x", default=False) and pick(r, "mirror_y", default=False):
        return "xy"
    if pick(r, "mirror_x", default=False):
        return "x"
    if pick(r, "mirror_y", default=False):
        return "y"
    return None


def norm_symbol(s: Any, *, sheet: str | None = None, path: str | None = None) -> dict[str, Any]:
    """`sheet`/`path`: the sheet instance the symbol is read for (file reader: reference, unit and
    pins are per instance path)."""
    r = rec(s)
    if path is not None and callable(getattr(s, "pins_for", None)):
        raw_pins = s.pins_for(path)
        ref = s.reference_for(path)
        unit = s.unit_for(path)
    else:
        raw_pins = pick(r, "pins", default=[]) or []
        ref = pick(r, "ref", "reference", "refdes")
        unit = int(pick(r, "unit", default=1) or 1)
    pins = [norm_pin(p) for p in raw_pins]
    pins.sort(key=lambda p: ref_key(p["number"]))
    x, y = _xy(r)
    return {
        "kind": "symbol",
        "ref": ref,
        "value": pick(r, "value"),
        "lib_id": pick(r, "lib_id", "libid"),
        "unit": unit,
        "sheet": sheet or pick(r, "sheet", "sheet_path", default="/"),
        "x_mm": x,
        "y_mm": y,
        "rotation_deg": mm(pick(r, "rotation_deg", "rotation", "orientation", default=0.0)),
        "mirror": _mirror(r),
        "footprint": pick(r, "footprint") or None,
        "dnp": bool(pick(r, "dnp", "do_not_populate", default=False)),
        "power": bool(pick(r, "power", "is_power", default=False)),
        "pins": pins,
        "nets": sorted({p["net"] for p in pins if p["net"]}),
        "uuid": pick(r, "uuid", "id"),
        "fields": dict(pick(r, "fields", "properties", default={}) or {}),
    }


def norm_wire(w: Any, *, sheet: str | None = None, kind: str = "wire") -> dict[str, Any]:
    r = rec(w)
    return {
        "kind": "wire",
        "line_kind": pick(r, "line_kind", "kind", default=kind),
        "sheet": sheet or pick(r, "sheet", "sheet_path", default="/"),
        "start_mm": mm_pair(pick(r, "start_mm", "start")),
        "end_mm": mm_pair(pick(r, "end_mm", "end")),
        "net": net_name(pick(r, "net", "net_name")),
        "uuid": pick(r, "uuid", "id"),
    }


def norm_label(lb: Any, *, sheet: str | None = None) -> dict[str, Any]:
    r = rec(lb)
    x, y = _xy(r)
    raw_kind = str(pick(r, "label_kind", "kind", "type", default="local"))
    return {
        "kind": "label",
        "label_kind": LABEL_KINDS.get(raw_kind, raw_kind),
        "text": pick(r, "text", "name", default=""),
        "sheet": sheet or pick(r, "sheet", "sheet_path", default="/"),
        "x_mm": x,
        "y_mm": y,
        "net": net_name(pick(r, "net", "net_name")),
        "shape": pick(r, "shape") or None,
        "uuid": pick(r, "uuid", "id"),
    }


def norm_point(kind: str, j: Any, *, sheet: str | None = None) -> dict[str, Any]:
    r = rec(j)
    x, y = _xy(r)
    return {
        "kind": kind,
        "sheet": sheet or pick(r, "sheet", "sheet_path", default="/"),
        "x_mm": x,
        "y_mm": y,
        "net": net_name(pick(r, "net", "net_name")),
        "uuid": pick(r, "uuid", "id"),
    }


def norm_sheet(s: Any, *, sheet: str | None = None, path: str | None = None) -> dict[str, Any]:
    r = rec(s)
    x, y = _xy(r)
    pins = []
    for p in pick(r, "pins", default=[]) or []:
        pr = rec(p)
        px, py = _xy(pr)
        pins.append({"name": pick(pr, "name", "text", default=""), "x_mm": px, "y_mm": py})
    pins.sort(key=lambda p: (p["name"], p["x_mm"] or 0.0, p["y_mm"] or 0.0))
    name = pick(r, "name", "sheet_name", default="")
    parent = sheet or pick(r, "sheet", "parent", "sheet_path", default="/")
    return {
        "kind": "sheet",
        "name": name,
        "file": pick(r, "file", "file_name", "filename", default=""),
        "sheet": parent,
        "path": path or pick(r, "path") or f"{parent.rstrip('/')}/{name}/",
        "x_mm": x,
        "y_mm": y,
        "size_mm": mm_pair(pick(r, "size_mm", "size")),
        "pins": pins,
        "uuid": pick(r, "uuid", "id"),
    }


def norm_hierarchy(h: Any) -> dict[str, Any] | None:
    if h is None:
        return None
    r = rec(h)
    children: list[dict[str, Any]] = []
    for c in pick(r, "children", "sheets", default=[]) or []:
        child = norm_hierarchy(c)
        if child:
            children.append(child)
    children.sort(key=lambda c: (c["name"], c["file"], c["path"]))
    return {
        "path": pick(r, "path", default="/"),
        "name": pick(r, "name", default=""),
        "file": pick(r, "file", "file_name", "filename", default=""),
        "children": children,
    }


def hierarchy_from_instances(instances: list[Any]) -> dict[str, Any] | None:
    """The tree from the file reader's flat `SheetInstance` list (parent links, depth-first)."""
    nodes: dict[int, dict[str, Any]] = {}
    root: dict[str, Any] | None = None
    for inst in instances:
        r = rec(inst)
        file = pick(r, "file")
        file_name = str(getattr(getattr(file, "path", None), "name", "") or "")
        node = {
            "path": pick(r, "names_path", "path", default="/"),
            "name": (pick(r, "name", default="") or "") or file_name.rsplit(".", 1)[0],
            "file": file_name,
            "children": [],
        }
        nodes[id(inst)] = node
        parent = pick(r, "parent")
        if parent is None:
            root = root or node
        elif id(parent) in nodes:
            nodes[id(parent)]["children"].append(node)
    return root


def norm_netlist(nl: Any) -> dict[str, Any]:
    """`{nets: [{name, code, nodes: [{ref, pin, sheet}]}], components: n}` from a reader/IPC/CLI
    netlist (any of the three contracts: `nets[]` with `nodes[]`)."""
    if isinstance(nl, (list, tuple)):  # the IPC reader: a tuple of SchematicNetModel
        r: dict[str, Any] = {"nets": list(nl)}
    else:
        r = rec(nl)
    nets = []
    for n in pick(r, "nets", default=[]) or []:
        nr = rec(n)
        nodes = []
        seen: set[tuple[str, str, str | None]] = set()
        for node in pick(nr, "nodes", "pins", "members", "items", default=[]) or []:
            xr = rec(node)
            ref = pick(xr, "ref", "reference")
            if not ref:  # IPC `NetItemRef`s also list wires/junctions/labels; only pins are nodes
                continue
            entry = {
                "ref": ref,
                "pin": str(pick(xr, "pin", "number", "pin_number", default="")),
                "sheet": pick(xr, "sheet", "sheet_path"),
            }
            # GetSchematicNetlist lists a unit-0 (shared) pin KIID once per unit instance of a
            # multi-unit symbol (multichannel IC1.8 twice); kicad-cli lists (ref, pin) once
            key = (str(entry["ref"]), entry["pin"], entry["sheet"])
            if key in seen:
                continue
            seen.add(key)
            nodes.append(entry)
        nodes.sort(key=lambda x: (ref_key(x["ref"]), ref_key(x["pin"])))
        nets.append({"name": pick(nr, "name", "net", default=""), "code": pick(nr, "code"), "nodes": nodes})
    nets.sort(key=lambda n: (n["name"],))
    comps = pick(r, "components", default=None)
    return {"nets": nets, "net_count": len(nets), "components": len(comps) if comps is not None else None}


def _sort(lists: dict[str, list[dict[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
    pkey = lambda j: (j["sheet"], j["x_mm"] or 0.0, j["y_mm"] or 0.0, j["uuid"] or "")  # noqa: E731
    lists["symbols"].sort(key=lambda s: (s["sheet"], ref_key(s["ref"]), s["unit"], s["uuid"] or ""))
    lists["wires"].sort(
        key=lambda w: (w["sheet"], w["start_mm"] or [0, 0], w["end_mm"] or [0, 0], w["uuid"] or "")
    )
    lists["labels"].sort(
        key=lambda x: (x["sheet"], x["text"], x["x_mm"] or 0.0, x["y_mm"] or 0.0, x["uuid"] or "")
    )
    lists["junctions"].sort(key=pkey)
    lists["no_connects"].sort(key=pkey)
    lists["sheets"].sort(key=lambda s: (s["sheet"], s["name"], s["file"], s["uuid"] or ""))
    return lists


def _flatten_hierarchy(hier: Any) -> dict[str, list[dict[str, Any]]]:
    """The file reader's `Hierarchy` (`instances` of `SheetInstance`, each holding a
    `SchematicFile`): one item list per kind with the instance's `names_path` as `sheet` and
    per-instance references / units / pins. Buses are listed as wires with `line_kind: bus`."""
    lists: dict[str, list[dict[str, Any]]] = {k: [] for k in LIST_KINDS}
    for inst in hier.instances:
        r = rec(inst)
        sheet = str(pick(r, "names_path", "path", default="/"))
        path = str(pick(r, "path", default="/"))
        f = rec(pick(r, "file"))
        for s in pick(f, "symbols", default=[]) or []:
            lists["symbols"].append(norm_symbol(s, sheet=sheet, path=path))
        for w in pick(f, "wires", default=[]) or []:
            lists["wires"].append(norm_wire(w, sheet=sheet))
        for b in pick(f, "buses", default=[]) or []:
            lists["wires"].append(norm_wire(b, sheet=sheet, kind="bus"))
        for lb in pick(f, "labels", default=[]) or []:
            lists["labels"].append(norm_label(lb, sheet=sheet))
        for j in pick(f, "junctions", default=[]) or []:
            lists["junctions"].append(norm_point("junction", j, sheet=sheet))
        for nc in pick(f, "no_connects", default=[]) or []:
            lists["no_connects"].append(norm_point("no_connect", nc, sheet=sheet))
        for sh in pick(f, "sheets", default=[]) or []:
            name = pick(rec(sh), "name", default="")
            lists["sheets"].append(norm_sheet(sh, sheet=sheet, path=f"{sheet}{name}/"))
    return _sort(lists)


def resolve_sheet_filters(
    requested: tuple[str, ...], lists: dict[str, list[dict[str, Any]]], root_sch: Path
) -> tuple[str, ...]:
    """The `sheets` filter accepts an instance path (`/blinker/`), a sheet name, a file stem
    (`blinker`, the spelling every writer takes) or a file name (`blinker.kicad_sch`); each is
    turned into the instance path(s) it names (a file placed twice gives every instance). A
    spelling that names no sheet is E_BAD_ARGS with the hierarchy paths as candidates instead of
    an empty page (reviewer finding, 2026-09-20)."""
    known: dict[str, set[str]] = {"/": {"/"}}
    root_stem = root_sch.name.removesuffix(".kicad_sch")
    for spelling in (root_stem, root_sch.name):
        known.setdefault(spelling, set()).add("/")
    for sh in lists.get("sheets", []):
        path = sheet_key(sh.get("path") or "/")
        known.setdefault(path, set()).add(path)
        for spelling in (
            str(sh.get("name") or ""),
            str(sh.get("file") or ""),
            str(sh.get("file") or "").removesuffix(".kicad_sch"),
        ):
            if spelling:
                known.setdefault(spelling, set()).add(path)
    resolved: list[str] = []
    unknown: list[str] = []
    for want in requested:
        text = str(want)
        hits = known.get(text) or known.get(sheet_key(text)) or known.get(text.strip("/"))
        if not hits:
            unknown.append(text)
            continue
        resolved.extend(h for h in sorted(hits) if h not in resolved)
    if unknown:
        paths = sorted({p for hits in known.values() for p in hits})
        raise KiwrapError(
            "E_BAD_ARGS",
            f"no sheet named {', '.join(repr(u) for u in unknown)} in this schematic",
            why="sheets are addressed by instance path, sheet name, file stem or file name",
            fix="pass one of the candidates (kw_sch_get(kinds=['hierarchy']) lists every instance path)",
            candidates=paths,
            example="sheets=['/blinker/'] or sheets=['blinker']",
        )
    return tuple(resolved)


def schematic_items(sch: Any) -> dict[str, list[dict[str, Any]]]:
    """Every list kind, normalised and deterministically ordered, from an IPC `SchematicModel`,
    a reader-contract record, or the file reader's `Hierarchy`."""
    if hasattr(sch, "instances") and not isinstance(sch, dict):
        return _flatten_hierarchy(sch)
    r = rec(sch)
    lists: dict[str, list[dict[str, Any]]] = {
        "symbols": [norm_symbol(s) for s in (pick(r, "symbols", default=[]) or [])],
        "wires": [norm_wire(w) for w in (pick(r, "wires", "lines", default=[]) or [])],
        "labels": [norm_label(x) for x in (pick(r, "labels", default=[]) or [])],
        "junctions": [norm_point("junction", j) for j in (pick(r, "junctions", default=[]) or [])],
        "no_connects": [norm_point("no_connect", j) for j in (pick(r, "no_connects", default=[]) or [])],
        "sheets": [norm_sheet(s) for s in (pick(r, "sheets", "sheet_symbols", default=[]) or [])],
    }
    return _sort(lists)


def schematic_hierarchy(
    sch: Any, lists: dict[str, list[dict[str, Any]]], root_name: str, root_file: str
) -> dict[str, Any]:
    """`data.hierarchy`: the reader's tree when it has one (IPC `SheetInfo`, or the file reader's
    instance list), else one level built from the root's sheet symbols."""
    if hasattr(sch, "instances") and not isinstance(sch, dict):
        tree = hierarchy_from_instances(list(sch.instances))
        if tree:
            return tree
    tree = norm_hierarchy(pick(rec(sch), "hierarchy"))
    if tree:
        return tree
    return {
        "path": "/",
        "name": root_name,
        "file": root_file,
        "children": [
            {"path": s["path"], "name": s["name"], "file": s["file"], "children": []}
            for s in lists["sheets"]
            if s["sheet"] == "/"
        ],
    }


def fill_item_nets(
    ctx: ToolContext, model: Any, lists: dict[str, list[dict[str, Any]]], warnings: list[str]
) -> None:
    """File provider: put the derived net on wires / labels / junctions from
    `pin_connectivity(...).item_nets` ((sheet names path, uuid) -> net). The IPC reader fills the
    same field from `GetSchematicNetlist`; without this the `nets` filter silently dropped every
    wire and label of the file provider (reviewer finding, 2026-09-19)."""
    try:
        conn = ctx.services.pin_connectivity(model)
        item_nets: dict[tuple[str, str], str] = dict(getattr(conn, "item_nets", {}) or {})
    except Exception as exc:  # noqa: BLE001 - nets are an enrichment, the items stay
        warnings.append(f"wire/label nets unavailable on this provider: {exc}")
        return
    if not item_nets:
        warnings.append("wire/label nets unavailable on this provider")
        return
    for kind in ("wires", "labels", "junctions"):
        for it in lists[kind]:
            if it.get("net") is None and it.get("uuid"):
                it["net"] = item_nets.get((str(it.get("sheet") or "/"), str(it["uuid"])))


# ---- shared reads (kw_sch_stats reuses these) --------------------------------------------------------


def read_schematic(
    ctx: ToolContext, install: Any, caps: Any, config: Any, sch: Path, *, prefer: str | None
) -> planner.Outcome[Any]:
    return planner.run(
        "schematic.read",
        caps,
        {
            planner.IPC11: lambda: ipc_schematic(ctx, install, config, sch),
            planner.FILE: lambda: ctx.services.file_read_schematic(sch),
        },
        install=install,
        prefer=prefer,
    )


def read_netlist(
    ctx: ToolContext, install: Any, caps: Any, config: Any, sch: Path, *, ipc_model: Any, prefer: str | None
) -> planner.Outcome[dict[str, Any]]:
    def from_ipc() -> dict[str, Any]:
        model = ipc_model if ipc_model is not None else ipc_schematic(ctx, install, config, sch)
        nl = pick(rec(model), "netlist")
        if nl is None:
            raise KiwrapError(
                "E_UNSUPPORTED",
                "the IPC reader returned no netlist",
                data={"capability": "ipc.netlist"},
            )
        return norm_netlist(nl)

    return planner.run(
        "netlist",
        caps,
        {
            planner.IPC11: from_ipc,
            planner.CLI: lambda: norm_netlist(ctx.services.export_netlist(install, config, sch)),
            planner.FILE: lambda: norm_netlist(
                ctx.services.netlist_from_file(
                    ctx.services.file_read_schematic(sch),
                    # `(pinfunction "<name>_<number>")` on KiCad 10+, the bare name before
                    pin_function_style="name_number"
                    if caps.has("cli.netlist_pinfunction_suffix")
                    else "name",
                )
            ),
        },
        install=install,
        prefer=prefer,
    )


# ---- the tool ---------------------------------------------------------------------------------------


class SchGetArgs(BaseModel):
    project: str = Field(description="project directory or .kicad_pro path")
    kinds: list[SchKind] = Field(
        default=["symbols", "hierarchy"],
        description=(
            "what to return: symbols (with absolute pin endpoints + nets), wires, labels, junctions, no_connects, "
            "sheets (items, chunked) and hierarchy, netlist (singletons)"
        ),
    )
    refs: list[str] | None = Field(default=None, description="only these reference designators")
    nets: list[str] | None = Field(default=None, description="only symbols/labels/wires on these nets")
    sheets: list[str] | None = Field(
        default=None,
        description="only items on these sheets: instance path ('/blinker/'), sheet name, file stem or file name",
    )
    bbox_mm: list[float] | None = Field(
        default=None, description="[x1, y1, x2, y2] in mm (sheet coordinates)"
    )
    detail: Detail = Field(default="normal", description="summary | normal (+ pins) | full (+ fields)")
    cursor: str | None = Field(default=None, description="continue a chunked listing")
    limit: int = Field(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT, description="items per page")
    provider: ProviderArg = Field(
        default="auto", description="auto (ipc11 when available, else file), or force one"
    )


class SchGetData(BaseModel):
    ok: bool = True
    schematic: str
    kinds: list[str]
    filters: dict[str, Any]
    detail: str
    counts: dict[str, int]
    total: int
    returned: int
    next_cursor: str | None = None
    items: list[dict[str, Any]]
    hierarchy: dict[str, Any] | None = None
    netlist: dict[str, Any] | None = None
    plan: dict[str, Any]


class SchGetOut(ReceiptOut):
    data: SchGetData  # type: ignore[assignment]


def sch_get(ctx: ToolContext, args: SchGetArgs) -> Receipt:
    project = Project.from_path(args.project)
    state, external = read_state(project)
    if not project.root_sch.exists():
        raise KiwrapError(
            "E_PROJECT_NOT_FOUND",
            f"root schematic {project.root_sch.name} is missing",
            fix="kw_project create writes it; if you renamed files, keep the .kicad_pro stem",
        )
    install = ctx.services.install(state.target_kicad)
    caps = ctx.services.caps(install)
    config = ctx.services.config_root(install)
    filters = Filters.from_args(args)
    prefer = None if args.provider == "auto" else args.provider

    outcome = read_schematic(ctx, install, caps, config, project.root_sch, prefer=prefer)
    model = outcome.value
    degraded, warnings = external_tags(external) + list(outcome.degraded), list(outcome.warnings)
    lists = schematic_items(model)
    if filters.sheets:
        filters = replace(filters, sheets=resolve_sheet_filters(filters.sheets, lists, project.root_sch))
    if outcome.provider_name == planner.FILE and (
        filters.nets or any(k in args.kinds for k in ("wires", "labels", "junctions"))
    ):
        fill_item_nets(ctx, model, lists, warnings)
    netlist: dict[str, Any] | None = None
    nl_outcome: planner.Outcome[dict[str, Any]] | None = None
    # the file reader knows pin positions but not nets (no pin carries one); the IPC reader does
    all_pins = [p for s in lists["symbols"] for p in s["pins"]]
    want_pin_nets = "symbols" in args.kinds and bool(all_pins) and not any(p["net"] for p in all_pins)
    if "netlist" in args.kinds or want_pin_nets:
        try:
            nl_outcome = read_netlist(
                ctx,
                install,
                caps,
                config,
                project.root_sch,
                ipc_model=model if outcome.provider_name == planner.IPC11 else None,
                prefer=prefer,
            )
        except KiwrapError as err:  # pins stay without nets; the netlist kind reports the reason
            if err.code != "E_UNSUPPORTED" or "netlist" in args.kinds:
                raise
            warnings.append(f"pin nets unavailable: {err.message}")
        else:
            netlist = nl_outcome.value
            for d in nl_outcome.degraded:
                if d not in degraded:
                    degraded.append(d)
            warnings.extend(nl_outcome.warnings)
    if want_pin_nets and netlist is not None:  # the file reader knows pins, not nets: join them
        by_pin = {(n["ref"], n["pin"]): net["name"] for net in netlist["nets"] for n in net["nodes"]}
        for sym in lists["symbols"]:
            for pin in sym["pins"]:
                if pin["net"] is None:
                    pin["net"] = by_pin.get((sym["ref"], pin["number"]))
                    pin["connected"] = pin_connected(pin["net"])
            sym["nets"] = sorted({p["net"] for p in sym["pins"] if p["net"]})
    if filters.nets:
        for kind in ("wires", "labels"):
            if kind in args.kinds and lists[kind] and not any(it.get("net") for it in lists[kind]):
                warnings.append(f"{kind} carry no net on this provider; the nets filter drops them all")
    items: list[dict[str, Any]] = []
    for kind in LIST_KINDS:
        if kind in args.kinds:
            items.extend(it for it in lists[kind] if matches(it, filters))
    counts = count_by(items, "kind")
    paged = chunk_by_budget(
        items, args.cursor, args.limit, render=lambda it: prune(it, args.detail, KEEP[it["kind"]])
    )
    page, nxt = paged.items, paged.next_cursor
    if paged.warning(args.limit):
        warnings.append(paged.warning(args.limit) or "")
    data: dict[str, Any] = {
        "ok": True,
        "schematic": project.root_sch.name,
        "kinds": list(args.kinds),
        "filters": filters.to_dict(),
        "detail": args.detail,
        "counts": counts,
        "total": len(items),
        "returned": len(page),
        "next_cursor": nxt,
        "page_chars": paged.chars,
        "items": page,
        "hierarchy": None,
        "netlist": None,
        "plan": outcome.plan.to_dict(),
    }
    if "hierarchy" in args.kinds:
        data["hierarchy"] = schematic_hierarchy(model, lists, project.name, project.root_sch.name)
    if "netlist" in args.kinds and netlist is not None and nl_outcome is not None:
        nl = nl_outcome
        netlist = dict(netlist)
        if filters.nets or filters.refs:
            netlist["nets"] = [
                n
                for n in netlist["nets"]
                if (not filters.nets or n["name"] in filters.nets)
                and (not filters.refs or any(x["ref"] in filters.refs for x in n["nodes"]))
            ]
        netlist["provider"] = nl.provider_used(install)
        npage = chunk_by_budget(netlist["nets"], args.cursor, args.limit)
        netlist["nets_total"] = len(netlist["nets"])
        netlist["nets"] = npage.items
        netlist["nets_returned"] = len(npage.items)
        netlist["truncated"] = len(npage.items) < netlist["nets_total"]
        netlist["next_cursor"] = npage.next_cursor
        if npage.next_cursor is not None:
            data["next_cursor"] = data["next_cursor"] or npage.next_cursor
            warnings.append(
                f"netlist.nets lists {len(npage.items)} of {netlist['nets_total']} nets; continue with "
                f"cursor={npage.next_cursor!r} (cursor/limit page items and nets alike)"
            )
        data["netlist"] = netlist
    return Receipt(
        provider_used=outcome.provider_used(install),
        degraded=degraded,
        warnings=warnings,
        stage=state.stage,
        data=data,
    )


SPEC = ToolSpec(
    name="kw_sch_get",
    title="Schematic data",
    description=(
        "Structured schematic state in mm (primary feedback; prefer this over kw_sch_render): symbols with "
        "absolute pin endpoints and nets, wires, labels, junctions, no-connects, sheets, hierarchy and the "
        "netlist. Filter by refs/nets/sheets/bbox_mm, choose detail, page with cursor/limit."
    ),
    input_model=SchGetArgs,
    output_model=SchGetOut,
    handler=sch_get,
    annotations=READ_ONLY,
)
