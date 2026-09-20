"""G1 (docs/UPSTREAM.md): KiCad's own ratsnest through the kiwrap patch `ratsnest-export`.

The patch (fork branch `mr/g1-ratsnest`, built and registered as `C:/kiwrap-builds/6dd893f7d1-4`)
adds `kicad-cli pcb export ratsnest` and the `GetRatsnest` API command. Every test
here runs only on installs whose capability record lists the patch (`kicad_patch("ratsnest-export")`:
presence probes, never the version string) except the two stock-fallback tests, which run on a
headless install WITHOUT the patch and prove the client degrades to the cluster walk.

What is verified (2026-09-19, 10.99.0+6dd893f7d1 fork build; the stock nightly for the fallback):
- the CLI export on the ecc83 demo (fully routed) reports 0 airwires, and on the demo with 30 %
  of its segments / vias / arcs removed (seeded CST mutation, as tests/test_ratsnest_drc_live.py)
  it lists, pair for pair, the item pairs `kicad-cli pcb drc` reports as unconnected_items;
- every end point's uuid is a pad / track / via of the board file and its position is that
  item's pad position or a track end (the anchors are real geometry, not centroids);
- kiwrap's file estimate agrees with KiCad's per-net counts on the mutated ecc83 (the estimate
  stays the fallback for installs without the patch);
- `GetRatsnest` over a headless session returns the same edges as the export (uuids, positions
  within 1 um, layers), the presence probe answers True there and False on the stock nightly,
  where `queries.ratsnest()` falls back to the cluster walk (`method`, `exact`);
- an item pair the project excludes as a DRC unconnected_items violation is listed by the export
  and by `GetRatsnest` flagged `excluded`, dropped from the counts and lengths (as the editor's
  "Unrouted" count drops it), and `kicad-cli pcb drc` marks the same pair excluded;
- the layer of a through hole pad end is the layer it shares with the other end (ecc83 is a
  two-layer board with THT parts: every airwire between a THT pad and a B.Cu track reads B.Cu at
  both ends);
- kw_pcb_check on the patched install answers `unrouted` through `cli:<v>+ratsnest-export` with
  `degraded` empty, and the items carry uuid + layer per end point.

Process hygiene: sessions are closed by the context manager; the user's real KiCad config hash is
asserted by the session-wide fixture (tests/_fakes.user_config_untouched).
"""

from __future__ import annotations

import json
import random
import shutil
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from kiwrap.backends.cli import structured
from kiwrap.backends.cli.drc import capped_types, run_drc
from kiwrap.backends.ipc import queries
from kiwrap.backends.ipc.sessions import HeadlessSession, IpcError
from kiwrap.capability import capabilities_for
from kiwrap.formats.analysis import ratsnest_estimate
from kiwrap.formats.board import BoardFile
from kiwrap.formats.reports.ratsnest import RatsnestReport, parse_ratsnest_json
from kiwrap.formats.sexpr import dump

pytestmark = [pytest.mark.kicad, pytest.mark.kicad_version("10.99")]

PATCH = "ratsnest-export"
DEMO = ("demos", "ecc83")
BOARD = "ecc83-pp.kicad_pcb"
FRACTION = 0.3
SEED = 2


# ---- fixtures ------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def patched(patched_installs) -> Any:
    """The newest install carrying the patch (the registered fork build)."""
    return patched_installs(PATCH)[0]


@pytest.fixture(scope="module")
def stock(installs, storage_home) -> Any:
    """A headless install WITHOUT the patch (the stock nightly), for the fallback tests."""
    for inst in installs:
        caps = capabilities_for(inst, storage_home=storage_home)
        if caps.has("ipc.headless") and PATCH not in (caps.get("kiwrap.patches") or []):
            return inst
    pytest.skip("no headless install without the patch")


def _demo_copy(install: Any, dest: Path) -> Path:
    src = install.dir(DEMO[0]) / DEMO[1]
    if not (src / BOARD).is_file():
        pytest.skip(f"{BOARD} not shipped under {src}")
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns("*.lck", "*-backups"))
    return dest / BOARD


