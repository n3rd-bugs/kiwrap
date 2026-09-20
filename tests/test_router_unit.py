"""`kiwrap.backends.router` without a JVM or KiCad: engine discovery over a fake storage tree, the
router rules derived from real board models (the conformance corpus + KiCad demo files), the DSN
post-processing hooks on the spike DSNs, Freerouting's log parser on captured lines, the per-run
profile, and the `Router` process plumbing (argv, progress, result, SES, timeout, cancel, failure)
against a Python script that impersonates `java -jar freerouting.jar` (`FAKE_FREEROUTING`).
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from kiwrap.backends.router import (
    EngineMissing,
    Engines,
    RouteProgress,
    Router,
    RouterError,
    RouterTimeout,
    RouteRules,
    discover_engines,
    dsn_post,
    rules_from_board,
)
from kiwrap.backends.router.freerouting import (
    ENFORCED_PROFILE,
    RouterCancelled,
    _LogParser,
    write_profile,
)
from kiwrap.backends.router.rules import bga_references, netclass_nets, plane_nets
from kiwrap.formats.board import BoardFile

REPO = Path(__file__).resolve().parents[1]
SPIKE_DSN = REPO / "spikes" / "specctra-verify" / "out_10.0.dsn"
TINY_BOARD = REPO / "conformance" / "corpus" / "10.0" / "tiny_routed" / "tiny_routed.kicad_pcb"

# A stand-in for `java -jar freerouting-2.4.1.jar …`: prints the 2.4.1 log lines the parser keys
# on, writes the SES and the result manifest. Behaviour switches on a `MODE:<x>` marker in the DSN.
FAKE_FREEROUTING = r"""
import json, os, re, sys, time
args = sys.argv[1:]
def opt(flag):
    for i, a in enumerate(args):
        if a == flag and i + 1 < len(args):
            return args[i + 1]
    return None
def kv(prefix):
    for a in args:
        if a.startswith(prefix):
            return a[len(prefix):]
    return None
print("2026-09-20 05:00:00.000 INFO   Freerouting v2.4.1 (build-date: 2026-09-03)", flush=True)
if "-help" in args or "-h" in args:
    print("USAGE"); sys.exit(0)
dsn = opt("-de"); ses = opt("-do"); passes = int(opt("-mp") or 10)
text = open(dsn, encoding="utf-8").read()
m = re.search(r"MODE:(\w+)", text)
mode = m.group(1) if m else "ok"
user_data = kv("--user_data_path=")
if user_data:
    prof = os.path.join(user_data, "freerouting.json")
    doc = json.load(open(prof, encoding="utf-8")) if os.path.exists(prof) else {}
    doc.setdefault("touched_by_fake", True)
    json.dump(doc, open(prof, "w", encoding="utf-8"))
if mode == "fail":
    print("2026-09-20 05:00:01.000 ERROR  Could not load the design: bad boundary", flush=True)
    sys.exit(3)
if mode == "hang":
    print("2026-09-20 05:00:01.000 INFO   [A\\B] Auto-routing stage started on board 'x' for 5 unrouted items.", flush=True)
    for i in range(1, 1000):
        print(f"2026-09-20 05:00:01.000 INFO   [A\\B] Auto-routing pass #{i} on board 'x' was completed in 0.10 seconds with score 100.00 (5 unrouted and 0 violations).", flush=True)
        time.sleep(0.2)
    sys.exit(0)
if mode == "slowexit":
    # the session is saved early, then the JVM takes its time shutting down (past a short budget)
    print("2026-09-20 05:00:01.000 INFO   [A\\B] Auto-routing stage started on board 'x' for 1 unrouted items.", flush=True)
    print("2026-09-20 05:00:01.000 INFO   [A\\B] Auto-routing pass #1 on board 'x' was completed in 0.10 seconds with score 99.50 (0 unrouted and 0 violations).", flush=True)
    print("2026-09-20 05:00:02.000 INFO   [A\\B] Job 'A\\B' finished with state: COMPLETED (elapsed: 0.5 seconds).", flush=True)
    open(ses, "w", encoding="utf-8").write("(session x (base_design x) (routes (resolution um 10) (network_out (net NET1 (wire (path F.Cu 2000 0 0 1000 0))))))\n")
    print(f"2026-09-20 05:00:02.100 INFO   Successfully saved output file '{ses}' (100 bytes).", flush=True)
    time.sleep(3.0)
    sys.exit(0)
unrouted_start = 3
print("2026-09-20 05:00:01.000 INFO   [A\\B] Loading board file 'x.dsn'...", flush=True)
print(f"2026-09-20 05:00:01.000 INFO   [A\\B] Auto-routing stage started on board 'x' for {unrouted_start} unrouted items.", flush=True)
left = unrouted_start
for i in range(1, passes + 1):
    left = max(0 if mode != "partial" else 1, left - 1)
    print(f"2026-09-20 05:00:01.000 INFO   [A\\B] Auto-routing pass #{i} on board 'x' was completed in 0.10 seconds with score {100 - i}.50 ({left} unrouted and 0 violations).", flush=True)
    if left == (1 if mode == "partial" else 0):
        break
