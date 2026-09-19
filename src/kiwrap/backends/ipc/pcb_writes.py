"""Board authoring twins over the KiCad 11 headless IPC API (`ipc.headless`).

IPC counterparts of `kiwrap.formats.board_writer.BoardWriter` (the file writer): same names,
arguments and result dataclasses. Every mutation is one commit (one undo step), lengths are
millimetres, ids are KIID strings.

Verified on the stock 10.99 nightly (10.99.0-4083-g6dd893f7d1, 2026-09-19; probe3.py / probe4.py,
then tests/test_ipc_writes_live.py):

- `ImportNetlist` is handled headless (T5: AS_BAD_REQUEST without a path; a real
  `kicad-cli sch export netlist` file answers in ~20-40 ms after `LoadAllLibraries(footprint)`,
  ~4 s once per session). The response carries `error_count`, `warning_count`,
  `new_footprint_count` and a free-text `report` (KiCad's netlist updater log: "Added R100
  (footprint 'X')", "Changed R1 footprint from 'A' to 'B'", "Connected R100 pin 2 to GND",
  "Cannot add R100 (footprint 'Nope:Missing' not found)", "Cannot add R101 (no footprint
  assigned)", "Total warnings: 0, errors: 2."). `dry_run=True` runs the same log without
  changing the board and works BEFORE libraries are loaded. `new_footprint_count` counts
  instantiated footprints (added + replaced), not additions: 6 with 0 additions and 4
  replacements in probe4 - the structured report is therefore diffed from `read_board` before /
  after (docs/UPSTREAM.md G10), the log only supplies the unresolved reasons.
- New footprints are dropped by KiCad near the sheet origin ((1.505, 0.755) mm for R100), not
  off-board, so `pcb_sync` moves them onto a grid below the board's bounding box in a second
  commit. Existing footprints keep position / side / rotation; pad nets follow the netlist by
  (reference, pad number). `is_document_modified()` stays False after an import that changed
  the board (verified), so `pcb_save` never relies on it.
- `UpdateItems(FootprintInstance)` applies position / orientation / locked; `FlipItems` flips in
  place (position kept, layer F.Cu <-> B.Cu) and is the only way to change side.
- `CreateItems` handles `Zone` (copper, `RefillZones` ~0.6 s for one zone), `Via` (through) and
  several `Track`s in one commit; `DeleteItems` and `SaveDocument` / `SaveCopyOfDocument` work.
  The nightly writes `(version 20260901)` boards, which kicad-cli 10.0.6 cannot load, so the
  result is validated with the nightly's own `pcb drc --schematic-parity` (0 parity issues after
  a sync in probe3).
"""

from __future__ import annotations

import contextlib
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import kiwrap.backends.ipc.writes as _w
from kiwrap.backends.ipc import readers
from kiwrap.backends.ipc.models import BBox, BoardModel, FootprintModel, Point
from kiwrap.backends.ipc.units import mm_to_nm
from kiwrap.formats.netlist_sync import SyncReport as _FileSyncReport

__all__ = [
    "SyncReport",
    "diff_boards",
    "offboard_grid",
    "parse_import_report",
    "pcb_add_track",
    "pcb_add_via",
    "pcb_add_zone",
    "pcb_delete",
    "pcb_place",
    "pcb_refill",
    "pcb_save",
    "pcb_sync",
    "probe_import_netlist",
]

CAP_IMPORT_NETLIST = "ipc.has_import_netlist"


@dataclass
class SyncReport(_FileSyncReport):
    """The file writer's `SyncReport` (kiwrap.formats.netlist_sync: added / updated / removed /
    footprint_changes / unresolved / pad_net_changes / warnings / placed ...) plus what only the
    IPC path has: KiCad's own log (`report`), its error lines and whether this was a dry run.
    The structured fields are diffed from `read_board` before / after (exact); `unresolved` and
    `errors` come from the log."""

    errors: list[str] = field(default_factory=list)
    report: str = ""
    dry_run: bool = False

    @property
    def ok(self) -> bool:
        return not self.errors and not self.unresolved

    def to_json(self) -> dict[str, Any]:
        out = self.to_dict()
        out.update({"errors": list(self.errors), "report": self.report, "dry_run": self.dry_run})
        return out


# --- pure helpers ------------------------------------------------------------------------------------

