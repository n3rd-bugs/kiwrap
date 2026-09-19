"""Semantic, read-only view of a `.kicad_pcb` over the lossless CST (`kiwrap.formats.sexpr`).

`BoardFile.load(path)` parses the file once and exposes layers, nets, footprints (with pads and
courtyards in board coordinates), tracks, arcs, vias, zones, the Edge.Cuts outline and the
`setup` design rules. The CST root stays reachable as `.root` (and every model keeps its `.node`),
so tokens this module does not interpret — embedded fonts/files, groups, generated items, images,
dimensions, teardrops, `pcbplotparams`, anything a future KiCad adds — are preserved untouched and
can be edited through the CST without losing the semantic view. Nothing here knows KiCad release
numbers; only the file's own tokens decide.

Dialects handled (all verified against the demo projects shipped with KiCad 7.0.6, 8.0.9, 9.0.9,
10.0.6 and the 10.99 nightly, conformance test T2):

* format stamps 20221018 (7), 20231007/20240108 (8), 20241030/20241229 (9 and most 10 demos),
  20250513 / 20250907 / 20260206 (10 and nightly);
* `(tstamp …)` (7) vs `(uuid …)` (8+);
* footprint reference/value as `(fp_text reference "C1" …)` (7) vs `(property "Reference" "C1" …)`
  (8+); `(property "Sheetfile" "…")` two-atom properties on every version;
* nets: a `(net N "name")` table plus `(net N "name")` on pads / `(net N)` on tracks and zones
  (≤ 20250907) versus *no* table and `(net "name")` everywhere (20260206, KiCad 10.0.6's
  pic_programmer and every board the nightly writes). `BoardFile.nets` is built from the table
  when present and from the referenced names otherwise; `Net.code` is None in the latter case;
* layer id tables that renumber between 7 (`31 "B.Cu"`) and 9+ (`2 "B.Cu"`): layers are looked
  up by name only;
* `(layerselection 0x00000f0_80000001)` (2 words, 7/8) vs 4 words (9+): kept as text in `setup`;
* `yes`/`no` (8+) vs `true`/`false` (7, `pcbplotparams`) booleans;
* `(attr smd exclude_from_bom dnp …)` bare flags and `locked` either as a bare flag (7/8) or as
  `(locked yes)` (9+);
* `(fp_arc (start) (mid) (end))` three-point arcs (≥ 20210925) with the pre-7 `(start=centre)
  (end) (angle)` form accepted as well; `fp_poly` point lists containing `(arc …)` entries.

Geometry conventions (pcbnew/pcb_io/kicad_sexpr/pcb_io_kicad_sexpr_parser.cpp, tag 10.0):
footprint items are stored footprint-relative and become board coordinates through
`Rotate(origin, fp.orientation)` then `Move(fp.position)` (lines 3766-3769 for shapes;
`SetFPRelativePosition` for pads); a pad's `(at x y angle)` angle is its *absolute* orientation
(`pad->SetOrientation`, line 6021), not relative to the footprint. Flipped (back-side) footprints
store already-mirrored local coordinates, so the same transform applies. Rotation follows KiCad's
`RotatePoint` sign convention (`kiwrap.formats.geometry.vec.rotate`).

Courtyards follow `FOOTPRINT::BuildCourtyardCaches` (pcbnew/footprint.cpp lines 3640-3720):
`PCB_SHAPE` items on F.CrtYd / B.CrtYd (texts ignored), chained with a 0.02 mm epsilon into
closed rings, arcs/circles polygonised with a 0.005 mm max error. Rings that do not close are
reported in `Footprint.courtyard_open` (KiCad flags the footprint as "malformed courtyard").
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kiwrap.formats.geometry import (
    MAX_ERROR_MM,
    BBox,
    Polygon,
    Vec2,
    arc_points,
    arc_to_segment_count,
    bezier_points,
    chain_outline,
    circle_polygon,
    nest_rings,
    pad_circle_polygon,
    rotate,
)
from kiwrap.formats.sexpr import Node, load, loads

__all__ = [
    "ArcTrack",
    "BoardFile",
    "DesignRules",
    "Drill",
    "Footprint",
    "Graphic",
    "Layer",
    "Net",
    "Pad",
    "StackupLayer",
    "Track",
    "Via",
    "Zone",
    "courtyard_polygons",
    "read_bool",
]

COURTYARD_CHAINING_EPSILON_MM = 0.02  # pcbnew/footprint.cpp BuildCourtyardCaches
OUTLINE_CHAINING_EPSILON_MM = 0.01  # DEFAULT_CHAINING_EPSILON_MM (board outline)
FRONT_COURTYARD = "F.CrtYd"
BACK_COURTYARD = "B.CrtYd"
EDGE_CUTS = "Edge.Cuts"

_TRUE = {"yes", "true", "1"}
_FALSE = {"no", "false", "0"}
_GRAPHIC_HEADS = ("fp_line", "fp_arc", "fp_circle", "fp_rect", "fp_poly", "fp_curve")
_BOARD_GRAPHIC_HEADS = ("gr_line", "gr_arc", "gr_circle", "gr_rect", "gr_poly", "gr_curve")
_VIA_KINDS = {"blind", "micro", "through"}


# ----------------------------------------------------------------------------------------------
# small CST helpers (shared with schematic.py through re-import)
# ----------------------------------------------------------------------------------------------


def read_bool(value: str | None, default: bool = False) -> bool:
    """`yes`/`true`/`1` -> True, `no`/`false`/`0` -> False, anything else -> `default`."""
    if value is None:
        return default
    v = value.strip().lower()
    if v in _TRUE:
        return True
    if v in _FALSE:
        return False
    return default


def _flag(node: Node, name: str, default: bool = False) -> bool:
    """A flag that is either a bare atom (`hide`, `locked`) or a `(name yes)` child."""
    for a in node.atoms:
        if a.kind == "symbol" and a.value == name:
            return True
    return read_bool(node.get(name), default)


def _uuid(node: Node) -> str:
    return node.get("uuid") or node.get("tstamp") or ""


def _xy(node: Node | None, default: Vec2 | None = None) -> Vec2 | None:
    if node is None:
        return default
    atoms = node.atoms
    if len(atoms) < 2:
        return default
    try:
        return Vec2(atoms[0].as_float(), atoms[1].as_float())
    except ValueError:
        return default


def _at(node: Node) -> tuple[Vec2, float]:
    at = node.find("at")
    if at is None:
        return Vec2(0.0, 0.0), 0.0
    atoms = at.atoms
    x = atoms[0].as_float() if len(atoms) > 0 else 0.0
    y = atoms[1].as_float() if len(atoms) > 1 else 0.0
    rot = atoms[2].as_float() if len(atoms) > 2 and atoms[2].kind == "number" else 0.0
    return Vec2(x, y), rot


def _float(node: Node, head: str, default: float = 0.0) -> float:
    text = node.get(head)
    if text is None:
        return default
    try:
        return float(text)
    except ValueError:
        return default


def _pts(node: Node | None) -> list[Vec2]:
    """Points of a `(pts (xy …) … (arc (start) (mid) (end)) …)` list; arcs are segmented."""
    if node is None:
        return []
    out: list[Vec2] = []
    for child in node.nodes:
        if child.head == "xy":
            p = _xy(child)
            if p is not None:
                out.append(p)
        elif child.head == "arc":
            s, m, e = _xy(child.find("start")), _xy(child.find("mid")), _xy(child.find("end"))
            if s is not None and m is not None and e is not None:
                seg = arc_points(s, m, e)
                if out and out[-1].close_to(seg[0]):
                    seg = seg[1:]
                out.extend(seg)
    return out


def _stroke_width(node: Node) -> float:
    stroke = node.find("stroke")
    if stroke is not None:
        return _float(stroke, "width")
    return _float(node, "width")


def _layer_names(node: Node, head: str = "layers") -> tuple[str, ...]:
    child = node.find(head)
    if child is None:
        single = node.get("layer")
        return (single,) if single else ()
    return tuple(child.values())


# ----------------------------------------------------------------------------------------------
# models
# ----------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Layer:
    """One row of the `(layers …)` table: `(id "name" type ["user name"])`."""

    id: int | None
    name: str
    kind: str
    user_name: str | None = None

    @property
    def is_copper(self) -> bool:
        return self.kind in ("signal", "power", "mixed", "jumper")


@dataclass(frozen=True)
class Net:
    """A net: `code` is the file's numeric code when the file has one (≤ 20250907), else None."""

    code: int | None
    name: str

    @property
    def is_unconnected(self) -> bool:
        return self.name == "" or self.name.startswith("unconnected-(")


