"""Courtyard overlap and off-board checks on a `BoardFile` (no KiCad process needed).

`courtyard_overlaps` mirrors `DRC_TEST_PROVIDER_COURTYARD_CLEARANCE::testCourtyardClearances`
(pcbnew/drc/drc_test_provider_courtyard_clearance.cpp, tag 10.0): footprints are compared per
side (front courtyards with front courtyards, back with back), a pair is a violation when their
courtyard polygons collide after each has been deflated by the 0.005 mm polygonisation error
(`FOOTPRINT::BuildCourtyardCaches`), i.e. touching is legal and overlaps deeper than 0.01 mm are
reported — `polygons_overlap` with `COURTYARD_TOLERANCE_MM`. KiCad's default courtyard clearance
constraint is 0; a project rule with a larger clearance is passed in as `clearance`. Footprints
without a courtyard are skipped here (KiCad reports them separately as `courtyards_missing` /
malformed, which `missing_courtyards` lists).

`off_board` reports footprints whose courtyard (or, without one, any pad) is not inside the
board outline built from Edge.Cuts. KiCad has no exact equivalent DRC check (it checks copper to
edge clearance); this is the file-tier "is it on the board" answer for placement feedback.
"""

from __future__ import annotations

from dataclasses import dataclass

from kiwrap.formats.board import BoardFile, Footprint
from kiwrap.formats.geometry import (
    COURTYARD_TOLERANCE_MM,
    BBox,
    Polygon,
    Vec2,
    overlap_bbox,
    polygons_overlap,
)

__all__ = ["CourtyardOverlap", "OffBoardItem", "courtyard_overlaps", "missing_courtyards", "off_board"]


@dataclass(frozen=True)
class CourtyardOverlap:
    a: str  # reference
    b: str
    side: str  # front | back
    bbox: BBox  # bounding box of the overlapping region (exact for the polygon pair)

    @property
    def position(self) -> Vec2:
        return self.bbox.center


def courtyard_overlaps(
    board: BoardFile, *, clearance: float = 0.0, tolerance: float = COURTYARD_TOLERANCE_MM
) -> list[CourtyardOverlap]:
    """Pairs of footprints whose same-side courtyards overlap by more than `tolerance`.

    `clearance` (mm) turns "overlap" into "closer than clearance" the way a courtyard clearance
    rule would; it is applied as a bbox pre-filter plus an overlap test on the clearance-expanded
    bboxes, which over-reports for non-rectangular courtyards — pass 0 for KiCad's default.
    """
    out: list[CourtyardOverlap] = []
    fps = board.footprints
    for side in ("front", "back"):
        entries: list[tuple[Footprint, tuple[Polygon, ...], BBox]] = []
        for fp in fps:
            polys = fp.courtyard_front if side == "front" else fp.courtyard_back
            if not polys:
                continue
            box = polys[0].bbox
            for p in polys[1:]:
                box = box.union(p.bbox)
            entries.append((fp, polys, box))
        for i, (fa, pa, ba) in enumerate(entries):
            for fb, pb, bb in entries[i + 1 :]:
                if not ba.intersects(bb, clearance + tolerance):
                    continue
                region: BBox | None = None
                for x in pa:
                    for y in pb:
                        xx, yy = x, y
                        if clearance > 0:
                            xx = Polygon.from_bbox(x.bbox.expanded(clearance / 2))
                            yy = Polygon.from_bbox(y.bbox.expanded(clearance / 2))
                        if polygons_overlap(xx, yy, tolerance):
                            r = overlap_bbox(xx, yy)
                            if r is not None:
                                region = r if region is None else region.union(r)
                if region is not None:
                    out.append(CourtyardOverlap(fa.reference, fb.reference, side, region))
    return out


def missing_courtyards(board: BoardFile) -> list[str]:
    """References of footprints with no closed courtyard on either side (DRC `courtyards_missing`)
    plus those whose courtyard did not close (`malformed_courtyard`)."""
    return [
        fp.reference
        for fp in board.footprints
        if (not fp.courtyard_front and not fp.courtyard_back) or fp.courtyard_open
    ]


@dataclass(frozen=True)
class OffBoardItem:
    """One footprint feature outside the outline. `kind` "pad" is copper outside the board (KiCad
    DRC `copper_edge_clearance` fails); "courtyard" is the courtyard alone overhanging, which
    KiCad has NO rule for (edge connectors and switches overhang by design: the ecc83 demo's P4,
    ten connectors of the jetson baseboard). `outside_by` is how far the feature's bounding box
    exceeds the outline's bounding box (mm, the largest of the four sides), `bbox` the feature's
    own box (courtyard or pad polygon)."""

    reference: str
    kind: str  # courtyard | pad
    detail: str  # "" for courtyard, pad number for pads
    position: Vec2  # a point that lies outside
    outside_by: float = 0.0
    bbox: BBox | None = None


def _overhang(box: BBox | None, outline: BBox) -> float:
    if box is None:
        return 0.0
    return round(
        max(
            0.0,
            outline.min_x - box.min_x,
            outline.min_y - box.min_y,
            box.max_x - outline.max_x,
            box.max_y - outline.max_y,
        ),
        4,
    )


def off_board(board: BoardFile, *, margin: float = 0.0) -> list[OffBoardItem]:
    """Footprints with a courtyard vertex or a pad polygon vertex outside the board outline
    (holes count as outside). A footprint with a courtyard is checked for BOTH: its courtyard
    (one item) and its pads (one item per pad outside), so callers can tell copper outside the
    board (must fix) from a courtyard overhang (review). Empty when the board has no closed
    outline — callers should check `board.outline` / `board.outline_open` first."""
    outer = board.board_polygon()
    if outer is None:
        return []
    outline_box = outer.bbox

    def inside(p: Vec2) -> bool:
        return outer.contains(p, on_boundary=True, eps=1e-6 + margin)

    out: list[OffBoardItem] = []
    for fp in board.footprints:
        polys = fp.courtyard
        if polys:
            bad = next((v for poly in polys for v in poly.points if not inside(v)), None)
            if bad is not None:
                box = BBox.of(v for poly in polys for v in poly.points)
                out.append(OffBoardItem(fp.reference, "courtyard", "", bad, _overhang(box, outline_box), box))
        for pad in fp.pads:
            bad = next((v for poly in pad.polygons() for v in poly.points if not inside(v)), None)
            if bad is not None:
                box = pad.bbox()
                out.append(
                    OffBoardItem(fp.reference, "pad", pad.number, bad, _overhang(box, outline_box), box)
                )
    return out