_CANNOT_ADD = re.compile(r"^Cannot add (?P<ref>\S+) \((?P<reason>.*)\)\.?$")
_FP_IN_REASON = re.compile(r"footprint '(?P<fpid>[^']*)'")
_TOTAL = re.compile(r"^Total warnings: (\d+), errors: (\d+)\.?$")
_WARN_PREFIXES = ("Warning", "warning")


def parse_import_report(report: str) -> tuple[list[tuple[str, str, str]], list[str], list[str]]:
    """(unresolved [(ref, fpid, reason)], warnings, errors) from KiCad's netlist updater log. The
    log is the only place the API states WHY a component was not instantiated."""
    unresolved: list[tuple[str, str, str]] = []
    warnings: list[str] = []
    errors: list[str] = []
    for raw in report.splitlines():
        line = raw.strip()
        if not line or _TOTAL.match(line) or line.startswith("Processing symbol"):
            continue
        m = _CANNOT_ADD.match(line)
        if m:
            reason = m.group("reason")
            fp = _FP_IN_REASON.search(reason)
            unresolved.append((m.group("ref"), fp.group("fpid") if fp else "", reason))
            errors.append(line)
            continue
        if "not found in any libraries" in line or line.startswith("No footprint defined"):
            errors.append(line)
        elif line.startswith(_WARN_PREFIXES) or "warning" in line.lower():
            warnings.append(line)
    return unresolved, warnings, errors


def diff_boards(before: BoardModel, after: BoardModel) -> SyncReport:
    """Exact structured report from two `read_board` models, keyed on the footprint id (KIID) so
    that a rename by KiCad's uuid-mode updater (`match="path"`: "Changed R1 reference designator
    to R2") is reported in `renamed`, not as an update with phantom pad-net changes: `added` /
    `removed` references, `renamed` {old: new}, footprint id changes, pad net changes per
    footprint, `updated` = refs with any change (fpid, value, pad nets, DNP, BOM flag). Ids
    missing on one side (a model without ids) fall back to reference matching."""
    old_by_id = {f.id: f for f in before.footprints if f.id}
    new_by_id = {f.id: f for f in after.footprints if f.id}
    pairs: list[tuple[FootprintModel, FootprintModel]] = []
    for fid, b in new_by_id.items():
        a = old_by_id.get(fid)
        if a is not None:
            pairs.append((a, b))
    paired_old = {a.id for a, _b in pairs}
    paired_new = {b.id for _a, b in pairs}
    old_rest = {f.ref: f for f in before.footprints if f.id not in paired_old}
    new_rest = {f.ref: f for f in after.footprints if f.id not in paired_new}
    for ref in sorted(set(old_rest) & set(new_rest)):
        pairs.append((old_rest.pop(ref), new_rest.pop(ref)))
    added = sorted(new_rest)
    removed = sorted(old_rest)
    renamed: dict[str, str] = {}
    fp_changes: list[tuple[str, str, str]] = []
    pad_changes: list[tuple[str, str, str, str]] = []
    updated: list[str] = []
    for a, b in sorted(pairs, key=lambda t: _ref_key(t[1].ref)):
        ref = b.ref
        changed = False
        if a.ref != b.ref:
            renamed[a.ref] = b.ref
        if a.fpid != b.fpid:
            fp_changes.append((ref, a.fpid, b.fpid))
            changed = True
        if (a.value, a.dnp, a.exclude_from_bom) != (b.value, b.dnp, b.exclude_from_bom):
            changed = True
        old_pads = {p.number: p.net for p in a.pads}
        for pad in b.pads:
            if pad.number in old_pads and old_pads[pad.number] != pad.net:
                pad_changes.append((ref, pad.number, old_pads[pad.number], pad.net))
                changed = True
        if changed:
            updated.append(ref)
    return SyncReport(
        added=added,
        updated=updated,
        removed=removed,
        footprint_changes=fp_changes,
        pad_net_changes=pad_changes,
        renamed=renamed,
    )