def _mutate(board: Path, fraction: float = FRACTION, seed: int = SEED) -> int:
    """Delete a seeded fraction of the copper tracks through the CST; returns how many."""
    model = BoardFile.load(board)
    rng = random.Random(seed)
    victims = [n for n in model.root.nodes if n.head in ("segment", "via", "arc") and rng.random() < fraction]
    for n in victims:
        model.root.remove(n)
    dump(model.root, board)
    return len(victims)


@pytest.fixture(scope="module")
def mutated(patched, tmp_path_factory) -> Path:
    """ecc83 with ~30 % of its copper removed (the same mutation tests/test_ratsnest_drc_live uses)."""
    board = _demo_copy(patched, tmp_path_factory.mktemp("g1") / "ecc83")
    removed = _mutate(board)
    assert removed > 0
    return board


def _pairs(report: RatsnestReport) -> set[frozenset[str]]:
    return {frozenset((e.a.uuid, e.b.uuid)) for e in report.edges}


def _drc_pairs(report: Any) -> set[frozenset[str]]:
    return {frozenset(i.uuid for i in v.items) for v in report.unconnected}


# ---- the CLI export --------------------------------------------------------------------------------


@pytest.mark.kicad_patch(PATCH)
def test_capability_and_provider_label(patched, storage_home):
    caps = capabilities_for(patched, storage_home=storage_home)
    assert caps.has("cli.has_ratsnest_export") and "pcb export ratsnest" in caps["cli.subcommands"]
    assert caps.has("ipc.has_get_ratsnest")
    # an integration build carries every patch of the series (specctra-jobs joined on -5)
    assert PATCH in caps["kiwrap.patches"] and caps.has("kiwrap.patched_build")
    assert caps.patch_for("cli.has_ratsnest_export") == PATCH
    assert caps.patch_for("cli.has_drc") == ""


@pytest.mark.kicad_patch(PATCH)
def test_routed_demo_has_no_airwires(patched, config_root, tmp_path):
    board = _demo_copy(patched, tmp_path / "ecc83")
    report = structured.export_ratsnest(patched, config_root(patched), board)
    assert report.coordinate_units == "mm" and report.kicad_version == patched.version
    assert report.unrouted_count == 0 and report.airline_length_mm == 0 and report.nets == ()
    everything = structured.export_ratsnest(patched, config_root(patched), board, include_routed=True)
    assert everything.unrouted_count == 0 and len(everything.nets) > 0
    assert all(n.unrouted_count == 0 and n.edges == () for n in everything.nets)
    assert {n.name for n in everything.nets} <= {n.name for n in BoardFile.load(board).nets}


@pytest.mark.kicad_patch(PATCH)
def test_mutated_demo_pairs_equal_drc_unconnected_items(patched, config_root, mutated):
    root = config_root(patched)
    report = structured.export_ratsnest(patched, root, mutated)
    drc = run_drc(patched, root, mutated, severity="all")
    assert "unconnected_items" not in capped_types(drc), "board too large for the DRC oracle"
    assert report.unrouted_count == len(drc.unconnected) > 0
    assert _pairs(report) == _drc_pairs(drc)
    assert report.unrouted_count == sum(n.unrouted_count for n in report.nets)
    assert report.unrouted_count == len(report.edges)
    for net in report.nets:
        assert net.unrouted_count == len(net.edges) > 0
        assert net.airline_length_mm == pytest.approx(sum(e.length_mm for e in net.edges), abs=1e-6)
    assert report.airline_length_mm == pytest.approx(sum(e.length_mm for e in report.edges), abs=1e-5)
    # --nets restricts the report to the named nets
    first = report.nets[0].name
    one = structured.export_ratsnest(patched, root, mutated, nets=[first])
    assert [n.name for n in one.nets] == [first] and one.unrouted_count == report.nets[0].unrouted_count


