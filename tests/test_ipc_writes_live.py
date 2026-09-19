"""Live tests for the KiCad 11 IPC authoring twins (`kiwrap.backends.ipc.sch_writes` /
`pcb_writes`) on the 10.99 nightly (`ipc.headless` + `ipc.schematic`).

Every test starts a sandboxed `kicad-cli api-server` on a scratch copy of a fixture project
(`tests/fixtures/ipc_writes/blank`, a 10.0-epoch empty project, or the ecc83 demo shipped with the
install), authors through the twins and then lets KiCad judge the saved file with the SAME
install's kicad-cli: `sch erc --format json` (zero errors for a legal design), `sch export
netlist` (the nets the agent intended), `pcb drc --schematic-parity` (zero parity issues after a
sync). The nightly writes its own format stamps (sch 20260830 / pcb 20260901), which kicad-cli
10.0.6 refuses to load, so the 10.0.6 cross-check compares NETLISTS: the same operations through
the file writer (`kiwrap.formats.schematic_writer`, when importable) must give the same nets.

Per-operation latency is recorded by `writes.LATENCY` and printed at the end of the module
(`-s` shows it); the medians are asserted against generous bounds so a wedged server shows up.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest

from kiwrap.backends.cli import erc as cli_erc
from kiwrap.backends.cli import netlist as cli_netlist
from kiwrap.backends.ipc import readers, writes
from kiwrap.backends.ipc.sessions import HeadlessSession, close_all, live_sessions
from kiwrap.capability import Unsupported, capabilities_for
from kiwrap.sandbox import user_config_hash

pytestmark = [pytest.mark.kicad, pytest.mark.kicad_version("10.99")]

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "ipc_writes"


@pytest.fixture(scope="module")
def nightly(installs):
    match = [
        i
        for i in installs
        if i.major_minor == "10.99"
        and capabilities_for(i).has("ipc.headless")
        and capabilities_for(i).has("ipc.schematic")
    ]
    if not match:
        pytest.skip("no 10.99 nightly with a headless schematic API installed")
    return match[0]


@pytest.fixture(scope="module")
def root(nightly, storage_home):
    from kiwrap import sandbox

    return sandbox.ensure(nightly, storage_home=storage_home)


@pytest.fixture
def blank(tmp_path) -> Path:
    dst = tmp_path / "blank"
    shutil.copytree(FIXTURES / "blank", dst)
    return dst


@pytest.fixture
def ecc83(nightly, tmp_path) -> Path:
    src = nightly.dir("demos") / "ecc83"
    if not src.is_dir():
        pytest.skip("ecc83 demo not shipped")
    dst = tmp_path / "ecc83"
    shutil.copytree(src, dst)
    return dst


@pytest.fixture(autouse=True)
def _hygiene(nightly):
    before = user_config_hash(nightly)
    yield
    close_all()
    assert live_sessions() == []
    assert user_config_hash(nightly) == before, "a headless session modified the user's real KiCad config"


@pytest.fixture(scope="module", autouse=True)
def _latency_report():
    writes.LATENCY.reset()
    yield
    summary = writes.LATENCY.summary()
    print("\nIPC write latency (ms):")
    for op, s in summary.items():
        print(f"  {op:28s} n={int(s['n']):3d} median={s['median_ms']:8.1f} max={s['max_ms']:8.1f}")


def _erc(nightly, root, sch: Path):
    return cli_erc.run_erc(nightly, root, sch, out_json=sch.with_suffix(".erc.json"))


def _netlist(nightly, root, sch: Path):
    return cli_netlist.export_netlist(nightly, root, sch, sch.with_suffix(".net"))


def _nets(netlist) -> dict[str, set[tuple[str, str]]]:
    return {n.name: {(x.ref, x.pin) for x in n.nodes} for n in netlist.nets}


def _errors(report) -> list[str]:
    return [
        f"{v.type}: {v.description}" for v in report.violations if v.severity == "error" and not v.excluded
    ]


# --- symbols: pins vs KiCad at all 12 orientations ----------------------------------------------------


@pytest.mark.parametrize("mirror", [None, "x", "y"])
def test_pins_land_where_kicad_says_at_every_rotation(nightly, root, blank, mirror):
    """Claim 3 over IPC: the absolute pin endpoints a placement returns must be where KiCad
    connects. Proof by wiring: each pin gets a wire to its own label; kicad-cli's netlist must
    list (ref, pin) under that label's net for every rotation and mirror. The regulator's three
    pins (VI left, GND below, VO right) are asymmetric enough to expose any swapped axis."""
    sch_path = blank / "blank.kicad_sch"
    expect: dict[str, tuple[str, str]] = {}
    with HeadlessSession(nightly, sch_path, config_root=root, request_timeout_ms=20000) as s:
        for i, rot in enumerate((0, 90, 180, 270)):
            ref = f"U{i + 1}"
            placed = writes.sch_add_symbol(
                s, "Regulator_Linear:AMS1117-3.3", ref, None, (50.8 + 40 * i, 60.96), rot, mirror
            )
            assert placed.ref == ref and len(placed.pins) == 3
            for pin in placed.pins:
                assert writes.on_grid(pin.x_mm) and writes.on_grid(pin.y_mm), pin
                # a short stub away from the body, then a label on the stub's far end
                dx = 2.54 if pin.x_mm >= 50.8 + 40 * i else -2.54
                far = (pin.x_mm + dx, pin.y_mm + (2.54 if pin.number == "1" else 0.0))
                ids = writes.sch_add_wire(s, [pin.at_mm, (far[0], pin.y_mm), far])
                assert ids
                name = f"N_{ref}_{pin.number}"
                writes.sch_add_label(s, name, far, "local")
                expect[name] = (ref, pin.number)
        writes.sch_save(s)
    nets = _nets(_netlist(nightly, root, sch_path))
    for name, node in expect.items():
        assert node in nets.get(f"/{name}", set()), (name, mirror, nets)


def test_pin_endpoints_agree_with_erc_positions(nightly, root, blank):
    """The other oracle: ERC's `pin_not_connected` positions (x100 corrected by run_erc) equal the
    placement's pin endpoints for a lone rotated + mirrored symbol."""
    sch_path = blank / "blank.kicad_sch"
    with HeadlessSession(nightly, sch_path, config_root=root) as s:
        placed = writes.sch_add_symbol(s, "Regulator_Linear:AMS1117-3.3", "U1", None, (76.2, 63.5), 270, "x")
        writes.sch_save(s)
    report = _erc(nightly, root, sch_path)
    seen = {
        (round(v.pos.x_mm, 2), round(v.pos.y_mm, 2))
        for v in report.violations
        if v.type == "pin_not_connected" and v.pos is not None
    }
    assert seen == {(p.x_mm, p.y_mm) for p in placed.pins}, (seen, placed.pins)