if mode == "partial":
    print("2026-09-20 05:00:02.000 INFO   [A\\B] The router's score (99.00) has not improved by more than 0.5 points in the last 10 passes (1 items still unconnected). Stopping the auto-router.", flush=True)
    print("The following connections could not be routed -- please review your design (e.g. check pad clearances, trace width rules, and available routing space):", flush=True)
    print("  Net 'NET2' (1 unrouted connection):", flush=True)
    print("    - R1-2  ->  R3-1", flush=True)
print(f"2026-09-20 05:00:02.000 INFO   [A\\B] Auto-routing stage completed: started with {unrouted_start} unrouted nets, completed in 0.50 seconds, final score: 99.00 ({left} unrouted and 0 violations), using 0.1 total CPU seconds.", flush=True)
print("2026-09-20 05:00:02.000 INFO   [A\\B] Optimizer pass #1 on board 'y' was completed in 0.01 seconds with the score of 99.00 (%d unrouted and 0 violations)." % left, flush=True)
print("2026-09-20 05:00:02.000 INFO   [A\\B] Stopping optimizer because the improvement in this pass (0.0031%) is below the threshold (1.00%).", flush=True)
print("2026-09-20 05:00:02.000 INFO   [A\\B] Job 'A\\B' finished with state: COMPLETED (elapsed: 0.5 seconds).", flush=True)
rj = kv("--router.result_json=")
if rj:
    json.dump({"schema_version": 1, "settings_snapshot": {"max_threads": 1, "fanout": {"enabled": "--router.fanout.enabled=true" in args}},
               "board_statistics": {"connections": {"maximum_count": unrouted_start, "incomplete_count": left},
                                    "clearance_violations": {"total_count": 0}, "traces": {"total_count": 4}, "vias": {"total_count": 1}},
               "final_state": "COMPLETED", "exit_code": 0, "output_written": mode != "noses"}, open(rj, "w"))
if mode != "noses":
    open(ses, "w", encoding="utf-8").write("(session x (base_design x) (routes (resolution um 10) (network_out (net NET1 (wire (path F.Cu 2000 0 0 1000 0))))))\n")
    print(f"2026-09-20 05:00:02.100 INFO   Successfully saved output file '{ses}' (100 bytes).", flush=True)
