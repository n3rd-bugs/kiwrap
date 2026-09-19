"""Unit tests (no KiCad) for the IPC authoring twins' pure parts: grid snapping, wire planning
(splits + junctions), annotation numbering, the ImportNetlist log parser, the board diff, the
off-board grid, the G13 guard, the status-checked delete and the lazy re-exports of `writes`."""

from __future__ import annotations

import types
from dataclasses import replace

import pytest

from kiwrap.backends.ipc import pcb_writes, sch_writes, writes
from kiwrap.backends.ipc.models import BBox, BoardModel, FootprintModel, PadModel, SheetInfo, SymbolModel
from kiwrap.capability import Unsupported

# --- grid --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "snapped"),
    [(0.0, 0.0), (1.27, 1.27), (1.3, 1.27), (2.0, 2.54), (96.52, 96.52), (100.0, 100.33), (-1.0, -1.27)],
)
def test_snap_mm(value, snapped):
    assert writes.snap_mm(value) == snapped
    assert writes.on_grid(snapped)


def test_on_grid_rejects_off_grid_values():
    assert not writes.on_grid(100.0)
    assert writes.snap_point((50.0, 76.2)) == (49.53, 76.2)


# --- wire planning -----------------------------------------------------------------------------------


def test_plan_wires_splits_at_a_pin_and_adds_a_junction():
    plan = sch_writes.plan_wires([(0.0, 0.0), (10.16, 0.0)], pins=[(5.08, 0.0)], wires=[], junctions=[])
    assert plan.segments == [((0.0, 0.0), (5.08, 0.0)), ((5.08, 0.0), (10.16, 0.0))]
    assert plan.junctions == [(5.08, 0.0)]  # two wire ends + a pin
    assert plan.split_wires == []


def test_plan_wires_pin_at_the_end_needs_no_junction():
    plan = sch_writes.plan_wires([(0.0, 0.0), (5.08, 0.0)], pins=[(5.08, 0.0)], wires=[], junctions=[])
    assert plan.segments == [((0.0, 0.0), (5.08, 0.0))]
    assert plan.junctions == []


def test_plan_wires_end_on_an_existing_wire_interior_splits_it():
    rail = ("w1", (0.0, 10.0), (20.0, 10.0))
    plan = sch_writes.plan_wires([(7.62, 0.0), (7.62, 10.0)], pins=[], wires=[rail], junctions=[])
    assert plan.segments == [((7.62, 0.0), (7.62, 10.0))]
    assert plan.junctions == [(7.62, 10.0)]
    assert plan.split_wires == [("w1", (0.0, 10.0), (7.62, 10.0), (20.0, 10.0))]


def test_plan_wires_end_on_an_existing_wire_end_connects_without_junction():
    plan = sch_writes.plan_wires(
        [(0.0, 0.0), (0.0, 10.0)], pins=[], wires=[("w1", (0.0, 10.0), (20.0, 10.0))], junctions=[]
    )
    assert plan.junctions == [] and plan.split_wires == []


def test_plan_wires_existing_wire_end_inside_new_segment_splits_the_new_one():
    plan = sch_writes.plan_wires(
        [(0.0, 0.0), (20.0, 0.0)], pins=[], wires=[("w1", (10.0, 0.0), (10.0, 5.0))], junctions=[]
    )
    assert plan.segments == [((0.0, 0.0), (10.0, 0.0)), ((10.0, 0.0), (20.0, 0.0))]
    assert plan.junctions == [(10.0, 0.0)]


def test_plan_wires_bend_and_degenerate_segments():
    plan = sch_writes.plan_wires(
        [(0.0, 0.0), (0.0, 0.0), (5.0, 0.0), (5.0, 5.0)], pins=[], wires=[], junctions=[]
    )
    assert plan.segments == [((0.0, 0.0), (5.0, 0.0)), ((5.0, 0.0), (5.0, 5.0))]
    assert plan.junctions == []


def test_plan_wires_keeps_an_existing_junction():
    plan = sch_writes.plan_wires(
        [(0.0, 0.0), (10.0, 0.0)],
        pins=[(5.0, 0.0)],
        wires=[("w1", (5.0, 0.0), (5.0, 5.0))],
        junctions=[(5.0, 0.0)],
    )
    assert plan.junctions == [] and len(plan.segments) == 2


