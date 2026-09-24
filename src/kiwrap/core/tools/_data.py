"""Shared plumbing for the data-first tools (kw_pcb_get / kw_sch_get / kw_pcb_check / kw_sch_stats /
kw_sch_diff): the canonical JSON item shapes, the reader contract they are normalised from,
filters (refs / nets / layers / bbox_mm / kinds), `detail` levels, deterministic ordering and
cursor/limit chunking (docs/ARCHITECTURE.md §11 "context budget"; docs/DECISIONS.md D17).

Reader contract (what `kiwrap.backends.ipc.readers` and `kiwrap.formats.board/schematic` hand
the core). Every object is a dataclass, a mapping or a plain object; `rec()` flattens it (fields
plus properties). Units are MILLIMETRES already (the IPC backend converts KiCad's integer
nanometres, docs/DECISIONS.md D16); angles are degrees; positions are `x_mm`/`y_mm` or a
`position` pair/Vec2; boxes are `[x1, y1, x2, y2]`, `{min_x, min_y, max_x, max_y}` (formats) or
`{x, y, w, h}` (IPC `BBox`); nets are names or `Net` objects with `name`. The canonical names
below are what the tools publish; the normalisers also accept the real modules' spellings as
they landed (IPC `BoardModel` / `SchematicModel`: `fpid`, `mounting`, `outline_bbox`,
`design_rules`, `sheet_path`, `is_power`, `mirror_x/y`, netlist as a tuple of nets with
`items[]`; file `BoardFile` / `Hierarchy`: `reference`, `attributes`, `Net` objects, `Drill`
objects, courtyard polygons, `outline` polygons, `project_rules`, per-instance
`reference_for/unit_for/pins_for(path)`, `names_path` sheets; analysis `CourtyardOverlap(a, b,
side, bbox)`, `OffBoardItem(reference, kind, detail, position)`, `Ratsnest.airwires[]` with
`start_ref` "R1.2", `Connectivity.dangling_wire_ends / unconnected_pins (PinRef) /
label_only_nets (DerivedNet)`).

    board:      footprints[], pads[], tracks[], vias[], zones[], nets[], outline, rules, unrouted[]|None
    footprint:  ref, value, lib_id, layer, x_mm, y_mm, rotation_deg, bbox_mm (body: pads + graphics
                incl. courtyard, NO reference/value text), bbox_with_text_mm (ipc11 only),
                courtyard_bbox_mm, uuid, smd|tht (attr), dnp, exclude_from_bom, pads[], properties{}
    pad:        ref, number, net, x_mm, y_mm, size_mm [w, h], shape (circle|rect|oval|trapezoid|
                roundrect|custom), layers[] (stack order), drill_mm, pad_type (thru_hole|smd|
                np_thru_hole|connect), uuid
    track:      uuid, net, layer, start_mm [x, y], end_mm [x, y], width_mm
    via:        uuid, net, x_mm, y_mm, drill_mm, size_mm, layers [from, to], via_type
    zone:       uuid, net, layers[], name, bbox_mm, filled, priority
    net:        code, name, pad_count
    outline:    bbox_mm, closed, segments
    rules:      min_clearance_mm, min_track_width_mm, min_via_diameter_mm, min_via_drill_mm,
                copper_layers, netclasses{name: {clearance_mm, track_width_mm, ...}}
    unrouted:   net, a {ref, pad, x_mm, y_mm}, b {ref, pad, x_mm, y_mm}, length_mm
    schematic:  symbols[], wires[], labels[], junctions[], no_connects[], sheets[], hierarchy, nets[]
    symbol:     ref, value, lib_id, unit, x_mm, y_mm, rotation_deg, mirror, sheet, uuid,
                footprint, dnp, power (bool), pins[], fields{}
    pin:        number, name, type (input|output|bidirectional|tri_state|passive|free|unspecified|
                power_in|power_out|open_collector|open_emitter|no_connect), x_mm, y_mm (ABSOLUTE,
                sheet coordinates), net, connected (net known and not an `unconnected-` net)
    wire:       uuid, sheet, start_mm, end_mm, net
    label:      uuid, sheet, kind (local|global|hierarchical|power|netclass), text, x_mm, y_mm, net
    junction:   uuid, sheet, x_mm, y_mm, net
    no_connect: uuid, sheet, x_mm, y_mm
    sheet:      uuid, name, file, path, x_mm, y_mm, size_mm [w, h], pins[{name, x_mm, y_mm}]
    hierarchy:  {path, name, file, children[]}
    net (sch):  code, name, nodes[{ref, pin, sheet}]
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime as _dt
import hashlib
import inspect
import json
import os
import re
import uuid
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from kiwrap.core.state import KiwrapError

Detail = Literal["summary", "normal", "full"]
DETAILS: tuple[str, ...] = ("summary", "normal", "full")
DEFAULT_LIMIT = 200
MAX_LIMIT = 2000

_REF_RE = re.compile(r"^([A-Za-z_#]*)(\d*)(.*)$")


def external_tags(external: Any) -> list[str]:
    """`["external_edit"]` when `read_state` found a hand-edited design file (the stage was already
    downgraded and persisted); the read tools surface it in `degraded` like the mutating ones."""
    return ["external_edit"] if getattr(external, "external_edit", False) else []


# ---- object -> dict ----------------------------------------------------------------------------------


def rec(obj: Any) -> dict[str, Any]:
    """A shallow dict view of a dataclass (fields + `property` values, so `Footprint.side` /
    `SchSymbol.is_power` are visible) / mapping / plain object (None -> {})."""
    if obj is None:
        return {}
    if isinstance(obj, Mapping):
        return dict(obj)
    out: dict[str, Any] = {}
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        out = {f.name: getattr(obj, f.name) for f in dataclasses.fields(obj)}
        for name, _attr in inspect.getmembers(type(obj), lambda a: isinstance(a, property)):
            if name.startswith("_") or name in out:
                continue
            try:
                out[name] = getattr(obj, name)
            except Exception:  # noqa: BLE001 - a property that raises is not data
                continue
        return out
    for name in dir(obj):
        if name.startswith("_"):
            continue
        try:
            val = getattr(obj, name)
        except Exception:  # noqa: BLE001 - a property that raises is not data
            continue
        if not callable(val):
            out[name] = val
    return out


def pick(r: Mapping[str, Any], *names: str, default: Any = None) -> Any:
    """The first present, non-None value among alias names."""
    for n in names:
        if n in r and r[n] is not None:
            return r[n]
    return default


def mm(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return round(float(value), 4)
    except (TypeError, ValueError):
        return None


def mm_pair(value: Any) -> list[float] | None:
    """`[x, y]` from a 2-sequence or an object/mapping with x_mm/y_mm (or x/y)."""
    if value is None:
        return None
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) and len(value) >= 2:
        x, y = mm(value[0]), mm(value[1])
        return None if x is None or y is None else [x, y]
    r = rec(value)
    x, y = mm(pick(r, "x_mm", "x")), mm(pick(r, "y_mm", "y"))
    return None if x is None or y is None else [x, y]


def mm_box(value: Any) -> list[float] | None:
    """`[x1, y1, x2, y2]` (normalised so x1 <= x2, y1 <= y2) from a 4-sequence or a box object."""
    if value is None:
        return None
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) and len(value) >= 4:
        vals = [mm(v) for v in value[:4]]
    else:
        r = rec(value)
        vals = [
            mm(pick(r, "x1_mm", "x1", "left", "min_x")),
            mm(pick(r, "y1_mm", "y1", "top", "min_y")),
            mm(pick(r, "x2_mm", "x2", "right", "max_x")),
            mm(pick(r, "y2_mm", "y2", "bottom", "max_y")),
        ]
    nums = [v for v in vals if v is not None]
    if len(nums) != 4:
        return None
    x1, y1, x2, y2 = nums
    return [min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)]


def net_name(value: Any) -> str | None:
    """A net given as its name, or as a `Net` object/mapping with `name` (the file reader); the
    unconnected net ("" / code 0) is None."""
    if value is None:
        return None
    if isinstance(value, str):
        return value or None
    name = pick(rec(value), "name", "net_name")
    return str(name) if name else None


def drill_mm(value: Any) -> float | None:
    """A drill as a diameter, a `(w, h)` pair (first value) or a `Drill` object with `diameter`."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return mm(value)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return mm(value[0]) if value else None
    return mm(pick(rec(value), "diameter_mm", "diameter", "size"))