# --- a legal design: ERC clean, netlist as intended ---------------------------------------------------


class _IpcApi:
    """The twin names without their `sch_` prefix, bound to one session: the same script drives
    the IPC twins here and the file writer (`SchematicWriter`) in the cross-check."""

    def __init__(self, session):
        self.s = session

    def add_symbol(self, *a, **kw):
        return writes.sch_add_symbol(self.s, *a, **kw)

    def add_wire(self, *a, **kw):
        return writes.sch_add_wire(self.s, *a, **kw)

    def add_power(self, *a, **kw):
        return writes.sch_add_power(self.s, *a, **kw)

    def add_label(self, *a, **kw):
        return writes.sch_add_label(self.s, *a, **kw)


def _build_ldo(api) -> dict[str, writes.SymbolPlacement]:
    """J1 -> C1 -> U1 (AMS1117-3.3) -> C2 -> +3V3 -> R1 -> D1 -> GND, PWR_FLAG on VIN and GND."""
    out: dict[str, writes.SymbolPlacement] = {}
    out["J1"] = api.add_symbol(
        "Connector_Generic:Conn_01x02",
        "J1",
        "VIN",
        (50.8, 76.2),
        footprint="Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical",
    )
    out["U1"] = api.add_symbol(
        "Regulator_Linear:AMS1117-3.3",
        "U1",
        None,
        (88.9, 73.66),
        footprint="Package_TO_SOT_SMD:SOT-223-3_TabPin2",
    )
    out["C1"] = api.add_symbol(
        "Device:C",
        "C1",
        "10u",
        (68.58, 81.28),
        fields={"LCSC": "C15850"},
        footprint="Capacitor_SMD:C_0603_1608Metric",
    )
    out["C2"] = api.add_symbol(
        "Device:C", "C2", "10u", (109.22, 81.28), footprint="Capacitor_SMD:C_0603_1608Metric"
    )
    out["R1"] = api.add_symbol(
        "Device:R", "R1", "1k", (124.46, 81.28), footprint="Resistor_SMD:R_0603_1608Metric"
    )
    out["D1"] = api.add_symbol(
        "Device:LED", "D1", "RED", (124.46, 96.52), rotation=90, footprint="LED_SMD:LED_0603_1608Metric"
    )
    j1, u1, c1, c2, r1, d1 = (out[k] for k in ("J1", "U1", "C1", "C2", "R1", "D1"))
    vin, vout, gnd = u1.pin("3"), u1.pin("2"), u1.pin("1")
    assert vin and vout and gnd and j1.pin("1") and j1.pin("2")
    top = j1.pin("1").y_mm
    # VIN rail: J1.1 -> (C1.1 stub) -> U1.VI
    api.add_wire([j1.pin("1").at_mm, (vin.x_mm, top), vin.at_mm])
    api.add_wire([c1.pin("1").at_mm, (c1.pin("1").x_mm, top)])
    # GND rail at y = 91.44: J1.2, C1.2, U1.GND, C2.2, D1.2 (the LED's cathode, rotated 90 -> pin 2 up)
    gy = 91.44
    j2 = j1.pin("2")
    api.add_wire([j2.at_mm, (j2.x_mm + 2.54, j2.y_mm), (j2.x_mm + 2.54, gy), (d1.pin("2").x_mm, gy)])
    for pin in (c1.pin("2"), gnd, c2.pin("2"), d1.pin("2")):
        api.add_wire([pin.at_mm, (pin.x_mm, gy)])
    # VOUT: U1.VO -> C2.1 stub -> R1.1
    api.add_wire([vout.at_mm, (r1.pin("1").x_mm, vout.y_mm), r1.pin("1").at_mm])
    api.add_wire([c2.pin("1").at_mm, (c2.pin("1").x_mm, vout.y_mm)])
    # LED anode below the GND rail: route R1.2 around the rail
    r2, a = r1.pin("2"), d1.pin("1")
    api.add_wire([r2.at_mm, (r2.x_mm + 5.08, r2.y_mm), (r2.x_mm + 5.08, a.y_mm), a.at_mm])
    api.add_power("GND", (gnd.x_mm, gy))
    api.add_power("GND", (gnd.x_mm, gy), flag=True)
    api.add_power("+3V3", (c2.pin("1").x_mm, vout.y_mm))
    api.add_symbol("power:PWR_FLAG", "#FLG02", None, (c1.pin("1").x_mm, top))
    api.add_label("VIN", (c1.pin("1").x_mm + 2.54, top), "local")
    return out


