"""kw_pcb_get: the board as structured JSON (millimetres), the primary feedback surface for the
agent (docs/DECISIONS.md D17: "the agent's feedback loop is structured data, not images").

Providers (kiwrap.core.planner `board.read`): `ipc11` - a headless `kicad-cli api-server`
session (verified on the installed nightly: `KiCad(headless=True, kicad_cli_path=..., file_path=
<.kicad_pcb>)` spawns the server, `get_board()` answers get_nets / get_footprints / get_pads /
get_tracks / get_vias / get_zones / get_item_bounding_box / get_design_rules; units are integer
nanometres and the reader converts to mm) - then `file` (the lossless CST reader). The receipt
says which one answered (`provider_used`) and what is estimated (`degraded`).

Item kinds (`kinds`): footprints, pads, tracks, vias, zones, nets go to `data.items` (one flat
list, each item carries `kind`, deterministic order, chunked by `cursor`/`limit`); outline,
rules and connectivity are singletons in `data.outline` / `data.rules` / `data.connectivity`.
Filters: `refs`, `nets`, `layers`, `bbox_mm` (an item passes when it intersects). `detail`:
summary (position + identity), normal (+ bbox, uuid, pad counts), full (everything incl. pads
and properties).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from kiwrap.capability import patch_providing
from kiwrap.core import planner
from kiwrap.core.state import KiwrapError, Receipt
from kiwrap.core.state.project import Project
from kiwrap.core.state.receipts import ReceiptOut
from kiwrap.core.tools._base import READ_ONLY, ToolContext, ToolSpec, read_state
from kiwrap.core.tools._data import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    PAD_SHAPES,
    PAD_TYPES,
    Detail,
    Filters,
    box_of,
    canonical,
    chunk_by_budget,
    count_by,
    drill_mm,
    external_tags,
    layer_sort_key,
    matches,
    mm,
    mm_pair,
    net_name,
    pick,
    prune,
    rec,
    ref_key,
    sorted_layers,
    str_list,
)

BoardKind = Literal[
    "footprints", "pads", "tracks", "vias", "zones", "nets", "outline", "rules", "connectivity"
]
LIST_KINDS: tuple[str, ...] = ("footprints", "pads", "tracks", "vias", "zones", "nets")
ProviderArg = Literal["auto", "ipc11", "cli", "file"]  # cli: only the connectivity operation has one

#: fields per detail level (`full` keeps everything)
KEEP: dict[str, dict[str, tuple[str, ...]]] = {
    "footprint": {
        "summary": ("kind", "ref", "value", "layer", "x_mm", "y_mm", "rotation_deg"),
        "normal": (
            "kind",
            "ref",
            "value",
            "lib_id",
            "layer",
            "x_mm",
            "y_mm",
            "rotation_deg",
            "bbox_mm",
            "courtyard_bbox_mm",
            "pad_count",
            "nets",
            "attr",
            "dnp",
            "uuid",
        ),
    },
    "pad": {
        "summary": ("kind", "ref", "number", "net", "x_mm", "y_mm"),
        "normal": (
            "kind",
            "ref",
            "number",
            "net",
            "x_mm",
            "y_mm",
            "size_mm",
            "shape",
            "layers",
            "drill_mm",
            "uuid",
        ),
    },
    "track": {
        "summary": ("kind", "net", "layer", "start_mm", "end_mm", "width_mm"),
        "normal": ("kind", "net", "layer", "start_mm", "end_mm", "width_mm", "length_mm", "uuid"),
    },
    "via": {
        "summary": ("kind", "net", "x_mm", "y_mm", "layers"),
        "normal": ("kind", "net", "x_mm", "y_mm", "drill_mm", "size_mm", "layers", "via_type", "uuid"),
    },
    "zone": {
        "summary": ("kind", "net", "layers", "name"),
        "normal": ("kind", "net", "layers", "name", "bbox_mm", "filled", "priority", "uuid"),
    },
    "net": {
        "summary": ("kind", "name", "code"),
        "normal": ("kind", "name", "code", "pad_count"),
    },
}


# ---- the IPC session -----------------------------------------------------------------------------


@contextmanager
def ipc_session(ctx: ToolContext, install: Any, config: Any, path: Path) -> Iterator[Any]:
    """Open `services.ipc_session(install, path, config_root=config)` and ALWAYS close it
    (PROCESS HYGIENE: every api-server started must be stopped). Works with a context manager or
    a plain object with `close()`."""
    sess = ctx.services.ipc_session(install, path, config_root=config)
    if hasattr(sess, "__enter__") and hasattr(sess, "__exit__"):
        with sess as s:
            yield s
        return
    try:
        yield sess
    finally:
        close = getattr(sess, "close", None)
        if callable(close):
            close()


def ipc_board(ctx: ToolContext, install: Any, config: Any, board: Path) -> Any:
    with ipc_session(ctx, install, config, board) as s:
        return ctx.services.ipc_read_board(s)


# ---- normalisation (reader contract -> canonical items) ---------------------------------------------


def _xy(r: dict[str, Any]) -> tuple[float | None, float | None]:
    pos = mm_pair(pick(r, "position", "at"))
    if pos is not None:
        return pos[0], pos[1]
    return mm(pick(r, "x_mm", "x")), mm(pick(r, "y_mm", "y"))


def _attr(r: dict[str, Any]) -> str | None:
    """Mounting: `smd` | `through_hole` | `unspecified` from a string (IPC `mounting`) or the file's
    `attributes` tuple (`smd`, `through_hole`, `dnp`, ...)."""
    raw = pick(r, "attr", "mounting", "attributes", "type")
    if raw is None:
        return None
    if isinstance(raw, str):
        return {"tht": "through_hole"}.get(raw, raw)
    vals = set(str_list(raw))
    if "smd" in vals:
        return "smd"
    if "through_hole" in vals or "tht" in vals:
        return "through_hole"
    return "unspecified"


def norm_pad(p: Any, ref: str | None) -> dict[str, Any]:
    r = rec(p)
    x, y = _xy(r)
    return {
        "kind": "pad",
        "ref": pick(r, "ref", "reference", default=ref),
        "number": str(pick(r, "number", "name", "pad", default="")),
        "net": net_name(pick(r, "net", "net_name", "netname")),
        "x_mm": x,
        "y_mm": y,
        "size_mm": mm_pair(pick(r, "size_mm", "size")),
        "shape": canonical(pick(r, "shape"), PAD_SHAPES),
        "layers": sorted_layers(pick(r, "layers")),
        "drill_mm": drill_mm(pick(r, "drill_mm", "drill")),
        "pad_type": canonical(pick(r, "type", "pad_type"), PAD_TYPES),
        "uuid": pick(r, "uuid", "id", "tstamp"),
    }


def norm_footprint(fp: Any) -> dict[str, Any]:
    r = rec(fp)
    ref = pick(r, "ref", "reference", "refdes")
    pads = [norm_pad(p, ref) for p in (pick(r, "pads", default=[]) or [])]
    pads.sort(key=lambda p: (ref_key(p["number"]), p["uuid"] or ""))
    x, y = _xy(r)
    nets = sorted({p["net"] for p in pads if p["net"]})
    bbox = box_of(pick(r, "bbox_mm", "bbox", "bounding_box"))
    if bbox is None and callable(getattr(fp, "bbox", None)):
        bbox = box_of(fp.bbox)  # the file reader computes it on demand
    return {
        "kind": "footprint",
        "ref": ref,
        "value": pick(r, "value"),
        "lib_id": pick(r, "lib_id", "fpid", "footprint"),
        "layer": pick(r, "layer", "side"),
        "x_mm": x,
        "y_mm": y,
        "rotation_deg": mm(
            pick(r, "rotation_deg", "rotation", "orientation_deg", "orientation", default=0.0)
        ),
        "bbox_mm": bbox,
        "bbox_with_text_mm": box_of(pick(r, "bbox_with_text_mm", "bbox_with_text")),
        "courtyard_bbox_mm": box_of(pick(r, "courtyard_bbox_mm", "courtyard_bbox", "courtyard")),
        "pad_count": len(pads),
        "nets": nets,
        "attr": _attr(r),
        "dnp": bool(pick(r, "dnp", "do_not_populate", default=False)),
        "exclude_from_bom": bool(pick(r, "exclude_from_bom", default=False)),
        "uuid": pick(r, "uuid", "id", "tstamp"),
        "pads": pads,
        "properties": dict(pick(r, "properties", "fields", default={}) or {}),
    }


def norm_track(t: Any) -> dict[str, Any]:
    r = rec(t)
    start, end = mm_pair(pick(r, "start_mm", "start")), mm_pair(pick(r, "end_mm", "end"))
    length = None
    if start and end:
        length = round(((end[0] - start[0]) ** 2 + (end[1] - start[1]) ** 2) ** 0.5, 4)
    return {
        "kind": "track",
        "net": net_name(pick(r, "net", "net_name")),
        "layer": pick(r, "layer"),
        "start_mm": start,
        "end_mm": end,
        "width_mm": mm(pick(r, "width_mm", "width")),
        "length_mm": mm(pick(r, "length_mm", default=length)),
        "uuid": pick(r, "uuid", "id", "tstamp"),
    }


def norm_via(v: Any) -> dict[str, Any]:
    r = rec(v)
    x, y = _xy(r)
    return {
        "kind": "via",
        "net": net_name(pick(r, "net", "net_name")),
        "x_mm": x,
        "y_mm": y,
        "drill_mm": drill_mm(pick(r, "drill_mm", "drill")),
        "size_mm": mm(pick(r, "size_mm", "size", "diameter_mm", "diameter")),
        "layers": sorted_layers(pick(r, "layers")),
        "via_type": pick(r, "via_type", "kind", "type", default="through"),
        "uuid": pick(r, "uuid", "id", "tstamp"),
    }


def norm_zone(z: Any) -> dict[str, Any]:
    r = rec(z)
    return {
        "kind": "zone",
        "net": net_name(pick(r, "net", "net_name")),
        "layers": str_list(pick(r, "layers", "layer")),
        "name": pick(r, "name", default=""),
        "bbox_mm": box_of(pick(r, "bbox_mm", "bbox", "outline")),
        "filled": bool(pick(r, "filled", default=False)),
        "rule_area": bool(pick(r, "is_rule_area", default=False)) or pick(r, "type") == "rule_area",
        "priority": int(pick(r, "priority", default=0) or 0),
        "uuid": pick(r, "uuid", "id", "tstamp"),
    }


def norm_net(n: Any) -> dict[str, Any]:
    r = rec(n)
    return {
        "kind": "net",
        "name": net_name(pick(r, "name", "net")) or "",
        "code": pick(r, "code", "number"),
        "pad_count": pick(r, "pad_count", "pads", "items"),
        "netclass": pick(r, "netclass") or None,
    }


def norm_outline(board_rec: dict[str, Any]) -> dict[str, Any] | None:
    """From an outline record, the file reader's `outline` polygons (+ `outline_open` segments),
    or the IPC reader's `outline_bbox`."""
    raw = pick(board_rec, "outline", "edge", "board_outline")
    if raw is None:
        box = box_of(pick(board_rec, "outline_bbox"))
        return {"bbox_mm": box, "closed": None, "segments": None} if box else None
    r = rec(raw) if not isinstance(raw, (list, tuple)) else {}
    if r and ("bbox_mm" in r or "bbox" in r or "closed" in r):
        return {
            "bbox_mm": box_of(pick(r, "bbox_mm", "bbox")),
            "closed": bool(pick(r, "closed", default=False)),
            "segments": int(pick(r, "segments", "segment_count", default=0) or 0),
        }
    polygons = list(raw) if isinstance(raw, (list, tuple)) else [raw]
    open_segments = list(pick(board_rec, "outline_open", default=[]) or [])
    box = box_of(polygons) if polygons else box_of(open_segments)
    if box is None:
        return None
    return {
        "bbox_mm": box,
        "closed": bool(polygons) and not open_segments,
        "segments": sum(len(rec(pg).get("points", ())) for pg in polygons) + len(open_segments),
    }


