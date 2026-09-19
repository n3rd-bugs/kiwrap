"""Unit tests for kiwrap.backends.ipc that need no KiCad: units, models, the symbol transform,
the command inventory rule, the reader over a fake kipy Board/Schematic, the ratsnest
approximation over a fake connectivity graph, capability gating and the session's socket rule.

The fakes build real protobuf messages (kicad-python's `*_pb2` classes) so the readers exercise the
same field paths as against a live server; only the transport is replaced.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from kiwrap.backends.ipc import handlers, kipy_proto_dir, models, queries, readers, sessions, units
from kiwrap.capability import Capabilities, Unsupported
from kiwrap.installation import KiCadInstallation

kipy = pytest.importorskip("kipy")


def _install(version: str = "10.0.6") -> KiCadInstallation:
    root = Path("C:/fake/KiCad") / version
    return KiCadInstallation(
        root=root,
        kicad_cli=root / "bin" / "kicad-cli.exe",
        share=root / "share",
        version=version,
        source="fake",
    )


# --- packaging -----------------------------------------------------------------------------------


def test_kipy_proto_dir_is_importable():
    proto = kipy_proto_dir()
    assert proto is not None and Path(proto).is_dir()
    from kipy import KiCad  # noqa: F401 - the whole point of the .pth / ensure_kipy_importable
    from kipy.proto.common.types import base_types_pb2

    assert base_types_pb2.Vector2(x_nm=1).x_nm == 1


# --- units ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "nm,mm", [(141605000, 141.605), (99695000, 99.695), (1, 0.000001), (-2540000, -2.54), (0, 0.0)]
)
def test_nm_to_mm_exact(nm, mm):
    assert units.nm_to_mm(nm) == mm
    assert units.nm_to_mm_decimal(nm) == Decimal(str(mm))


@pytest.mark.parametrize(
    "mm,nm",
    [
        (141.605, 141605000),
        (0.1, 100000),
        (2.54, 2540000),
        ("0.0005", 500),
        (Decimal("1.2345678"), 1234568),
        (3, 3000000),
    ],
)
def test_mm_to_nm_rounds_once(mm, nm):
    assert units.mm_to_nm(mm) == nm


def test_round_trip_is_identity_for_every_nm_grid_value():
    for nm in (0, 1, 999999, 1000000, 123456789, 2**40):
        assert units.mm_to_nm(units.nm_to_mm(nm)) == nm


# --- models ----------------------------------------------------------------------------------------


def test_bbox_geometry_and_json():
    a = models.BBox(0, 0, 10, 5)
    b = models.BBox(9, 4, 10, 10)
    assert a.intersects(b) and not a.intersects(models.BBox(20, 20, 1, 1))
    assert a.contains((10, 5)) and not a.contains((10.1, 5))
    assert models.BBox.union([a, b]) == models.BBox(0, 0, 19, 14)
    assert models.BBox.union([]) is None
    track = models.TrackModel(
        id="t", kind="track", start=(0, 0), end=(3, 4), width=0.2, layer="F.Cu", net="N"
    )
    assert track.length == 5.0
    assert json.loads(json.dumps(track.to_json()))["start"] == [0, 0]


# --- symbol transform ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rot,mx,my,point,expected",
    [
        (0, False, False, (10, 20), (10, 20)),
        (0, False, True, (10, 20), (-10, 20)),  # mirror about the Y axis flips x
        (0, True, False, (10, 20), (10, -20)),
        (180, False, False, (10, 20), (-10, -20)),
        (90, False, False, (10, 0), (0, -10)),  # 90 CCW on screen (y down): +x -> -y
        (270, False, False, (10, 0), (0, 10)),
    ],
)
def test_symbol_transform(rot, mx, my, point, expected):
    m = readers.symbol_transform(rot, mx, my)
    assert readers.transform_point(m, *point) == expected


def test_symbol_transform_180_is_two_quarter_turns():
    once = readers.symbol_transform(90, False, False)
    twice = readers._compose(once, readers._ROT_CCW)
    assert twice == readers.symbol_transform(180, False, False)


# --- layer names ---------------------------------------------------------------------------------


def test_layer_names():
    from kipy.proto.board.board_types_pb2 import BoardLayer as BL

    assert readers.layer_name(BL.BL_F_Cu) == "F.Cu"
    assert readers.layer_name(BL.BL_B_Cu) == "B.Cu"
    assert readers.layer_name(BL.BL_In1_Cu) == "In1.Cu"
    assert readers.layer_name(BL.BL_Edge_Cuts) == "Edge.Cuts"
    assert readers.layer_name(BL.BL_F_SilkS) == "F.SilkS"
    assert readers.layer_name(BL.BL_Margin) == "Margin"
    assert readers.layer_name(999999) == "layer_999999"


# --- command inventory ---------------------------------------------------------------------------


def test_command_inventory_rule():
    commands = handlers.command_messages()
    assert 120 <= len(commands) <= 200
    assert {
        "Ping",
        "GetVersion",
        "OpenDocument",
        "GetItems",
        "GetSchematicNetlist",
        "PlaceFootprintFromLibrary",
    } <= set(commands)
    assert (
        "GetItemsResponse" not in commands
        and "ItemHeader" not in commands
        and "DocumentSpecifier" not in commands
    )
    assert commands["SaveDocument"].stateful and commands["CloseAllDocuments"].stateful
    assert not commands["GetNets"].stateful and not commands["HitTest"].stateful
    assert commands["GetNets"].proto_file.endswith("board_commands.proto")


def test_minimal_instance_targets_the_open_document():
    from kipy.proto.common.types.base_types_pb2 import DocumentSpecifier, DocumentType

    doc = DocumentSpecifier()
    doc.type = DocumentType.DOCTYPE_PCB
    doc.board_filename = "x.kicad_pcb"
    commands = handlers.command_messages()
    nets = handlers._minimal_instance(commands["GetNets"], doc)
    assert nets.board.board_filename == "x.kicad_pcb"
    items = handlers._minimal_instance(commands["GetItems"], doc)
    assert items.header.document.board_filename == "x.kicad_pcb"
    ping = handlers._minimal_instance(commands["Ping"], doc)
    assert ping.ByteSize() == 0


def test_snapshot_compare_reports_drift():
    commands = handlers.command_messages()
    snap = handlers.Snapshot(version="10.99.0", client="test", commands=commands)
    snap.results["GetNets"] = {"pcb": handlers.ProbeResult("GetNets", "pcb", "handled", "AS_OK")}
    previous = json.loads(json.dumps(snap.to_json()))
    assert handlers.compare(previous, snap) == []
    snap.results["GetNets"] = {"pcb": handlers.ProbeResult("GetNets", "pcb", "unhandled", "AS_UNHANDLED")}
    assert handlers.compare(previous, snap) == ["GetNets [pcb]: handled -> unhandled"]
    previous["commands"]["Gone"] = {"results": {}}
    assert "command gone: Gone" in handlers.compare(previous, snap)


# --- fake board / schematic for the readers ---------------------------------------------------------


class _Vec:
    def __init__(self, x: int, y: int):
        self.x, self.y = x, y


class _Box:
    def __init__(self, x: int, y: int, w: int, h: int):
        self.pos, self.size = _Vec(x, y), _Vec(w, h)


class _Wrapped:
    """Minimal stand-in for a kipy wrapper: `.proto` + a few attributes the readers touch."""

    def __init__(self, proto, name: str):
        self.proto = proto
        self._name = name

    @property
    def id(self):
        return self.proto.id


class _Pad(_Wrapped):
    pass


class _FootprintDef:
    def __init__(self, pads):
        self.pads = pads


class _Footprint(_Wrapped):
    def __init__(self, proto):
        super().__init__(proto, "FootprintInstance")
        self.definition = _FootprintDef([])


def _fake_board():
    from google.protobuf.any_pb2 import Any as AnyMsg
    from kipy.proto.board import board_types_pb2 as bt
    from kipy.proto.common.types.base_types_pb2 import DocumentSpecifier, DocumentType

    def vec(v, x, y):
        v.x_nm, v.y_nm = x, y

    fp = bt.FootprintInstance()
    fp.id.value = "fp-c1"
    vec(fp.position, 141605000, 99695000)
    fp.orientation.value_degrees = 90
    fp.layer = bt.BoardLayer.BL_F_Cu
    fp.reference_field.text.text.text = "C1"
    fp.value_field.text.text.text = "10uF"
    fp.definition.id.library_nickname = "Footprints"
    fp.definition.id.entry_name = "CP_Radial"
    fp.attributes.mounting_style = bt.FootprintMountingStyle.FMS_THROUGH_HOLE
    for number, net, x in (("1", "Net-(P3-P1)", 141605000), ("2", "GND", 146605000)):
        pad = bt.Pad()
        pad.id.value = f"pad-{number}"
        pad.number = number
        pad.net.name = net
        pad.type = bt.PadType.PT_PTH
        vec(pad.position, x, 99695000)
        layer = pad.pad_stack.copper_layers.add()
        layer.layer = bt.BoardLayer.BL_F_Cu
        layer.shape = bt.PadStackShape.PSS_CIRCLE
        vec(layer.size, 2000000, 2000000)
        vec(pad.pad_stack.drill.diameter, 1000000, 1000000)
        pad.pad_stack.layers.extend([bt.BoardLayer.BL_F_Cu, bt.BoardLayer.BL_B_Cu])
        item = AnyMsg()
        item.Pack(pad)
        fp.definition.items.append(item)
    # a courtyard rectangle on F.CrtYd (absolute board coordinates, as the API serialises it)
    crt = bt.BoardGraphicShape()
    crt.id.value = "c1-crtyd"
    crt.layer = bt.BoardLayer.BL_F_CrtYd
    vec(crt.shape.rectangle.top_left, 136830000, 92420000)
    vec(crt.shape.rectangle.bottom_right, 146380000, 101970000)
    item = AnyMsg()
    item.Pack(crt)
    fp.definition.items.append(item)

    fp2 = bt.FootprintInstance()
    fp2.id.value = "fp-r1"
    vec(fp2.position, 50000000, 50000000)
    fp2.layer = bt.BoardLayer.BL_B_Cu
    fp2.reference_field.text.text.text = "R1"
    fp2.definition.id.entry_name = "R_0603"

    track = bt.Track()
    track.id.value = "trk"
    vec(track.start, 139573000, 99695000)
    vec(track.end, 141605000, 99695000)
    track.width.value_nm = 800000
    track.layer = bt.BoardLayer.BL_B_Cu
    track.net.name = "Net-(P3-P1)"

    arc = bt.Arc()
    arc.id.value = "arc"
    vec(arc.start, 0, 0)
    vec(arc.mid, 1000000, 1000000)
    vec(arc.end, 2000000, 0)
    arc.width.value_nm = 250000
    arc.layer = bt.BoardLayer.BL_F_Cu
    arc.net.name = "GND"

    via = bt.Via()
    via.id.value = "via"
    vec(via.position, 10000000, 10000000)
    via.type = bt.ViaType.VT_THROUGH
    via.net.name = "GND"
    vl = via.pad_stack.copper_layers.add()
    vec(vl.size, 800000, 800000)
    vec(via.pad_stack.drill.diameter, 400000, 400000)
    via.pad_stack.layers.extend([bt.BoardLayer.BL_F_Cu, bt.BoardLayer.BL_B_Cu])

    zone = bt.Zone()
    zone.id.value = "zone"
    zone.type = bt.ZoneType.ZT_COPPER
    zone.layers.append(bt.BoardLayer.BL_B_Cu)
    zone.copper_settings.net.name = "GND"
    zone.filled = True
    poly = zone.outline.polygons.add()
    for x, y in ((0, 0), (100000000, 0), (100000000, 50000000), (0, 50000000)):
        node = poly.outline.nodes.add()
        vec(node.point, x, y)
    poly.outline.closed = True

    edge = bt.BoardGraphicShape()
    edge.id.value = "edge"
    edge.layer = bt.BoardLayer.BL_Edge_Cuts
    vec(edge.shape.segment.start, 0, 0)
    vec(edge.shape.segment.end, 100000000, 0)
    edge.shape.attributes.stroke.width.value_nm = 100000

    doc = DocumentSpecifier()
    doc.type = DocumentType.DOCTYPE_PCB
    doc.board_filename = "fake.kicad_pcb"
    doc.project.name = "fake"

    class Net:
        def __init__(self, name):
            self.name = name

    class Board:
        name = "fake.kicad_pcb"
        document = doc
        client = None
        requests: list[str] = []

        def get_enabled_layers(self):
            return [bt.BoardLayer.BL_F_Cu, bt.BoardLayer.BL_B_Cu, bt.BoardLayer.BL_Edge_Cuts]

        def get_layer_name(self, layer):
            return {bt.BoardLayer.BL_F_Cu: "top"}.get(layer, readers.layer_name(layer))

        def get_copper_layer_count(self):
            return 2

        def get_nets(self):
            return [Net(""), Net("GND"), Net("Net-(P3-P1)")]

        def get_netclass_for_nets(self, nets):
            raise RuntimeError("not in the fake")

        def get_footprints(self):
            return [_Footprint(fp), _Footprint(fp2)]

        def get_item_bounding_box(self, items, include_text=False):
            self.requests.append("GetBoundingBox")
            if include_text:
                return [_Box(136330000, 91920000, 10550000, 10814646), None]
            return [_Box(136830000, 92420000, 9550000, 9550000), None]

        def get_tracks(self):
            return [_Wrapped(track, "Track"), _Wrapped(arc, "ArcTrack")]

        def get_vias(self):
            return [_Wrapped(via, "Via")]

        def get_zones(self):
            return [_Wrapped(zone, "Zone")]

        def get_shapes(self):
            return [_Wrapped(edge, "BoardSegment")]

    return Board()


def test_read_board_from_fake(monkeypatch):
    monkeypatch.setattr(readers, "_design_rules", lambda board: None)
    board = _fake_board()
    model = readers.read_board(board)
    assert model.name == "fake.kicad_pcb" and model.project == "fake" and model.copper_layer_count == 2
    assert [layer.name for layer in model.layers] == ["F.Cu", "B.Cu", "Edge.Cuts"]
    assert model.layers[0].user_name == "top" and model.layers[0].copper and not model.layers[2].copper
    assert [n.name for n in model.nets] == ["GND", "Net-(P3-P1)"]
    c1 = model.footprint("C1")
    assert c1 is not None
    assert (
        c1.position == (141.605, 99.695) and c1.rotation == 90.0 and c1.layer == "F.Cu" and c1.side == "front"
    )
    assert c1.fpid == "Footprints:CP_Radial" and c1.mounting == "through_hole"
    assert c1.bbox == models.BBox(136.83, 92.42, 9.55, 9.55)  # item only: no reference/value text
    assert [(p.number, p.net, p.type, p.shape, p.size, p.drill) for p in c1.pads] == [
        ("1", "Net-(P3-P1)", "pth", "circle", (2.0, 2.0), (1.0, 1.0)),
        ("2", "GND", "pth", "circle", (2.0, 2.0), (1.0, 1.0)),
    ]
    assert c1.pads[0].layers == ("F.Cu", "B.Cu")
    r1 = model.footprint("R1")
    assert r1 is not None and r1.side == "back" and r1.fpid == "R_0603" and r1.bbox is None
    assert [(t.kind, t.width, t.layer, t.net) for t in model.tracks] == [
        ("track", 0.8, "B.Cu", "Net-(P3-P1)"),
        ("arc", 0.25, "F.Cu", "GND"),
    ]
    assert model.tracks[1].mid == (1.0, 1.0)
    assert model.vias[0].diameter == 0.8 and model.vias[0].drill == 0.4 and model.vias[0].type == "through"
    assert model.zones[0].net == "GND" and model.zones[0].layers == ("B.Cu",) and model.zones[0].filled
    assert model.zones[0].outline[0][2] == (100.0, 50.0)
    assert (
        model.shapes[0].kind == "segment"
        and model.shapes[0].layer == "Edge.Cuts"
        and model.shapes[0].width == 0.1
    )
    assert model.outline_bbox == models.BBox(0.0, 0.0, 100.0, 0.0)
    assert model.design_rules is None
    assert board.requests.count("GetBoundingBox") == 2  # batched (item only + with text), not per footprint
    assert c1.bbox_with_text == models.BBox(136.33, 91.92, 10.55, 10.814646)
    assert c1.courtyard_bbox is not None and c1.courtyard_bbox.to_json() == {
        "x": 136.83,
        "y": 92.42,
        "w": pytest.approx(9.55),
        "h": pytest.approx(9.55),
    }  # from the F.CrtYd shapes
    json.dumps(model.to_json())  # JSON-serialisable


def test_read_board_filters(monkeypatch):
    monkeypatch.setattr(readers, "_design_rules", lambda board: None)
    board = _fake_board()
    by_ref = readers.read_board(board, refs=["R1"])
    assert [f.ref for f in by_ref.footprints] == ["R1"] and by_ref.filters == {"refs": ["R1"]}
    by_net = readers.read_board(board, nets=["GND"])
    assert [f.ref for f in by_net.footprints] == ["C1"]
    assert [t.id for t in by_net.tracks] == ["arc"] and [v.id for v in by_net.vias] == ["via"]
    by_box = readers.read_board(board, bbox=(130, 90, 20, 20))
    assert [f.ref for f in by_box.footprints] == ["C1"]
    assert [t.id for t in by_box.tracks] == ["trk"] and by_box.vias == () and by_box.zones == ()
    assert by_box.filters["bbox"] == {"x": 130, "y": 90, "w": 20, "h": 20}


def _fake_schematic():
    from google.protobuf.any_pb2 import Any as AnyMsg
    from kipy.proto.common.types.base_types_pb2 import DocumentSpecifier, DocumentType, ElectricalPinType
    from kipy.proto.schematic import schematic_types_pb2 as st

    def vec(v, x, y):
        v.x_nm, v.y_nm = x, y

    sym = st.SchematicSymbolInstance()
    sym.id.value = "sym-c1"
    sym.path.path_human_readable = "/"
    vec(sym.position, 86360000, 57150000)
    sym.transform.orientation = st.SchematicSymbolOrientation.SSO_0
    sym.transform.mirror_y = True
    sym.reference_field.text.text = "C1"
    sym.value_field.text.text = "10uF"
    sym.footprint_field.text.text = "Capacitor_THT:CP"
    sym.definition.id.library_nickname = "ecc83-pp"
    sym.definition.id.entry_name = "CP"
    sym.unit.unit = 1
    for pid, number, y, unit in (
        ("pin-1", "1", -3810000, 1),
        ("pin-2", "2", 3810000, 1),
        ("pin-x", "9", 0, 2),
    ):
        pin = st.SchematicPin()
        pin.id.value = pid
        pin.number = number
        pin.name = "~"
        vec(pin.position, 0, y)
        pin.electrical_type = ElectricalPinType.EPT_PASSIVE
        child = sym.definition.items.add()
        child.item.Pack(pin)
        child.unit.unit = unit
    wire = st.SchematicLine()
    wire.id.value = "wire-1"
    vec(wire.start, 86360000, 53340000)
    vec(wire.end, 86360000, 50800000)
    wire.type = st.SchematicLineType.SLT_WIRE
    junction = st.Junction()
    junction.id.value = "j-1"
    vec(junction.position, 86360000, 50800000)
    label = st.GlobalLabel()
    label.id.value = "lbl-1"
    label.text.text = "VCC"
    vec(label.position, 1000000, 2000000)
    label.shape = st.SchematicLabelShape.SLSH_INPUT
    nc = st.NoConnectMarker()
    nc.id.value = "nc-1"
    vec(nc.position, 5000000, 5000000)

    # A shared sub-sheet placed twice (`/A/` and `/B/`): the SAME item KIIDs, different per-instance
    # references and nets - exactly what the multichannel / complex_hierarchy demos do.
    ROOT, SUB_A, SUB_B = "root-kiid", "sheet-a-kiid", "sheet-b-kiid"
    sub_sym = st.SchematicSymbolInstance()
    sub_sym.id.value = "sym-shared"
    vec(sub_sym.position, 10000000, 10000000)
    sub_sym.transform.orientation = st.SchematicSymbolOrientation.SSO_0
    sub_sym.definition.id.library_nickname = "Device"
    sub_sym.definition.id.entry_name = "R"
    sub_sym.unit.unit = 1
    pin = st.SchematicPin()
    pin.id.value = "pin-shared"
    pin.number = "1"
    pin.name = "~"
    vec(pin.position, 0, 2540000)
    pin.electrical_type = ElectricalPinType.EPT_PASSIVE
    child = sub_sym.definition.items.add()
    child.item.Pack(pin)
    sub_wire = st.SchematicLine()
    sub_wire.id.value = "wire-shared"
    vec(sub_wire.start, 10000000, 12540000)
    vec(sub_wire.end, 20000000, 12540000)
    sub_wire.type = st.SchematicLineType.SLT_WIRE

    net = st.SchematicNet()
    net.name = "GND"
    sheet = net.sheets.add()
    sheet.path.path.add().value = ROOT
    sheet.path.path_human_readable = "/"  # the netlist's human path is empty on real builds
    for kid in ("pin-2", "wire-1", "j-1"):
        sheet.items.add().value = kid
    net2 = st.SchematicNet()
    net2.name = "Net-(P3-P1)"
    s2 = net2.sheets.add()
    s2.path.path.add().value = ROOT
    s2.items.add().value = "pin-1"
    nets_shared = []
    for sub_kiid, name in ((SUB_A, "Net-(R1-Pad1)"), (SUB_B, "Net-(R2-Pad1)")):
        n = st.SchematicNet()
        n.name = name
        sh = n.sheets.add()
        sh.path.path.add().value = ROOT
        sh.path.path.add().value = sub_kiid
        sh.items.add().value = "pin-shared"
        sh.items.add().value = "wire-shared"
        nets_shared.append(n)

    root = st.SheetInstance()
    root.path.path.add().value = ROOT
    root.path.path_human_readable = "/"
    root.name = "root"
    root.filename = "fake.kicad_sch"
    root.page_number = "1"
    for sub_kiid, name, page in ((SUB_A, "A", "2"), (SUB_B, "B", "3")):
        c = root.children.add()
        c.path.path.add().value = ROOT
        c.path.path.add().value = sub_kiid
        c.name = name  # path_human_readable left empty: the reader builds `/A/` from the names
        c.filename = "shared.kicad_sch"
        c.page_number = page

    doc = DocumentSpecifier()
    doc.type = DocumentType.DOCTYPE_SCHEMATIC
    doc.project.name = "fake"

    class Hierarchy:
        def __init__(self):
            self.root = _Wrapped(root, "SheetInstance")

    class GlobalLabel(_Wrapped):
        pass

    class Schematic:
        name = "fake.kicad_sch"
        document = doc

        def __init__(self, sheet_key: str | None = None):
            self.sheet_key = sheet_key

        def get_hierarchy(self):
            return Hierarchy()

        def get_netlist(self):
            return [_Wrapped(n, "SchematicNet") for n in (net, net2, *nets_shared)]

        def for_sheet(self, sheet_path):
            return Schematic("/".join(k.value for k in sheet_path.proto.path))

        def get_items(self, types=None, sheet_path=None):
            if self.sheet_key in (None, ROOT):
                return [
                    _Wrapped(sym, "SchematicSymbolInstance"),
                    _Wrapped(wire, "SchematicLine"),
                    GlobalLabel(label, "GlobalLabel"),
                    _Wrapped(junction, "Junction"),
                    _Wrapped(nc, "NoConnectMarker"),
                ]
            # the shared sheet: same protos for both instances, per-instance reference
            inst = st.SchematicSymbolInstance()
            inst.CopyFrom(sub_sym)
            inst.path.path.add().value = ROOT
            inst.path.path.add().value = self.sheet_key.split("/")[-1]
            inst.reference_field.text.text = "R1" if self.sheet_key.endswith(SUB_A) else "R2"
            return [_Wrapped(inst, "SchematicSymbolInstance"), _Wrapped(sub_wire, "SchematicLine")]

    del AnyMsg
    return Schematic()


def test_read_schematic_from_fake():
    model = readers.read_schematic(_fake_schematic())
    assert model.name == "fake.kicad_sch" and model.path == "fake.kicad_sch" and model.project == "fake"
    assert model.hierarchy is not None and model.hierarchy.name == "root" and model.hierarchy.page == "1"
    c1 = model.symbol("C1")
    assert c1 is not None
    assert c1.position == (86.36, 57.15) and c1.rotation == 0 and c1.mirror_y and not c1.mirror_x
    assert c1.lib_id == "ecc83-pp:CP" and c1.unit == 1 and c1.footprint == "Capacitor_THT:CP"
    # unit-2 pin filtered out; positions transformed with the API's Y-down convention
    assert [(p.number, p.position, p.net, p.electrical_type) for p in c1.pins] == [
        ("1", (86.36, 53.34), "Net-(P3-P1)", "passive"),
        ("2", (86.36, 60.96), "GND", "passive"),
    ]
    assert model.wires[0].kind == "wire" and model.wires[0].sheet_path == "/" and model.wires[0].net == "GND"
    assert model.junctions[0].net == "GND" and model.labels[0].net == ""
    # the shared sheet placed twice: one symbol / wire per INSTANCE, keyed by (sheet, KIID)
    assert [(s.ref, s.sheet_path, s.pins[0].net) for s in model.symbols[1:]] == [
        ("R1", "/A/", "Net-(R1-Pad1)"),
        ("R2", "/B/", "Net-(R2-Pad1)"),
    ]
    assert [(w.sheet_path, w.net) for w in model.wires[1:]] == [
        ("/A/", "Net-(R1-Pad1)"),
        ("/B/", "Net-(R2-Pad1)"),
    ]
    assert [c.path for c in model.hierarchy.children] == ["/A/", "/B/"]
    assert model.hierarchy.children[0].kiid_path == "root-kiid/sheet-a-kiid"
    r1 = next(n for n in model.netlist if n.name == "Net-(R1-Pad1)")
    assert [(i.kind, i.ref, i.pin, i.sheet_path) for i in r1.items] == [
        ("pin", "R1", "1", "/A/"),
        ("wire", "", "", "/A/"),
    ]
    r2 = next(n for n in model.netlist if n.name == "Net-(R2-Pad1)")
    assert [(i.ref, i.sheet_path) for i in r2.pins] == [("R2", "/B/")]
    assert (
        model.labels[0].kind == "global"
        and model.labels[0].text == "VCC"
        and model.labels[0].shape == "input"
    )
    assert model.junctions[0].position == (86.36, 50.8) and model.no_connects[0].position == (5.0, 5.0)
    gnd = next(n for n in model.netlist if n.name == "GND")
    assert [(i.kind, i.ref, i.pin) for i in gnd.items] == [
        ("pin", "C1", "2"),
        ("wire", "", ""),
        ("junction", "", ""),
    ]
    assert [i.pin for i in gnd.pins] == ["2"]
    json.dumps(model.to_json())


def test_read_schematic_filters():
    sch = _fake_schematic()
    by_net = readers.read_schematic(sch, nets=["GND"])
    assert [s.ref for s in by_net.symbols] == ["C1"] and [n.name for n in by_net.netlist] == ["GND"]
    assert [w.id for w in by_net.wires] == ["wire-1"] and by_net.labels == ()  # label not on GND
    none = readers.read_schematic(sch, refs=["R99"])
    assert none.symbols == () and none.netlist == ()
    by_box = readers.read_schematic(sch, bbox=(0, 0, 9, 9))
    assert (
        by_box.symbols == () and [lbl.id for lbl in by_box.labels] == ["lbl-1"] and by_box.no_connects != ()
    )
    only_b = readers.read_schematic(sch, refs=["R2"])
    assert [(s.ref, s.sheet_path) for s in only_b.symbols] == [("R2", "/B/")]
    assert [n.name for n in only_b.netlist] == ["Net-(R2-Pad1)"]


# --- ratsnest approximation over a fake connectivity graph -------------------------------------------


def _fake_connectivity_board():
    from kipy.proto.board import board_types_pb2 as bt

    class Net:
        def __init__(self, name):
            self.name = name

    class Pad(_Wrapped):
        pass

    pads = {}
    for pid, x in (("a", 0), ("b", 10_000_000), ("c", 50_000_000)):
        p = bt.Pad()
        p.id.value = pid
        p.number = pid
        p.net.name = "N"
        p.position.x_nm = x
        pads[pid] = Pad(p, "Pad")
    clusters = {"a": ["a", "b"], "b": ["a", "b"], "c": ["c"]}

    class Board:
        def get_nets(self):
            return [Net(""), Net("N"), Net("unconnected-(X-Pad1)")]

        def get_footprints(self):
            return []

        def get_items_by_net(self, nets, types=None):
            return list(pads.values())

        def get_connected_items(self, item, types=None):
            return [pads[i] for i in clusters[item.proto.id.value]]

    return Board()


def test_ratsnest_clusters_and_airwires():
    rn = queries.ratsnest(_fake_connectivity_board())
    assert [n.net for n in rn.nets] == ["N"]
    n = rn.nets[0]
    assert n.unrouted == 1 and len(n.clusters) == 2 and rn.unrouted == 1
    assert len(n.airwires) == 1
    aw = n.airwires[0]
    assert {aw.from_item, aw.to_item} == {"b", "c"} and aw.length == 40.0  # nearest pads across clusters
    assert json.loads(json.dumps(rn.to_json()))["unrouted"] == 1


# --- sessions: gating, socket rule, teardown ---------------------------------------------------------------


def _caps(install, **values):
    return Capabilities(install, values, probed=True)


def test_session_refuses_installs_without_headless_ipc(tmp_path):
    install = _install("10.0.6")
    session = sessions.HeadlessSession(install, capabilities=_caps(install, **{"ipc.headless": False}))
    with pytest.raises(Unsupported) as info:
        session.start()
    assert info.value.capability == "ipc.headless" and info.value.fallback == "cli"
    assert info.value.oldest_kicad_with_it == "11.0.0"
    assert not session.running and sessions.live_sessions() == []


def test_session_rejects_missing_file(tmp_path):
    install = _install("10.99.0")
    session = sessions.HeadlessSession(
        install, tmp_path / "missing.kicad_pcb", capabilities=_caps(install, **{"ipc.headless": True})
    )
    with pytest.raises(sessions.SessionStartError) as info:
        session.start()
    assert info.value.code == "E_IPC_START" and "file not found" in str(info.value)
    assert sessions.live_sessions() == []


def test_socket_path_length_rule(monkeypatch, tmp_path):
    install = _install("10.99.0")
    long_root = tmp_path / ("x" * 100)

    class Root:
        def temp_dir(self, platform=None):
            return long_root

    session = sessions.HeadlessSession(
        install, config_root=Root(), capabilities=_caps(install, **{"ipc.headless": True})
    )
    with pytest.raises(sessions.SessionError) as info:
        session._socket_for()
    from kiwrap.platform import Platform, current

    limit = current().max_socket_path
    assert f"over the {limit}-char limit" in str(info.value) and "KIWRAP_HOME" in info.value.fix
    # the limit is the OS seam's: Windows named pipes 120, Linux AF_UNIX sun_path 107 (finding 20)
    assert Platform("windows", ".exe", tmp_path, tmp_path, tmp_path).max_socket_path == 120
    assert Platform("linux", "", tmp_path, tmp_path, tmp_path).max_socket_path == 107


def test_kill_tree_and_stop_on_a_plain_process(tmp_path):
    """`_ApiServerProcess.stop()` terminates an arbitrary child (a python sleep) and cleans up."""
    import subprocess
    import sys

    proc = sessions._ApiServerProcess(
        kicad_cli=Path(sys.executable), socket_path=tmp_path / "s.sock", env=None, cwd=tmp_path
    )
    proc.process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], cwd=tmp_path)
    assert proc.alive
    proc.stop()
    assert proc.process is None
    sessions._kill_tree(999999999)  # unknown pid: never raises


def test_stop_removes_the_lock_a_killed_server_left(tmp_path, monkeypatch):
    """A server that had to be killed (CloseAllDocuments failed: the wedged-server case) never
    released KiCad's `~<name>.kicad_pro.lck`; `stop()` removes the lock written around the
    server's start once the process is confirmed dead, and leaves a lock a GUI wrote later."""
    import sys

    monkeypatch.setattr(sessions, "_assign_to_job", lambda process: False)
    project = tmp_path / "proj"
    project.mkdir()
    board = project / "p.kicad_pcb"
    board.write_text("(kicad_pcb)", encoding="utf-8")
    proc = sessions._ApiServerProcess(
        kicad_cli=Path(sys.executable),
        socket_path=tmp_path / "sock" / "s.sock",
        env=None,
        cwd=tmp_path,
        file=board,
    )
    monkeypatch.setattr(
        sessions._ApiServerProcess,
        "argv",
        property(lambda self: [sys.executable, "-c", "import time; time.sleep(60)"]),
    )
    proc.start()
    assert proc.alive and proc.started_epoch > 0
    lock = project / "~p.kicad_pro.lck"
    lock.write_text('{"hostname": "h", "username": "u", "token": 1}', encoding="utf-8")  # as the server does
    later = project / "~other.kicad_pro.lck"
    later.write_text("{}", encoding="utf-8")
    future = proc.started_epoch + 120
    import os

    os.utime(later, (future, future))  # a GUI that took another project over afterwards
    record = proc.record
    assert record is not None and record.exists()
    proc.stop()
    assert proc.process is None
    assert not lock.exists(), "the dead server's lock must go with the process"
    assert later.exists()
    assert record is not None and not record.exists()
    later.unlink()