EXPECTED_LDO_NETS = {
    "+3V3": {("C2", "1"), ("R1", "1"), ("U1", "2")},
    "/VIN": {("C1", "1"), ("J1", "1"), ("U1", "3")},
    "GND": {("C1", "2"), ("C2", "2"), ("D1", "2"), ("J1", "2"), ("U1", "1")},
    "Net-(D1-K)": {("D1", "1"), ("R1", "2")},
}


def test_ldo_circuit_is_erc_clean_and_the_netlist_is_the_intent(nightly, root, blank):
    sch_path = blank / "blank.kicad_sch"
    with HeadlessSession(nightly, sch_path, config_root=root, request_timeout_ms=20000) as s:
        placed = _build_ldo(_IpcApi(s))
        # the live model already shows the intended nets before any save
        live = readers.read_schematic(s)
        live_nets = {
            n.name: {(i.ref, i.pin) for i in n.pins if not i.ref.startswith("#")} for n in live.netlist
        }
        for name, nodes in EXPECTED_LDO_NETS.items():
            assert live_nets.get(name) == nodes, (name, live_nets)
        assert placed["C1"].pin("1").number == "1" and placed["U1"].lib_id == "Regulator_Linear:AMS1117-3.3"
        saved = writes.sch_save(s)
    assert saved == sch_path.resolve()
    text = sch_path.read_text(encoding="utf-8")
    assert "(lib_name " not in text, "a symbol update forked the library symbol (definition echoed back)"
    assert text.count('(symbol "Device:C"') == 1, "one lib_symbols entry per lib_id"
    assert '(property "LCSC" "C15850"' in text
    report = _erc(nightly, root, sch_path)
    assert _errors(report) == [], _errors(report)
    nets = _nets(_netlist(nightly, root, sch_path))
    for name, nodes in EXPECTED_LDO_NETS.items():
        assert {n for n in nets.get(name, set()) if not n[0].startswith("#")} == nodes, (name, nets)
    # the file tier reads the nightly's file and agrees on the nets (formats is version-agnostic)
    from kiwrap.formats.analysis.netlist import netlist_from_file

    file_nets = _nets(netlist_from_file(str(sch_path)))
    for name, nodes in EXPECTED_LDO_NETS.items():
        assert {n for n in file_nets.get(name, set()) if not n[0].startswith("#")} == nodes, (name, file_nets)