sys.exit(0)
"""

PY_JVM_ARGS = (
    "-c",
    "import sys, runpy; sys.argv = sys.argv[2:]; runpy.run_path(sys.argv[0], run_name='__main__')",
)


@pytest.fixture
def fake_engines(tmp_path: Path) -> Engines:
    """`Engines` whose `java` is this Python and whose `jar` is the fake router script (the Router
    runs `python -c <shim> -jar <script> …`; the shim drops `-jar` and runs the script)."""
    script = tmp_path / "freerouting-2.4.1.jar"
    script.write_text(FAKE_FREEROUTING, encoding="utf-8")
    profile = tmp_path / "profile" / "freerouting.json"
    profile.parent.mkdir()
    profile.write_text(
        json.dumps({"profile": {"id": "11111111-2222-3333-4444-555555555555", "allow_telemetry": True}})
    )
    return Engines(
        storage_home=tmp_path,
        java=Path(sys.executable),
        jar=script,
        profile=profile,
        jre_release="py",
        freerouting_version="2.4.1",
    )


def router(engines: Engines, tmp_path: Path, **kw: Any) -> Router:
    return Router(engines, temp_dir=tmp_path / "runs", jvm_args=PY_JVM_ARGS, **kw)


def dsn_with_mode(tmp_path: Path, mode: str) -> Path:
    text = SPIKE_DSN.read_text(encoding="utf-8")
    p = tmp_path / f"{mode}.dsn"
    # a comment-free grammar: the marker rides in the host_version string
    p.write_text(
        text.replace('(host_version "10.0.0")', f'(host_version "10.0.0 MODE:{mode}")'), encoding="utf-8"
    )
    return p


# ---- engine discovery -------------------------------------------------------------------------------------------


def test_discover_engines_over_bootstrap_layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("KIWRAP_JAVA", raising=False)
    monkeypatch.delenv("KIWRAP_FREEROUTING_JAR", raising=False)
    empty = discover_engines(tmp_path)
    assert not empty.available and empty.java is None and empty.jar is None
    with pytest.raises(EngineMissing) as exc:
        empty.require()
    assert "bootstrap" in str(exc.value) and exc.value.which.startswith("the managed JRE")
    from kiwrap.platform import current

    exe = "java" + current().exe_suffix
    jre = tmp_path / "engines" / "jre"
    (jre / "jdk-25.0.4.1+1-jre" / "bin").mkdir(parents=True)
    (jre / "jdk-25.0.4.1+1-jre" / "bin" / exe).write_bytes(b"")
    (jre / "installed.json").write_text(json.dumps({"release_name": "jdk-25.0.4.1+1"}))
    fr = tmp_path / "engines" / "freerouting"
    fr.mkdir()
    (fr / "freerouting-2.3.0.jar").write_bytes(b"")
    (fr / "freerouting-2.4.1.jar").write_bytes(b"")
    (tmp_path / "freerouting").mkdir()
    (tmp_path / "freerouting" / "freerouting.json").write_text("{}")
    e = discover_engines(tmp_path)
    assert e.available and e.java == jre / "jdk-25.0.4.1+1-jre" / "bin" / exe
    assert e.jar == fr / "freerouting-2.4.1.jar" and e.freerouting_version == "2.4.1"
    assert e.jre_release == "jdk-25.0.4.1+1" and e.profile == tmp_path / "freerouting" / "freerouting.json"
    assert e.require() is e
    d = e.to_dict()
    assert d["available"] and d["freerouting_version"] == "2.4.1" and d["jre_release"] == "jdk-25.0.4.1+1"
    # a missing jar alone names the jar
    (fr / "freerouting-2.4.1.jar").unlink()
    (fr / "freerouting-2.3.0.jar").unlink()
    with pytest.raises(EngineMissing) as exc2:
        discover_engines(tmp_path).require()
    assert exc2.value.which == "the Freerouting jar"


def test_discover_engines_env_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    java = tmp_path / "myjava.exe"
    java.write_bytes(b"")
    jar = tmp_path / "freerouting-9.9.9.jar"
    jar.write_bytes(b"")
    monkeypatch.setenv("KIWRAP_JAVA", str(java))
    monkeypatch.setenv("KIWRAP_FREEROUTING_JAR", str(jar))
    e = discover_engines(tmp_path)
    assert e.java == java and e.jar == jar and e.freerouting_version == "9.9.9"


# ---- rules ------------------------------------------------------------------------------------------------------


class _Pad:
    def __init__(self, x: float, y: float, kind: str = "smd", net: str = "N") -> None:
        self.position = type("V", (), {"x": x, "y": y})()
        self.type = kind
        self.net = net


class _FP:
    def __init__(self, ref: str, fpid: str, pads: list[_Pad]) -> None:
        self.reference, self.fpid, self.pads, self.description, self.value = ref, fpid, pads, "", ""


class _Zone:
    def __init__(self, net: str, rule_area: bool = False) -> None:
        self.net, self.is_rule_area = net, rule_area


class _Board:
    def __init__(
        self, footprints: list[_FP], zones: list[_Zone], project_rules: dict[str, Any] | None, nets: list[str]
    ) -> None:
        self.footprints, self.zones = footprints, zones
        self.design_rules = type("DR", (), {"project_rules": project_rules})()
        self.nets = nets
        self.copper_layers = ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu")
        self.path = None


def test_rules_from_board_reads_project_rules_and_caps(tmp_path: Path):
    caps = {"rules.default_min_track_width_mm": 0.2}
    board = _Board(
        [],
        [],
        {
            "min_track_width": 0.15,
            "min_clearance": 0.127,
            "min_copper_edge_clearance": 0.3,
            "min_via_diameter": 0.45,
            "min_through_hole_diameter": 0.25,
        },
        ["A"],
    )
    r = rules_from_board(board, caps)
    assert (r.min_track_width_mm, r.min_clearance_mm, r.copper_to_edge_mm) == (0.15, 0.127, 0.3)
    assert r.copper_to_edge_from_board and r.copper_to_edge_um == 300
    assert (r.min_via_diameter_mm, r.min_via_drill_mm) == (0.45, 0.25)
    assert r.copper_layers == 4 and not r.fanout and not r.has_bga and r.plane_nets == []
    assert "--router.fanout.enabled=false" in r.cli_settings()
    assert "--router.copper_to_edge_clearance_um=300" in r.cli_settings()
    assert "--router.automatic_neckdown=false" in r.cli_settings()
    # no project rules: the capability default width, Freerouting's edge default + a note
    bare = rules_from_board(_Board([], [], None, []), caps)
    assert (
        bare.min_track_width_mm == 0.2
        and bare.copper_to_edge_mm == 0.5
        and not bare.copper_to_edge_from_board
    )
    assert any("copper-to-edge" in n for n in bare.notes)
    # a KiCad whose default minimum is 0 (9.x) has no minimum: reported as such, never invented
    assert (
        rules_from_board(_Board([], [], None, []), {"rules.default_min_track_width_mm": 0}).min_track_width_mm
        == 0.0
    )
    d = r.to_dict()
    assert d["fanout"] is False and d["copper_layers"] == 4


def test_rules_bga_detection_turns_fanout_on():
    grid = [_Pad(x * 0.8, y * 0.8) for x in range(7) for y in range(7)]  # 49 SMD pads in a 7x7 array
    ring = [_Pad(x * 0.5, 0) for x in range(20)] + [_Pad(x * 0.5, 5) for x in range(20)]  # a 40-pin dual row
    named = _FP("U1", "Package_BGA:BGA-64_10.0x10.0mm", [_Pad(0, 0)])
    assert bga_references(_Board([named], [], None, [])) == ["U1"]
    assert bga_references(_Board([_FP("U2", "Package_SO:SOIC-8", grid)], [], None, [])) == ["U2"]
    assert bga_references(_Board([_FP("U3", "Package_DFN:X", ring)], [], None, [])) == []
    r = rules_from_board(_Board([named], [], None, []), None)
    assert (
        r.has_bga and r.fanout and r.bga_refs == ["U1"] and "--router.fanout.enabled=true" in r.cli_settings()
    )
    assert any("BGA" in n for n in r.notes)


def test_rules_plane_nets_and_ignored_classes(tmp_path: Path):
    pro = tmp_path / "b.kicad_pro"
    pro.write_text(
        json.dumps(
            {
                "net_settings": {
                    "classes": [{"name": "Default"}, {"name": "Power"}, {"name": "Mixed"}],
                    "netclass_patterns": [
                        {"netclass": "Power", "pattern": "GND"},
                        {"netclass": "Power", "pattern": "VCC"},
                        {"netclass": "Mixed", "pattern": "/sig*"},
                        {"netclass": "Mixed", "pattern": "+5V"},
                    ],
                }
            }
        )
    )
    nets = ["GND", "VCC", "+5V", "/sig1", "/sig2", "N1"]
    # classes that leave every parameter to Default are the EFFECTIVE `<name>,Default` netclasses
    # KiCad 9+/10 exports (`kiwrap.formats.specctra.rules`): the `-inc` names must match the DSN
    assert netclass_nets(pro, nets) == {
        "Power,Default": ["GND", "VCC"],
        "Mixed,Default": ["+5V", "/sig1", "/sig2"],
        "Default": ["N1"],
    }
    board = _Board([], [_Zone("GND"), _Zone("VCC"), _Zone("+5V"), _Zone("N1", rule_area=True)], None, nets)
    assert plane_nets(board) == ["+5V", "GND", "VCC"]
    r = rules_from_board(board, None, pro_path=pro)
    assert r.plane_nets == ["+5V", "GND", "VCC"]
    # Power holds only plane nets -> ignored; Mixed also holds /sig* -> never ignored
    assert r.ignore_net_classes == ["Power,Default"]
    assert netclass_nets(None, nets) == {"Default": nets}


def test_rules_from_real_board_files():
    board = BoardFile.load(TINY_BOARD)
    r = rules_from_board(board, {"rules.default_min_track_width_mm": 0.2})
    assert r.copper_layers == 2 and r.plane_nets == [] and not r.fanout
    assert r.min_track_width_mm > 0 and r.copper_to_edge_um > 0


@pytest.mark.kicad_version("10.0")
def test_rules_from_ecc83_demo(install10):
    demo = install10.share / "demos" / "ecc83" / "ecc83-pp.kicad_pcb"
    if not demo.is_file():
        pytest.skip("ecc83 demo missing")
    r = rules_from_board(BoardFile.load(demo), {"rules.default_min_track_width_mm": 0.2})
    assert r.plane_nets == ["GND"] and r.copper_layers == 2 and not r.has_bga
    assert (
        r.copper_to_edge_from_board and r.copper_to_edge_um == 10
    )  # min_copper_edge_clearance 0.01 in the demo
    assert r.ignore_net_classes == []  # Default holds the signal nets too


# ---- DSN post-processing ----------------------------------------------------------------------------------------


def test_dsn_parser_handles_kicad_string_quote_and_round_trips():
    text = SPIKE_DSN.read_text(encoding="utf-8")
    tree = dsn_post.parse_dsn(text)
    assert dsn_post.head(tree) == "pcb"
    parser = dsn_post.find(tree, "parser")
    assert parser is not None and dsn_post.atoms(dsn_post.find(parser, "string_quote")) == ['"']
    assert dsn_post.atoms(dsn_post.find(parser, "host_cad")) == ['"KiCad\'s Pcbnew"']
    summary = dsn_post.dsn_summary(tree)
    assert (
        summary["layers"] == ["F.Cu", "B.Cu"]
        and summary["boundary"]
        and summary["rule"] == {"width": 200.0, "clearance": 200.0}
    )
    assert summary["nets"] == {"NET1": 2, "NET2": 2, "NET3": 2} and summary["classes"] == {
        "kicad_default": ["NET1", "NET2", "NET3"]
    }
    assert (summary["wires"], summary["vias"], summary["fixed"], summary["components"]) == (3, 2, 2, 3)
    dumped = dsn_post.dump_dsn(tree)
    again = dsn_post.parse_dsn(dumped)
    assert dsn_post.dsn_summary(again) == summary
    assert dsn_post.dump_dsn(again) == dumped  # the re-emitted form is a fixed point
    for bad in ["(pcb", "(pcb ())", ")", '(pcb (a "x)']:
        with pytest.raises(dsn_post.DsnError):
            dsn_post.parse_dsn(bad)


def test_restrict_nets_removes_the_rest_from_the_network_and_fixes_their_wiring():
    """Freerouting 2.4.1 headless ignores `-inc`, so the subset is enforced by the DSN itself:
    the other nets have no (net …) entry and no class membership, their wiring is `fix`."""
    tree = dsn_post.parse_dsn(SPIKE_DSN.read_text(encoding="utf-8"))
    before = dsn_post.dsn_summary(tree)
    rep = dsn_post.restrict_nets(tree, ["NET1", "NOPE"])
    assert (
        rep.applied
        and rep.routed_nets == ["NET1"]
        and rep.ignored_nets == ["NET2", "NET3"]
        and rep.unknown_nets == ["NOPE"]
    )
    summary = dsn_post.dsn_summary(tree)
    assert summary["classes"] == {"kicad_default": ["NET1"]} and list(summary["nets"]) == ["NET1"]
    assert "kiwrap_ignore" not in dsn_post.dump_dsn(tree)
    # the removed nets' copper stays as fixed obstacles; NET1's wiring is untouched
    wiring = dsn_post.find(tree, "wiring")
    kinds = {}
    for item in dsn_post.lists(wiring):
        net = dsn_post.unquote(dsn_post.atoms(dsn_post.find(item, "net"))[0])
        kinds.setdefault(net, set()).add(tuple(dsn_post.atoms(dsn_post.find(item, "type"))))
    assert kinds.get("NET2", set()) <= {("fix",)} and kinds.get("NET3", set()) <= {("fix",)}
    assert summary["wires"] + summary["vias"] == before["wires"] + before["vias"]
    assert rep.wires_fixed == 2  # the third NET2/NET3 item of the spike DSN was `fix` already
    # an older DSN carrying a kiwrap_ignore class: dissolved, its nets re-evaluated
    old = dsn_post.parse_dsn(SPIKE_DSN.read_text(encoding="utf-8"))
    dsn_post.find(old, "network").append(["class", "kiwrap_ignore", "NET3", ["rule", ["width", "200"]]])
    rep2 = dsn_post.restrict_nets(old, ["NET1", "NET2"])
    assert dsn_post.dsn_summary(old)["classes"] == {"kicad_default": ["NET1", "NET2"]}
    assert rep2.ignored_nets == ["NET3"]
    # every net wanted -> nothing changes, the text is returned unchanged
    post = dsn_post.postprocess_dsn(
        SPIKE_DSN.read_text(encoding="utf-8"), route_nets=["NET1", "NET2", "NET3"]
    )
    assert (
        not post.changed and post.ignore_classes == [] and post.text == SPIKE_DSN.read_text(encoding="utf-8")
    )
    post2 = dsn_post.postprocess_dsn(SPIKE_DSN.read_text(encoding="utf-8"))
    assert not post2.changed and post2.restrict is None
    post3 = dsn_post.postprocess_dsn(SPIKE_DSN.read_text(encoding="utf-8"), route_nets=["NET2"])
    assert post3.changed and post3.ignore_classes == [] and "kiwrap_ignore" not in post3.text
    assert list(post3.summary["nets"]) == ["NET2"] and post3.summary["classes"] == {"kicad_default": ["NET2"]}


def test_restrict_nets_quoted_names_and_missing_network():
    text = SPIKE_DSN.read_text(encoding="utf-8").replace("NET1", '"Net-(R1-Pad1)"')
    tree = dsn_post.parse_dsn(text)
    rep = dsn_post.restrict_nets(tree, ["Net-(R1-Pad1)"])
    assert rep.routed_nets == ["Net-(R1-Pad1)"] and rep.ignored_nets == ["NET2", "NET3"]
    with pytest.raises(dsn_post.DsnError):
        dsn_post.restrict_nets(dsn_post.parse_dsn("(pcb x (structure (layer F.Cu)))"), ["A"])
    assert (
        dsn_post.quote_if_needed("Net-(R1-Pad1)") == '"Net-(R1-Pad1)"'
        and dsn_post.quote_if_needed("GND") == "GND"
    )


# ---- log parser / profile ---------------------------------------------------------------------------------------


CAPTURED_LOG = """2026-09-20 04:54:45.470 INFO   Freerouting v2.4.1 (build-date: 2026-09-03)
2026-09-20 04:54:46.898 INFO   [36E956\\B47A97] Loading board file 'probe.dsn'...
2026-09-20 04:58:29.354 WARN   Unknown settings property: router.autorouter.max_threads
2026-09-20 04:54:46.970 INFO   [36E956\\B47A97] Auto-routing stage started on board '18c0' for 3 unrouted items.
2026-09-20 04:54:47.248 INFO   [36E956\\B47A97] Auto-routing pass #1 on board '18c0' was completed in 0.26 seconds with score 133.32 (2 unrouted and 3 violations).
2026-09-20 04:54:48.047 INFO   [36E956\\B47A97] Auto-routing pass #11 on board '0b0c' was completed in 0.11 seconds with score 133.32 (2 unrouted and 3 violations), using 0.98 CPU seconds and the job allocated 3.59 GB of memory so far.
2026-09-20 04:54:48.382 INFO   [36E956\\B47A97] The router's score (133.32) has not improved by more than 0.5 points in the last 10 passes (2 items still unconnected). Stopping the auto-router.
The following connections could not be routed -- please review your design (e.g. check pad clearances, trace width rules, and available routing space):
  Net 'NET1' (1 unrouted connection):
    - R1-1  ->  R2-1
  Net 'Net-(U1-Pad2)' (1 unrouted connection):
    - R1-2  ->  via
