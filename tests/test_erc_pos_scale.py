"""`cli.erc_json_pos_scale` after the upstream fix (plan step S1, docs/decisions/D24-D31.md).

docs/VERIFICATION.md claim 1: kicad-cli 8.0.1-10.0.6 and the 10.99 nightly on 6dd893f7d1 write ERC
JSON positions 100x too small; upstream master 407f6293 / 10.0 6d8e1fea (milestone 10.0.7) fix it.
Offline: the runtime plausibility check (`kiwrap.backends.cli.erc.check_erc_pos_scale`) on synthetic
ERC JSON at both scales, anchored on real schematic files; the fixture probe and `run_erc` with
kicad-cli replaced; the D16 re-probe helpers of scripts/ci/refresh_matrix.py. Live (`kicad`): the
check and the probe agree with the shipped matrix on every installed kicad-cli with ERC.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from kiwrap.backends.cli import erc
from kiwrap.backends.cli.erc import (
    DEGRADED_POS_SCALE_CORRECTED,
    Anchor,
    ErcScaleCheck,
    SchematicEvidence,
    check_erc_pos_scale,
    paper_size_mm,
    plausible_erc_pos_scale,
    schematic_evidence,
)
from kiwrap.backends.cli.runner import CliResult
from kiwrap.capability import Capabilities, capabilities_for, load_matrix
from kiwrap.formats.schematic import SchematicFile
from kiwrap.formats.sexpr import loads
from kiwrap.installation import KiCadInstallation
from tests._fakes import ERC_UNITS_SPIKE

REPO = Path(__file__).resolve().parents[1]
HIER = REPO / "conformance" / "corpus" / "writer" / "hier_10"
SPIKE_SCH = ERC_UNITS_SPIKE / "t.kicad_sch"
KEY = "cli.erc_json_pos_scale"


def _affected_doc() -> dict[str, Any]:
    """Real 10.0.0 output for the spike schematic: positions 100x too small (claim 1)."""
    return json.loads((ERC_UNITS_SPIKE / "erc_10.0.json").read_text(encoding="utf-8"))


def _scaled(doc: dict[str, Any], factor: float) -> dict[str, Any]:
    """`doc` with every item pos multiplied by `factor` (x100 = what a fixed kicad-cli writes)."""
    out = copy.deepcopy(doc)
    for sheet in out["sheets"]:
        for v in sheet["violations"]:
            for item in v["items"]:
                if "pos" in item:
                    item["pos"] = {k: round(val * factor, 6) for k, val in item["pos"].items()}
    return out


def _pin_doc(truth_scale: float, n: int = 6) -> dict[str, Any]:
    """Synthetic erc.v1 JSON naming real pins of the hierarchical corpus' amp sheet, written the way
    a kicad-cli with `truth_scale` would (positions divided by it)."""
    amp = SchematicFile.load(HIER / "amp.kicad_sch")
    pins = [p for s in amp.symbols for p in s.pins if p.uuid][:n]
    items = [
        {
            "description": f"Symbol pin {p.number}",
            "pos": {"x": p.position.x / truth_scale, "y": p.position.y / truth_scale},
            "uuid": p.uuid,
        }
        for p in pins
    ]
    return {
        "$schema": "https://schemas.kicad.org/erc.v1.json",
        "coordinate_units": "mm",
        "kicad_version": "synthetic",
        "sheets": [
            {
                "path": "/amp_a/",
                "violations": [
                    {
                        "type": "pin_not_connected",
                        "severity": "error",
                        "description": "Pin not connected",
                        "items": [item],
                    }
                    for item in items
                ],
            }
        ],
    }


# --- the anchor check, both scales ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("truth", "recorded", "corrected"),
    [(100.0, 100.0, False), (100.0, 1.0, True), (1.0, 100.0, True), (1.0, 1.0, False)],
)
def test_anchor_check_on_the_spike_wire(truth: float, recorded: float, corrected: bool):
    """Affected (x100 small) and fixed (real mm) JSON of the same wire, under both records."""
    doc = _affected_doc() if truth == 100.0 else _scaled(_affected_doc(), 100.0)
    check = plausible_erc_pos_scale(doc, recorded, SPIKE_SCH)
    assert check.evidence == "anchor" and check.scale == truth and check.corrected is corrected
    assert dict(check.votes)[truth] >= 1 and check.checked >= 1
    report = erc.report_from_erc_json(doc, source="t", position_scale=check.scale)
    wire = next(v for v in report.violations if v.type == "wire_dangling")
    assert wire.pos is not None and (wire.pos.x_mm, wire.pos.y_mm) == pytest.approx((100.33, 50.8))
    assert wire.items[0].description.endswith("length 20.3200 mm") == (truth == 100.0)


@pytest.mark.parametrize("truth", [1.0, 100.0])
@pytest.mark.parametrize("recorded", [1.0, 100.0])
def test_anchor_check_on_hierarchical_pins(truth: float, recorded: float):
    """Pins of a sub-sheet: the evidence walk follows the sheet files from the root."""
    doc = _pin_doc(truth)
    check = plausible_erc_pos_scale(doc, recorded, HIER / "top.kicad_sch")
    assert check.evidence == "anchor" and check.scale == truth
    assert check.corrected is (truth != recorded)
    assert dict(check.votes)[truth] == check.checked == 6


def test_units_in_are_checked_in_mm():
    """`--units in` output: the check converts through coordinate_units before comparing."""
    doc = _affected_doc()
    doc["coordinate_units"] = "in"
    for item in (i for s in doc["sheets"] for v in s["violations"] for i in v["items"]):
        item["pos"] = {k: val / 25.4 for k, val in item["pos"].items()}
    assert plausible_erc_pos_scale(doc, 1.0, SPIKE_SCH).scale == 100.0


def test_ties_and_ambiguous_items_keep_the_record():
    ev = SchematicEvidence({"a": (Anchor.point(100.0, 50.0),), "b": (Anchor.point(1.0, 0.5),)})
    # "a" fits 100, "b" fits 1: one vote each -> the record stands
    tie = check_erc_pos_scale([("a", 1.0, 0.5), ("b", 1.0, 0.5)], 1.0, ev)
    assert tie.evidence == "anchor" and tie.scale == 1.0 and not tie.corrected
    # an item at the origin fits every scale: no vote, and nothing else decides
    origin = check_erc_pos_scale([("o", 0.0, 0.0)], 100.0, SchematicEvidence({"o": (Anchor.point(0, 0),)}))
    assert origin.evidence == "none" and origin.scale == 100.0 and origin.votes == ((1.0, 0), (100.0, 0))


def test_segment_distance():
    seg = Anchor(0.0, 0.0, 10.0, 0.0)
    assert seg.distance(5.0, 3.0) == pytest.approx(3.0)
    assert seg.distance(-4.0, 3.0) == pytest.approx(5.0)
    assert Anchor.point(1.0, 1.0).distance(4.0, 5.0) == pytest.approx(5.0)


# --- the page fallback -------------------------------------------------------------------------------------


def test_page_bounds_decide_when_no_anchor_matches():
    a4 = SchematicEvidence({}, (297.0, 210.0))
    small = [("x", 1.5494, 0.1778), ("y", 2.6035, 1.3589)]  # a 100x-too-small report
    real = [("x", 154.94, 17.78), ("y", 260.35, 135.89)]
    up = check_erc_pos_scale(small, 1.0, a4)
    assert (up.evidence, up.scale, up.corrected) == ("page", 100.0, True)
    down = check_erc_pos_scale(real, 100.0, a4)
    assert (down.evidence, down.scale, down.corrected) == ("page", 1.0, True)
    for pts, rec in ((small, 100.0), (real, 1.0)):
        ok = check_erc_pos_scale(pts, rec, a4)
        assert (ok.evidence, ok.scale, ok.corrected) == ("page", rec, False)
    # items near the origin under both scales: nothing is decisive
    assert check_erc_pos_scale([("z", 0.0, 0.0)], 1.0, a4).evidence == "none"


def test_no_items_and_no_evidence():
    assert check_erc_pos_scale([], 100.0, SchematicEvidence()) == ErcScaleCheck(100.0, 100.0, "no_items")
    none = check_erc_pos_scale([("u", 1.0, 0.5)], 100.0, SchematicEvidence())
    assert none.evidence == "none" and not none.corrected


def test_paper_sizes():
    def node(text: str):
        return loads(text)

    assert paper_size_mm(node('(paper "A4")')) == (297.0, 210.0)
    assert paper_size_mm(node('(paper "A3" portrait)')) == (297.0, 420.0)
    assert paper_size_mm(node('(paper "User" 500 250)')) == (500.0, 250.0)
    assert paper_size_mm(node('(paper "USLetter")')) == (279.4, 215.9)
    assert paper_size_mm(node('(paper "Weird")')) is None and paper_size_mm(None) is None


# --- evidence gathering --------------------------------------------------------------------------------------


def test_evidence_stops_once_every_uuid_is_anchored(monkeypatch):
    loaded: list[str] = []
    real_load = SchematicFile.load

    def spy(path):
        loaded.append(Path(path).name)
        return real_load(path)

    monkeypatch.setattr(SchematicFile, "load", staticmethod(spy))
    top = SchematicFile.load(HIER / "top.kicad_sch")
    loaded.clear()
    want = {top.wires[0].uuid}
    ev = schematic_evidence(HIER / "top.kicad_sch", want)
    assert loaded == ["top.kicad_sch"] and ev.files == 1 and want <= set(ev.anchors)
    assert ev.page_mm == (297.0, 210.0)
    loaded.clear()
    full = schematic_evidence(HIER / "top.kicad_sch", {"not-there"})
    assert loaded == ["top.kicad_sch", "amp.kicad_sch", "source.kicad_sch"] and full.files == 3


def test_unreadable_sheets_are_skipped(tmp_path: Path):
    root = tmp_path / "root.kicad_sch"
    root.write_text(
        '(kicad_sch (version 20231120) (generator "eeschema") (uuid "r") (paper "A3")\n'
        ' (sheet (at 10 10) (size 20 10) (uuid "s1")\n'
        '  (property "Sheetname" "bad") (property "Sheetfile" "bad.kicad_sch")\n'
        '  (pin "IN" input (at 10 15 180) (uuid "p1")))\n'
        ' (sheet (at 50 10) (size 20 10) (uuid "s2")\n'
        '  (property "Sheetname" "gone") (property "Sheetfile" "missing.kicad_sch"))\n'
        ' (label "N" (at 30.48 25.4 0) (uuid "l1")))\n',
        encoding="utf-8",
    )
    (tmp_path / "bad.kicad_sch").write_text("(not a schematic", encoding="utf-8")
    ev = schematic_evidence(root)
    assert ev.files == 1 and ev.page_mm == (420.0, 297.0)
    assert ev.anchors["l1"][0] == Anchor.point(30.48, 25.4)
    assert ev.anchors["p1"][0] == Anchor.point(10.0, 15.0)
    # the sheet outline is four segments besides its origin
    assert len(ev.anchors["s1"]) == 5 and min(a.distance(30.0, 15.0) for a in ev.anchors["s1"]) == 0


def test_schematic_is_not_read_without_items_and_errors_never_escape():
    doc = _affected_doc()
    empty = copy.deepcopy(doc)
    for sheet in empty["sheets"]:
        sheet["violations"] = []

    def boom(path: Path, want: set[str]) -> SchematicEvidence:
        raise RuntimeError("must not be called / must be swallowed")

    assert plausible_erc_pos_scale(empty, 100.0, SPIKE_SCH, evidence_for=boom).evidence == "no_items"
    failed = plausible_erc_pos_scale(doc, 100.0, SPIKE_SCH, evidence_for=boom)
    assert failed.evidence == "none" and failed.scale == 100.0


# --- run_erc and the fixture probe with kicad-cli replaced --------------------------------------------------


class _Config:
    """Stands in for a ConfigRoot: the replaced runner never touches it."""


def _install(tmp_path: Path) -> KiCadInstallation:
    return KiCadInstallation(
        root=tmp_path,
        kicad_cli=tmp_path / "kicad-cli.exe",
        share=tmp_path / "share",
        version="10.0.7",
        source="explicit",
    )


def _caps(install: KiCadInstallation, scale: float) -> Capabilities:
    return Capabilities(install, {"cli.has_erc": True, KEY: scale}, probed=True)


def _fake_cli(monkeypatch, outputs: dict[str, str]) -> list[list[str]]:
    """Replace erc.run_kicad_cli: write outputs[<--format>] to the `-o` path."""
    calls: list[list[str]] = []

    def fake(install, args, *, config, caps=None, timeout_s=300, **kw):
        argv = [str(a) for a in args]
        calls.append(argv)
        out = Path(argv[argv.index("-o") + 1])
        out.write_text(outputs[argv[argv.index("--format") + 1]], encoding="utf-8")
        return CliResult(argv=argv, returncode=5, stdout="", stderr="", duration_s=0.0)

    monkeypatch.setattr(erc, "run_kicad_cli", fake)
    return calls


@pytest.mark.parametrize(("truth", "recorded"), [(100.0, 100.0), (100.0, 1.0), (1.0, 100.0), (1.0, 1.0)])
def test_run_erc_uses_the_plausible_scale_and_tags_degraded(monkeypatch, tmp_path: Path, truth, recorded):
    doc = _affected_doc() if truth == 100.0 else _scaled(_affected_doc(), 100.0)
    _fake_cli(monkeypatch, {"json": json.dumps(doc)})
    sch = tmp_path / "t.kicad_sch"
    shutil.copy(SPIKE_SCH, sch)
    install = _install(tmp_path)
    report = erc.run_erc(install, _Config(), sch, caps=_caps(install, recorded))  # type: ignore[arg-type]
    cli = report.raw["_cli"]
    assert cli["pos_scale"]["used"] == truth and cli["pos_scale"]["recorded"] == recorded
    assert cli["pos_scale"]["evidence"] == "anchor"
    wire = next(v for v in report.violations if v.type == "wire_dangling")
    assert wire.pos is not None and wire.pos.x_mm == pytest.approx(100.33)
    if truth != recorded:
        assert cli["degraded"] == [DEGRADED_POS_SCALE_CORRECTED]
        assert KEY in cli["warnings"][0] and "refresh_matrix.py" in cli["warnings"][0]
    else:
        assert "degraded" not in cli and "warnings" not in cli
    # raw is rescaled consistently with the violations (one rescaler, kiwrap.formats)
    assert report.raw["sheets"][0]["violations"][0]["items"][0]["pos"]["x"] == pytest.approx(100.33)


def test_run_erc_verify_scale_off_trusts_the_record(monkeypatch, tmp_path: Path):
    _fake_cli(monkeypatch, {"json": json.dumps(_affected_doc())})
    sch = tmp_path / "t.kicad_sch"
    shutil.copy(SPIKE_SCH, sch)
    install = _install(tmp_path)
    report = erc.run_erc(install, _Config(), sch, caps=_caps(install, 1.0), verify_scale=False)  # type: ignore[arg-type]
    assert report.violations[0].pos is not None and report.violations[0].pos.x_mm == pytest.approx(1.0033)
    assert "degraded" not in report.raw["_cli"]


@pytest.mark.parametrize("truth", [1.0, 100.0])
def test_fixture_probe_measures_both_scales(monkeypatch, tmp_path: Path, truth: float):
    json_doc = _affected_doc() if truth == 100.0 else _scaled(_affected_doc(), 100.0)
    text = (ERC_UNITS_SPIKE / "erc_10.0.txt").read_text(encoding="utf-8", errors="replace")
    calls = _fake_cli(monkeypatch, {"json": json.dumps(json_doc), "report": text})
    install = _install(tmp_path)
    probe = erc.probe_erc_pos_scale(install, _Config(), SPIKE_SCH, tmp_path / "w", caps=_caps(install, 7))  # type: ignore[arg-type]
    assert probe is not None and probe.scale == truth and probe.ratio == pytest.approx(truth)
    assert probe.pairs >= 2
    # it ran on a copy, never on the fixture itself
    assert all(str(SPIKE_SCH) not in argv for argv in calls) and (tmp_path / "w" / "t.kicad_sch").is_file()


# --- the D16 re-probe helpers of scripts/ci/refresh_matrix.py ------------------------------------------------


def _refresh_matrix() -> ModuleType:
    path = REPO / "scripts" / "ci" / "refresh_matrix.py"
    spec = importlib.util.spec_from_file_location("refresh_matrix_under_test", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_erc_scale_is_on_the_nightly_reprobe_list():
    rm = _refresh_matrix()
    assert KEY in rm.NIGHTLY_REPROBE_KEYS and KEY in rm.REPROBES
    assert rm.ERC_SCALE_FIXTURE.is_file()


def test_promote_never_promotes_an_unmeasured_reprobe_key():
    rm = _refresh_matrix()
    values: dict[str, Any] = {}
    prov: dict[str, str] = {KEY: "docs"}
    caps = {KEY: 100, "cli.has_erc": True, "swig.available": False}
    base = {KEY: 1, "cli.has_erc": False, "swig.available": False}
    rm.promote(caps, {}, base, values, prov)
    assert KEY not in values and prov[KEY] == "docs" and values["cli.has_erc"] is True
    rm.promote(caps, {KEY: 100}, base, values, prov)
    assert values[KEY] == 100 and prov[KEY] == "verified"
    # a later nightly measures the base's value: the override goes, the provenance stays verified
    rm.promote(caps, {KEY: 1}, base, values, prov)
    assert KEY not in values and prov[KEY] == "verified"


def test_promote_removes_a_promoted_key_the_install_no_longer_reports():
    """Review finding 12: a fork-only `--help` flag folded in from a bench build stayed in the stock
    entry, marked verified, because promote() only visited the keys the current install had."""
    rm = _refresh_matrix()
    flag = "cli.flags.pcb export ratsnest"
    values: dict[str, Any] = {flag: ["--help", "--output"], "cli.has_erc": True}
    prov: dict[str, str] = {flag: "verified", "cli.has_erc": "verified"}
    touched = rm.promote({"cli.has_erc": True}, {}, {"cli.has_erc": False}, values, prov)
    assert flag not in values and flag not in prov and touched == 1
    # a key the seed carries, or an unmeasured re-probe key, is left alone
    values = {KEY: 100, "cli.x": 1}
    prov = {KEY: "verified", "cli.x": "docs"}
    rm.promote({}, {}, {"cli.x": 2}, values, prov)
    assert values == {KEY: 100, "cli.x": 1} and prov == {KEY: "verified", "cli.x": "docs"}


@dataclass
class _Inst:
    version: str
    root: Path
    extra: dict[str, str]

    @property
    def is_bench_build(self) -> bool:
        return bool(self.extra.get("commit"))

    @property
    def major_minor(self) -> str:
        return ".".join(self.version.split(".")[:2])


def test_select_installs_refreshes_a_semver_from_one_stock_install(tmp_path: Path):
    """Review finding 12: every 10.99 nightly and bench build reports '10.99.0'; the entry is
    refreshed from the first stock install only - bench builds need an explicit --root."""
    rm = _refresh_matrix()
    stock = _Inst("10.99.0", tmp_path / "stock", {})
    bench6 = _Inst("10.99.0", tmp_path / "b6", {"commit": "6dd893f7d1"})
    bench2 = _Inst("10.99.0", tmp_path / "b2", {"commit": "6dd893f7d1"})
    other = _Inst("10.99.0", tmp_path / "second-stock", {})
    stable = _Inst("10.0.6", tmp_path / "10.0", {})
    chosen, notes = rm.select_installs([stock, bench6, bench2, other, stable])
    assert chosen == [stock, stable]
    assert sum("bench build" in n for n in notes) == 2 and any("one install per semver" in n for n in notes)
    chosen, _ = rm.select_installs([stock, bench6, stable], only=["10.99.0"], roots=[str(bench6.root)])
    assert chosen == [bench6]
    chosen, _ = rm.select_installs([bench6, stock], only=["10.0.6"])
    assert chosen == []


def test_refresh_main_never_folds_a_bench_build_into_the_stock_entry(tmp_path: Path, monkeypatch):
    """The finding's repro in miniature: the stock nightly, then a patched bench build with fork-only
    flags; the written 10.99.0 entry carries the stock install's keys only."""
    rm = _refresh_matrix()
    matrix = tmp_path / "matrix.json"
    shutil.copy(rm.MATRIX_PATH, matrix)
    stock = _Inst("10.99.0", tmp_path / "stock", {})
    bench = _Inst("10.99.0", tmp_path / "bench", {"commit": "6dd893f7d1"})
    fork_flag = "cli.flags.pcb export ratsnest"
    records = {
        str(stock.root): {"probe.ok": True, "cli.has_erc": True},
        str(bench.root): {
            "probe.ok": True,
            "cli.has_erc": True,
            fork_flag: ["--help"],
            "kiwrap.patches": ["x"],
        },
    }
    monkeypatch.setattr(rm, "MATRIX_PATH", matrix)
    monkeypatch.setattr(rm, "discover", lambda: [stock, bench])
    monkeypatch.setattr(rm, "capabilities_for", lambda inst, **kw: dict(records[str(inst.root)]))
    assert rm.main(["--only", "10.99.0", "--skip-reprobe"]) == 0
    entry = json.loads(matrix.read_text(encoding="utf-8"))["versions"]["10.99.0"]
    assert fork_flag not in entry["values"]
    # named explicitly it is refreshed - and a patched record still needs that explicit choice
    monkeypatch.setattr(rm, "discover", lambda: [bench])
    assert rm.main(["--only", "10.99.0", "--skip-reprobe"]) == 0
    assert fork_flag not in json.loads(matrix.read_text(encoding="utf-8"))["versions"]["10.99.0"]["values"]
    assert rm.main(["--only", "10.99.0", "--skip-reprobe", "--root", str(bench.root)]) == 0
    assert fork_flag in json.loads(matrix.read_text(encoding="utf-8"))["versions"]["10.99.0"]["values"]


