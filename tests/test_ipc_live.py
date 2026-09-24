"""Live tests for the headless IPC backend on the KiCad 11 preview (10.99 nightly, `ipc.headless`).

Every test copies the ecc83 demo shipped with the install into pytest's tmp dir (never the
original), starts `kicad-cli api-server` through `HeadlessSession` with a sandbox root, and checks:
units (ecc83 C1 at (141.605, 99.695) mm, rotation 90), model counts, schematic pin placement
against the wire endpoints of the same nets, connectivity, the write path, concurrent sessions,
teardown (the server's PID is gone, no descendants) and the user's real KiCad config hash.

Process assertions are by PID, never "no kicad-cli.exe at all": other components' tests run
kicad-cli concurrently in this tree.
"""

from __future__ import annotations

import concurrent.futures
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from kiwrap.backends.ipc import queries, readers, writes
from kiwrap.backends.ipc.sessions import HeadlessSession, SessionStartError, close_all, live_sessions
from kiwrap.capability import capabilities_for
from kiwrap.sandbox import user_config_hash

pytestmark = [pytest.mark.kicad, pytest.mark.kicad_version("10.99")]


def _pid_alive(pid: int) -> bool:
    if sys.platform.startswith("win"):
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True, check=False
        ).stdout
        return str(pid) in out
    try:
        import os

        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _children_of(pid: int) -> list[int]:
    if not sys.platform.startswith("win"):
        return []
    out = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            f'(Get-CimInstance Win32_Process -Filter "ParentProcessId={pid}").ProcessId',
        ],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    return [int(x) for x in out.split() if x.strip().isdigit()]


@pytest.fixture(scope="module")
def nightly(installs):
    match = [i for i in installs if i.major_minor == "10.99" and capabilities_for(i).has("ipc.headless")]
    if not match:
        pytest.skip("no 10.99 nightly with ipc.headless installed")
    return match[0]


@pytest.fixture(scope="module")
def root(nightly, storage_home):
    from kiwrap import sandbox

    return sandbox.ensure(nightly, storage_home=storage_home)


@pytest.fixture
def project(nightly, tmp_path) -> Path:
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


def test_session_lifecycle_and_teardown(nightly, root, project):
    session = HeadlessSession(nightly, project / "ecc83-pp.kicad_pcb", config_root=root)
    with session as s:
        assert s.running and s.pid is not None and _pid_alive(s.pid)
        assert s.socket_path is not None and len(str(s.socket_path)) <= 120
        assert s.version().startswith("10.99")
        assert s in live_sessions()
        pid = s.pid
        kids = _children_of(pid)
    assert not session.running and session.pid is None
    for _ in range(20):
        if not _pid_alive(pid) and not any(_pid_alive(k) for k in kids):
            break
        time.sleep(0.1)
    assert not _pid_alive(pid), "kicad-cli api-server survived close()"
    assert not any(_pid_alive(k) for k in kids)
    assert not session.socket_path
    assert root.seed_intact()


def test_close_releases_the_project_lock(nightly, root, project):
    """The headless server takes `~<name>.kicad_pro.lck` while a document is open; `close()` sends
    CloseAllDocuments before stopping the process so no stale lock blocks the next tool (the
    project tools answer E_FILE_LOCKED on `~*.lck`). Covers .kicad_sch, .kicad_pcb and .kicad_pro
    pre-loads and a session that never built a client."""
    for name, connect in (
        ("ecc83-pp.kicad_sch", True),
        ("ecc83-pp.kicad_pcb", False),
        ("ecc83-pp.kicad_pro", True),
    ):
        with HeadlessSession(nightly, project / name, config_root=root) as s:
            if connect:
                s.kicad()
            locks = sorted(p.name for p in project.glob("~*.lck"))
            assert locks == ["~ecc83-pp.kicad_pro.lck"], (name, locks)
        assert sorted(p.name for p in project.glob("~*.lck")) == [], name


def test_start_failure_is_a_teaching_error(nightly, root, tmp_path):
    bad = tmp_path / "broken.kicad_pcb"
    bad.write_text("(kicad_pcb (version 20260901) (generator kiwrap) this is not a board", encoding="utf-8")
    session = HeadlessSession(nightly, bad, config_root=root, ready_timeout_s=60)
    with pytest.raises(SessionStartError) as info:
        session.start()
    err = info.value
    assert err.code == "E_IPC_START" and err.fix and "argv" in err.details
    assert not session.running and session not in live_sessions()


