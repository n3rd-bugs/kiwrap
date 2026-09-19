"""Unit tests for kiwrap.formats.geometry (pure Python, no KiCad needed)."""

from __future__ import annotations

import math

import pytest

from kiwrap.formats.geometry import (
    COURTYARD_TOLERANCE_MM,
    BBox,
    Polygon,
    Vec2,
    arc_angles,
    arc_center,
    arc_points,
    arc_to_segment_count,
    bezier_points,
    chain_outline,
    circle_polygon,
    nest_rings,
    overlap_bbox,
    pad_circle_polygon,
    point_in_polygon,
    point_segment_distance,
    polygons_overlap,
    rotate,
    segment_intersection,
    segments_intersect,
)

# ---- vectors and rotation ----------------------------------------------------------------------


def test_rotate_follows_kicad_rotatepoint_convention():
    # trigo.cpp: 90 deg -> (y, -x); 180 -> (-x, -y); 270 -> (-y, x)
    assert rotate(Vec2(5, 0), 90) == Vec2(0, -5)
    assert rotate(Vec2(5, 0), 180) == Vec2(-5, 0)
    assert rotate(Vec2(5, 0), 270) == Vec2(0, 5)
    assert rotate(Vec2(5, 0), 0) == Vec2(5, 0)
    assert rotate(Vec2(5, 0), 450) == Vec2(0, -5)
    r = rotate(Vec2(1, 0), 45)
    assert r.x == pytest.approx(math.sqrt(0.5)) and r.y == pytest.approx(-math.sqrt(0.5))


def test_vec_ops_and_bbox():
    a, b = Vec2(1, 2), Vec2(3, 5)
    assert a + b == Vec2(4, 7) and b - a == Vec2(2, 3) and a * 2 == Vec2(2, 4) and -a == Vec2(-1, -2)
    assert a.dot(b) == 13 and a.cross(b) == -1 and a.distance(b) == pytest.approx(math.sqrt(13))
    box = BBox.of([a, b, Vec2(-1, 9)])
    assert box == BBox(-1, 2, 3, 9) and box.width == 4 and box.height == 7 and box.center == Vec2(1, 5.5)
    assert box.intersects(BBox(3, 9, 4, 10)) and not box.intersects(BBox(3.1, 9, 4, 10))
    assert (
        box.intersection(BBox(2, 2, 10, 3)) == BBox(2, 2, 3, 3) and box.intersection(BBox(5, 5, 6, 6)) is None
    )
    assert box.contains(Vec2(0, 5)) and box.contains_box(BBox(0, 3, 1, 4)) and box.expanded(1).min_x == -2
    assert BBox.of([]) is None


# ---- arcs --------------------------------------------------------------------------------------


def test_arc_to_segment_count_matches_kicad_formula():
    # GetArcToSegmentCount(1 mm, 0.005 mm, 360): increment = 2*acos(1-0.005) deg = 11.46 -> 31
    assert arc_to_segment_count(1.0, 0.005, 360) == 31
    # tiny radius: increment clamps to 45 deg -> 8 segments for a circle, min 2 for arcs
    assert arc_to_segment_count(0.0001, 0.005, 360) == 8
    assert arc_to_segment_count(0.0001, 0.005, 10) == 2
    assert arc_to_segment_count(10.0, 0.005, 90) == round(90 / (2 * math.degrees(math.acos(1 - 0.0005))))


def test_circle_polygons():
    pts = circle_polygon(Vec2(1, 1), 2.0)
    assert len(pts) == arc_to_segment_count(2.0, 0.005, 360)
    assert all(p.distance(Vec2(1, 1)) == pytest.approx(2.0) for p in pts)
    pad = pad_circle_polygon(Vec2(0, 0), 0.5)
    assert len(pad) % 8 == 0 and all(p.length() == pytest.approx(0.5) for p in pad)