@pytest.mark.kicad_patch(PATCH)
def test_end_points_are_real_pad_and_track_positions(patched, config_root, mutated):
    report = structured.export_ratsnest(patched, config_root(patched), mutated)
    model = BoardFile.load(mutated)
    pads = {pad.uuid: (fp, pad) for fp, pad in model.pads()}
    tracks = {t.uuid: t for t in model.tracks}
    vias = {v.uuid: v for v in model.vias}
    seen = Counter()
    for edge in report.edges:
        assert edge.length_mm == pytest.approx(
            ((edge.a.pos.x_mm - edge.b.pos.x_mm) ** 2 + (edge.a.pos.y_mm - edge.b.pos.y_mm) ** 2) ** 0.5,
            abs=1e-3,
        )
        for node in (edge.a, edge.b):
            assert node.layer.endswith(".Cu")
            if node.uuid in pads:
                fp, pad = pads[node.uuid]
                seen["pad"] += 1
                # Pad.position is absolute (board frame); the anchor is the pad's shape position
                assert (node.pos.x_mm, node.pos.y_mm) == pytest.approx(
                    (pad.position.x, pad.position.y), abs=1e-3
                ), (
                    fp.reference,
                    pad.number,
                )
                assert (
                    "pad" in node.description.lower()
                )  # "Pad 2 [GND] of C1 on F.Cu" / "PTH pad 9 ... of U1"
            elif node.uuid in tracks:
                t = tracks[node.uuid]
                seen["track"] += 1
                ends = [(t.start.x, t.start.y), (t.end.x, t.end.y)]
                assert any((node.pos.x_mm, node.pos.y_mm) == pytest.approx(end, abs=1e-3) for end in ends), (
                    node.description,
                    ends,
                )
                assert node.layer == t.layer
            elif node.uuid in vias:
                v = vias[node.uuid]
                seen["via"] += 1
                assert (node.pos.x_mm, node.pos.y_mm) == pytest.approx((v.position.x, v.position.y), abs=1e-3)
            else:
                seen["zone"] += 1  # zone fills are the only other connected items
    assert seen["pad"] > 0 and seen["track"] > 0
    assert seen["zone"] <= sum(1 for _ in model.zones) * 2


@pytest.mark.kicad_patch(PATCH)
def test_file_estimate_agrees_with_kicad_per_net(patched, config_root, mutated):
    report = structured.export_ratsnest(patched, config_root(patched), mutated)
    est = ratsnest_estimate(BoardFile.load(mutated))
    kicad = {n.name.replace("{slash}", "/"): n.unrouted_count for n in report.nets}
    estimate = {net.replace("{slash}", "/"): n for net, n in est.unrouted_by_net.items() if n}
    diff = {
        net: (kicad.get(net, 0), estimate.get(net, 0))
        for net in set(kicad) | set(estimate)
        if kicad.get(net, 0) != estimate.get(net, 0)
    }
    assert diff == {}, "per-net (kicad, estimate) differences"
    assert est.unrouted == report.unrouted_count


def test_parser_round_trip_from_the_schema_example():
    text = """{"$schema": "https://schemas.kicad.org/ratsnest.v1.json", "source": "b.kicad_pcb",
    "date": "2026-09-19T00:00:00", "kicad_version": "10.99.0", "coordinate_units": "in",
    "unrouted_count": 1, "airline_length": 1.0,
    "nets": [{"name": "N", "unrouted_count": 1, "airline_length": 1.0, "edges": [
      {"a": {"uuid": "a", "description": "Pad 1 [N] of R1 on F.Cu", "pos": {"x": 0, "y": 0}, "layer": "F.Cu"},
       "b": {"uuid": "b", "description": "Track [N] on B.Cu, length 1 in", "pos": {"x": 1, "y": 0}, "layer": "B.Cu"},
       "length": 1.0}]}]}"""
    report = parse_ratsnest_json(text)
    assert report.coordinate_units == "in" and report.unrouted_count == 1
    edge = report.edges[0]
    assert edge.length_mm == pytest.approx(25.4) and edge.b.pos.x_mm == pytest.approx(25.4)
    assert edge.a.layer == "F.Cu" and edge.b.layer == "B.Cu" and edge.net == "N"
    assert report.airline_length_mm == pytest.approx(25.4) and report.unrouted_by_net == {"N": 1}
    # totals and counts fall back to the edges when a writer omits them
    minimal = parse_ratsnest_json(
        '{"nets": [{"name": "N", "edges": [{"a": {"pos": {"x": 0, "y": 0}}, "b": {"pos": {"x": 3, "y": 4}}}]}]}'
    )
    assert minimal.unrouted_count == 1 and minimal.edges[0].length_mm == pytest.approx(5.0)
    assert minimal.nets[0].airline_length_mm == pytest.approx(5.0) and minimal.coordinate_units == "mm"