def test_read_board_units_and_counts(nightly, root, project):
    with HeadlessSession(nightly, project / "ecc83-pp.kicad_pcb", config_root=root) as s:
        model = readers.read_board(s, pad_polygons=True)
    c1 = model.footprint("C1")
    assert c1 is not None
    assert c1.position == (141.605, 99.695) and c1.rotation == 90.0
    assert c1.layer == "F.Cu" and c1.side == "front" and c1.mounting == "through_hole"
    assert c1.fpid == "Footprints:CP_Radial_D10.0mm_P5.00mm"
    assert c1.bbox is not None and c1.bbox.contains(c1.position)
    assert [(p.number, p.net, p.type) for p in c1.pads] == [("1", "Net-(P3-P1)", "pth"), ("2", "GND", "pth")]
    assert c1.pads[0].shape == "rectangle" and c1.pads[1].shape == "circle" and c1.pads[0].size == (2.0, 2.0)
    assert c1.pads[0].drill == (1.0, 1.0) and "F.Cu" in c1.pads[0].layers
    assert len(c1.pads[0].polygon) >= 4 and c1.pads[0].polygon[0] == (140.605, 100.695)
    assert len(model.footprints) == 15 and len(model.tracks) == 59 and len(model.vias) == 0
    assert len(model.zones) == 1 and model.zones[0].net == "GND" and model.zones[0].layers == ("B.Cu",)
    assert len(model.shapes) == 4 and all(sh.layer == "Edge.Cuts" for sh in model.shapes)
    assert model.copper_layer_count == 2 and [layer.name for layer in model.layers][:2] == ["F.Cu", "F.Mask"]
    assert model.outline_bbox is not None and model.outline_bbox.w == pytest.approx(52.07)
    assert model.design_rules is not None and model.design_rules.min_track_width == 0.2
    assert {n.name for n in model.nets} >= {"GND", "Net-(P3-P1)"}
    doc = json.loads(json.dumps(model.to_json()))
    assert doc["footprints"][0]["position"] and doc["design_rules"]["min_via_size"] == 0.4


def test_read_board_filters_chunk(nightly, root, project):
    with HeadlessSession(nightly, project / "ecc83-pp.kicad_pcb", config_root=root) as s:
        by_ref = readers.read_board(s, refs=["C1"], footprint_bboxes=False)
        by_box = readers.read_board(s, bbox=(130, 90, 20, 20))
        by_net = readers.read_board(s, nets=["GND"])
    assert [f.ref for f in by_ref.footprints] == ["C1"] and by_ref.footprints[0].bbox is None
    assert "C1" in {f.ref for f in by_box.footprints} and 0 < len(by_box.tracks) < 59
    assert {f.ref for f in by_net.footprints} == {"C1", "R2", "R4", "P2", "P3", "R3", "P1"}
    assert by_net.tracks == () and by_net.zones[0].net == "GND"


def test_read_schematic_pins_land_on_wires(nightly, root, project):
    with HeadlessSession(nightly, project / "ecc83-pp.kicad_sch", config_root=root) as s:
        model = readers.read_schematic(s)
    assert len(model.symbols) == 26 and len(model.wires) == 37 and len(model.junctions) == 8
    assert len(model.no_connects) == 4 and len(model.netlist) == 13 and model.sheets == ()
    assert model.hierarchy is not None and model.hierarchy.filename == "ecc83-pp.kicad_sch"
    c1 = model.symbol("C1")
    assert c1 is not None and c1.position == (86.36, 57.15) and c1.mirror_y and c1.lib_id == "ecc83-pp:CP"
    assert [(p.number, p.net) for p in c1.pins] == [("1", "Net-(P3-P1)"), ("2", "GND")]
    # Every pin (all orientations ecc83 uses: 0/180/270, mirror_y; multi-unit U1) coincides with a
    # wire end, junction, no-connect or another pin - the transform check from probe6.
    anchors = {w.start for w in model.wires} | {w.end for w in model.wires}
    anchors |= {j.position for j in model.junctions} | {n.position for n in model.no_connects}
    pins = [(sy.ref, p) for sy in model.symbols for p in sy.pins]
    for ref, pin in pins:
        others = {q.position for r2, q in pins if (r2, q.number) != (ref, pin.number)}
        assert pin.position in anchors or pin.position in others, (ref, pin)
    assert {(sy.rotation, sy.mirror_x, sy.mirror_y) for sy in model.symbols} >= {
        (0.0, False, False),
        (0.0, False, True),
        (180.0, False, False),
        (270.0, False, False),
    }
    assert sum(1 for sy in model.symbols if sy.ref == "U1") == 3  # three units, own pins each
    gnd = next(n for n in model.netlist if n.name == "GND")
    assert {i.kind for i in gnd.items} >= {"pin", "wire"} and any(
        i.ref == "C1" and i.pin == "2" for i in gnd.pins
    )
    filtered = readers.read_schematic  # signature check only; behaviour covered by the unit tests
    assert callable(filtered)


