"""`.kicad_sym` parsing and `extends` flattening (KiCad's LIB_SYMBOL::Flatten rules, docs/VERIFICATION.md
claim 2) and `.kicad_mod` parsing, on the hand-written fixture library. No KiCad needed."""

from __future__ import annotations

from pathlib import Path

import pytest

from kiwrap.formats import sexpr
from kiwrap.formats.libs import (
    LibResolver,
    NotFound,
    SymbolLibrary,
    flatten,
    load_footprint,
    numbered_pad_count,
    parse_symbol,
    split_lib_id,
)
from kiwrap.formats.libs.footprints import footprint_names
from tests.test_libs_tables import FIXTURES, StubInstall, env_for


@pytest.fixture(scope="module")
def lib() -> SymbolLibrary:
    return SymbolLibrary.load(FIXTURES / "Test.kicad_sym", nickname="Test")


def names_in(node: sexpr.Node) -> list[str]:
    return [node.atoms[0].value, *(s.atoms[0].value for s in node.find_all("symbol"))]


def props_of(node: sexpr.Node) -> dict[str, str]:
    return {p.atoms[0].value: p.atoms[1].value for p in node.find_all("property")}


def test_split_lib_id():
    assert split_lib_id("Device:R") == ("Device", "R")
    assert split_lib_id("Lib:A:B") == ("Lib", "A:B")
    assert split_lib_id("bare") == ("", "bare")


def test_library_names_and_root_symbol_model(lib: SymbolLibrary):
    assert lib.names() == ["Base", "Derived", "Chain", "Pwr", "Cyc1", "Cyc2", "Orphan"]
    assert (
        lib.version == 20251024 and "Base" in lib and not lib.is_derived("Base") and lib.is_derived("Chain")
    )
    base = lib.symbol("Base")
    assert base.lib_id == "Test:Base" and base.nickname == "Test" and base.name == "Base"
    assert base.units == 2 and base.body_styles == 2 and base.extends is None and base.power is None
    assert base.description == "Base part with two units" and base.keywords == "base test"
    assert base.footprint_filters == ("TP_*", "TH_?") and base.footprint == "Test:TP_2"
    assert base.datasheet.endswith("base.pdf") and base.reference_prefix == "U"
    assert base.pin_name_offset_mm == 0.254 and not base.pin_names_hidden and not base.pin_numbers_hidden
    assert base.in_bom and base.on_board and not base.exclude_from_sim
    # pins: library mm, Y-up, the `(at x y angle)` connection point (claim 3 input)
    by_num = {(p.number, p.unit, p.body_style): p for p in base.pins}
    p1 = by_num[("1", 1, 1)]
    assert (p1.x_mm, p1.y_mm, p1.orientation, p1.direction, p1.electrical_type, p1.name) == (
        -5.08,
        2.54,
        0,
        "R",
        "input",
        "IN",
    )
    assert by_num[("2", 1, 1)].orientation == 180 and by_num[("2", 1, 1)].direction == "L"
    assert by_num[("3", 2, 1)].direction == "D" and by_num[("3", 2, 1)].electrical_type == "power_in"
    assert by_num[("4", 2, 1)].hidden and by_num[("4", 2, 1)].direction == "U"
    assert ("1", 1, 2) in by_num  # De Morgan body style 2
    # pin_count = every unit, body style 1 (LIB_SYMBOL::GetPinCount)
    assert base.pin_count == 4
    assert [p.number for p in base.pins_for(unit=1)] == ["1", "2"]
    assert [p.number for p in base.pins_for(unit=2)] == ["3", "4"]
    assert [p.number for p in base.pins_for(unit=1, body_style=2)] == ["1"]
    # the embed node is renamed to Nick:Name, sub-units stay unprefixed, and it is a fresh copy per call
    assert names_in(base.node) == ["Test:Base", "Base_0_1", "Base_1_1", "Base_2_1", "Base_1_2"]
    assert base.node is not lib.symbol("Base").node and base.raw_node is not None
    assert base.raw_node.atoms[0].value == "Base"
    d = base.as_dict(unit=2)
    assert (
        d["pin_count"] == 4
        and [p["number"] for p in d["pins"]] == ["3", "4"]
        and d["pins"][0]["y_mm"] == 5.08
    )


def test_flatten_derived_follows_kicad_rules(lib: SymbolLibrary):
    derived = lib.symbol("Derived")
    node = derived.node
    assert names_in(node) == ["Test:Derived", "Derived_0_1", "Derived_1_1", "Derived_2_1", "Derived_1_2"]
    assert node.find("extends") is None
    props = props_of(node)
    # mandatory fields: the derived text wins only when non-empty
    assert props["Value"] == "Derived" and props["Datasheet"].endswith("derived.pdf")
    assert props["Footprint"] == "Test:TP_2" and props["Description"] == "Base part with two units"
    # user field added, root's user field kept, dcm info overridden when non-empty
    assert props["MPN"] == "DER-1" and props["Color"] == "red"
    assert props["ki_keywords"] == "derived test" and props["ki_fp_filters"] == "TP_2"
    # header flags come from the root; pins and units are the root's
    assert node.get("in_bom") == "yes" and node.find("pin_names") is not None
    assert derived.units == 2 and derived.pin_count == 4 and derived.extends == "Base"
    assert derived.footprint_filters == ("TP_2",) and derived.description == "Base part with two units"
    # property order as KiCad writes a flattened symbol: mandatory, then user fields, appended ones last
    assert list(props)[:5] == ["Reference", "Value", "Footprint", "Datasheet", "Description"]
    assert list(props).index("Color") < list(props).index("MPN") < list(props).index("ki_keywords")