@dataclass(frozen=True)
class Drill:
    diameter: float
    width: float | None = None  # slot: second dimension
    offset: Vec2 = Vec2(0.0, 0.0)

    @property
    def is_slot(self) -> bool:
        return self.width is not None and self.width != self.diameter


@dataclass(frozen=True)
class Pad:
    """A pad in board coordinates (`position`, `rotation` absolute). `local_position` is the
    footprint-relative `(at …)` as written."""

    number: str
    type: str  # thru_hole | smd | np_thru_hole | connect
    shape: str  # circle | rect | oval | trapezoid | roundrect | chamfered_rect | custom
    position: Vec2
    rotation: float
    size: Vec2
    layers: tuple[str, ...]
    net: Net | None
    local_position: Vec2
    drill: Drill | None = None
    pin_function: str | None = None
    pin_type: str | None = None
    uuid: str = ""
    roundrect_rratio: float | None = None
    rect_delta: Vec2 | None = None
    primitives: tuple[Polygon, ...] = ()  # custom pads: primitive polygons in board coordinates
    node: Node | None = field(default=None, repr=False, compare=False)

    @property
    def is_through_hole(self) -> bool:
        return self.type in ("thru_hole", "np_thru_hole")

    def on_layer(self, layer: str, copper_layers: Iterable[str] = ("F.Cu", "B.Cu")) -> bool:
        """Layer membership with KiCad's wildcards (`*.Cu`, `F&B.Cu`, `*.Mask`, …) expanded."""
        return layer in self.expand_layers(copper_layers)

    def expand_layers(self, copper_layers: Iterable[str] = ("F.Cu", "B.Cu")) -> tuple[str, ...]:
        copper = tuple(copper_layers)
        out: list[str] = []
        for entry in self.layers:
            if entry == "*.Cu":
                out.extend(copper)
            elif entry == "F&B.Cu":
                out.extend(c for c in copper if c in ("F.Cu", "B.Cu"))
            elif entry.startswith("*."):
                suffix = entry[1:]
                out.extend(("F" + suffix, "B" + suffix))
            else:
                out.append(entry)
        return tuple(dict.fromkeys(out))

    def polygons(self, max_error: float = MAX_ERROR_MM) -> list[Polygon]:
        """Outline(s) of the copper shape in board coordinates (custom pads: anchor + primitives).

        Chamfers are ignored (a chamfered rect is returned as its full rectangle), which errs on
        the side of the larger shape.
        """
        c, rot = self.position, self.rotation
        w, h = self.size.x, self.size.y
        rw, rh = w / 2, h / 2

        def place(local: Iterable[Vec2]) -> Polygon:
            return Polygon.of(rotate(p, rot) + c for p in local)

        if self.shape == "circle":
            return [Polygon.of(pad_circle_polygon(c, rw, max_error))]
        if self.shape in ("rect", "chamfered_rect"):
            return [place((Vec2(-rw, -rh), Vec2(rw, -rh), Vec2(rw, rh), Vec2(-rw, rh)))]
        if self.shape == "trapezoid":
            d = self.rect_delta or Vec2(0.0, 0.0)
            dx, dy = d.x / 2, d.y / 2
            # KiCad: rect_delta.x changes the width of the top/bottom edges, .y the left/right ones
            return [
                place(
                    (
                        Vec2(-rw - dy, -rh + dx),
                        Vec2(rw + dy, -rh - dx),
                        Vec2(rw - dy, rh + dx),
                        Vec2(-rw + dy, rh - dx),
                    )
                )
            ]
        if self.shape == "oval":
            return [place(_stadium(rw, rh, max_error))]
        if self.shape == "roundrect":
            ratio = self.roundrect_rratio if self.roundrect_rratio is not None else 0.25
            return [place(_roundrect(rw, rh, ratio * min(w, h), max_error))]
        if self.shape == "custom":
            anchor_shape = "rect"
            if self.node is not None:
                options = self.node.find("options")
                if options is not None:
                    anchor_shape = options.get("anchor") or "rect"
            if anchor_shape == "circle":
                anchor = Polygon.of(pad_circle_polygon(c, rw, max_error))
            else:
                anchor = place((Vec2(-rw, -rh), Vec2(rw, -rh), Vec2(rw, rh), Vec2(-rw, rh)))
            return [anchor, *self.primitives]
        return [place((Vec2(-rw, -rh), Vec2(rw, -rh), Vec2(rw, rh), Vec2(-rw, rh)))]

    def bbox(self) -> BBox:
        box: BBox | None = None
        for poly in self.polygons():
            box = poly.bbox if box is None else box.union(poly.bbox)
        if box is None:
            return BBox(self.position.x, self.position.y, self.position.x, self.position.y)
        return box