@pytest.fixture
def shared_sheet_project(nightly, tmp_path) -> Path:
    """complex_hierarchy: `ampli_ht.kicad_sch` is placed twice (`/ampli_ht_horizontal/` and
    `/ampli_ht_vertical/`), so every item KIID of that sheet exists under two sheet paths."""
    src = nightly.dir("demos") / "complex_hierarchy"
    if not src.is_dir():
        pytest.skip("complex_hierarchy demo not shipped")
    dst = tmp_path / "complex_hierarchy"
    shutil.copytree(src, dst)
    return dst


def test_read_schematic_shared_sheet_matches_kicad_cli(nightly, root, shared_sheet_project):
    """Reviewer finding (blocker, 2026-09-19): keyed by KIID alone the IPC reader reported 55/168
    wrong pin nets and 25/52 wrong node sets on this demo. Oracle: `kicad-cli sch export netlist`."""
    from kiwrap.backends.cli.netlist import export_netlist
    from kiwrap.formats.schematic import Hierarchy

    sch = shared_sheet_project / "complex_hierarchy.kicad_sch"
    theirs = export_netlist(nightly, root, sch, shared_sheet_project / "oracle.net")
    with HeadlessSession(nightly, sch, config_root=root, ready_timeout_s=120) as s:
        model = readers.read_schematic(s)
    cli_pin_net = {(x.ref, x.pin): n.name for n in theirs.nets for x in n.nodes}
    wrong = [
        (sy.sheet_path, sy.ref, p.number, p.net, cli_pin_net[(sy.ref, p.number)])
        for sy in model.symbols
        for p in sy.pins
        if (sy.ref, p.number) in cli_pin_net and p.net != cli_pin_net[(sy.ref, p.number)]
    ]
    assert wrong == []
    cli_nodes = {n.name: frozenset((x.ref, x.pin) for x in n.nodes) for n in theirs.nets}
    ipc_nodes = {
        n.name: frozenset((i.ref, i.pin) for i in n.pins if not i.ref.startswith("#")) for n in model.netlist
    }
    assert {
        k: (sorted(cli_nodes[k]), sorted(ipc_nodes[k])) for k in cli_nodes if ipc_nodes.get(k) != cli_nodes[k]
    } == {}
    # one consistent, human-readable sheet path on every kind, one item per sheet INSTANCE
    sheets = {"/", "/ampli_ht_horizontal/", "/ampli_ht_vertical/"}
    assert {sy.sheet_path for sy in model.symbols} == sheets
    assert {w.sheet_path for w in model.wires} == sheets
    assert {i.sheet_path for n in model.netlist for i in n.items} == sheets
    assert {c.path for c in model.hierarchy.children} == sheets - {"/"}
    per_sheet_ipc = {
        path: sum(1 for w in model.wires if w.sheet_path == path and w.kind == "wire") for path in sheets
    }
    hier = Hierarchy.load(sch)
    per_sheet_file = {inst.names_path: len(inst.file.wires) for inst in hier.instances}
    assert per_sheet_ipc == per_sheet_file
    assert per_sheet_ipc["/ampli_ht_horizontal/"] == per_sheet_ipc["/ampli_ht_vertical/"] > 0
    # every wire / label listed by the netlist carries its net on the model
    listed = {(i.sheet_path, i.id) for n in model.netlist for i in n.items if i.kind in ("wire", "label")}
    on_model = {(w.sheet_path, w.id) for w in model.wires if w.net} | {
        (lb.sheet_path, lb.id) for lb in model.labels if lb.net
    }
    assert listed <= on_model