def test_flatten_two_level_chain_applies_overrides_root_down(lib: SymbolLibrary):
    chain = lib.symbol("Chain")
    props = props_of(chain.node)
    assert names_in(chain.node)[0] == "Test:Chain" and "Chain_1_1" in names_in(chain.node)
    assert props["Value"] == "Chain" and props["Description"] == "Two levels down"
    assert props["Datasheet"].endswith("derived.pdf")  # Chain's empty Datasheet keeps Derived's
    assert props["Footprint"] == "Test:TP_2"  # empty at both derived levels -> root's
    assert props["Color"] == "blue" and props["MPN"] == "DER-1"  # Chain overrides Color, inherits MPN
    assert props["ki_fp_filters"] == "TP_2" and props["ki_keywords"] == "derived test"
    assert chain.extends == "Derived" and chain.pin_count == 4
    # the overriding user field moved to the end of the user fields (KiCad removes + appends)
    keys = list(props)
    assert keys.index("MPN") < keys.index("Color") < keys.index("ki_keywords")


def test_flatten_output_is_byte_stable_and_detached(lib: SymbolLibrary):
    a = sexpr.dumps(lib.symbol("Derived").node)
    b = sexpr.dumps(lib.symbol("Derived").node)
    assert a == b
    assert a.startswith('(symbol "Test:Derived"')
    # the library file itself is untouched by flattening
    assert sexpr.dumps(lib.root) == (FIXTURES / "Test.kicad_sym").read_text(encoding="utf-8")
    # a flattened node keeps the library's indentation for its depth (symbol at depth 1)
    assert '\n\t\t(property "Value" "Derived"' in a


def test_power_symbol_and_hidden_flags(lib: SymbolLibrary):
    pwr = lib.symbol("Pwr")
    assert pwr.power == "global" and pwr.is_power and pwr.exclude_from_sim
    assert pwr.pin_names_hidden and pwr.pin_numbers_hidden and pwr.pin_name_offset_mm == 0
    assert len(pwr.pins) == 1 and pwr.pins[0].hidden and pwr.pins[0].length_mm == 0
    assert pwr.reference_prefix == "#PWR" and pwr.keywords == "power-flag"


def test_flatten_cycles_and_missing_parents(lib: SymbolLibrary):
    cyc = lib.symbol("Cyc1")  # KiCad's cycle guard: the symbol itself, `extends` dropped
    assert cyc.node.find("extends") is None and names_in(cyc.node) == ["Test:Cyc1"]
    with pytest.raises(NotFound) as e:
        lib.symbol("Orphan")
    assert "Missing" in e.value.message and e.value.code == "E_LIB_NOT_FOUND"
    with pytest.raises(NotFound) as e2:
        lib.symbol("base")
    assert "Base" in e2.value.candidates
    with pytest.raises(NotFound):
        flatten(lib.by_name, "nothing")


def test_v7_dialect_bare_hide_and_ki_description():
    text = """(kicad_symbol_lib (version 20220914) (generator kicad_symbol_editor)
  (symbol "Old" (pin_names (offset 0.254) hide) (in_bom yes) (on_board yes)
    (property "Reference" "U" (id 0) (at 0 0 0) (effects (font (size 1.27 1.27))))
    (property "Value" "Old" (id 1) (at 0 0 0) (effects (font (size 1.27 1.27))))
    (property "ki_description" "seven" (id 4) (at 0 0 0) (effects (font (size 1.27 1.27)) hide))
    (symbol "Old_1_1"
      (pin passive line (at 0 2.54 270) (length 2.54) hide (name "~" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
    )
  )
)"""
    lib = SymbolLibrary.loads(text, nickname="V7")
    old = lib.symbol("Old")
    assert old.description == "seven" and old.pin_names_hidden and old.pins[0].hidden
    assert old.pins[0].shape == "line" and old.pins[0].shown_name == "" and old.pins[0].name == "~"
    with pytest.raises(ValueError):
        SymbolLibrary.loads("(kicad_sch (version 1))")


def test_parse_symbol_without_nickname_keeps_plain_name(lib: SymbolLibrary):
    sym = parse_symbol(flatten(lib.by_name, "Base"), nickname="")
    assert sym.lib_id == "Base" and sym.node.atoms[0].value == "Base"


