"""Specctra DSN writer over `kiwrap.formats.board.BoardFile` — the SWIG-free twin of KiCad's
`ExportSpecctraDSN` (pcbnew/specctra_import_export/specctra_export.cpp, tag 10.0.6, whose
conventions the 10.99 nightly keeps for every padstack a `.kicad_pcb` of this model can hold).

What is reproduced, section by section (verified against KiCad's own export of the same boards
in conformance test T7, `conformance/tests/t7_specctra_codec.py`):

* `(parser …)`, `(resolution um 10)`, `(unit um)`: coordinates in micrometres, y flipped
  (`kiwrap.formats.specctra.format`);
* `(structure …)`: one `(layer <id> (type signal|power|jumper) (property (index N)))` per copper
  layer in stack order (F.Cu, In1.Cu …, B.Cu). The layer id is the board's *user* layer name when
  one is set (`BOARD::GetLayerName`; ecc83 exports `top_cu`/`bottom_cu`), `mixed` maps to
  `signal` because Freerouting knows only signal/power. `(boundary (path pcb 0 …))` is the closed
  Edge.Cuts outline (arcs polygonised, `OUTLINE_CHAINING_EPSILON`); every cutout becomes a
  `(keepout "" (polygon signal 0 …))`. Copper zones that are not rule areas are `(plane <net>
  (polygon <layer> 0 …) (window …)*)` per copper layer (netless zones get `@:no_net_N`), rule
  areas `(keepout|via_keepout|wire_keepout "" (polygon <layer> 0 …))` per copper layer. `(via …)`
  lists the via padstacks (Default class first, then the other classes in use, then those of
  existing vias); `(rule (width) (clearance) (clearance C/4 (type smd_smd)))` from the Default
  class. The copper-to-edge clearance is NOT in the DSN (KiCad omits it; the router gets it as a
  flag, VERIFICATION claim 8);
* `(placement …)`: `(component <image id> (place <ref> x y front|back rot (PN <value>)))`,
  x / y / rot printed with ten significant digits (`PLACE::Format` uses `FormatDouble2Str`,
  every other number `%.6g`). Images are the footprint seen from the top: back-side footprints
  are flipped as `FOOTPRINT::Flip( TOP_BOTTOM )` does, so a back part's local pad coordinates
  keep their y sign in DSN space, its pad layers AND its rule areas' layers mirror through the
  stack (`ZONE::Flip` flips the layer set: a B.Cu-only keepout of a back part is `F.Cu` in the
  image and comes back to B.Cu through the `back` placement), its pin rotation is `fp_rot -
  pad_rot` and its place rotation `(180 + fp_rot) mod 360`; a front part's pin rotation is
  `pad_rot - fp_rot` and its place rotation the footprint's own, normalised to (-180, 180].
  Empty or duplicate references become `REF**`, `<ref>_1`, … (case-folded uniqueness);
* `(library …)`: `(image <fpid> (outline …)* (pin <padstack> [(rotate r)] <pin> x y)*
  (keepout …)*)`. Two footprints share an image when their *content* is identical (KiCad hashes
  the formatted contents, not the id); a different content under an already used id is written
  as `<fpid>::N`. Outlines carry the courtyard polygons first (`polygon signal 0`), then the
  footprint's line/circle/rect/arc graphics on every layer (`path signal <width>`; polygons and
  curves are skipped like KiCad does). Pin ids are pad numbers, duplicates suffixed `@1`, `@2`, …
  Copper-less round pads (NPTH with drill >= size, or a circle pad with no copper layer) become
  per-layer `(keepout (circle <layer> <drill + 2·hole_clearance>))`; other copper-less pads are
  skipped. A footprint's rule areas become `(keepout|via_keepout|wire_keepout "" (polygon …))`
  of the image, in image coordinates: from the BOARD-frame outline the file holds on every
  dialect before stamp 20260616 (`STAMP_FP_AFFINE_TRANSFORM`: un-rotated and un-translated like
  `makeIMAGE` does) and straight from the LIBRARY-frame outline a board saved by the 10.99
  nightly / KiCad 11 stores (KiCad master's own exporter still un-rotates that outline as if it
  were board-frame and lands tens of mm off; docs/UPSTREAM.md G17). Padstacks:
  `Round[A]Pad_1600.000000_um`, `Rect[T]Pad_WxH_um`, `Oval[A]Pad_WxH_um` (a path with
  aperture), `Trapz[…]Pad_WxH_pDXxpDY_um`, `RoundRect[T]Pad_WxH_R_um_<chamfer ratio>_<chamfer
  positions hex>` (a polygon from `TransformRoundChamferedRectToPolygon` with KiCad's
  36-segment correction; a `roundrect` pad whose `(chamfer …)` lists a corner is KiCad's
  CHAMFERED_RECT - the file format has no other spelling - and cuts `chamfer_ratio * min(w, h)`
  off those corners exactly like the `doChamfer` branch, mirrored top/bottom for a back part
  like `PAD::Flip`), `Cust[…]Pad_WxH_BWxBH_N_um_<hash>` (convex hull of anchor + primitives). `[A]` marks a
  pad on every copper layer, otherwise `T`, `B` and inner indices name the layers; a drill
  offset adds `[ox,oy]`. Vias: `Via[<top>-<bot>]_<dia>:<drill>_um` with a circle per spanned
  layer; a via without a `(drill …)` token takes the via drill of its net's effective netclass
  (`PCB_VIA::GetDrillValue`), never the Default class's;
* `(network …)`: `(net <name> (pins <ref>-<pin> …))` for every net with at least one pin, in the
  board's net order; the quote character in a name is folded to `''`; then one `(class …)` per
  netclass in use (`kicad_default` for Default) listing every named net of the board in that
  class with `(circuit (use_via …))` and `(rule (width) (clearance))`; a class id is the
  EFFECTIVE netclass name KiCad 9+/10 resolves (`<name>,Default` for a class that leaves a
  parameter to Default, `A,B` for a net in two classes; `kiwrap.formats.specctra.rules`);
* `(wiring …)`: existing tracks as `(wire (path <layer> <width> …)(net …)(type route|fix))`,
  consecutive segments of one net/width/layer chained into one path, locked ones `fix` (KiCad's
  importer deletes every unlocked track/via and re-adds locked ones; claim 7), vias as
  `(via <padstack> x y (net …)(type …))`. Track arcs are written as their start-end chord like
  KiCad does (`arcs="polyline"` writes the polygonised arc instead).

`nets=` restricts what the router may touch the way `kiwrap.backends.router.dsn_post.
restrict_nets` does: nets outside the selection are left out of the `(network …)` altogether
(no `(net …)` entry, no class membership - their pads stay in the images as net-less pins the
router keeps clearance from) and their wiring is written `fix`, so Freerouting has no airwire to
route for them and the SES apply keeps their copper (`SesReader.apply(keep_nets=…)`). The
`kiwrap_ignore` class / `-inc` mechanism was dropped: Freerouting 2.4.1 headless ignores
`router.ignore_net_classes` (Phase 1c finding, verified on the reference board and the spike DSN).
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from kiwrap.formats.board import (
    EDGE_CUTS,
    OUTLINE_CHAINING_EPSILON_MM,
    STAMP_FP_AFFINE_TRANSFORM,
    BoardFile,
    Footprint,
    Pad,
    Zone,
)
from kiwrap.formats.geometry import (
    BBox,
    Polygon,
    Vec2,
    arc_angles,
    arc_points,
    arc_to_segment_count,
    chain_outline,
    nest_rings,
    pad_circle_polygon,
    rotate,
)
from kiwrap.formats.sexpr import Node
from kiwrap.formats.specctra.format import dsn_string, g6, g10, join_ids, kiround, place_pt, pt, quote_id, um
from kiwrap.formats.specctra.rules import DsnRules, NetclassRule, rules_from_project

__all__ = [
    "IGNORE_CLASS",
    "DsnWriter",
    "SpecctraError",
    "copper_stack",
    "via_padstack_id",
    "write_dsn",
]

IGNORE_CLASS = "kiwrap_ignore"  # historical: an older DSN may still carry it (dsn_post dissolves it)
KICAD_DEFAULT_CLASS_ID = "kicad_default"
HOST_CAD = "KiCad's Pcbnew"
ROUNDRECT_SEGMENTS = 36  # circleToSegmentsCount in makePADSTACK
_RIGHT_MARGIN = 70
_INDENT = "  "


class SpecctraError(ValueError):
    """The board cannot be expressed as a DSN / the SES cannot be applied."""


# ----------------------------------------------------------------------------------------------
# layers
# ----------------------------------------------------------------------------------------------


def _copper_sort_key(name: str) -> tuple[int, int]:
    if name == "F.Cu":
        return (0, 0)
    if name == "B.Cu":
        return (2, 0)
    if name.startswith("In") and name.endswith(".Cu") and name[2:-3].isdigit():
        return (1, int(name[2:-3]))
    return (1, 10_000)


def copper_stack(board: BoardFile, layers: Sequence[str] | None = None) -> list[str]:
    """Canonical copper layer names in physical stack order (`LSET::CuStack`)."""
    names = list(layers) if layers is not None else list(board.copper_layers)
    known = {layer.name for layer in board.layers if layer.is_copper}
    for n in names:
        if n not in known:
            raise SpecctraError(f"{n!r} is not a copper layer of this board ({sorted(known)})")
    return sorted(names, key=_copper_sort_key)


def via_padstack_id(diameter_mm: float, drill_mm: float, top: int, bot: int) -> str:
    """`Via[%d-%d]_%.6g:%.6g_um` (`SPECCTRA_DB::makeVia`); the drill rides in the name so the
    importer can recover it."""
    return f"Via[{top}-{bot}]_{g6(um(diameter_mm))}:{g6(um(drill_mm))}_um"


# ----------------------------------------------------------------------------------------------
# geometry
# ----------------------------------------------------------------------------------------------


def _norm360(deg: float) -> float:
    v = math.fmod(deg, 360.0)
    if v < 0:
        v += 360.0
    return 0.0 if v == 0 else v


def _norm180(deg: float) -> float:
    v = math.fmod(deg, 360.0)
    if v <= -180.0:
        v += 360.0
    elif v > 180.0:
        v -= 360.0
    return 0.0 if v == 0 else v


_kiround = kiround  # KiROUND (round half away from zero), shared with `format.um`


def kicad_arc_polyline(start: Vec2, mid: Vec2, end: Vec2, max_error_mm: float) -> list[Vec2]:
    """`SHAPE_ARC::ConvertToPolyline` (libs/kimath/src/geometry/shape_arc.cpp, tag 10.0.6) for a
    zero-width arc: `n = GetArcToSegmentCount(r, err, angle)` segments, the interior vertices at
    the odd half-steps on a circle of radius `r + effectiveError / 2` so the chords straddle the
    arc, start and end exact. This is what board outlines and planes are polygonised with."""
    geo = arc_angles(start, mid, end)
    if geo is None:
        return [start, end]
    c, r, a0, sweep = geo
    half = max(1e-6, max_error_mm / 2)
    # distance of the mid point from the start-end chord (SEG::Distance)
    dx, dy = end.x - start.x, end.y - start.y
    seg_len2 = dx * dx + dy * dy
    if seg_len2 == 0:
        chord_dist = mid.distance(start)
    else:
        t = max(0.0, min(1.0, ((mid.x - start.x) * dx + (mid.y - start.y) * dy) / seg_len2))
        chord_dist = math.hypot(mid.x - (start.x + t * dx), mid.y - (start.y + t * dy))
    if r < half or chord_dist < half:
        n = 0
        effective = r
    else:
        n = arc_to_segment_count(r, max_error_mm, sweep)
        seg360 = int(n * 360.0 / abs(sweep))
        if seg360 <= 2:
            seg360 = 3
        effective = abs(r * (1 - 1 / math.cos(math.pi / seg360)))
        effective = _kiround(effective * 1e6) / 1e6
    r += effective / 2
    n *= 2
    pts = [start]
    for i in range(1, n, 2):
        a = math.radians(a0 + sweep * i / n)
        pts.append(Vec2(c.x + r * math.cos(a), c.y + r * math.sin(a)))
    pts.append(end)
    return pts


#: `RECT_CHAMFER_*` bits in the order of the corner list (-x,-y), (x,-y), (x,y), (-x,y)
_CHAMFER_IDS = (1, 2, 8, 4)
_CHAMFER_SIGN = (0, 1, -1, 0, 0, -1, 1, 0)


def _chamfered_corners(
    sx: int, sy: int, radius: int, chamfer_mask: int, chamfer_ratio: float
) -> list[tuple[int, int, int]]:
    """The `ROUNDED_CORNER` list of `TransformRoundChamferedRectToPolygon` (tag 10.0.6, integer
    nanometres): the four corners with `radius`, a chamfered corner replaced by two radius-0
    corners `chamfer = KiROUND(ratio * min(w, h))` along its edges; duplicates removed when the
    chamfers meet (`CornerListRemoveDuplicates`)."""
    corners: list[list[int]] = [[-sx, -sy, radius], [sx, -sy, radius], [sx, sy, radius], [-sx, sy, radius]]
    if not chamfer_mask:
        return [tuple(c) for c in corners]  # type: ignore[misc]
    shorter = min(2 * sx, 2 * sy)
    chamfer = max(0, _kiround(chamfer_ratio * shorter))
    count = bin(chamfer_mask & 0xFF).count("1")
    pos = 0
    for cc in range(4):
        if not (chamfer_mask & _CHAMFER_IDS[cc]):
            pos += 1
            continue
        corners[pos][2] = 0
        if chamfer == 0:
            pos += 1
            continue
        corners.insert(pos + 1, list(corners[pos]))
        corners[pos][0] += _CHAMFER_SIGN[(2 * cc) & 7] * chamfer
        corners[pos][1] += _CHAMFER_SIGN[(2 * cc - 2) & 7] * chamfer
        corners[pos + 1][0] += _CHAMFER_SIGN[(2 * cc + 1) & 7] * chamfer
        corners[pos + 1][1] += _CHAMFER_SIGN[(2 * cc - 1) & 7] * chamfer
        pos += 2
    if count > 1 and 2 * chamfer >= shorter:
        prev = (corners[0][0], corners[0][1])
        for i in range(len(corners) - 1, -1, -1):
            if (corners[i][0], corners[i][1]) == prev:
                del corners[i]
            else:
                prev = (corners[i][0], corners[i][1])
    return [tuple(c) for c in corners]  # type: ignore[misc]


def _roundrect_polygon(
    w_mm: float,
    h_mm: float,
    radius_mm: float,
    max_error_mm: float,
    *,
    chamfer_mask: int = 0,
    chamfer_ratio: float = 0.0,
) -> list[Vec2]:
    """`TransformRoundChamferedRectToPolygon` (ERROR_INSIDE, no rotation) after the exporter's
    36-segment correction, in KiCad's y-down millimetres, closed (first point repeated). The
    chamfer (`chamfer_mask` = `Pad.chamfer_mask`, `chamfer_ratio`) follows the `doChamfer` branch
    of `makePADSTACK`: cut on the inflated size, listed corners become two sharp ones.

    Integer nanometre arithmetic mirrors `CornerListToPolygon` closely enough that the vertices
    agree with KiCad's to a few nanometres (verified against spikes/autorouting/swigwd/tiny2.dsn).
    """
    nm = 1e6
    r0 = _kiround(radius_mm * nm)
    extra = _kiround(r0 * (1.0 - math.cos(math.pi / ROUNDRECT_SEGMENTS)))
    sx = (_kiround(w_mm * nm) + extra * 2) // 2
    sy = (_kiround(h_mm * nm) + extra * 2) // 2
    radius = r0 + extra
    corners = _chamfered_corners(sx, sy, max(0, radius), chamfer_mask, chamfer_ratio)
    if radius <= 0 or all(c[2] <= 0 for c in corners):
        pts = [Vec2(x / nm, y / nm) for x, y, _r in corners]
        return pts + [pts[0]]
    err = max(1, _kiround(max_error_mm * nm))
    num_segs = max(16, arc_to_segment_count(radius / nm, err / nm, 360.0))
    ang_delta = 360.0 / num_segs
    last_seg = 90.0
    while last_seg > ang_delta:
        last_seg -= ang_delta
    ang_start = ang_delta if abs(last_seg) < 1e-9 else (ang_delta + last_seg) / 2
    out: list[Vec2] = []
    count = len(corners)
    incoming = (corners[0][0] - corners[-1][0], corners[0][1] - corners[-1][1])
    for n in range(count):
        cx, cy, cr = corners[n]
        nx, ny, _nr = corners[(n + 1) % count]
        outgoing = (nx - cx, ny - cy)
        if cr <= 0:
            out.append(Vec2(cx / nm, cy / nm))
            incoming = outgoing
            continue
        ilen = math.hypot(*incoming)
        ux, uy = incoming[0] / ilen, incoming[1] / ilen  # unit incoming
        px, py = -uy, ux  # Perpendicular()
        arc_start = (cx - ux * cr, cy - uy * cr)
        center = (arc_start[0] + px * cr, arc_start[1] + py * cr)
        # reflect arc_start across the line corner -> center
        dx, dy = center[0] - cx, center[1] - cy
        dl = math.hypot(dx, dy)
        dx, dy = dx / dl, dy / dl
        vx, vy = arc_start[0] - cx, arc_start[1] - cy
        proj = vx * dx + vy * dy
        arc_end = (cx + 2 * proj * dx - vx, cy + 2 * proj * dy - vy)
        origin = Vec2(arc_start[0] - center[0], arc_start[1] - center[1])
        out.append(Vec2(arc_start[0] / nm, arc_start[1] / nm))
        ang = ang_start
        while ang < 90.0 - 1e-9:
            p = rotate(origin, -ang)
            out.append(Vec2((p.x + center[0]) / nm, (p.y + center[1]) / nm))
            ang += ang_delta
        out.append(Vec2(arc_end[0] / nm, arc_end[1] / nm))
        incoming = outgoing
    out.append(out[0])
    return out


def _convex_hull(points: Iterable[Vec2]) -> list[Vec2]:
    """Andrew's monotone chain; returns the hull counter-clockwise in y-up terms (closed later)."""
    pts = sorted({(p.x, p.y) for p in points})
    if len(pts) <= 2:
        return [Vec2(x, y) for x, y in pts]

    def cross(o: tuple[float, float], a: tuple[float, float], b: tuple[float, float]) -> float:
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: list[tuple[float, float]] = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper: list[tuple[float, float]] = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    hull = lower[:-1] + upper[:-1]
    return [Vec2(x, y) for x, y in hull]


