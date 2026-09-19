"""Polygons, segment tests and the overlap test used by the file-tier analyses. Pure Python.

Design notes:

* `Polygon` is a simple closed ring (`points`, implicitly closed) with optional `holes`; a
  courtyard or a board outline with several disjoint rings is a *list* of polygons.
* `point_in_polygon` is the even-odd ray cast with an explicit on-boundary answer, so touching
  never counts as inside unless the caller asks for it.
* `polygons_overlap` decides "do the interiors overlap by more than `tolerance`?" without a
  general clipper: the vertices of the intersection region are exactly the vertices of A inside
  B, the vertices of B inside A and the edge/edge crossing points; along every edge of A the
  sub-segments between consecutive crossings that lie inside B (and vice versa) bound the
  region. Two shapes overlap when the region holds a disc of radius `tolerance / 2`, probed
  from the mid point of each boundary piece pushed inward by that radius. KiCad deflates each courtyard by its 0.005 mm polygonisation error before
  `SHAPE_POLY_SET::Collide(..., clearance=0)` (pcbnew/footprint.cpp `BuildCourtyardCaches`,
  drc_test_provider_courtyard_clearance.cpp), i.e. it reports overlaps deeper than 0.01 mm and
  never mere touching; `COURTYARD_TOLERANCE_MM` reproduces that. Exact clipping is not needed for
  a yes/no plus a bounding box, and the bbox of the region is exact (`overlap_bbox`).
* `chain_outline` mirrors `ConvertOutlineToPolygon`'s chaining: polylines whose end points fall
  within `epsilon` of each other are joined into closed rings (chaining epsilon 0.02 mm for
  courtyards; Edge.Cuts uses the board's own tolerance, 0.01 mm here).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field

from kiwrap.formats.geometry.vec import BBox, Vec2

__all__ = [
    "COURTYARD_TOLERANCE_MM",
    "Polygon",
    "chain_outline",
    "nest_rings",
    "overlap_bbox",
    "point_in_polygon",
    "point_segment_distance",
    "polygons_overlap",
    "segment_intersection",
    "segments_intersect",
]

#: Overlap depth below which KiCad's courtyard DRC does not fire (2 x the 0.005 mm deflation).
COURTYARD_TOLERANCE_MM = 0.01
_EPS = 1e-9


@dataclass(frozen=True)
class Polygon:
    """A closed ring (first point is NOT repeated) with optional holes, all in millimetres."""

    points: tuple[Vec2, ...]
    holes: tuple[tuple[Vec2, ...], ...] = field(default_factory=tuple)

    @classmethod
    def of(cls, points: Iterable[Vec2], holes: Iterable[Iterable[Vec2]] = ()) -> Polygon:
        return cls(tuple(_dedupe(points)), tuple(tuple(_dedupe(h)) for h in holes))

    @classmethod
    def rect(cls, min_x: float, min_y: float, max_x: float, max_y: float) -> Polygon:
        return cls((Vec2(min_x, min_y), Vec2(max_x, min_y), Vec2(max_x, max_y), Vec2(min_x, max_y)))

    @classmethod
    def from_bbox(cls, box: BBox) -> Polygon:
        return cls.rect(box.min_x, box.min_y, box.max_x, box.max_y)

    def __len__(self) -> int:
        return len(self.points)

    @property
    def bbox(self) -> BBox:
        box = BBox.of(self.points)
        if box is None:
            raise ValueError("empty polygon has no bbox")
        return box

    def signed_area(self) -> float:
        """Shoelace area; negative for clockwise-on-screen rings in a Y-down frame."""
        pts = self.points
        n = len(pts)
        return sum(pts[i].x * pts[(i + 1) % n].y - pts[(i + 1) % n].x * pts[i].y for i in range(n)) / 2

    def area(self) -> float:
        return abs(self.signed_area()) - sum(abs(Polygon(h).signed_area()) for h in self.holes)

    def edges(self) -> list[tuple[Vec2, Vec2]]:
        pts = self.points
        n = len(pts)
        return [(pts[i], pts[(i + 1) % n]) for i in range(n)]

    def all_rings(self) -> list[tuple[Vec2, ...]]:
        return [self.points, *self.holes]

    def contains(self, p: Vec2, *, on_boundary: bool = False, eps: float = _EPS) -> bool:
        """Inside the outer ring and outside every hole. Boundary points count only if asked."""
        r = point_in_polygon(p, self.points, eps)
        if r == 0:
            return on_boundary
        if r < 0:
            return False
        for hole in self.holes:
            rh = point_in_polygon(p, hole, eps)
            if rh > 0:
                return False
            if rh == 0:
                return on_boundary
        return True

    def boundary_distance(self, p: Vec2) -> float:
        """Distance from `p` to the nearest edge of the outer ring or any hole."""
        best = math.inf
        for ring in self.all_rings():
            n = len(ring)
            for i in range(n):
                best = min(best, point_segment_distance(p, ring[i], ring[(i + 1) % n]))
        return best

    def translated(self, d: Vec2) -> Polygon:
        return self.transformed(lambda p: p + d)

    def transformed(self, fn: Callable[[Vec2], Vec2]) -> Polygon:
        return Polygon(tuple(fn(p) for p in self.points), tuple(tuple(fn(p) for p in h) for h in self.holes))


def _dedupe(points: Iterable[Vec2]) -> list[Vec2]:
    out = _dedupe_open(list(points))
    if len(out) > 1 and out[0].close_to(out[-1]):
        out.pop()
    return out


def _dedupe_open(points: Sequence[Vec2]) -> list[Vec2]:
    out: list[Vec2] = []
    for p in points:
        if out and out[-1].close_to(p):
            continue
        out.append(p)
    return out


# ----------------------------------------------------------------------------------------------
# segments
# ----------------------------------------------------------------------------------------------


def point_segment_distance(p: Vec2, a: Vec2, b: Vec2) -> float:
    ab = b - a
    ll = ab.dot(ab)
    if ll <= _EPS:
        return p.distance(a)
    t = max(0.0, min(1.0, (p - a).dot(ab) / ll))
    return p.distance(Vec2(a.x + ab.x * t, a.y + ab.y * t))


def _orient(a: Vec2, b: Vec2, c: Vec2) -> float:
    return (b - a).cross(c - a)


def segments_intersect(a: Vec2, b: Vec2, c: Vec2, d: Vec2, eps: float = _EPS) -> bool:
    """True when segments ab and cd share at least one point (touching included)."""
    o1, o2 = _orient(a, b, c), _orient(a, b, d)
    o3, o4 = _orient(c, d, a), _orient(c, d, b)
    if ((o1 > eps and o2 < -eps) or (o1 < -eps and o2 > eps)) and (
        (o3 > eps and o4 < -eps) or (o3 < -eps and o4 > eps)
    ):
        return True
    for p, q, r, o in ((a, b, c, o1), (a, b, d, o2), (c, d, a, o3), (c, d, b, o4)):
        if abs(o) <= eps and _on_segment_box(p, q, r, eps):
            return True
    return False


def _on_segment_box(p: Vec2, q: Vec2, r: Vec2, eps: float) -> bool:
    return (
        min(p.x, q.x) - eps <= r.x <= max(p.x, q.x) + eps
        and min(p.y, q.y) - eps <= r.y <= max(p.y, q.y) + eps
    )


def segment_intersection(a: Vec2, b: Vec2, c: Vec2, d: Vec2, eps: float = _EPS) -> Vec2 | None:
    """The single crossing point of two non-parallel segments, or None (parallel or disjoint)."""
    r, s = b - a, d - c
    denom = r.cross(s)
    if abs(denom) <= _EPS:
        return None
    qp = c - a
    t = qp.cross(s) / denom
    u = qp.cross(r) / denom
    if -eps <= t <= 1 + eps and -eps <= u <= 1 + eps:
        return Vec2(a.x + r.x * t, a.y + r.y * t)
    return None


# ----------------------------------------------------------------------------------------------
# point in polygon
# ----------------------------------------------------------------------------------------------


def point_in_polygon(p: Vec2, ring: Sequence[Vec2], eps: float = _EPS) -> int:
    """Even-odd test: 1 inside, 0 on the boundary (within `eps`), -1 outside."""
    n = len(ring)
    if n < 3:
        return -1
    inside = False
    for i in range(n):
        a, b = ring[i], ring[(i + 1) % n]
        if point_segment_distance(p, a, b) <= eps:
            return 0
        if (a.y > p.y) != (b.y > p.y):
            x = a.x + (p.y - a.y) * (b.x - a.x) / (b.y - a.y)
            if p.x < x:
                inside = not inside
    return 1 if inside else -1


# ----------------------------------------------------------------------------------------------
# polygon / polygon overlap
# ----------------------------------------------------------------------------------------------


def _region_samples(
    a: Polygon, b: Polygon, eps: float
) -> tuple[list[Vec2], list[tuple[Vec2, Polygon, Polygon, Vec2]]]:
    """Vertices of the intersection region plus, per polygon, the mid points of its edge pieces
    that lie inside the other polygon: `(region_vertices, [(mid, other, owner, inward_normal)])`."""
    vertices: list[Vec2] = []
    samples: list[tuple[Vec2, Polygon, Polygon, Vec2]] = []
    for poly, other in ((a, b), (b, a)):
        other_bbox = other.bbox
        for ring in poly.all_rings():
            n = len(ring)
            for i in range(n):
                p0, p1 = ring[i], ring[(i + 1) % n]
                if other.contains(p0, on_boundary=True, eps=eps):
                    vertices.append(p0)
                seg_box = BBox.of((p0, p1))
                if seg_box is None or not seg_box.intersects(other_bbox, eps):
                    continue
                cuts: list[float] = [0.0, 1.0]
                d = p1 - p0
                ll = d.dot(d)
                for ring_o in other.all_rings():
                    m = len(ring_o)
                    for j in range(m):
                        x = segment_intersection(p0, p1, ring_o[j], ring_o[(j + 1) % m], eps)
                        if x is not None:
                            vertices.append(x)
                            if ll > _EPS:
                                cuts.append(max(0.0, min(1.0, (x - p0).dot(d) / ll)))
                cuts.sort()
                normal: Vec2 | None = None
                for t0, t1 in zip(cuts, cuts[1:], strict=False):
                    if t1 - t0 <= 1e-9:
                        continue
                    tm = (t0 + t1) / 2
                    mid = Vec2(p0.x + d.x * tm, p0.y + d.y * tm)
                    if other.contains(mid, eps=eps):
                        if normal is None:
                            normal = _inward_normal(p0, p1, poly, eps)
                        samples.append((mid, other, poly, normal))
                        vertices.append(Vec2(p0.x + d.x * t0, p0.y + d.y * t0))
                        vertices.append(Vec2(p0.x + d.x * t1, p0.y + d.y * t1))
    return vertices, samples


def _inward_normal(p0: Vec2, p1: Vec2, poly: Polygon, eps: float) -> Vec2:
    """Unit normal of edge p0->p1 pointing into `poly` (probed, so ring orientation is irrelevant)."""
    d = p1 - p0
    ll = d.length()
    if ll <= _EPS:
        return Vec2(0.0, 0.0)
    n = Vec2(-d.y / ll, d.x / ll)
    mid = Vec2((p0.x + p1.x) / 2, (p0.y + p1.y) / 2)
    probe = max(eps * 10, ll * 1e-3)
    if poly.contains(mid + n * probe, eps=eps):
        return n
    return -n


def polygons_overlap(
    a: Polygon, b: Polygon, tolerance: float = COURTYARD_TOLERANCE_MM, eps: float = 1e-7
) -> bool:
    """True when the interiors of `a` and `b` overlap by more than `tolerance` (see module doc).

    The test asks whether the intersection region contains a disc of radius `tolerance / 2`
    (what remains after deflating both polygons by that amount, KiCad's `Inflate(-maxError)`):
    every piece of A's boundary inside B (and vice versa) is probed at its mid point, pushed
    inward by the radius. Containment counts (a courtyard fully inside another is an overlap);
    touching and slivers thinner than `tolerance` do not.
    """
    if not a.bbox.intersects(b.bbox, tolerance):
        return False
    d = tolerance / 2
    _, samples = _region_samples(a, b, eps)
    for p, other, mine, normal in samples:
        c = p + normal * d
        if not (mine.contains(c, eps=eps) and other.contains(c, eps=eps)):
            continue
        if mine.boundary_distance(c) >= d - eps and other.boundary_distance(c) >= d - eps:
            return True
    return False


def overlap_bbox(a: Polygon, b: Polygon, eps: float = 1e-7) -> BBox | None:
    """Exact bounding box of the intersection region of two polygons (None when disjoint)."""
    vertices, samples = _region_samples(a, b, eps)
    pts = [
        p
        for p in vertices
        if a.contains(p, on_boundary=True, eps=eps) and b.contains(p, on_boundary=True, eps=eps)
    ]
    pts += [p for p, _, _, _ in samples]
    return BBox.of(pts)


# ----------------------------------------------------------------------------------------------
# outline chaining
# ----------------------------------------------------------------------------------------------


def chain_outline(
    polylines: Iterable[Sequence[Vec2]], epsilon: float = 0.01
) -> tuple[list[Polygon], list[tuple[Vec2, ...]]]:
    """Join open polylines end-to-end (either direction) into closed rings.

    Returns `(closed, open)`: closed rings as polygons (holes not assigned, see `nest_rings`) and
    the chains that could not be closed (a malformed outline). A polyline whose own ends meet is
    already a ring.
    """
    pending: list[list[Vec2]] = [_dedupe_open(pl) for pl in polylines if len(pl) >= 2]
    closed: list[Polygon] = []
    open_chains: list[tuple[Vec2, ...]] = []
    while pending:
        chain = pending.pop(0)
        grew = True
        while grew and not _is_closed(chain, epsilon):
            grew = False
            for i, other in enumerate(pending):
                if chain[-1].distance(other[0]) <= epsilon:
                    chain = chain + other[1:]
                elif chain[-1].distance(other[-1]) <= epsilon:
                    chain = chain + other[-2::-1]
                elif chain[0].distance(other[-1]) <= epsilon:
                    chain = other[:-1] + chain
                elif chain[0].distance(other[0]) <= epsilon:
                    chain = other[:0:-1] + chain
                else:
                    continue
                pending.pop(i)
                grew = True
                break
        if _is_closed(chain, epsilon):
            ring = _dedupe(chain)
            if len(ring) >= 3:
                closed.append(Polygon(tuple(ring)))
        else:
            open_chains.append(tuple(chain))
    return closed, open_chains


def _is_closed(chain: Sequence[Vec2], epsilon: float) -> bool:
    return len(chain) >= 3 and chain[0].distance(chain[-1]) <= epsilon


def nest_rings(rings: Sequence[Polygon]) -> list[Polygon]:
    """Assign rings lying inside another ring as its holes (one level: KiCad board outlines are
    outer rings plus cutouts). Largest rings first; a ring is a hole of the first larger ring
    that contains its first vertex."""
    if len(rings) <= 1:
        return list(rings)
    order = sorted(range(len(rings)), key=lambda i: -abs(rings[i].signed_area()))
    assigned: dict[int, int] = {}
    for pos, i in enumerate(order):
        for j in order[:pos]:
            if j in assigned:
                continue
            if rings[j].bbox.contains_box(rings[i].bbox, 1e-6) and rings[j].contains(
                rings[i].points[0], on_boundary=True
            ):
                assigned[i] = j
                break
    out: list[Polygon] = []
    for i in order:
        if i in assigned:
            continue
        holes = tuple(rings[k].points for k, parent in assigned.items() if parent == i)
        out.append(Polygon(rings[i].points, holes))
    return out