def offboard_grid(
    footprints: Sequence[FootprintModel],
    outline: BBox | None,
    items: BBox | None,
    *,
    gap_mm: float = 2.0,
    occupied: Sequence[BBox] | None = None,
) -> dict[str, Point]:
    """Positions for newly added footprints: rows below the board (outline box, else the box of
    everything, else the origin), left to right, each cell the footprint's own bounding box plus
    `gap_mm`, wrapping at the board width. Deterministic (sorted by reference).

    The first row starts below everything already there: the outline AND the `items` box AND
    every `occupied` box (footprints an earlier sync parked below the outline) — the file writer's
    anchor (`BoardWriter.sync` places below max y of the existing parts). Anchoring on the
    outline alone stacked the parts of a second sync on top of the first sync's parked ones
    (courtyards_overlap / shorting_items noise in the receipt, verified on 10.99)."""
    base = outline or items
    x0 = base.x if base else 0.0
    bottoms = [box.y2 for box in (outline, items, *(occupied or ())) if box is not None]
    y0 = (max(bottoms) + gap_mm) if bottoms else 0.0
    width = max(base.w if base else 0.0, 50.0)
    out: dict[str, Point] = {}
    x = x0
    row_h = 0.0
    for fp in sorted(footprints, key=lambda f: _ref_key(f.ref)):
        box = fp.bbox or fp.courtyard_bbox
        w = (box.w if box else 5.0) + gap_mm
        h = (box.h if box else 5.0) + gap_mm
        if x > x0 and x + w > x0 + width:
            x = x0
            y0 += row_h
            row_h = 0.0
        # position = bbox centre; the footprint origin may be off-centre, correct for it
        off_x = (box.center[0] - fp.position[0]) if box else 0.0
        off_y = (box.center[1] - fp.position[1]) if box else 0.0
        out[fp.ref] = (round(x + w / 2 - off_x, 3), round(y0 + h / 2 - off_y, 3))
        x += w
        row_h = max(row_h, h)
    return out


def _ref_key(ref: str) -> tuple[str, int]:
    m = re.match(r"^(#?[A-Za-z_]+)(\d*)", ref)
    return (m.group(1), int(m.group(2) or 0)) if m else (ref, 0)


# --- session plumbing --------------------------------------------------------------------------------


def _board(source: Any) -> Any:
    return _w.editor_of(source, "pcb")


def _ensure_footprint_libraries(source: Any) -> None:
    if not hasattr(source, "load_libraries") or getattr(source, "_kiwrap_fp_libs_loaded", False):
        return
    with _w.timed("pcb.load_libraries"):
        source.load_libraries(("footprint",))
    with contextlib.suppress(AttributeError):
        source._kiwrap_fp_libs_loaded = True


def _layer(name: str) -> Any:
    return _w._layer_enum(name)


def probe_import_netlist(source: Any) -> bool:
    """Presence probe for `ImportNetlist`: an empty `netlist_path` answers AS_BAD_REQUEST
    ("netlist file '' could not be opened") when the handler exists, AS_UNHANDLED when not.
    Records `ipc.has_import_netlist` through `Capabilities.refine` for a session."""
    from kipy.errors import ApiError
    from kipy.proto.board import board_commands_pb2

    board = _board(source)
    request = board_commands_pb2.ImportNetlist()
    request.board.CopyFrom(board.document)
    request.dry_run = True
    try:
        board.client.send(request, board_commands_pb2.ImportNetlistResponse)
        present = True
    except ApiError as e:
        present = not _w.is_unhandled(e)
    caps = _w.capabilities_of(source)
    refine = getattr(caps, "refine", None)
    if callable(refine):
        with contextlib.suppress(Exception):  # a cache problem never breaks the probe
            refine(CAP_IMPORT_NETLIST, present)
    return present


_PROBED_BARE: dict[int, bool] = {}  # id(client) -> ImportNetlist present (bare editors)


def _can_import_netlist(source: Any) -> bool:
    caps = _w.capabilities_of(source)
    if caps is not None and CAP_IMPORT_NETLIST in (caps.get("kiwrap.ipc_probed", []) or []):
        return bool(caps.has(CAP_IMPORT_NETLIST))
    key = id(_board(source).client)
    if key not in _PROBED_BARE:
        _PROBED_BARE[key] = probe_import_netlist(source)
    return _PROBED_BARE[key]


# --- sync --------------------------------------------------------------------------------------------