def _stadium(rw: float, rh: float, max_error: float) -> list[Vec2]:
    """An oval pad: a rectangle with semicircular ends along its longer axis."""
    if rw == rh:
        return pad_circle_polygon(Vec2(0.0, 0.0), rw, max_error)
    r = min(rw, rh)
    pts: list[Vec2] = []
    if rw > rh:
        left, right = Vec2(-(rw - r), 0.0), Vec2(rw - r, 0.0)
        pts += _arc_ccw(right, r, -90, 90, max_error)
        pts += _arc_ccw(left, r, 90, 270, max_error)
    else:
        top, bottom = Vec2(0.0, -(rh - r)), Vec2(0.0, rh - r)
        pts += _arc_ccw(bottom, r, 0, 180, max_error)
        pts += _arc_ccw(top, r, 180, 360, max_error)
    return pts


def _roundrect(rw: float, rh: float, radius: float, max_error: float) -> list[Vec2]:
    r = max(0.0, min(radius, rw, rh))
    if r <= 0.0:
        return [Vec2(-rw, -rh), Vec2(rw, -rh), Vec2(rw, rh), Vec2(-rw, rh)]
    pts: list[Vec2] = []
    pts += _arc_ccw(Vec2(rw - r, rh - r), r, 0, 90, max_error)
    pts += _arc_ccw(Vec2(-rw + r, rh - r), r, 90, 180, max_error)
    pts += _arc_ccw(Vec2(-rw + r, -rh + r), r, 180, 270, max_error)
    pts += _arc_ccw(Vec2(rw - r, -rh + r), r, 270, 360, max_error)
    return pts


