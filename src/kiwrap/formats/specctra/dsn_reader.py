"""Semantic model of a Specctra DSN (`parse_dsn`) and a normalised comparer (`compare_dsn`).

The reader is what T7 (`conformance/tests/t7_specctra_codec.py`) uses to check the codec's DSN
against KiCad's own `ExportSpecctraDSN` of the same board, so it models exactly the subset KiCad
writes (docs/VERIFICATION.md claim 7 fixtures, `spikes/specctra-verify`, `spikes/autorouting`):
layers, boundary, planes, keepouts, via list, rules, placements, images with pins/outlines/
keepouts, padstacks, nets with pins, classes and wiring. Everything is kept in DSN units
(micrometres, y up) as written.

`compare_dsn` decides semantic equality the way a router sees the design, not byte equality:

* layers: same ids in the same order with the same types;
* boundary, cutout keepouts, planes and rule-area keepouts: polygons equal within `tol_um`
  (two-sided vertex-to-boundary distance, so start vertex, direction and collinear extra
  vertices do not matter), matched by kind/layer/net;
* rules and classes: width, clearance, `use_via` and net membership (as sets — KiCad's class net
  order comes from an unordered map);
* placements: per reference — position within `tol_um`, side, rotation (mod 360), and the pad
  geometry of the placed image resolved per pin: padstack shapes per layer within `tol_um`, pin
  position and rotation. Image *ids* are not compared: KiCad numbers content-different images
  `::N` in collector order, which is not semantic, and folds identical content under one id;
* nets: identical name -> pin-reference sets; the `(via …)` list as a set;
* wiring: identical multiset of (layer, width, net, type) segments within `tol_um`;
* image outlines are cosmetic for Freerouting (not obstacles): only their count per image is
  compared, and a mismatch is a warning, not a difference.

Two dialect differences of KiCad's exporter are normalised so that the codec's KiCad-10 form
compares equal to the KiCad 11 form (`kicad-cli pcb export specctra` of the fork patch
`specctra-jobs`, verified 2026-09-20 on the bench build; upstream b76f3d52f5 / 31220febea):

* unlocked tracks and vias are `(type route)` on 10 and `(type protect)` on 11 (Freerouting
  keeps protect wires and returns them in the session instead of ripping them up); both mean
  "unlocked" to KiCad's importer, so `compare_dsn` maps them onto one type (`unlocked_types`);
* copper zones are `(plane NET (polygon …))` in `structure` on 10 and `(wire (polygon …) (type
  protect) (window …))` in `wiring` on 11, the windows being the outline minus the CURRENT fill;
  `parse_dsn` files the 11 form as a plane with `windows_from_fill` set, and the window
  comparison is skipped for such a plane (fill-derived windows have no counterpart in the
  10 form, which writes the zone's own cutouts).
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from kiwrap.formats.sexpr import Atom, Node, loads

__all__ = [
    "DsnClass",
    "DsnDiff",
    "DsnDocument",
    "DsnImage",
    "DsnKeepout",
    "DsnLayer",
    "DsnNet",
    "DsnPadstack",
    "DsnPin",
    "DsnPlace",
    "DsnShape",
    "DsnVia",
    "DsnWire",
    "UNLOCKED_WIRE_TYPES",
    "compare_dsn",
    "load_specctra",
    "parse_dsn",
    "polygon_distance",
]

Point = tuple[float, float]
_STRING_QUOTE = re.compile(r'\(string_quote\s+"\)')


def load_specctra(text: str) -> Node:
    """Parse DSN/SES text with the lossless CST. Specctra strings carry no escapes, and the
    `(string_quote ")` token KiCad writes would open an unterminated string, so it is folded first."""
    return loads(_STRING_QUOTE.sub("(string_quote)", text))


# ----------------------------------------------------------------------------------------------
# model
# ----------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class DsnLayer:
    id: str
    type: str
    index: int | None = None


@dataclass(frozen=True)
class DsnShape:
    """`(circle L dia [x y])`, `(rect L x0 y0 x1 y1)`, `(path L width x y …)`, `(polygon L width x y …)`."""

    kind: str
    layer: str
    values: tuple[float, ...]  # circle: (dia, cx, cy); rect: (x0, y0, x1, y1); path/polygon: (width, x, y, …)

    @property
    def points(self) -> list[Point]:
        vals = self.values[1:] if self.kind in ("path", "polygon") else ()
        return [(vals[i], vals[i + 1]) for i in range(0, len(vals) - 1, 2)]

    @property
    def width(self) -> float:
        return self.values[0] if self.kind in ("path", "polygon") and self.values else 0.0


@dataclass(frozen=True)
class DsnKeepout:
    kind: str  # keepout | via_keepout | wire_keepout | plane
    name: str
    shape: DsnShape | None
    windows: tuple[DsnShape, ...] = ()
    #: the plane came from KiCad 11's `(wire (polygon …) (window …))` form, whose windows are
    #: the outline minus the current fill (not the zone's cutouts): they are not compared
    windows_from_fill: bool = False


@dataclass(frozen=True)
class DsnPadstack:
    id: str
    shapes: tuple[DsnShape, ...]
    attach: str = "off"


@dataclass(frozen=True)
class DsnPin:
    padstack: str
    pin_id: str
    x: float
    y: float
    rotation: float = 0.0


@dataclass(frozen=True)
class DsnImage:
    id: str
    pins: tuple[DsnPin, ...]
    outlines: tuple[DsnShape, ...] = ()
    keepouts: tuple[DsnKeepout, ...] = ()


@dataclass(frozen=True)
class DsnPlace:
    ref: str
    image: str
    x: float
    y: float
    side: str
    rotation: float
    part_number: str = ""


@dataclass(frozen=True)
class DsnNet:
    name: str
    pins: tuple[str, ...]


@dataclass(frozen=True)
class DsnClass:
    name: str
    nets: tuple[str, ...]
    use_via: str | None
    width: float | None
    clearance: float | None


@dataclass(frozen=True)
class DsnWire:
    layer: str
    width: float
    points: tuple[Point, ...]
    net: str
    type: str


@dataclass(frozen=True)
class DsnVia:
    padstack: str
    x: float
    y: float
    net: str
    type: str


@dataclass
class DsnDocument:
    name: str = ""
    unit: str = "um"
    resolution: tuple[str, int] = ("um", 10)
    layers: list[DsnLayer] = field(default_factory=list)
    boundary: list[DsnShape] = field(default_factory=list)
    planes: list[DsnKeepout] = field(default_factory=list)
    keepouts: list[DsnKeepout] = field(default_factory=list)
    via_ids: list[str] = field(default_factory=list)
    rules: dict[str, float] = field(default_factory=dict)  # width, clearance, clearance:smd_smd
    placements: list[DsnPlace] = field(default_factory=list)
    images: dict[str, DsnImage] = field(default_factory=dict)
    padstacks: dict[str, DsnPadstack] = field(default_factory=dict)
    nets: list[DsnNet] = field(default_factory=list)
    classes: list[DsnClass] = field(default_factory=list)
    wires: list[DsnWire] = field(default_factory=list)
    vias: list[DsnVia] = field(default_factory=list)

    def image_of(self, ref: str) -> DsnImage | None:
        for p in self.placements:
            if p.ref == ref:
                return self.images.get(p.image)
        return None


# ----------------------------------------------------------------------------------------------
# parsing
# ----------------------------------------------------------------------------------------------


def _floats(atoms: Sequence[Atom]) -> tuple[float, ...]:
    out: list[float] = []
    for a in atoms:
        try:
            out.append(float(a.value))
        except ValueError:
            break
    return tuple(out)


def _shape(node: Node) -> DsnShape | None:
    """A `(circle …)`, `(rect …)`, `(path …)` or `(polygon …)` list; `(qarc …)` is not modelled."""
    if node.head not in ("circle", "rect", "path", "polygon"):
        return None
    atoms = node.atoms
    if not atoms:
        return None
    layer = atoms[0].value
    vals = _floats(atoms[1:])
    if node.head == "circle":
        vals = (vals + (0.0, 0.0))[:3] if len(vals) < 3 else vals[:3]
    return DsnShape(node.head, layer, vals)


def _first_shape(node: Node) -> DsnShape | None:
    for child in node.nodes:
        s = _shape(child)
        if s is not None:
            return s
    return None


def _keepout(node: Node) -> DsnKeepout:
    atoms = node.atoms
    name = atoms[0].value if atoms else ""
    windows = tuple(s for w in node.find_all("window") for s in [_first_shape(w)] if s is not None)
    return DsnKeepout(node.head, name, _first_shape(node), windows)


def _padstack(node: Node) -> DsnPadstack:
    atoms = node.atoms
    pid = atoms[0].value if atoms else ""
    shapes = tuple(s for sh in node.find_all("shape") for s in [_first_shape(sh)] if s is not None)
    return DsnPadstack(pid, shapes, node.get("attach") or "off")


def _image(node: Node) -> DsnImage:
    atoms = node.atoms
    iid = atoms[0].value if atoms else ""
    pins: list[DsnPin] = []
    for p in node.find_all("pin"):
        pa = p.atoms
        if len(pa) < 4:
            continue
        rot = p.find("rotate")
        rotation = float(rot.atoms[0].value) if rot is not None and rot.atoms else 0.0
        pins.append(DsnPin(pa[0].value, pa[1].value, float(pa[2].value), float(pa[3].value), rotation))
    outlines = tuple(s for o in node.find_all("outline") for s in [_first_shape(o)] if s is not None)
    keepouts = tuple(_keepout(k) for k in node.nodes if k.head in ("keepout", "via_keepout", "wire_keepout"))
    return DsnImage(iid, tuple(pins), outlines, keepouts)


def _rules(node: Node | None) -> dict[str, float]:
    out: dict[str, float] = {}
    if node is None:
        return out
    for r in node.nodes:
        if not r.atoms:
            continue
        try:
            value = float(r.atoms[0].value)
        except ValueError:
            continue
        key = r.head
        typ = r.find("type")
        if typ is not None and typ.atoms:
            key = f"{key}:{typ.atoms[0].value}"
        out[key] = value
    return out


def parse_dsn(text: str | Node) -> DsnDocument:
    root = load_specctra(text) if isinstance(text, str) else text
    if root.head != "pcb":
        raise ValueError(f"not a Specctra DSN: top-level list is ({root.head or '?'} …)")
    doc = DsnDocument(name=root.atoms[0].value if root.atoms else "")
    unit = root.find("unit")
    if unit is not None and unit.atoms:
        doc.unit = unit.atoms[0].value
    res = root.find("resolution")
    if res is not None and len(res.atoms) >= 2:
        doc.resolution = (res.atoms[0].value, int(float(res.atoms[1].value)))
    structure = root.find("structure")
    if structure is not None:
        for layer in structure.find_all("layer"):
            la = layer.atoms
            prop = layer.find("property")
            index = None
            if prop is not None:
                idx = prop.find("index")
                if idx is not None and idx.atoms:
                    index = int(float(idx.atoms[0].value))
            doc.layers.append(DsnLayer(la[0].value if la else "", layer.get("type") or "signal", index))
        boundary = structure.find("boundary")
        if boundary is not None:
            doc.boundary = [s for c in boundary.nodes for s in [_shape(c)] if s is not None]
        for child in structure.nodes:
            if child.head == "plane":
                doc.planes.append(_keepout(child))
            elif child.head in ("keepout", "via_keepout", "wire_keepout"):
                doc.keepouts.append(_keepout(child))
        via = structure.find("via")
        if via is not None:
            doc.via_ids = [a.value for a in via.atoms]
        doc.rules = _rules(structure.find("rule"))
    placement = root.find("placement")
    if placement is not None:
        for comp in placement.find_all("component"):
            image_id = comp.atoms[0].value if comp.atoms else ""
            for place in comp.find_all("place"):
                pa = place.atoms
                if len(pa) < 5:
                    continue
                pn = place.find("PN")
                doc.placements.append(
                    DsnPlace(
                        pa[0].value,
                        image_id,
                        float(pa[1].value),
                        float(pa[2].value),
                        pa[3].value,
                        float(pa[4].value),
                        pn.atoms[0].value if pn is not None and pn.atoms else "",
                    )
                )
    library = root.find("library")
    if library is not None:
        for im in library.find_all("image"):
            image = _image(im)
            doc.images[image.id] = image
        for ps in library.find_all("padstack"):
            padstack = _padstack(ps)
            doc.padstacks[padstack.id] = padstack
    network = root.find("network")
    if network is not None:
        for net in network.find_all("net"):
            pins = net.find("pins")
            doc.nets.append(
                DsnNet(
                    net.atoms[0].value if net.atoms else "",
                    tuple(a.value for a in pins.atoms) if pins else (),
                )
            )
        for cls in network.find_all("class"):
            ca = cls.atoms
            circuit = cls.find("circuit")
            use_via = circuit.get("use_via") if circuit is not None else None
            rules = _rules(cls.find("rule"))
            doc.classes.append(
                DsnClass(
                    ca[0].value if ca else "",
                    tuple(a.value for a in ca[1:]),
                    use_via,
                    rules.get("width"),
                    rules.get("clearance"),
                )
            )
    wiring = root.find("wiring")
    if wiring is not None:
        for w in wiring.find_all("wire"):
            path = w.find("path")
            if path is None:
                polygon = w.find("polygon")
                if polygon is not None:
                    # KiCad 11's zone form (module docstring): a plane keyed by its net
                    windows = tuple(
                        s for win in w.find_all("window") for s in [_first_shape(win)] if s is not None
                    )
                    doc.planes.append(
                        DsnKeepout(
                            "plane", w.get("net") or "", _shape(polygon), windows, windows_from_fill=True
                        )
                    )
                continue
            shape = _shape(path)
            if shape is None:
                continue
            doc.wires.append(
                DsnWire(
                    shape.layer,
                    shape.width,
                    tuple(shape.points),
                    w.get("net") or "",
                    w.get("type") or "route",
                )
            )
        for v in wiring.find_all("via"):
            va = v.atoms
            if len(va) < 3:
                continue
            doc.vias.append(
                DsnVia(
                    va[0].value,
                    float(va[1].value),
                    float(va[2].value),
                    v.get("net") or "",
                    v.get("type") or "route",
                )
            )
    return doc


# ----------------------------------------------------------------------------------------------
# comparison
# ----------------------------------------------------------------------------------------------


def _dist(a: Point, b: Point) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _seg_distance(p: Point, a: Point, b: Point) -> float:
    ax, ay = a
    bx, by = b
    px, py = p
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _ring(points: Sequence[Point]) -> list[Point]:
    pts = list(points)
    while len(pts) > 1 and _close(pts[0], pts[-1], 1e-9):
        pts.pop()
    return pts


def _close(a: Point, b: Point, tol: float) -> bool:
    return abs(a[0] - b[0]) <= tol and abs(a[1] - b[1]) <= tol


def _boundary_distance(points: Sequence[Point], ring: Sequence[Point]) -> float:
    if not ring:
        return math.inf
    worst = 0.0
    n = len(ring)
    for p in points:
        best = math.inf
        for i in range(n):
            d = _seg_distance(p, ring[i], ring[(i + 1) % n])
            if d < best:
                best = d
                if best == 0.0:
                    break
        worst = max(worst, best)
    return worst


def polygon_distance(a: Sequence[Point], b: Sequence[Point]) -> float:
    """Two-sided vertex-to-boundary distance between closed rings (0 when they trace the same
    outline whatever the start vertex, direction or collinear vertices)."""
    ra, rb = _ring(a), _ring(b)
    if not ra or not rb:
        return 0.0 if not ra and not rb else math.inf
    return max(_boundary_distance(ra, rb), _boundary_distance(rb, ra))


def _shape_distance(a: DsnShape, b: DsnShape) -> float:
    """Geometric distance between two shapes of the same kind on the same layer (inf otherwise)."""
    if a.layer != b.layer:
        return math.inf
    if a.kind == "circle" and b.kind == "circle":
        return max(
            abs(a.values[0] - b.values[0]), math.hypot(a.values[1] - b.values[1], a.values[2] - b.values[2])
        )
    if a.kind == "rect" and b.kind == "rect":
        ax0, ay0, ax1, ay1 = a.values[:4]
        bx0, by0, bx1, by1 = b.values[:4]
        return max(
            abs(min(ax0, ax1) - min(bx0, bx1)),
            abs(min(ay0, ay1) - min(by0, by1)),
            abs(max(ax0, ax1) - max(bx0, bx1)),
            abs(max(ay0, ay1) - max(by0, by1)),
        )
    if a.kind == "path" and b.kind == "path":
        if abs(a.width - b.width) > 1e-6:
            return math.inf
        pa, pb = a.points, b.points
        if len(pa) == 2 and len(pb) == 2:
            fwd = max(_dist(pa[0], pb[0]), _dist(pa[1], pb[1]))
            rev = max(_dist(pa[0], pb[1]), _dist(pa[1], pb[0]))
            return min(fwd, rev)
        return polygon_distance(pa, pb)
    if a.kind == "polygon" and b.kind == "polygon":
        return polygon_distance(a.points, b.points)
    return math.inf


@dataclass
class DsnDiff:
    differences: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    checked: Counter = field(default_factory=Counter)

    @property
    def ok(self) -> bool:
        return not self.differences

    def summary(self) -> str:
        parts = [f"{k}={v}" for k, v in sorted(self.checked.items())]
        return f"{len(self.differences)} differences, {len(self.warnings)} warnings; checked " + ", ".join(
            parts
        )


def _match_keepouts(
    kind: str, ours: Iterable[DsnKeepout], theirs: Iterable[DsnKeepout], tol: float, diff: DsnDiff
) -> None:
    pool = list(theirs)
    for k in ours:
        diff.checked[kind] += 1
        layer = k.shape.layer if k.shape else ""
        best_i, best_d = -1, math.inf
        for i, cand in enumerate(pool):
            if cand.kind != k.kind or cand.name != k.name:
                continue
            if (cand.shape is None) != (k.shape is None):
                continue
            d = 0.0 if k.shape is None or cand.shape is None else _shape_distance(k.shape, cand.shape)
            if d < best_d:
                best_i, best_d = i, d
        if best_i < 0 or best_d > tol:
            diff.differences.append(
                f"{kind} {k.kind} {k.name!r} on {layer}: no match (best distance {best_d:.3f} um)"
            )
            continue
        cand = pool.pop(best_i)
        if cand.windows_from_fill or k.windows_from_fill:
            continue  # KiCad 11's fill-derived windows (module docstring)
        if len(cand.windows) != len(k.windows):
            diff.differences.append(
                f"{kind} {k.kind} {k.name!r} on {layer}: {len(k.windows)} vs {len(cand.windows)} windows"
            )
            continue
        wpool = list(cand.windows)
        for w in k.windows:
            j = min(range(len(wpool)), key=lambda j: _shape_distance(w, wpool[j]), default=-1)
            if j < 0 or _shape_distance(w, wpool[j]) > tol:
                diff.differences.append(f"{kind} {k.kind} {k.name!r} on {layer}: window without match")
            else:
                wpool.pop(j)
    for cand in pool:
        layer = cand.shape.layer if cand.shape else ""
        diff.differences.append(f"{kind} {cand.kind} {cand.name!r} on {layer}: only in the reference")


def _pin_geometry(doc: DsnDocument, pin: DsnPin) -> dict[str, DsnShape]:
    ps = doc.padstacks.get(pin.padstack)
    if ps is None:
        return {}
    return {s.layer: s for s in ps.shapes}


def _compare_images(
    ours: DsnDocument, theirs: DsnDocument, a: DsnImage, b: DsnImage, ref: str, tol: float, diff: DsnDiff
) -> None:
    pins_a = {p.pin_id: p for p in a.pins}
    pins_b = {p.pin_id: p for p in b.pins}
    if set(pins_a) != set(pins_b):
        diff.differences.append(f"{ref}: pins {sorted(set(pins_a) ^ set(pins_b))} differ")
        return
    for pid, pa in pins_a.items():
        pb = pins_b[pid]
        diff.checked["pins"] += 1
        if math.hypot(pa.x - pb.x, pa.y - pb.y) > tol:
            diff.differences.append(f"{ref}-{pid}: pin position ({pa.x}, {pa.y}) vs ({pb.x}, {pb.y})")
        if (
            abs((pa.rotation - pb.rotation) % 360.0) > 1e-3
            and abs((pa.rotation - pb.rotation) % 360.0 - 360.0) > 1e-3
        ):
            diff.differences.append(f"{ref}-{pid}: pin rotation {pa.rotation} vs {pb.rotation}")
        ga, gb = _pin_geometry(ours, pa), _pin_geometry(theirs, pb)
        if set(ga) != set(gb):
            diff.differences.append(f"{ref}-{pid}: pad layers {sorted(ga)} vs {sorted(gb)}")
            continue
        for layer, sa in ga.items():
            d = _shape_distance(sa, gb[layer])
            if d > tol:
                diff.differences.append(
                    f"{ref}-{pid}: pad shape on {layer} differs by {d:.3f} um ({sa.kind} {pa.padstack} vs {gb[layer].kind} {pb.padstack})"
                )
    if len(a.outlines) != len(b.outlines):
        diff.warnings.append(f"{ref}: {len(a.outlines)} outlines vs {len(b.outlines)} (cosmetic)")
    _match_keepouts(f"{ref} image keepout", a.keepouts, b.keepouts, tol, diff)


#: wire / via types KiCad writes for unlocked copper: `route` (<= 10) and `protect` (11)
UNLOCKED_WIRE_TYPES: tuple[str, ...] = ("route", "protect")


def compare_dsn(
    ours: DsnDocument,
    theirs: DsnDocument,
    *,
    tol_um: float = 5.0,
    unlocked_types: Sequence[str] = UNLOCKED_WIRE_TYPES,
) -> DsnDiff:
    """Semantic difference of `ours` against the reference `theirs` (module docstring).
    `unlocked_types` are the wire / via types treated as one (KiCad 10's `route`, 11's
    `protect`); pass `()` to compare the types literally."""
    diff = DsnDiff()
    d = diff.differences
    if unlocked_types:
        ours = _normalise_types(ours, unlocked_types)
        theirs = _normalise_types(theirs, unlocked_types)
    if ours.unit != theirs.unit or ours.resolution != theirs.resolution:
        d.append(f"units {ours.unit}/{ours.resolution} vs {theirs.unit}/{theirs.resolution}")
    if [(la.id, la.type) for la in ours.layers] != [(la.id, la.type) for la in theirs.layers]:
        d.append(
            f"layers {[(la.id, la.type) for la in ours.layers]} vs {[(la.id, la.type) for la in theirs.layers]}"
        )
    diff.checked["layers"] = len(theirs.layers)
    # boundary
    if len(ours.boundary) != len(theirs.boundary):
        d.append(f"boundary: {len(ours.boundary)} paths vs {len(theirs.boundary)}")
    else:
        for sa, sb in zip(ours.boundary, theirs.boundary, strict=True):
            dist = polygon_distance(sa.points, sb.points)
            diff.checked["boundary"] += 1
            if dist > tol_um:
                d.append(f"boundary differs by {dist:.3f} um")
    _match_keepouts("plane", ours.planes, theirs.planes, tol_um, diff)
    _match_keepouts("keepout", ours.keepouts, theirs.keepouts, tol_um, diff)
    if set(ours.via_ids) != set(theirs.via_ids):
        d.append(f"via list {sorted(ours.via_ids)} vs {sorted(theirs.via_ids)}")
    for key in set(ours.rules) | set(theirs.rules):
        diff.checked["rules"] += 1
        va, vb = ours.rules.get(key), theirs.rules.get(key)
        if va is None or vb is None or abs(va - vb) > 1e-3:
            d.append(f"rule {key}: {va} vs {vb}")
    # placements + images
    pa = {p.ref: p for p in ours.placements}
    pb = {p.ref: p for p in theirs.placements}
    if set(pa) != set(pb):
        d.append(f"components differ: {sorted(set(pa) ^ set(pb))}")
    for ref in sorted(set(pa) & set(pb)):
        a, b = pa[ref], pb[ref]
        diff.checked["placements"] += 1
        if math.hypot(a.x - b.x, a.y - b.y) > tol_um:
            d.append(f"{ref}: position ({a.x}, {a.y}) vs ({b.x}, {b.y})")
        if a.side != b.side:
            d.append(f"{ref}: side {a.side} vs {b.side}")
        if (
            abs((a.rotation - b.rotation) % 360.0) > 1e-3
            and abs((a.rotation - b.rotation) % 360.0 - 360.0) > 1e-3
        ):
            d.append(f"{ref}: rotation {a.rotation} vs {b.rotation}")
        if a.part_number != b.part_number:
            diff.warnings.append(f"{ref}: part number {a.part_number!r} vs {b.part_number!r}")
        ia, ib = ours.images.get(a.image), theirs.images.get(b.image)
        if ia is None or ib is None:
            d.append(f"{ref}: image {a.image!r} / {b.image!r} missing from the library")
            continue
        _compare_images(ours, theirs, ia, ib, ref, tol_um, diff)
    # nets
    na = {n.name: set(n.pins) for n in ours.nets}
    nb = {n.name: set(n.pins) for n in theirs.nets}
    if set(na) != set(nb):
        d.append(f"nets differ: {sorted(set(na) ^ set(nb))}")
    for name in sorted(set(na) & set(nb)):
        diff.checked["nets"] += 1
        if na[name] != nb[name]:
            d.append(f"net {name!r}: pins {sorted(na[name] ^ nb[name])} differ")
    ca = {c.name: c for c in ours.classes}
    cb = {c.name: c for c in theirs.classes}
    if set(ca) != set(cb):
        d.append(f"classes differ: {sorted(set(ca) ^ set(cb))}")
    for name in sorted(set(ca) & set(cb)):
        diff.checked["classes"] += 1
        a_cls, b_cls = ca[name], cb[name]
        if set(a_cls.nets) != set(b_cls.nets):
            d.append(f"class {name!r}: nets {sorted(set(a_cls.nets) ^ set(b_cls.nets))} differ")
        if a_cls.use_via != b_cls.use_via:
            d.append(f"class {name!r}: use_via {a_cls.use_via} vs {b_cls.use_via}")
        for label, va, vb in (
            ("width", a_cls.width, b_cls.width),
            ("clearance", a_cls.clearance, b_cls.clearance),
        ):
            if (va is None) != (vb is None) or (va is not None and vb is not None and abs(va - vb) > 1e-3):
                d.append(f"class {name!r}: {label} {va} vs {vb}")
    # wiring: multiset of segments
    seg_a = _segments(ours.wires, tol_um)
    seg_b = _segments(theirs.wires, tol_um)
    diff.checked["wire_segments"] = len(seg_b)
    if seg_a != seg_b:
        missing = list((seg_b - seg_a).elements())[:5]
        extra = list((seg_a - seg_b).elements())[:5]
        d.append(
            f"wiring segments differ: {len(seg_b - seg_a)} only in reference (e.g. {missing}), "
            f"{len(seg_a - seg_b)} only in ours (e.g. {extra})"
        )
    va_set = Counter((v.padstack, round(v.x / tol_um), round(v.y / tol_um), v.net, v.type) for v in ours.vias)
    vb_set = Counter(
        (v.padstack, round(v.x / tol_um), round(v.y / tol_um), v.net, v.type) for v in theirs.vias
    )
    diff.checked["wire_vias"] = len(theirs.vias)
    if va_set != vb_set:
        d.append(
            f"wiring vias differ: {list((vb_set - va_set).elements())[:5]} vs {list((va_set - vb_set).elements())[:5]}"
        )
    return diff


def _normalise_types(doc: DsnDocument, unlocked: Sequence[str]) -> DsnDocument:
    """A shallow copy of `doc` whose unlocked wire / via types all read as `unlocked[0]`."""
    canon = unlocked[0]
    kinds = set(unlocked)
    if not any(w.type in kinds and w.type != canon for w in doc.wires) and not any(
        v.type in kinds and v.type != canon for v in doc.vias
    ):
        return doc
    out = DsnDocument(**{k: v for k, v in vars(doc).items()})
    out.wires = [
        DsnWire(w.layer, w.width, w.points, w.net, canon if w.type in kinds else w.type) for w in doc.wires
    ]
    out.vias = [DsnVia(v.padstack, v.x, v.y, v.net, canon if v.type in kinds else v.type) for v in doc.vias]
    return out


def _segments(wires: Iterable[DsnWire], tol: float) -> Counter:
    out: Counter = Counter()
    for w in wires:
        pts = w.points
        for i in range(len(pts) - 1):
            a = (round(pts[i][0] / tol), round(pts[i][1] / tol))
            b = (round(pts[i + 1][0] / tol), round(pts[i + 1][1] / tol))
            key = (w.layer, round(w.width, 3), w.net, w.type, min(a, b), max(a, b))
            out[key] += 1
    return out