def test_release_project_sweeps_dead_servers_locks(tmp_path, monkeypatch):
    """Crash recovery: a record of a dead kiwrap server (the Job Object took it with a
    hard-killed parent) plus its `~<name>.kicad_pro.lck` must not survive the mutation
    prologue's `release_project()` - otherwise every later mutating tool is E_FILE_LOCKED."""
    import os
    import time

    from kiwrap.platform import current

    temp = tmp_path / "temp"
    monkeypatch.setattr(sessions, "current", lambda: type("P", (), {"temp_dir": temp})())
    project = tmp_path / "p"
    project.mkdir()
    (project / "p.kicad_sch").write_text("(kicad_sch)", encoding="utf-8")
    lock = project / "~p.kicad_pro.lck"
    lock.write_text('{"hostname": "x", "username": "kiwrap", "token": 1}', encoding="utf-8")
    dead = 4_000_000_000 - 1
    assert not sessions._pid_alive(dead)
    record = sessions._write_record(
        temp,
        dead,
        ["kicad-cli", "api-server"],
        temp / "x.sock",
        project / "p.kicad_sch",
        started=time.time() - 30,
    )
    record.write_text(record.read_text(encoding="utf-8").replace(str(os.getpid()), "1"), encoding="utf-8")
    info = sessions.release_project(project)
    assert info == {"closed": [], "busy": []}
    assert not lock.exists() and not record.exists()
    assert current().temp_dir  # the real platform is untouched by the monkeypatch


