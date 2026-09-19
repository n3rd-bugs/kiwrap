"""`sch export netlist --format kicadsexpr` on 7.0-10.0 against the ecc83 demo, plus the adapter's
parse path (`kiwrap.formats.reports.netlist`) offline."""

from __future__ import annotations

from pathlib import Path

import pytest

from kiwrap.backends.cli import netlist
from kiwrap.formats.reports import Netlist
from tests._fakes import (
    demo_project,
    env_for,
)

_SAMPLE = """
(export (version "E")
  (design (source "x.kicad_sch") (date "d") (tool "Eeschema 10.0.0"))
  (components
    (comp (ref "R1") (value "10k") (footprint "Resistor_SMD:R_0603_1608Metric")
      (libsource (lib "Device") (part "R") (description "Resistor"))
      (property (name "Sheetname") (value ""))
      (property (name "dnp"))
      (sheetpath (names "/") (tstamps "/"))
      (tstamps "aaaa"))
    (comp (ref "C1") (value "100n") (footprint "Capacitor_SMD:C_0603_1608Metric")
      (libsource (lib "Device") (part "C") (description "Cap \\"quoted\\""))
      (sheetpath (names "/sub/") (tstamps "/x/"))
      (tstamps "bbbb")))
  (libparts)
  (libraries)
  (nets
    (net (code "1") (name "GND")
      (node (ref "C1") (pin "2") (pintype "passive"))
      (node (ref "R1") (pin "2") (pinfunction "K") (pintype "passive")))
    (net (code "2") (name "/sub/SIG")
      (node (ref "R1") (pin "1") (pintype "passive")))))
"""


def test_parse_netlist_text_components_and_nets():
    n = netlist.parse_netlist_text(_SAMPLE, source="x.kicad_sch", kicad_version="10.0.0")
    assert isinstance(n, Netlist)
    assert [c.ref for c in n.components] == ["R1", "C1"]
    r1 = n.component("R1")
    assert r1.value == "10k" and r1.footprint == "Resistor_SMD:R_0603_1608Metric"
    assert r1.lib_id == "Device:R" and r1.sheet_path == "/" and r1.dnp is True
    assert n.component("C1").sheet_path == "/sub/" and n.component("C1").dnp is False
    gnd = n.net_by_name()["GND"]
    assert gnd.code == 1 and {(x.ref, x.pin) for x in gnd.nodes} == {("C1", "2"), ("R1", "2")}
    assert gnd.nodes[1].pin_function == "K" and gnd.nodes[1].pin_type == "passive"
    assert n.net_by_name()["/sub/SIG"].nodes[0].ref == "R1"
    assert netlist.netlist_summary(n) == {"components": 2, "nets": 2, "nodes": 3}


def test_parse_netlist_text_handles_unquoted_v7_atoms():
    text = "(export (version D) (components (comp (ref R1) (value 1k))) (nets (net (code 1) (name N) (node (ref R1) (pin 1)))))"
    n = netlist.parse_netlist_text(text, source="s", kicad_version="7.0.6")
    assert n.components[0].ref == "R1" and n.nets[0].nodes[0].pin == "1"


_ALL = [
    pytest.param("7.0", marks=pytest.mark.kicad_version("7.0")),
    pytest.param("8.0", marks=pytest.mark.kicad_version("8.0")),
    pytest.param("9.0", marks=pytest.mark.kicad_version("9.0")),
    pytest.param("10.0", marks=pytest.mark.kicad_version("10.0")),
]


@pytest.mark.kicad
@pytest.mark.usefixtures("user_config_untouched")
@pytest.mark.parametrize("mm", _ALL)
def test_export_netlist_demo(cli_envs, mm, tmp_path: Path):
    env = env_for(cli_envs, mm)
    proj = demo_project(env.install, tmp_path)
    sch = proj / "ecc83-pp.kicad_sch"
    out = tmp_path / "out" / "ecc83.net"
    n = netlist.export_netlist(env.install, env.config, sch, out, caps=env.caps)
    assert out.is_file() and out.read_text(encoding="utf-8").lstrip().startswith("(export")
    assert n.kicad_version == env.install.version and n.source.endswith("ecc83-pp.kicad_sch")
    assert len(n.components) == 15 and len(n.nets) == 13  # the ecc83 demo, identical on 7-10
    assert "GND" in n.net_by_name()
    assert n.component("C1") is not None and n.component("C1").footprint.startswith("Capacitor_THT:")
    assert not list(proj.glob("*.kicad_prl"))


@pytest.mark.kicad
@pytest.mark.kicad_version("10.0")
@pytest.mark.usefixtures("user_config_untouched")
def test_export_netlist_default_output_path(cli_envs, tmp_path: Path):
    env = env_for(cli_envs, "10.0")
    proj = demo_project(env.install, tmp_path)
    path = netlist.export_netlist_file(env.install, env.config, proj / "ecc83-pp.kicad_sch", caps=env.caps)
    assert path == proj / "ecc83-pp.net" and path.is_file()