def norm_rules(board_rec: dict[str, Any]) -> dict[str, Any] | None:
    """From `rules` / `design_rules` (IPC `DesignRulesSummary` in mm, or the file reader's
    `DesignRules` whose constraint values live in `project_rules`, the .kicad_pro
    `design_settings.rules` object) plus the board's copper layers."""
    rules = pick(board_rec, "rules", "design_rules", "setup")
    if rules is None:
        return None
    r = rec(rules)
    pro = dict(pick(r, "project_rules", default={}) or {})
    src = {**pro, **{k: v for k, v in r.items() if isinstance(v, (int, float))}}

    def val(*names: str) -> float | None:
        v = pick(src, *names)
        return mm(v) if isinstance(v, (int, float)) else None

    classes = {}
    for name, nc in dict(pick(r, "netclasses", default={}) or {}).items():
        ncr = rec(nc)
        classes[str(name)] = {k: (mm(v) if isinstance(v, (int, float)) else v) for k, v in ncr.items()}
    copper = str_list(pick(r, "copper_layers")) or copper_layer_names(board_rec)
    return {
        "min_clearance_mm": val("min_clearance_mm", "min_clearance"),
        "min_track_width_mm": val("min_track_width_mm", "min_track_width"),
        "min_via_diameter_mm": val("min_via_diameter_mm", "min_via_diameter", "min_via_size"),
        "min_via_drill_mm": val(
            "min_via_drill_mm", "min_via_drill", "min_through_hole_diameter", "min_through_drill"
        ),
        "min_hole_clearance_mm": val("min_hole_clearance", "hole_clearance"),
        "copper_edge_clearance_mm": val("min_copper_edge_clearance", "copper_edge_clearance"),
        "copper_layers": copper,
        "netclasses": dict(sorted(classes.items())),
    }