def test_pool_stamp_covers_the_sheet_tree(tmp_path: Path):
    """A pooled root-schematic session must be dropped when a CHILD sheet changes on disk
    (the server holds the whole hierarchy); a board stamp is the file's own."""
    import os

    root = tmp_path / "top.kicad_sch"
    child = tmp_path / "blinker.kicad_sch"
    root.write_text("(kicad_sch)", encoding="utf-8")
    child.write_text("(kicad_sch)", encoding="utf-8")
    before = sessions._file_stamp(root)
    assert before is not None and before == sessions._file_stamp(root)
    child.write_text("(kicad_sch (changed))", encoding="utf-8")
    later = child.stat().st_mtime + 5
    os.utime(child, (later, later))
    assert sessions._file_stamp(root) != before
    assert sessions._file_stamp(tmp_path / "x.kicad_pro") is None  # missing file
    board = tmp_path / "b.kicad_pcb"
    board.write_text("(kicad_pcb)", encoding="utf-8")
    st = board.stat()
    assert sessions._file_stamp(board) == (st.st_mtime_ns, st.st_size)


def test_ipc_pin_types_are_spelled_like_the_file_tokens():
    """Receipts must not depend on the provider: the IPC twins / readers spell electrical pin
    types with KiCad's file tokens (`power_in`, `power_out`, `tri_state`), the same words the
    file writer, kw_lib_info and kw_sch_stats use."""
    from kipy.proto.common.types.base_types_pb2 import ElectricalPinType

    from kiwrap.formats.schematic import parse_lib_symbols
    from kiwrap.formats.sexpr import loads

    got = {name: readers.pin_type_token(value) for name, value in ElectricalPinType.items()}
    assert got["EPT_POWER_INPUT"] == "power_in" and got["EPT_POWER_OUTPUT"] == "power_out"
    assert got["EPT_TRISTATE"] == "tri_state" and got["EPT_OPEN_COLLECTOR"] == "open_collector"
    assert got["EPT_BIDIRECTIONAL"] == "bidirectional" and got["EPT_NO_CONNECT"] == "no_connect"
    file_tokens = {
        "input",
        "output",
        "bidirectional",
        "tri_state",
        "passive",
        "free",
        "unspecified",
        "power_in",
        "power_out",
        "open_collector",
        "open_emitter",
        "no_connect",
    }
    assert {v for k, v in got.items() if k != "EPT_UNKNOWN"} == file_tokens
    # the file side speaks the same words (a library symbol's pins as the file writer reports them)
    lib = loads(
        '(lib_symbols (symbol "X" (symbol "X_1_1"'
        ' (pin power_in line (at 0 0 0) (length 2.54) (name "V") (number "1"))'
        ' (pin tri_state line (at 0 0 0) (length 2.54) (name "T") (number "2")))))'
    )
    types = {p.number: p.type for p in parse_lib_symbols(lib)["X"].pins}
    assert types == {"1": "power_in", "2": "tri_state"} and set(types.values()) <= file_tokens