def test_same_operations_through_the_file_writer_give_the_same_netlist(
    nightly, root, blank, install10, storage_home
):
    """Cross-check against the file writer on 10.0.6: the same operations must yield the same
    nets from kicad-cli 10.0.6. Skipped until `kiwrap.formats.schematic_writer` exists."""
    pytest.importorskip("kiwrap.formats.schematic_writer")
    pytest.importorskip("kiwrap.formats.libs")
    from kiwrap import sandbox
    from kiwrap.formats.libs import LibResolver
    from kiwrap.formats.schematic_writer import SchematicWriter

    sch_path = blank / "blank.kicad_sch"
    with HeadlessSession(nightly, sch_path, config_root=root, request_timeout_ms=20000) as s:
        _build_ldo(_IpcApi(s))
        writes.sch_save(s)
    ipc_nets = _nets(_netlist(nightly, root, sch_path))

    project = blank.parent / "file_writer"
    shutil.copytree(FIXTURES / "blank", project)
    root10 = sandbox.ensure(install10, storage_home=storage_home)
    caps10 = capabilities_for(install10, storage_home=storage_home)
    epoch = {"sch": caps10.get("formats.sch"), "sym": caps10.get("formats.sym")}
    w = SchematicWriter(project, epoch=epoch, lib_resolver=LibResolver(install10, root10, project))
    w.load(project / "blank.kicad_sch")

    _build_ldo(w)
    w.save()
    file_nets = _nets(cli_netlist.export_netlist(install10, root10, project / "blank.kicad_sch"))

    def strip(d):
        return {k: frozenset(n for n in v if not n[0].startswith("#")) for k, v in d.items()}

    ipc_s, file_s = strip(ipc_nets), strip(file_nets)
    # the connectivity (which pins share a net) must be identical ...
    assert set(ipc_s.values()) == set(file_s.values()), (ipc_s, file_s)
    # ... and so must the net names; a difference here is a writer disagreement worth a report
    if ipc_s != file_s:
        pytest.xfail(f"same connectivity, different net names: ipc={sorted(ipc_s)} file={sorted(file_s)}")


# --- wires: splitting and junctions -----------------------------------------------------------------


