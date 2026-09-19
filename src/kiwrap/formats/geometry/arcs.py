"""Arcs, circles and Bézier curves as polylines, segmented the way KiCad does it.

Facts encoded here (KiCad source at tag 10.0, downloaded 2026-09-19 and compared with master):

* `GetArcToSegmentCount(radius, maxError, angle)` (libs/kimath/src/geometry/geometry_utils.cpp
  lines 42-64): `rel = maxError / radius`, `increment = 2·acos(1 - rel)` degrees clamped to at most
  `360 / MIN_SEGCOUNT_FOR_CIRCLE` (= 45° for 8 segments), `segCount = round(|angle| / increment)`,
  at least 2. Radius and error are clamped to ≥ 1 nm before dividing.
* Courtyard and board-outline polygonisation use `maxError = 0.005 mm` (`FOOTPRINT::
  BuildCourtyardCaches`, pcbnew/footprint.cpp line 3679) — `MAX_ERROR_MM` below.
* `TransformCircleToPolygon` (common/convert_basic_shapes_to_polygon.cpp lines 80-119, used for
  pad shapes) rounds the segment count up to a multiple of 8 and starts at `delta/2` so the
  polygon aligns at 45°; outline conversion (`ConvertOutlineToPolygon`) puts vertices *on* the
  circle at `k·360/n`. `circle_polygon()` implements the outline flavour; `pad_circle_polygon()`
  the pad flavour (with `ERROR_INSIDE`, i.e. no radius correction — the inside variant is what
  hit-testing needs).
* File arcs are `(start) (mid) (end)` three-point arcs since format 20210925 (`LEGACY_ARC_FORMATTING`
  in pcb_io_kicad_sexpr_parser.cpp); `arc_center()` follows `CalcArcCenter` (perpendicular
  bisector intersection). Direction is taken from the mid point, never assumed.
"""

from __future__ import annotations

import math

from kiwrap.formats.geometry.vec import Vec2

__all__ = [
    "MAX_ERROR_MM",
    "MIN_SEGCOUNT_FOR_CIRCLE",
    "arc_angles",
    "arc_center",
    "arc_points",
    "arc_to_segment_count",
    "bezier_points",
    "circle_polygon",
    "pad_circle_polygon",
]

#: KiCad's polygonisation error for courtyards / outlines (pcbnew/footprint.cpp: mmToIU(0.005)).
MAX_ERROR_MM = 0.005
#: `MIN_SEGCOUNT_FOR_CIRCLE` in include/geometry/geometry_utils.h.
MIN_SEGCOUNT_FOR_CIRCLE = 8
_NM = 1e-6  # KiCad clamps radius and error to >= 1 internal unit (1 nm)


def arc_to_segment_count(radius: float, max_error: float, arc_angle_deg: float) -> int:
    """`GetArcToSegmentCount` in millimetres (see module docstring)."""
    radius = max(_NM, radius)
    max_error = max(_NM, max_error)
    rel_error = max_error / radius
    if rel_error >= 2.0:
        increment = 360.0 / MIN_SEGCOUNT_FOR_CIRCLE
    else:
        increment = math.degrees(math.acos(1.0 - rel_error)) * 2
        increment = min(360.0 / MIN_SEGCOUNT_FOR_CIRCLE, increment)
    seg_count = int(round(abs(arc_angle_deg) / increment))  # KiROUND: round half away from zero
    return max(seg_count, 2)


def _on_circle(center: Vec2, radius: float, angle_deg: float) -> Vec2:
    a = math.radians(angle_deg)
    return Vec2(center.x + radius * math.cos(a), center.y + radius * math.sin(a))


def circle_polygon(center: Vec2, radius: float, max_error: float = MAX_ERROR_MM) -> list[Vec2]:
    """Vertices on the circle at `k·360/n` (outline flavour, `ConvertOutlineToPolygon`)."""
    n = arc_to_segment_count(radius, max_error, 360.0)
    return [_on_circle(center, radius, 360.0 * k / n) for k in range(n)]


def pad_circle_polygon(center: Vec2, radius: float, max_error: float = MAX_ERROR_MM) -> list[Vec2]:
    """`TransformCircleToPolygon` with ERROR_INSIDE: n rounded up to a multiple of 8, vertices at
    `delta/2 + k·delta` on the circle."""
    n = arc_to_segment_count(radius, max_error, 360.0)
    n = (n + 7) // 8 * 8
    delta = 360.0 / n
    return [_on_circle(center, radius, delta / 2 + k * delta) for k in range(n)]


def arc_center(start: Vec2, mid: Vec2, end: Vec2) -> Vec2 | None:
    """Centre of the circle through three points; None when they are collinear."""
    ax, ay = start.x, start.y
    bx, by = mid.x, mid.y
    cx, cy = end.x, end.y
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-12:
        return None
    a2, b2, c2 = ax * ax + ay * ay, bx * bx + by * by, cx * cx + cy * cy
    ux = (a2 * (by - cy) + b2 * (cy - ay) + c2 * (ay - by)) / d
    uy = (a2 * (cx - bx) + b2 * (ax - cx) + c2 * (bx - ax)) / d
    return Vec2(ux, uy)


def arc_angles(start: Vec2, mid: Vec2, end: Vec2) -> tuple[Vec2, float, float, float] | None:
    """`(center, radius, start_angle_deg, sweep_deg)` of a three-point arc, in the mathematical
    (atan2, Y-down) frame; the sweep is signed so that it passes through `mid`. None if collinear."""
    c = arc_center(start, mid, end)
    if c is None:
        return None
    r = c.distance(start)
    a0 = math.degrees(math.atan2(start.y - c.y, start.x - c.x))
    am = math.degrees(math.atan2(mid.y - c.y, mid.x - c.x))
    a1 = math.degrees(math.atan2(end.y - c.y, end.x - c.x))
    ccw_to_mid = (am - a0) % 360.0
    ccw_to_end = (a1 - a0) % 360.0
    if ccw_to_mid <= ccw_to_end:
        sweep = ccw_to_end  # increasing atan2 angle passes through mid
    else:
        sweep = ccw_to_end - 360.0
    if sweep == 0.0:
        sweep = 360.0
    return c, r, a0, sweep


def arc_points(start: Vec2, mid: Vec2, end: Vec2, max_error: float = MAX_ERROR_MM) -> list[Vec2]:
    """Polyline approximation of a three-point arc, start and end exact, interior points on the arc."""
    geo = arc_angles(start, mid, end)
    if geo is None:
        return [start, end]
    c, r, a0, sweep = geo
    n = arc_to_segment_count(r, max_error, sweep)
    pts = [start]
    for k in range(1, n):
        a = math.radians(a0 + sweep * k / n)
        pts.append(Vec2(c.x + r * math.cos(a), c.y + r * math.sin(a)))
    pts.append(end)
    return pts


def bezier_points(p0: Vec2, p1: Vec2, p2: Vec2, p3: Vec2, segments: int = 16) -> list[Vec2]:
    """Uniformly sampled cubic Bézier (KiCad's BEZIER_POLY uses an adaptive scheme; 16 uniform
    steps are within courtyard tolerances for the curve sizes seen on boards)."""
    pts: list[Vec2] = []
    for k in range(segments + 1):
        t = k / segments
        u = 1 - t
        pts.append(
            Vec2(
                u * u * u * p0.x + 3 * u * u * t * p1.x + 3 * u * t * t * p2.x + t * t * t * p3.x,
                u * u * u * p0.y + 3 * u * u * t * p1.y + 3 * u * t * t * p2.y + t * t * t * p3.y,
            )
        )
    return pts
