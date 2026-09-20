"""Unit tests for the SES reader / apply (no KiCad needed).

Offline oracles: the session files Freerouting 2.4.1 wrote in the research spikes and the boards
KiCad 10.0.x's own `ImportSpecctraSES` produced from them (docs/VERIFICATION.md claim 7:
`spikes/autorouting/k10/tiny_routed.kicad_pcb`, `spikes/specctra-verify/imported_10.0*.kicad_pcb`).
The live twin (SWIG import of the same session on the demo boards) is conformance test T7.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiwrap.formats.board import BoardFile
from kiwrap.formats.board_writer import BoardWriter
from kiwrap.formats.epochs import read_stamp
from kiwrap.formats.sexpr import load
from kiwrap.formats.specctra import SesReader, SpecctraError, drill_from_padstack_id, session_scale

REPO = Path(__file__).resolve().parent.parent
SPIKES = REPO / "spikes"
K10 = SPIKES / "autorouting" / "k10"
VERIFY = SPIKES / "specctra-verify"
MED_SES = SPIKES / "autorouting" / "med10" / "medium.ses"
BLINKER = REPO / "tests" / "fixtures" / "specctra" / "blinker_10" / "blinky.kicad_pcb"


def writer_for(tmp_path: Path, board: Path) -> BoardWriter:
    stamp = read_stamp(load(board))
    w = BoardWriter(tmp_path, epoch={"pcb": stamp})
    w.load(board)
    w.board_path = tmp_path / board.name
    return w


def track_keys(board: BoardFile) -> set[tuple]:
    out = set()
    for t in board.tracks:
        ends = tuple(
            sorted([(round(t.start.x, 3), round(t.start.y, 3)), (round(t.end.x, 3), round(t.end.y, 3))])
        )
        out.add((t.layer, round(t.width, 3), t.net.name if t.net else "", ends, t.locked))
    return out


def via_keys(board: BoardFile, default_drill: float) -> set[tuple]:
    return {
        (
            round(v.position.x, 3),
            round(v.position.y, 3),
            round(v.size, 3),
            round(v.drill or default_drill, 3),
            v.layers,
            v.net.name if v.net else "",
            v.kind,
            v.locked,
        )
        for v in board.vias
    }


# ------------------------------------------------------------------------------------------------
# parsing
# ------------------------------------------------------------------------------------------------


def test_session_scale_and_drill_parsing():
    assert session_scale("um", 10) == pytest.approx(0.0001)
    assert session_scale("mm", 1) == 1.0
    assert session_scale("mil", 1) == pytest.approx(0.0254)
    assert session_scale("inch", 1) == 25.4
    assert drill_from_padstack_id("Via[0-1]_600:300_um") == pytest.approx(0.3)
    assert drill_from_padstack_id("Via[0-3]_1200:600_um") == pytest.approx(0.6)
    assert drill_from_padstack_id("Round[A]Pad_1600.000000_um") is None
    assert drill_from_padstack_id("Via_x:y_um") is None


def test_freerouting_session_is_read_in_millimetres():
    ses = SesReader((K10 / "tiny.ses").read_text(encoding="utf-8"))
    assert ses.name == "tiny"
    assert len(ses.wires) == 2 and not ses.vias
    w = {x.net: x for x in ses.wires}
    assert w["NET1"].layer == "F.Cu" and w["NET1"].width == pytest.approx(0.2)
    assert w["NET1"].points[0] == (
        pytest.approx(14.0875),
        pytest.approx(7.5),
    )  # y negated back to KiCad's frame
    assert [p.ref for p in ses.placements] == ["R2", "R1"]
    assert ses.placements[1].x == pytest.approx(5.0) and ses.placements[1].y == pytest.approx(7.5)
    ps = ses.padstacks["Via[0-1]_600:300_um"]
    assert (
        ps.diameter == pytest.approx(0.6) and ps.drill == pytest.approx(0.3) and ps.layers == ("F.Cu", "B.Cu")
    )


def test_medium_session_counts_and_widths():
    ses = SesReader(MED_SES.read_text(encoding="utf-8"))
    assert len(ses.vias) == 26 and len(ses.wires) == 90
    widths = {round(w.width, 3) for w in ses.wires}
    assert widths == {0.2, 0.15}  # the fanout pre-pass neck-downs documented in claim 8
    assert all(len(w.points) >= 2 for w in ses.wires)


def test_malformed_sessions_are_rejected():
    with pytest.raises(SpecctraError):
        SesReader("(pcb x)")
    with pytest.raises(SpecctraError):
        SesReader("(session x (placement))")
    with pytest.raises(SpecctraError):
        SesReader("(session x (routes (resolution um 10)))")


# ------------------------------------------------------------------------------------------------
# apply: KiCad's own import as the oracle
# ------------------------------------------------------------------------------------------------


def test_apply_matches_kicads_import_on_the_tiny_board(tmp_path: Path):
    w = writer_for(tmp_path, K10 / "tiny.kicad_pcb")
    report = SesReader((K10 / "tiny.ses").read_text(encoding="utf-8")).apply(w)
    out = w.save()
    ours = BoardFile.load(out, project_rules=False)
    theirs = BoardFile.load(K10 / "tiny_routed.kicad_pcb", project_rules=False)
    assert track_keys(ours) == track_keys(theirs)
    assert via_keys(ours, 0.3) == via_keys(theirs, 0.3)
    assert report.tracks_added == 6 and report.vias_added == 0 and report.tracks_removed == 0
    assert report.placement_ignored == 2 and report.nets_routed == ["NET1", "NET2"]
    assert not report.warnings and report.skipped == 0
    # KiCad's import leaves the footprints where they were; so does the default apply
    assert {f.reference: f.position for f in ours.footprints} == {
        f.reference: f.position for f in theirs.footprints
    }


def test_apply_deletes_unlocked_keeps_locked_and_matches_kicad_without_placement(tmp_path: Path):
    """`imported_10.0_np.kicad_pcb` is KiCad 10's import of `test_noplace.ses` (claim 7): the
    unlocked track/vias were deleted, the locked segment and via survived, the session's wires and
    via were added with their own widths/padstack. Re-applying the session onto that result must
    reproduce it exactly (the unlocked copper is replaced by the same copper)."""
    base = VERIFY / "imported_10.0_np.kicad_pcb"
    w = writer_for(tmp_path, base)
    report = SesReader((VERIFY / "test_noplace.ses").read_text(encoding="utf-8")).apply(
        w, default_via_drill=0.3
    )
    ours = BoardFile.load(w.save(), project_rules=False)
    theirs = BoardFile.load(base, project_rules=False)
    assert track_keys(ours) == track_keys(theirs)
    assert via_keys(ours, 0.3) == via_keys(theirs, 0.3)
    assert report.tracks_removed == 3 and report.vias_removed == 1  # the unlocked ones only
    assert report.tracks_added == 3 and report.vias_added == 1
    assert sum(1 for t in ours.tracks if t.locked) == 1 and sum(1 for v in ours.vias if v.locked) == 1
    assert report.placement_ignored == 0


def test_apply_placement_moves_flips_and_rotates_like_kicad(tmp_path: Path):
    base = VERIFY / "imported_10.0_np.kicad_pcb"  # footprints still at their original places
    w = writer_for(tmp_path, base)
    report = SesReader((VERIFY / "test.ses").read_text(encoding="utf-8")).apply(w, apply_placement=True)
    ours = BoardFile.load(w.save(), project_rules=False)
    theirs = BoardFile.load(VERIFY / "imported_10.0.kicad_pcb", project_rules=False)
    for ref in ("R1", "R2", "R3"):
        a, b = ours.footprint(ref), theirs.footprint(ref)
        assert a is not None and b is not None
        assert a.position == b.position and a.layer == b.layer, ref
        assert (a.rotation - b.rotation) % 360 == 0, (ref, a.rotation, b.rotation)
        assert {(p.number, p.position) for p in a.pads} == {(p.number, p.position) for p in b.pads}, ref
    assert report.placement_ignored == 0
    # KiCad ends its import with BuildConnectivity(), which re-propagates track net codes from the
    # pads the moved footprints now touch (claim 7); the codec keeps the session's net names, so
    # compare the copper geometry without the nets here.
    strip = lambda keys: {(k[0], k[1], k[3], k[4]) for k in keys}  # noqa: E731
    assert strip(track_keys(ours)) == strip(track_keys(theirs))
    assert {v[:5] + v[6:] for v in via_keys(ours, 0.3)} == {v[:5] + v[6:] for v in via_keys(theirs, 0.3)}


def test_default_apply_ignores_placement_and_reports_it(tmp_path: Path):
    base = VERIFY / "imported_10.0_np.kicad_pcb"
    w = writer_for(tmp_path, base)
    before = {
        f.reference: (f.position, f.rotation, f.layer)
        for f in BoardFile.load(base, project_rules=False).footprints
    }
    report = SesReader((VERIFY / "test.ses").read_text(encoding="utf-8")).apply(w)
    after = {
        f.reference: (f.position, f.rotation, f.layer)
        for f in BoardFile.load(w.save(), project_rules=False).footprints
    }
    assert before == after and report.placement_ignored == 3


# ------------------------------------------------------------------------------------------------
# apply: edge cases on a hand-built session
# ------------------------------------------------------------------------------------------------


def _session(body: str, padstacks: str = "") -> str:
    return (
        "(session s (base_design s)\n"
        "  (routes (resolution um 10)\n"
        "    (library_out\n"
        '      (padstack "Via[0-1]_600:300_um" (shape (circle F.Cu 6000 0 0)) (shape (circle B.Cu 6000 0 0)) (attach off))\n'
        f"{padstacks}    )\n"
        f"    (network_out\n{body}    )\n  )\n)\n"
    )


def test_keep_nets_fix_wires_unknown_items_and_user_layer_names(tmp_path: Path):
    w = writer_for(tmp_path, BLINKER)
    root = w.root
    assert root is not None
    # give the board a user layer name, like ecc83's top_cu, and check the mapping back
    layers = root.find("layers")
    assert layers is not None
    for row in layers.nodes:
        if row.values()[1] == "F.Cu":
            row.append("top_cu")
    before = BoardFile.from_node(root)
    from collections import Counter

    keep_net = Counter(t.net.name for t in before.tracks if t.net).most_common(1)[0][0]
    gnd_tracks = sum(1 for t in before.tracks if t.net and t.net.name == keep_net)
    total = len(before.tracks)
    assert gnd_tracks and total > gnd_tracks
    body = (
        '      (net "/VIN" (wire (path top_cu 2500 100000 -100000 120000 -100000) (type fix)) (via "Via[0-1]_600:300_um" 120000 -100000))\n'
        '      (net "NOPE" (wire (path B.Cu 2000 50000 -50000 60000 -50000)))\n'
        '      (net "GND" (wire (path Nowhere 2000 50000 -50000 60000 -50000)) (via "Missing" 1 1))\n'
    )
    report = SesReader(_session(body)).apply(w, keep_nets=[keep_net], default_via_drill=0.3)
    after = BoardFile.load(w.save(), project_rules=False)
    assert report.tracks_removed == total - gnd_tracks
    assert sum(1 for t in after.tracks if t.net and t.net.name == keep_net) == gnd_tracks  # kept copper
    assert report.skipped == 2 and len([x for x in report.warnings if "Nowhere" in x or "Missing" in x]) == 2
    assert any("NOPE" in x for x in report.warnings)
    vin = [t for t in after.tracks if t.net and t.net.name == "/VIN"]
    assert len(vin) == 1 and vin[0].layer == "F.Cu" and vin[0].locked and vin[0].width == pytest.approx(0.25)
    nope = [t for t in after.tracks if t.net is None]
    assert len(nope) == 1 and nope[0].layer == "B.Cu"
    vias = [v for v in after.vias if v.net and v.net.name == "/VIN"]
    assert len(vias) == 1 and vias[0].size == pytest.approx(0.6) and vias[0].drill == pytest.approx(0.3)
    assert vias[0].position.x == pytest.approx(12.0) and vias[0].position.y == pytest.approx(10.0)
    assert report.nets_routed == ["/VIN"]


def test_blind_and_micro_vias_from_partial_padstacks(tmp_path: Path):
    src = BLINKER.read_text(encoding="utf-8").replace(
        '(0 "F.Cu" signal)', '(0 "F.Cu" signal)\n\t\t(4 "In1.Cu" signal)\n\t\t(6 "In2.Cu" signal)', 1
    )
    board = tmp_path / "four.kicad_pcb"
    board.write_text(src, encoding="utf-8")
    w = writer_for(tmp_path, board)
    padstacks = (
        '      (padstack "Via[0-1]_300:100_um" (shape (circle F.Cu 3000 0 0)) (shape (circle In1.Cu 3000 0 0)) (attach off))\n'
        '      (padstack "Via[1-2]_500:200_um" (shape (circle In1.Cu 5000 0 0)) (shape (circle In2.Cu 5000 0 0)) (attach off))\n'
        '      (padstack "Via[0-2]_500:200_um" (shape (circle F.Cu 5000 0 0)) (shape (circle In1.Cu 5000 0 0)) (shape (circle In2.Cu 5000 0 0)) (attach off))\n'
    )
    body = (
        '      (net "GND" (via "Via[0-1]_300:100_um" 100000 -100000) (via "Via[1-2]_500:200_um" 110000 -100000)'
        ' (via "Via[0-2]_500:200_um" 120000 -100000) (via "Via[0-1]_600:300_um" 130000 -100000))\n'
    )
    report = SesReader(_session(body, padstacks)).apply(w)
    after = BoardFile.load(w.save(), project_rules=False)
    kinds = {
        round(v.position.x, 1): (v.kind, v.layers, round(v.size, 2), round(v.drill, 2)) for v in after.vias
    }
    assert kinds == {
        10.0: ("micro", ("F.Cu", "In1.Cu"), 0.3, 0.1),
        11.0: ("blind", ("In1.Cu", "In2.Cu"), 0.5, 0.2),  # buried: KiCad's file token is `blind`
        12.0: ("blind", ("F.Cu", "In2.Cu"), 0.5, 0.2),
        13.0: ("through", ("F.Cu", "B.Cu"), 0.6, 0.3),
    }
    assert report.vias_added == 4 and not report.warnings


def test_keep_locked_false_deletes_everything(tmp_path: Path):
    w = writer_for(tmp_path, VERIFY / "imported_10.0_np.kicad_pcb")
    report = SesReader(_session("")).apply(w, keep_locked=False)
    after = BoardFile.load(w.save(), project_rules=False)
    assert not after.tracks and not after.vias
    assert report.tracks_removed == 4 and report.vias_removed == 2


def test_delete_unlocked_false_keeps_the_existing_copper(tmp_path: Path):
    w = writer_for(tmp_path, K10 / "tiny_routed.kicad_pcb")
    report = SesReader(_session("")).apply(w, delete_unlocked=False)
    assert report.tracks_removed == 0 and len(BoardFile.load(w.save(), project_rules=False).tracks) == 6