def _arc_ccw(center: Vec2, r: float, a0: float, a1: float, max_error: float) -> list[Vec2]:
    """Points on an arc from angle a0 to a1 (degrees, atan2 frame), both ends included."""
    n = arc_to_segment_count(r, max_error, a1 - a0)
    out: list[Vec2] = []
    for k in range(n + 1):
        a = math.radians(a0 + (a1 - a0) * k / n)
        out.append(Vec2(center.x + r * math.cos(a), center.y + r * math.sin(a)))
    return out


@dataclass(frozen=True)
class Graphic:
    """A graphic item (footprint- or board-level) as a polyline in board coordinates.

    `closed` is True for circles, rectangles and polygons (the last point joins the first).
    """

    kind: str  # line | arc | circle | rect | poly | curve
    layer: str
    points: tuple[Vec2, ...]
    closed: bool
    width: float
    uuid: str = ""
    node: Node | None = field(default=None, repr=False, compare=False)

    def polygon(self) -> Polygon | None:
        return Polygon.of(self.points) if self.closed and len(self.points) >= 3 else None


@dataclass(frozen=True)
class Footprint:
    reference: str
    value: str
    fpid: str
    position: Vec2
    rotation: float
    layer: str
    attributes: tuple[str, ...]
    uuid: str
    path: str
    properties: dict[str, str]
    pads: tuple[Pad, ...]
    graphics: tuple[Graphic, ...]
    courtyard_front: tuple[Polygon, ...]
    courtyard_back: tuple[Polygon, ...]
    courtyard_open: tuple[tuple[Vec2, ...], ...]
    locked: bool = False
    description: str = ""
    node: Node | None = field(default=None, repr=False, compare=False)

    @property
    def side(self) -> str:
        return "back" if self.layer.startswith("B.") else "front"

    @property
    def is_smd(self) -> bool:
        return "smd" in self.attributes

    @property
    def dnp(self) -> bool:
        return "dnp" in self.attributes

    @property
    def exclude_from_pos_files(self) -> bool:
        return "exclude_from_pos_files" in self.attributes

    @property
    def exclude_from_bom(self) -> bool:
        return "exclude_from_bom" in self.attributes

    @property
    def courtyard(self) -> tuple[Polygon, ...]:
        """The courtyard on the footprint's own side."""
        return self.courtyard_back if self.side == "back" else self.courtyard_front

    def pad(self, number: str) -> Pad | None:
        return next((p for p in self.pads if p.number == number), None)

    def bbox(self) -> BBox:
        pts: list[Vec2] = [self.position]
        for poly in (*self.courtyard_front, *self.courtyard_back):
            pts.extend(poly.points)
        for pad in self.pads:
            b = pad.bbox()
            pts.extend(b.corners())
        for g in self.graphics:
            pts.extend(g.points)
        box = BBox.of(pts)
        assert box is not None
        return box


@dataclass(frozen=True)
class Track:
    start: Vec2
    end: Vec2
    width: float
    layer: str
    net: Net | None
    uuid: str = ""
    locked: bool = False
    node: Node | None = field(default=None, repr=False, compare=False)

    def length(self) -> float:
        return self.start.distance(self.end)


@dataclass(frozen=True)
class ArcTrack:
    start: Vec2
    mid: Vec2
    end: Vec2
    width: float
    layer: str
    net: Net | None
    uuid: str = ""
    locked: bool = False
    node: Node | None = field(default=None, repr=False, compare=False)

    def points(self, max_error: float = MAX_ERROR_MM) -> list[Vec2]:
        return arc_points(self.start, self.mid, self.end, max_error)


@dataclass(frozen=True)
class Via:
    position: Vec2
    size: float
    drill: float
    layers: tuple[str, ...]
    net: Net | None
    kind: str = "through"  # through | blind | micro
    uuid: str = ""
    locked: bool = False
    free: bool = False
    node: Node | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class Zone:
    net: Net | None
    layers: tuple[str, ...]
    outline: tuple[Polygon, ...]
    filled: dict[str, tuple[Polygon, ...]]  # layer -> filled polygons (islands included)
    name: str = ""
    uuid: str = ""
    priority: int = 0
    is_rule_area: bool = False
    node: Node | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class StackupLayer:
    name: str
    type: str
    thickness: float | None = None
    material: str | None = None
    epsilon_r: float | None = None
    loss_tangent: float | None = None
    color: str | None = None


@dataclass(frozen=True)
class DesignRules:
    """What `(setup …)` carries: scalar entries as text (`pad_to_mask_clearance`, `tenting`,
    `aux_axis_origin`, …), the stackup, and `pcbplotparams` as a name -> text map. Constraint
    rules (clearance, track width …) live in the `.kicad_pro` since KiCad 6; `project_rules` is
    its `design_settings.rules` object when the project file sits next to the board."""

    values: dict[str, tuple[str, ...]]
    stackup: tuple[StackupLayer, ...]
    plot_params: dict[str, tuple[str, ...]]
    project_rules: dict[str, Any] | None = None

    def scalar(self, name: str, default: float | None = None) -> float | None:
        vals = self.values.get(name)
        if not vals:
            return default
        try:
            return float(vals[0])
        except ValueError:
            return default

    @property
    def pad_to_mask_clearance(self) -> float:
        return self.scalar("pad_to_mask_clearance", 0.0) or 0.0


