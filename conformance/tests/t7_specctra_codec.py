"""T7 — the Specctra codec certified by KiCad and Freerouting (docs/ARCHITECTURE.md §8).

KiCad 10.0.x is the oracle: its bundled Python still carries SWIG `ExportSpecctraDSN` /
`ImportSpecctraSES` (docs/VERIFICATION.md claim 7), the very functions the codec replaces on
KiCad 11 where SWIG is gone (D16). Every run goes through the sandboxed config root, no GUI is
launched, and Freerouting runs from the managed JRE 25 that `scripts/bootstrap.ps1` installed
(claim 8: `--router.fanout.enabled=false`, `copper_to_edge_clearance_um` from the board rule).

1. DSN equality — for ecc83 (2 layers, user layer names, plane, THT), the Phase 1b blinker
   (SMD + THT, plane, netclass patterns), kit-dev-coldfire-xilinx_5213 (4 layers, 278 nets,
   2935 tracks, 253 vias), One-Air-Max (4 layers, custom pads, rule areas in footprints and on
   the board), multichannel_mixer-unrouted (a KiCad 8.99-written layer table whose `User.1-9`
   rows are typed `signal`: KiCad's parser takes the leading block only, so the board has 2
   copper layers, not 11 - Phase 1c finding 1), the jetson-agx-thor-baseboard (10 layers, 1387
   nets, a back-side battery holder with a B.Cu via keepout that the flip to the top writes on
   F.Cu - finding 2 - and six diff-pair classes that export as `<name>,Default` - finding 11)
   and the blinker with chamfered pads (`roundrect` + `(chamfer …)` = KiCad's CHAMFERED_RECT,
   the `doChamfer` polygon and id - finding 13): `DsnWriter` vs SWIG export, `compare_dsn`
   reports zero differences at 5 µm (same layers, boundary, cutouts, planes, keepouts, rules,
   classes, placements, pin geometry, nets -> pins and wiring). Image outlines (cosmetic) only
   count.
2. Round trip — ecc83 with half of its nets stripped, the blinker with all copper stripped, and
   the blinker turned into a 4-layer board: codec DSN -> Freerouting 2.4.1 -> SES; the SES applied
   by `SesReader` and by SWIG `ImportSpecctraSES` produce the same tracks and vias (positions
   within 1e-3 mm, layer, width, net, via size/drill - the drill KiCad materialises on save: a
   via whose padstack drill equals the Default class drill is UNDEFINED to the importer and is
   saved with its NET's netclass drill, finding 4); `kicad-cli pcb drc --refill-zones
   --save-board` on the codec board reports 0 errors and 0 unconnected on 10.0 and, when
   installed, on the newest KiCad (the 10.99 nightly / 11). A fourth case gives GND a Power
   class with `via_drill 0.5` and a drill-less GND via so the two importers must agree on 0.5.
3. Library-frame footprint keepouts (finding 3) — a blinker with a track+via rule area inside a
   rotated footprint, saved through `kicad-cli pcb upgrade` by the newest KiCad (stamp
   >= 20260616, the zone now in the LIBRARY frame): the codec's DSN of the upgraded board equals
   10.0.6's SWIG export of the original (board-frame) file, keepout included.

The numbers land in the test properties (`-rP`).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import textwrap
import time
from pathlib import Path

import pytest

from kiwrap.backends.cli.drc import run_drc
from kiwrap.formats.board import BoardFile
from kiwrap.formats.board_writer import BoardWriter
from kiwrap.formats.epochs import read_stamp
from kiwrap.formats.sexpr import build, dumps, load
from kiwrap.formats.specctra import DsnWriter, SesReader, compare_dsn, parse_dsn, rules_from_project

pytestmark = [pytest.mark.kicad, pytest.mark.slow]

REPO = Path(__file__).resolve().parents[2]
BLINKER_DIR = REPO / "tests" / "fixtures" / "specctra" / "blinker_10"
DEMOS = {
    "ecc83": ("ecc83", "ecc83-pp"),
    "coldfire": ("kit-dev-coldfire-xilinx_5213", "kit-dev-coldfire-xilinx_5213"),
    "oneairmax": ("openair-max", "One-Air-Max"),
    "multichannel_unrouted": ("multichannel", "multichannel_mixer-unrouted"),
    "jetson": ("jetson-agx-thor-baseboard", "jetson-agx-thor-baseboard"),
}
#: a track + via keepout inside R1 (at 37.3 14.6 -90) in BOARD coordinates: a 1 x 3 mm box right
#: of the part (the <= 10 dialect stores footprint zones in the board frame)
R1_KEEPOUT_BOARD_FRAME = """
		(zone
			(net 0)
			(net_name "")
			(layer "F.Cu")
			(uuid "11111111-2222-3333-4444-555555555555")
			(hatch edge 0.5)
			(connect_pads
				(clearance 0)
			)
			(min_thickness 0.25)
			(filled_areas_thickness no)
			(keepout
				(tracks not_allowed)
				(vias not_allowed)
				(pads allowed)
				(copperpour allowed)
				(footprints allowed)
			)
			(fill
				(thermal_gap 0.5)
				(thermal_bridge_width 0.5)
			)
			(polygon
				(pts
					(xy 38.3 13.1) (xy 39.3 13.1) (xy 39.3 16.1) (xy 38.3 16.1)
				)
			)
		)