def test_oldest_kicad_with_skips_nightlies():
    assert sessions.oldest_kicad_with("ipc.headless") == "11.0.0"
    assert sessions.oldest_kicad_with("ipc.available") == "9.0.5"
    assert sessions.oldest_kicad_with("no.such.key") is None


def test_ipc_error_shape():
    err = sessions.SessionStartError("boom", why="w", fix="f", details="d")
    assert err.to_json() == {"code": "E_IPC_START", "message": "boom", "why": "w", "fix": "f", "details": "d"}


def test_presence_probes_run_once_per_key(monkeypatch):
    """docs/UPSTREAM.md §3.3: the first session with a board runs every manifest presence probe
    whose key the record has not probed yet; a probed key (`kiwrap.ipc_probed`) is skipped and a
    probe that fails for another reason is swallowed and left for the next session."""
    from kiwrap.backends.ipc import presence

    assert presence.PROBES["ipc.has_get_ratsnest"] is queries.probe_get_ratsnest
    calls: list[str] = []

    def probe_ok(session):
        calls.append("ok")
        session.capabilities.refine("ipc.fake_ok", True)
        return True

    def probe_boom(session):
        calls.append("boom")
        raise RuntimeError("session broken")

    monkeypatch.setattr(
        presence, "PROBES", {"ipc.fake_ok": probe_ok, "ipc.done": probe_ok, "ipc.fake_boom": probe_boom}
    )
    caps = Capabilities(_install(), {"ipc.headless": True, "kiwrap.ipc_probed": ["ipc.done"]}, probed=True)

    class Session:
        capabilities = caps

    assert presence.run_pending(Session()) == ["ipc.fake_ok"]
    assert calls == ["ok", "boom"]
    assert caps["ipc.fake_ok"] is True and caps["kiwrap.ipc_probed"] == ["ipc.done", "ipc.fake_ok"]
    # the next session probes only what is still pending
    calls.clear()
    assert presence.run_pending(Session()) == []
    assert calls == ["boom"]