# ----------------------------------------------------------------------------------------------
# parsing
# ----------------------------------------------------------------------------------------------


class _NetTable:
    """Resolves `(net …)` references in either dialect (see module docstring)."""

    def __init__(self, root: Node) -> None:
        self.by_code: dict[int, Net] = {}
        self.by_name: dict[str, Net] = {}
        self.has_codes = False
        for n in root.find_all("net"):
            atoms = n.atoms
            if not atoms:
                continue
            if atoms[0].kind == "number":
                code = atoms[0].as_int()
                name = atoms[1].value if len(atoms) > 1 else ""
                net = Net(code, name)
                self.has_codes = True
                self.by_code[code] = net
                self.by_name.setdefault(name, net)
            else:
                net = Net(None, atoms[0].value)
                self.by_name.setdefault(net.name, net)

    def resolve(self, node: Node) -> Net | None:
        ref = node.find("net")
        if ref is None:
            return None
        atoms = ref.atoms
        if not atoms:
            return None
        if atoms[0].kind == "number":
            code = atoms[0].as_int()
            name = atoms[1].value if len(atoms) > 1 else None
            net = self.by_code.get(code)
            if net is None:
                if name is None:
                    name_node = node.find("net_name")  # zones on <= 9
                    name = name_node.atoms[0].value if name_node is not None and name_node.atoms else ""
                net = Net(code, name)
                self.by_code[code] = net
                self.by_name.setdefault(name, net)
            return net
        name = atoms[0].value
        net = self.by_name.get(name)
        if net is None:
            net = Net(None, name)
            self.by_name[name] = net
        return net

    def nets(self) -> tuple[Net, ...]:
        if self.has_codes:
            return tuple(self.by_code[c] for c in sorted(self.by_code))
        return tuple(self.by_name[n] for n in sorted(self.by_name))


def _parse_graphic(node: Node, transform) -> Graphic | None:
    """A `fp_*` / `gr_*` shape as a polyline; `transform` maps local -> board coordinates."""
    head = node.head
    kind = head.split("_", 1)[1] if "_" in head else head
    layer = node.get("layer") or ""
    width = _stroke_width(node)
    uuid = _uuid(node)
    pts: list[Vec2] = []
    closed = False
    if kind == "line":
        s, e = _xy(node.find("start")), _xy(node.find("end"))
        if s is None or e is None:
            return None
        pts = [s, e]
    elif kind == "arc":
        s, m, e = _xy(node.find("start")), _xy(node.find("mid")), _xy(node.find("end"))
        if m is None and node.find("angle") is not None and s is not None and e is not None:
            # legacy (< 20210925): start = centre, end = arc start, angle = sweep (deg)
            centre, arc_start, sweep = s, e, _float(node, "angle")
            r = centre.distance(arc_start)
            a0 = math.degrees(math.atan2(arc_start.y - centre.y, arc_start.x - centre.x))
            mid_a = math.radians(a0 + sweep / 2)
            end_a = math.radians(a0 + sweep)
            m = Vec2(centre.x + r * math.cos(mid_a), centre.y + r * math.sin(mid_a))
            e = Vec2(centre.x + r * math.cos(end_a), centre.y + r * math.sin(end_a))
            s = arc_start
        if s is None or m is None or e is None:
            return None
        pts = arc_points(s, m, e)
    elif kind == "circle":
        c, e = _xy(node.find("center")), _xy(node.find("end"))
        if c is None or e is None:
            return None
        pts = circle_polygon(c, c.distance(e))
        closed = True
    elif kind == "rect":
        s, e = _xy(node.find("start")), _xy(node.find("end"))
        if s is None or e is None:
            return None
        pts = [s, Vec2(e.x, s.y), e, Vec2(s.x, e.y)]
        closed = True
    elif kind == "poly":
        pts = _pts(node.find("pts"))
        closed = True
    elif kind == "curve":
        raw = _pts(node.find("pts"))
        if len(raw) != 4:
            return None
        pts = bezier_points(raw[0], raw[1], raw[2], raw[3])
    else:
        return None
    if not pts:
        return None
    return Graphic(kind, layer, tuple(transform(p) for p in pts), closed, width, uuid, node)


def courtyard_polygons(
    graphics: Iterable[Graphic], layer: str
) -> tuple[list[Polygon], list[tuple[Vec2, ...]]]:
    """Closed courtyard rings on `layer` (F.CrtYd / B.CrtYd) the way `BuildCourtyardCaches` chains
    them; second element = chains that did not close."""
    polylines: list[list[Vec2]] = []
    for g in graphics:
        if g.layer != layer:
            continue
        pts = list(g.points)
        if g.closed and pts:
            pts.append(pts[0])
        polylines.append(pts)
    closed, open_chains = chain_outline(polylines, COURTYARD_CHAINING_EPSILON_MM)
    return nest_rings(closed), open_chains