def test_arc_center_and_points():
    s, m, e = Vec2(1, 0), Vec2(0, 1), Vec2(-1, 0)
    assert arc_center(s, m, e).close_to(Vec2(0, 0))
    assert arc_center(Vec2(0, 0), Vec2(1, 1), Vec2(2, 2)) is None
    c, r, a0, sweep = arc_angles(s, m, e)
    assert r == pytest.approx(1.0) and a0 == pytest.approx(0.0) and sweep == pytest.approx(180.0)
    pts = arc_points(s, m, e)
    assert pts[0] == s and pts[-1] == e and all(p.length() == pytest.approx(1.0) for p in pts)
    assert any(p.y > 0.9 for p in pts)  # passes through the mid point side
    # opposite direction
    pts2 = arc_points(s, Vec2(0, -1), e)
    assert all(p.y <= 1e-9 for p in pts2)
    assert arc_points(Vec2(0, 0), Vec2(1, 1), Vec2(2, 2)) == [Vec2(0, 0), Vec2(2, 2)]


def test_bezier_points():
    pts = bezier_points(Vec2(0, 0), Vec2(0, 1), Vec2(1, 1), Vec2(1, 0), segments=4)
    assert len(pts) == 5 and pts[0] == Vec2(0, 0) and pts[-1] == Vec2(1, 0)
    assert pts[2].x == pytest.approx(0.5) and pts[2].y == pytest.approx(0.75)


# ---- segments ----------------------------------------------------------------------------------


def test_segment_tests():
    assert point_segment_distance(Vec2(1, 1), Vec2(0, 0), Vec2(2, 0)) == pytest.approx(1.0)
    assert point_segment_distance(Vec2(5, 0), Vec2(0, 0), Vec2(2, 0)) == pytest.approx(3.0)
    assert point_segment_distance(Vec2(5, 0), Vec2(2, 2), Vec2(2, 2)) == pytest.approx(math.sqrt(13))
    assert segments_intersect(Vec2(0, 0), Vec2(2, 2), Vec2(0, 2), Vec2(2, 0))
    assert segments_intersect(Vec2(0, 0), Vec2(2, 0), Vec2(2, 0), Vec2(2, 5))  # touching at an end
    assert segments_intersect(Vec2(0, 0), Vec2(4, 0), Vec2(1, 0), Vec2(2, 0))  # collinear overlap
    assert not segments_intersect(Vec2(0, 0), Vec2(1, 0), Vec2(0, 1), Vec2(1, 1))
    x = segment_intersection(Vec2(0, 0), Vec2(2, 2), Vec2(0, 2), Vec2(2, 0))
    assert x is not None and x.close_to(Vec2(1, 1))
    assert segment_intersection(Vec2(0, 0), Vec2(1, 0), Vec2(0, 1), Vec2(1, 1)) is None


# ---- polygons ----------------------------------------------------------------------------------


def test_point_in_polygon_and_holes():
    ring = (Vec2(0, 0), Vec2(10, 0), Vec2(10, 10), Vec2(0, 10))
    assert point_in_polygon(Vec2(5, 5), ring) == 1
    assert point_in_polygon(Vec2(10, 5), ring) == 0
    assert point_in_polygon(Vec2(11, 5), ring) == -1
    assert point_in_polygon(Vec2(5, 5), ring[:2]) == -1
    poly = Polygon(ring, ((Vec2(4, 4), Vec2(6, 4), Vec2(6, 6), Vec2(4, 6)),))
    assert poly.contains(Vec2(1, 1)) and not poly.contains(Vec2(5, 5))
    assert not poly.contains(Vec2(10, 5)) and poly.contains(Vec2(10, 5), on_boundary=True)
    assert not poly.contains(Vec2(4, 5)) and poly.contains(Vec2(4, 5), on_boundary=True)
    assert poly.area() == pytest.approx(96.0) and abs(poly.signed_area()) == pytest.approx(100.0)
    assert poly.boundary_distance(Vec2(1, 5)) == pytest.approx(1.0)
    assert poly.boundary_distance(Vec2(5, 3.5)) == pytest.approx(0.5)  # nearest is the hole
    assert Polygon.of([Vec2(0, 0), Vec2(0, 0), Vec2(1, 0), Vec2(1, 1), Vec2(0, 0)]).points == (
        Vec2(0, 0),
        Vec2(1, 0),
        Vec2(1, 1),
    )
    assert poly.translated(Vec2(1, 1)).points[0] == Vec2(1, 1)
    assert len(poly.edges()) == 4 and len(poly) == 4
    with pytest.raises(ValueError):
        _ = Polygon(()).bbox