# ----------------------------------------------------------------------------------------------
# intermediate model (formatted pieces, deduplicated like KiCad does)
# ----------------------------------------------------------------------------------------------


@dataclass
class _Padstack:
    id: str
    shapes: list[str]  # formatted `(shape …)` lines

    def text(self) -> str:
        return "".join(f"{_INDENT * 3}{s}\n" for s in self.shapes) + f"{_INDENT * 3}(attach off)\n"


@dataclass
class _Pin:
    padstack: str
    rotation: float
    pin_id: str
    x: float  # DSN um
    y: float
    net: str | None

    def text(self) -> str:
        rot = f" (rotate {g6(self.rotation)})" if self.rotation != 0.0 else ""
        return f"(pin {quote_id(self.padstack)}{rot} {quote_id(self.pin_id)} {g6(self.x)} {g6(self.y)})"


@dataclass
class _Image:
    image_id: str
    outlines: list[str] = field(default_factory=list)
    pins: list[_Pin] = field(default_factory=list)
    keepouts: list[str] = field(default_factory=list)
    duplicated: int = 0

    @property
    def full_id(self) -> str:
        return f"{self.image_id}::{self.duplicated}" if self.duplicated else self.image_id

    def content(self) -> str:
        """What KiCad hashes: everything but the id."""
        parts = list(self.outlines) + [p.text() for p in self.pins] + list(self.keepouts)
        return "\n".join(parts)