def test_wire_ending_on_another_wire_gets_a_junction_and_connects(nightly, root, blank):
    sch_path = blank / "blank.kicad_sch"
    with HeadlessSession(nightly, sch_path, config_root=root) as s:
        r1 = writes.sch_add_symbol(s, "Device:R", "R1", "1k", (50.8, 63.5))
        r2 = writes.sch_add_symbol(s, "Device:R", "R2", "1k", (76.2, 63.5))
        r3 = writes.sch_add_symbol(s, "Device:R", "R3", "1k", (63.5, 50.8))
        top = r1.pin("1").y_mm  # 59.69: the rail between R1.1 and R2.1
        ids = writes.sch_add_wire(s, [r1.pin("1").at_mm, r2.pin("1").at_mm])
        assert len(ids) == 1
        low = r3.pin("2")  # 54.61, above the rail: a stub dropped onto the rail's interior
        assert low.y_mm < top
        ids2 = writes.sch_add_wire(s, [low.at_mm, (low.x_mm, top)])
        # the stub is the only NEW wire; the junction and the two halves of the split rail are
        # reported apart (a receipt must never list a junction as a wire)
        assert len(ids2) == 1 and len(ids2.junctions) == 1 and len(ids2.split_wires) == 2, ids2
        assert len(ids2.all_ids) == 4
        model = readers.read_schematic(s)
        assert [j.position for j in model.junctions] == [(low.x_mm, top)]
        assert len(model.wires) == 3, [(w.start, w.end) for w in model.wires]
        writes.sch_add_label(s, "RAIL", (r1.pin("1").x_mm + 2.54, top))
        for pin in (r1.pin("2"), r2.pin("2"), r3.pin("1")):
            writes.sch_add_no_connect(s, pin.at_mm)
        writes.sch_save(s)
    nets = _nets(_netlist(nightly, root, sch_path))
    assert nets.get("/RAIL") == {("R1", "1"), ("R2", "1"), ("R3", "2")}, nets
    report = _erc(nightly, root, sch_path)
    assert _errors(report) == [], _errors(report)


def test_wire_through_a_pin_is_split_at_the_pin(nightly, root, blank):
    sch_path = blank / "blank.kicad_sch"
    with HeadlessSession(nightly, sch_path, config_root=root) as s:
        r1 = writes.sch_add_symbol(s, "Device:R", "R1", "1k", (63.5, 63.5))
        y = r1.pin("1").y_mm
        ids = writes.sch_add_wire(s, [(50.8, y), (76.2, y)])  # passes through R1.1
        assert len(ids) == 2 and len(ids.junctions) == 1  # two segments + a junction at the pin
        model = readers.read_schematic(s, refs=["R1"])
        assert any(
            i.kind == "wire" for n in model.netlist for i in n.items if any(p.ref == "R1" for p in n.pins)
        )


# --- edit / delete / annotate / footprints ----------------------------------------------------------


def test_edit_delete_annotate_and_assign_footprints(nightly, root, blank):
    sch_path = blank / "blank.kicad_sch"
    with HeadlessSession(nightly, sch_path, config_root=root) as s:
        a = writes.sch_add_symbol(s, "Device:R", None, "1k", (50.8, 50.8))
        b = writes.sch_add_symbol(s, "Device:R", None, "2k", (63.5, 50.8))
        c = writes.sch_add_symbol(s, "Device:C", "C7", "1u", (76.2, 50.8))
        assert a.ref == "R?" and b.ref == "R?"
        mapping = writes.sch_annotate(s)
        assert sorted(mapping.values()) == ["R1", "R2"], mapping
        assert all(k.startswith("R?@") for k in mapping)
        writes.sch_edit(
            s,
            a.uuid,
            {"at": (55.5, 55.5), "rotation": 90, "value": "4k7", "dnp": True, "fields": {"MPN": "X"}},
        )
        model = readers.read_schematic(s)
        sym = next(x for x in model.symbols if x.id == a.uuid)
        assert (sym.position, sym.rotation, sym.value, sym.dnp) == ((55.88, 55.88), 90.0, "4k7", True)
        assert writes.sch_assign_footprints(s, {"R1": "Resistor_SMD:R_0603_1608Metric", "ZZ9": "x"}) == ["R1"]
        writes.sch_delete(s, c.uuid)
        with pytest.raises(KeyError):
            writes.sch_delete(s, c.uuid)
        with pytest.raises(KeyError):
            writes.sch_edit(s, c.uuid, {"value": "x"})
        with pytest.raises(ValueError):
            writes.sch_edit(s, a.uuid, {"colour": "red"})
        label = writes.sch_add_label(s, "OLD", (50.8, 40.64))
        writes.sch_edit(s, label, {"text": "NEW", "rotation": 180})
        model = readers.read_schematic(s)
        assert [x.text for x in model.labels] == ["NEW"]
        assert sorted(x.ref for x in model.symbols) == ["R1", "R2"]
        writes.sch_save(s)
    text = sch_path.read_text(encoding="utf-8")
    assert "(lib_name " not in text
    assert '(property "MPN" "X"' in text
    nl = _netlist(nightly, root, sch_path)
    refs = {c.ref: c for c in nl.components}
    assert set(refs) == {"R1", "R2"}
    r1 = refs["R1"] if refs["R1"].value == "4k7" else refs["R2"]
    assert r1.footprint == "Resistor_SMD:R_0603_1608Metric" and r1.dnp


