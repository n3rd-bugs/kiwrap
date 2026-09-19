"""Library resolution + index against the installed KiCads, with KiCad as the judge.

* The index builds over every install's shipped libraries (time budget asserted) and the phase
  brief's reference searches (LM358, ESP32-S3-WROOM-1, USB_C_Receptacle, R_0603) hit.
* `Device:R`'s footprint filters resolve `R_0603_1608Metric`.
* Flattened symbols (simple, derived, multi-unit derived, power, big connector) embedded into a
  generated schematic in the install's own dialect pass `kicad-cli sch erc` without
  `lib_symbol_mismatch` / `lib_symbol_issues` and export a netlist listing every visible pin -
  the failure modes docs/VERIFICATION.md claim 2 documented for a wrong flatten (pinless symbol,
  `lib_symbol_mismatch`, component dropped from the netlist). On installs with `sch upgrade`
  (10.0+) the re-saved `lib_symbols` entry is compared structurally with kiwrap's flatten.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path

import pytest

from kiwrap.backends.cli.erc import run_erc
from kiwrap.backends.cli.netlist import export_netlist
from kiwrap.backends.cli.upgrade import upgrade_copy
from kiwrap.capability import capabilities_for
from kiwrap.formats import sexpr
from kiwrap.formats.geometry import Vec2
from kiwrap.formats.libs import LibIndex, LibResolver, LibSymbol
from kiwrap.formats.schematic import pin_sheet_position

pytestmark = pytest.mark.kicad

REFERENCE_SYMBOLS = (
    "Device:R",
    "power:GND",
    "Amplifier_Operational:LM358",  # extends LM2904, 3 units
    "Regulator_Linear:AMS1117-3.3",  # extends AP1117-15 (claim 2's symbol)
    "Transistor_BJT:2N2219",  # extends Q_NPN_EBC (spike exp_extends.py)
    "Connector:USB_C_Receptacle",
    "RF_Module:ESP32-S3-WROOM-1",
)


def _caps(install):
    return capabilities_for(install)


def _skip_unless_libs(install):
    if not (install.share / "symbols").is_dir() or not (install.share / "footprints").is_dir():
        pytest.skip(f"{install.version} ships no libraries")


@pytest.fixture(scope="module")
def indexes(installs, storage_home, tmp_path_factory):
    """One built index per install (>= 9; 7/8 best-effort are skipped when their libs lack the
    Description property the searches rely on)."""
    from kiwrap import sandbox

    out = {}
    for install in installs:
        if install.major < 9:
            continue
        if not (install.share / "symbols").is_dir():
            continue
        root = sandbox.ensure(install, storage_home=storage_home)
        idx = LibIndex(install, root, storage_home)
        t0 = time.perf_counter()
        rep = idx.build()
        out[install.version + "@" + install.root.name] = (install, root, idx, rep, time.perf_counter() - t0)
    if not out:
        pytest.skip("no KiCad >= 9 with libraries installed")
    return out


def test_index_builds_over_the_shipped_libraries_within_budget(indexes):
    for key, (_install, _root, idx, rep, seconds) in indexes.items():
        assert rep.symbols > 15_000 and rep.footprints > 10_000, (key, rep.as_dict())
        assert seconds < 90, f"{key}: index build took {seconds:.1f}s"
        assert idx.ensure().skipped
        again = idx.build()
        assert again.scanned == 0 and again.unchanged == rep.libraries, key
        assert not [p for p in rep.problems if "does not exist" in p and "3RD_PARTY" not in p], (
            key,
            rep.problems,
        )


@pytest.mark.parametrize(
    ("kind", "query", "expect_id"),
    [
        ("symbol", "LM358", "Amplifier_Operational:LM358"),
        ("symbol", "ESP32-S3-WROOM-1", "RF_Module:ESP32-S3-WROOM-1"),
        ("symbol", "USB_C_Receptacle", "Connector:USB_C_Receptacle"),
        ("symbol", "GND", "power:GND"),
        ("symbol", "resistor", "Device:R"),
        ("footprint", "R_0603", "Resistor_SMD:R_0603_1608Metric"),
        ("footprint", "SOIC-8 3.9x4.9", "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm"),
    ],
)
def test_reference_searches_rank_the_expected_item_first(indexes, kind, query, expect_id):
    for key, (_install, _root, idx, _rep, _s) in indexes.items():
        res = idx.query(kind, query, limit=5)
        assert res.hits, (key, kind, query, res.strategy, [m.id for m in res.near_misses])
        assert res.hits[0].id == expect_id, (key, [h.id for h in res.hits])
        if kind == "symbol":
            assert res.hits[0].pin_count and res.hits[0].pin_count > 0


def test_filters_and_near_misses_on_real_libraries(indexes):
    for key, (_install, _root, idx, _rep, _s) in indexes.items():
        res = idx.query("symbol", "LM358", {"pins": 14})
        assert res.hits == [] and res.near_misses[0].id == "Amplifier_Operational:LM358"
        assert res.near_misses[0].miss == "8 pins (wanted 14)", key
        assert idx.search("symbol", "LM358", {"pins": 8})[0].units == 3
        fp = idx.query("footprint", "USB_C_Receptacle", {"pins": 24})
        assert all(h.pad_count == 24 for h in fp.hits) and fp.near_misses, key  # 9's libs ship no 24-pad one
        assert all(m.miss and m.miss.endswith("pads (wanted 24)") for m in fp.near_misses), key
        assert idx.search("footprint", "R_0603", {"smd": True})[0].id == "Resistor_SMD:R_0603_1608Metric"
        assert idx.query("footprint", "R_0603", {"through_hole": True}).near_misses[0].miss == "SMD footprint"


def test_footprint_filters_resolve_r_0603(indexes):
    for key, (_install, _root, idx, _rep, _s) in indexes.items():
        r = idx.resolver.symbol("Device:R")
        assert r.footprint_filters == ("R_*",)
        matches = idx.footprints_for(r.footprint_filters, pin_count=r.pin_count, limit=5000)
        ids = [m["id"] for m in matches]
        assert "Resistor_SMD:R_0603_1608Metric" in ids, key
        two_pad = [m for m in matches if m["id"] == "Resistor_SMD:R_0603_1608Metric"][0]
        assert two_pad["pad_count"] == 2 and two_pad["pin_count_match"]
        # the directory-walking resolver agrees with the index
        assert "Resistor_SMD:R_0603_1608Metric" in idx.resolver.matching_footprints(
            r.footprint_filters, limit=10000
        )
        lm = idx.resolver.symbol("Amplifier_Operational:LM358")
        soic = [m["id"] for m in idx.footprints_for(lm.footprint_filters, pin_count=8, limit=500)]
        assert "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm" in soic and "Package_DIP:DIP-8_W7.62mm" in soic


def test_resolver_models_on_real_libraries(indexes):
    for key, (install, _root, idx, _rep, _s) in indexes.items():
        res = idx.resolver
        r = res.symbol("Device:R")
        assert [(p.number, p.x_mm, p.y_mm, p.orientation) for p in r.pins] == [
            ("1", 0.0, 3.81, 270),
            ("2", 0.0, -3.81, 90),
        ]
        lm = res.symbol("Amplifier_Operational:LM358")
        assert lm.extends == "LM2904" and lm.units == 3 and lm.pin_count == 8
        assert lm.node.atoms[0].value == "Amplifier_Operational:LM358"
        assert [s.atoms[0].value for s in lm.node.find_all("symbol")] == [
            "LM358_1_1",
            "LM358_2_1",
            "LM358_3_1",
        ]
        assert lm.node.find("extends") is None
        assert res.symbol("power:GND").power == "global"
        fp = res.footprint("Resistor_SMD:R_0603_1608Metric")
        assert fp.pad_count == 2 and fp.is_smd and fp.courtyard and fp.bbox_mm is not None
        assert [p.number for p in fp.pads] == ["1", "2"] and fp.pads[0].size_mm == (0.8, 0.95)
        tables = res.tables()
        assert {r["name"] for r in tables["sym"]} >= {"Device", "power", "Amplifier_Operational"}
        assert all(
            r["exists"] for r in tables["sym"] if r["type"] == "KiCad" and "3RD_PARTY" not in r["uri"]
        ), key
        assert install.version in key


# ---- KiCad as the judge: embed flattened symbols and run ERC / netlist ------------------------------


def _mm(v: float) -> float:
    return round(v, 4)


def _snap(v: float) -> float:
    return round(round(v / 1.27) * 1.27, 4)


class SchBuilder:
    """A minimal `.kicad_sch` in the target install's dialect: the writer rules of ARCHITECTURE
    section 7 reduced to what this test needs (lib_symbols entries, symbol instances with the
    instances block, one wire + local label per visible pin so every pin lands on its own net)."""

    def __init__(self, epoch: int, generator_version: str, name: str) -> None:
        self.root_uuid = str(uuid.uuid4())
        self.name = name
        self.epoch = epoch
        self.doc = sexpr.build("kicad_sch")
        self.doc.append(sexpr.build("version", epoch))
        self.doc.append(sexpr.build("generator", "eeschema"))
        self.doc.append(sexpr.build("generator_version", generator_version))
        self.doc.append(sexpr.build("uuid", self.root_uuid))
        self.doc.append(sexpr.build("paper", "A3"))
        self.libs = sexpr.build("lib_symbols")
        self.doc.append(self.libs)
        self.embedded: set[str] = set()
        self.refs: dict[str, int] = {}
        self.expect: dict[str, set[str]] = {}
        self.pos = [25.4, 25.4]

    def _prop(self, name: str, value: str, x: float, y: float, hide: bool) -> sexpr.Node:
        p = sexpr.build("property", name, value)
        p.append(sexpr.build("at", _mm(x), _mm(y), 0))
        if hide:
            p.append(sexpr.build("hide", sexpr.Atom.symbol("yes")))
        p.append(sexpr.build("effects", sexpr.build("font", sexpr.build("size", 1.27, 1.27))))
        return p

    def place(self, sym: LibSymbol, ref: str, unit: int = 1) -> None:
        if sym.lib_id not in self.embedded:
            entry = self.libs.append(sym.node)
            entry.reformat()  # copied from a library at depth 1; lib_symbols entries sit at depth 2
            self.embedded.add(sym.lib_id)
        x, y = self.pos
        pins = [p for p in sym.pins_for(unit=unit) if not p.hidden]
        node = sexpr.build("symbol")
        node.append(sexpr.build("lib_id", sym.lib_id))
        node.append(sexpr.build("at", _mm(x), _mm(y), 0))
        node.append(sexpr.build("unit", unit))
        if self.epoch >= 20260306:  # `body_style` is a 10.0 token (9.0 files carry none)
            node.append(sexpr.build("body_style", 1))
        node.append(sexpr.build("exclude_from_sim", sexpr.Atom.symbol("no")))
        node.append(sexpr.build("in_bom", sexpr.Atom.symbol("yes")))
        node.append(sexpr.build("on_board", sexpr.Atom.symbol("yes")))
        if self.epoch >= 20260306:
            node.append(sexpr.build("in_pos_files", sexpr.Atom.symbol("yes")))
        node.append(sexpr.build("dnp", sexpr.Atom.symbol("no")))
        node.append(sexpr.build("uuid", str(uuid.uuid4())))
        node.append(self._prop("Reference", ref, x + 2.54, y - 2.54, False))
        node.append(self._prop("Value", sym.properties.get("Value", sym.name), x + 2.54, y, False))
        node.append(self._prop("Footprint", sym.footprint, x, y, True))
        node.append(self._prop("Datasheet", sym.datasheet, x, y, True))
        node.append(self._prop("Description", sym.description, x, y, True))
        for p in sym.pins_for(unit=unit):
            pin = sexpr.build("pin", p.number)
            pin.append(sexpr.build("uuid", str(uuid.uuid4())))
            node.append(pin)
        inst = sexpr.build(
            "instances",
            sexpr.build(
                "project",
                self.name,
                sexpr.build(
                    "path", "/" + self.root_uuid, sexpr.build("reference", ref), sexpr.build("unit", unit)
                ),
            ),
        )
        node.append(inst)
        self.doc.append(node)
        expect = self.expect.setdefault(ref, set())
        for p in pins:
            sheet = pin_sheet_position(Vec2(x, y), 0, None, Vec2(p.x_mm, p.y_mm))
            dx = 12.7 if p.direction == "L" else -12.7 if p.direction == "R" else 0.0
            dy = 12.7 if p.direction == "U" else -12.7 if p.direction == "D" else 0.0
            end = (_snap(sheet.x + dx), _snap(sheet.y + dy))
            wire = sexpr.build(
                "wire",
                sexpr.build("pts", sexpr.build("xy", _mm(sheet.x), _mm(sheet.y)), sexpr.build("xy", *end)),
                sexpr.build(
                    "stroke", sexpr.build("width", 0), sexpr.build("type", sexpr.Atom.symbol("default"))
                ),
                sexpr.build("uuid", str(uuid.uuid4())),
            )
            self.doc.append(wire)
            label = sexpr.build("label", f"{ref}_{p.number}")
            label.append(sexpr.build("at", *end, 0))
            label.append(
                sexpr.build(
                    "effects",
                    sexpr.build("font", sexpr.build("size", 1.27, 1.27)),
                    sexpr.build("justify", sexpr.Atom.symbol("left"), sexpr.Atom.symbol("bottom")),
                )
            )
            label.append(sexpr.build("uuid", str(uuid.uuid4())))
            self.doc.append(label)
            expect.add(p.number)
        self.pos[0] += 76.2
        if self.pos[0] > 330:
            self.pos = [25.4, self.pos[1] + 101.6]

    def write(self, path: Path) -> Path:
        self.doc.append(sexpr.build("sheet_instances", sexpr.build("path", "/", sexpr.build("page", "1"))))
        if self.epoch >= 20250114:
            self.doc.append(sexpr.build("embedded_fonts", sexpr.Atom.symbol("no")))
        path.write_text(sexpr.dumps(self.doc) + "\n", encoding="utf-8")
        return path


def _structure(node: sexpr.Node):
    return tuple(_structure(c) if isinstance(c, sexpr.Node) else c.value for c in node.children)


@pytest.mark.kicad
def test_flattened_symbols_pass_erc_and_netlist_on_every_install(install, storage_home, tmp_path):
    from kiwrap import sandbox

    caps = _caps(install)
    if not caps.has("cli.has_erc") or install.major < 9:
        pytest.skip(f"{install.version}: no ERC or best-effort tier")
    _skip_unless_libs(install)
    epoch = int(caps.get("formats.sch") or 0)
    assert epoch, "capability record lacks formats.sch"
    root = sandbox.ensure(install, storage_home=storage_home)
    resolver = LibResolver(install, root, None)
    project = tmp_path / "libcheck"
    project.mkdir()
    (project / "libcheck.kicad_pro").write_text(
        '{"meta": {"filename": "libcheck.kicad_pro", "version": 1}}\n', encoding="utf-8"
    )
    builder = SchBuilder(epoch, install.major_minor, "libcheck")
    placed: list[tuple[str, str, int]] = []
    n = 1
    for lib_id in REFERENCE_SYMBOLS:
        sym = resolver.symbol(lib_id)
        for unit in range(1, sym.units + 1):
            ref = f"{sym.reference_prefix or 'U'}{n}"
            builder.place(sym, ref, unit)
            placed.append((lib_id, ref, unit))
        n += 1
    sch = builder.write(project / "libcheck.kicad_sch")

    report = run_erc(install, root, sch, caps=caps, timeout_s=300)
    bad = [v for v in report.violations if v.type in ("lib_symbol_mismatch", "lib_symbol_issues")]
    assert not bad, [(v.type, v.description) for v in bad]

    netlist = export_netlist(install, root, sch, caps=caps, timeout_s=300)
    refs = {c.ref for c in netlist.components}
    seen: dict[str, set[str]] = {}
    for net in netlist.nets:
        for node in net.nodes:
            seen.setdefault(node.ref, set()).add(node.pin)
    for lib_id, ref, _unit in placed:
        if lib_id.startswith("power:"):
            continue  # power symbols are not components
        assert ref in refs, (lib_id, ref)
        comp = netlist.component(ref)
        assert comp is not None and comp.lib_id == lib_id
        missing = builder.expect[ref] - seen.get(ref, set())
        assert not missing, (install.version, lib_id, ref, sorted(missing))

    if caps.has("cli.has_sch_pcb_upgrade"):
        resaved = upgrade_copy(install, root, sch, tmp_path / "resave", caps=caps, timeout_s=300)
        doc = sexpr.load(resaved)
        libs = doc.find("lib_symbols")
        assert libs is not None
        theirs = {s.atoms[0].value: s for s in libs.find_all("symbol")}
        for lib_id in REFERENCE_SYMBOLS:
            mine = resolver.symbol(lib_id).node
            assert lib_id in theirs, lib_id
            assert _structure(theirs[lib_id]) == _structure(mine), (
                f"{install.version}: {lib_id} differs after re-save"
            )
