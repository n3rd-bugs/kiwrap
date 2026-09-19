"""Schematic writer unit tests (no KiCad needed): epoch dialects, `extends` flattening, the
claim-3 pin transform through the writer, wire splitting / junctions, per-instance references
of a twice-placed sheet, lossless edit / delete / annotate / assign_footprints round trips.

The library is the hand-written `conformance/corpus/writer/libs/Test.kicad_sym` (a 2-pin
asymmetric symbol, a derived one, a 3-unit symbol and a power symbol)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from kiwrap.formats.epoch_dialects import convert_lib_symbol, dialect_for, dialect_for_stamp
from kiwrap.formats.geometry import Vec2
from kiwrap.formats.schematic import Hierarchy, SchematicFile, pin_sheet_position
from kiwrap.formats.schematic_writer import (
    FallbackLibResolver,
    SchematicWriter,
    WriterError,
    flatten_symbol,
)
from kiwrap.formats.sexpr import dumps, load, loads

REPO = Path(__file__).resolve().parents[1]
LIBS = REPO / "conformance" / "corpus" / "writer" / "libs"
EPOCH_10 = {"sch": 20260306, "sym": 20251024, "pro": 3}
EPOCH_9 = {"sch": 20250114, "sym": 20241209, "pro": 3}
EPOCH_8 = {"sch": 20231120, "sym": 20231120, "pro": 1}
EPOCH_7 = {"sch": 20230121, "sym": 20220914, "pro": 1}
EPOCH_11 = {"sch": 20260830, "sym": 20260830, "pro": 4}


@pytest.fixture
def resolver() -> FallbackLibResolver:
    return FallbackLibResolver(symbol_dirs=[LIBS])


def make(tmp_path: Path, epoch=EPOCH_10, resolver=None, name="t") -> tuple[SchematicWriter, Path]:
    w = SchematicWriter(
        tmp_path, epoch=epoch, lib_resolver=resolver or FallbackLibResolver(symbol_dirs=[LIBS])
    )
    root = tmp_path / f"{name}.kicad_sch"
    w.load(root)
    return w, root


# ----------------------------------------------------------------------------------------------
# dialects
# ----------------------------------------------------------------------------------------------


def test_dialect_eras_from_stamps():
    assert dialect_for(EPOCH_7).name == "7" and dialect_for(EPOCH_7).generator_version is None
    assert dialect_for(EPOCH_8).name == "8" and not dialect_for(EPOCH_8).hide_yes_lists
    assert dialect_for(EPOCH_9).name == "9" and dialect_for(EPOCH_9).hide_yes_lists
    assert dialect_for(EPOCH_9).sheet_flags and not dialect_for(EPOCH_8).sheet_flags
    d10 = dialect_for(EPOCH_10)
    assert d10.name == "10" and d10.property_level_hide and d10.power_scope and d10.in_pos_files
    assert dialect_for({"sch": 20260101}).name == "10"  # the early 10.0 stamp (shipped demos)
    d11 = dialect_for(EPOCH_11)
    assert d11.name == "11" and d11.empty_unit_blocks and d11.sch_stamp == 20260830
    assert dialect_for_stamp(20260306, generator_version="10.0.6").generator_version == "10.0.6"
    with pytest.raises(ValueError):
        dialect_for({"sym": 1})


def test_convert_lib_symbol_downgrades_tokens(resolver):
    node = resolver.symbol("Test:VCC").node.copy()
    d8 = dialect_for(EPOCH_8)
    convert_lib_symbol(node, d8)
    text = dumps(node)
    assert "in_pos_files" not in text and "duplicate_pin_numbers" not in text
    assert "(power)" in text and "(power global)" not in text
    assert "(pin_numbers hide)" in text
    assert "show_name" not in text and "do_not_autoplace" not in text
    assert "embedded_fonts" not in text
    # property hide moved into effects as (hide yes); pin hide is a bare token on 8
    ref = next(p for p in node.find_all("property") if p.atoms[0].value == "Reference")
    assert ref.find("hide") is None and ref.find("effects").find("hide") is not None
    pin = node.find_all("symbol")[1].find("pin")
    assert pin.find("hide") is None and any(a.value == "hide" for a in pin.atoms)
    # 7: bare hide inside effects too
    node7 = resolver.symbol("Test:VCC").node.copy()
    convert_lib_symbol(node7, dialect_for(EPOCH_7))
    ref7 = next(p for p in node7.find_all("property") if p.atoms[0].value == "Reference")
    assert any(a.value == "hide" for a in ref7.find("effects").atoms)
    # 9: (hide yes) lists on pins, property hide still in effects
    node9 = resolver.symbol("Test:VCC").node.copy()
    convert_lib_symbol(node9, dialect_for(EPOCH_9))
    assert "(pin_numbers\n\t\t\t(hide yes)" in dumps(node9).replace("\r\n", "\n") or "(hide yes)" in dumps(
        node9
    )
    ref9 = next(p for p in node9.find_all("property") if p.atoms[0].value == "Reference")
    assert ref9.find("hide") is None and ref9.find("effects").find("hide") is not None
    # 11: the empty unit-1 block the nightly writer emits
    node11 = resolver.symbol("Test:VCC").node.copy()
    convert_lib_symbol(node11, dialect_for(EPOCH_11))
    assert [s.atoms[0].value for s in node11.find_all("symbol")] == ["VCC_0_1", "VCC_1_1"]
    flag = loads(
        '(symbol "PWR_FLAG" (power global) (symbol "PWR_FLAG_0_1" (pin power_out line (at 0 0 90) (length 0)'
        ' (name "~") (number "1"))) (embedded_fonts no))'
    )
    convert_lib_symbol(flag, dialect_for(EPOCH_11))
    assert [s.atoms[0].value for s in flag.find_all("symbol")] == ["PWR_FLAG_0_1", "PWR_FLAG_1_1"]
    assert [c.head for c in flag.nodes][-1] == "embedded_fonts"


def test_convert_lib_symbol_round_trip_10_is_identity(resolver):
    node = resolver.symbol("Test:T2").node.copy()
    before = dumps(node)
    convert_lib_symbol(node, dialect_for(EPOCH_10))
    assert dumps(node) == before


# ----------------------------------------------------------------------------------------------
# library resolution / flattening
# ----------------------------------------------------------------------------------------------


def test_flatten_extends_matches_kicad_rules(resolver):
    t2d = resolver.symbol("Test:T2D")
    assert t2d.units == 1 and [p.number for p in t2d.pins] == ["1", "2"]
    node = t2d.node
    assert node.get("extends") is None
    assert node.atoms[0].value == "T2D"
    subs = [s.atoms[0].value for s in node.find_all("symbol")]
    assert subs == ["T2D_0_1", "T2D_1_1"]  # parent units renamed to the child
    props = {p.atoms[0].value: p.atoms[1].value for p in node.find_all("property")}
    assert props["Value"] == "T2D" and props["Footprint"] == "Test:FP"  # child overrides
    assert props["ki_keywords"] == "test"  # parent-only property kept
    assert props["ki_fp_filters"] == "FP*"  # child-only property added
    order = [p.atoms[0].value for p in node.find_all("property")]
    assert order.index("ki_fp_filters") > order.index("ki_keywords")
    # child-only properties sit before the first unit block, flags come from the parent
    heads = [c.head for c in node.nodes]
    assert heads.index("property") < heads.index("symbol")
    assert node.get("in_pos_files") == "yes"
    # only one embedded_fonts survives
    assert len(node.find_all("embedded_fonts")) == 1


def test_flatten_reports_missing_parent_and_cycles():
    lib = loads('(kicad_symbol_lib (version 1) (symbol "A" (extends "B")) (symbol "C" (extends "C")))')
    raw = {s.atoms[0].value: s for s in lib.find_all("symbol")}
    with pytest.raises(WriterError, match="not found"):
        flatten_symbol("A", raw.get)
    with pytest.raises(WriterError, match="cycle"):
        flatten_symbol("C", raw.get)


def test_fallback_resolver_errors_are_teaching(resolver):
    with pytest.raises(WriterError) as e:
        resolver.symbol("Nope:T2")
    assert e.value.code == "E_LIB_NOT_FOUND" and "Test" in e.value.candidates
    with pytest.raises(WriterError) as e:
        resolver.symbol("Test:T")
    assert e.value.code == "E_SYMBOL_NOT_FOUND" and "T2" in e.value.candidates
    with pytest.raises(WriterError) as e:
        resolver.symbol("T2")
    assert e.value.code == "E_LIB_ID"
    assert resolver.search("t2") == ["Test:T2", "Test:T2D"]


def test_fallback_resolver_reads_sym_lib_table(tmp_path: Path):
    (tmp_path / "sym-lib-table").write_text(
        '(sym_lib_table (version 7) (lib (name "Alias")(type "KiCad")(uri "${KIPRJMOD}/libs/Test.kicad_sym")(options "")(descr "")))',
        encoding="utf-8",
    )
    (tmp_path / "libs").mkdir()
    (tmp_path / "libs" / "Test.kicad_sym").write_bytes((LIBS / "Test.kicad_sym").read_bytes())
    r = FallbackLibResolver(None, None, tmp_path)
    assert "Alias" in r.nicknames
    assert r.symbol("Alias:T2").name == "T2"


# ----------------------------------------------------------------------------------------------
# placement geometry (claim 3 through the writer)
# ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
@pytest.mark.parametrize("mirror", [None, "x", "y"])
def test_pin_endpoints_follow_verified_transform(tmp_path: Path, rotation, mirror):
    w, root = make(tmp_path)
    p = w.add_symbol("Test:T2", "T1", None, (50.8, 76.2), rotation=rotation, mirror=mirror)
    lib = {"1": Vec2(-5.08, 2.54), "2": Vec2(5.08, -2.54)}
    for pin in p.pins:
        want = pin_sheet_position(Vec2(50.8, 76.2), rotation, mirror, lib[pin.number])
        assert (pin.x_mm, pin.y_mm) == (round(want.x, 4), round(want.y, 4))
        assert abs(pin.x_mm / 1.27 - round(pin.x_mm / 1.27)) < 1e-6  # on grid
    # the file model computes the same end points from the saved file
    w.save()
    sch = SchematicFile.load(root)
    got = {sp.number: (sp.position.x, sp.position.y) for sp in sch.symbols[0].pins}
    assert got == {pin.number: (pin.x_mm, pin.y_mm) for pin in p.pins}
    assert w.pins_of("T1") == p.pins


def test_add_symbol_snaps_to_grid_and_validates(tmp_path: Path):
    w, root = make(tmp_path)
    p = w.add_symbol("Test:T2", None, None, (50.5, 76.0))
    assert p.ref == "T1"
    sch_at = w.find(p.uuid)[1].find("at").values()
    assert sch_at[:2] == ["50.8", "76.2"]
    with pytest.raises(WriterError, match="rotation"):
        w.add_symbol("Test:T2", None, None, (0, 0), rotation=45)
    with pytest.raises(WriterError, match="unit 2"):
        w.add_symbol("Test:T2", None, None, (0, 0), unit=2)
    with pytest.raises(WriterError) as e:
        w.add_symbol("Test:MU", "T1", None, (0, 0))
    assert e.value.code == "E_REF_TAKEN"
    with pytest.raises(WriterError) as e:
        w.add_symbol("Test:T2", "T1", None, (0, 0))
    assert e.value.code == "E_REF_TAKEN"


def test_lib_symbols_embedded_once_prefixed_and_sorted(tmp_path: Path):
    w, root = make(tmp_path)
    w.add_symbol("Test:T2", None, None, (50.8, 50.8))
    w.add_symbol("Test:T2", None, None, (76.2, 50.8))
    w.add_symbol("Test:MU", None, None, (101.6, 50.8))
    w.add_power("VCC", (127, 50.8), library="Test")
    w.save()
    text = root.read_text(encoding="utf-8")
    names = re.findall(r'^\t\t\(symbol "([^"]+)"', text, re.M)
    assert names == ["Test:MU", "Test:T2", "Test:VCC"]  # dedup + KiCad's sorted map order
    assert '(symbol "T2_1_1"' in text and '(symbol "Test:T2_1_1"' not in text  # sub-units unprefixed
    sch = SchematicFile.load(root)
    assert set(sch.lib_symbols) == {"Test:MU", "Test:T2", "Test:VCC"}
    refs = sorted(s.reference for s in sch.symbols)
    assert refs == ["#PWR01", "T1", "T2", "U1"]
    for s in sch.symbols:  # reference in BOTH the property and the instances block
        assert s.instances and s.instances[0].reference == s.reference
        assert s.instances[0].path == "/" + sch.uuid


def test_placed_symbol_lists_every_pin_and_unit_pins_are_returned(tmp_path: Path):
    w, root = make(tmp_path)
    a = w.add_symbol("Test:MU", "U1", None, (50.8, 50.8))
    b = w.add_symbol("Test:MU", "U1", None, (50.8, 76.2), unit=2)
    c = w.add_symbol("Test:MU", "U1", None, (101.6, 63.5), unit=3)
    assert [p.number for p in a.pins] == ["1", "2"]
    assert [p.number for p in b.pins] == ["3", "4"]
    assert [p.number for p in c.pins] == ["5", "6"]
    node = w.find(b.uuid)[1]
    assert [p.atoms[0].value for p in node.find_all("pin")] == ["1", "2", "3", "4", "5", "6"]
    assert node.get("unit") == "2"
    assert len(w.pins_of("U1")) == 6
    with pytest.raises(WriterError, match="unit 2 is already placed"):
        w.add_symbol("Test:MU", "U1", None, (0, 0), unit=2)


def test_demorgan_symbol_carries_one_body_style(tmp_path: Path):
    """`Test:DM` has `DM_1_1` and `DM_1_2` (De Morgan) blocks: the placed symbol lists each
    library pin of body style 1 ONCE (KiCad's `SCH_SYMBOL::UpdatePins` skips the other style, a
    `sch upgrade` re-save drops duplicates) and the unit pins come back once each."""
    w, root = make(tmp_path)
    a = w.add_symbol("Test:DM", "U1", None, (50.8, 50.8))
    assert [p.number for p in a.pins] == ["1", "2", "3"]
    assert [(p.number, p.at_mm) for p in a.pins] == [
        ("1", (43.18, 48.26)),
        ("2", (43.18, 53.34)),
        ("3", (58.42, 50.8)),
    ]
    node = w.find(a.uuid)[1]
    assert [p.atoms[0].value for p in node.find_all("pin")] == ["1", "2", "3", "4", "5"]
    b = w.add_symbol("Test:DM", "U1", None, (50.8, 76.2), unit=2)
    assert [p.number for p in b.pins] == ["4", "5"]
    assert sorted(p.number for p in w.pins_of("U1")) == ["1", "2", "3", "4", "5"]
    assert sorted(p.number for s in w.symbols() for p in s.pins) == ["1", "2", "3", "4", "5"]
    # a wire ending on a style-2 pin position connects nothing: it is not a connection point
    assert (43.18, 45.72) not in [pt for pt in w._connection_points(w._doc(""))]


def test_epoch_11_unit_blocks_cover_the_unit_x_style_matrix(tmp_path: Path):
    """The 11-preview writer emits every `_U_S` block (styles 1..2 when a De Morgan block exists):
    `DM_2_2` is added next to `DM_2_1`; a symbol without alternate bodies gets none."""
    w, root = make(tmp_path, epoch=EPOCH_11)
    w.add_symbol("Test:DM", "U1", None, (50.8, 50.8))
    w.add_symbol("Test:MU", "U2", None, (76.2, 50.8))
    lib = w._doc("").lib_symbols()
    names = {
        sym.atoms[0].value: [b.atoms[0].value for b in sym.find_all("symbol")]
        for sym in lib.find_all("symbol")
    }
    assert names["Test:DM"] == ["DM_1_1", "DM_1_2", "DM_2_1", "DM_2_2"]
    assert names["Test:MU"] == ["MU_1_1", "MU_2_1", "MU_3_1"]
    w10, _ = make(tmp_path / "ten", epoch=EPOCH_10) if (tmp_path / "ten").mkdir() is None else (None, None)
    w10.add_symbol("Test:DM", "U1", None, (50.8, 50.8))
    lib10 = w10._doc("").lib_symbols()
    dm10 = next(sym for sym in lib10.find_all("symbol") if sym.atoms[0].value == "Test:DM")
    assert [b.atoms[0].value for b in dm10.find_all("symbol")] == ["DM_1_1", "DM_1_2", "DM_2_1"]


def test_add_sheet_rejects_files_outside_the_project(tmp_path: Path):
    """A sheet file is a bare name in the project directory: `../x`, `C:/x`, `a/b` would make
    save() create a .kicad_sch outside the project (not owned, not snapshotted)."""
    w, root = make(tmp_path, name="top")
    for bad in ("../escaped", "C:/x/y", "a/b", "a\\b", "/abs", ""):
        with pytest.raises(WriterError, match="E_SHEET_FILE|bare file name") as info:
            w.add_sheet("evil", bad, (50.8, 50.8), (20.32, 20.32))
        assert info.value.code == "E_SHEET_FILE", bad
    assert w.save() == [root, tmp_path / "top.kicad_pro"] or all(p.parent == tmp_path for p in w.save())
    assert not (tmp_path.parent / "escaped.kicad_sch").exists()
    ok = w.add_sheet("fine", "child", (50.8, 50.8), (20.32, 20.32))
    assert ok.file == tmp_path / "child.kicad_sch"


def test_nested_sheet_pages_are_contiguous_after_a_second_instance(tmp_path: Path):
    """root -> stage.kicad_sch (placed twice) -> inner.kicad_sch: pages 1..5, no gap (the
    nested sheet under the second instance was numbered 6 of 5)."""
    w, root = make(tmp_path, name="top")
    a = w.add_sheet("stage_a", "stage.kicad_sch", (50.8, 50.8), (25.4, 25.4))
    inner = w.add_sheet("inner", "inner.kicad_sch", (76.2, 63.5), (25.4, 20.32), sheet="stage")
    assert (a.page, inner.page) == ("2", "3")
    b = w.add_sheet("stage_b", "stage.kicad_sch", (101.6, 50.8), (25.4, 25.4))
    assert b.page == "4"
    w.save()
    hier = Hierarchy.load(root)
    pages = {i.names_path: int(i.page) for i in hier.instances}
    assert pages == {"/": 1, "/stage_a/": 2, "/stage_a/inner/": 3, "/stage_b/": 4, "/stage_b/inner/": 5}
    assert sorted(pages.values()) == list(range(1, 6))


def test_connectivity_reports_the_dangling_wire_and_the_pin_it_leaves_unconnected(tmp_path: Path):
    """kw_sch_stats offenders must be actionable: a dangling end carries the wire's uuid and the
    `/name/` sheet path, and the pin whose only connection is that wire is `pin_not_connected`
    (KiCad's subgraph rule) - not only pins nothing touches."""
    from kiwrap.formats.analysis import pin_connectivity

    w, root = make(tmp_path)
    t = w.add_symbol("Test:T2", "T1", None, (50.8, 50.8))
    p2 = t.pin("2")
    (wire_id,) = w.add_wire([p2.at_mm, (p2.x_mm + 7.62, p2.y_mm)])
    w.save()
    conn = pin_connectivity(Hierarchy.load(root))
    assert {(p.reference, p.number) for p in conn.unconnected_pins} == {("T1", "1"), ("T1", "2")}
    (end,) = conn.dangling_wire_ends
    assert (
        end.wire_uuid == wire_id
        and end.names_path == "/"
        and end.position.as_tuple()
        == (
            p2.x_mm + 7.62,
            p2.y_mm,
        )
    )
    # a label on the stub is a connection for KiCad (the pin is no longer lonely; the net is label-only)
    w2, _ = make(tmp_path)
    w2.add_label("STUB", (p2.x_mm + 7.62, p2.y_mm), "local")
    w2.save()
    conn2 = pin_connectivity(Hierarchy.load(root))
    assert {(p.reference, p.number) for p in conn2.unconnected_pins} == {("T1", "1")}


# ----------------------------------------------------------------------------------------------
# wires and junctions
# ----------------------------------------------------------------------------------------------


def _wires(w: SchematicWriter) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    out = []
    for node in w._docs[""].items("wire"):
        xy = node.find("pts").find_all("xy")
        out.append((tuple(float(v) for v in xy[0].values()), tuple(float(v) for v in xy[1].values())))
    return sorted(out)


def test_add_wire_splits_at_pins_and_snaps(tmp_path: Path):
    w, root = make(tmp_path)
    t = w.add_symbol("Test:T2", "T1", None, (50.8, 50.8))  # pin 1 at (45.72, 48.26), pin 2 at (55.88, 53.34)
    p1 = t.pin("1").at_mm
    # a wire running THROUGH pin 1 (claim 2, negative control B) is split so the pin connects
    uuids = w.add_wire([(30.0, p1[1]), (60.96, p1[1])])
    assert len(uuids) == 2
    assert _wires(w) == [((30.48, 48.26), (45.72, 48.26)), ((45.72, 48.26), (60.96, 48.26))]
    # the pin with its two collinear wire ends is a junction (KiCad counts the pin as an exit:
    # `IsExplicitJunctionNeeded`, pin_count + end_count > 2; junction_helpers.cpp on the nightly)
    junctions = [tuple(float(v) for v in j.find("at").values()) for j in w._docs[""].items("junction")]
    assert junctions == [(45.72, 48.26)]
    # a wire ending on the interior of an existing wire splits it and gets a junction
    w.add_wire([(38.1, 48.26), (38.1, 38.1)])
    assert ((30.48, 48.26), (38.1, 48.26)) in _wires(w) and ((38.1, 48.26), (45.72, 48.26)) in _wires(w)
    junctions = [tuple(float(v) for v in j.find("at").values()) for j in w._docs[""].items("junction")]
    assert sorted(junctions) == [(38.1, 48.26), (45.72, 48.26)]
    with pytest.raises(WriterError):
        w.add_wire([(1, 1)])


def test_add_symbol_splits_existing_wire_under_its_pin(tmp_path: Path):
    w, root = make(tmp_path)
    w.add_wire([(25.4, 48.26), (76.2, 48.26)])
    t = w.add_symbol("Test:T2", "T1", None, (50.8, 50.8))
    assert t.pin("1").at_mm == (45.72, 48.26)
    assert _wires(w) == [((25.4, 48.26), (45.72, 48.26)), ((45.72, 48.26), (76.2, 48.26))]


def test_junction_rules(tmp_path: Path):
    w, root = make(tmp_path)
    # a plain corner: two non-parallel wire ends, no pin -> no junction
    w.add_wire([(25.4, 25.4), (50.8, 25.4), (50.8, 50.8)])
    assert not w._docs[""].items("junction")
    # three wire ends -> junction
    w.add_wire([(50.8, 25.4), (76.2, 25.4)])
    js = [tuple(float(v) for v in j.find("at").values()) for j in w._docs[""].items("junction")]
    assert js == [(50.8, 25.4)]
    # a pin with two collinear wire ends (a wire passing over the pin, split) -> junction: the
    # pin is a third exit (eeschema/sch_screen.cpp IsExplicitJunctionNeeded: pin_count +
    # end_count > 2; junction_helpers.cpp AnalyzePoint: "any three things of the same type")
    t = w.add_symbol("Test:T2", "T1", None, (50.8, 101.6))
    p1 = t.pin("1").at_mm
    w.add_wire([(p1[0] - 10.16, p1[1]), (p1[0] + 5.08, p1[1])])
    js = [tuple(float(v) for v in j.find("at").values()) for j in w._docs[""].items("junction")]
    assert p1 in js
    # a pin with ONE wire end -> no junction; a label on a wire's interior -> no junction (labels
    # are not exits; the extra dot breaks the net on 9.0.9, tests/test_e2e_authoring.py)
    p2 = t.pin("2").at_mm
    w.add_wire([p2, (p2[0] + 7.62, p2[1])])
    w.add_label("MID", (p2[0] + 3.81, p2[1]), "local")
    js = [tuple(float(v) for v in j.find("at").values()) for j in w._docs[""].items("junction")]
    assert p2 not in js and (p2[0] + 3.81, p2[1]) not in js


def test_labels_and_no_connect(tmp_path: Path):
    w, root = make(tmp_path)
    u_local = w.add_label("NET_A", (25.4, 25.4), "local")
    u_glob = w.add_label("NET_B", (25.4, 30.48), "global", rotation=180, shape="output")
    u_hier = w.add_label("NET_C", (25.4, 35.56), "hierarchical", rotation=90)
    u_nc = w.add_no_connect((25.4, 40.64))
    with pytest.raises(WriterError):
        w.add_label("X", (0, 0), "bogus")
    w.save()
    sch = SchematicFile.load(root)
    kinds = {lab.uuid: (lab.kind, lab.text, lab.shape, lab.rotation) for lab in sch.labels}
    assert kinds[u_local] == ("label", "NET_A", None, 0.0)
    assert kinds[u_glob] == ("global_label", "NET_B", "output", 180.0)
    assert kinds[u_hier] == ("hierarchical_label", "NET_C", "input", 90.0)
    assert sch.no_connects[0].uuid == u_nc and sch.no_connects[0].position == Vec2(25.4, 40.64)
    text = root.read_text(encoding="utf-8")
    assert "(justify right)" in text and "(justify left bottom)" in text
    assert '"Intersheetrefs" "${INTERSHEET_REFS}"' in text


# ----------------------------------------------------------------------------------------------
# hierarchy
# ----------------------------------------------------------------------------------------------


def test_sheet_twice_gives_two_references_and_instance_paths(tmp_path: Path):
    w, root = make(tmp_path, name="top")
    s1 = w.add_sheet(
        "blk_a",
        "blk.kicad_sch",
        (50.8, 50.8),
        (25.4, 20.32),
        [("IN", "left", "input"), ("OUT", "right", "output")],
    )
    assert [(p.name, p.at_mm) for p in s1.pins] == [("IN", (50.8, 53.34)), ("OUT", (76.2, 53.34))]
    t = w.add_symbol("Test:T2", None, None, (50.8, 50.8), sheet="blk")
    assert t.ref == "T1" and list(t.references.values()) == ["T1"]
    s2 = w.add_sheet(
        "blk_b",
        "blk.kicad_sch",
        (50.8, 88.9),
        (25.4, 20.32),
        [("IN", "left", "input"), ("OUT", "right", "output")],
    )
    assert s1.instance_path == f"/{w.root_uuid}/{s1.uuid}" and s2.instance_path == f"/{w.root_uuid}/{s2.uuid}"
    assert w.instance_paths("blk") == [s1.instance_path, s2.instance_path]
    u = w.add_symbol("Test:MU", None, None, (76.2, 50.8), sheet="blk.kicad_sch")
    assert u.references == {s1.instance_path: "U1", s2.instance_path: "U2"}
    written = w.save()
    assert {p.name for p in written} == {"top.kicad_sch", "blk.kicad_sch", "top.kicad_pro"}
    hier = Hierarchy.load(root)
    refs = {inst.names_path: sorted(inst.references()) for inst in hier.instances}
    assert refs == {"/": [], "/blk_a/": ["T1", "U1"], "/blk_b/": ["T2", "U2"]}
    # pages: root 1, blk_a 2, blk_b 3; sheet_instances only in the root file
    assert {i.names_path: i.page for i in hier.instances} == {"/": "1", "/blk_a/": "2", "/blk_b/": "3"}
    assert "sheet_instances" not in (tmp_path / "blk.kicad_sch").read_text(encoding="utf-8")
    assert '(sheet_instances\n\t\t(path "/"\n\t\t\t(page "1")' in root.read_text(encoding="utf-8").replace(
        "\r\n", "\n"
    )
    # deleting one sheet instance drops its paths from the sheet file
    w.delete(s2.uuid)
    w.save()
    hier = Hierarchy.load(root)
    assert {inst.names_path for inst in hier.instances} == {"/", "/blk_a/"}
    sub = SchematicFile.load(tmp_path / "blk.kicad_sch")
    assert all(len(s.instances) == 1 for s in sub.symbols)


def test_sheet_pin_errors(tmp_path: Path):
    w, root = make(tmp_path)
    with pytest.raises(WriterError, match="side"):
        w.add_sheet("a", "a.kicad_sch", (0, 0), (10, 10), [("X", "middle", "input")])
    with pytest.raises(WriterError, match="too many pins"):
        w.add_sheet(
            "b",
            "b.kicad_sch",
            (0, 0),
            (10, 5.08),
            [("X", "left", "input"), ("Y", "left", "input"), ("Z", "left", "input")],
        )
    with pytest.raises(WriterError, match="no sheet"):
        w.add_symbol("Test:T2", None, None, (0, 0), sheet="nowhere")


# ----------------------------------------------------------------------------------------------
# lossless round trips
# ----------------------------------------------------------------------------------------------


def _changed_lines(before: str, after: str) -> list[tuple[str, str]]:
    a, b = before.splitlines(), after.splitlines()
    return (
        [(x, y) for x, y in zip(a, b, strict=False) if x != y]
        + [("", y) for y in b[len(a) :]]
        + [(x, "") for x in a[len(b) :]]
    )


def _project(tmp_path: Path) -> Path:
    w, root = make(tmp_path)
    t1 = w.add_symbol("Test:T2", "T1", "one", (50.8, 50.8))
    t2 = w.add_symbol("Test:T2", "T2", "two", (76.2, 50.8), rotation=90)
    w.add_wire([t1.pin("2").at_mm, (t2.pin("1").x_mm, t1.pin("2").y_mm), t2.pin("1").at_mm])
    w.add_label("MID", (60.96, t1.pin("2").y_mm), "local")
    w.save()
    return root


def test_edit_value_footprint_and_dnp_touch_only_their_lines(tmp_path: Path):
    root = _project(tmp_path)
    before = root.read_text(encoding="utf-8")
    w = SchematicWriter(tmp_path, epoch=EPOCH_10, lib_resolver=FallbackLibResolver(symbol_dirs=[LIBS]))
    w.load(root)
    t1 = next(s for s in w.symbols() if s.ref == "T1")
    w.edit(t1.uuid, {"value": "1k", "footprint": "Test:FP", "dnp": True})
    w.save()
    after = root.read_text(encoding="utf-8")
    changed = _changed_lines(before, after)
    assert len(changed) == 3, changed
    assert {b.strip() for _a, b in changed} == {
        '(property "Value" "1k"',
        '(property "Footprint" "Test:FP"',
        "(dnp yes)",
    }
    sch = SchematicFile.load(root)
    t = sch.symbol("T1")
    assert t.value == "1k" and t.footprint == "Test:FP" and t.dnp


def test_edit_move_rotate_mirror_and_reference(tmp_path: Path):
    root = _project(tmp_path)
    w = SchematicWriter(tmp_path, epoch=EPOCH_10, lib_resolver=FallbackLibResolver(symbol_dirs=[LIBS]))
    w.load(root)
    t2 = next(s for s in w.symbols() if s.ref == "T2")
    w.edit(
        t2.uuid,
        {"at": (101.6, 101.6), "rotation": 180, "mirror": "y", "reference": "T9", "fields": {"MPN": "X-1"}},
    )
    w.save()
    sch = SchematicFile.load(root)
    s = sch.symbol("T9")
    assert s is not None and s.position == Vec2(101.6, 101.6) and s.rotation == 180 and s.mirror == "y"
    assert s.instances[0].reference == "T9" and s.properties["MPN"] == "X-1"
    assert w.pins_of("T9")[0].at_mm == (96.52, 104.14)  # rot180 (X-lx, Y+ly), mirror y negates dx
    w.edit(s.uuid, {"mirror": None})
    assert w.find(s.uuid)[1].find("mirror") is None
    with pytest.raises(WriterError, match="unknown change"):
        w.edit(s.uuid, {"colour": "red"})
    with pytest.raises(WriterError, match="no item"):
        w.edit("nope", {"value": "x"})


def test_delete_removes_only_that_node(tmp_path: Path):
    root = _project(tmp_path)
    before = root.read_text(encoding="utf-8")
    w = SchematicWriter(tmp_path, epoch=EPOCH_10, lib_resolver=FallbackLibResolver(symbol_dirs=[LIBS]))
    w.load(root)
    label = SchematicFile.load(root).labels[0]
    w.delete(label.uuid)
    w.save()
    after = root.read_text(encoding="utf-8")
    removed = [a for a, b in _changed_lines(before, after) if not b]
    assert len(before.splitlines()) - len(after.splitlines()) == 10  # exactly the label node
    assert "MID" not in after
    assert not [x for x in removed if x.strip().startswith("(symbol")]
    with pytest.raises(WriterError):
        w.delete(label.uuid)


def test_annotate_and_assign_footprints(tmp_path: Path):
    w, root = make(tmp_path)
    a = w.add_symbol("Test:T2", "T?", None, (101.6, 50.8))
    b = w.add_symbol("Test:T2", None, None, (50.8, 50.8))
    u1 = w.add_symbol("Test:MU", "U7", None, (76.2, 76.2))
    u2 = w.add_symbol("Test:MU", "U7", None, (76.2, 101.6), unit=2)
    p = w.add_power("VCC", (25.4, 25.4), library="Test")
    assert a.ref == "T1" and b.ref == "T2" and p.ref == "#PWR01"
    w.save()
    before = root.read_text(encoding="utf-8")
    mapping = w.annotate("global", False)
    # sorted by X then Y: T2 at x=50.8 comes first -> T1, the old T1 (x=101.6) -> T2; U7 -> U1
    assert mapping == {"T2": "T1", "T1": "T2", "U7": "U1"}
    sch_nodes = {s.uuid: s for s in w.symbols()}
    assert sch_nodes[u1.uuid].ref == "U1" and sch_nodes[u2.uuid].ref == "U1"
    w.save()
    after = root.read_text(encoding="utf-8")
    changed = _changed_lines(before, after)
    assert all("Reference" in x or "(reference" in x for x, _y in changed), changed
    assert len(changed) == 8  # 4 symbols x (property + instances), power symbol untouched
    assert sorted(w.assign_footprints({"U1": "Test:DIP", "T2": "Test:FP", "ZZ9": "Test:none"})) == [
        "T2",
        "U1",
    ]
    w.save()
    sch = SchematicFile.load(root)
    assert sch.symbol("T2").footprint == "Test:FP"
    assert all(s.footprint == "Test:DIP" for s in sch.symbols if s.reference == "U1")
    # keep_existing only fills in the unannotated ones
    w2, root2 = make(tmp_path, name="k")
    w2.add_symbol("Test:T2", "T5", None, (50.8, 50.8))
    w2.add_symbol("Test:T2", None, None, (76.2, 50.8))
    assert w2.annotate("sheet", False, keep_existing=True) == {}
    # sheet-scheme numbering restarts at page x 100 + 1
    w3, root3 = make(tmp_path, name="s")
    w3.add_sheet("child", "child.kicad_sch", (50.8, 50.8), (25.4, 20.32))
    w3.add_symbol("Test:T2", None, None, (50.8, 50.8))
    w3.add_symbol("Test:T2", None, None, (50.8, 50.8), sheet="child")
    m = w3.annotate("sheet", True)
    assert set(m.values()) == {"T101", "T201"}


def test_untouched_sheets_are_not_rewritten(tmp_path: Path):
    w, root = make(tmp_path, name="top")
    w.add_sheet("c", "c.kicad_sch", (50.8, 50.8), (25.4, 20.32))
    w.add_symbol("Test:T2", None, None, (50.8, 50.8), sheet="c")
    w.save()
    w2 = SchematicWriter(tmp_path, epoch=EPOCH_10, lib_resolver=FallbackLibResolver(symbol_dirs=[LIBS]))
    w2.load(root)
    w2.add_symbol("Test:T2", None, None, (25.4, 25.4))
    assert [p.name for p in w2.save()] == ["top.kicad_sch"]


def test_existing_file_bytes_survive_load_save(tmp_path: Path):
    """Loading a KiCad-written demo and saving with a no-op edit keeps every untouched byte."""
    src = REPO / "spikes" / "schematic-authoring" / "hier_out" / "ledhier"
    if not src.is_dir():
        pytest.skip("spike fixture missing")
    for f in list(src.glob("*.kicad_sch")) + list(src.glob("*.kicad_pro")):
        (tmp_path / f.name).write_bytes(f.read_bytes())
    root = tmp_path / "ledhier.kicad_sch"
    before = root.read_bytes()
    w = SchematicWriter(tmp_path, epoch=EPOCH_10, lib_resolver=FallbackLibResolver(symbol_dirs=[LIBS]))
    w.load(root)
    assert w.sheet_keys == ["", "led_sub.kicad_sch"]
    assert w.project_name == "ledhier"
    assert w.save() == []  # nothing touched, nothing written
    sym = next(s for s in w.symbols("led_sub") if s.ref == "R1")
    assert [p.at_mm for p in sym.pins] == [(63.5, 46.99), (63.5, 54.61)]
    w.edit(sym.uuid, {"value": "1k"})
    assert w.save() == [tmp_path / "led_sub.kicad_sch"]
    assert root.read_bytes() == before
    after = (tmp_path / "led_sub.kicad_sch").read_text(encoding="utf-8")
    assert '(property "Value" "1k"' in after and '"330R"' not in after


# ----------------------------------------------------------------------------------------------
# dialect output shape
# ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "epoch, expect, forbid",
    [
        (
            EPOCH_7,
            ["(generator eeschema)", "(uuid ", "hide)"],
            ["generator_version", "exclude_from_sim", "(hide yes)", "embedded_fonts", "body_style"],
        ),
        (
            EPOCH_8,
            ['(generator_version "8.0")', "(exclude_from_sim no)", "(hide yes)"],
            ["in_pos_files", "show_name", "embedded_fonts", "(power global)"],
        ),
        (
            EPOCH_9,
            ['(generator_version "9.0")', "(embedded_fonts no)"],
            ["in_pos_files", "show_name", "body_style", "(power global)"],
        ),
        (
            EPOCH_10,
            [
                '(generator_version "10.0")',
                "(body_style 1)",
                "(in_pos_files yes)",
                "(show_name no)",
                "(power global)",
            ],
            [],
        ),
        (EPOCH_11, ["(version 20260830)", '(generator_version "10.99")', "(in_pos_files yes)"], []),
    ],
)
def test_document_dialects(tmp_path: Path, epoch, expect, forbid):
    w, root = make(tmp_path, epoch=epoch)
    t = w.add_symbol("Test:T2", None, None, (50.8, 50.8))
    w.add_power("VCC", (25.4, 25.4), library="Test")
    w.add_wire([t.pin("1").at_mm, (25.4, t.pin("1").y_mm)])
    w.add_sheet("s", "s.kicad_sch", (101.6, 25.4), (25.4, 20.32), [("A", "left", "input")])
    w.save()
    text = root.read_text(encoding="utf-8")
    assert f"(version {epoch['sch']})" in text
    for token in expect:
        assert token in text, token
    for token in forbid:
        assert token not in text, token
    # every dialect is still a schematic the file model reads with connected pins
    sch = SchematicFile.load(root)
    assert sch.stamp == epoch["sch"] and len(sch.symbols) == 2 and sch.symbols[0].pins
    assert load(root).get("uuid") == sch.uuid