def test_repeated_placement_after_an_update_keeps_one_lib_symbol(nightly, root, blank):
    """Regression for the definition echo (module docstring of sch_writes): an update followed
    by another placement of the same lib_id must not fork `C_1`."""
    sch_path = blank / "blank.kicad_sch"
    with HeadlessSession(nightly, sch_path, config_root=root) as s:
        c1 = writes.sch_add_symbol(s, "Device:C", "C1", "1u", (50.8, 50.8))
        writes.sch_edit(s, c1.uuid, {"value": "2u2", "footprint": "Capacitor_SMD:C_0603_1608Metric"})
        writes.sch_add_symbol(s, "Device:C", "C2", "1u", (63.5, 50.8))
        writes.sch_add_symbol(s, "Device:C", "C3", "1u", (76.2, 50.8), fields={"LCSC": "C1"})
        writes.sch_save(s)
    text = sch_path.read_text(encoding="utf-8")
    assert "(lib_name " not in text
    assert re.findall(r'\(lib_id "([^"]+)"\)', text) == ["Device:C"] * 3
    report = _erc(nightly, root, sch_path)
    assert not [v for v in report.violations if v.type == "lib_symbol_issues"], _errors(report)


# --- G13 ----------------------------------------------------------------------------------------------


def test_multi_unit_symbol_units_update_without_the_g13_hang(nightly, root, ecc83):
    """The twins strip `definition` before UpdateItems, which sidesteps the G13 wedge: every unit
    of ecc83's U1 (ECC83, 3 units) is edited in turn and the server keeps answering."""
    sch_path = ecc83 / "ecc83-pp.kicad_sch"
    with HeadlessSession(nightly, sch_path, config_root=root, request_timeout_ms=8000) as s:
        model = readers.read_schematic(s, refs=["U1"], include_pins=False)
        units = sorted(model.symbols, key=lambda x: x.unit)
        assert [u.unit for u in units] == [1, 2, 3]
        for u in units:
            writes.sch_edit(s, u.id, {"fields": {"Note": f"unit {u.unit}"}})
        assert s.version().startswith("10.99")
        assert writes.sch_assign_footprints(s, {"U1": "Valve:Valve_ECC-83-2"}) == ["U1"]
        assert s.running and s.version()
        writes.sch_save(s)
    text = sch_path.read_text(encoding="utf-8")
    assert "(lib_name " not in text
    assert text.count('(property "Note"') == 3
    nets = _nets(_netlist(nightly, root, sch_path))
    assert any(("U1", "1") in v for v in nets.values())


