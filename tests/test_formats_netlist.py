"""Unit tests for kiwrap.formats.reports.netlist against real kicad-cli 7.0 - 10.0 exports."""

from __future__ import annotations

from pathlib import Path

import pytest

from kiwrap.formats.reports import Component, Net, NetNode
from kiwrap.formats.reports.netlist import parse_netlist

CORPUS = Path(__file__).resolve().parents[1] / "conformance" / "corpus"


def _read(rel: str) -> str:
    return (CORPUS / rel).read_text(encoding="utf-8")


def test_kicad10_netlist_components_and_nets():
    n = parse_netlist(_read("10.0/ldo_test/net1.net"), source="ldo_test.kicad_sch")
    assert n.source == "ldo_test.kicad_sch" and n.kicad_version == "10.0.0"
    assert [c.ref for c in n.components] == ["C1", "C2", "D1", "J1", "R1", "U1"]
    u1 = n.component("U1")
    assert u1 is not None
    assert u1.value == "AMS1117-3.3"
    assert u1.footprint == "Package_TO_SOT_SMD:SOT-223-3_TabPin2"
    assert u1.lib_id == "Regulator_Linear:AMS1117-3.3"
    assert u1.sheet_path == "/"
    assert u1.properties["Sheetfile"] == "ldo_test.kicad_sch"
    assert u1.properties["Datasheet"].startswith("http")
    assert u1.properties["ki_fp_filters"] == "SOT?223*TabPin2*"
    assert not u1.dnp and not u1.exclude_from_board
    assert n.component("nope") is None
    nets = n.net_by_name()
    assert set(nets) == {"+3V3", "GND", "Net-(D1-K)", "Net-(J1-Pin_1)"}
    assert nets["+3V3"] == Net(
        code=1,
        name="+3V3",
        nodes=(
            NetNode(ref="C2", pin="1", pin_function=None, pin_type="passive"),
            NetNode(ref="R1", pin="1", pin_function=None, pin_type="passive"),
            NetNode(ref="U1", pin="2", pin_function="VO_2", pin_type="power_out"),
        ),
    )
    assert {nd.ref + "." + nd.pin for nd in nets["GND"].nodes} == {"C1.2", "C2.2", "D1.2", "J1.2", "U1.1"}


@pytest.mark.parametrize(
    ("rel", "version"),
    [
        ("7.0/led_d7/netlist_7.0.net", "7.0.6"),
        ("8.0/led_d8/netlist_8.0.net", "8.0.1"),
        ("9.0/led_d9/netlist_9.0.net", "9.0.5"),
        ("10.0/led_d10/netlist_10.0.net", "10.0.0"),
    ],
)
def test_same_design_parses_identically_across_versions(rel, version):
    n = parse_netlist(_read(rel))
    assert n.kicad_version == version
    assert sorted(c.ref for c in n.components) == ["D1", "J1", "R1"]
    assert all(c.lib_id and ":" in c.lib_id for c in n.components)
    assert sorted(net.name for net in n.nets) == ["/LED_A", "GND", "VIN"]  # local labels are sheet-scoped
    by_name = n.net_by_name()
    assert {f"{x.ref}.{x.pin}" for x in by_name["/LED_A"].nodes} == {"D1.2", "R1.2"}
    assert all(net.code > 0 for net in n.nets)


def test_source_and_version_fallbacks_and_dnp_flag():
    text = """(export (version "E")
  (design (source "C:\\\\proj\\\\a.kicad_sch") (date "x") (tool "Eeschema 8.0.1"))
  (components
    (comp (ref "R1") (value "10k")
      (fields (field (name "Footprint")) (field (name "Description") "Resistor"))
      (libsource (lib "Device") (part "R") (description ""))
      (property (name "Sheetname") (value ""))
      (property (name "dnp"))
      (property (name "exclude_from_board"))
      (sheetpath (names "/sub/") (tstamps "/abc/"))
      (tstamps "u1"))
    (comp (ref "J1") (value "Conn") (footprint "Lib:FP") (libsource (lib "") (part "P"))))
  (nets
    (net (code "1") (name "N1") (node (ref "R1") (pin "1")))
    (net (code "2") (name "") (class "Default"))))
"""
    n = parse_netlist(text)
    assert n.source == "C:\\proj\\a.kicad_sch" and n.kicad_version == "8.0.1"
    r1 = n.component("R1")
    assert r1 == Component(
        ref="R1",
        value="10k",
        footprint=None,
        lib_id="Device:R",
        sheet_path="/sub/",
        properties={"Footprint": "", "Description": "Resistor", "Sheetname": ""},
        dnp=True,
        exclude_from_board=True,
    )
    j1 = n.component("J1")
    assert j1.footprint == "Lib:FP" and j1.lib_id == "P" and j1.sheet_path is None
    assert n.nets[0].nodes == (NetNode(ref="R1", pin="1"),)
    assert n.nets[1] == Net(code=2, name="", nodes=())


def test_rejects_non_netlist():
    with pytest.raises(ValueError, match="not a KiCad netlist"):
        parse_netlist("(kicad_sch (version 1))")


def test_empty_sections():
    n = parse_netlist('(export (version "E") (design (tool "Eeschema 10.0.0")) (components) (nets))')
    assert n.components == () and n.nets == () and n.kicad_version == "10.0.0"
    n = parse_netlist('(export (version "E"))')
    assert n.components == () and n.nets == () and n.source == "" and n.kicad_version == ""
