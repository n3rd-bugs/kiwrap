"""T7 — IPC authoring certification for every install with a headless schematic API.

For each such install (the 10.99 nightly today; 11.0 at GA) the twins in
`kiwrap.backends.ipc.sch_writes` / `pcb_writes` author a small LDO circuit on a scratch copy of
`tests/fixtures/ipc_writes/blank`, save it and let the SAME install's kicad-cli judge the result
(`sch erc --format json`: zero errors; `sch export netlist`: the intended nets; `pcb drc
--schematic-parity` after `ImportNetlist`: zero parity issues). Alongside, every write command the
twins rely on is exercised once and its outcome classified (handled / bad_request / unhandled /
error / hang / save_fails), together with the behaviours the twins work around (the body style a
fresh placement carries, whether echoing a definition forks the library symbol, where
`ImportNetlist` drops new footprints, whether a `SheetSymbol` survives a save). The classified
outcomes are written to `conformance/snapshots/ipc-writes/<snapshot_name>.json` on the first run
and compared on later runs (drift canary: a nightly bump that fixes or breaks one of them fails
this test until the snapshot is refreshed). Latency medians are recorded next to the outcomes but
never compared.

Run: `uv run pytest conformance/tests/t7_ipc_writes.py -q` (about a minute per install).
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any

import pytest

from kiwrap.backends.cli import drc as cli_drc
from kiwrap.backends.cli import erc as cli_erc
from kiwrap.backends.cli import netlist as cli_netlist
from kiwrap.backends.ipc import client_version, readers, writes
from kiwrap.backends.ipc.sessions import HeadlessSession, close_all
from kiwrap.capability import capabilities_for

SNAPSHOT_DIR = Path(__file__).resolve().parent.parent / "snapshots" / "ipc-writes"
FIXTURE = Path(__file__).resolve().parent.parent.parent / "tests" / "fixtures" / "ipc_writes" / "blank"
SCHEMA = 1

#: what the LDO script must produce (power references excluded)
EXPECTED_NETS = {
    "+3V3": {("C2", "1"), ("R1", "1"), ("U1", "2")},
    "/VIN": {("C1", "1"), ("J1", "1"), ("U1", "3")},
    "GND": {("C1", "2"), ("C2", "2"), ("D1", "2"), ("J1", "2"), ("U1", "1")},
    "Net-(D1-K)": {("D1", "1"), ("R1", "2")},
}


def _outcome(fn) -> str:
    """Classify one write step by how the server answered."""
    from kipy.errors import ApiError

    try:
        fn()
        return "handled"
    except ApiError as e:
        return "unhandled" if writes.is_unhandled(e) else f"bad_request: {str(e)[:80]}"
    except ConnectionError:
        return "hang"
    except Exception as e:  # noqa: BLE001 - recorded, never raised: the snapshot is the report
        return f"error: {type(e).__name__}: {str(e)[:80]}"


def _build_ldo(s) -> None:
    add = writes.sch_add_symbol
    j1 = add(
        s,
        "Connector_Generic:Conn_01x02",
        "J1",
        "VIN",
        (50.8, 76.2),
        footprint="Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical",
    )
    u1 = add(
        s,
        "Regulator_Linear:AMS1117-3.3",
        "U1",
        None,
        (88.9, 73.66),
        footprint="Package_TO_SOT_SMD:SOT-223-3_TabPin2",
    )
    c1 = add(s, "Device:C", "C1", "10u", (68.58, 81.28), footprint="Capacitor_SMD:C_0603_1608Metric")
    c2 = add(s, "Device:C", "C2", "10u", (109.22, 81.28), footprint="Capacitor_SMD:C_0603_1608Metric")
    r1 = add(s, "Device:R", "R1", "1k", (124.46, 81.28), footprint="Resistor_SMD:R_0603_1608Metric")
    d1 = add(
        s, "Device:LED", "D1", "RED", (124.46, 96.52), rotation=90, footprint="LED_SMD:LED_0603_1608Metric"
    )
    vin, vout, gnd = u1.pin("3"), u1.pin("2"), u1.pin("1")
    top, gy = j1.pin("1").y_mm, 91.44
    writes.sch_add_wire(s, [j1.pin("1").at_mm, (vin.x_mm, top), vin.at_mm])
    writes.sch_add_wire(s, [c1.pin("1").at_mm, (c1.pin("1").x_mm, top)])
    j2 = j1.pin("2")
    writes.sch_add_wire(
        s, [j2.at_mm, (j2.x_mm + 2.54, j2.y_mm), (j2.x_mm + 2.54, gy), (d1.pin("2").x_mm, gy)]
    )
    for pin in (c1.pin("2"), gnd, c2.pin("2"), d1.pin("2")):
        writes.sch_add_wire(s, [pin.at_mm, (pin.x_mm, gy)])
    writes.sch_add_wire(s, [vout.at_mm, (r1.pin("1").x_mm, vout.y_mm), r1.pin("1").at_mm])
    writes.sch_add_wire(s, [c2.pin("1").at_mm, (c2.pin("1").x_mm, vout.y_mm)])
    r2, a = r1.pin("2"), d1.pin("1")
    writes.sch_add_wire(s, [r2.at_mm, (r2.x_mm + 5.08, r2.y_mm), (r2.x_mm + 5.08, a.y_mm), a.at_mm])
    writes.sch_add_power(s, "GND", (gnd.x_mm, gy))
    writes.sch_add_power(s, "GND", (gnd.x_mm, gy), flag=True)
    writes.sch_add_power(s, "+3V3", (c2.pin("1").x_mm, vout.y_mm))
    add(s, "power:PWR_FLAG", "#FLG02", None, (c1.pin("1").x_mm, top))
    writes.sch_add_label(s, "VIN", (c1.pin("1").x_mm + 2.54, top), "local")


def _nets(netlist) -> dict[str, set[tuple[str, str]]]:
    return {n.name: {(x.ref, x.pin) for x in n.nodes if not x.ref.startswith("#")} for n in netlist.nets}


def probe_writes(install, root, project: Path) -> dict[str, Any]:
    """Author the LDO circuit and exercise every write command once; returns the snapshot dict."""
    from kipy.proto.schematic.schematic_types_pb2 import SchematicSymbolOrientation as O

    sch_path, pcb_path = project / "blank.kicad_sch", project / "blank.kicad_pcb"
    schematic: dict[str, Any] = {}
    board: dict[str, Any] = {}
    checks: dict[str, Any] = {}
    info: dict[str, Any] = {}  # volatile numbers, recorded but never compared
    writes.LATENCY.reset()
    with HeadlessSession(install, sch_path, config_root=root, request_timeout_ms=15000) as s:
        sch = s.schematic()
        schematic["LoadAllLibraries(symbol)"] = _outcome(lambda: s.load_libraries(("symbol",)))
        schematic["PlaceSymbolFromLibrary"] = "handled" if writes.probe_place_symbol(s) else "unhandled"
        fresh = sch.place_symbol_from_library("Device:R", writes.vec((25.4, 25.4)), O.SSO_0, None, "R99")
        schematic["body_style_after_place"] = (
            fresh.proto.body_style.style if fresh.proto.HasField("body_style") else 0
        )
        schematic["DeleteItems"] = _outcome(lambda: writes.sch_delete(s, fresh.proto.id.value))
        schematic["DeleteItems(unknown id)"] = _outcome(lambda: writes.sch_delete(s, fresh.proto.id.value))
        schematic["ldo_script"] = _outcome(lambda: _build_ldo(s))
        model = readers.read_schematic(s)
        live = {n.name: {(i.ref, i.pin) for i in n.pins if not i.ref.startswith("#")} for n in model.netlist}
        checks["live_netlist_matches_intent"] = all(live.get(k) == v for k, v in EXPECTED_NETS.items())
        info["junctions_added"] = len(model.junctions)
        r1 = next(x for x in model.symbols if x.ref == "R1")
        schematic["UpdateItems(symbol, definition stripped)"] = _outcome(
            lambda: writes.sch_edit(s, r1.id, {"value": "1k5", "dnp": True})
        )
        schematic["UpdateItems(label)"] = _outcome(
            lambda: writes.sch_edit(s, model.labels[0].id, {"text": "VIN", "rotation": 0})
        )
        extras: list[str] = []
        schematic["CreateItems(GlobalLabel)"] = _outcome(
            lambda: extras.append(writes.sch_add_label(s, "G", (12.7, 12.7), "global"))
        )
        schematic["CreateItems(HierarchicalLabel)"] = _outcome(
            lambda: extras.append(writes.sch_add_label(s, "H", (12.7, 25.4), "hierarchical"))
        )
        schematic["CreateItems(NoConnectMarker)"] = _outcome(
            lambda: extras.append(writes.sch_add_no_connect(s, (12.7, 38.1)))
        )
        lone: list[str] = []
        schematic["CreateItems(Junction)"] = _outcome(
            lambda: lone.append(writes.sch_add_junction(s, (12.7, 50.8)))
        )
        # KiCad prunes a junction nothing needs when the commit is pushed (the KIID is dead)
        schematic["lone_junction_pruned_on_commit"] = bool(lone) and lone[0] not in {
            j.id for j in readers.read_schematic(s).junctions
        }
        # the extras are dangling on purpose; they go before the save so ERC judges the design
        schematic["DeleteItems(labels, marker)"] = _outcome(
            lambda: [writes.sch_delete(s, uuid) for uuid in extras]
        )
        schematic["annotate"] = _outcome(lambda: writes.sch_annotate(s))
        schematic["assign_footprints"] = _outcome(
            lambda: writes.sch_assign_footprints(s, {"R1": "Resistor_SMD:R_0805_2012Metric"})
        )
        schematic["SaveCopyOfDocument"] = _outcome(lambda: writes.sch_save(s, project / "copy.kicad_sch"))
        schematic["SaveDocument"] = _outcome(lambda: writes.sch_save(s))
    text = sch_path.read_text(encoding="utf-8")
    schematic["lib_name_fork_after_update"] = "(lib_name " in text
    info["saved_version"] = text.split("\n")[1].strip()
    close_all()

    # a second session: the definition echo (G13 mechanism) and the sheet symbol, each observed
    with HeadlessSession(install, sch_path, config_root=root, request_timeout_ms=15000) as s:
        sch = s.schematic()
        s.load_libraries(("symbol",))
        c1 = next(i for i in sch.get_symbols() if i.reference_field.text.value == "C1")
        c1.value_field.text.value = "22u"

        def echo() -> None:
            with writes.commit(sch, "echo"):
                sch.update_items(c1)

        schematic["UpdateItems(symbol, definition echoed)"] = _outcome(echo)
        sch.place_symbol_from_library("Device:C", writes.vec((25.4, 50.8)), O.SSO_0, None, "C98")
        writes.save_copy(sch, project / "echo.kicad_sch")
        schematic["lib_name_fork_after_definition_echo"] = "(lib_name " in (
            project / "echo.kicad_sch"
        ).read_text(encoding="utf-8")
    close_all()
    with HeadlessSession(install, sch_path, config_root=root, request_timeout_ms=15000) as s:
        from kipy.schematic_types import SheetSymbol

        sch = s.schematic()
        (project / "sub.kicad_sch").write_text(
            '(kicad_sch (version 20260306) (generator "kiwrap") (generator_version "0.1") '
            '(uuid "3d5b1c2e-5b7f-4a1e-9c3d-2f4e6a8b0c1e") (paper "A4") (lib_symbols) '
            '(sheet_instances (path "/" (page "1"))) (embedded_fonts no))\n',
            encoding="utf-8",
        )
        sheet = SheetSymbol()
        sheet.proto.position.CopyFrom(writes.vec((25.4, 63.5)).proto)
        sheet.proto.size.x_nm, sheet.proto.size.y_nm = 25400000, 20320000
        sheet.proto.name_field.name, sheet.proto.name_field.text.text = "Sheetname", "sub"
        sheet.proto.filename_field.name, sheet.proto.filename_field.text.text = "Sheetfile", "sub.kicad_sch"
        created = _outcome(lambda: sch.create_items(sheet))
        if created == "handled":
            saved = _outcome(sch.save)
            copy = _outcome(lambda: writes.save_copy(sch, project / "sheet.kicad_sch"))
            in_copy = (
                "(sheet " in (project / "sheet.kicad_sch").read_text(encoding="utf-8")
                if copy == "handled"
                else False
            )
            schematic["CreateItems(SheetSymbol)+SaveDocument"] = (
                "handled" if saved == "handled" and in_copy else f"save_fails ({saved}; in copy: {in_copy})"
            )
        else:
            schematic["CreateItems(SheetSymbol)+SaveDocument"] = created
    close_all()

    # KiCad judges the saved schematic
    erc = cli_erc.run_erc(install, root, sch_path, out_json=project / "erc.json")
    checks["erc_errors"] = erc.errors
    checks["erc_error_types"] = sorted({v.type for v in erc.violations if v.severity == "error"})
    netlist_path = cli_netlist.export_netlist_file(install, root, sch_path, project / "blank.net")
    nets = _nets(
        cli_netlist.parse_netlist_text(netlist_path.read_text(encoding="utf-8"), source="", kicad_version="")
    )
    checks["netlist_matches_intent"] = all(nets.get(k) == v for k, v in EXPECTED_NETS.items())

    # board
    with HeadlessSession(install, pcb_path, config_root=root, request_timeout_ms=60000) as s:
        board["ImportNetlist(dry_run)"] = _outcome(lambda: writes.pcb_sync(s, netlist_path, dry_run=True))
        board["LoadAllLibraries(footprint)"] = _outcome(lambda: s.load_libraries(("footprint",)))
        report_holder: dict[str, Any] = {}

        def sync() -> None:
            report_holder["r"] = writes.pcb_sync(s, netlist_path, place_new_offboard=False)

        board["ImportNetlist"] = _outcome(sync)
        r = report_holder.get("r")
        if r is not None:
            model = readers.read_board(s, footprint_bboxes=False)
            new = [f for f in model.footprints if f.ref in r.added]
            outline = model.outline_bbox
            board["new_footprints_outside_outline"] = (
                bool(new) and outline is not None and all(not outline.contains(f.position) for f in new)
            )
            info["import_added"] = r.added
            info["new_footprint_positions"] = {f.ref: f.position for f in new}
            board["is_document_modified_after_import"] = s.board().is_document_modified()
        board["UpdateItems(FootprintInstance)+FlipItems"] = _outcome(
            lambda: writes.pcb_place(s, "U1", (110.0, 100.0), rotation=90, side="back", locked=True)
        )
        for i, ref in enumerate(("C1", "C2", "R1", "D1", "J1")):
            writes.pcb_place(s, ref, (120.0 + 8 * i, 100.0))
        board["CreateItems(Zone)+RefillZones"] = _outcome(
            lambda: writes.pcb_add_zone(s, "GND", "B.Cu", [(101, 81), (159, 81), (159, 119), (101, 119)])
        )
        board["CreateItems(Via)"] = _outcome(lambda: writes.pcb_add_via(s, (125.0, 110.0), 0.8, 0.4, "GND"))
        ids: list[str] = []
        board["CreateItems(Track x2)"] = _outcome(
            lambda: ids.extend(
                writes.pcb_add_track(s, [(125.0, 110.0), (125.0, 115.0), (130.0, 115.0)], 0.3, "F.Cu", "GND")
            )
        )
        board["DeleteItems"] = _outcome(lambda: writes.pcb_delete(s, ids[-1])) if ids else "skipped"
        board["SaveDocument"] = _outcome(lambda: writes.pcb_save(s))
    close_all()
    info["saved_pcb_version"] = pcb_path.read_text(encoding="utf-8").split("\n")[1].strip()
    drc = cli_drc.run_drc(install, root, pcb_path, schematic_parity=True, out_json=project / "drc.json")
    checks["drc_parity_issues"] = len(drc.schematic_parity)
    info["drc_errors"] = drc.errors
    return {
        "schema": SCHEMA,
        "kicad": install.version,
        "client": client_version(),
        "schematic": schematic,
        "board": board,
        "checks": checks,
        "info": info,
        "latency_ms": writes.LATENCY.summary(),
        "notes": [
            "schematic / board / checks are compared on later runs; info and latency_ms are not",
            "the LDO script is tests/test_ipc_writes_live.py::_build_ldo; nets in EXPECTED_NETS",
        ],
    }


def _comparable(snapshot: dict[str, Any]) -> dict[str, Any]:
    out = {k: dict(snapshot.get(k, {})) for k in ("schematic", "board", "checks")}
    for section in out.values():
        for key, value in list(section.items()):
            if isinstance(value, str) and value.startswith(("bad_request", "error")):
                section[key] = value.split(":")[0]  # message texts may vary between builds
    return out


def compare(previous: dict[str, Any], current: dict[str, Any]) -> list[str]:
    old, new = _comparable(previous), _comparable(current)
    lines: list[str] = []
    for section in ("schematic", "board", "checks"):
        for key in sorted(set(old[section]) | set(new[section])):
            a, b = old[section].get(key), new[section].get(key)
            if a != b:
                lines.append(f"{section}.{key}: {a!r} -> {b!r}")
    return lines


@pytest.mark.kicad
@pytest.mark.slow
def test_t7_ipc_writes_certification(installs, storage_home, config_root, tmp_path):
    targets = [
        i
        for i in installs
        if capabilities_for(i, storage_home=storage_home).has("ipc.headless")
        and capabilities_for(i, storage_home=storage_home).has("ipc.schematic")
    ]
    if not targets:
        pytest.skip("no installed KiCad has a headless schematic API (ipc.headless + ipc.schematic)")
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    drift: list[str] = []
    try:
        for install in targets:
            project = tmp_path / install.root.name / "blank"
            shutil.copytree(FIXTURE, project)
            t0 = time.monotonic()
            snap = probe_writes(install, config_root(install), project)
            snap["elapsed_s"] = round(time.monotonic() - t0, 1)
            checks = snap["checks"]
            # the certification itself: KiCad accepts what the twins wrote
            assert checks["erc_errors"] == 0, (install.version, checks["erc_error_types"])
            assert checks["netlist_matches_intent"] and checks["live_netlist_matches_intent"], checks
            assert checks["drc_parity_issues"] == 0, checks
            assert snap["schematic"]["PlaceSymbolFromLibrary"] == "handled"
            assert snap["board"]["ImportNetlist"] == "handled"
            assert not snap["schematic"]["lib_name_fork_after_update"]
            path = SNAPSHOT_DIR / f"{install.snapshot_name}.json"
            if path.is_file():
                previous = json.loads(path.read_text(encoding="utf-8"))
                lines = compare(previous, snap)
                if lines:
                    drift.append(f"KiCad {install.snapshot_name}: " + "; ".join(lines))
                    path.with_suffix(".drift.json").write_text(json.dumps(snap, indent=1), encoding="utf-8")
            else:
                path.write_text(json.dumps(snap, indent=1), encoding="utf-8")
    finally:
        close_all()
    assert not drift, "IPC write behaviour drift (delete the snapshot to accept):\n" + "\n".join(drift)


def test_t7_compare_ignores_message_text_and_latency():
    a = {
        "schematic": {"x": "bad_request: foo"},
        "board": {},
        "checks": {"erc_errors": 0},
        "latency_ms": {"a": 1},
    }
    b = {
        "schematic": {"x": "bad_request: bar"},
        "board": {},
        "checks": {"erc_errors": 0},
        "latency_ms": {"a": 2},
    }
    assert compare(a, b) == []
    b["checks"]["erc_errors"] = 1
    assert compare(a, b) == ["checks.erc_errors: 0 -> 1"]