def _pad_primitives(node: Node, transform) -> tuple[Polygon, ...]:
    prims = node.find("primitives")
    if prims is None:
        return ()
    out: list[Polygon] = []
    for child in prims.nodes:
        g = _parse_graphic(child, transform)
        if g is None:
            continue
        poly = g.polygon()
        if poly is not None:
            out.append(poly)
        elif g.width > 0 and len(g.points) >= 2:
            out.append(_thick_polyline(g.points, g.width))
    return tuple(out)


def _thick_polyline(points: tuple[Vec2, ...], width: float) -> Polygon:
    """Coarse hull of a stroked polyline (its bbox expanded by half the width)."""
    box = BBox.of(points)
    assert box is not None
    return Polygon.from_bbox(box.expanded(width / 2))


def _parse_pad(node: Node, fp_pos: Vec2, fp_rot: float, nets: _NetTable) -> Pad:
    atoms = node.atoms
    number = atoms[0].value if atoms else ""
    ptype = atoms[1].value if len(atoms) > 1 else "smd"
    shape = atoms[2].value if len(atoms) > 2 else "rect"
    local, angle = _at(node)
    position = rotate(local, fp_rot) + fp_pos
    size = _xy(node.find("size"), Vec2(0.0, 0.0)) or Vec2(0.0, 0.0)
    drill: Drill | None = None
    drill_node = node.find("drill")
    if drill_node is not None:
        nums = [a.as_float() for a in drill_node.atoms if a.kind == "number"]
        offset = _xy(drill_node.find("offset"), Vec2(0.0, 0.0)) or Vec2(0.0, 0.0)
        if nums:
            drill = Drill(nums[0], nums[1] if len(nums) > 1 else None, offset)
        elif ptype in ("thru_hole", "np_thru_hole"):
            drill = Drill(0.0, None, offset)
    delta = _xy(node.find("rect_delta"))
    rr = node.get("roundrect_rratio")

    def transform(p: Vec2) -> Vec2:
        return rotate(p, angle) + position

    return Pad(
        number=number,
        type=ptype,
        shape=shape,
        position=position,
        rotation=angle,
        size=size,
        layers=_layer_names(node),
        net=nets.resolve(node),
        local_position=local,
        drill=drill,
        pin_function=node.get("pinfunction"),
        pin_type=node.get("pintype"),
        uuid=_uuid(node),
        roundrect_rratio=float(rr) if rr is not None else None,
        rect_delta=delta,
        primitives=_pad_primitives(node, transform),
        node=node,
    )


def _parse_footprint(node: Node, nets: _NetTable) -> Footprint:
    atoms = node.atoms
    fpid = atoms[0].value if atoms else ""
    position, rotation = _at(node)
    layer = node.get("layer") or "F.Cu"
    attributes: list[str] = []
    attr = node.find("attr")
    if attr is not None:
        attributes = [a.value for a in attr.atoms]
    if read_bool(node.get("dnp")) and "dnp" not in attributes:
        attributes.append("dnp")
    properties: dict[str, str] = {}
    reference = value = ""
    for prop in node.find_all("property"):
        pa = prop.atoms
        if len(pa) >= 1:
            name = pa[0].value
            val = pa[1].value if len(pa) > 1 else ""
            properties[name] = val
    for text in node.find_all("fp_text"):
        ta = text.atoms
        if len(ta) >= 2 and ta[0].value == "reference":
            reference = ta[1].value
        elif len(ta) >= 2 and ta[0].value == "value":
            value = ta[1].value
    reference = properties.get("Reference", reference)
    value = properties.get("Value", value)

    def transform(p: Vec2) -> Vec2:
        return rotate(p, rotation) + position

    graphics: list[Graphic] = []
    for child in node.nodes:
        if child.head in _GRAPHIC_HEADS:
            g = _parse_graphic(child, transform)
            if g is not None:
                graphics.append(g)
    pads = tuple(_parse_pad(p, position, rotation, nets) for p in node.find_all("pad"))
    front, open_front = courtyard_polygons(graphics, FRONT_COURTYARD)
    back, open_back = courtyard_polygons(graphics, BACK_COURTYARD)
    return Footprint(
        reference=reference,
        value=value,
        fpid=fpid,
        position=position,
        rotation=rotation,
        layer=layer,
        attributes=tuple(attributes),
        uuid=_uuid(node),
        path=node.get("path") or "",
        properties=properties,
        pads=pads,
        graphics=tuple(graphics),
        courtyard_front=tuple(front),
        courtyard_back=tuple(back),
        courtyard_open=tuple(open_front + open_back),
        locked=_flag(node, "locked"),
        description=node.get("descr") or "",
        node=node,
    )