def test_parser_reads_the_excluded_flag():
    text = """{"coordinate_units": "mm", "unrouted_count": 1, "airline_length": 2.0,
    "nets": [{"name": "N", "unrouted_count": 1, "airline_length": 2.0, "edges": [
      {"a": {"uuid": "a", "description": "", "pos": {"x": 0, "y": 0}, "layer": "F.Cu"},
       "b": {"uuid": "b", "description": "", "pos": {"x": 2, "y": 0}, "layer": "F.Cu"}, "length": 2.0},
      {"a": {"uuid": "c", "description": "", "pos": {"x": 0, "y": 0}, "layer": "F.Cu"},
       "b": {"uuid": "d", "description": "", "pos": {"x": 5, "y": 0}, "layer": "F.Cu"}, "length": 5.0,
       "excluded": true}]}]}"""
    report = parse_ratsnest_json(text)
    assert [e.excluded for e in report.edges] == [False, True]
    assert len(report.active_edges) == 1 and report.nets[0].active_edges[0].a.uuid == "a"
    assert report.unrouted_count == 1 and report.airline_length_mm == pytest.approx(2.0)
    # a writer that omits the totals: the excluded edge is not counted either
    minimal = parse_ratsnest_json(
        '{"nets": [{"name": "N", "edges": [{"a": {"pos": {"x": 0, "y": 0}}, "b": {"pos": {"x": 3, "y": 4}},'
        ' "excluded": true}, {"a": {"pos": {"x": 0, "y": 0}}, "b": {"pos": {"x": 1, "y": 0}}}]}]}'
    )
    assert minimal.unrouted_count == 1 and minimal.nets[0].airline_length_mm == pytest.approx(1.0)
    assert len(minimal.edges) == 2