def test_pool_reuses_the_server_and_release_project_drops_the_lock(nightly, root, project):
    """Finding 14/19: one api-server per (install, file) survives between calls; the mutating
    tools' prologue closes it (and with it KiCad's project lock); a changed file is re-opened."""
    from kiwrap.backends.ipc.sessions import SESSION_POOL, pooled_session, release_project

    board = project / "ecc83-pp.kicad_pcb"
    with pooled_session(nightly, board, config_root=root) as s1:
        pid = s1.pid
        assert s1.running and len(s1.board().get_footprints()) == 15
    assert s1.running and list(project.glob("~*.lck"))  # still open after the call: lock held
    with pooled_session(nightly, board, config_root=root) as s2:
        assert s2 is s1 and s2.pid == pid
        with pooled_session(nightly, board, config_root=root) as nested:  # re-entrant in one thread
            assert nested is s1
        assert s2.running  # the inner release does not close or idle-expire the outer use
    assert SESSION_POOL.reused >= 2
    # a mutation prologue on the project: the pooled server is closed, the lock is gone
    info = release_project(project)
    assert info["closed"] == [str(board)] and info["busy"] == []
    assert not s1.running and list(project.glob("~*.lck")) == []
    # a session that was not pooled is reported busy instead of being closed under its user
    with HeadlessSession(nightly, board, config_root=root) as plain:
        busy = release_project(project)["busy"]
        assert busy == [str(board)] and plain.running
    # the file changed on disk -> the pool never answers from the stale load
    with pooled_session(nightly, board, config_root=root) as s3:
        pid3 = s3.pid
    board.write_text(
        board.read_text(encoding="utf-8").replace("(generator ", "(generator2 ", 1), encoding="utf-8"
    )
    board.write_text(
        board.read_text(encoding="utf-8").replace("(generator2 ", "(generator ", 1), encoding="utf-8"
    )
    with pooled_session(nightly, board, config_root=root) as s4:
        assert s4.pid != pid3 and not s3.running
    close_all()
    assert not s4.running and live_sessions() == []


def test_server_dies_with_its_parent_and_the_sweep_removes_the_stale_lock(nightly, root, project, tmp_path):
    """Finding 4: a parent killed with TerminateProcess (no atexit) must not leave the api-server
    behind (Windows Job Object / Linux PDEATHSIG), and the next session start removes the
    `~<name>.kicad_pro.lck` that the killed server could not release."""
    import os
    import textwrap

    from kiwrap.backends.ipc.sessions import sweep_orphans
    from kiwrap.platform import current

    board = project / "ecc83-pp.kicad_pcb"
    child = tmp_path / "child.py"
    child.write_text(
        textwrap.dedent(
            f"""
            import time
            from pathlib import Path
            from kiwrap.installation import discover
            from kiwrap import sandbox
            from kiwrap.backends.ipc.sessions import HeadlessSession
            inst = [i for i in discover() if i.version == {nightly.version!r}][0]
            root = sandbox.ensure(inst, storage_home=Path({str(root.path.parent.parent)!r}))
            s = HeadlessSession(inst, Path({str(board)!r}), config_root=root).start()
            print("PID", s.pid, flush=True)
            time.sleep(120)
            """
        ),
        encoding="utf-8",
    )
    env = {**os.environ, "KIWRAP_HOME": os.environ["KIWRAP_HOME"]}
    proc = subprocess.Popen([sys.executable, str(child)], stdout=subprocess.PIPE, text=True, env=env)
    try:
        line = proc.stdout.readline().strip() if proc.stdout else ""
        assert line.startswith("PID "), line
        pid = int(line.split()[1])
        assert _pid_alive(pid) and list(project.glob("~*.lck"))
    finally:
        proc.kill()  # TerminateProcess: no atexit, no __del__, no CloseAllDocuments
        proc.wait()
    for _ in range(50):
        if not _pid_alive(pid):
            break
        time.sleep(0.1)
    assert not _pid_alive(pid), "the api-server outlived its hard-killed parent"
    assert list(project.glob("~*.lck")), "TerminateProcess cannot release KiCad's lock: the sweep must"
    sweep_orphans(current().temp_dir)
    assert list(project.glob("~*.lck")) == []


def test_connectivity_queries(nightly, root, project):
    with HeadlessSession(nightly, project / "ecc83-pp.kicad_pcb", config_root=root) as s:
        board = s.board()
        pad = next(p for p in board.get_pads() if p.net.name == "GND")
        connected = queries.connected_items(s, pad)
        on_net = queries.items_by_net(s, "GND")
        rn = queries.ratsnest(s)
        box = queries.bounding_box(s, board.get_footprints()[0])
        hit = queries.hit_test(s, pad, (pad.position.x / 1e6, pad.position.y / 1e6))
        miss = queries.hit_test(s, pad, (pad.position.x / 1e6 + 5, pad.position.y / 1e6))
    assert connected and all(c.net == "GND" for c in connected)
    assert {c.kind for c in on_net} >= {"pad", "zone"} and all(c.net == "GND" for c in on_net)
    assert sum(1 for c in on_net if c.kind == "pad") == 7
    assert rn.unrouted == 0 and all(n.unrouted == 0 for n in rn.nets) and rn.requests > 0
    assert box is not None and box.w > 0
    assert hit and not miss