2026-09-20 04:54:48.389 INFO   [36E956\\B47A97] Auto-routing stage completed: started with 3 unrouted nets, completed in 1.43 seconds, final score: 133.32 (2 unrouted and 3 violations), using 0.98 total CPU seconds, 3.59 GB total allocated, and 333.5 MB peak heap usage.
2026-09-20 04:54:48.400 INFO   [36E956\\B47A97] Optimizer pass #1 on board 'e8ae' was completed in 0.01 seconds with the score of 133.32 (2 unrouted and 3 violations).
2026-09-20 04:54:48.401 INFO   [36E956\\B47A97] Stopping optimizer because the improvement in this pass (0.0031%) is below the threshold (1.00%).
2026-09-20 04:54:48.403 INFO   [36E956\\B47A97] Job '36E956\\B47A97' finished with state: COMPLETED (elapsed: 1.47 seconds, finished at UTC: 2026-09-19T23:54:48.402470300Z).
2026-09-20 04:54:48.657 INFO   Saving output file 'C:\\x\\probe.ses' (SES)...
2026-09-20 04:54:48.659 INFO   Successfully saved output file 'C:\\x\\probe.ses' (1732 bytes).
"""


def test_log_parser_on_captured_freerouting_output():
    p = _LogParser(20, time.perf_counter())
    updates: list[RouteProgress] = [
        u for u in (p.feed(ln) for ln in CAPTURED_LOG.splitlines()) if u is not None
    ]
    assert p.version == "2.4.1" and p.unrouted_at_start == 3 and p.passes == 11
    assert (p.unrouted, p.violations, p.score) == (2, 3, 133.32)
    assert p.state == "COMPLETED" and p.saved and p.stage == "done"
    assert "Stopping the auto-router" in p.stop_reason
    assert p.connections == [
        {"net": "NET1", "from": "R1-1", "to": "R2-1"},
        {"net": "Net-(U1-Pad2)", "from": "R1-2", "to": "via"},
    ]
    assert p.warnings == ["Unknown settings property: router.autorouter.max_threads"] and p.errors == []
    stages = [u.stage for u in updates]
    assert stages[0] == "autoroute" and "optimize" in stages and stages[-1] == "done"
    pass11 = next(u for u in updates if u.pass_no == 11 and u.stage == "autoroute")
    assert pass11.unrouted == 2 and pass11.max_passes == 20 and 0.5 < pass11.fraction < 0.9
    assert updates[-1].fraction == 1.0 and updates[-1].to_dict()["stage"] == "done"


def test_write_profile_enforces_the_telemetry_off_keys(tmp_path: Path):
    template = tmp_path / "t.json"
    template.write_text(
        json.dumps(
            {
                "profile": {"id": "abc", "allow_telemetry": True, "email": "x@y"},
                "gui": {"enabled": True},
                "extra": 1,
            }
        )
    )
    run = tmp_path / "run"
    run.mkdir()
    path = write_profile(run, template, version="2.4.1")
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["profile"] == {"id": "abc", "allow_telemetry": False, "email": "", "allow_contact": False}
    assert doc["gui"]["enabled"] is False and doc["extra"] == 1 and doc["version"] == "2.4.1"
    assert doc["usage_and_diagnostic_data"]["disable_analytics"] is True
    assert doc["network"] == {"proxy_url": "http://127.0.0.1:9", "no_proxy": "localhost,127.0.0.1"}
    assert doc["logging"]["file"]["location"] == str(run / "freerouting.log")
    for key, value in ENFORCED_PROFILE.items():
        cur: Any = doc
        for part in key.split("."):
            cur = cur[part]
        assert cur == value, key
    # no template: a fresh uuid
    run2 = tmp_path / "run2"
    run2.mkdir()
    doc2 = json.loads(write_profile(run2, None).read_text(encoding="utf-8"))
    assert len(doc2["profile"]["id"]) == 36 and doc2["profile"]["allow_telemetry"] is False


# ---- Router against the fake JVM ----------------------------------------------------------------------------------


def test_router_requires_engines(tmp_path: Path):
    with pytest.raises(EngineMissing):
        Router(Engines(storage_home=tmp_path, java=None, jar=None, profile=None))


def test_router_argv_and_result(fake_engines: Engines, tmp_path: Path):
    r = router(fake_engines, tmp_path, keep_run_dir=True)
    assert r.version() == "2.4.1"
    dsn = dsn_with_mode(tmp_path, "ok")
    ses = tmp_path / "out" / "x.ses"
    seen: list[RouteProgress] = []
    rules = RouteRules(copper_to_edge_mm=0.25, fanout=False, ignore_net_classes=["kicad_default"])
    res = r.route(dsn, ses, rules=rules, passes=7, timeout_s=60, progress_cb=seen.append)
    assert res.ok and res.exit_code == 0 and res.ses_written and ses.read_text().startswith("(session")
    assert res.passes == 3 and res.unrouted == 0 and res.unrouted_at_start == 3 and res.state == "COMPLETED"
    assert res.violations == 0 and res.result_json and res.result_json["final_state"] == "COMPLETED"
    argv = res.argv
    assert argv[0] == str(fake_engines.java) and "-jar" in argv and str(fake_engines.jar) in argv
    assert "--gui.enabled=false" in argv and "-l" in argv and argv[argv.index("-l") + 1] == "en"
    assert argv[argv.index("-mp") + 1] == "7" and argv[argv.index("-mt") + 1] == "1"
    assert "--router.optimizer.max_threads=1" in argv
    assert "--router.fanout.enabled=false" in argv and "--router.copper_to_edge_clearance_um=250" in argv
    assert "--router.automatic_neckdown=false" in argv
    assert (
        "--usage_and_diagnostic_data.disable_analytics=true" in argv
        and "--profile.allow_telemetry=false" in argv
    )
    assert any(a.startswith("--user_data_path=") for a in argv) and any(
        a.startswith("--router.result_json=") for a in argv
    )
    assert argv[argv.index("-inc") + 1] == "kicad_default"  # a plane-only netclass present in the DSN
    assert argv[argv.index("-de") + 1] != str(dsn)  # the DSN is copied into the run dir
    assert res.run_dir is not None and (res.run_dir / "freerouting.json").is_file()
    prof = json.loads((res.run_dir / "freerouting.json").read_text())
    assert (
        prof["profile"]["allow_telemetry"] is False
        and prof["profile"]["id"] == "11111111-2222-3333-4444-555555555555"
    )
    assert prof.get("touched_by_fake")  # the run dir profile is the one the router wrote into
    assert (
        json.loads(fake_engines.profile.read_text())["profile"]["allow_telemetry"] is True
    )  # the template is never rewritten
    assert [u.stage for u in seen][0] == "autoroute" and seen[-1].stage == "done"
    d = res.to_dict()
    assert d["ok"] and d["traces"] == 4 and d["vias"] == 1 and d["ignore_classes"] == ["kicad_default"]


def test_router_net_restriction_and_partial_result(fake_engines: Engines, tmp_path: Path):
    r = router(fake_engines, tmp_path, keep_run_dir=True)
    dsn = dsn_with_mode(tmp_path, "partial")
    res = r.route(dsn, tmp_path / "p.ses", rules=RouteRules(), nets=["NET1", "NET2"], passes=5, timeout_s=60)
    assert res.ok and res.unrouted == 1 and res.ignored_nets == ["NET3"] and res.ignore_classes == []
    assert "-inc" not in res.argv  # the subset lives in the DSN, not in a flag 2.4.1 ignores
    used = Path(res.argv[res.argv.index("-de") + 1]).read_text(encoding="utf-8")
    assert "(net NET3\n" not in used and "kiwrap_ignore" not in used  # no network entry; wiring refs stay
    assert res.unrouted_connections == [{"net": "NET2", "from": "R1-2", "to": "R3-1"}]
    assert "Stopping the auto-router" in res.stop_reason
    assert res.run_dir is not None and res.run_dir.is_dir()  # keep_run_dir; removed on success otherwise
    assert (
        router(fake_engines, tmp_path)
        .route(dsn, tmp_path / "p1.ses", rules=RouteRules(), passes=5, timeout_s=60)
        .run_dir
        is None
    )
    res2 = r.route(dsn, tmp_path / "p2.ses", rules=RouteRules(), nets=["NET1", "ZZZ"], passes=5, timeout_s=60)
    assert any("ZZZ" in w for w in res2.warnings)


def test_router_failure_modes(fake_engines: Engines, tmp_path: Path):
    r = router(fake_engines, tmp_path)
    with pytest.raises(RouterError) as exc:
        r.route(
            dsn_with_mode(tmp_path, "fail"), tmp_path / "f.ses", rules=RouteRules(), passes=3, timeout_s=60
        )
    assert exc.value.exit_code == 3 and "bad boundary" in str(exc.value) and "ERROR" in exc.value.log
    with pytest.raises(RouterError) as exc2:
        r.route(
            dsn_with_mode(tmp_path, "noses"), tmp_path / "n.ses", rules=RouteRules(), passes=3, timeout_s=60
        )
    assert exc2.value.exit_code == 0 and "no session file" in str(exc2.value)
    with pytest.raises(FileNotFoundError):
        r.route(tmp_path / "missing.dsn", tmp_path / "m.ses", rules=RouteRules(), passes=3, timeout_s=60)


def test_router_timeout_kills_the_process(fake_engines: Engines, tmp_path: Path):
    r = router(fake_engines, tmp_path)
    t0 = time.perf_counter()
    with pytest.raises(RouterTimeout) as exc:
        r.route(
            dsn_with_mode(tmp_path, "hang"), tmp_path / "h.ses", rules=RouteRules(), passes=999, timeout_s=2.0
        )
    assert time.perf_counter() - t0 < 15 and exc.value.elapsed_s >= 2.0
    assert (
        isinstance(exc.value, TimeoutError)
        and "pass" in str(exc.value)
        and "Auto-routing pass" in exc.value.log
    )
    assert not (tmp_path / "h.ses").exists()


def test_router_keeps_a_session_saved_as_the_budget_runs_out(fake_engines: Engines, tmp_path: Path):
    """Phase 1c finding 14: the budget check ran before the exit check, so a run that saved its
    SES within the last poll interval (or whose JVM shutdown outlasted the budget) was killed
    and reported as a timeout with a complete session sitting on disk."""
    r = router(fake_engines, tmp_path)
    res = r.route(
        dsn_with_mode(tmp_path, "slowexit"), tmp_path / "s.ses", rules=RouteRules(), passes=5, timeout_s=1.5
    )
    assert res.ok and res.ses_written and (tmp_path / "s.ses").read_text().startswith("(session")
    assert res.exit_code == 0 and res.state == "COMPLETED" and res.unrouted == 0
    assert any("budget ran out" in w for w in res.warnings), res.warnings


def test_router_prunes_old_failed_run_dirs(fake_engines: Engines, tmp_path: Path):
    """Phase 1c finding 17: failed runs kept their run dir (DSN copy, log) forever; only the
    newest KEEP_FAILED_RUN_DIRS survive."""
    from kiwrap.backends.router.freerouting import KEEP_FAILED_RUN_DIRS

    r = router(fake_engines, tmp_path)
    runs = tmp_path / "runs"
    for _ in range(KEEP_FAILED_RUN_DIRS + 3):
        with pytest.raises(RouterError):
            r.route(
                dsn_with_mode(tmp_path, "fail"),
                tmp_path / "f.ses",
                rules=RouteRules(),
                passes=3,
                timeout_s=60,
            )
        time.sleep(0.01)
    kept = sorted(d for d in runs.glob("r*-*") if d.is_dir())
    assert len(kept) == KEEP_FAILED_RUN_DIRS, kept
    assert all((d / "freerouting.json").is_file() for d in kept)  # the newest ones, intact


def test_router_cancel_event_kills_the_process(fake_engines: Engines, tmp_path: Path):
    r = router(fake_engines, tmp_path)
    cancel = threading.Event()
    seen: list[RouteProgress] = []

    def on_progress(p: RouteProgress) -> None:
        seen.append(p)
        if p.pass_no >= 3:
            cancel.set()

    t0 = time.perf_counter()
    with pytest.raises(RouterCancelled):
        r.route(
            dsn_with_mode(tmp_path, "hang"),
            tmp_path / "c.ses",
            rules=RouteRules(),
            passes=999,
            timeout_s=60,
            progress_cb=on_progress,
            cancel_event=cancel,
        )
    assert time.perf_counter() - t0 < 15 and any(p.pass_no >= 3 for p in seen)


def test_router_env_strips_java_tool_options(
    fake_engines: Engines, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("JAVA_TOOL_OPTIONS", "-Xmx1g")
    monkeypatch.setenv("_JAVA_OPTIONS", "-Dx=y")
    r = router(fake_engines, tmp_path)
    env = r._env()
    assert (
        "JAVA_TOOL_OPTIONS" not in env
        and "_JAVA_OPTIONS" not in env
        and env.get("PATH") == os.environ.get("PATH")
    )