@dataclass
class _Wire:
    layer: str
    width: float  # um
    points: list[tuple[float, float]]  # DSN um
    net: str
    fixed: bool


@dataclass
class _WireVia:
    padstack: str
    x: float
    y: float
    net: str
    fixed: bool


# ----------------------------------------------------------------------------------------------
# the writer
# ----------------------------------------------------------------------------------------------


class DsnWriter:
    """Build the DSN text for `board` (see the module docstring for the conventions).

    `rules` defaults to `rules_from_project(board)` (the `.kicad_pro` next to the board or KiCad's
    built-in defaults); `layers` selects/orders the copper layers to export (default: all, stack
    order); `nets` restricts routing to the named nets (the others leave the network and keep
    fixed wiring); `name` is the `(pcb <name> …)` token (KiCad writes the output path);
    `layer_directions` adds `(direction horizontal|vertical)` hints per canonical layer name;
    `arcs="chord"` writes track arcs as their start-end chord exactly like KiCad (the arc itself is
    lost either way when the router replaces unlocked copper), `"polyline"` writes the polygonised
    arc so a locked arc is a faithful obstacle. `warnings` collects what was skipped or approximated.
    """

    def __init__(
        self,
        board: BoardFile,
        *,
        rules: DsnRules | None = None,
        layers: Sequence[str] | None = None,
        nets: Iterable[str] | None = None,
        name: str | None = None,
        host_version: str = "kiwrap",
        layer_directions: Mapping[str, str] | None = None,
        max_error_mm: float | None = None,
        arcs: str = "chord",
    ) -> None:
        self.board = board
        self.rules = rules if rules is not None else rules_from_project(board)
        self.copper = copper_stack(board, layers)
        self.layer_ids = [self._dsn_layer_id(n) for n in self.copper]
        self.index_of = {name: i for i, name in enumerate(self.copper)}
        self.route_nets: set[str] | None = set(nets) if nets is not None else None
        self.name = name or (board.path.name if board.path is not None else "board.dsn")
        self.host_version = host_version
        self.layer_directions = dict(layer_directions or {})
        self.max_error = max_error_mm if max_error_mm is not None else self.rules.max_error
        if arcs not in ("chord", "polyline"):
            raise SpecctraError("arcs must be 'chord' (KiCad's export) or 'polyline'")
        self.arcs = arcs
        self.warnings: list[str] = []
        self._padstacks: dict[str, _Padstack] = {}
        self._via_padstacks: dict[str, _Padstack] = {}
        self._images: list[_Image] = []
        self._image_by_content: dict[str, _Image] = {}
        self._components: dict[str, list[str]] = {}  # image full id -> place lines
        self._component_order: list[str] = []
        self._net_pins: dict[str, list[str]] = {}
        self._wires: list[_Wire] = []
        self._wire_vias: list[_WireVia] = []
        self._netless_zones = 0
        self._extra_nets: list[str] = []
        self.component_ids: list[str] = []  # per footprint, board order
        self._built = False

    # -- public --------------------------------------------------------------------------------

    def write(self) -> str:
        if not self._built:
            self._build()
        return self._format()

    def __str__(self) -> str:
        return self.write()

    # -- layers --------------------------------------------------------------------------------

    def _dsn_layer_id(self, canonical: str) -> str:
        layer = self.board.layer(canonical)
        if layer is not None and layer.user_name:
            return layer.user_name
        return canonical

    def _layer_type(self, canonical: str) -> str:
        layer = self.board.layer(canonical)
        kind = layer.kind if layer is not None else "signal"
        if kind == "power":
            return "power"
        if kind == "jumper":
            return "jumper"
        return "signal"  # signal and mixed (Freerouting knows only signal/power)

    # -- nets ----------------------------------------------------------------------------------

    @staticmethod
    def _pad_net(pad: Pad) -> str | None:
        net = pad.net
        if net is None or net.name == "" or (net.code is not None and net.code <= 0):
            return None
        return dsn_string(net.name)

    def _board_net_names(self) -> list[str]:
        return [
            dsn_string(n.name) for n in self.board.nets if n.name != "" and (n.code is None or n.code > 0)
        ]

    def _class_of(self, net: str) -> NetclassRule:
        return self.rules.class_of(net)

    def _routable(self, net: str) -> bool:
        return self.route_nets is None or net in self.route_nets

    # -- build ---------------------------------------------------------------------------------

    def _build(self) -> None:
        self._built = True
        top, bot = 0, len(self.copper) - 1
        default = self.rules.default
        self._register_via(default.via_diameter, default.via_drill, top, bot)
        for cls in self._classes_in_use():
            self._register_via(cls.via_diameter, cls.via_drill, top, bot)
        self._build_components()
        self._build_wiring()

    def _register_via(self, dia: float, drill: float, top: int, bot: int) -> str:
        vid = via_padstack_id(dia, drill, top, bot)
        if vid not in self._via_padstacks:
            shapes = [
                f"(shape (circle {quote_id(self.layer_ids[i])} {g6(um(dia))}))" for i in range(top, bot + 1)
            ]
            self._via_padstacks[vid] = _Padstack(vid, shapes)
        return vid

    def _classes_in_use(self) -> list[NetclassRule]:
        seen: dict[str, NetclassRule] = {}
        for net in self._board_net_names():
            cls = self._class_of(net)
            if not cls.is_default and cls.name not in seen:
                seen[cls.name] = cls
        return list(seen.values())

    # components / images ----------------------------------------------------------------------

    def _component_ids(self) -> list[str]:
        """One component id per footprint (board order): the reference, `REF**` when empty,
        `<ref>_N` for case-insensitive duplicates (FromBOARD's uniquifier)."""
        used: set[str] = set()
        out: list[str] = []
        for fp in self.board.footprints:
            ref = fp.reference or "REF**"
            unique = ref
            suffix = 1
            while unique.lower() in used:
                unique = f"{ref}_{suffix}"
                suffix += 1
            used.add(unique.lower())
            out.append(unique)
        return out

    def _build_components(self) -> None:
        self.component_ids = self._component_ids()
        for fp, comp_id in zip(self.board.footprints, self.component_ids, strict=True):
            image = self._make_image(fp)
            for pin in image.pins:
                if pin.net is not None:
                    # PIN_REF::FormatIt quotes the two halves separately
                    ref = f"{quote_id(comp_id)}-{quote_id(pin.pin_id)}"
                    self._net_pins.setdefault(pin.net, []).append(ref)
            registered = self._register_image(image)
            back = fp.side == "back"
            if back:
                rotation = _norm360(180.0 + fp.rotation)
            else:
                rotation = _norm180(fp.rotation)
            place = (
                f"(place {quote_id(comp_id)} {place_pt(fp.position.x, fp.position.y)} "
                f"{'back' if back else 'front'} {g10(rotation)} (PN {quote_id(dsn_string(fp.value))}))"
            )
            key = registered.full_id
            if key not in self._components:
                self._components[key] = []
                self._component_order.append(key)
            self._components[key].append(place)

    def _register_image(self, image: _Image) -> _Image:
        content = image.content()
        found = self._image_by_content.get(content)
        if found is not None:
            return found
        dups = sum(1 for im in self._images if im.image_id == image.image_id)
        image.duplicated = dups
        self._images.append(image)
        self._image_by_content[content] = image
        return image

    def _make_image(self, fp: Footprint) -> _Image:
        image = _Image(fp.fpid)
        back = fp.side == "back"
        n_layers = len(self.copper)
        pinmap: dict[str, int] = {}

        def local(p: Vec2) -> Vec2:
            """Board -> image coordinates (KiCad y-down, footprint-relative, flipped to the top)."""
            v = rotate(p - fp.position, -fp.rotation)
            return Vec2(v.x, -v.y) if back else v

        def dsn_local(p: Vec2) -> str:
            v = local(p)
            return f"{g6(um(v.x))} {g6(-um(v.y))}"

        for pad in fp.pads:
            lp = pad.local_position
            lpos = Vec2(lp.x, -lp.y) if back else lp
            if self._is_round_keepout(pad):
                dia = (pad.drill.diameter if pad.drill else pad.size.x) + 2 * self.rules.hole_clearance
                for lid in self.layer_ids:
                    image.keepouts.append(
                        f"(keepout (circle {quote_id(lid)} {g6(um(dia))} {g6(um(lpos.x))} {g6(-um(lpos.y))}))"
                    )
                continue
            layers = self._pad_copper_layers(pad, back)
            if not layers:
                continue
            padstack = self._pad_padstack(pad, back, layers, n_layers)
            if padstack is None:
                continue
            number = pad.number
            if number != "" and number not in pinmap:
                pinmap[number] = 0
                pin_id = number
            else:
                pinmap[number] = pinmap.get(number, 0) + 1
                pin_id = f"{number}@{pinmap[number]}"
            rot = _norm360((fp.rotation - pad.rotation) if back else (pad.rotation - fp.rotation))
            image.pins.append(_Pin(padstack.id, rot, pin_id, um(lpos.x), -um(lpos.y), self._pad_net(pad)))

        # courtyards first (both sides, Simplify'd by KiCad: identical rings folded)
        seen_rings: set[str] = set()
        for poly in (*fp.courtyard_front, *fp.courtyard_back):
            ring = _dedupe_closed(poly.points)
            text = "  ".join(dsn_local(p) for p in ring)
            if text in seen_rings:
                continue
            seen_rings.add(text)
            image.outlines.append(f"(outline (polygon signal 0  {text}))")
        for g in fp.graphics:
            if g.layer in ("F.CrtYd", "B.CrtYd"):
                continue
            if g.kind == "line":
                pts = list(g.points)
            elif g.kind == "rect":
                pts = list(g.points) + [g.points[0]]
            elif g.kind in ("circle", "arc"):
                pts = list(g.points) + ([g.points[0]] if g.closed else [])
            else:
                continue  # poly / curve: KiCad skips them too
            text = "  ".join(dsn_local(p) for p in pts)
            image.outlines.append(f"(outline (path signal {g6(um(g.width))}  {text}))")
        library_frame = self.board.stamp is not None and self.board.stamp >= STAMP_FP_AFFINE_TRANSFORM

        def dsn_library(p: Vec2) -> str:
            """Library-frame (footprint-relative) outline -> image coordinates: the flip to the top
            mirrors a back part's outline about its origin (`ZONE::Mirror` through the inverse
            transform), so the y sign is kept for `back` like the pads' local coordinates."""
            return f"{g6(um(p.x))} {g6(um(p.y) if back else -um(p.y))}"

        zone_mapper = dsn_library if library_frame else dsn_local
        for zone in _footprint_rule_areas(fp):
            kind = _keepout_kind(zone)
            indices = [self.index_of[layer] for layer in self._zone_copper_layers(zone)]
            if back:
                # ZONE::Flip mirrors the layer set through the stack, exactly like the pads
                indices = sorted(n_layers - 1 - i for i in indices)
            for idx in indices:
                lid = self.layer_ids[idx]
                image.keepouts.append(self._polygon_keepout(kind, "", lid, zone, zone_mapper, nest=3))
        return image

    def _is_round_keepout(self, pad: Pad) -> bool:
        """`isRoundKeepout`: a circle pad whose drill swallows the copper, or with no copper layer."""
        if pad.shape != "circle":
            return False
        if pad.drill is not None and pad.drill.diameter >= pad.size.x:
            return True
        return not pad.expand_layers(self.board.copper_layers)

    def _pad_copper_layers(self, pad: Pad, back: bool) -> list[int]:
        """Stack indices of the copper layers the pad's copper sits on, after the top flip."""
        expanded = set(pad.expand_layers(self.board.copper_layers))
        idx = [self.index_of[n] for n in self.copper if n in expanded]
        if back:
            last = len(self.copper) - 1
            idx = sorted(last - i for i in idx)
        return idx

    def _pad_padstack(self, pad: Pad, back: bool, layers: list[int], n_layers: int) -> _Padstack | None:
        on_all = len(layers) == n_layers
        uniq = "["
        if on_all:
            uniq += "A"
        else:
            for i in layers:
                uniq += "T" if i == 0 else ("B" if i == n_layers - 1 else str(i))
        uniq += "]"
        offset = pad.drill.offset if pad.drill is not None else Vec2(0.0, 0.0)
        ox, oy = um(offset.x), -um(offset.y)
        if back:
            oy = -oy
        if ox != 0.0 or oy != 0.0:
            uniq += f"[{ox:.6f},{oy:.6f}]"
        names = [self.layer_ids[i] for i in layers]
        w, h = um(pad.size.x), um(pad.size.y)
        shapes: list[str]
        if pad.shape == "circle":
            off = f" {g6(ox)} {g6(oy)}" if (ox != 0.0 or oy != 0.0) else ""
            shapes = [f"(shape (circle {quote_id(n)} {g6(w)}{off}))" for n in names]
            psid = f"Round{uniq}Pad_{w:.6f}_um"
        elif pad.shape == "rect":
            x0, y0, x1, y1 = -w / 2 + ox, -h / 2 + oy, w / 2 + ox, h / 2 + oy
            shapes = [f"(shape (rect {quote_id(n)} {g6(x0)} {g6(y0)} {g6(x1)} {g6(y1)}))" for n in names]
            psid = f"Rect{uniq}Pad_{w:.6f}x{h:.6f}_um"
        elif pad.shape == "oval":
            dx, dy = w / 2, h / 2
            dr = dx - dy
            if dr >= 0:
                radius, p0, p1 = dy, (-dr, 0.0), (dr, 0.0)
            else:
                radius, p0, p1 = dx, (0.0, dr), (0.0, -dr)
            p0 = (p0[0] + ox, p0[1] + oy)
            p1 = (p1[0] + ox, p1[1] + oy)
            shapes = [
                f"(shape (path {quote_id(n)} {g6(2 * radius)}  {g6(p0[0])} {g6(p0[1])}  {g6(p1[0])} {g6(p1[1])}))"
                for n in names
            ]
            psid = f"Oval{uniq}Pad_{w:.6f}x{h:.6f}_um"
        elif pad.shape == "trapezoid":
            delta = pad.rect_delta or Vec2(0.0, 0.0)
            ddx_mm, ddy_mm = delta.x, (-delta.y if back else delta.y)
            dx, dy = w / 2, h / 2
            ddx, ddy = um(ddx_mm) / 2, um(ddy_mm) / 2
            corners = [
                (-dx - ddy, -dy - ddx),
                (-dx + ddy, dy + ddx),
                (dx - ddy, dy - ddx),
                (dx + ddy, -dy + ddx),
            ]
            text = "  ".join(f"{g6(x + ox)} {g6(y + oy)}" for x, y in corners)
            shapes = [f"(shape (polygon {quote_id(n)} 0  {text}))" for n in names]
            sx = "n" if ddx_mm < 0 else "p"
            sy = "n" if ddy_mm < 0 else "p"
            psid = f"Trapz{uniq}Pad_{w:.6f}x{h:.6f}_{sx}{abs(um(ddx_mm)):.6f}x{sy}{abs(um(ddy_mm)):.6f}_um"
        elif pad.shape in ("roundrect", "chamfered_rect"):
            ratio = pad.roundrect_rratio if pad.roundrect_rratio is not None else 0.25
            r0 = min(pad.size.x, pad.size.y) * ratio
            # `doChamfer` for KiCad's CHAMFERED_RECT: a `roundrect` pad whose `(chamfer …)` names
            # a corner (the file format's only spelling; 10.0.6's RoyalBlue54L demo has 8 of them)
            do_chamfer = pad.effective_shape == "chamfered_rect"
            chamfer_ratio = (
                (pad.chamfer_ratio if pad.chamfer_ratio is not None else 0.2) if do_chamfer else 0.0
            )
            chamfer_mask = pad.chamfer_mask if do_chamfer else 0
            if back and chamfer_mask:
                # the flip to the top mirrors y: top corners become bottom corners
                chamfer_mask = ((chamfer_mask & 0b0011) << 2) | ((chamfer_mask & 0b1100) >> 2)
            ring = _roundrect_polygon(
                pad.size.x,
                pad.size.y,
                r0,
                self.max_error,
                chamfer_mask=chamfer_mask,
                chamfer_ratio=chamfer_ratio,
            )
            text = "  ".join(f"{g6(um(p.x) + ox)} {g6(-um(p.y) + oy)}" for p in ring)
            shapes = [f"(shape (polygon {quote_id(n)} 0  {text}))" for n in names]
            nm = 1e6
            rr = _kiround(r0 * nm)
            rr += _kiround(rr * (1.0 - math.cos(math.pi / ROUNDRECT_SEGMENTS)))
            psid = (
                f"RoundRect{uniq}Pad_{w:.6f}x{h:.6f}_{rr / 1000.0:.6f}_um_"
                f"{chamfer_ratio if do_chamfer else 0.0:.6f}_{chamfer_mask if do_chamfer else 0:X}"
            )
        elif pad.shape == "custom":
            hull = self._custom_hull(pad, back)
            if len(hull) < 3:
                self.warnings.append(f"pad {pad.number!r}: custom pad without primitives skipped")
                return None
            text = "  ".join(f"{g6(um(p.x) + ox)} {g6(-um(p.y) + oy)}" for p in hull)
            shapes = [f"(shape (polygon {quote_id(n)} 0  {text}))" for n in names]
            xs = [p.x for p in hull]
            ys = [p.y for p in hull]
            bw, bh = um(max(xs) - min(xs)), um(max(ys) - min(ys))
            digest = hashlib.md5(text.encode("utf-8")).hexdigest().upper()  # noqa: S324 - identity only
            psid = f"Cust{uniq}Pad_{w:.6f}x{h:.6f}_{bw:.6f}x{bh:.6f}_{len(hull)}_um_{digest}"
        else:
            self.warnings.append(f"pad {pad.number!r}: unsupported shape {pad.shape!r} exported as rect")
            x0, y0, x1, y1 = -w / 2 + ox, -h / 2 + oy, w / 2 + ox, h / 2 + oy
            shapes = [f"(shape (rect {quote_id(n)} {g6(x0)} {g6(y0)} {g6(x1)} {g6(y1)}))" for n in names]
            psid = f"Rect{uniq}Pad_{w:.6f}x{h:.6f}_um"
        ps = self._padstacks.get(psid)
        if ps is None:
            ps = _Padstack(psid, shapes)
            self._padstacks[psid] = ps
        return ps

    def _custom_hull(self, pad: Pad, back: bool) -> list[Vec2]:
        """Convex hull of anchor + primitives in pad-local (unrotated, top-view) coordinates,
        closed; `EXPORT_CUSTOM_PADS_CONVEX_HULL` is defined in KiCad's exporter.

        `PAD::MergePrimitivesAsPolygon` strokes every primitive with its `(width w)` (round
        corners, so a filled polygon grows by w/2 with rounded vertices) before the hull is taken;
        each vertex therefore contributes a circle of radius w/2, polygonised with the board's
        max error exactly like `TransformCircleToPolygon( ERROR_INSIDE )`."""
        points: list[Vec2] = []

        def add(p: Vec2, radius: float) -> None:
            q = Vec2(p.x, -p.y) if back else p
            if radius > 0:
                # the rounded offset corner is tangent to the sides, so its axis extremes are exact
                points.extend(_pad_circle(q, radius, self.max_error))
                points.extend(
                    (
                        Vec2(q.x + radius, q.y),
                        Vec2(q.x - radius, q.y),
                        Vec2(q.x, q.y + radius),
                        Vec2(q.x, q.y - radius),
                    )
                )
            else:
                points.append(q)

        w, h = pad.size.x / 2, pad.size.y / 2
        anchor = "rect"
        prims: Node | None = None
        if pad.node is not None:
            options = pad.node.find("options")
            if options is not None:
                anchor = options.get("anchor") or "rect"
            prims = pad.node.find("primitives")
        if anchor == "circle":
            add(Vec2(0.0, 0.0), w)
        else:
            for corner in (Vec2(-w, -h), Vec2(w, -h), Vec2(w, h), Vec2(-w, h)):
                add(corner, 0.0)
        for prim in prims.nodes if prims is not None else ():
            width = _prim_width(prim)
            half = width / 2
            head = prim.head
            if head == "gr_poly":
                pts_node = prim.find("pts")
                for xy in pts_node.find_all("xy") if pts_node is not None else ():
                    a = xy.atoms
                    if len(a) >= 2:
                        add(Vec2(a[0].as_float(), a[1].as_float()), half)
            elif head == "gr_line":
                for key in ("start", "end"):
                    v = _xy_of(prim, key)
                    if v is not None:
                        add(v, half)
            elif head == "gr_rect":
                s0, e0 = _xy_of(prim, "start"), _xy_of(prim, "end")
                if s0 is not None and e0 is not None:
                    for corner in (s0, Vec2(e0.x, s0.y), e0, Vec2(s0.x, e0.y)):
                        add(corner, half)
            elif head == "gr_circle":
                c0, e0 = _xy_of(prim, "center"), _xy_of(prim, "end")
                if c0 is not None and e0 is not None:
                    add(c0, c0.distance(e0) + half)
            elif head == "gr_arc":
                s0, m0, e0 = _xy_of(prim, "start"), _xy_of(prim, "mid"), _xy_of(prim, "end")
                if s0 is not None and m0 is not None and e0 is not None:
                    for v in arc_points(s0, m0, e0, self.max_error):
                        add(v, half)
        hull = _convex_hull(points)
        if len(hull) >= 3:
            hull.append(hull[0])
        return hull

    def _zone_copper_layers(self, zone: Zone) -> list[str]:
        """Copper layers of a zone in stack order (`*.Cu` and `F&B.Cu` wildcards expanded)."""
        if "*.Cu" in zone.layers:
            return list(self.copper)
        wanted = set(zone.layers)
        if "F&B.Cu" in wanted:
            wanted.update(("F.Cu", "B.Cu"))
        return [n for n in self.copper if n in wanted]

    def _polygon_keepout(self, kind: str, name: str, layer_id: str, zone: Zone, mapper, nest: int) -> str:
        """`(kind "name" (polygon L 0 …) [ (window (polygon L 0 …)) … ])` formatted like KEEPOUT::Format."""
        if not zone.outline:
            return f"({kind} {quote_id(name)})"
        main = _dedupe_closed(_zone_main_ring(zone))
        text = "  ".join(mapper(p) for p in main)
        head = f"({kind} {quote_id(name)} (polygon {quote_id(layer_id)} 0  {text})"
        holes = [*(h for h in zone.outline[1:]), *(Polygon.of(h) for h in zone.outline[0].holes)]
        if not holes:
            return head + ")"
        lines = [head]
        for hole in holes:
            ring = _dedupe_closed(hole.points)
            htext = "  ".join(mapper(p) for p in ring)
            lines.append(f"{_INDENT * (nest + 1)}(window (polygon {quote_id(layer_id)} 0  {htext}))")
        lines.append(f"{_INDENT * nest})")
        return "\n".join(lines)

    # wiring -----------------------------------------------------------------------------------

    def _build_wiring(self) -> None:
        top, bot = 0, len(self.copper) - 1
        current: _Wire | None = None
        items: list[tuple[str, float, str, list[Vec2], bool]] = []
        for t in self.board.tracks:
            if t.net is None:
                continue
            items.append(
                (dsn_string(t.net.name), t.width, t.layer, [t.start, t.end], _is_locked(t.locked, t.node))
            )
        for a in self.board.arcs:
            if a.net is None:
                continue
            items.append(
                (
                    dsn_string(a.net.name),
                    a.width,
                    a.layer,
                    a.points(self.max_error) if self.arcs == "polyline" else [a.start, a.end],
                    _is_locked(a.locked, a.node),
                )
            )
        for net, width, layer, pts, locked in items:
            if net == "" or layer not in self.index_of:
                continue
            fixed = locked or not self._routable(net)
            start = (um(pts[0].x), -um(pts[0].y))
            if (
                current is None
                or current.net != net
                or current.width != um(width)
                or current.layer != layer
                or current.fixed != fixed
                or current.points[-1] != start
            ):
                current = _Wire(layer, um(width), [start], net, fixed)
                self._wires.append(current)
            for p in pts[1:]:
                current.points.append((um(p.x), -um(p.y)))
        for via in self.board.vias:
            if via.net is None or via.net.name == "":
                continue
            net = dsn_string(via.net.name)
            layers = [self.index_of[n] for n in via.layers if n in self.index_of]
            if not layers:
                lo, hi = top, bot
            else:
                lo, hi = min(layers), max(layers)
            # `PCB_VIA::GetDrillValue`: an undefined drill is the via's own net's netclass drill
            vid = self._register_via(via.size, via.drill or self._class_of(net).via_drill, lo, hi)
            self._wire_vias.append(
                _WireVia(
                    vid,
                    um(via.position.x),
                    -um(via.position.y),
                    net,
                    _is_locked(via.locked, via.node) or not self._routable(net),
                )
            )

    # -- format --------------------------------------------------------------------------------

    def _format(self) -> str:
        out: list[str] = []
        w = out.append
        w(f"(pcb {quote_id(self.name)}\n")
        w(f"{_INDENT}(parser\n")
        w(f'{_INDENT * 2}(string_quote ")\n')
        w(f"{_INDENT * 2}(space_in_quoted_tokens on)\n")
        w(f'{_INDENT * 2}(host_cad "{HOST_CAD}")\n')
        w(f'{_INDENT * 2}(host_version "{self.host_version}")\n')
        w(f"{_INDENT})\n")
        w(f"{_INDENT}(resolution um 10)\n")
        w(f"{_INDENT}(unit um)\n")
        self._format_structure(out)
        self._format_placement(out)
        self._format_library(out)
        self._format_network(out)
        self._format_wiring(out)
        w(")\n")
        return "".join(out)

    def _format_structure(self, out: list[str]) -> None:
        w = out.append
        w(f"{_INDENT}(structure\n")
        for i, name in enumerate(self.copper):
            w(f"{_INDENT * 2}(layer {quote_id(self.layer_ids[i])}\n")
            w(f"{_INDENT * 3}(type {self._layer_type(name)})\n")
            w(f"{_INDENT * 3}(property\n{_INDENT * 4}(index {i})\n{_INDENT * 3})\n")
            direction = self.layer_directions.get(name)
            if direction:
                w(f"{_INDENT * 3}(direction {direction})\n")
            w(f"{_INDENT * 2})\n")
        # boundary
        w(f"{_INDENT * 2}(boundary\n")
        outline, holes = self._board_boundary()
        w(_path_text("path", "pcb", 0.0, outline, nest=3))
        w(f"{_INDENT * 2})\n")
        # planes
        for zone in self.board.zones:
            if zone.is_rule_area or not zone.outline:
                continue
            layers = self._zone_copper_layers(zone)
            if not layers:
                continue
            zone_net = dsn_string(zone.net.name) if zone.net is not None else ""
            for layer in layers:
                net_name = zone_net
                if net_name == "":
                    # KiCad numbers the bogus net per (zone, layer) pair
                    net_name = f"@:no_net_{self._netless_zones}"
                    self._netless_zones += 1
                    self._extra_nets.append(net_name)
                lid = self.layer_ids[self.index_of[layer]]
                w(f"{_INDENT * 2}{self._polygon_keepout('plane', net_name, lid, zone, _board_pt, nest=2)}\n")
        # keepouts: outline cutouts, then rule areas
        for hole in holes:
            w(_keepout_text("keepout", "signal", hole))
        for zone in self.board.zones:
            if not zone.is_rule_area or not zone.outline:
                continue
            kind = _keepout_kind(zone)
            for layer in self._zone_copper_layers(zone):
                lid = self.layer_ids[self.index_of[layer]]
                w(f"{_INDENT * 2}{self._polygon_keepout(kind, '', lid, zone, _board_pt, nest=2)}\n")
        # vias
        w(f"{_INDENT * 2}(via {join_ids(self._via_padstacks)})\n")
        # rules
        d = self.rules.default
        w(f"{_INDENT * 2}(rule\n")
        w(f"{_INDENT * 3}(width {g6(um(d.track_width))})\n")
        w(f"{_INDENT * 3}(clearance {g6(um(d.clearance))})\n")
        w(f"{_INDENT * 3}(clearance {g6(um(d.clearance) / 4)} (type smd_smd))\n")
        w(f"{_INDENT * 2})\n")
        w(f"{_INDENT})\n")

    def _edge_rings(self) -> list[Polygon]:
        """The Edge.Cuts rings re-chained with KiCad's arc polygonisation (`SHAPE_ARC::
        ConvertToPolyline`, vertices straddling the arc) instead of `BoardFile`'s on-arc vertices,
        so the boundary agrees with `GetBoardPolygonOutlines` to well under the 5 µm error."""
        edge = [g for g in self.board.graphics if g.layer == EDGE_CUTS]
        for fp in self.board.footprints:
            edge.extend(g for g in fp.graphics if g.layer == EDGE_CUTS)
        polylines: list[list[Vec2]] = []
        for g in edge:
            pts = list(g.points)
            if g.kind == "arc" and len(pts) >= 3:
                pts = kicad_arc_polyline(pts[0], pts[len(pts) // 2], pts[-1], self.max_error)
            if g.closed and pts:
                pts.append(pts[0])
            polylines.append(pts)
        closed, _open = chain_outline(polylines, OUTLINE_CHAINING_EPSILON_MM)
        return nest_rings(closed)

    def _board_boundary(self) -> tuple[list[Vec2], list[list[Vec2]]]:
        polys = self._edge_rings() if self.board.outline else []
        if not polys:
            self.warnings.append("board outline is missing or malformed: bounding box of the items used")
            box = self.board.outline_bbox()
            if box is None:
                box = _items_bbox(self.board)
            outline = [
                Vec2(box.min_x, box.min_y),
                Vec2(box.max_x, box.min_y),
                Vec2(box.max_x, box.max_y),
                Vec2(box.min_x, box.max_y),
            ]
            return outline + [outline[0]], []
        if len(polys) > 1:
            self.warnings.append(f"board has {len(polys)} outlines; only the largest is the boundary")
        main = max(polys, key=lambda p: abs(p.signed_area()))
        ring = _dedupe_closed(main.points)
        holes = [_dedupe_closed(h) for h in main.holes]
        return ring, holes

    def _format_placement(self, out: list[str]) -> None:
        w = out.append
        w(f"{_INDENT}(placement\n")
        for key in self._component_order:
            w(f"{_INDENT * 2}(component {quote_id(key)}\n")
            for place in self._components[key]:
                w(f"{_INDENT * 3}{place}\n")
            w(f"{_INDENT * 2})\n")
        w(f"{_INDENT})\n")

    def _format_library(self, out: list[str]) -> None:
        w = out.append
        w(f"{_INDENT}(library\n")
        for image in self._images:
            w(f"{_INDENT * 2}(image {quote_id(image.full_id)}\n")
            for line in image.outlines:
                w(f"{_INDENT * 3}{line}\n")
            for pin in image.pins:
                w(f"{_INDENT * 3}{pin.text()}\n")
            for line in image.keepouts:
                w(f"{_INDENT * 3}{line}\n")
            w(f"{_INDENT * 2})\n")
        for ps in list(self._padstacks.values()) + list(self._via_padstacks.values()):
            w(f"{_INDENT * 2}(padstack {quote_id(ps.id)}\n")
            w(ps.text())
            w(f"{_INDENT * 2})\n")
        w(f"{_INDENT})\n")

    def _format_network(self, out: list[str]) -> None:
        w = out.append
        w(f"{_INDENT}(network\n")
        for net in self._board_net_names():
            pins = self._net_pins.get(net)
            if not pins or not self._routable(net):
                continue
            w(f"{_INDENT * 2}(net {quote_id(net)}\n")
            w(_wrapped_list(f"{_INDENT * 3}(pins", pins, _INDENT * 4, margin=80))
            w(f"{_INDENT * 2})\n")
        for net in self._extra_nets:
            w(f"{_INDENT * 2}(net {quote_id(net)}\n{_INDENT * 2})\n")
        top, bot = 0, len(self.copper) - 1
        classes: list[tuple[str, NetclassRule, list[str]]] = []
        default_nets: list[str] = []
        by_class: dict[str, list[str]] = {}
        for net in sorted(self._board_net_names()):
            if not self._routable(net):
                continue  # not in the network at all (module docstring)
            cls = self._class_of(net)
            if cls.is_default:
                default_nets.append(net)
            else:
                by_class.setdefault(cls.name, []).append(net)
        classes.append((KICAD_DEFAULT_CLASS_ID, self.rules.default, default_nets))
        for cls in self._classes_in_use():
            classes.append((cls.name, cls, by_class.get(cls.name, [])))
        for class_id, cls, nets in classes:
            head = f"{_INDENT * 2}(class {quote_id(class_id)}"
            w(_wrapped_list(head, [quote_id(n) for n in nets], _INDENT * 3, margin=72, close=False))
            w(f"{_INDENT * 3}(circuit\n")
            w(
                f"{_INDENT * 4}(use_via {quote_id(via_padstack_id(cls.via_diameter, cls.via_drill, top, bot))})\n"
            )
            w(f"{_INDENT * 3})\n")
            w(f"{_INDENT * 3}(rule\n")
            w(f"{_INDENT * 4}(width {g6(um(cls.track_width))})\n")
            w(f"{_INDENT * 4}(clearance {g6(um(cls.clearance))})\n")
            w(f"{_INDENT * 3})\n")
            w(f"{_INDENT * 2})\n")
        w(f"{_INDENT})\n")

    def _format_wiring(self, out: list[str]) -> None:
        w = out.append
        w(f"{_INDENT}(wiring\n")
        for wire in self._wires:
            lid = self.layer_ids[self.index_of[wire.layer]]
            pts = "  ".join(f"{g6(x)} {g6(y)}" for x, y in wire.points)
            kind = "fix" if wire.fixed else "route"
            w(
                f"{_INDENT * 2}(wire (path {quote_id(lid)} {g6(wire.width)}  {pts})(net {quote_id(wire.net)})(type {kind}))\n"
            )
        for via in self._wire_vias:
            kind = "fix" if via.fixed else "route"
            w(
                f"{_INDENT * 2}(via {quote_id(via.padstack)}  {g6(via.x)} {g6(via.y)} "
                f"(net {quote_id(via.net)})(type {kind}))\n"
            )
        w(f"{_INDENT})\n")


# ----------------------------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------------------------


def write_dsn(board: BoardFile, **kwargs) -> str:
    """`DsnWriter(board, **kwargs).write()`."""
    return DsnWriter(board, **kwargs).write()


def _is_locked(flag: bool, node: Node | None) -> bool:
    """`BoardFile` reads `locked` / `(locked yes)`; KiCad's parser also takes a bare `(locked)`
    list (spikes/specctra-verify/test_locked.kicad_pcb exports it as `fix`), so honour that too."""
    if flag:
        return True
    if node is None:
        return False
    child = node.find("locked")
    return child is not None and not child.atoms


def _prim_width(prim: Node) -> float:
    stroke = prim.find("stroke")
    text = stroke.get("width") if stroke is not None else prim.get("width")
    try:
        return float(text) if text else 0.0
    except ValueError:
        return 0.0


def _xy_of(node: Node, head: str) -> Vec2 | None:
    child = node.find(head)
    if child is None or len(child.atoms) < 2:
        return None
    return Vec2(child.atoms[0].as_float(), child.atoms[1].as_float())


def _pad_circle(center: Vec2, radius: float, max_error: float) -> list[Vec2]:
    return pad_circle_polygon(center, radius, max_error)


def _board_pt(p: Vec2) -> str:
    return pt(p.x, p.y)


def _dedupe_closed(points: Sequence[Vec2]) -> list[Vec2]:
    """Ring without a repeated closing vertex, then closed again (KiCad appends the first point)."""
    pts = list(points)
    while len(pts) > 1 and pts[-1].close_to(pts[0], 1e-9):
        pts.pop()
    if not pts:
        return []
    return pts + [pts[0]]


def _path_text(kind: str, layer_id: str, width: float, points: Sequence[Vec2], nest: int) -> str:
    """`PATH::Format`: `(path L W  x y  x y …)` wrapped after column 70 at nest max(nest+1, 6)."""
    head = f"{_INDENT * nest}({kind} {quote_id(layer_id)} {g6(width)}"
    per_line = len(head)
    wrap = _INDENT * max(nest + 1, 6)
    out = [head]
    for p in points:
        token = pt(p.x, p.y)
        if per_line > _RIGHT_MARGIN:
            out.append("\n" + wrap)
            per_line = len(wrap)
        else:
            out.append("  ")
            per_line += 2
        out.append(token)
        per_line += len(token)
    out.append(")\n")
    return "".join(out)


def _wrapped_list(
    head: str, tokens: Sequence[str], wrap_indent: str, *, margin: int, close: bool = True
) -> str:
    """KiCad's `perLine > RIGHTMARGIN` wrapping of `(pins …)` / `(class …)` token lists."""
    out = [head]
    per_line = len(head)
    for token in tokens:
        if per_line > margin:
            out.append("\n" + wrap_indent)
            per_line = len(wrap_indent)
        else:
            out.append(" ")
            per_line += 1
        out.append(token)
        per_line += len(token)
    out.append(")\n" if close else "\n")
    return "".join(out)


def _keepout_text(kind: str, layer_id: str, ring: Sequence[Vec2]) -> str:
    text = "  ".join(pt(p.x, p.y) for p in ring)
    return f'{_INDENT * 2}({kind} "" (polygon {quote_id(layer_id)} 0  {text}))\n'


def _keepout_kind(zone: Zone) -> str:
    """`T_keepout` when tracks and vias are both forbidden (or neither), else the specific one."""
    node = zone.node
    no_tracks = no_vias = False
    if node is not None:
        ko = node.find("keepout")
        if ko is not None:
            no_tracks = (ko.get("tracks") or "") == "not_allowed"
            no_vias = (ko.get("vias") or "") == "not_allowed"
    if no_vias and no_tracks:
        return "keepout"
    if no_vias:
        return "via_keepout"
    if no_tracks:
        return "wire_keepout"
    return "keepout"


def _footprint_rule_areas(fp: Footprint) -> list[Zone]:
    """Rule areas stored inside a footprint node, with the outline as the file holds it: board
    coordinates before stamp 20260616, the library frame from then on (`STAMP_FP_AFFINE_TRANSFORM`;
    `_make_image` picks the mapper)."""
    node: Node | None = fp.node
    if node is None:
        return []
    out: list[Zone] = []
    for z in node.find_all("zone"):
        if z.find("keepout") is None:
            continue
        polys = []
        for poly in z.find_all("polygon"):
            pts_node = poly.find("pts")
            pts: list[Vec2] = []
            if pts_node is not None:
                for xy in pts_node.find_all("xy"):
                    a = xy.atoms
                    if len(a) >= 2:
                        pts.append(Vec2(a[0].as_float(), a[1].as_float()))
            if len(pts) >= 3:
                polys.append(Polygon.of(pts))
        layers_node = z.find("layers") or z.find("layer")
        layers = tuple(layers_node.values()) if layers_node is not None else ()
        out.append(Zone(None, layers, tuple(polys), {}, z.get("name") or "", "", 0, True, z))
    return out


ZONE_ARC_ACCURACY_MM = 0.005  # SHAPE_ARC::DefaultAccuracyForPCB (5000 nm): zone outline arcs at load


def _zone_main_ring(zone: Zone) -> list[Vec2]:
    """The zone's main outline as KiCad holds it: a `(pts …)` with `(arc …)` entries is
    polygonised at load by `SHAPE_LINE_CHAIN::Append( SHAPE_ARC )` with the PCB default accuracy
    (`SHAPE_ARC::ConvertToPolyline`, vertices straddling the arc), which the exporter then
    iterates; `BoardFile` puts its vertices ON the arc, up to ~5 um away (RoyalBlue54L demo
    planes)."""
    node = zone.node
    poly = node.find("polygon") if node is not None else None
    pts_node = poly.find("pts") if poly is not None else None
    if pts_node is None or not any(c.head == "arc" for c in pts_node.nodes):
        return list(zone.outline[0].points)
    out: list[Vec2] = []
    for child in pts_node.nodes:
        if child.head == "xy":
            p = _xy_of_atoms(child)
            if p is not None:
                out.append(p)
        elif child.head == "arc":
            s0, m0, e0 = _xy_of(child, "start"), _xy_of(child, "mid"), _xy_of(child, "end")
            if s0 is None or m0 is None or e0 is None:
                continue
            seg = kicad_arc_polyline(s0, m0, e0, ZONE_ARC_ACCURACY_MM)
            if out and out[-1].close_to(seg[0], 1e-9):
                seg = seg[1:]
            out.extend(seg)
    return out or list(zone.outline[0].points)


def _xy_of_atoms(node: Node) -> Vec2 | None:
    a = node.atoms
    if len(a) < 2:
        return None
    return Vec2(a[0].as_float(), a[1].as_float())


def _items_bbox(board: BoardFile) -> BBox:
    box: BBox | None = None
    for fp in board.footprints:
        b = fp.bbox()
        box = b if box is None else box.union(b)
    for t in board.tracks:
        for p in (t.start, t.end):
            b = BBox(p.x, p.y, p.x, p.y)
            box = b if box is None else box.union(b)
    if box is None:
        return BBox(0.0, 0.0, 10.0, 10.0)
    return box