# ----------------------------------------------------------------------------------------------
# epochs of existing files and the committed fixtures
# ----------------------------------------------------------------------------------------------

WRITER_CORPUS = REPO / "conformance" / "corpus" / "writer"


def _fixture(tmp_path: Path, name: str) -> Path:
    src = WRITER_CORPUS / name
    if not src.is_dir():
        pytest.skip(f"{src} missing")
    for f in src.iterdir():
        if f.suffix in (".kicad_sch", ".kicad_pro"):
            (tmp_path / f.name).write_bytes(f.read_bytes())
    return tmp_path / ("top.kicad_sch" if name.startswith("hier") else "blink.kicad_sch")


def test_older_file_is_restamped_on_save_and_newer_file_refused(tmp_path: Path):
    root = _fixture(tmp_path, "blink_9")
    w = SchematicWriter(tmp_path, epoch=EPOCH_10, lib_resolver=FallbackLibResolver(symbol_dirs=[LIBS]))
    w.load(root)
    assert w.save() == []  # untouched: the 9-era bytes stay
    assert SchematicFile.load(root).stamp == EPOCH_9["sch"]
    r1 = next(s for s in w.symbols() if s.ref == "R1")
    w.edit(r1.uuid, {"value": "1k"})
    w.save()
    sch = SchematicFile.load(root)
    assert sch.stamp == EPOCH_10["sch"] and sch.generator_version == "10.0"
    assert sch.symbol("R1").value == "1k" and len(sch.symbols) == 7
    w9 = SchematicWriter(tmp_path, epoch=EPOCH_9, lib_resolver=FallbackLibResolver(symbol_dirs=[LIBS]))
    with pytest.raises(WriterError) as e:
        w9.load(root)
    assert e.value.code == "E_EPOCH"


@pytest.mark.parametrize("name", ["blink_8", "blink_9", "blink_10", "blink_11", "hier_10", "hier_11"])
def test_committed_fixtures_are_connected_in_the_file_model(tmp_path: Path, name: str):
    from kiwrap.formats.analysis import pin_connectivity

    root = _fixture(tmp_path, name)
    hier = Hierarchy.load(root)
    assert not hier.missing
    conn = pin_connectivity(hier)
    assert not conn.dangling_wire_ends
    nets = {n.name: sorted((p.reference, p.number) for p in n.pins) for n in conn.nets}
    assert "GND" in nets and "+5V" in nets
    if name.startswith("hier"):
        assert {i.names_path for i in hier.instances} == {"/", "/source/", "/amp_a/", "/amp_b/"}
        refs = {i.names_path: sorted(i.references()) for i in hier.instances}
        assert [r for r in refs["/amp_a/"] if not r.startswith("#")] == ["R1", "U1"]
        assert [r for r in refs["/amp_b/"] if not r.startswith("#")] == ["R2", "U2"]
        assert ("U1", "8") in nets["+5V"] and ("U2", "4") in nets["GND"]