#: `.kicad_pro` netclass keys (KiCad's `NETCLASS` JSON, mm) -> the tool's names
_NETCLASS_KEYS = {
    "clearance": "clearance_mm",
    "track_width": "track_width_mm",
    "via_diameter": "via_size_mm",
    "via_drill": "via_drill_mm",
    "microvia_diameter": "microvia_size_mm",
    "microvia_drill": "microvia_drill_mm",
    "diff_pair_width": "diff_pair_width_mm",
    "diff_pair_gap": "diff_pair_gap_mm",
    "diff_pair_via_gap": "diff_pair_via_gap_mm",
}


def project_netclasses(pro_path: Path | None) -> dict[str, dict[str, Any]]:
    """`{name: {clearance_mm, track_width_mm, via_size_mm, via_drill_mm, ..., nets: [patterns],
    priority}}` from the project's `net_settings` (`classes` + `netclass_patterns`): the
    assignment kw_pcb_setup wrote, readable back on every provider (neither reader carries
    netclasses; reviewer finding, 2026-09-20). Empty when the file is missing or unreadable."""
    if pro_path is None or not pro_path.is_file():
        return {}
    try:
        doc = json.loads(pro_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    ns = doc.get("net_settings") if isinstance(doc, dict) else None
    if not isinstance(ns, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for cls in ns.get("classes") or []:
        if not isinstance(cls, dict) or not cls.get("name"):
            continue
        entry: dict[str, Any] = {}
        for key, name in _NETCLASS_KEYS.items():
            val = cls.get(key)
            if isinstance(val, (int, float)):
                entry[name] = round(float(val), 4)
        if isinstance(cls.get("priority"), int):
            entry["priority"] = cls["priority"]
        entry["nets"] = []
        out[str(cls["name"])] = entry
    for pat in ns.get("netclass_patterns") or []:
        if not isinstance(pat, dict):
            continue
        nc_name, pattern = pat.get("netclass"), pat.get("pattern")
        if nc_name and pattern is not None:
            out.setdefault(str(nc_name), {"nets": []})["nets"].append(str(pattern))
    return dict(sorted(out.items()))


def norm_unrouted(u: Any) -> dict[str, Any]:
    r = rec(u)

    def end(e: Any, ref_text: Any) -> dict[str, Any]:
        er = rec(e)
        pos = mm_pair(pick(er, "position", "at"))
        if pos is None:
            pos = mm_pair(e)  # a bare point (the file estimate's `start`/`end` Vec2)
        ref, pad = pick(er, "ref", "reference"), pick(er, "pad", "number")
        if ref is None and isinstance(ref_text, str):  # "R1.2" (pads) or "via" / "track"
            ref, _, pad = ref_text.partition(".")
        out = {
            "ref": ref or None,
            "pad": str(pad if pad is not None else ""),
            "x_mm": mm(pick(er, "x_mm", "x")) if pos is None else pos[0],
            "y_mm": mm(pick(er, "y_mm", "y")) if pos is None else pos[1],
        }
        # KiCad-computed end points (GetRatsnest / pcb export ratsnest) also carry the item id and
        # the copper layer of the anchor; the estimates do not.
        for extra in ("uuid", "layer"):
            value = pick(er, extra)
            if value:
                out[extra] = str(value)
        return out

    a = end(pick(r, "a", "from", "start"), pick(r, "start_ref", "a_ref"))
    b = end(pick(r, "b", "to", "end"), pick(r, "end_ref", "b_ref"))
    length = pick(r, "length_mm", "length")
    if length is None and None not in (a["x_mm"], a["y_mm"], b["x_mm"], b["y_mm"]):
        length = ((a["x_mm"] - b["x_mm"]) ** 2 + (a["y_mm"] - b["y_mm"]) ** 2) ** 0.5
    return {"net": net_name(pick(r, "net", "net_name")), "a": a, "b": b, "length_mm": mm(length)}


#: `*.<side>` wildcard layer names as the board file writes them for through-hole pads
_WILDCARD_SIDES = ("F", "B")


def copper_layer_names(board: Any) -> list[str]:
    """The board's copper layer names from either reader (`copper_layers` on the file model, the
    `layers[]` list with a `copper` flag on the IPC model)."""
    r = rec(board)
    copper = str_list(pick(r, "copper_layers"))
    if not copper:
        for layer in pick(r, "layers", default=[]) or []:
            lr = rec(layer)
            if pick(lr, "copper", "is_copper", default=False):
                copper.append(str(pick(lr, "name", default="")))
    return copper


def expand_layers(layers: list[str], copper: list[str]) -> list[str]:
    """`*.Cu` -> every copper layer of the board, `F&B.Cu` -> F.Cu + B.Cu, `*.Mask` -> F.Mask +
    B.Mask (and the other `*.X` sides), so the file provider lists the same concrete layer
    names as the IPC provider (which reports the padstack's layer set) and `layers` filters
    behave the same on both."""
    out: list[str] = []
    for name in layers:
        if name == "*.Cu":
            out.extend(copper or ["F.Cu", "B.Cu"])
        elif name == "F&B.Cu":
            out.extend(["F.Cu", "B.Cu"])
        elif name.startswith("*."):
            out.extend(f"{side}.{name[2:]}" for side in _WILDCARD_SIDES)
        else:
            out.append(name)
    return sorted(dict.fromkeys(out), key=layer_sort_key)  # stack order on both providers


def board_items(board: Any) -> dict[str, list[dict[str, Any]]]:
    """Every list kind of the board, normalised and deterministically ordered."""
    r = rec(board)
    copper = copper_layer_names(board)
    fps = sorted((norm_footprint(f) for f in (pick(r, "footprints", default=[]) or [])), key=_fp_key)
    for f in fps:
        for p in f["pads"]:
            p["layers"] = expand_layers(p["layers"], copper)
    pads = [p for f in fps for p in f["pads"]]
    loose = pick(r, "pads")
    if (
        loose
    ):  # a reader that lists pads flat as well: prefer its list (it may carry nets the footprint lacks)
        pads = sorted(
            (norm_pad(p, None) for p in loose), key=lambda p: (ref_key(p["ref"]), ref_key(p["number"]))
        )
    raw_tracks = [*(pick(r, "tracks", default=[]) or []), *(pick(r, "arcs", default=[]) or [])]
    tracks = sorted((norm_track(t) for t in raw_tracks), key=_geom_key)
    vias = sorted((norm_via(v) for v in (pick(r, "vias", default=[]) or [])), key=_geom_key)
    for v in vias:
        v["layers"] = expand_layers(v["layers"], copper)
    zones = sorted((norm_zone(z) for z in (pick(r, "zones", default=[]) or [])), key=_zone_key)
    for z in zones:
        z["layers"] = expand_layers(z["layers"], copper)
    nets = sorted(
        (norm_net(n) for n in (pick(r, "nets", default=[]) or [])), key=lambda n: (n["name"] or "",)
    )
    nets = [n for n in nets if n["name"]]  # the file's unconnected net ("" / code 0) is not a net
    return {"footprints": fps, "pads": pads, "tracks": tracks, "vias": vias, "zones": zones, "nets": nets}


def _fp_key(f: dict[str, Any]) -> tuple:
    return (ref_key(f["ref"]), f["uuid"] or "")


def _geom_key(i: dict[str, Any]) -> tuple:
    start = i.get("start_mm") or [i.get("x_mm") or 0.0, i.get("y_mm") or 0.0]
    return (i.get("layer") or "", i.get("net") or "", start[0], start[1], i.get("uuid") or "")


def _zone_key(z: dict[str, Any]) -> tuple:
    return (",".join(z["layers"]), z["net"] or "", z["name"], z["uuid"] or "")


def unrouted_list(source: Any) -> list[dict[str, Any]] | None:
    """`None` when the source has no connectivity answer (the planner then tries the next provider)."""
    if source is None:
        return None
    if isinstance(source, (list, tuple)):
        items: Any = source
    else:
        items = pick(rec(source), "airwires", "unrouted", "ratsnest")
    if items is None or isinstance(items, int):
        return None
    out = [norm_unrouted(u) for u in items]
    out.sort(
        key=lambda u: (
            u["net"] or "",
            ref_key(u["a"]["ref"]),
            u["a"]["pad"],
            ref_key(u["b"]["ref"]),
            u["b"]["pad"],
        )
    )
    return out


# ---- shared reads (kw_pcb_check reuses these) -------------------------------------------------------


def read_board(
    ctx: ToolContext, install: Any, caps: Any, config: Any, board: Path, *, prefer: str | None
) -> Any:
    return planner.run(
        "board.read",
        caps,
        {
            planner.IPC11: lambda: ipc_board(ctx, install, config, board),
            planner.FILE: lambda: ctx.services.file_read_board(board),
        },
        install=install,
        prefer=prefer,
    )


class _FileCache:
    """Load the file model at most once per call (the file provider serves several operations)."""

    def __init__(self, ctx: ToolContext, board: Path):
        self.ctx, self.board, self._model = ctx, board, None

    def get(self) -> Any:
        if self._model is None:
            self._model = self.ctx.services.file_read_board(self.board)
        return self._model


def ipc_unrouted(ratsnest: Any) -> list[dict[str, Any]] | None:
    """The canonical unrouted list from `kiwrap.backends.ipc.queries.ratsnest`'s `Ratsnest`
    (nets[].anchors[] / airwires[] with `from_item` / `to_item` KIIDs), joined to pad refs
    through the anchors. `None` when the object has no per-net structure (an already flat
    airwire list is handled by `unrouted_list`). A KiCad-computed answer (`GetRatsnest`) also
    carries `nets[].edges[]` with the anchor layers, which become the end points' `layer`."""
    r = rec(ratsnest)
    nets = pick(r, "nets")
    if nets is None:
        return None
    anchors: dict[str, dict[str, Any]] = {}
    layers: dict[tuple[str, str, str], tuple[str, str]] = {}  # (net, from, to) -> (layer a, layer b)
    for net in nets:
        nr = rec(net)
        for a in pick(nr, "anchors", default=()) or ():
            ar = rec(a)
            key = pick(ar, "id", "uuid")
            if key is not None:
                anchors[str(key)] = ar
        for e in pick(nr, "edges", default=()) or ():
            er = rec(e)
            ea, eb = rec(pick(er, "a")), rec(pick(er, "b"))
            key3 = (
                str(pick(er, "net", default="")),
                str(pick(ea, "id", default="")),
                str(pick(eb, "id", default="")),
            )
            layers[key3] = (
                str(pick(ea, "layer", default="") or ""),
                str(pick(eb, "layer", default="") or ""),
            )
    out: list[dict[str, Any]] = []
    for net in nets:
        for aw in pick(rec(net), "airwires", default=()) or ():
            wr = rec(aw)
            ends: list[dict[str, Any]] = []
            edge_layers = layers.get(
                (
                    str(pick(wr, "net", default="")),
                    str(pick(wr, "from_item", default="")),
                    str(pick(wr, "to_item", default="")),
                )
            )
            for index, (item_key, point_key) in enumerate((("from_item", "start"), ("to_item", "end"))):
                item_id = str(pick(wr, item_key, default=""))
                anchor = anchors.get(item_id, {})
                pos = mm_pair(pick(wr, point_key)) or mm_pair(pick(anchor, "position"))
                kind = pick(anchor, "kind")
                ref = pick(anchor, "ref") or (kind if kind and kind != "pad" else None)
                end: dict[str, Any] = {
                    "ref": ref or None,
                    "pad": str(pick(anchor, "number", "pad", default="")),
                    "x_mm": None if pos is None else pos[0],
                    "y_mm": None if pos is None else pos[1],
                }
                if item_id:
                    end["uuid"] = item_id
                if edge_layers and edge_layers[index]:
                    end["layer"] = edge_layers[index]
                ends.append(end)
            out.append(
                {
                    "net": net_name(pick(wr, "net")),
                    "a": ends[0],
                    "b": ends[1],
                    "length_mm": mm(pick(wr, "length", "length_mm")),
                }
            )
    out.sort(
        key=lambda u: (
            u["net"] or "",
            ref_key(u["a"]["ref"]),
            u["a"]["pad"],
            ref_key(u["b"]["ref"]),
            u["b"]["pad"],
        )
    )
    return out


#: above this many named nets the ipc11 cluster walk is skipped for the file estimate (env
#: KIWRAP_IPC_MAX_NETS overrides; provider='ipc11' forces the walk regardless)
DEFAULT_IPC_MAX_NETS = 300


def ipc_connectivity_max_nets() -> int:
    import os

    raw = os.environ.get("KIWRAP_IPC_MAX_NETS")
    try:
        return int(raw) if raw else DEFAULT_IPC_MAX_NETS
    except ValueError:
        return DEFAULT_IPC_MAX_NETS


def _net_count(session: Any) -> int:
    """Named nets of the open board (0 when the session cannot tell, e.g. a test double)."""
    try:
        nets = session.board().get_nets()
    except Exception:  # noqa: BLE001 - a fake session without a board: no threshold
        return 0
    return sum(1 for n in nets if getattr(n, "name", "") and not str(n.name).startswith("unconnected-"))


def cli_unrouted(report: Any, board_model: Any | None) -> list[dict[str, Any]]:
    """The canonical unrouted list from `kiwrap.formats.reports.ratsnest.RatsnestReport`
    (`kicad-cli pcb export ratsnest`): KiCad's airwires with the end items' uuids, anchor
    positions and layers. Pad references / numbers come from joining the uuids to the file
    model's pads (`board_model`, a `BoardFile`; None skips the join); other items get the kind
    the file estimate uses as `ref` ("via", "track", "zone")."""
    pads: dict[str, tuple[str, str]] = {}
    kinds: dict[str, str] = {}
    if board_model is not None:
        try:
            items = board_items(board_model)  # the reader contract, normalised (uuid, ref, number)
            for pad in items["pads"]:
                if pad.get("uuid"):
                    pads[str(pad["uuid"])] = (str(pad.get("ref") or ""), str(pad.get("number") or ""))
            for kind in ("track", "via", "zone"):
                for item in items[kind + "s"]:
                    if item.get("uuid"):
                        kinds[str(item["uuid"])] = kind
        except Exception:  # noqa: BLE001 - the join is a convenience; the export stands on its own
            pads, kinds = {}, {}

    def end(node: Any) -> dict[str, Any]:
        ref, pad = pads.get(node.uuid, (None, ""))
        if ref is None:
            ref = kinds.get(node.uuid) or _kind_from_description(node.description)
        return {
            "ref": ref or None,
            "pad": pad,
            "x_mm": round(node.pos.x_mm, 6),
            "y_mm": round(node.pos.y_mm, 6),
            "uuid": node.uuid,
            "layer": node.layer or None,
        }

    out: list[dict[str, Any]] = []
    for net in report.nets:
        for edge in net.edges:
            out.append(
                {
                    "net": net.name or None,
                    "a": end(edge.a),
                    "b": end(edge.b),
                    "length_mm": round(edge.length_mm, 6),
                }
            )
    return out


def _kind_from_description(text: str) -> str | None:
    """Best effort item kind from KiCad's (localised) item description when the uuid is not in
    the file model: "Pad ..." -> None (a pad we could not join), "Via ..." -> via, "Track ..."
    -> track, "Zone ..." -> zone. English texts only; anything else -> None."""
    lowered = (text or "").strip().lower()
    for kind in ("via", "track", "zone"):
        if lowered.startswith(kind):
            return kind
    return None


def read_connectivity(
    ctx: ToolContext,
    install: Any,
    caps: Any,
    config: Any,
    board: Path,
    *,
    ipc_model: Any,
    files: _FileCache,
    prefer: str | None,
) -> planner.Outcome[list[dict[str, Any]]]:
    """Unrouted connections: ipc11, then the kicad-cli ratsnest export, then the file estimate.

    ipc11 answers in one of three ways: KiCad's own airwires through `GetRatsnest`
    (`Ratsnest.exact`; nothing to tag; kiwrap patch ratsnest-export), the reader model's airwires
    (nothing to tag), or - what the stock nightly offers - KiCad's connectivity clusters through
    `services.ipc_ratsnest` (`GetItemsByNet` + `GetConnectedItems` per net: the unrouted count is
    KiCad's, the airwire end points are nearest-anchor guesses), which the outcome tags
    `airwire_endpoints_estimated`. When the install has `cli.has_ratsnest_export` but not
    `ipc.has_get_ratsnest`, ipc11 is not attempted in auto mode: the export is exact and the
    cluster walk is not. The file estimate is tagged `ratsnest_estimated`. The outcome's `patch`
    names the kiwrap patch the exact answer relied on (`provider_used` shows it).
    """
    extra_tags: list[str] = []
    forced = prefer == planner.IPC11  # provider='ipc11': walk every net whatever the count
    exact_cli = any(c.name == planner.CLI for c in planner.plan("connectivity", caps).candidates)
    skip_ipc = exact_cli and not caps.has("ipc.has_get_ratsnest") and prefer != planner.IPC11

    def from_ipc() -> list[dict[str, Any]]:
        found = unrouted_list(ipc_model) if ipc_model is not None else None
        if found is not None:
            return found
        with ipc_session(ctx, install, config, board) as s:
            net_count = 0 if forced else _net_count(s)
            if net_count > ipc_connectivity_max_nets():
                # the cluster walk is GetItemsByNet + GetConnectedItems PER NET: minutes on the
                # jetson baseboard (135 s for the same answer the file estimate gives in 28 s)
                raise KiwrapError(
                    "E_UNSUPPORTED",
                    f"ipc11 connectivity skipped: {net_count} nets exceed KIWRAP_IPC_MAX_NETS="
                    f"{ipc_connectivity_max_nets()}; the file estimate (agrees with DRC on partially routed "
                    "demo boards, tests/test_ratsnest_drc_live.py) answers instead - force provider='ipc11' "
                    "to walk every net over IPC",
                    data={"capability": "ipc.ratsnest"},
                )
            try:
                ratsnest = ctx.services.ipc_ratsnest(s)
            except KiwrapError as err:
                if err.code != "E_UNSUPPORTED":
                    raise
                ratsnest = None
            if ratsnest is None and ipc_model is None:
                found = unrouted_list(ctx.services.ipc_read_board(s))
                if found is not None:
                    return found
        if ratsnest is not None:
            found = ipc_unrouted(ratsnest)
            if found is None:
                found = unrouted_list(ratsnest)
            if found is not None:
                if not bool(pick(rec(ratsnest), "exact", default=False)):
                    extra_tags.append("airwire_endpoints_estimated")
                return found
        raise KiwrapError(
            "E_UNSUPPORTED",
            "the IPC backend returned no connectivity (KiCad exposes no ratsnest over IPC)",
            why="docs/DECISIONS.md D17 tier 3: ratsnest export is an upstream candidate",
            fix="the file estimate is used instead",
            data={"capability": "ipc.ratsnest"},
        )

    def from_cli() -> list[dict[str, Any]]:
        report = ctx.services.export_ratsnest(install, config, board)
        model = None
        try:
            model = files.get()
        except Exception:  # noqa: BLE001 - the join is a convenience; KiCad's answer stands alone
            model = None
        return cli_unrouted(report, model)

    attempts: dict[str, Any] = {
        planner.IPC11: from_ipc,
        planner.CLI: from_cli,
        planner.FILE: lambda: unrouted_list(ctx.services.ratsnest_estimate(files.get())) or [],
    }
    if skip_ipc:
        del attempts[planner.IPC11]
    outcome = planner.run("connectivity", caps, attempts, install=install, prefer=prefer)
    for tag in extra_tags:
        if tag not in outcome.degraded:
            outcome.degraded.append(tag)
    if outcome.provider_name == planner.CLI or (
        outcome.provider_name == planner.IPC11 and "airwire_endpoints_estimated" not in outcome.degraded
    ):
        key = "cli.has_ratsnest_export" if outcome.provider_name == planner.CLI else "ipc.has_get_ratsnest"
        outcome.patch = patch_providing(caps, key)
    return outcome


# ---- the tool -------------------------------------------------------------------------------------


class PcbGetArgs(BaseModel):
    project: str = Field(description="project directory or .kicad_pro path")
    kinds: list[BoardKind] = Field(
        default=["footprints", "outline"],
        description=(
            "what to return: footprints, pads, tracks, vias, zones, nets (items, chunked) and outline, rules, "
            "connectivity (singletons). connectivity = unrouted pad pairs (KiCad's when available, else an "
            "estimate tagged ratsnest_estimated)"
        ),
    )
    refs: list[str] | None = Field(default=None, description="only these reference designators (R1, U3, ...)")
    nets: list[str] | None = Field(default=None, description="only items on these net names")
    layers: list[str] | None = Field(
        default=None, description="only items on these layers (F.Cu, B.SilkS, ...)"
    )
    bbox_mm: list[float] | None = Field(
        default=None, description="[x1, y1, x2, y2] in mm; only items whose extent intersects the box"
    )
    detail: Detail = Field(
        default="normal", description="summary | normal | full (full includes pads and properties)"
    )
    cursor: str | None = Field(
        default=None, description="continue a chunked listing (the previous next_cursor)"
    )
    limit: int = Field(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT, description="items per page")
    provider: ProviderArg = Field(
        default="auto", description="auto (ipc11 when the KiCad has ipc.headless, else file), or force one"
    )


class PcbGetData(BaseModel):
    ok: bool = True
    board: str
    kinds: list[str]
    filters: dict[str, Any]
    detail: str
    counts: dict[str, int] = Field(description="items per kind BEFORE chunking (after filters)")
    total: int
    returned: int
    next_cursor: str | None = None
    items: list[dict[str, Any]]
    outline: dict[str, Any] | None = None
    rules: dict[str, Any] | None = None
    connectivity: dict[str, Any] | None = None
    plan: dict[str, Any]


class PcbGetOut(ReceiptOut):
    data: PcbGetData  # type: ignore[assignment]


def pcb_get(ctx: ToolContext, args: PcbGetArgs) -> Receipt:
    project = Project.from_path(args.project)
    state, external = read_state(project)
    if not project.board.exists():
        raise KiwrapError(
            "E_PROJECT_NOT_FOUND", f"board {project.board.name} is missing", fix="kw_project create writes it"
        )
    install = ctx.services.install(state.target_kicad)
    caps = ctx.services.caps(install)
    config = ctx.services.config_root(install)
    filters = Filters.from_args(args)
    prefer = None if args.provider == "auto" else args.provider

    outcome = read_board(ctx, install, caps, config, project.board, prefer=prefer)
    model = outcome.value
    files = _FileCache(ctx, project.board)
    if outcome.provider_name == planner.FILE:
        files._model = model
    degraded, warnings = external_tags(external) + list(outcome.degraded), list(outcome.warnings)

    lists = board_items(model)
    if filters.refs:  # nets are kept by the refs filter only when a pad of those refs is on them
        touched = {
            p["net"] for f in lists["footprints"] if f["ref"] in filters.refs for p in f["pads"] if p["net"]
        }
        lists["nets"] = [n for n in lists["nets"] if n["name"] in touched]
    items: list[dict[str, Any]] = []
    for kind in LIST_KINDS:  # declared order, so the flat list is deterministic across providers
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
        "board": project.board.name,
        "kinds": list(args.kinds),
        "filters": filters.to_dict(),
        "detail": args.detail,
        "counts": counts,
        "total": len(items),
        "returned": len(page),
        "next_cursor": nxt,
        "page_chars": paged.chars,
        "items": page,
        "outline": None,
        "rules": None,
        "connectivity": None,
        "plan": outcome.plan.to_dict(),
    }
    r = rec(model)
    if "outline" in args.kinds:
        data["outline"] = norm_outline(r)
        if data["outline"] is None:
            warnings.append("no board outline (Edge.Cuts) found")
    if "rules" in args.kinds:
        data["rules"] = norm_rules(r)
        if data["rules"] is not None and not data["rules"].get("netclasses"):
            data["rules"]["netclasses"] = project_netclasses(project.pro_path)
    if "connectivity" in args.kinds:
        conn = read_connectivity(
            ctx,
            install,
            caps,
            config,
            project.board,
            ipc_model=model if outcome.provider_name == planner.IPC11 else None,
            files=files,
            prefer=prefer,
        )
        unrouted = [
            u
            for u in conn.value
            if matches({"kind": "unrouted", "net": u["net"], "refs": [u["a"]["ref"], u["b"]["ref"]]}, filters)
        ]
        # the same cursor / limit page the airwires (finding 24: a cut list looked complete)
        upage = chunk_by_budget(unrouted, args.cursor, args.limit)
        data["connectivity"] = {
            "provider": conn.provider_used(install),
            "unrouted": upage.items,
            "unrouted_total": len(unrouted),
            "unrouted_returned": len(upage.items),
            "truncated": len(upage.items) < len(unrouted),
            "next_cursor": upage.next_cursor,
            "nets_with_unrouted": sorted({u["net"] for u in unrouted if u["net"]}),
        }
        if upage.next_cursor is not None:
            data["next_cursor"] = data["next_cursor"] or upage.next_cursor
            warnings.append(
                f"connectivity.unrouted lists {len(upage.items)} of {len(unrouted)} airwires; continue with "
                f"cursor={upage.next_cursor!r} (cursor/limit page items and airwires alike)"
            )
        for d in conn.degraded:
            if d not in degraded:
                degraded.append(d)
        warnings.extend(conn.warnings)
    return Receipt(
        provider_used=outcome.provider_used(install),
        degraded=degraded,
        warnings=warnings,
        stage=state.stage,
        data=data,
    )


SPEC = ToolSpec(
    name="kw_pcb_get",
    title="Board data",
    description=(
        "Structured board state in mm (primary feedback; prefer this over kw_pcb_render): footprints, pads, "
        "tracks, vias, zones, nets, outline, design rules and unrouted connectivity. Filter by refs/nets/layers/"
        "bbox_mm, choose detail, page with cursor/limit. provider_used says ipc11 (KiCad computed) or file."
    ),
    input_model=PcbGetArgs,
    output_model=PcbGetOut,
    handler=pcb_get,
    annotations=READ_ONLY,
)