@pytest.mark.kicad_patch(PATCH)
def test_excluded_pairs_are_listed_flagged_and_not_counted(patched, config_root, mutated, tmp_path):
    """Exclusion = a DRC unconnected_items marker (position + the two items) in the project's
    `drc_exclusions`, written the way the editor stores it; KiCad applies it when a DRC run would
    mark the violation excluded, and the export / GetRatsnest agree with that run."""
    root = config_root(patched)
    project = tmp_path / "ecc83"
    shutil.copytree(mutated.parent, project, ignore=shutil.ignore_patterns("*.lck", "*-backups"))
    board = project / mutated.name
    before = structured.export_ratsnest(patched, root, board)
    edge = before.edges[0]
    pro = board.with_suffix(".kicad_pro")
    data = json.loads(pro.read_text(encoding="utf-8"))
    settings = data.setdefault("board", {}).setdefault("design_settings", {})
    marker = f"unconnected_items|{round(edge.a.pos.x_mm * 1e6)}|{round(edge.a.pos.y_mm * 1e6)}|{edge.a.uuid}|{edge.b.uuid}"
    settings["drc_exclusions"] = [[marker, "accepted until routing"]]
    pro.write_text(json.dumps(data, indent=2), encoding="utf-8")

    after = structured.export_ratsnest(patched, root, board)
    assert len(after.edges) == len(before.edges)
    flagged = [e for e in after.edges if e.excluded]
    assert len(flagged) == 1 and {flagged[0].a.uuid, flagged[0].b.uuid} == {edge.a.uuid, edge.b.uuid}
    assert after.unrouted_count == before.unrouted_count - 1 == len(after.active_edges)
    assert after.net(edge.net).unrouted_count == before.net(edge.net).unrouted_count - 1
    assert after.airline_length_mm == pytest.approx(before.airline_length_mm - edge.length_mm, abs=1e-5)
    assert after.net(edge.net).airline_length_mm == pytest.approx(
        sum(e.length_mm for e in after.net(edge.net).active_edges), abs=1e-6
    )
    # DRC marks exactly the same pair excluded
    drc = run_drc(patched, root, board, severity="all")
    excluded_by_drc = {frozenset(i.uuid for i in v.items) for v in drc.unconnected if v.excluded}
    assert excluded_by_drc == {frozenset((edge.a.uuid, edge.b.uuid))}
    # GetRatsnest lists it flagged and drops it from the counts too
    with HeadlessSession(patched, board, config_root=root) as s:
        rn = queries.get_ratsnest(s)
        listed = [e for n in rn.nets for e in n.edges]
        assert len(listed) == len(before.edges)
        excluded = [e for e in listed if e.excluded]
        assert len(excluded) == 1 and {excluded[0].a.id, excluded[0].b.id} == {edge.a.uuid, edge.b.uuid}
        assert rn.unrouted == after.unrouted_count
        net = next(n for n in rn.nets if n.net == edge.net)
        assert net.unrouted == len(net.airwires) == len(net.edges) - 1


@pytest.mark.kicad_patch(PATCH)
def test_through_hole_pad_ends_share_the_other_ends_layer(patched, config_root, mutated):
    """ecc83 is a two-layer board with THT footprints: an airwire from a THT pad to a B.Cu track
    end is reported on B.Cu at both ends (the pad reaches B.Cu), not F.Cu <-> B.Cu."""
    root = config_root(patched)
    report = structured.export_ratsnest(patched, root, mutated)
    model = BoardFile.load(mutated)
    tht = {p.uuid for fp in model.footprints for p in fp.pads if p.uuid and p.type == "thru_hole"}
    tracks = {t.uuid: t.layer for t in model.tracks if t.uuid}
    checked = 0
    for e in report.edges:
        for pad_end, other in ((e.a, e.b), (e.b, e.a)):
            if pad_end.uuid in tht and other.uuid in tracks:
                assert pad_end.layer == other.layer == tracks[other.uuid], (pad_end, other)
                checked += 1
            elif pad_end.uuid in tht and other.uuid in tht:
                assert pad_end.layer == other.layer == "F.Cu"
                checked += 1
    assert checked > 0, "the mutated ecc83 should have airwires ending on THT pads"


# ---- GetRatsnest over the headless session -------------------------------------------------------------


@pytest.mark.kicad_patch(PATCH)
def test_get_ratsnest_equals_the_export(patched, config_root, mutated):
    root = config_root(patched)
    report = structured.export_ratsnest(patched, root, mutated)
    with HeadlessSession(patched, mutated, config_root=root) as s:
        assert queries.probe_get_ratsnest(s) is True
        rn = queries.ratsnest(s)
        assert rn.exact and rn.method == queries.KICAD_METHOD and rn.requests == 3
        assert rn.unrouted == report.unrouted_count
        by_pair = {frozenset((e.a.uuid, e.b.uuid)): e for e in report.edges}
        assert {frozenset((e.a.id, e.b.id)) for n in rn.nets for e in n.edges} == set(by_pair)
        for n in rn.nets:
            assert n.unrouted == len(n.edges) == len(n.airwires) and n.clusters == ()
            assert not any(e.excluded for e in n.edges)
            for e in n.edges:
                ref = by_pair[frozenset((e.a.id, e.b.id))]
                ours = {e.a.id: e.a, e.b.id: e.b}
                for node in (ref.a, ref.b):
                    mine = ours[node.uuid]
                    assert mine.position == pytest.approx((node.pos.x_mm, node.pos.y_mm), abs=1e-3)
                    assert mine.layer == node.layer
                assert e.length == pytest.approx(ref.length_mm, abs=1e-3)
            # anchors are joined to the footprint pads like the cluster walk's
            pad_anchors = [a for a in n.anchors if a.kind == "pad"]
            assert all(a.ref and a.number for a in pad_anchors)
        # the net filter keeps only the named net, and an unknown net yields nothing
        only = queries.get_ratsnest(s, nets=[rn.nets[0].net])
        assert [n.net for n in only.nets] == [rn.nets[0].net]
        assert queries.get_ratsnest(s, nets=["no-such-net"]).nets == ()
        # the session's capability record keeps the probed value
        assert s.capabilities.has("ipc.has_get_ratsnest")