# ---- footprints -----------------------------------------------------------------------------------


def test_footprint_parsing_pads_courtyard_attributes():
    assert footprint_names(FIXTURES / "Test.pretty") == ["TH_4", "TP_2"]
    fp = load_footprint(FIXTURES / "Test.pretty", "TP_2", nickname="Test")
    assert fp.fpid == "Test:TP_2" and fp.name == "TP_2" and fp.attributes == ("smd",) and fp.is_smd
    assert fp.description.startswith("Two-pad") and fp.tags == "test smd pad" and fp.layer == "F.Cu"
    assert fp.pad_count == 2 and len(fp.pads) == 2
    p1, p2 = fp.pads
    assert (p1.number, p1.type, p1.shape, p1.at_mm, p1.size_mm) == (
        "1",
        "smd",
        "roundrect",
        (-0.8, 0.0),
        (0.8, 0.9),
    )
    assert p1.layers == ("F.Cu", "F.Mask", "F.Paste") and p1.drill is None and p2.rotation == 90
    assert fp.courtyard == (((-1.5, -0.75), (1.5, -0.75), (1.5, 0.75), (-1.5, 0.75)),)
    assert fp.courtyard_open == () and fp.bbox_mm == (-1.5, -0.75, 1.5, 0.75)
    assert fp.models == ("${KICAD10_3DMODEL_DIR}/Test.3dshapes/TP_2.wrl",)
    assert fp.properties["Value"] == "TP_2"
    d = fp.as_dict()
    assert d["pads"][0]["x_mm"] == -0.8 and d["courtyard"][0][0] == [-1.5, -0.75]


def test_footprint_numbered_pad_count_follows_kicad():
    fp = load_footprint(FIXTURES / "Test.pretty", "TH_4", nickname="Test")
    assert fp.is_through_hole and len(fp.pads) == 7
    # 1, 2, 3 + A1_B12 (counts twice); MP (no digits), the NPTH and the paste-only pad 9 are excluded
    assert fp.pad_count == 5 and numbered_pad_count(fp.pads) == 5
    by = {p.number: p for p in fp.pads}
    assert by["1"].drill == 0.8 and by["1"].type == "thru_hole" and by["1"].layers == ("*.Cu", "*.Mask")
    assert by["3"].drill == 0.8 and by["3"].drill_slot_mm == (0.8, 1.4)
    assert by[""].type == "np_thru_hole"
    assert fp.courtyard[0] == ((-4.0, -2.0), (4.0, -2.0), (4.0, 2.0), (-4.0, 2.0))


def test_resolver_end_to_end_on_fixture_tables():
    resolver = LibResolver(StubInstall(), FIXTURES, None, env=env_for(FIXTURES))
    sym = resolver.symbol("Test:Derived")
    assert sym.lib_id == "Test:Derived" and sym.path == FIXTURES / "Test.kicad_sym"
    assert resolver.symbol("Inner:Base").lib_id == "Inner:Base"  # nested-table nickname
    assert resolver.symbol("Hidden:Base").nickname == "Hidden"  # hidden rows still resolve
    fp = resolver.footprint("Test:TH_4")
    assert fp.fpid == "Test:TH_4" and resolver.footprint("Test:TH_4") is fp  # cached
    assert resolver.symbol_names("Test")[0] == "Base" and resolver.footprint_names("Test") == ["TH_4", "TP_2"]
    assert resolver.matching_footprints(("TP_*", "TH_?")) == ["Test:TH_4", "Test:TP_2"]
    assert resolver.matching_footprints(("test:tp_2",)) == ["Test:TP_2"]
    assert resolver.matching_footprints(("Other:TP_*",)) == []
    with pytest.raises(NotFound) as e:
        resolver.footprint("Test:TP_3")
    assert "TP_2" not in e.value.candidates and e.value.candidates == []
    with pytest.raises(NotFound):
        resolver.footprint("TP_2")


def test_symbol_cache_invalidates_on_file_change(tmp_path: Path):
    src = (FIXTURES / "Test.kicad_sym").read_text(encoding="utf-8")
    lib_path = tmp_path / "T.kicad_sym"
    lib_path.write_text(src, encoding="utf-8")
    (tmp_path / "sym-lib-table").write_text(
        '(sym_lib_table (version 7) (lib (name "T") (type "KiCad") (uri "${KIPRJMOD}/T.kicad_sym") (options "") (descr "")))\n',
        encoding="utf-8",
    )
    (tmp_path / "fp-lib-table").write_text("(fp_lib_table (version 7))\n", encoding="utf-8")
    resolver = LibResolver(StubInstall(), tmp_path, None, env={"KIPRJMOD": str(tmp_path)})
    assert resolver.symbol("T:Base").description == "Base part with two units"
    import os
    import time

    lib_path.write_text(src.replace("Base part with two units", "changed"), encoding="utf-8")
    future = time.time() + 5
    os.utime(lib_path, (future, future))
    assert resolver.symbol("T:Base").description == "changed"
