"""Pure-Python 2-D geometry for the file-tier models and analyses (zero dependencies).

`vec` (Vec2, BBox, KiCad-convention rotate), `arcs` (KiCad's arc/circle segmentation),
`polygon` (Polygon, point/segment/polygon tests, outline chaining). Everything is millimetres in
KiCad's Y-down frame; the docstrings of each module cite the KiCad source they reproduce.
"""

from __future__ import annotations

from kiwrap.formats.geometry.arcs import (
    MAX_ERROR_MM,
    arc_angles,
    arc_center,
    arc_points,
    arc_to_segment_count,
    bezier_points,
    circle_polygon,
    pad_circle_polygon,
)
from kiwrap.formats.geometry.polygon import (
    COURTYARD_TOLERANCE_MM,
    Polygon,
    chain_outline,
    nest_rings,
    overlap_bbox,
    point_in_polygon,
    point_segment_distance,
    polygons_overlap,
    segment_intersection,
    segments_intersect,
)
from kiwrap.formats.geometry.vec import BBox, Vec2, rotate

__all__ = [
    "COURTYARD_TOLERANCE_MM",
    "MAX_ERROR_MM",
    "BBox",
    "Polygon",
    "Vec2",
    "arc_angles",
    "arc_center",
    "arc_points",
    "arc_to_segment_count",
    "bezier_points",
    "chain_outline",
    "circle_polygon",
    "nest_rings",
    "overlap_bbox",
    "pad_circle_polygon",
    "point_in_polygon",
    "point_segment_distance",
    "polygons_overlap",
    "rotate",
    "segment_intersection",
    "segments_intersect",
]