def test_plan_wires_crossing_without_touching_is_not_a_connection():
    plan = sch_writes.plan_wires(
        [(0.0, 5.0), (10.0, 5.0)], pins=[], wires=[("w1", (5.0, 0.0), (5.0, 10.0))], junctions=[]
    )
    assert plan.segments == [((0.0, 5.0), (10.0, 5.0))]
    assert plan.junctions == [] and plan.split_wires == []


# --- annotation --------------------------------------------------------------------------------------


def _sym(uuid: str, ref: str, pos, *, unit=1, lib_id="Device:R", sheet="/") -> SymbolModel:
    return SymbolModel(
        id=uuid,
        ref=ref,
        value="",
        lib_id=lib_id,
        unit=unit,
        position=pos,
        rotation=0.0,
        mirror_x=False,
        mirror_y=False,
        footprint="",
        dnp=False,
        exclude_from_bom=False,
        exclude_from_board=False,
        sheet_path=sheet,
    )


ROOT = SheetInfo(path="/", name="root", filename="root.kicad_sch", page="1")


def test_plan_annotation_numbers_unannotated_and_duplicates_top_down_left_right():
    symbols = [
        _sym("a", "R?", (50.0, 60.0)),
        _sym("b", "R?", (10.0, 50.0)),
        _sym("c", "R3", (10.0, 10.0)),
        _sym("d", "R3", (20.0, 10.0)),  # duplicate of c: renumbered
        _sym("e", "C7", (0.0, 0.0)),
        _sym("p", "#PWR01", (0.0, 0.0)),
    ]
    plan = sch_writes.plan_annotation(symbols, [ROOT])
    assert plan == {"d": ("R3", "R1"), "b": ("R?", "R2"), "a": ("R?", "R4")}


def test_plan_annotation_reset_and_sheet_start():
    sub = SheetInfo(path="/sub/", name="sub", filename="sub.kicad_sch", page="2")
    symbols = [_sym("a", "R5", (0.0, 0.0)), _sym("b", "R?", (0.0, 0.0), sheet="/sub/")]
    plan = sch_writes.plan_annotation(symbols, [ROOT, sub], scheme="sheet", start_per_sheet=True, reset=True)
    assert plan == {"a": ("R5", "R101"), "b": ("R?", "R201")}
    plan = sch_writes.plan_annotation(symbols, [ROOT, sub], scheme="global")
    assert plan == {"b": ("R?", "R1")}


def test_plan_annotation_groups_units_of_one_multi_unit_symbol():
    symbols = [
        _sym("a", "U?", (0.0, 0.0), unit=1, lib_id="Amplifier_Operational:LM358"),
        _sym("b", "U?", (10.0, 0.0), unit=2, lib_id="Amplifier_Operational:LM358"),
        _sym("c", "U?", (20.0, 0.0), unit=1, lib_id="Amplifier_Operational:LM358"),
    ]
    plan = sch_writes.plan_annotation(symbols, [ROOT])
    assert plan == {"a": ("U?", "U1"), "b": ("U?", "U1"), "c": ("U?", "U2")}


def test_next_reference_skips_used_numbers():
    symbols = [_sym("a", "#PWR01", (0, 0)), _sym("b", "#PWR03", (0, 0)), _sym("c", "R1", (0, 0))]
    assert sch_writes._next_reference("#PWR", symbols) == "#PWR02"
    assert sch_writes._next_reference("#FLG", symbols) == "#FLG01"
    assert sch_writes._next_reference("R", symbols) == "R2"


# --- ImportNetlist log -------------------------------------------------------------------------------

LOG = """Processing symbol 'R1:Resistor_THT:R_Axial'.
Changed R1 footprint from 'Footprints:R_Axial' to 'Resistor_THT:R_Axial'.
Processing symbol 'R100:Nope:Missing'.
Cannot add R100 (footprint 'Nope:Missing' not found).
Processing symbol 'R101:'.
Cannot add R101 (no footprint assigned).
Warning: something odd
Total warnings: 1, errors: 2.
"""