def pcb_sync(
    source: Any,
    netlist_path: str | Path,
    *,
    remove_orphans: bool = True,
    keep_positions: bool = True,
    dry_run: bool = False,
    place_new_offboard: bool = True,
    update_footprints: bool = True,
    match: str = "path",
) -> SyncReport:
    """Update the open board from `netlist_path` (a `kicad-cli sch export netlist --format
    kicadsexpr` file) with `ImportNetlist`: footprints are instantiated through the project's
    fp-lib-table (libraries loaded first), paired with symbols by `match` (`"path"` = KiCad's
    default `NMM_UUID`, the file writer's default; `"reference"` = `NMM_REFERENCE`), pad nets
    assigned by pad number, value / DNP / fields propagated, orphans removed when
    `remove_orphans`. Existing footprints keep their
    position, side and rotation (KiCad's updater never moves them; `keep_positions` is accepted
    for the contract and must be True). New footprints are moved off-board onto a grid
    (`offboard_grid`, positions in `SyncReport.placed`) in a second commit unless
    `place_new_offboard=False`. `dry_run=True` only
    returns the log-derived part of the report. Raises `Unsupported(fallback="file")` when the
    build has no `ImportNetlist` handler."""
    from kipy.errors import ApiError
    from kipy.proto.board.board_commands_pb2 import NetlistMatchMode

    if not keep_positions:
        raise ValueError("keep_positions=False is not available over IPC (KiCad's updater keeps positions)")
    if match not in ("path", "reference"):
        raise ValueError(f"match must be 'path' or 'reference', not {match!r}")
    path = Path(netlist_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"netlist {path} does not exist")
    if not _can_import_netlist(source):
        raise _w.unsupported(source, CAP_IMPORT_NETLIST, fallback="file")
    board = _board(source)
    if not dry_run:
        _ensure_footprint_libraries(source)  # timed on its own: ~4-5 s once per session
    with _w.timed("pcb_sync"):
        before = readers.read_board(board, footprint_bboxes=False, include_zone_outlines=False)
        try:
            result = board.import_netlist(
                str(path),
                dry_run=dry_run,
                match_mode=NetlistMatchMode.NMM_REFERENCE
                if match == "reference"
                else NetlistMatchMode.NMM_UUID,
                delete_extra_footprints=remove_orphans,
                update_footprints=update_footprints,
            )
        except ApiError as e:
            if _w.is_unhandled(e):
                raise _w.unsupported(source, CAP_IMPORT_NETLIST, fallback="file") from e
            raise
        unresolved, warnings, errors = parse_import_report(result.report)
        if dry_run:
            return SyncReport(
                unresolved=unresolved, warnings=warnings, errors=errors, report=result.report, dry_run=True
            )
        after = readers.read_board(board, footprint_bboxes=True, include_zone_outlines=False)
        report = diff_boards(before, after)
        placed: dict[str, Point] = {}
        if place_new_offboard and report.added:
            new = [f for f in after.footprints if f.ref in set(report.added)]
            others = [
                f.bbox for f in after.footprints if f.ref not in set(report.added) and f.bbox is not None
            ]
            targets = offboard_grid(
                new, after.outline_bbox, _items_box_without(after, set(report.added)), occupied=others
            )
            instances = {
                fp.reference_field.text.value: fp
                for fp in board.get_footprints()
                if fp.reference_field.text.value in targets
            }
            with _w.commit(board, "kiwrap: place new footprints off-board"):
                for ref, pos in targets.items():
                    inst = instances.get(ref)
                    if inst is None:
                        continue
                    inst.position = _w.vec(pos)
                    board.update_items(inst)
                    placed[ref] = pos
        return SyncReport(
            added=report.added,
            updated=report.updated,
            removed=report.removed,
            footprint_changes=report.footprint_changes,
            unresolved=unresolved,
            pad_net_changes=report.pad_net_changes,
            renamed=report.renamed,
            warnings=warnings,
            placed=placed,
            errors=errors,
            report=result.report,
        )


def _items_box_without(model: BoardModel, refs: set[str]) -> BBox | None:
    boxes = [f.bbox for f in model.footprints if f.ref not in refs and f.bbox is not None]
    return BBox.union(boxes) if boxes else model.items_bbox


# --- placement ---------------------------------------------------------------------------------------


def pcb_place(
    source: Any,
    ref: str,
    at_mm: Point,
    rotation: float | None = None,
    side: str | None = None,
    locked: bool | None = None,
) -> None:
    """Move `ref` to `at_mm` (footprint origin), optionally rotate (degrees), change side
    (`"front"` / `"back"`, via `FlipItems` in place) and lock, in one commit. KeyError when no
    footprint has that reference.

    A CHANGED `rotation` on a build with `ipc.footprint_update_rotation_noise` (10.99.0-4083)
    leaves 1 nm noise in the footprint's library coordinates and DRC then reports
    `lib_footprint_mismatch` (docs/UPSTREAM.md G16); moves, flips and locks are exact. The core
    tool (`kw_pcb_place`) keeps rotating placements off ipc11 on such builds."""
    from kipy.geometry import Angle

    board = _board(source)
    with _w.timed("pcb_place"):
        match = [fp for fp in board.get_footprints() if fp.reference_field.text.value == ref]
        if not match:
            raise KeyError(f"no footprint with reference {ref!r} on the board")
        fp = match[0]
        with _w.commit(board, f"kiwrap: place {ref}"):
            if side is not None:
                if side not in ("front", "back"):
                    raise ValueError("side must be 'front' or 'back'")
                current = "back" if fp.layer == _layer("B.Cu") else "front"
                if current != side:
                    flipped = board.flip_items(fp)
                    if flipped:
                        fp = flipped[0]
            fp.position = _w.vec((float(at_mm[0]), float(at_mm[1])))
            if rotation is not None:
                fp.orientation = Angle.from_degrees(float(rotation))
            if locked is not None:
                fp.locked = bool(locked)
            board.update_items(fp)