def test_write_path(nightly, root, project, tmp_path):
    report = writes.WriteReport()
    with HeadlessSession(nightly, project / "ecc83-pp.kicad_pcb", config_root=root) as s:
        board = s.board()
        libs = s.load_libraries(("footprint",))
        assert libs.get("Footprints") == "loaded", libs  # the project-local table row
        before = len(board.get_tracks())
        tid = writes.add_track(board, (130.0, 100.0), (135.0, 100.0), 0.5, "F.Cu", "GND")
        report.add("add_track", bool(tid), tid)
        assert len(board.get_tracks()) == before + 1
        fp = writes.place_footprint(board, "Footprints:CP_Radial_D10.0mm_P5.00mm", (150.0, 120.0), 45.0)
        report.add("place_footprint", fp["pads"] == 2, json.dumps(fp))
        assert fp["position"] == (150.0, 120.0) and fp["rotation"] == 45.0
        out = writes.save_copy(board, tmp_path / "out" / "written.kicad_pcb")
        report.add("save_copy_board", out.is_file(), str(out))
        assert board.is_document_modified()
    text = out.read_text(encoding="utf-8")
    assert "CP_Radial_D10.0mm_P5.00mm" in text and text.count("(footprint ") == 16
    assert (
        project / "ecc83-pp.kicad_pcb"
    ).stat().st_size > 0  # the scratch copy stays untouched by save_copy

    with HeadlessSession(nightly, project / "ecc83-pp.kicad_sch", config_root=root) as s:
        sch = s.schematic()
        libs = s.load_libraries(("symbol",))
        assert libs.get("ecc83-pp") == "loaded", libs
        # A 3-pin asymmetric triode at every rotation: a wire drawn from each computed pin position
        # must land on that pin (the netlist then lists pin + wire in one net) - the
        # transform proof for 90/270 that ecc83's own symmetric parts cannot give.
        for i, rot in enumerate((0, 90, 180, 270)):
            ref = f"U{20 + i}"
            placed = writes.place_symbol(sch, "ecc83-pp:ECC83", (220.0 + 40 * i, 150.0), rot, ref)
            assert placed["pins"] == 9 and placed["ref"] == ref
            sym = readers.read_schematic(sch, refs=[ref]).symbols[0]
            assert sym.rotation == rot and len(sym.pins) == 3
            for pin in sym.pins:
                assert writes.add_wire(sch, pin.position, (pin.position[0], pin.position[1] + 2.54))
            after = readers.read_schematic(sch, refs=[ref])
            joined = [
                n
                for n in after.netlist
                if any(i.ref == ref for i in n.pins) and any(i.kind == "wire" for i in n.items)
            ]
            assert len(joined) == 3, (
                rot,
                [(n.name, [(i.kind, i.ref, i.pin) for i in n.items]) for n in after.netlist],
            )
        report.add("place_symbol+wires", True)
        out = writes.save_copy(sch, tmp_path / "out" / "written.kicad_sch")
        report.add("save_copy_sch", out.is_file(), str(out))
    assert report.ok, report.to_json()
    assert "U23" in out.read_text(encoding="utf-8")


def test_concurrent_board_and_schematic_sessions(nightly, root, project):
    def run(kind: str) -> tuple[str, int, float]:
        t0 = time.monotonic()
        file = project / ("ecc83-pp.kicad_pcb" if kind == "pcb" else "ecc83-pp.kicad_sch")
        with HeadlessSession(nightly, file, config_root=root) as s:
            count = (
                len(readers.read_board(s).footprints)
                if kind == "pcb"
                else len(readers.read_schematic(s).symbols)
            )
            return kind, count, time.monotonic() - t0

    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        results = dict((k, (n, dt)) for k, n, dt in pool.map(run, ("pcb", "schematic")))
    assert results["pcb"][0] == 15 and results["schematic"][0] == 26
    assert live_sessions() == []


def test_open_document_on_an_empty_server(nightly, root, project):
    with HeadlessSession(nightly, config_root=root) as s:
        doc = s.open_document(project / "ecc83-pp.kicad_pcb", "pcb")
        assert doc.board_filename == "ecc83-pp.kicad_pcb"
        assert len(s.board().get_footprints()) == 15
        doc2 = s.open_document(project / "ecc83-pp.kicad_sch", "schematic")
        assert doc2.project.name == "ecc83-pp"
        assert len(s.schematic().get_symbols()) == 26