def _points_of(value: Any) -> list[tuple[float, float]]:
    """Every point of a polygon / polygon list / point list (Vec2 objects, pairs or mappings)."""
    pts: list[tuple[float, float]] = []
    if value is None:
        return pts
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        if len(value) == 2 and all(isinstance(v, (int, float)) for v in value):
            return [(float(value[0]), float(value[1]))]
        for v in value:
            pts.extend(_points_of(v))
        return pts
    r = rec(value)
    if "points" in r:
        return _points_of(r["points"])
    pair = mm_pair(r)
    return [(pair[0], pair[1])] if pair else []


def box_of(value: Any) -> list[float] | None:
    """`[x1, y1, x2, y2]` from a box (`[x1, y1, x2, y2]`, `{min_x, ...}`, `{x, y, w, h}`), a callable
    returning one (`Footprint.bbox()`), or the extent of polygons / points."""
    if value is None:
        return None
    if callable(value) and not isinstance(value, type):
        try:
            value = value()
        except Exception:  # noqa: BLE001 - an unbuildable box is no box
            return None
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        if len(value) == 4 and all(isinstance(v, (int, float)) for v in value):
            return mm_box(value)
        pts = _points_of(value)
    else:
        r = rec(value)
        if "w" in r and "h" in r and "x" in r and "y" in r:
            x, y, w, h = (float(r[k]) for k in ("x", "y", "w", "h"))
            return mm_box((x, y, x + w, y + h))
        if any(k in r for k in ("min_x", "x1", "x1_mm", "left")):
            return mm_box(r)
        pts = _points_of(value)
    if not pts:
        return None
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return mm_box((min(xs), min(ys), max(xs), max(ys)))