# --- copper ------------------------------------------------------------------------------------------


def pcb_add_zone(
    source: Any,
    net: str,
    layers: str | Sequence[str],
    polygon_mm: Sequence[Point],
    priority: int = 0,
    *,
    name: str = "",
    clearance_mm: float | None = None,
    min_thickness_mm: float | None = None,
    thermal_gap_mm: float = 0.5,
    thermal_bridge_mm: float = 0.5,
    pad_connection: str = "thermal",
    keepout: Mapping[str, bool] | None = None,
    refill: bool = True,
    refill_timeout_s: float = 120.0,
) -> str:
    """A copper `Zone` on `layers` with outline `polygon_mm` (>= 3 points) on `net`, filled with
    `RefillZones` when `refill` (blocking; ~0.6 s for one zone on ecc83), or a rule area when
    `keepout` is given (`{tracks, vias, pads, copperpour, footprints: bool}`, the file writer's
    keys). Returns its KIID.

    Pad connection defaults are the file writer's (`BoardWriter.add_zone`): thermal reliefs with
    a 0.5 mm gap and 0.5 mm spokes. A kipy `Zone()` carries 0 for both, and KiCad's fill then
    draws spokes of zero width: `starved_thermal` errors on every pad (verified on 10.99.0-4083
    with a 2-layer GND pour; the same design filled by kicad-cli 10.0.6 from the file writer's
    zone was clean)."""
    from kipy.board_types import Net, Zone
    from kipy.geometry import PolygonWithHoles, PolyLine, PolyLineNode
    from kipy.proto.board.board_types_pb2 import ZoneConnectionStyle as ZCS
    from kipy.proto.board.board_types_pb2 import ZoneType

    if len(polygon_mm) < 3:
        raise ValueError("a zone outline needs at least three points")
    layer_names = [layers] if isinstance(layers, str) else list(layers)
    if not layer_names:
        raise ValueError("a zone needs at least one layer")
    board = _board(source)
    zone = Zone()
    zone.layers = [_layer(n) for n in layer_names]
    if keepout:
        zone.proto.type = ZoneType.ZT_RULE_AREA
        ras = zone.proto.rule_area_settings
        ras.keepout_tracks = bool(keepout.get("tracks"))
        ras.keepout_vias = bool(keepout.get("vias"))
        ras.keepout_pads = bool(keepout.get("pads"))
        ras.keepout_copper = bool(keepout.get("copperpour", keepout.get("copper")))
        ras.keepout_footprints = bool(keepout.get("footprints"))
    if net and not keepout:
        zone.net = Net(name=net)
    outline = PolyLine()
    for x, y in polygon_mm:
        outline.append(PolyLineNode.from_xy(mm_to_nm(x), mm_to_nm(y)))
    outline.closed = True
    poly = PolygonWithHoles()
    poly.outline = outline
    zone.outline = poly
    zone.priority = int(priority)
    if name:
        zone.name = name
    styles = {"thermal": ZCS.ZCS_THERMAL, "solid": ZCS.ZCS_FULL, "full": ZCS.ZCS_FULL, "none": ZCS.ZCS_NONE}
    if pad_connection not in styles:
        raise ValueError(f"pad_connection must be one of {sorted(styles)}, not {pad_connection!r}")
    if not keepout:
        if clearance_mm is not None:
            zone.clearance = mm_to_nm(clearance_mm)
        if min_thickness_mm is not None:
            zone.min_thickness = mm_to_nm(min_thickness_mm)
        # straight on the proto: kipy 0.9's `ZoneConnectionSettings.thermal_spokes` hands out a
        # COPY (Wrapper(proto=...) instead of proto_ref), so its setters never reach the zone
        connection = zone.proto.copper_settings.connection
        connection.zone_connection = styles[pad_connection]
        connection.thermal_spokes.gap.value_nm = mm_to_nm(thermal_gap_mm)
        connection.thermal_spokes.width.value_nm = mm_to_nm(thermal_bridge_mm)
    with _w.timed("pcb_add_zone"):
        with _w.commit(board, f"kiwrap: add zone {net or name}"):
            created = board.create_items(zone)
        zid = created[0].id.value if created else ""
        if refill and zid and not keepout:
            pcb_refill(source, [zid], timeout_s=refill_timeout_s)
    return zid