def test_stock_server_has_no_handler_and_ratsnest_falls_back(stock, config_root, tmp_path):
    board = _demo_copy(stock, tmp_path / "ecc83")
    with HeadlessSession(stock, board, config_root=config_root(stock)) as s:
        assert queries.probe_get_ratsnest(s) is False
        with pytest.raises(IpcError) as ei:
            queries.get_ratsnest(s)
        assert "GetRatsnest" in str(ei.value)
        with pytest.raises(IpcError):
            queries.ratsnest(s, kicad=True)
        rn = queries.ratsnest(s, nets=["GND"])
        assert not rn.exact and rn.method == queries.CLUSTER_METHOD


def test_message_classes_are_built_when_kipy_lacks_the_proto():
    request_cls, response_cls = queries.ratsnest_messages()
    assert request_cls.DESCRIPTOR.full_name == "kiapi.board.commands.GetRatsnest"
    assert response_cls.DESCRIPTOR.full_name == "kiapi.board.commands.GetRatsnestResponse"
    req = request_cls()
    req.board.type = 1
    req.nets.add().name = "GND"
    parsed = request_cls.FromString(req.SerializeToString())
    assert parsed.nets[0].name == "GND" and parsed.board.type == 1
    from google.protobuf import any_pb2

    packed = any_pb2.Any()
    packed.Pack(req)
    assert packed.type_url.endswith("/kiapi.board.commands.GetRatsnest")
    fields = {f.name: f.number for f in response_cls.DESCRIPTOR.fields_by_name["nets"].message_type.fields}
    assert fields == {"net": 1, "edges": 2, "airline_length": 3}


# ---- kw_pcb_check ---------------------------------------------------------------------------------------


@pytest.mark.kicad_patch(PATCH)
def test_pcb_check_uses_the_export_with_nothing_degraded(patched, storage_home, mutated):
    from kiwrap.core.services import Services
    from kiwrap.core.state.jobs import JobQueue
    from kiwrap.core.state.project import Project
    from kiwrap.core.tools._base import ToolContext
    from kiwrap.core.tools.pcb_check import PcbCheckArgs, pcb_check

    project = Project.from_path(mutated.with_suffix(".kicad_pro"))
    state = project.load_state()
    state.target_kicad = str(patched.root)
    project.save_state(state)
    ctx = ToolContext(services=Services.real(storage_home), jobs=JobQueue())
    export = structured.export_ratsnest(patched, ctx.services.config_root(patched), mutated)
    pairs = {frozenset((e.a.uuid, e.b.uuid)) for e in export.edges}
    # auto: the live session answers first (GetRatsnest, exact); provider="cli" forces the export
    for provider, label in (("auto", "ipc11"), ("cli", "cli")):
        args = PcbCheckArgs(project=str(project.pro_path), checks=["unrouted"], provider=provider)
        receipt = pcb_check(ctx, args)
        data = receipt.data
        assert data["providers"]["unrouted"] == f"{label}:{patched.version}+{PATCH}"
        assert receipt.degraded == [] and data["summary"]["unrouted"] == export.unrouted_count > 0
        items = [i for i in data["items"] if i["check"] == "unrouted"]
        assert {frozenset((i["a"]["uuid"], i["b"]["uuid"])) for i in items} == pairs
        assert all(i["a"]["layer"].endswith(".Cu") and i["b"]["layer"].endswith(".Cu") for i in items)
        assert all(i["length_mm"] > 0 and i["pos"] is not None for i in items)
    # the pooled api-server the auto run started is closed here (process hygiene)
    closed = ctx.services.ipc_release_project(project.dir)
    assert closed["busy"] == []