def test_parse_import_report():
    unresolved, warnings, errors = pcb_writes.parse_import_report(LOG)
    assert unresolved == [
        ("R100", "Nope:Missing", "footprint 'Nope:Missing' not found"),
        ("R101", "", "no footprint assigned"),
    ]
    assert warnings == ["Warning: something odd"]
    assert errors == [
        "Cannot add R100 (footprint 'Nope:Missing' not found).",
        "Cannot add R101 (no footprint assigned).",
    ]


# --- board diff and off-board grid -------------------------------------------------------------------


def _pad(number: str, net: str) -> PadModel:
    return PadModel(
        id=f"p{number}",
        number=number,
        net=net,
        type="smd",
        position=(0.0, 0.0),
        shape="rectangle",
        size=(1.0, 1.0),
        rotation=0.0,
        corner_rounding_ratio=0.0,
        drill=None,
        layers=("F.Cu",),
    )


def _fp(
    ref: str, fpid: str, pads: dict[str, str], pos=(0.0, 0.0), bbox: BBox | None = None, fid: str = ""
) -> FootprintModel:
    return FootprintModel(
        id=fid or ref,
        ref=ref,
        value="",
        fpid=fpid,
        position=pos,
        rotation=0.0,
        layer="F.Cu",
        side="front",
        locked=False,
        mounting="smd",
        dnp=False,
        exclude_from_bom=False,
        exclude_from_pos=False,
        sheet_path="/",
        bbox=bbox,
        pads=tuple(_pad(n, net) for n, net in pads.items()),
    )


def _board(*fps: FootprintModel, outline: BBox | None = None) -> BoardModel:
    return BoardModel(
        path="b.kicad_pcb",
        name="b",
        project="b",
        copper_layer_count=2,
        layers=(),
        nets=(),
        footprints=tuple(fps),
        tracks=(),
        vias=(),
        zones=(),
        shapes=(),
        outline_bbox=outline,
        items_bbox=None,
        design_rules=None,
    )


def test_diff_boards():
    before = _board(
        _fp("R1", "A:x", {"1": "N1", "2": "GND"}), _fp("R2", "A:x", {"1": "N1"}), _fp("C9", "A:c", {})
    )
    after = _board(
        _fp("R1", "A:y", {"1": "N1", "2": "VCC"}), _fp("R2", "A:x", {"1": "N1"}), _fp("R3", "A:x", {})
    )
    report = pcb_writes.diff_boards(before, after)
    assert report.added == ["R3"] and report.removed == ["C9"] and report.updated == ["R1"]
    assert report.footprint_changes == [("R1", "A:x", "A:y")]
    assert report.pad_net_changes == [("R1", "2", "GND", "VCC")]
    assert report.renamed == {}


def test_diff_boards_reports_uuid_mode_renames_not_pad_changes():
    """KiCad's uuid-mode updater swapped R1 <-> R2 (ids stay, references move, nets stay):
    the report says `renamed`, no `updated`, no pad-net changes."""
    before = _board(
        _fp("R1", "A:x", {"1": "/A", "2": "GND"}, fid="u1"),
        _fp("R2", "A:x", {"1": "/B", "2": "GND"}, fid="u2"),
    )
    after = _board(
        _fp("R2", "A:x", {"1": "/A", "2": "GND"}, fid="u1"),
        _fp("R1", "A:x", {"1": "/B", "2": "GND"}, fid="u2"),
    )
    report = pcb_writes.diff_boards(before, after)
    assert report.renamed == {"R1": "R2", "R2": "R1"}
    assert report.updated == [] and report.pad_net_changes == [] and not report.added and not report.removed
    # a rename plus a real change on the same footprint is still an update
    after2 = _board(
        _fp("R9", "A:x", {"1": "/Z", "2": "GND"}, fid="u1"), _fp("R2", "A:x", {"1": "/B"}, fid="u2")
    )
    report2 = pcb_writes.diff_boards(before, after2)
    assert report2.renamed == {"R1": "R9"} and report2.updated == ["R9"]
    assert report2.pad_net_changes == [("R9", "1", "/A", "/Z")]
    # models without ids (older readers) pair by reference
    before3 = _board(replace(_fp("R1", "A:x", {"1": "N"}), id=""))
    after3 = _board(replace(_fp("R1", "A:x", {"1": "M"}), id=""))
    assert pcb_writes.diff_boards(before3, after3).pad_net_changes == [("R1", "1", "N", "M")]