def test_merge_note_keeps_hand_written_sources_and_replaces_the_measurement():
    rm = _refresh_matrix()
    old = "probed live on windows 10.99.0 | cli.erc_json_pos_scale=100 verified by hand | ipc.* verified X"
    new = rm.merge_note(
        old, "probed live on windows 10.99.0 (b2)", {KEY: "cli.erc_json_pos_scale=1 probed D"}
    )
    assert new == "probed live on windows 10.99.0 (b2) | cli.erc_json_pos_scale=1 probed D | ipc.* verified X"
    assert rm.merge_note(old, "h", {}) == "h | cli.erc_json_pos_scale=100 verified by hand | ipc.* verified X"


# --- live ------------------------------------------------------------------------------------------------------


@pytest.mark.kicad
@pytest.mark.usefixtures("user_config_untouched")
def test_live_check_and_probe_agree_with_the_matrix(install, config_root, storage_home, tmp_path: Path):
    """On every discovered install (stock releases, the nightly, bench builds): the shipped record
    is right for its kicad-cli (no correction), a deliberately wrong record is corrected by the
    anchors, and the fixture probe measures the recorded value."""
    caps = capabilities_for(install, storage_home=storage_home)
    if not caps.has("cli.has_erc"):
        pytest.skip(f"KiCad {install.version} has no sch erc")
    config = config_root(install)
    sch = tmp_path / "t.kicad_sch"
    shutil.copy(SPIKE_SCH, sch)
    recorded = float(caps[KEY])
    report = erc.run_erc(install, config, sch, caps=caps, out_json=tmp_path / "a.erc.json")
    assert report.raw["_cli"]["pos_scale"]["evidence"] == "anchor"
    assert report.raw["_cli"]["pos_scale"]["corrected"] is False, report.raw["_cli"]["pos_scale"]
    wrong = 1.0 if recorded == 100.0 else 100.0
    bad = Capabilities(install, {**dict(caps), KEY: wrong}, probed=True)
    fixed = erc.run_erc(install, config, sch, caps=bad, out_json=tmp_path / "b.erc.json")
    assert fixed.raw["_cli"]["degraded"] == [DEGRADED_POS_SCALE_CORRECTED]
    dangling = next(v for v in fixed.violations if v.type == "wire_dangling")
    assert dangling.pos is not None and dangling.pos.x_mm == pytest.approx(100.33)
    probe = erc.probe_erc_pos_scale(install, config, SPIKE_SCH, tmp_path / "probe", caps=caps)
    assert probe is not None and probe.scale == recorded


# --- the shipped records ---------------------------------------------------------------------------------------


def test_matrix_records_the_upstream_fix():
    """10.0.7 (6d8e1fea) and 11.0.0 (407f6293) are fixed; 10.0.6 and the 6dd893f7d1 nightly are not."""
    m = load_matrix()
    assert m.versions["10.0.6"][KEY] == 100 and m.provenance["10.0.6"][KEY] == "verified"
    assert m.versions["10.0.7"][KEY] == 1 and "6d8e1fea" in m.notes["10.0.7"]
    assert m.versions["11.0.0"][KEY] == 1 and "407f6293" in m.notes["11.0.0"]
    assert m.versions["10.99.0"][KEY] == 100 and m.provenance["10.99.0"][KEY] == "verified"
    assert "6dd893f7d1" in m.notes["10.99.0"]
    for ver in m.known():
        assert m.versions[ver][KEY] in (1, 100), ver