#: Canonical vocabularies are the KiCad s-expression tokens (what the file provider reads and
#: kicad-cli writes); the IPC API's enum names are mapped onto them so both providers of one
#: board answer the same words (reviewer findings 16/25, 2026-09-19: ipc11 said `pth` /
#: `edge_connector` / `rectangle` / `power_output` where the file provider said `thru_hole` /
#: `connect` / `rect` / `power_out`).
PAD_TYPES: dict[str, str] = {
    "pth": "thru_hole",
    "thru_hole": "thru_hole",
    "through_hole": "thru_hole",
    "smd": "smd",
    "npth": "np_thru_hole",
    "np_thru_hole": "np_thru_hole",
    "edge_connector": "connect",
    "connect": "connect",
}
PAD_SHAPES: dict[str, str] = {
    "circle": "circle",
    "rectangle": "rect",
    "rect": "rect",
    "oval": "oval",
    "trapezoid": "trapezoid",
    "roundrect": "roundrect",
    "rounded_rectangle": "roundrect",
    "chamferedrect": "roundrect",  # the file writes chamfered rects as roundrect + chamfer params
    "chamfered_rect": "roundrect",
    "custom": "custom",
}
PIN_TYPES: dict[str, str] = {
    "tristate": "tri_state",
    "power_input": "power_in",
    "power_output": "power_out",
}
_LAYER_TYPE_ORDER = ("Cu", "Adhes", "Paste", "SilkS", "Mask", "CrtYd", "Fab")


def canonical(value: Any, table: dict[str, str]) -> str | None:
    """Map an enum word from either provider onto the canonical token (unknown words pass through)."""
    if value is None:
        return None
    key = str(value).strip().lower()
    return table.get(key, key) if key else None


def layer_sort_key(name: str) -> tuple[int, int, int, str]:
    """Board stack order: F.Cu, In1.Cu .. InN.Cu, B.Cu, then the technical layers grouped by type
    with the front side first (F.Paste, B.Paste, F.Mask, B.Mask, ...), then anything else."""
    side, _, kind = name.partition(".")
    if kind == "Cu":
        if side == "F":
            return (0, 0, 0, name)
        if side == "B":
            return (0, 10_000, 0, name)
        if side.startswith("In") and side[2:].isdigit():
            return (0, int(side[2:]), 0, name)
        return (0, 5_000, 0, name)
    if kind in _LAYER_TYPE_ORDER:
        return (1, _LAYER_TYPE_ORDER.index(kind), 0 if side == "F" else 1, name)
    return (2, 0, 0, name)


def sorted_layers(value: Any) -> list[str]:
    return sorted(str_list(value), key=layer_sort_key)


def str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value]