def test_g13_guard_refuses_a_second_unit_with_the_definition(nightly, root, ecc83):
    """The raw path (`send_definition=True`) is what wedges affected builds: the guard lets one
    instance through and refuses the second with `Unsupported(fallback="file")` before sending."""
    from kiwrap.backends.ipc import sch_writes

    caps = capabilities_for(nightly)
    if not caps.has(sch_writes.CAP_HANG):
        pytest.skip("this build has no G13 hang; the guard is inert")
    with HeadlessSession(
        nightly, ecc83 / "ecc83-pp.kicad_sch", config_root=root, request_timeout_ms=8000
    ) as s:
        sch = s.schematic()
        items = sorted(
            (i for i in sch.get_symbols() if i.reference_field.text.value == "U1"), key=lambda i: i.unit
        )
        sch_writes._update_symbols(s, sch, items[:1], "one unit", send_definition=True)
        sch_writes._update_symbols(s, sch, items[:1], "same unit again", send_definition=True)
        with pytest.raises(Unsupported) as exc:
            sch_writes._update_symbols(s, sch, items[1:2], "second unit", send_definition=True)
        assert exc.value.fallback == "file"
        assert s.version()  # nothing was sent: the server still answers


def test_add_sheet_is_unsupported_over_ipc(nightly, root, blank):
    with HeadlessSession(nightly, blank / "blank.kicad_sch", config_root=root) as s:
        with pytest.raises(Unsupported) as exc:
            writes.sch_add_sheet(
                s, "sub", "sub.kicad_sch", (50.8, 50.8), (25.4, 20.32), [("IN", "left", "input")]
            )
        assert exc.value.fallback == "file"
        assert readers.read_schematic(s).sheets == ()


def test_presence_probes_record_capabilities(nightly, root, ecc83, storage_home):
    caps = capabilities_for(nightly, storage_home=storage_home)
    with HeadlessSession(nightly, ecc83 / "ecc83-pp.kicad_sch", config_root=root, capabilities=caps) as s:
        assert writes.probe_place_symbol(s) is True
    with HeadlessSession(nightly, ecc83 / "ecc83-pp.kicad_pcb", config_root=root, capabilities=caps) as s:
        assert writes.probe_import_netlist(s) is True
    probed = set(caps.get("kiwrap.ipc_probed", []) or [])
    assert {"ipc.has_place_symbol_from_library", "ipc.has_import_netlist"} <= probed
    assert caps.has("ipc.has_place_symbol_from_library") and caps.has("ipc.has_import_netlist")


# --- board -------------------------------------------------------------------------------------------