def test_offboard_grid_rows_below_the_outline():
    outline = BBox(100.0, 80.0, 60.0, 40.0)
    fps = [
        _fp("R2", "A:x", {}, pos=(0.0, 0.0), bbox=BBox(-1.0, -0.5, 2.0, 1.0)),
        _fp("R1", "A:x", {}, pos=(0.0, 0.0), bbox=BBox(-1.0, -0.5, 2.0, 1.0)),
        _fp("U1", "A:u", {}, pos=(0.0, 0.0), bbox=BBox(-2.0, -1.0, 40.0, 10.0)),  # wraps to a second row
    ]
    grid = pcb_writes.offboard_grid(fps, outline, None, gap_mm=2.0)
    assert list(grid) == ["R1", "R2", "U1"]
    assert grid["R1"] == (102.0, 123.5) and grid["R2"] == (106.0, 123.5)
    assert grid["U1"][1] > grid["R1"][1]  # second row
    for _x, y in grid.values():
        assert y > outline.y2
    # no outline, no items: rows from the origin
    grid0 = pcb_writes.offboard_grid(fps[:1], None, None)
    assert grid0["R2"][1] > 0


def test_offboard_grid_starts_below_footprints_an_earlier_sync_parked():
    """A second sync must not stack its new parts on the cells the first sync used: the first
    row starts below the lowest occupied box, not at the outline's bottom edge."""
    outline = BBox(0.0, 0.0, 30.0, 20.0)
    parked = BBox(1.28, 23.28, 0.9, 0.95)  # C1 parked by sync #2 below the 30x20 outline
    fps = [_fp("R1", "A:x", {}, pos=(0.0, 0.0), bbox=BBox(-0.4, -0.475, 0.8, 0.95))]
    grid = pcb_writes.offboard_grid(fps, outline, None, gap_mm=2.0, occupied=[parked])
    x, y = grid["R1"]
    assert y - 0.475 >= parked.y2 + 2.0 - 1e-9, grid
    # without the occupied box the old behaviour put it on top of C1
    _x0, y0 = pcb_writes.offboard_grid(fps, outline, None, gap_mm=2.0)["R1"]
    assert parked.y < y0 < parked.y2  # the old anchor lands inside C1's box


def test_sync_report_is_the_file_writers_shape_plus_ipc_extras():
    from kiwrap.formats.netlist_sync import SyncReport as FileSyncReport

    report = pcb_writes.SyncReport(
        added=["R1"], errors=["Cannot add R2 (no footprint assigned)."], report="log"
    )
    assert isinstance(report, FileSyncReport)
    assert not report.ok and report.to_json()["errors"] and report.to_json()["added"] == ["R1"]
    assert pcb_writes.SyncReport().ok


# --- G13 guard ---------------------------------------------------------------------------------------


class _Caps:
    def __init__(self, hang: bool):
        self.hang = hang

    def has(self, key: str) -> bool:
        return key == sch_writes.CAP_HANG and self.hang

    def get(self, key, default=None):
        return default


class _Session:
    def __init__(self, hang: bool):
        self.capabilities = _Caps(hang)
        self.install = types.SimpleNamespace(version="10.99.0")

    def schematic(self):  # marks this as a session for editor_of
        return None


def _proto(uuid: str, lib: str, units: int):
    return types.SimpleNamespace(
        id=types.SimpleNamespace(value=uuid),
        definition=types.SimpleNamespace(
            unit_count=units,
            id=types.SimpleNamespace(library_nickname=lib.split(":")[0], entry_name=lib.split(":")[1]),
        ),
    )