"""

SWIG_EXPORT = textwrap.dedent(
    """
    import sys, pcbnew
    b = pcbnew.LoadBoard(sys.argv[1])
    ok = pcbnew.ExportSpecctraDSN(b, sys.argv[2])
    print("EXPORT", ok)
    """
)
SWIG_IMPORT = textwrap.dedent(
    """
    import sys, pcbnew
    b = pcbnew.LoadBoard(sys.argv[1])
    ok = pcbnew.ImportSpecctraSES(b, sys.argv[2])
    pcbnew.SaveBoard(sys.argv[3], b)
    print("IMPORT", ok)
    """
)


# ------------------------------------------------------------------------------------------------
# helpers
# ------------------------------------------------------------------------------------------------


def swig_run(install, config, script: str, args: list[str], tmp: Path, timeout_s: float = 600) -> str:
    """Run `script` under the install's bundled python (SWIG pcbnew) inside the sandbox root."""
    if install.python is None or not Path(install.python).is_file():
        pytest.skip(f"{install.version}: no bundled python (no SWIG oracle)")
    path = tmp / f"swig_{abs(hash(script))}.py"
    path.write_text(script, encoding="utf-8")
    env = config.env()
    if install.python_dll_dir:
        env["PATH"] = str(install.python_dll_dir) + os.pathsep + env.get("PATH", "")
    with config.lock():
        try:
            proc = subprocess.run(
                [str(install.python), str(path), *args],
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                cwd=str(tmp),
            )
        finally:
            config.restore_seed()
    if proc.returncode != 0:
        raise AssertionError(f"SWIG script failed ({proc.returncode}): {proc.stderr[-2000:]}")
    return proc.stdout


def engines_home() -> Path:
    """The real engines home (bootstrap installs them into %LOCALAPPDATA%/kiwrap, not the
    per-session KIWRAP_HOME the tests run with)."""
    override = os.environ.get("KIWRAP_ENGINES_HOME")
    if override:
        return Path(override)
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "kiwrap"
    return Path.home() / ".local" / "share" / "kiwrap"


def find_engines() -> tuple[Path, Path, Path | None]:
    home = engines_home()
    jars = sorted((home / "engines" / "freerouting").glob("freerouting-*.jar"))
    javas = sorted((home / "engines" / "jre").glob("*/bin/java.exe")) + sorted(
        (home / "engines" / "jre").glob("*/bin/java")
    )
    if not jars or not javas:
        pytest.skip(f"managed Freerouting/JRE missing under {home} (run scripts/bootstrap.ps1)")
    profile = home / "freerouting" / "freerouting.json"
    return javas[-1], jars[-1], profile if profile.is_file() else None