# ---- no KiCad needed: the tool wiring with fakes ------------------------------------------------------------


def test_pcb_check_prefers_the_export_over_the_cluster_walk_with_fakes(tmp_path: Path):
    """A KiCad with the CLI half of the patch but no GetRatsnest (or a session not yet probed):
    kw_pcb_check answers from the export (exact, `degraded` empty, patch-labelled) and never
    starts the ipc11 cluster walk; `provider="ipc11"` still forces the walk (tagged)."""
    from kiwrap.formats.reports import Position
    from kiwrap.formats.reports.ratsnest import RatsnestEdge, RatsnestNet, RatsnestNode, RatsnestReport
    from tests.test_core_datatools import CAPS_11, FakeSession, harness, with_11

    caps = {
        **CAPS_11,
        "cli.has_ratsnest_export": True,
        "cli.subcommands": [*CAPS_11["cli.subcommands"], "pcb export ratsnest"],
        "ipc.has_get_ratsnest": False,
        "kiwrap.patches": [PATCH],
    }
    h, backend, project = harness(tmp_path, **with_11(caps={"11.0.0": caps}))
    from kiwrap.core.tools.pcb_get import board_items

    fake_pads = board_items(backend.file_read_board(Path(project) / "demo.kicad_pcb"))["pads"]
    pad_uuid = next(p["uuid"] for p in fake_pads if p["ref"] == "C1" and p["number"] == "2")
    a = RatsnestNode(pad_uuid, "Pad 2 [GND] of C1 on F.Cu", Position(60.8, 5.0), "F.Cu")
    b = RatsnestNode("t-1", "Track [GND] on B.Cu, length 3 mm", Position(11.3, 10.5), "B.Cu")
    edge = RatsnestEdge("GND", a, b, 49.8)
    report = RatsnestReport(
        "demo.kicad_pcb", "11.0.0", "mm", 1, 49.8, (RatsnestNet("GND", 1, 49.8, (edge,)),)
    )
    calls: list[tuple[str, str]] = []

    def export_ratsnest(install, config, board, **kw):
        calls.append((install.version, Path(board).name))
        return report

    h.ctx.services.export_ratsnest = export_ratsnest
    sc = h.ok("kw_pcb_check", project=project, checks=["unrouted"])
    assert sc["provider_used"] == f"cli:11.0.0+{PATCH}" and sc["degraded"] == []
    assert calls == [("11.0.0", "demo.kicad_pcb")] and FakeSession.log == []  # no api-server for the walk
    item = sc["data"]["items"][0]
    assert item["check"] == "unrouted" and item["net"] == "GND" and item["length_mm"] == 49.8
    assert item["a"]["uuid"] == pad_uuid and item["a"]["layer"] == "F.Cu"
    assert item["a"]["ref"] == "C1" and item["a"]["pad"] == "2"  # joined to the file model by uuid
    assert item["b"]["uuid"] == "t-1" and item["b"]["layer"] == "B.Cu" and item["b"]["ref"] == "track"
    assert item["layer"] is None  # the two ends are on different layers
    forced = h.ok("kw_pcb_check", project=project, checks=["unrouted"], provider="ipc11")
    assert forced["provider_used"] == "ipc11:11.0.0" and "airwire_endpoints_estimated" in forced["degraded"]
    assert FakeSession.log[:2] == ["open:demo.kicad_pcb", "close:demo.kicad_pcb"]