def pcb_refill(source: Any, zones: Sequence[str] | None = None, *, timeout_s: float = 120.0) -> None:
    """`RefillZones` for `zones` (KIIDs) or every zone; blocks until the server answers Ping."""
    from kipy.proto.common.types.base_types_pb2 import KIID

    board = _board(source)
    with _w.timed("pcb_refill"):
        ids = [KIID(value=z) for z in zones] if zones else None
        board.refill_zones(ids, max_poll_seconds=timeout_s, poll_interval_seconds=0.2)


def pcb_add_track(source: Any, pts: Sequence[Point], width_mm: float, layer: str, net: str) -> list[str]:
    """Tracks along the polyline `pts` (one `Track` per segment) on copper `layer` with `net`, in
    one commit. Non-copper layers are refused here (KiCad silently drops such tracks,
    docs/UPSTREAM.md §1.3). Returns the KIIDs."""
    from kipy.board_types import Net, Track

    if len(pts) < 2:
        raise ValueError("a track needs at least two points")
    if not _is_copper(layer):
        raise ValueError(f"tracks must be on a copper layer, not {layer!r}")
    if width_mm <= 0:
        raise ValueError("width_mm must be positive")
    board = _board(source)
    items = []
    for a, b in zip(pts, pts[1:], strict=False):
        if math.isclose(a[0], b[0], abs_tol=1e-6) and math.isclose(a[1], b[1], abs_tol=1e-6):
            continue
        tr = Track()
        tr.start = _w.vec(a)
        tr.end = _w.vec(b)
        tr.width = mm_to_nm(width_mm)
        tr.layer = _layer(layer)
        if net:
            tr.net = Net(name=net)
        items.append(tr)
    if not items:
        return []
    with _w.timed("pcb_add_track"), _w.commit(board, f"kiwrap: add track {net}"):
        created = board.create_items(items)
    return [c.id.value for c in created]


def pcb_add_via(
    source: Any,
    at_mm: Point,
    size_mm: float,
    drill_mm: float,
    net: str,
    layers: Sequence[str] | None = None,
) -> str:
    """A through via at `at_mm` (`layers` must be None or the outer pair: blind / buried padstacks
    are not written by this twin). Returns its KIID."""
    from kipy.board_types import Net, Via

    if layers and set(layers) != {"F.Cu", "B.Cu"}:
        raise ValueError("only through vias (layers None or ['F.Cu', 'B.Cu']) are supported over IPC")
    if drill_mm >= size_mm:
        raise ValueError("drill_mm must be smaller than size_mm")
    board = _board(source)
    via = Via()
    via.position = _w.vec(at_mm)
    via.diameter = mm_to_nm(size_mm)
    via.drill_diameter = mm_to_nm(drill_mm)
    if net:
        via.net = Net(name=net)
    with _w.timed("pcb_add_via"), _w.commit(board, f"kiwrap: add via {net}"):
        created = board.create_items(via)
    return created[0].id.value if created else ""


def _is_copper(layer: str) -> bool:
    return layer.endswith(".Cu")


# --- delete / save -----------------------------------------------------------------------------------


def pcb_delete(source: Any, uuid: str) -> None:
    """`DeleteItems` of one board item in its own commit (KeyError when KiCad reports the KIID as
    nonexistent; `writes.delete_items` checks the per-item status)."""
    board = _board(source)
    with _w.timed("pcb_delete"), _w.commit(board, f"kiwrap: delete {uuid[:8]}"):
        _w.delete_items(board, [uuid])


def pcb_save(source: Any, path: str | Path | None = None, *, include_project: bool = False) -> Path:
    """`SaveDocument` in place (returns the session's file) or `SaveCopyOfDocument` to `path`."""
    board = _board(source)
    with _w.timed("pcb_save"):
        if path is None:
            board.save()
            file = getattr(source, "file", None)
            return Path(file) if file else Path(board.name)
        return _w.save_copy(board, path, include_project=include_project)