def test_polygons_overlap_semantics():
    a = Polygon.rect(0, 0, 10, 10)
    assert polygons_overlap(a, Polygon.rect(5, 5, 15, 15))
    assert not polygons_overlap(a, Polygon.rect(10, 0, 20, 10))  # touching edge
    assert not polygons_overlap(a, Polygon.rect(10, 10, 20, 20))  # touching corner
    assert not polygons_overlap(a, Polygon.rect(9.995, 0, 20, 10))  # 5 um strip: below KiCad's deflation
    assert polygons_overlap(a, Polygon.rect(9.98, 0, 20, 10))  # 20 um strip
    assert polygons_overlap(a, Polygon.rect(2, 2, 3, 3))  # containment
    assert polygons_overlap(Polygon.rect(2, 2, 3, 3), a)
    # plus sign: no vertex of either inside the other
    assert polygons_overlap(Polygon.rect(4, 0, 6, 10), Polygon.rect(0, 4, 10, 6))
    assert not polygons_overlap(Polygon.rect(4, 0, 6, 10), Polygon.rect(0, 4, 10, 4.005))
    assert not polygons_overlap(a, Polygon.rect(30, 30, 40, 40))
    assert polygons_overlap(a, Polygon.rect(9.5, 0, 20, 10), tolerance=0.1)
    assert not polygons_overlap(a, Polygon.rect(9.95, 0, 20, 10), tolerance=0.1)
    assert COURTYARD_TOLERANCE_MM == 0.01


def test_overlap_bbox_is_exact():
    a = Polygon.rect(0, 0, 10, 10)
    assert overlap_bbox(a, Polygon.rect(5, 5, 15, 15)) == BBox(5, 5, 10, 10)
    assert overlap_bbox(Polygon.rect(4, 0, 6, 10), Polygon.rect(0, 4, 10, 6)) == BBox(4, 4, 6, 6)
    assert overlap_bbox(a, Polygon.rect(2, 2, 3, 3)) == BBox(2, 2, 3, 3)
    assert overlap_bbox(a, Polygon.rect(30, 30, 40, 40)) is None
    # concave: a U shape against a bar through its opening
    u = Polygon.of(
        [Vec2(0, 0), Vec2(9, 0), Vec2(9, 9), Vec2(6, 9), Vec2(6, 3), Vec2(3, 3), Vec2(3, 9), Vec2(0, 9)]
    )
    bar = Polygon.rect(4, 5, 5, 12)
    assert not polygons_overlap(u, bar)
    assert polygons_overlap(u, Polygon.rect(4, 1, 5, 12))
    assert overlap_bbox(u, Polygon.rect(4, 1, 5, 12)) == BBox(4, 1, 5, 3)


def test_chain_outline_and_nesting():
    closed, open_chains = chain_outline(
        [
            [Vec2(0, 0), Vec2(10, 0)],
            [Vec2(10, 10), Vec2(10, 0)],  # reversed
            [Vec2(0, 10), Vec2(10, 10)],
            [Vec2(0, 10), Vec2(0, 0)],
            [Vec2(20, 20), Vec2(30, 20)],  # open leftover
            [Vec2(30, 20.005), Vec2(30, 30)],  # within epsilon of the previous end
        ]
    )
    assert len(closed) == 1 and set(closed[0].points) == {Vec2(0, 0), Vec2(10, 0), Vec2(10, 10), Vec2(0, 10)}
    assert len(open_chains) == 1 and len(open_chains[0]) == 3
    # a ring that closes on itself is kept as-is; nesting assigns the inner ring as a hole
    outer = Polygon.rect(0, 0, 10, 10)
    inner = Polygon.rect(2, 2, 4, 4)
    nested = nest_rings([inner, outer])
    assert len(nested) == 1 and nested[0].holes == (inner.points,)
    assert nest_rings([outer]) == [outer]
    two = nest_rings([outer, Polygon.rect(20, 20, 30, 30)])
    assert len(two) == 2 and all(not p.holes for p in two)