def ref_key(ref: str | None) -> tuple[str, int, str]:
    """Natural sort for reference designators: R1 < R2 < R10; None last."""
    if not ref:
        return ("~", 0, "")
    m = _REF_RE.match(ref)
    if not m:
        return (ref, 0, "")
    prefix, num, rest = m.groups()
    return (prefix.upper(), int(num) if num else -1, rest)


# ---- filters ---------------------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Filters:
    refs: tuple[str, ...] = ()
    nets: tuple[str, ...] = ()
    layers: tuple[str, ...] = ()
    bbox_mm: tuple[float, float, float, float] | None = None
    sheets: tuple[str, ...] = ()

    @classmethod
    def from_args(cls, args: Any) -> Filters:
        box = getattr(args, "bbox_mm", None)
        if box is not None:
            if len(box) != 4:
                raise KiwrapError(
                    "E_BAD_ARGS",
                    f"bbox_mm needs 4 numbers [x1, y1, x2, y2], got {len(box)}",
                    fix="pass bbox_mm=[x1, y1, x2, y2] in millimetres",
                    example="bbox_mm=[0, 0, 50, 30]",
                )
            b = mm_box(box)
            box = tuple(b) if b else None
        return cls(
            refs=tuple(getattr(args, "refs", None) or ()),
            nets=tuple(getattr(args, "nets", None) or ()),
            layers=tuple(getattr(args, "layers", None) or ()),
            bbox_mm=box,
            sheets=tuple(getattr(args, "sheets", None) or ()),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "refs": list(self.refs),
            "nets": list(self.nets),
            "layers": list(self.layers),
            "bbox_mm": list(self.bbox_mm) if self.bbox_mm else None,
            "sheets": list(self.sheets),
        }

    @property
    def active(self) -> bool:
        return bool(self.refs or self.nets or self.layers or self.bbox_mm or self.sheets)


def _item_refs(item: Mapping[str, Any]) -> set[str]:
    out: set[str] = set()
    if item.get("ref"):
        out.add(str(item["ref"]))
    out.update(str_list(item.get("refs")))
    for side in ("a", "b"):
        r = item.get(side)
        if isinstance(r, Mapping) and r.get("ref"):
            out.add(str(r["ref"]))
    return out


def _item_nets(item: Mapping[str, Any]) -> set[str]:
    out: set[str] = set()
    if item.get("net"):
        out.add(str(item["net"]))
    if item.get("kind") == "net" and item.get("name"):
        out.add(str(item["name"]))
    out.update(str_list(item.get("nets")))
    return out


def _item_layers(item: Mapping[str, Any]) -> set[str]:
    out: set[str] = set()
    if item.get("layer"):
        out.add(str(item["layer"]))
    out.update(str_list(item.get("layers")))
    return out


def _item_box(item: Mapping[str, Any]) -> list[float] | None:
    """The extent the `bbox_mm` filter tests: the item's own box, else the span of its points -
    `start_mm` / `end_mm` (wires, tracks), `x_mm` / `y_mm`, a nested `pos` (kw_pcb_check items) and
    the `a` / `b` airwire end points (so a far-away off_board or unrouted item is excluded)."""
    box = item.get("bbox_mm") or item.get("courtyard_bbox_mm")
    if box:
        return list(box)
    pts: list[list[float]] = []
    for key in ("start_mm", "end_mm"):
        p = item.get(key)
        if p:
            pts.append(list(p))
    if item.get("x_mm") is not None and item.get("y_mm") is not None:
        pts.append([float(item["x_mm"]), float(item["y_mm"])])
    for key in ("pos", "a", "b"):
        nested = item.get(key)
        if isinstance(nested, Mapping) and nested.get("x_mm") is not None and nested.get("y_mm") is not None:
            pts.append([float(nested["x_mm"]), float(nested["y_mm"])])
    if not pts:
        return None
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return [min(xs), min(ys), max(xs), max(ys)]


def sheet_key(sheet: Any) -> str:
    """A sheet instance path in canonical form: `/a/b/` for `/a/b/`, `a/b`, `/a/b`; `/` for the
    root (`""`, `/`)."""
    inner = str(sheet).strip("/")
    return f"/{inner}/" if inner else "/"


def boxes_intersect(a: Sequence[float], b: Sequence[float]) -> bool:
    return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])