def _parse_zone(node: Node, nets: _NetTable) -> Zone:
    outline = [Polygon.of(_pts(poly.find("pts"))) for poly in node.find_all("polygon")]
    filled: dict[str, list[Polygon]] = {}
    for fp in node.find_all("filled_polygon"):
        layer = fp.get("layer") or ""
        pts = _pts(fp.find("pts"))
        if len(pts) >= 3:
            filled.setdefault(layer, []).append(Polygon.of(pts))
    priority = node.get("priority")
    return Zone(
        net=nets.resolve(node),
        layers=_layer_names(node),
        outline=tuple(p for p in outline if len(p) >= 3),
        filled={k: tuple(v) for k, v in filled.items()},
        name=node.get("name") or "",
        uuid=_uuid(node),
        priority=int(priority) if priority and priority.isdigit() else 0,
        is_rule_area=node.find("keepout") is not None,
        node=node,
    )


def _parse_via(node: Node, nets: _NetTable) -> Via:
    kind = "through"
    for a in node.atoms:
        if a.kind == "symbol" and a.value in _VIA_KINDS:
            kind = a.value
    pos, _ = _at(node)
    return Via(
        position=pos,
        size=_float(node, "size"),
        drill=_float(node, "drill"),
        layers=_layer_names(node),
        net=nets.resolve(node),
        kind=kind,
        uuid=_uuid(node),
        locked=_flag(node, "locked"),
        free=_flag(node, "free"),
        node=node,
    )


def _parse_setup(setup: Node | None) -> DesignRules:
    values: dict[str, tuple[str, ...]] = {}
    stackup: list[StackupLayer] = []
    plot: dict[str, tuple[str, ...]] = {}
    if setup is not None:
        for child in setup.nodes:
            if child.head == "stackup":
                for layer in child.find_all("layer"):
                    la = layer.atoms
                    stackup.append(
                        StackupLayer(
                            name=la[0].value if la else "",
                            type=layer.get("type") or "",
                            thickness=_opt_float(layer.get("thickness")),
                            material=layer.get("material"),
                            epsilon_r=_opt_float(layer.get("epsilon_r")),
                            loss_tangent=_opt_float(layer.get("loss_tangent")),
                            color=layer.get("color"),
                        )
                    )
                for scalar in child.nodes:
                    if scalar.head != "layer":
                        values["stackup." + scalar.head] = tuple(scalar.values())
            elif child.head == "pcbplotparams":
                for p in child.nodes:
                    plot[p.head] = tuple(p.values())
            elif child.head:
                values[child.head] = tuple(child.values())
    return DesignRules(values=values, stackup=tuple(stackup), plot_params=plot)


def _opt_float(text: str | None) -> float | None:
    if text is None:
        return None
    try:
        return float(text)
    except ValueError:
        return None


