"""`ratsnest_estimate`: airwires per net from the board file, as a minimum spanning tree between
the connected groups of a net.

THIS IS AN ESTIMATE. KiCad computes its ratsnest from the real copper connectivity (pad shapes,
track/arc geometry, via stacks and zone fills, `CONNECTIVITY_DATA`) and exposes it only in the
editor; neither kicad-cli nor the KiCad 11 IPC API exports airwires with end points (D17 tier 3
lists a `pcb export ratsnest` / `GetRatsnest` upstream candidate). Until that lands the
file-tier estimate is:

* the items of a net are its pads, tracks, arcs and vias, plus the zone fills of the net:
  `zones_connect="filled"` (the default) joins to a zone's group every pad whose copper polygon
  overlaps one of the zone's STORED fill polygons on a shared copper layer (thermal-relief spokes
  reach into the pad, solid fills cover it), every via whose disc overlaps a fill and every track
  whose STROKED SEGMENT overlaps a fill (an end centre inside it, a fill vertex within half the
  track width of the segment, or a fill edge crossing / within half the width of it) - the
  copper overlap KiCad's `CN_ZONE_LAYER` collision computes from the same fill. The end-centre
  rule alone missed complex_hierarchy `Net-(R212-Pad2)` (a 0.4318 mm track overlapping a tiny
  fill whose end centre lies outside the fill bbox; reviewer finding 2026-09-19). Every stored fill polygon is its own copper
  region: two pads on different polygons of one zone (islands) are not connected by it, while
  polygons that touch each other - of one zone or of two zones of the net - are one region
  (the openair-max demo abuts two PGND zones; multichannel's GND zone keeps islands).
  `zones_connect=True` uses the zone OUTLINE instead (an unfilled zone the agent intends to
  fill), `False` makes zones connect nothing;
* two items are connected when their copper touches on a shared copper layer, approximated as
  KiCad's shape intersection does it: a track end and a via are discs of half the track width /
  half the via size (KiCad's round track ends), a pad is its polygon (`pad_hit=True`; with it off
  only the pad centre counts), so a track that stops short of a pad edge but still overlaps it with
  its width, a track ending on the body of another track (T-junction), two overlapping track
  ends and two overlapping pads (a vertex inside the other polygon or crossing edges) all
  connect (verified 2026-09-19 on ecc83: P1.2 / P4.1 tracks end 0.28 / 0.15 mm outside the
  pad edge with 0.8 mm width — KiCad routes them, the end-point rule reported them unrouted).
  The touch is EXACT and STRICT (`touch_tolerance` 1e-6 mm, float noise only): KiCad's `CN_ITEM`
  collision uses clearance 0 and `SHAPE_SEGMENT::Collide` connects only when the centre-line
  distance is strictly below the sum of the half-widths (two 0.15 mm track ends exactly 0.15 mm
  apart are unconnected). The earlier 0.01 mm tolerance joined copper KiCad keeps apart (stickhub
  `/LC`: an arc end 0.0035 mm short of pad D20.GK's edge, four such pairs in one net; the 0.01 mm
  tolerance under-counted 105 vs DRC's 107 airwires on the mutated board);
* copper graphics (board-level `gr_rect` / `gr_poly` / `gr_circle` / `gr_line` / `gr_arc` and
  footprint `fp_*` shapes on a copper layer) carry no net but conduct: every node of one net that
  touches the same graphic (or a chain of touching graphics) is joined - the way a connector's
  shield pads are bridged by a copper rectangle on the openair-max demo, or a solder-jumper bridge
  is drawn (KiCad's connectivity treats copper shapes as items; DRC reports the graphic only when
  it touches two different nets);
* KiCad's footprint-level jumper rules (`CN_CONNECTIVITY_ALGO::updateJumperPads`, master and the
  10.0 branch): the same-number pads of a footprint are one node ONLY when it carries
  `(duplicate_pad_numbers_are_jumpers yes)`, and the pads listed together in a
  `(jumper_pad_groups ("1" "2"))` group are joined by the footprint's own copper. Plain stacked
  pads with one number and `net_tie_pad_groups` do NOT connect: verified 2026-09-19 with
  `kicad-cli pcb drc` on 10.0.6 and 10.99.0-4083 (two "1" pads of one footprint on one net get
  an `unconnected_items` violation between them; a net-tie footprint's pads too; the same
  footprint with the jumper attribute / group gets none - scratchpad fixer/probe_stacked.py).
  A blind / buried via connects every copper layer between the two it names, a through via
  every copper layer;
* the remaining groups are joined by Kruskal's MST on the distances between the closest pad
  centres (or via/track ends) of each group, which is how KiCad's ratsnest chooses its airwires
  (RN_NET::compute, Delaunay-then-MST) up to tie-breaking.

Hence the airwire *count* (unrouted connections per net) matches KiCad whenever the grouping
matches, the end points may differ from KiCad's when a group has several candidates at the same
distance, and the estimate is silent about DRC-level shorts. Airwires between two track / via
groups carry `track@<layer>` / `via` end refs (distinct per layer) so a cross-layer gap at one
x/y is visible as such instead of a zero-length wire.

Measured against `kicad-cli pcb drc` `unconnected_items` (one violation per missing connection):
on the routed demo boards of 10.99 / 10.0.6 / 9.0.9 / 8.0.9 both sides report 0 (incl. the 148
of multichannel_mixer-unrouted); on boards with 30 % of their segments / arcs / vias deleted
through the CST (the case the estimate exists for; `tests/test_ratsnest_drc_live.py`, seed 2 on
10.0.6) the per-net counts agree on stickhub (107/107), complex_hierarchy (75/75) and
tiny_tapeout (383/383) after the exact-touch and stroked-segment fixes above; boards whose DRC
count hits KiCad's 499-item cap for `unconnected_items` (kit-dev-coldfire: 499 vs 588 estimated)
cannot be compared - the DRC oracle only validates counts below the cap
(`cli.drc_extended_error_limit`). The ipc11 provider (`backends.ipc.queries.ratsnest`, KiCad's
own connectivity clusters) is preferred whenever available. The hand-written cases are in
`tests/test_formats_analysis.py`. Cost: ~0.03 s for ecc83, ~7 s for vme-wren (1508 footprints,
25k tracks) after the file parse.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from kiwrap.formats.board import ArcTrack, BoardFile, Footprint, Pad, Track, Via, Zone
from kiwrap.formats.geometry import BBox, Polygon, Vec2, point_segment_distance, segments_intersect
from kiwrap.formats.reports import Netlist

__all__ = ["Airwire", "Ratsnest", "ratsnest_estimate"]


@dataclass(frozen=True)
class Airwire:
    net: str
    start: Vec2
    end: Vec2
    start_ref: str  # "R1.2" for pads, "via", "track"
    end_ref: str

    @property
    def length(self) -> float:
        return self.start.distance(self.end)


@dataclass
class Ratsnest:
    airwires: list[Airwire]
    unrouted_by_net: dict[str, int]
    pads_without_net_items: list[str]  # "R1.2" pads whose netlist net has no board net yet
    netlist_only_nets: list[str]  # nets in the netlist with no pads on the board

    @property
    def unrouted(self) -> int:
        return len(self.airwires)


class _UF:
    def __init__(self, n: int) -> None:
        self.p = list(range(n))

    def find(self, i: int) -> int:
        while self.p[i] != i:
            self.p[i] = self.p[self.p[i]]
            i = self.p[i]
        return i

    def union(self, a: int, b: int) -> bool:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return False
        self.p[rb] = ra
        return True


@dataclass
class _Node:
    pos: Vec2
    layers: frozenset[str]
    ref: str
    pad: Pad | None = None
    radius: float = 0.0  # copper radius around `pos`: track half-width, via half-size (pads: polygon)
    seg: tuple[Vec2, Vec2] | None = None  # the track this end point belongs to (T-junctions)


def ratsnest_estimate(
    board: BoardFile,
    netlist: Netlist | None = None,
    *,
    touch_tolerance: float = 1e-6,
    pad_hit: bool = True,
    zones_connect: bool | Literal["filled"] = "filled",
) -> Ratsnest:
    """Airwires per net (see module docstring). With a `netlist`, pads are assigned to nets by
    (reference, pin) from it — which makes the estimate usable right after a schematic change,
    before KiCad has updated the board — otherwise the pads' own `(net …)` is used.
    `zones_connect`: "filled" (stored fill polygons connect, the default), True (zone outlines
    connect) or False (zones connect nothing)."""
    copper = set(board.copper_layers) or {"F.Cu", "B.Cu"}
    pad_net: dict[tuple[str, str], str] = {}
    netlist_only: list[str] = []
    if netlist is not None:
        board_pads = {(fp.reference, pad.number) for fp, pad in board.pads()}
        for net in netlist.nets:
            hit = False
            for node in net.nodes:
                pad_net[(node.ref, node.pin)] = net.name
                if (node.ref, node.pin) in board_pads:
                    hit = True
            if not hit:
                netlist_only.append(net.name)
    nets: dict[str, list[_Node]] = {}
    pairs: dict[str, list[tuple[int, int]]] = {}
    orphans: list[str] = []
    by_number: dict[tuple[str, str, str], list[int]] = {}  # (net, ref, pad number) -> node indices
    jumpers: dict[tuple[str, str], tuple[bool, list[list[str]]]] = {}  # (net, ref) -> footprint rules
    for fp, pad in board.pads():
        if netlist is not None:
            name = pad_net.get((fp.reference, pad.number))
            if name is None:
                if pad.net is not None and not pad.net.is_unconnected:
                    orphans.append(f"{fp.reference}.{pad.number}")
                continue
        else:
            if pad.net is None or pad.net.is_unconnected:
                continue
            name = pad.net.name
        layers = frozenset(layer for layer in pad.expand_layers(copper) if layer in copper)
        lst = nets.setdefault(name, [])
        lst.append(_Node(pad.position, layers, f"{fp.reference}.{pad.number}", pad))
        by_number.setdefault((name, fp.reference, pad.number), []).append(len(lst) - 1)
        if (name, fp.reference) not in jumpers:
            jumpers[(name, fp.reference)] = _jumper_rules(fp)
    for (name, ref, _number), idx in by_number.items():
        if jumpers[(name, ref)][0]:  # duplicate_pad_numbers_are_jumpers: same-number pads are one node
            for other in idx[1:]:
                pairs.setdefault(name, []).append((idx[0], other))
    for (name, ref), (_dup, groups) in jumpers.items():
        for group in groups:  # jumper_pad_groups: the listed pads are one node
            members = [i for number in group for i in by_number.get((name, ref, number), ())]
            for other in members[1:]:
                pairs.setdefault(name, []).append((members[0], other))
    copper_items: list[Track | ArcTrack] = [*board.tracks, *board.arcs]
    for t in copper_items:
        if t.net is None or t.net.name not in nets:
            continue
        lst = nets[t.net.name]
        half = t.width / 2
        seg = (t.start, t.end) if isinstance(t, Track) else None  # arcs: end discs only
        ref = f"track@{t.layer}"
        lst.append(_Node(t.start, frozenset({t.layer}), ref, radius=half, seg=seg))
        lst.append(_Node(t.end, frozenset({t.layer}), ref, radius=half, seg=seg))
        pairs.setdefault(t.net.name, []).append((len(lst) - 2, len(lst) - 1))
    for v in board.vias:
        if v.net is None or v.net.name not in nets:
            continue
        nets[v.net.name].append(_Node(v.position, _via_layers(v, board), "via", radius=v.size / 2))

    bridges, chains = _copper_bridges(board, copper)
    airwires: list[Airwire] = []
    unrouted: dict[str, int] = {}
    for name, nodes in nets.items():
        n = len(nodes)
        uf = _UF(n)
        for i, j in pairs.get(name, ()):  # a track connects its own two end points
            uf.union(i, j)
        zone_groups: list[list[int]] = []
        if zones_connect:
            net_zones = [
                z for z in board.zones if z.net is not None and z.net.name == name and not z.is_rule_area
            ]
            if zones_connect == "filled":
                regions: list[tuple[str, _FillIndex]] = [
                    (layer, fill)
                    for z in net_zones
                    for layer, fills in _fills_of(z).items()
                    for fill in fills
                ]
                region_members = [
                    [
                        i
                        for i, x in enumerate(nodes)
                        if layer in x.layers and _in_fill(x, {layer: [fill]}, touch_tolerance)
                    ]
                    for layer, fill in regions
                ]
                ruf = _UF(len(regions))
                for ra in range(len(regions)):
                    for rc in range(ra + 1, len(regions)):
                        la, fa = regions[ra]
                        lc, fc = regions[rc]
                        if la == lc and _fills_touch({la: [fa]}, {lc: [fc]}, touch_tolerance):
                            ruf.union(ra, rc)
                merged: dict[int, list[int]] = {}
                for ri, members in enumerate(region_members):
                    merged.setdefault(ruf.find(ri), []).extend(members)
                zone_groups = [m for m in merged.values() if m]
            else:
                zone_members: list[list[int]] = [
                    [
                        i
                        for i, x in enumerate(nodes)
                        if x.layers & set(z.layers)
                        and any(poly.contains(x.pos, on_boundary=True) for poly in z.outline)
                    ]
                    for z in net_zones
                ]
                zuf = _UF(len(net_zones))
                for za in range(len(net_zones)):
                    for zc in range(za + 1, len(net_zones)):
                        joined = bool(set(net_zones[za].layers) & set(net_zones[zc].layers)) and any(
                            oa.bbox.intersects(oc.bbox, touch_tolerance)
                            and (
                                any(oc.contains(v, on_boundary=True) for v in oa.points)
                                or any(oa.contains(v, on_boundary=True) for v in oc.points)
                            )
                            for oa in net_zones[za].outline
                            for oc in net_zones[zc].outline
                        )
                        if joined:
                            zuf.union(za, zc)
                merged_z: dict[int, list[int]] = {}
                for zi, members in enumerate(zone_members):
                    merged_z.setdefault(zuf.find(zi), []).extend(members)
                zone_groups = [m for m in merged_z.values() if m]
        for i, j in _candidate_pairs(nodes, touch_tolerance):
            a, b = nodes[i], nodes[j]
            if not (a.layers & b.layers):
                continue
            if _touch(a, b, touch_tolerance, pad_hit):
                uf.union(i, j)
        for members in zone_groups:
            for m in members[1:]:
                uf.union(members[0], m)
        for chain in chains:
            touching = [
                i
                for i, x in enumerate(nodes)
                for k in chain
                if bridges[k].layer in x.layers and _touches_bridge(x, bridges[k], touch_tolerance)
            ]
            for m in touching[1:]:
                uf.union(touching[0], m)
        clusters: dict[int, list[int]] = {}
        for i in range(n):
            clusters.setdefault(uf.find(i), []).append(i)
        if len(clusters) <= 1:
            unrouted[name] = 0
            continue
        # candidate points per group: pads first, else any node
        reps: list[list[int]] = []
        for members in clusters.values():
            pads = [i for i in members if nodes[i].pad is not None]
            reps.append(pads or members)
        edges: list[tuple[float, int, int, int, int]] = []
        for gi in range(len(reps)):
            for gj in range(gi + 1, len(reps)):
                best = None
                for i in reps[gi]:
                    for j in reps[gj]:
                        d = nodes[i].pos.distance(nodes[j].pos)
                        if best is None or d < best[0]:
                            best = (d, i, j)
                if best is not None:
                    edges.append((best[0], gi, gj, best[1], best[2]))
        edges.sort()
        guf = _UF(len(reps))
        count = 0
        for _d, gi, gj, i, j in edges:
            if guf.union(gi, gj):
                airwires.append(Airwire(name, nodes[i].pos, nodes[j].pos, nodes[i].ref, nodes[j].ref))
                count += 1
        unrouted[name] = count
    return Ratsnest(
        airwires=airwires,
        unrouted_by_net=unrouted,
        pads_without_net_items=orphans,
        netlist_only_nets=netlist_only,
    )


@dataclass(frozen=True)
class _Bridge:
    """A copper graphic as a conductor: a polygon (closed shapes) or stroked segments (open ones)."""

    layer: str
    bbox: BBox
    polygon: Polygon | None
    segments: tuple[tuple[Vec2, Vec2], ...]
    half_width: float

    def edges(self) -> list[tuple[Vec2, Vec2]]:
        return (self.polygon.edges() if self.polygon is not None else []) + list(self.segments)


def _copper_bridges(board: BoardFile, copper: set[str]) -> tuple[list[_Bridge], list[list[int]]]:
    """Board and footprint graphics on copper layers, plus the chains of graphics that touch each
    other on one layer (a rectangle drawn as four lines conducts as one shape)."""
    bridges: list[_Bridge] = []
    items = list(board.graphics) + [g for fp in board.footprints for g in fp.graphics]
    for g in items:
        if g.layer not in copper or len(g.points) < 2:
            continue
        poly = g.polygon()
        half = g.width / 2
        pts = list(g.points)
        if poly is not None and len(poly):
            bridges.append(_Bridge(g.layer, poly.bbox, poly, (), half))
            continue
        segs = tuple((pts[i], pts[i + 1]) for i in range(len(pts) - 1))
        box = BBox(
            min(q.x for q in pts) - half,
            min(q.y for q in pts) - half,
            max(q.x for q in pts) + half,
            max(q.y for q in pts) + half,
        )
        bridges.append(_Bridge(g.layer, box, None, segs, half))
    uf = _UF(len(bridges))
    for i in range(len(bridges)):
        for j in range(i + 1, len(bridges)):
            a, b = bridges[i], bridges[j]
            if a.layer == b.layer and a.bbox.intersects(b.bbox, 0.01) and _bridges_touch(a, b):
                uf.union(i, j)
    groups: dict[int, list[int]] = {}
    for i in range(len(bridges)):
        groups.setdefault(uf.find(i), []).append(i)
    return bridges, list(groups.values())


def _bridges_touch(a: _Bridge, b: _Bridge) -> bool:
    reach = a.half_width + b.half_width + 0.01
    ea, eb = a.edges(), b.edges()
    for p, q in ea:
        for r, t in eb:
            if segments_intersect(p, q, r, t):
                return True
            if (
                point_segment_distance(p, r, t) <= reach
                or point_segment_distance(q, r, t) <= reach
                or point_segment_distance(r, p, q) <= reach
                or point_segment_distance(t, p, q) <= reach
            ):
                return True
    for inner, outer in ((a, b), (b, a)):
        if outer.polygon is not None and any(
            outer.polygon.contains(p, on_boundary=True, eps=0.01) for p, _q in inner.edges()
        ):
            return True
    return False


def _touches_bridge(node: _Node, bridge: _Bridge, tol: float) -> bool:
    """Node copper meets the graphic: pad polygon vs graphic (vertex containment / edge crossing /
    stroke distance), disc nodes by centre distance."""
    if not _extent(node).intersects(bridge.bbox, tol):
        return False
    reach = bridge.half_width + node.radius + tol
    edges = bridge.edges()
    if node.pad is None:
        if bridge.polygon is not None and bridge.polygon.contains(node.pos, on_boundary=True, eps=tol):
            return True
        return any(point_segment_distance(node.pos, a, b) <= reach for a, b in edges)
    for pp in node.pad.polygons():
        if not len(pp):
            continue
        if bridge.polygon is not None:
            if any(pp.contains(v, on_boundary=True, eps=tol) for v in bridge.polygon.points):
                return True
            if any(bridge.polygon.contains(v, on_boundary=True, eps=tol) for v in pp.points):
                return True
        pad_edges = pp.edges()
        for c, d in edges:
            if any(segments_intersect(a, b, c, d) for a, b in pad_edges):
                return True
            if bridge.half_width > 0 and (
                any(point_segment_distance(v, c, d) <= reach for v in pp.points)
                or pp.contains(c, on_boundary=True, eps=reach)
                or pp.contains(d, on_boundary=True, eps=reach)
            ):
                return True
    return False


def _via_layers(via: Via, board: BoardFile) -> frozenset[str]:
    """The copper layers a via joins: all of them for a through via, the span between the two
    named layers (inclusive, in stack order) for blind / buried / micro vias."""
    order = list(board.copper_layers)
    if via.kind == "through" or len(via.layers) < 2 or not order:
        return frozenset(order) if order else frozenset({"F.Cu", "B.Cu"})
    try:
        a, b = order.index(via.layers[0]), order.index(via.layers[1])
    except ValueError:
        return frozenset(via.layers)
    lo, hi = min(a, b), max(a, b)
    return frozenset(order[lo : hi + 1])


def _jumper_rules(fp: Footprint) -> tuple[bool, list[list[str]]]:
    """A footprint's `(duplicate_pad_numbers_are_jumpers yes)` flag and its
    `(jumper_pad_groups ("1" "2") ("3" "4"))` groups (module docstring: the only footprint-level
    rules KiCad's connectivity joins pads by; `net_tie_pad_groups` is a DRC allowance, not a
    connection)."""
    node = fp.node
    if node is None:
        return False, []
    dup = False
    for child in node.find_all("duplicate_pad_numbers_are_jumpers"):
        dup = any(v in ("yes", "true") for v in child.values())
    groups: list[list[str]] = []
    for child in node.find_all("jumper_pad_groups"):
        for group in child.children:
            values = getattr(group, "values", None)
            numbers = [str(v).strip() for v in (values() if callable(values) else [])]
            if len(numbers) > 1:
                groups.append(numbers)
    return dup, groups


def _extent(node: _Node) -> BBox:
    """The copper extent of a node: its pad bbox, else a disc of its radius, plus the track body
    (a track end is tested against the whole segment of the other track)."""
    if node.pad is not None:
        boxes = [pp.bbox for pp in node.pad.polygons() if len(pp)]
        if boxes:
            return BBox(
                min(b.min_x for b in boxes),
                min(b.min_y for b in boxes),
                max(b.max_x for b in boxes),
                max(b.max_y for b in boxes),
            )
    r = node.radius
    pts = [node.pos] if node.seg is None else [node.pos, node.seg[0], node.seg[1]]
    return BBox(
        min(q.x for q in pts) - r,
        min(q.y for q in pts) - r,
        max(q.x for q in pts) + r,
        max(q.y for q in pts) + r,
    )


def _candidate_pairs(nodes: list[_Node], tol: float, cell: float = 2.0) -> list[tuple[int, int]]:
    """Index pairs whose copper extents can touch (a uniform grid over the extents; the ecc83
    GND net has 40 nodes, vme-wren's thousands - the O(n^2) loop was the whole runtime there)."""
    n = len(nodes)
    if n < 64:
        return [(i, j) for i in range(n) for j in range(i + 1, n)]
    grid: dict[tuple[int, int], list[int]] = {}
    boxes = [_extent(x) for x in nodes]
    for i, b in enumerate(boxes):
        for cx in range(int((b.min_x - tol) // cell), int((b.max_x + tol) // cell) + 1):
            for cy in range(int((b.min_y - tol) // cell), int((b.max_y + tol) // cell) + 1):
                grid.setdefault((cx, cy), []).append(i)
    seen: set[tuple[int, int]] = set()
    for members in grid.values():
        for u in range(len(members)):
            for v in range(u + 1, len(members)):
                i, j = members[u], members[v]
                if i > j:
                    i, j = j, i
                if (i, j) in seen:
                    continue
                if boxes[i].intersects(boxes[j], tol):
                    seen.add((i, j))
    return sorted(seen)


class _FillIndex:
    """One stored fill polygon with its edges bucketed on a grid, so `contains` costs the edges
    near the point instead of all of them (`Polygon.contains` walks every edge for the boundary
    tolerance: ~3 ms per call on a 2000-vertex fill, and a routed board asks thousands of times).
    Even-odd rule over every ring (holes included), boundary points within `tol` count as inside."""

    def __init__(self, poly: Polygon, cell: float = 1.0):
        self.poly = poly
        self.bbox = poly.bbox
        self.cell = cell
        self.rows: dict[int, list[tuple[Vec2, Vec2]]] = {}
        self.cells: dict[tuple[int, int], list[tuple[Vec2, Vec2]]] = {}
        for ring in poly.all_rings():
            n = len(ring)
            for k in range(n):
                a, b = ring[k], ring[(k + 1) % n]
                y0, y1 = int(min(a.y, b.y) // cell), int(max(a.y, b.y) // cell)
                for cy in range(y0, y1 + 1):
                    self.rows.setdefault(cy, []).append((a, b))
                x0, x1 = int(min(a.x, b.x) // cell), int(max(a.x, b.x) // cell)
                for cx in range(x0, x1 + 1):
                    for cy in range(y0, y1 + 1):
                        self.cells.setdefault((cx, cy), []).append((a, b))

    def edges_near(self, box: BBox) -> list[tuple[Vec2, Vec2]]:
        """The fill edges whose grid cells overlap `box` (each edge once)."""
        cx0, cx1 = int(box.min_x // self.cell) - 1, int(box.max_x // self.cell) + 1
        cy0, cy1 = int(box.min_y // self.cell) - 1, int(box.max_y // self.cell) + 1
        out: list[tuple[Vec2, Vec2]] = []
        seen: set[int] = set()
        for cx in range(cx0, cx1 + 1):
            for cy in range(cy0, cy1 + 1):
                for e in self.cells.get((cx, cy), ()):
                    if id(e) not in seen:
                        seen.add(id(e))
                        out.append(e)
        return out

    def contains(self, p: Vec2, tol: float) -> bool:
        if not self.bbox.contains(p, tol):
            return False
        cx, cy = int(p.x // self.cell), int(p.y // self.cell)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for a, b in self.cells.get((cx + dx, cy + dy), ()):
                    if point_segment_distance(p, a, b) <= tol:
                        return True
        inside = False
        for a, b in self.rows.get(cy, ()):
            if (a.y > p.y) != (b.y > p.y):
                x = a.x + (p.y - a.y) * (b.x - a.x) / (b.y - a.y)
                if x > p.x:
                    inside = not inside
        return inside


def _fills_touch(a: dict[str, list[_FillIndex]], b: dict[str, list[_FillIndex]], tol: float) -> bool:
    """Do two zones' stored fills share copper on some layer (a vertex of one inside the other)?"""
    for layer, fa in a.items():
        for fb in b.get(layer, ()):
            for ia in fa:
                if not ia.bbox.intersects(fb.bbox, tol):
                    continue
                if any(ia.contains(v, tol) for v in fb.poly.points if ia.bbox.contains(v, tol)):
                    return True
                if any(fb.contains(v, tol) for v in ia.poly.points if fb.bbox.contains(v, tol)):
                    return True
    return False


def _fills_of(zone: Zone) -> dict[str, list[_FillIndex]]:
    """The zone's stored fill polygons per layer, indexed once per zone (the fills carry
    thousands of vertices, the nodes of a ground net are many)."""
    return {layer: [_FillIndex(poly) for poly in polys if len(poly)] for layer, polys in zone.filled.items()}


def _in_fill(node: _Node, fills: dict[str, list[_FillIndex]], tol: float) -> bool:
    """Does the node touch the zone's stored fill on a layer they share? Pads by copper contact
    with the fill polygon (a thermal spoke ends inside the pad copper, a solid fill covers it),
    vias by their disc, tracks by their stroked segment (module docstring)."""
    for layer, polys in fills.items():
        if layer not in node.layers:
            continue
        if node.pad is not None:
            pad_polys = [(pp, pp.bbox) for pp in node.pad.polygons() if len(pp)]
            for fill in polys:
                for pp, pp_box in pad_polys:
                    if pp_box.intersects(fill.bbox, tol) and _pad_touches_fill(
                        pp, pp_box, node.pos, fill, tol
                    ):
                        return True
            continue
        for fill in polys:
            if node.seg is not None:
                if _segment_touches_fill(node.seg, node.radius, fill, tol):
                    return True
            elif _disc_touches_fill(node.pos, node.radius, fill, tol):
                return True
    return False


def _within(distance: float, reach: float, tol: float) -> bool:
    """KiCad's collision predicate for stroked shapes (`SHAPE_SEGMENT::Collide`,
    `SHAPE_CIRCLE::Collide`: `dist_sq == 0 || dist_sq < min_dist^2`): copper overlaps only when
    the centre-line distance is STRICTLY below the sum of the half-widths - two 0.15 mm tracks
    whose ends sit exactly 0.15 mm apart are NOT connected (feather board `/Connectors/I2C.SDA`,
    verified against DRC 2026-09-19). `tol` absorbs float noise; a zero reach (pad centres in
    `pad_hit=False` mode) means coincidence."""
    if reach <= 0:
        return distance <= tol
    return distance + tol < reach


def _disc_touches_fill(centre: Vec2, radius: float, fill: _FillIndex, tol: float) -> bool:
    """A via / arc-end disc overlaps the fill: centre inside, or a fill edge within the radius."""
    if fill.contains(centre, tol):
        return True
    if radius <= 0:
        return False
    box = BBox(centre.x - radius, centre.y - radius, centre.x + radius, centre.y + radius)
    if not box.intersects(fill.bbox, 0.0):
        return False
    return any(_within(point_segment_distance(centre, a, b), radius, tol) for a, b in fill.edges_near(box))


def _segment_touches_fill(seg: tuple[Vec2, Vec2], half: float, fill: _FillIndex, tol: float) -> bool:
    """A stroked track segment overlaps the fill copper: an end centre inside it, a fill vertex
    inside the stroke, or a fill edge crossing / within half the width of the segment."""
    a, b = seg
    if fill.contains(a, tol) or fill.contains(b, tol):
        return True
    box = BBox(min(a.x, b.x) - half, min(a.y, b.y) - half, max(a.x, b.x) + half, max(a.y, b.y) + half)
    if not box.intersects(fill.bbox, 0.0):
        return False
    for c, d in fill.edges_near(box):
        if _within(point_segment_distance(c, a, b), half, tol) or _within(
            point_segment_distance(d, a, b), half, tol
        ):
            return True
        if segments_intersect(a, b, c, d):
            return True
        if _within(point_segment_distance(a, c, d), half, tol) or _within(
            point_segment_distance(b, c, d), half, tol
        ):
            return True
    return False


def _pad_touches_fill(pad: Polygon, pad_box: BBox, centre: Vec2, fill: _FillIndex, tol: float) -> bool:
    """Pad copper meets fill copper: a fill vertex inside the pad (thermal spoke tips), a fill
    edge crossing a pad edge (partial cover), or the pad centre inside the fill (solid cover).
    Cheap on purpose: only the fill vertices and edges near the pad are looked at
    (`polygons_overlap` costs ~0.7 s per pad on the ecc83 GND fill)."""
    cx0, cx1 = int(pad_box.min_x // fill.cell) - 1, int(pad_box.max_x // fill.cell) + 1
    cy0, cy1 = int(pad_box.min_y // fill.cell) - 1, int(pad_box.max_y // fill.cell) + 1
    edges: list[tuple[Vec2, Vec2]] = []
    seen: set[int] = set()
    for cx in range(cx0, cx1 + 1):
        for cy in range(cy0, cy1 + 1):
            for e in fill.cells.get((cx, cy), ()):
                if id(e) not in seen:
                    seen.add(id(e))
                    edges.append(e)
    for a, _b in edges:  # every fill vertex near the pad is the start of one of these edges
        if pad_box.contains(a, tol) and pad.contains(a, on_boundary=True, eps=tol):
            return True
    if edges:
        pad_edges = pad.edges()
        for c, d in edges:
            if any(segments_intersect(a, b, c, d) for a, b in pad_edges):
                return True
    return fill.contains(centre, tol)


def _pads_touch(a: Pad, b: Pad, tol: float) -> bool:
    """Two pad polygons share copper: a vertex of one inside the other, or crossing edges
    (overlapping same-number pads such as the feather board's U1 `A9` pair, 0.25 mm apart with
    0.4 mm copper, which KiCad connects by copper, not by number)."""
    for pa in a.polygons():
        if not len(pa):
            continue
        for pb in b.polygons():
            if not len(pb) or not pa.bbox.intersects(pb.bbox, tol):
                continue
            if any(pb.contains(v, on_boundary=True, eps=tol) for v in pa.points):
                return True
            if any(pa.contains(v, on_boundary=True, eps=tol) for v in pb.points):
                return True
            eb = pb.edges()
            if any(segments_intersect(p0, p1, q0, q1) for p0, p1 in pa.edges() for q0, q1 in eb):
                return True
    return False


def _touch(a: _Node, b: _Node, tol: float, pad_hit: bool) -> bool:
    """Copper contact between two nodes of one net on a shared layer (module docstring)."""
    if a.pad is not None and b.pad is not None:
        return _pads_touch(a.pad, b.pad, tol)
    reach = a.radius + b.radius
    if _within(a.pos.distance(b.pos), reach, tol):
        return True
    for end, body in ((a, b), (b, a)):  # a track end on the body of another track
        if (
            body.seg is not None
            and end.pad is None
            and _within(point_segment_distance(end.pos, body.seg[0], body.seg[1]), reach, tol)
        ):
            return True
    if pad_hit:
        for pad_node, other in ((a, b), (b, a)):
            if pad_node.pad is None or other.pad is not None:
                continue
            for poly in pad_node.pad.polygons():
                if poly.contains(other.pos, on_boundary=True, eps=tol):
                    return True
                if other.radius > 0 and _within(poly.boundary_distance(other.pos), other.radius, tol):
                    return True
                if other.seg is not None and (
                    _within(
                        point_segment_distance(pad_node.pos, other.seg[0], other.seg[1]), other.radius, tol
                    )
                    or any(
                        _within(point_segment_distance(v, other.seg[0], other.seg[1]), other.radius, tol)
                        for v in poly.points
                    )
                ):
                    return True  # the track body crosses the pad (its end is elsewhere)
    return False