def test_g13_guard_allows_one_instance_per_multi_unit_lib_symbol_on_affected_builds():
    sch = types.SimpleNamespace(client=object())
    session = _Session(hang=True)
    u1a, u1b, u2 = _proto("a", "L:ECC83", 3), _proto("b", "L:ECC83", 3), _proto("c", "L:ECC83", 3)
    sch_writes._guard_update(session, sch, [u1a])
    sch_writes._guard_update(session, sch, [u1a])  # the same instance again is fine
    with pytest.raises(Unsupported) as exc:
        sch_writes._guard_update(session, sch, [u1b])
    assert exc.value.fallback == "file" and "10.99.0" in str(exc.value)
    with pytest.raises(Unsupported):
        sch_writes._guard_update(session, sch, [u1a, u2])  # two instances in one request
    # single-unit and power symbols are never guarded
    for _ in range(3):
        sch_writes._guard_update(session, sch, [_proto("r", "Device:R", 1), _proto("s", "Device:R", 1)])


def test_g13_guard_is_inert_when_the_build_has_no_hang():
    sch = types.SimpleNamespace(client=object())
    session = _Session(hang=False)
    for uuid in ("a", "b", "c"):
        sch_writes._guard_update(session, sch, [_proto(uuid, "L:ECC83", 3)])


def test_g13_guard_assumes_the_worst_for_a_bare_editor():
    sch = types.SimpleNamespace(client=object())
    sch_writes._guard_update(sch, sch, [_proto("a", "L:ECC83", 3)])
    with pytest.raises(Unsupported):
        sch_writes._guard_update(sch, sch, [_proto("b", "L:ECC83", 3)])


# --- delete with status check ------------------------------------------------------------------------


def test_delete_items_checks_per_item_status():
    from kipy.proto.common.commands import editor_commands_pb2 as ec

    class _Client:
        def __init__(self, statuses):
            self.statuses = statuses

        def send(self, command, response_type):
            response = ec.DeleteItemsResponse()
            for kiid in command.item_ids:
                r = response.deleted_items.add()
                r.id.CopyFrom(kiid)
                r.status = self.statuses.get(kiid.value, ec.ItemDeletionStatus.IDS_NONEXISTENT)
            return response

    from kipy.proto.common.types import DocumentSpecifier

    editor = types.SimpleNamespace(
        document=DocumentSpecifier(), client=_Client({"ok": ec.ItemDeletionStatus.IDS_OK})
    )
    assert writes.delete_items(editor, ["ok"]) == ["ok"]
    with pytest.raises(KeyError, match="gone"):
        writes.delete_items(editor, ["ok", "gone"])
    editor.client = _Client({"x": ec.ItemDeletionStatus.IDS_IMMUTABLE})
    with pytest.raises(ValueError, match="immutable"):
        writes.delete_items(editor, ["x"])


# --- module plumbing ---------------------------------------------------------------------------------


def test_writes_reexports_the_twins_lazily():
    assert writes.sch_add_symbol is sch_writes.sch_add_symbol
    assert writes.pcb_sync is pcb_writes.pcb_sync
    assert writes.SyncReport is pcb_writes.SyncReport
    assert "sch_add_wire" in writes.__all__ and "pcb_add_zone" in writes.__all__
    with pytest.raises(AttributeError):
        _ = writes.no_such_twin


def test_latency_recorder_summary():
    rec = writes.LatencyRecorder()
    for ms in (10.0, 30.0, 20.0):
        rec.record("op", ms)
    assert rec.summary() == {"op": {"n": 3.0, "median_ms": 20.0, "max_ms": 30.0}}
    rec.reset()
    assert rec.summary() == {}


def test_unsupported_for_a_bare_editor_names_the_fallback():
    err = writes.unsupported(object(), "ipc.x", fallback="file")
    assert isinstance(err, Unsupported) and err.fallback == "file" and "unknown" in str(err)


def test_editor_of_accepts_sessions_and_editors():
    editor = types.SimpleNamespace(get_symbols=lambda: [])
    assert writes.editor_of(editor, "schematic") is editor
    session = types.SimpleNamespace(schematic=lambda: editor)
    assert writes.editor_of(session, "schematic") is editor
    board = types.SimpleNamespace(get_footprints=lambda: [])
    assert writes.editor_of(types.SimpleNamespace(board=lambda: board), "pcb") is board


def test_sheet_creation_is_refused_without_a_server():
    with pytest.raises(Unsupported) as exc:
        sch_writes.sch_add_sheet(_Session(True), "sub", "sub.kicad_sch", (0, 0), (10, 10))
    assert exc.value.fallback == "file"