@dataclass
class BoardFile:
    """Semantic view of one `.kicad_pcb`; see the module docstring for what is modelled."""

    path: Path | None
    root: Node
    stamp: int | None
    generator: str
    generator_version: str | None
    layers: tuple[Layer, ...]
    nets: tuple[Net, ...]
    footprints: tuple[Footprint, ...]
    tracks: tuple[Track, ...]
    arcs: tuple[ArcTrack, ...]
    vias: tuple[Via, ...]
    zones: tuple[Zone, ...]
    graphics: tuple[Graphic, ...]
    outline: tuple[Polygon, ...]
    outline_open: tuple[tuple[Vec2, ...], ...]
    design_rules: DesignRules
    net_codes_in_file: bool
    paper: str | None = None
    title_block: dict[str, str] = field(default_factory=dict)

    # loading ----------------------------------------------------------------------------------
    @classmethod
    def load(cls, path: str | Path, *, project_rules: bool = True) -> BoardFile:
        """Parse `path`. With `project_rules`, a sibling `<stem>.kicad_pro` (JSON) supplies
        `design_rules.project_rules` when present and readable."""
        p = Path(path)
        board = cls.from_node(load(p), path=p)
        if project_rules:
            pro = p.with_suffix(".kicad_pro")
            if pro.is_file():
                try:
                    doc = json.loads(pro.read_text(encoding="utf-8"))
                    rules = doc.get("board", {}).get("design_settings", {}).get("rules")
                    if isinstance(rules, dict):
                        board.design_rules = DesignRules(
                            board.design_rules.values,
                            board.design_rules.stackup,
                            board.design_rules.plot_params,
                            rules,
                        )
                except (OSError, ValueError):
                    pass
        return board

    @classmethod
    def loads(cls, text: str, path: str | Path | None = None) -> BoardFile:
        return cls.from_node(loads(text), path=Path(path) if path else None)

    @classmethod
    def from_node(cls, root: Node, path: Path | None = None) -> BoardFile:
        if root.head != "kicad_pcb":
            raise ValueError(f"not a KiCad board: top-level list is ({root.head or '?'} …)")
        stamp_text = root.get("version")
        stamp = int(stamp_text) if stamp_text and stamp_text.isdigit() else None
        layers: list[Layer] = []
        layer_table = root.find("layers")
        if layer_table is not None:
            for row in layer_table.nodes:
                vals = row.values()
                if len(vals) >= 2:
                    lid = int(vals[0]) if vals[0].lstrip("-").isdigit() else None
                    layers.append(
                        Layer(
                            lid, vals[1], vals[2] if len(vals) > 2 else "", vals[3] if len(vals) > 3 else None
                        )
                    )
        nets = _NetTable(root)
        footprints = tuple(_parse_footprint(n, nets) for n in root.find_all("footprint"))
        tracks: list[Track] = []
        arcs: list[ArcTrack] = []
        vias: list[Via] = []
        zones: list[Zone] = []
        graphics: list[Graphic] = []
        for child in root.nodes:
            head = child.head
            if head == "segment":
                s, e = _xy(child.find("start")), _xy(child.find("end"))
                if s is not None and e is not None:
                    tracks.append(
                        Track(
                            s,
                            e,
                            _float(child, "width"),
                            child.get("layer") or "",
                            nets.resolve(child),
                            _uuid(child),
                            _flag(child, "locked"),
                            child,
                        )
                    )
            elif head == "arc":
                s, m, e = _xy(child.find("start")), _xy(child.find("mid")), _xy(child.find("end"))
                if s is not None and m is not None and e is not None:
                    arcs.append(
                        ArcTrack(
                            s,
                            m,
                            e,
                            _float(child, "width"),
                            child.get("layer") or "",
                            nets.resolve(child),
                            _uuid(child),
                            _flag(child, "locked"),
                            child,
                        )
                    )
            elif head == "via":
                vias.append(_parse_via(child, nets))
            elif head == "zone":
                zones.append(_parse_zone(child, nets))
            elif head in _BOARD_GRAPHIC_HEADS:
                g = _parse_graphic(child, lambda p: p)
                if g is not None:
                    graphics.append(g)
        edge_items = [g for g in graphics if g.layer == EDGE_CUTS]
        for fp in footprints:
            edge_items.extend(g for g in fp.graphics if g.layer == EDGE_CUTS)
        polylines = []
        for g in edge_items:
            pts = list(g.points)
            if g.closed and pts:
                pts.append(pts[0])
            polylines.append(pts)
        closed, open_chains = chain_outline(polylines, OUTLINE_CHAINING_EPSILON_MM)
        title: dict[str, str] = {}
        tb = root.find("title_block")
        if tb is not None:
            for c in tb.nodes:
                if c.head and c.atoms:
                    title[c.head] = " ".join(c.values())
        board = cls(
            path=path,
            root=root,
            stamp=stamp,
            generator=root.get("generator") or "",
            generator_version=root.get("generator_version"),
            layers=tuple(layers),
            nets=nets.nets(),
            footprints=footprints,
            tracks=tuple(tracks),
            arcs=tuple(arcs),
            vias=tuple(vias),
            zones=tuple(zones),
            graphics=tuple(graphics),
            outline=tuple(nest_rings(closed)),
            outline_open=tuple(open_chains),
            design_rules=_parse_setup(root.find("setup")),
            net_codes_in_file=nets.has_codes,
            paper=root.get("paper"),
            title_block=title,
        )
        return board

    # queries ----------------------------------------------------------------------------------
    @property
    def copper_layers(self) -> tuple[str, ...]:
        return tuple(layer.name for layer in self.layers if layer.is_copper)

    def layer(self, name: str) -> Layer | None:
        return next((layer for layer in self.layers if layer.name == name), None)

    def net_by_name(self) -> dict[str, Net]:
        return {n.name: n for n in self.nets}

    def net(self, name: str) -> Net | None:
        return self.net_by_name().get(name)

    def footprint(self, reference: str) -> Footprint | None:
        return next((fp for fp in self.footprints if fp.reference == reference), None)

    def pads(self) -> Iterator[tuple[Footprint, Pad]]:
        for fp in self.footprints:
            for pad in fp.pads:
                yield fp, pad

    def outline_bbox(self) -> BBox | None:
        pts: list[Vec2] = []
        for poly in self.outline:
            pts.extend(poly.points)
        for chain in self.outline_open:
            pts.extend(chain)
        return BBox.of(pts)

    def board_polygon(self) -> Polygon | None:
        """The outer outline ring (largest by area) with its cutouts, or None without a closed
        outline."""
        if not self.outline:
            return None
        return max(self.outline, key=lambda p: abs(p.signed_area()))

    def items_by_net(self, net_name: str) -> dict[str, list[Any]]:
        """`{"pads": [(fp, pad)…], "tracks": […], "arcs": […], "vias": […], "zones": […]}`."""
        out: dict[str, list[Any]] = {"pads": [], "tracks": [], "arcs": [], "vias": [], "zones": []}
        for fp, pad in self.pads():
            if pad.net is not None and pad.net.name == net_name:
                out["pads"].append((fp, pad))
        out["tracks"] = [t for t in self.tracks if t.net is not None and t.net.name == net_name]
        out["arcs"] = [a for a in self.arcs if a.net is not None and a.net.name == net_name]
        out["vias"] = [v for v in self.vias if v.net is not None and v.net.name == net_name]
        out["zones"] = [z for z in self.zones if z.net is not None and z.net.name == net_name]
        return out