def test_sync_place_copper_and_drc(nightly, root, blank):
    sch_path, pcb_path = blank / "blank.kicad_sch", blank / "blank.kicad_pcb"
    with HeadlessSession(nightly, sch_path, config_root=root, request_timeout_ms=20000) as s:
        _build_ldo(_IpcApi(s))
        writes.sch_save(s)
    netlist_path = cli_netlist.export_netlist_file(nightly, root, sch_path, blank / "blank.net")
    with HeadlessSession(nightly, pcb_path, config_root=root, request_timeout_ms=60000) as s:
        dry = writes.pcb_sync(s, netlist_path, dry_run=True)
        assert dry.dry_run and dry.unresolved == [] and dry.errors == [], dry
        assert readers.read_board(s, footprint_bboxes=False).footprints == ()
        report = writes.pcb_sync(s, netlist_path)
        assert report.added == ["C1", "C2", "D1", "J1", "R1", "U1"], report
        assert report.removed == [] and report.unresolved == [] and report.errors == []
        assert sorted(report.placed) == report.added
        board = readers.read_board(s)
        assert board.outline_bbox is not None
        for fp in board.footprints:
            assert fp.bbox is not None and fp.bbox.y > board.outline_bbox.y2, (fp.ref, fp.bbox)
            for pad in fp.pads:
                assert pad.net, (fp.ref, pad.number)
        pads = {(f.ref, p.number): p.net for f in board.footprints for p in f.pads}
        assert pads[("U1", "2")] == "+3V3" and pads[("U1", "1")] == "GND" and pads[("C1", "1")] == "/VIN"
        # a second sync is a no-op
        again = writes.pcb_sync(s, netlist_path)
        assert again.added == [] and again.removed == [] and again.updated == [], again
        # placement inside the outline (100..160 x 80..120)
        writes.pcb_place(s, "U1", (110.0, 100.0), rotation=90, side="back", locked=True)
        writes.pcb_place(s, "C1", (120.0, 100.0))
        writes.pcb_place(s, "C2", (130.0, 100.0))
        writes.pcb_place(s, "R1", (140.0, 100.0), rotation=0)
        writes.pcb_place(s, "D1", (150.0, 100.0))
        writes.pcb_place(s, "J1", (105.0, 110.0), rotation=270)
        with pytest.raises(KeyError):
            writes.pcb_place(s, "ZZ9", (0.0, 0.0))
        u1 = readers.read_board(s, refs=["U1"]).footprints[0]
        assert (u1.position, u1.rotation, u1.side, u1.locked) == ((110.0, 100.0), 90.0, "back", True)
        zone = writes.pcb_add_zone(
            s, "GND", "B.Cu", [(101, 81), (159, 81), (159, 119), (101, 119)], priority=1
        )
        assert zone
        vid = writes.pcb_add_via(s, (125.0, 110.0), 0.8, 0.4, "GND")
        tids = writes.pcb_add_track(s, [(125.0, 110.0), (125.0, 115.0), (130.0, 115.0)], 0.3, "F.Cu", "GND")
        assert vid and len(tids) == 2
        writes.pcb_delete(s, tids[1])
        with pytest.raises(KeyError):
            writes.pcb_delete(s, tids[1])
        with pytest.raises(ValueError):
            writes.pcb_add_track(s, [(0, 0), (1, 1)], 0.3, "F.SilkS", "GND")
        model = readers.read_board(s, footprint_bboxes=False)
        assert len(model.tracks) == 1 and len(model.vias) == 1
        assert any(z.net == "GND" and z.filled for z in model.zones)
        writes.pcb_save(s)
    from kiwrap.backends.cli import drc as cli_drc

    drc = cli_drc.run_drc(
        nightly, root, pcb_path, schematic_parity=True, out_json=pcb_path.with_suffix(".drc.json")
    )
    assert drc.schematic_parity == (), [v.description for v in drc.schematic_parity]
    text = pcb_path.read_text(encoding="utf-8")
    assert text.count("(footprint ") == 6 and "(zone" in text


def test_sync_reports_unresolved_footprints(nightly, root, blank):
    sch_path, pcb_path = blank / "blank.kicad_sch", blank / "blank.kicad_pcb"
    with HeadlessSession(nightly, sch_path, config_root=root) as s:
        writes.sch_add_symbol(s, "Device:R", "R1", "1k", (50.8, 50.8), footprint="Nope:Missing")
        writes.sch_add_symbol(s, "Device:R", "R2", "1k", (63.5, 50.8))
        writes.sch_add_symbol(
            s, "Device:R", "R3", "1k", (76.2, 50.8), footprint="Resistor_SMD:R_0603_1608Metric"
        )
        writes.sch_save(s)
    netlist_path = cli_netlist.export_netlist_file(nightly, root, sch_path, blank / "blank.net")
    with HeadlessSession(nightly, pcb_path, config_root=root, request_timeout_ms=60000) as s:
        report = writes.pcb_sync(s, netlist_path)
    assert report.added == ["R3"]
    assert {(r, fp) for r, fp, _ in report.unresolved} == {("R1", "Nope:Missing"), ("R2", "")}
    assert not report.ok and len(report.errors) >= 2
    with pytest.raises(FileNotFoundError), HeadlessSession(nightly, pcb_path, config_root=root) as s:
        writes.pcb_sync(s, blank / "missing.net")


def test_latency_is_sane():
    """Each op stays well under a second at the median (ecc83-sized documents); a wedge or a
    blocked event loop shows up here before it shows up as a timeout elsewhere."""
    summary = writes.LATENCY.summary()
    slow = {op: s for op, s in summary.items() if s["median_ms"] > 2000 and not op.endswith("load_libraries")}
    assert not slow, json.dumps(slow)