def matches(item: Mapping[str, Any], f: Filters) -> bool:
    """An item passes when every ACTIVE filter matches; an item without the attribute a filter
    targets (e.g. a net has no layer) is kept only by filters that do not apply to it."""
    if f.refs:
        refs = _item_refs(item)
        if refs and not refs & set(f.refs):
            return False
        if not refs and item.get("kind") not in ("net", "hierarchy"):
            return False
    if f.nets:
        nets = _item_nets(item)
        if nets and not nets & set(f.nets):
            return False
        if not nets:
            return False
    if f.layers:
        layers = _item_layers(item)
        if layers and not layers & set(f.layers):
            return False
    if f.sheets:
        sheet = item.get("sheet") or item.get("path")
        if sheet is not None and sheet_key(sheet) not in {sheet_key(x) for x in f.sheets}:
            return False
    if f.bbox_mm:
        box = _item_box(item)
        if box is not None and not boxes_intersect(box, f.bbox_mm):
            return False
    return True


# ---- chunking -----------------------------------------------------------------------------------


def parse_cursor(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        n = int(cursor)
    except ValueError:
        raise KiwrapError(
            "E_BAD_ARGS",
            f"cursor {cursor!r} is not one this tool returned",
            why="cursors are opaque offsets returned as `next_cursor`",
            fix="pass the `next_cursor` value from the previous result, or omit it to start over",
        ) from None
    if n < 0:
        raise KiwrapError("E_BAD_ARGS", "cursor must not be negative", fix="omit cursor to start over")
    return n


def chunk(items: Sequence[Any], cursor: str | None, limit: int) -> tuple[list[Any], str | None]:
    """`(page, next_cursor)`; `next_cursor` is None on the last page."""
    start = parse_cursor(cursor)
    page = list(items[start : start + limit])
    nxt = str(start + limit) if start + limit < len(items) else None
    return page, nxt


#: A page's serialised size budget: below `receipts.MAX_TEXT_CHARS` (60 000) with room for the
#: rest of the receipt, so the text block of a result is never cut mid-JSON (reviewer finding 7:
#: 200 symbols at `normal` detail were 60 112 chars, truncated with `next_cursor: null`).
PAGE_BUDGET_CHARS = 40_000
#: the smallest item budget a page keeps after the singletons (outline, rules, hierarchy, ...) took
#: their share of `PAGE_BUDGET_CHARS`
MIN_PAGE_BUDGET_CHARS = 8_000
#: nesting depth of `data.items[i]` in the receipt text (`{` > `data` > `items` > item)
ITEMS_DEPTH = 3


def rendered_size(obj: Any, depth: int = ITEMS_DEPTH) -> int:
    """Characters `obj` adds to the receipt text when it sits `depth` levels deep in the receipt
    JSON (`json.dumps(receipt, indent=1)`, `kiwrap.core.state.receipts.receipt_to_result`): its own
    indent=1 dump, plus `depth` spaces on every one of its lines, plus the `,\\n` separator.
    Review item 6 / §3 small defects: measuring at indent 0 under-counted a page by about 25%
    (a "40k" page rendered as 48.6-49.7k)."""
    text = json.dumps(obj, indent=1, default=str)
    return len(text) + depth * (text.count("\n") + 1) + 2


@dataclasses.dataclass(frozen=True)
class Page:
    items: list[Any]
    next_cursor: str | None
    chars: int  # size of `items` in the receipt text (`rendered_size` at their depth)
    shortened: bool  # fewer than `limit` items because the budget was reached

    def warning(self, limit: int) -> str | None:
        if not self.shortened:
            return None
        return (
            f"page shortened to {len(self.items)} of the {limit} requested items to fit the result budget "
            f"({self.chars} chars); continue with cursor={self.next_cursor!r}, or use a smaller limit, "
            "detail='summary' or filters"
        )


def chunk_by_budget(
    items: Sequence[Any],
    cursor: str | None,
    limit: int,
    *,
    render: Any = None,
    budget_chars: int = PAGE_BUDGET_CHARS,
    depth: int = ITEMS_DEPTH,
) -> Page:
    """Page by COUNT and by SIZE: items are rendered (`render(item)`, e.g. the detail pruning) and
    measured as the receipt text shows them (`rendered_size` at `depth`); the page closes at
    `limit` items or when the next item would push the page over `budget_chars` (at least one item
    is always returned). `next_cursor` is the offset reached, so paging continues exactly where
    this page stopped. ONE list only: a tool paging several lists uses `page_lanes`."""
    start = parse_cursor(cursor)
    page: list[Any] = []
    chars = 2
    shortened = False
    for item in items[start : start + limit]:
        rendered = render(item) if render is not None else item
        size = rendered_size(rendered, depth)
        if page and chars + size > budget_chars:
            shortened = True
            break
        page.append(rendered)
        chars += size
    end = start + len(page)
    return Page(page, str(end) if end < len(items) else None, chars, shortened)


# ---- several lists behind one cursor (review item 6, B3) ------------------------------------------

_CURSOR_PART = re.compile(r"^(?P<lane>[a-z])(?P<off>\d+)$")


@dataclasses.dataclass(frozen=True)
class Lane:
    """One list a page walks: `key` is its one-letter cursor tag (`i` items, `u` unrouted airwires,
    `n` netlist nets), `depth` where its entries sit in the receipt text."""

    key: str
    items: Sequence[Any]
    render: Any = None
    depth: int = ITEMS_DEPTH


@dataclasses.dataclass(frozen=True)
class MultiPage:
    """`pages[key]` = the rendered entries of each lane; `starts` / `ends` the offsets walked;
    `next_cursor` is None only when EVERY lane is exhausted."""

    pages: dict[str, list[Any]]
    starts: dict[str, int]
    ends: dict[str, int]
    totals: dict[str, int]
    next_cursor: str | None
    chars: int
    shortened: bool
    version: str | None

    def more(self, key: str) -> bool:
        return self.ends[key] < self.totals[key]

    def warning(self, limit: int, names: Mapping[str, str] | None = None) -> str | None:
        """The one continuation hint (None on the last page): names every lane with entries left
        and repeats the exact `next_cursor`, so the text body (warnings come before `data`) can
        never disagree with `data.next_cursor` (review item 6's text/structured mismatch)."""
        if self.next_cursor is None:
            return None
        left = [
            f"{(names or {}).get(k, k)} {self.ends[k]}/{self.totals[k]}" for k in self.pages if self.more(k)
        ]
        why = (
            f"the result budget ({self.chars} chars) closed the page early"
            if self.shortened
            else f"limit={limit} per list reached"
        )
        return (
            f"more results: {', '.join(left)} returned so far ({why}); continue with "
            f"cursor={self.next_cursor!r} - one cursor pages every list, nothing is skipped"
        )


def format_multi_cursor(offsets: Mapping[str, int], version: str | None) -> str:
    """`i<off>.u<off>@<version>` (lane order as given; `@<version>` only when versioned)."""
    body = ".".join(f"{k}{int(v)}" for k, v in offsets.items())
    return f"{body}@{version}" if version else body


def parse_multi_cursor(cursor: str | None, lanes: Sequence[str], version: str | None) -> dict[str, int]:
    """Offsets per lane from a cursor `page_lanes` returned (`i12.u40@1a2b3c4d`).

    - None / "" start every lane at 0.
    - A bare integer (the pre-review cursor) sets every lane to that offset, which is exactly what
      the old shared cursor meant; a lane the cursor does not name starts at 0.
    - A lane tag this call does not page is E_BAD_ARGS (the cursor came from other `kinds`).
    - A version other than `version` (the data changed between pages) is E_STALE: the offsets
      would point into a different list (review B3: "a board edit between pages")."""
    out = {k: 0 for k in lanes}
    if not cursor:
        return out
    body, _, ver = str(cursor).strip().partition("@")
    if ver and version and ver != version:
        raise KiwrapError(
            "E_STALE",
            f"cursor {cursor!r} was issued for version {ver}; the design is now at {version}",
            why="the file changed between pages, so the offsets point into a different list and items would be "
            "skipped or repeated",
            fix="start over without cursor (the first page reflects the current file)",
            data={"reason": "cursor_version", "cursor_version": ver, "current_version": version},
        )
    if body.lstrip("-").isdigit():
        n = parse_cursor(body)
        return {k: n for k in lanes}
    for part in body.split("."):
        m = _CURSOR_PART.match(part)
        if not m or m.group("lane") not in out:
            raise KiwrapError(
                "E_BAD_ARGS",
                f"cursor {cursor!r} is not one this tool returned",
                why="cursors are opaque (`i<offset>.u<offset>@<version>`) and belong to the kinds of the call "
                "that returned them",
                fix="pass the `next_cursor` of the previous result with the same arguments, or omit it to start over",
            )
        out[m.group("lane")] = int(m.group("off"))
    return out


def page_lanes(
    lanes: Sequence[Lane],
    cursor: str | None,
    limit: int,
    *,
    version: str | None = None,
    budget_chars: int = PAGE_BUDGET_CHARS,
) -> MultiPage:
    """Page several lists with ONE cursor and ONE size budget (review item 6, B3).

    The old code paged `data.items` and `connectivity.unrouted` (kw_sch_get: `netlist.nets`) with
    the same offset but separate budgets and returned `items_cursor or airwire_cursor`: when the
    items stopped at 92 and the airwires at 43, the next page started both at 92 and airwires
    43-91 were never returned. Here every lane keeps its own offset in the composite cursor, the
    lanes fill in order from one shared budget (`limit` applies per lane), a lane the budget closed
    early resumes exactly where it stopped, and `next_cursor` is None only when all lanes are
    exhausted. `version` (e.g. the board sha8) makes a cursor from an older file E_STALE. At least
    one entry is returned while any lane has one left."""
    keys = [lane.key for lane in lanes]
    starts = parse_multi_cursor(cursor, keys, version)
    pages: dict[str, list[Any]] = {k: [] for k in keys}
    ends: dict[str, int] = {}
    totals: dict[str, int] = {}
    chars = 2
    shortened = False
    for lane in lanes:
        start = min(starts[lane.key], len(lane.items))
        starts[lane.key] = start
        totals[lane.key] = len(lane.items)
        end = start
        if not shortened:
            for item in lane.items[start : start + limit]:
                rendered = lane.render(item) if lane.render is not None else item
                size = rendered_size(rendered, lane.depth)
                if chars + size > budget_chars and any(pages.values()):
                    shortened = True
                    break
                pages[lane.key].append(rendered)
                chars += size
                end += 1
        ends[lane.key] = end
    done = all(ends[k] >= totals[k] for k in keys)
    nxt = None if done else format_multi_cursor(ends, version)
    return MultiPage(pages, starts, ends, totals, nxt, chars, shortened, version)


def singleton_budget(*singletons: Any, depth: int = 2) -> int:
    """`PAGE_BUDGET_CHARS` minus what the singletons (outline, rules, hierarchy, ...) add to the
    same receipt text, floored at `MIN_PAGE_BUDGET_CHARS`, so items + singletons stay below the
    60 000-character text cap together and the text body is never cut (review item 6)."""
    used = sum(rendered_size(s, depth) for s in singletons if s is not None)
    return max(MIN_PAGE_BUDGET_CHARS, PAGE_BUDGET_CHARS - used)


def file_version(*paths: Any) -> str:
    """The first 8 hex characters of the sha256 over the given files' bytes (a missing file counts
    as empty): the `@<version>` of a composite cursor."""
    h = hashlib.sha256()
    for p in paths:
        with contextlib.suppress(OSError):
            h.update(Path(p).read_bytes())
        h.update(b"\0")
    return h.hexdigest()[:8]


# ---- complete results in a file (review item 6: out_path / format='ndjson') ------------------------

OUT_SUFFIXES = (".json", ".ndjson", ".jsonl", ".txt")
OutFormat = Literal["json", "ndjson"]


def project_results_dir(storage_home: Path, project_dir: Path, name: str = "") -> Path:
    """`<storage>/results/<name>-<sha1(project dir)[:12]>/`: where kiwrap keeps per-project results
    OUTSIDE the project (review theme T2; D23). Keyed by the resolved, case-folded directory, so two
    copies with the same name never share a folder (the collision behind review B4)."""
    folder = Path(project_dir).resolve()
    digest = hashlib.sha1(os.path.normcase(str(folder)).encode("utf-8")).hexdigest()[:12]
    label = re.sub(r"[^A-Za-z0-9_.-]+", "_", name or folder.name)[:40] or "project"
    return Path(storage_home) / "results" / f"{label}-{digest}"


def resolve_out_file(
    requested: str | None,
    *,
    storage_home: Path,
    project_dir: Path,
    project_name: str,
    stem: str,
    fmt: str,
) -> Path:
    """The file a complete result is written to.

    `None` / `'auto'` -> `<storage>/results/<project>/exports/<stem>-<utc>-<rand>.<fmt>` (never
    inside the project, D23). Otherwise an ABSOLUTE path: an existing directory gets the same
    generated file name; a file must end in .json / .ndjson / .jsonl / .txt, must not be a
    `.kicad_*` file, and its folder must exist (review T2's allow-rules). Anything else is
    E_BAD_ARGS."""
    ext = ".ndjson" if fmt == "ndjson" else ".json"
    stamp = _dt.datetime.now(_dt.UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]
    generated = f"{stem}-{stamp}{ext}"
    if requested is None or str(requested).strip().lower() in ("", "auto"):
        folder = project_results_dir(storage_home, project_dir, project_name) / "exports"
        folder.mkdir(parents=True, exist_ok=True)
        return folder / generated
    path = Path(str(requested)).expanduser()
    example = (
        "out_path='C:/Users/me/review/board.ndjson' (or 'auto' for a file under the kiwrap storage home)"
    )
    if not path.is_absolute():
        raise KiwrapError(
            "E_BAD_ARGS",
            f"out_path {requested!r} is not an absolute path",
            fix="pass an absolute file or directory path, or 'auto'",
            example=example,
        )
    if path.is_dir():
        return path / generated
    if ".kicad_" in path.name.lower() or path.suffix.lower() not in OUT_SUFFIXES:
        raise KiwrapError(
            "E_BAD_ARGS",
            f"out_path {path.name!r} must end in {', '.join(OUT_SUFFIXES)}",
            why="kiwrap never writes over a KiCad file and only writes data files here",
            fix="choose a .ndjson / .json file name, or pass a directory",
            example=example,
        )
    if not path.parent.is_dir():
        raise KiwrapError(
            "E_BAD_ARGS",
            f"the folder of out_path ({path.parent}) does not exist",
            fix="create the folder first, or pass 'auto'",
            example=example,
        )
    return path


def write_result_file(
    path: Path, fmt: str, header: Mapping[str, Any], sections: Mapping[str, Any]
) -> dict[str, Any]:
    """Write a complete (unpaged) result.

    - `fmt='json'`: one document `{**header, **sections}`.
    - `fmt='ndjson'`: a `{"kind": "header", ...}` line, then one line per list entry (entries keep
      their own `kind`; entries of a list without one get the list name without its plural `s`,
      e.g. `unrouted` / `net`) and one line per singleton (`{"kind": "<name>", ...}`).

    Written to `<path>.tmp` and renamed, so a reader never sees half a file. Returns
    `{path, format, bytes, sha256, lines, counts}` for the receipt."""
    counts: dict[str, int] = {}
    if fmt == "ndjson":
        lines = [json.dumps({"kind": "header", **header}, default=str)]
        for name, value in sections.items():
            if isinstance(value, list):
                counts[name] = len(value)
                default_kind = name[:-1] if name.endswith("s") else name
                for entry in value:
                    row = dict(entry) if isinstance(entry, Mapping) else {"value": entry}
                    row.setdefault("kind", default_kind)
                    lines.append(json.dumps(row, default=str))
            elif value is not None:
                body = dict(value) if isinstance(value, Mapping) else {"value": value}
                lines.append(json.dumps({"kind": name, **body}, default=str))
        text = "\n".join(lines) + "\n"
    else:
        for name, value in sections.items():
            if isinstance(value, list):
                counts[name] = len(value)
        text = json.dumps({**header, **sections}, indent=1, default=str) + "\n"
    data = text.encode("utf-8")
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)
    return {
        "path": str(path),
        "format": fmt,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "lines": text.count("\n") if fmt == "ndjson" else None,
        "counts": counts,
    }


def prune(item: Mapping[str, Any], detail: str, keep: Mapping[str, Iterable[str]]) -> dict[str, Any]:
    """Project an item onto the fields its `detail` level publishes (`full` keeps everything)."""
    if detail == "full":
        return dict(item)
    fields = set(keep.get(detail, ()))
    return {k: v for k, v in item.items() if k in fields}


def count_by(items: Iterable[Mapping[str, Any]], key: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for it in items:
        k = str(it.get(key))
        out[k] = out.get(k, 0) + 1
    return dict(sorted(out.items()))