def freeroute(
    dsn: Path, ses: Path, *, edge_clearance_um: int, passes: int = 40, timeout_s: float = 900
) -> dict:
    """Route `dsn` headlessly with the managed engine; telemetry off via a private profile copy."""
    java, jar, profile = find_engines()
    work = ses.parent / "fr_work"
    work.mkdir(parents=True, exist_ok=True)
    userdata = work / "userdata"
    userdata.mkdir(exist_ok=True)
    if profile is not None:
        shutil.copy(profile, userdata / "freerouting.json")
    else:
        (userdata / "freerouting.json").write_text(
            json.dumps(
                {
                    "profile": {"allow_telemetry": False, "allow_contact": False},
                    "usage_and_diagnostic_data": {"disable_analytics": True},
                    "gui": {"enabled": False},
                }
            ),
            encoding="utf-8",
        )
    result_json = work / "result.json"
    argv = [
        str(java),
        "-jar",
        str(jar),
        f"--user_data_path={userdata}",
        "--gui.enabled=false",
        "-de",
        str(dsn),
        "-do",
        str(ses),
        "-mp",
        str(passes),
        "-mt",
        "1",
        "--router.fanout.enabled=false",
        f"--router.copper_to_edge_clearance_um={edge_clearance_um}",
        f"--router.result_json={result_json}",
        f"--logging.file.location={work}",
    ]
    started = time.time()
    proc = subprocess.Popen(argv, cwd=str(work), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        out, _ = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        raise
    summary = [ln for ln in out.splitlines() if "Auto-routing stage completed" in ln or "unrouted" in ln]
    assert proc.returncode == 0 and ses.is_file(), f"Freerouting failed ({proc.returncode}): {out[-3000:]}"
    return {"seconds": round(time.time() - started, 1), "summary": summary[-1] if summary else "", "log": out}


def demo_copy(install, key: str, dst: Path) -> Path:
    project, stem = DEMOS[key]
    src = install.dir("demos") / project
    pcb = src / f"{stem}.kicad_pcb"
    if not pcb.is_file():
        pytest.skip(f"{install.version} ships no {project} demo")
    dst.mkdir(parents=True, exist_ok=True)
    for suffix in (".kicad_pcb", ".kicad_pro", ".kicad_dru"):
        if (src / f"{stem}{suffix}").is_file():
            shutil.copy(src / f"{stem}{suffix}", dst / f"{stem}{suffix}")
    return dst / f"{stem}.kicad_pcb"


def blinker_copy(dst: Path, *, layers: int = 2) -> Path:
    dst.mkdir(parents=True, exist_ok=True)
    for f in BLINKER_DIR.iterdir():
        shutil.copy(f, dst / f.name)
    pcb = dst / "blinky.kicad_pcb"
    if layers == 4:
        text = pcb.read_text(encoding="utf-8")
        text = text.replace(
            '(0 "F.Cu" signal)', '(0 "F.Cu" signal)\n\t\t(4 "In1.Cu" signal)\n\t\t(6 "In2.Cu" signal)', 1
        )
        pcb.write_text(text, encoding="utf-8")
    return pcb


def chamfer_blinker_pads(pcb: Path) -> None:
    """Three chamfered pads the way KiCad writes them (shape `roundrect` + `(chamfer …)`): two
    corners with rratio 0, four corners with rratio 0.1, one corner with rratio 0.25."""
    import re

    text = pcb.read_text(encoding="utf-8")

    def chamfer(ref: str, pad_no: str, ratio: str, corners: str, rratio: str) -> None:
        nonlocal text
        i = text.find(f'(property "Reference" "{ref}"')
        j = text.find(f'(pad "{pad_no}" smd', i)
        k = text.find("\n", j)
        end = text.find("\n\t\t)", k)
        head = re.sub(r"smd (\w+)", "smd roundrect", text[j:k])
        body = re.sub(r"\(roundrect_rratio [^)]*\)", f"(roundrect_rratio {rratio})", text[k:end])
        if "roundrect_rratio" not in body:
            body = body.replace("(layers", f"(roundrect_rratio {rratio})\n\t\t\t(layers", 1)
        body = body.replace(
            f"(roundrect_rratio {rratio})",
            f"(roundrect_rratio {rratio})\n\t\t\t(chamfer_ratio {ratio})\n\t\t\t(chamfer {corners})",
            1,
        )
        text = text[:j] + head + body + text[end:]

    chamfer("R1", "1", "0.2", "top_left bottom_right", "0")
    chamfer("R1", "2", "0.25", "top_left top_right bottom_left bottom_right", "0.1")
    chamfer("C1", "1", "0.3", "top_right", "0.25")
    pcb.write_text(text, encoding="utf-8")


def add_footprint_keepout(pcb: Path, ref: str, zone_text: str) -> None:
    """Insert a `(zone …)` as the last child of footprint `ref` (the <= 10 board-frame form)."""
    text = pcb.read_text(encoding="utf-8")
    i = text.find(f'(property "Reference" "{ref}"')
    start = text.rfind("\n\t(footprint", 0, i)
    end = text.find("\n\t(footprint", i)
    if end < 0:
        end = text.find("\n\t(gr_", i)
    block = text[start:end]
    close = block.rfind("\n\t)")
    block = block[:close] + zone_text + block[close:]
    pcb.write_text(text[:start] + block + text[end:], encoding="utf-8")


def power_class_with_own_via_drill(pcb: Path, *, via_drill: float = 0.5, via_diameter: float = 0.9) -> None:
    """GND and +3V3 into a Power class with its own via drill / diameter (schema 5 patterns), and
    one GND via WITHOUT a `(drill …)` token (the editor's netclass-drill via) on the GND plane."""
    pro = pcb.with_suffix(".kicad_pro")
    d = json.loads(pro.read_text(encoding="utf-8"))
    ns = d["net_settings"]
    ns["meta"] = {"version": 5}
    ns["netclass_patterns"] = [
        {"netclass": "Power", "pattern": "GND"},
        {"netclass": "Power", "pattern": "+3V3"},
    ]
    for c in ns["classes"]:
        if c["name"] == "Power":
            c["via_drill"] = via_drill
            c["via_diameter"] = via_diameter
    pro.write_text(json.dumps(d, indent=2), encoding="utf-8")
    root = load(pcb)
    via = build(
        "via",
        build("at", 20.0, 22.0),
        build("size", via_diameter),
        build("layers", "F.Cu", "B.Cu"),
        build("net", "GND"),
        build("uuid", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"),
    )
    last = max(i for i, n in enumerate(root.nodes) if n.head in ("footprint", "gr_line", "gr_rect", "zone"))
    root.insert(root.index(root.nodes[last]) + 1, via)
    pcb.write_text(dumps(root), encoding="utf-8", newline="")


def strip_copper(pcb: Path, *, nets: str = "all") -> tuple[int, int]:
    """Delete tracks/arcs/vias: every one (`all`) or those of every other net (`half`, sorted
    net names, deterministic). Returns (removed, kept)."""
    board = BoardFile.load(pcb, project_rules=False)
    names = sorted({t.net.name for t in (*board.tracks, *board.vias) if t.net and t.net.name})
    victims = set(names) if nets == "all" else set(names[::2])
    root = load(pcb)
    removed = kept = 0
    for node in list(root.nodes):
        if node.head not in ("segment", "arc", "via"):
            continue
        ref = node.find("net")
        name = ""
        if ref is not None and ref.atoms:
            first = ref.atoms[0]
            if first.kind == "number":
                code = first.as_int()
                name = next((n.name for n in board.nets if n.code == code), "")
            else:
                name = first.value
        if name in victims:
            root.remove(node)
            removed += 1
        else:
            kept += 1
    pcb.write_text(dumps(root), encoding="utf-8", newline="")
    return removed, kept


def apply_with_codec(pcb: Path, ses: Path, out: Path):
    stamp = read_stamp(load(pcb))
    w = BoardWriter(pcb.parent, epoch={"pcb": stamp})
    w.load(pcb)
    rules = rules_from_project(BoardFile.load(pcb))
    report = SesReader(ses.read_text(encoding="utf-8")).apply(w, default_via_drill=rules.default.via_drill)
    w.board_path = out
    w.save()
    pro = pcb.with_suffix(".kicad_pro")
    if pro.is_file():
        shutil.copy(pro, out.with_suffix(".kicad_pro"))
    dru = pcb.with_suffix(".kicad_dru")
    if dru.is_file():
        shutil.copy(dru, out.with_suffix(".kicad_dru"))
    return report


def copper_keys(pcb: Path, default_drill: float) -> tuple[set, set]:
    """Tracks and vias as KiCad saves them; the via drill is the one on disk (KiCad always writes
    `(drill …)`: an undefined drill is materialised as the via net's netclass drill, so the two
    importers must agree on the number itself, not on `drill or default`)."""
    b = BoardFile.load(pcb, project_rules=False)
    segs = set()
    for t in b.tracks:
        ends = tuple(
            sorted([(round(t.start.x, 3), round(t.start.y, 3)), (round(t.end.x, 3), round(t.end.y, 3))])
        )
        segs.add((t.layer, round(t.width, 3), t.net.name if t.net else "", ends))
    vias = {
        (
            round(v.position.x, 3),
            round(v.position.y, 3),
            round(v.size, 3),
            round(v.drill if v.drill else default_drill, 3),
            v.layers,
            v.net.name if v.net else "",
            v.kind,
        )
        for v in b.vias
    }
    return segs, vias


# ------------------------------------------------------------------------------------------------
# fixtures
# ------------------------------------------------------------------------------------------------


@pytest.fixture
def oracle(install10, config_root):
    """KiCad 10.0 + its sandbox root: the SWIG oracle and the primary DRC judge."""
    return install10, config_root(install10)


# ------------------------------------------------------------------------------------------------
# 1. DSN equality
# ------------------------------------------------------------------------------------------------


@pytest.mark.timeout(3600)
@pytest.mark.parametrize(
    "key",
    [
        "ecc83",
        "blinker",
        "coldfire",
        "oneairmax",
        "multichannel_unrouted",
        "jetson",
        "blinker_chamfer",
        "blinker_viaclass",
    ],
)
def test_t7_dsn_equals_kicads_export(oracle, key, tmp_path, record_property):
    inst, cfg = oracle
    if key == "jetson" and not os.environ.get("KIWRAP_T7_HEAVY"):
        # 10 layers / 1387 nets / 22k segments: 10.0.6's SWIG export alone takes 3 to 20+ min on
        # this machine (189 s idle, 1252 s under load), so it runs only on request; the
        # back-side keepout and the `<name>,Default` classes it certifies are also unit-tested
        # (tests/test_specctra_dsn.py) and were swept with scratchpad phase1c/fixer/all_demos_dsn.py
        pytest.skip("set KIWRAP_T7_HEAVY=1 to certify the jetson-agx-thor-baseboard demo (slow SWIG export)")
    if key.startswith("blinker"):
        pcb = blinker_copy(tmp_path / key)
        if key == "blinker_chamfer":
            chamfer_blinker_pads(pcb)
        elif key == "blinker_viaclass":
            power_class_with_own_via_drill(pcb)
    else:
        pcb = demo_copy(inst, key, tmp_path / key)
    swig_dsn = tmp_path / key / "swig.dsn"
    out = swig_run(inst, cfg, SWIG_EXPORT, [str(pcb), str(swig_dsn)], tmp_path, timeout_s=3000)
    assert "EXPORT True" in out, out
    board = BoardFile.load(pcb)
    writer = DsnWriter(board, host_version=inst.version)
    text = writer.write()
    (tmp_path / key / "codec.dsn").write_text(text, encoding="utf-8", newline="\n")
    diff = compare_dsn(parse_dsn(text), parse_dsn(swig_dsn.read_text(encoding="utf-8")), tol_um=5.0)
    record_property(
        f"t7_dsn_{key}",
        json.dumps({"kicad": inst.version, "summary": diff.summary(), "writer_warnings": writer.warnings}),
    )
    assert diff.ok, "\n".join(diff.differences[:20])
    cosmetic = [w for w in diff.warnings if "(cosmetic)" not in w]
    assert not cosmetic, cosmetic[:10]
    assert diff.checked["placements"] > 0 and diff.checked["nets"] > 0 and diff.checked["pins"] > 0
    assert not writer.warnings, writer.warnings
    if key == "multichannel_unrouted":
        assert board.copper_layers == ("F.Cu", "B.Cu") and diff.checked["layers"] == 2
    if key == "jetson":
        swig_doc = parse_dsn(swig_dsn.read_text(encoding="utf-8"))
        assert any("," in c.name for c in swig_doc.classes)  # `<name>,Default` effective classes
        bt1 = swig_doc.image_of("BT1")
        assert bt1 is not None and bt1.keepouts and bt1.keepouts[0].shape.layer == "F.Cu"
    if key == "blinker_viaclass":
        assert "Via[0-1]_900:500_um" in parse_dsn(text).via_ids


# ------------------------------------------------------------------------------------------------
# 2. Freerouting round trip: codec apply == SWIG import, DRC clean after refill
# ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("key", ["ecc83_half", "blinker", "blinker_4layer", "blinker_viaclass"])
def test_t7_freerouting_roundtrip_matches_swig_and_passes_drc(
    oracle, installs, config_root, key, tmp_path, record_property
):
    inst, cfg = oracle
    work = tmp_path / key
    if key == "ecc83_half":
        pcb = demo_copy(inst, "ecc83", work)
        removed, kept = strip_copper(pcb, nets="half")
    elif key == "blinker_viaclass":
        pcb = blinker_copy(work)
        removed, kept = strip_copper(pcb, nets="all")
        power_class_with_own_via_drill(pcb)  # + one drill-less GND via the router keeps
    else:
        pcb = blinker_copy(work, layers=4 if key.endswith("4layer") else 2)
        removed, kept = strip_copper(pcb, nets="all")
    board = BoardFile.load(pcb)
    rules = rules_from_project(board)
    writer = DsnWriter(board, host_version=inst.version)
    dsn = work / "codec.dsn"
    dsn.write_text(writer.write(), encoding="utf-8", newline="\n")
    ses = work / "codec.ses"
    run = freeroute(dsn, ses, edge_clearance_um=int(round(rules.copper_edge_clearance * 1000)))

    codec_pcb = work / "codec_routed.kicad_pcb"
    report = apply_with_codec(pcb, ses, codec_pcb)
    swig_pcb = work / "swig_routed.kicad_pcb"
    out = swig_run(inst, cfg, SWIG_IMPORT, [str(pcb), str(ses), str(swig_pcb)], tmp_path)
    assert "IMPORT True" in out, out
    ours = copper_keys(codec_pcb, rules.default.via_drill)
    theirs = copper_keys(swig_pcb, rules.default.via_drill)
    seg_diff = ours[0] ^ theirs[0]
    via_diff = ours[1] ^ theirs[1]
    assert not seg_diff, f"{len(seg_diff)} segments differ, e.g. {sorted(seg_diff)[:4]}"
    assert not via_diff, f"{len(via_diff)} vias differ, e.g. {sorted(via_diff)[:4]}"
    if key == "blinker_viaclass":
        gnd = [
            v for v in BoardFile.load(codec_pcb, project_rules=False).vias if v.net and v.net.name == "GND"
        ]
        assert gnd and all(abs(v.drill - 0.5) < 1e-9 for v in gnd), [(v.size, v.drill) for v in gnd]
    assert report.tracks_added == len(theirs[0]) - sum(
        1 for t in BoardFile.load(swig_pcb, project_rules=False).tracks if t.locked
    )
    assert report.placement_ignored == len(board.footprints)

    drc10 = run_drc(inst, cfg, codec_pcb, refill_zones=True, save_board=True, severity="all")
    numbers = {
        "kicad": inst.version,
        "stripped": removed,
        "kept": kept,
        "routing_seconds": run["seconds"],
        "freerouting": run["summary"],
        "codec_apply": report.as_dict(),
        "segments": len(ours[0]),
        "vias": len(ours[1]),
        "drc10": {"errors": drc10.errors, "warnings": drc10.warnings, "unconnected": len(drc10.unconnected)},
    }
    newest = installs[0]
    if newest.major_minor != inst.major_minor:
        cfg_new = config_root(newest)
        copy = work / "codec_routed_newest.kicad_pcb"
        shutil.copy(codec_pcb, copy)
        for suffix in (".kicad_pro", ".kicad_dru"):
            if codec_pcb.with_suffix(suffix).is_file():
                shutil.copy(codec_pcb.with_suffix(suffix), copy.with_suffix(suffix))
        drc_new = run_drc(newest, cfg_new, copy, refill_zones=True, save_board=True, severity="all")
        numbers["drc_newest"] = {
            "kicad": newest.version,
            "errors": drc_new.errors,
            "warnings": drc_new.warnings,
            "unconnected": len(drc_new.unconnected),
        }
        assert drc_new.errors == 0 and not drc_new.unconnected, [
            v.type for v in drc_new.violations if v.severity == "error"
        ]
    record_property(f"t7_roundtrip_{key}", json.dumps(numbers))
    assert drc10.errors == 0, [(v.type, v.description) for v in drc10.violations if v.severity == "error"][
        :10
    ]
    assert not drc10.unconnected, [v.description for v in drc10.unconnected][:10]


# ------------------------------------------------------------------------------------------------
# 3. footprint rule areas in the library frame (the nightly's dialect) vs the board frame (<= 10)
# ------------------------------------------------------------------------------------------------


def test_t7_library_frame_footprint_keepout_equals_swig_export_of_the_board_frame_file(
    oracle, installs, config_root, tmp_path, record_property
):
    """Phase 1c finding 3: a board saved by the nightly (stamp >= 20260616) stores a footprint's
    rule areas in the library frame; the codec applied the board -> image transform to library
    coordinates (keepout ~42 mm off, invisible to the router). KiCad 10.0.6's SWIG export of the
    ORIGINAL board-frame file is the truth for both dialects."""
    from kiwrap.backends.cli.runner import run_kicad_cli

    inst, cfg = oracle
    newest = installs[0]
    if newest.major_minor == inst.major_minor:
        pytest.skip("no KiCad newer than 10.0 installed (the library-frame dialect needs the nightly / 11)")
    work = tmp_path / "fpzone"
    pcb = blinker_copy(work)
    add_footprint_keepout(pcb, "R1", R1_KEEPOUT_BOARD_FRAME)
    swig_dsn = work / "truth.dsn"
    out = swig_run(inst, cfg, SWIG_EXPORT, [str(pcb), str(swig_dsn)], tmp_path)
    assert "EXPORT True" in out, out
    truth = parse_dsn(swig_dsn.read_text(encoding="utf-8"))
    r1 = truth.image_of("R1")
    assert r1 is not None and len(r1.keepouts) == 1 and r1.keepouts[0].shape is not None
    # the <= 10 file: board frame
    legacy = DsnWriter(BoardFile.load(pcb), host_version=inst.version).write()
    diff_legacy = compare_dsn(parse_dsn(legacy), truth, tol_um=5.0)
    assert diff_legacy.ok, diff_legacy.differences[:10]
    # the same board re-saved by the newest KiCad: library frame from stamp 20260616 on
    upgraded = work / "nightly.kicad_pcb"
    shutil.copy(pcb, upgraded)
    shutil.copy(pcb.with_suffix(".kicad_pro"), upgraded.with_suffix(".kicad_pro"))
    cfg_new = config_root(newest)
    res = run_kicad_cli(newest, ["pcb", "upgrade", "--force", str(upgraded)], config=cfg_new, timeout_s=300)
    assert res.returncode == 0, res.stderr[-500:]
    board_new = BoardFile.load(upgraded)
    assert board_new.stamp is not None and board_new.stamp >= 20260616, board_new.stamp
    writer = DsnWriter(board_new, host_version=newest.version)
    text = writer.write()
    diff = compare_dsn(parse_dsn(text), truth, tol_um=5.0)
    record_property(
        "t7_fp_zone_frame",
        json.dumps(
            {
                "kicad": inst.version,
                "newest": newest.version,
                "stamp": board_new.stamp,
                "summary": diff.summary(),
            }
        ),
    )
    assert diff.ok, "\n".join(diff.differences[:10])
    assert not writer.warnings, writer.warnings
    ko = parse_dsn(text).image_of("R1").keepouts[0]
    assert ko.shape is not None and {(round(x), round(y)) for x, y in ko.shape.points} == {
        (-1500, 1000),
        (1500, 1000),
        (1500, 2000),
        (-1500, 2000),
    }
