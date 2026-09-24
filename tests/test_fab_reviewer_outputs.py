"""Reviewer outputs without KiCad (requests/kiwrap-feature-requests.review.md items 4, 7 / B4, 38).

- item 4: `kw_fab_export` `layers` filter, `dry_run` file plan (nothing written, gate reported),
  `positions_preview` / `positions.excluded` with the reason, the BOM diff, `out_dir` rules, D22's
  `--check-zones` / `zones_stale`, read-only (`persist=false`) exports;
- item 7 / B4: renders go to per-call directories (or `out_path`) and the next render never
  deletes them; the sweep keeps / drops by count and age;
- item 38: `bbox_mm`, `px_per_mm`, `mirror_bottom`, any layer list, auto-2D, the crop cap and the
  inline budget; the region rasteriser against a hand-made PDF.

Live counterparts (10.0.6 / 10.99 demo copies): tests/test_fab_reviewer_live.py.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import time
from pathlib import Path
from typing import Any

import pytest

from kiwrap.backends.cli import render as backend_render
from kiwrap.core.fab import outputs
from kiwrap.core.fab.assembly import PosOptions, SchPart, bom_diff, plan_positions, schematic_parts
from kiwrap.core.fab.plan import drill_files, gerber_files, normalise_layers
from kiwrap.core.services import Services
from kiwrap.formats.board import BoardFile
from kiwrap.formats.schematic import Hierarchy
from tests.test_core_fakes import CAPS_10, FakeBackend, png_bytes
from tests.test_core_tools import Harness

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH (snapshots)")

#: a hand-written 10.0-format board: every position-file rule and a zone (item 4, D22)
MINI_BOARD = """(kicad_pcb (version 20260206) (generator "pcbnew") (generator_version "10.0")
  (general (thickness 1.6))
  (paper "A4")
  (layers
    (0 "F.Cu" signal "top_copper")
    (4 "In1.Cu" signal)
    (6 "In2.Cu" signal)
    (2 "B.Cu" signal)
    (13 "F.Paste" user)
    (1 "F.Mask" user)
    (5 "F.SilkS" user "F.Silkscreen")
    (25 "Edge.Cuts" user)
  )
  (net 0 "")
  (footprint "R:R_0603" (layer "F.Cu") (uuid "a1") (at 10 10)
    (property "Reference" "R1" (at 0 0 0) (layer "F.SilkS") (uuid "p1"))
    (attr smd)
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (uuid "d1")))
  (footprint "C:C_THT" (layer "F.Cu") (uuid "a2") (at 20 10)
    (property "Reference" "C1" (at 0 0 0) (layer "F.SilkS") (uuid "p3"))
    (attr through_hole)
    (pad "1" thru_hole circle (at 0 0) (size 1.6 1.6) (drill 0.8) (layers "*.Cu" "*.Mask") (uuid "d2")))
  (footprint "U:ANT" (layer "B.Cu") (uuid "a3") (at 30 10)
    (property "Reference" "AE1" (at 0 0 0) (layer "B.SilkS") (uuid "p4"))
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "B.Cu") (uuid "d3")))
  (footprint "H:MountingHole" (layer "F.Cu") (uuid "a4") (at 40 10)
    (property "Reference" "H1" (at 0 0 0) (layer "F.SilkS") (uuid "p5"))
    (attr exclude_from_pos_files exclude_from_bom)
    (pad "" np_thru_hole circle (at 0 0) (size 3 3) (drill 3) (layers "*.Cu") (uuid "d4")))
  (footprint "R:R_0603" (layer "F.Cu") (uuid "a5") (at 50 10)
    (property "Reference" "R2" (at 0 0 0) (layer "F.SilkS") (uuid "p6"))
    (attr smd dnp)
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (uuid "d5")))
  (footprint "L:LOGO" (layer "F.Cu") (uuid "a6") (at 60 10)
    (property "Reference" "G1" (at 0 0 0) (layer "F.SilkS") (uuid "p7"))
    (attr board_only exclude_from_bom))
  (gr_rect (start 0 0) (end 70 20) (stroke (width 0.1) (type default)) (fill no) (layer "Edge.Cuts") (uuid "e1"))
  (zone (net 0) (net_name "") (layer "F.Cu") (uuid "z1") (hatch edge 0.5) (connect_pads (clearance 0.2))
    (min_thickness 0.2) (fill (thermal_gap 0.5) (thermal_bridge_width 0.5))
    (polygon (pts (xy 1 1) (xy 69 1) (xy 69 19) (xy 1 19))))
)
"""


class Readers(FakeBackend):
    """The fake KiCad backend plus the real zero-dependency file readers (the pos preview, the
    layer table and the board outline come from them)."""

    def services(self, storage_home: Path) -> Services:
        svc = super().services(storage_home)
        svc.file_read_board = BoardFile.load
        svc.file_read_schematic = Hierarchy.load
        return svc


def _h(tmp_path: Path, backend: FakeBackend | None = None) -> Harness:
    return Harness(tmp_path, backend or Readers())


def _mini(tmp_path: Path) -> BoardFile:
    p = tmp_path / "mini.kicad_pcb"
    p.write_text(MINI_BOARD, encoding="utf-8")
    return BoardFile.load(p)


def _with_mini(h: Harness, name: str = "demo") -> str:
    proj = h.create(name)
    (Path(proj) / f"{name}.kicad_pcb").write_text(MINI_BOARD, encoding="utf-8")
    return proj


def _tree(d: Path) -> dict[str, str]:
    """sha256 of every file under the project, `.kiwrap/` included (nothing may change)."""
    out: dict[str, str] = {}
    for p in sorted(d.rglob("*")):
        if p.is_file():
            out[p.relative_to(d).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


# ---- assembly: position-file rules and the BOM diff (item 4) ------------------------------------------------


def test_position_rules_name_every_reason(tmp_path: Path):
    fps = _mini(tmp_path).footprints
    default = plan_positions(fps, PosOptions())
    assert default.included == ["R1", "C1", "AE1", "R2", "G1"]
    assert [(e.ref, e.reason) for e in default.excluded] == [("H1", "exclude_from_pos")]
    # included but flagged: the doc's "unspecified-type SMD parts" and a board-only logo
    assert {(f.ref, f.reason) for f in default.flags} == {
        ("AE1", "attr_unspecified"),
        ("G1", "attr_unspecified"),
        ("G1", "board_only"),
    }
    smd = plan_positions(fps, PosOptions(smd_only=True))
    assert smd.included == ["R1", "R2"]
    reasons = {e.ref: e.reasons for e in smd.excluded}
    assert reasons == {
        "C1": ("tht",),
        "AE1": ("attr_unspecified",),
        "H1": ("exclude_from_pos", "tht"),  # NPTH-only counts as through-hole (verified on pic_programmer)
        "G1": ("attr_unspecified",),
    }
    strict = plan_positions(fps, PosOptions(exclude_fp_th=True, exclude_dnp=True, side="front"))
    assert strict.included == ["R1", "G1"]
    assert {e.ref: e.reason for e in strict.excluded} == {
        "C1": "tht",
        "AE1": "side",
        "H1": "exclude_from_pos",
        "R2": "dnp",
    }
    d = strict.to_dict()
    assert d["excluded_by_reason"] == {"tht": 1, "side": 1, "exclude_from_pos": 1, "dnp": 1}
    assert all(e["why"] for e in d["excluded"]) and d["options"]["side"] == "front"


def test_pos_options_from_the_profile_and_kiwrap_dnp_skip():
    o = PosOptions.from_options({"format": "csv", "side": "both", "exclude_dnp": False}, extra_dnp=True)
    assert o == PosOptions(exclude_dnp=True)
    assert PosOptions.from_options({"smd_only": True}).smd_only


def test_bom_diff_reports_missing_and_extra_with_reasons(tmp_path: Path):
    fps = _mini(tmp_path).footprints
    plan = plan_positions(fps, PosOptions(exclude_fp_th=True))
    parts = {
        "R1": SchPart("R1"),
        "C1": SchPart("C1"),  # fitted, dropped by exclude_fp_th
        "R2": SchPart("R2", dnp=True),  # DNP in the schematic, the board footprint too
        "AE1": SchPart("AE1", in_bom=False),  # excluded from the BOM but in the CPL
        "U9": SchPart("U9"),  # not on the board (kw_pcb_sync not run)
        "J5": SchPart("J5", on_board=False),
    }
    diff = bom_diff(plan.included, parts, fps, plan)
    missing = {m["ref"]: m["reason"] for m in diff["missing_from_cpl"]}
    extra = {e["ref"]: e["reason"] for e in diff["extra_in_cpl"]}
    assert missing == {"C1": "tht", "U9": "not_on_board", "J5": "sch_not_on_board"}
    assert extra == {"AE1": "sch_not_in_bom", "R2": "sch_dnp", "G1": "board_only"}
    assert diff["bom_fitted"] == 4 and not diff["match"]
    assert bom_diff(["R1"], {"R1": SchPart("R1")}, fps)["match"]


def test_schematic_parts_skips_power_and_merges_units():
    class Sym:
        def __init__(self, ref: str, *, power: bool = False, dnp: bool = False, in_bom: bool = True):
            self.reference, self.is_power, self.dnp, self.in_bom, self.on_board = (
                ref,
                power,
                dnp,
                in_bom,
                True,
            )

        def reference_for(self, path: str) -> str:
            return self.reference

    class Inst:
        path = "/"

    class Hier:
        def symbols(self):
            yield Inst(), Sym("#PWR01", power=True)
            yield Inst(), Sym("U1", dnp=True)
            yield Inst(), Sym("U1")  # a second unit placed fitted
            yield Inst(), Sym("#FLG01")

    parts = schematic_parts(Hier())
    assert list(parts) == ["U1"] and parts["U1"].fitted


# ---- plan: layer names and predicted file names ---------------------------------------------------------------


def test_normalise_layers_accepts_canonical_standard_and_user_names(tmp_path: Path):
    from kiwrap.core.fab.plan import board_layer_names

    table = board_layer_names(_mini(tmp_path))
    assert ("F.Cu", "top_copper") in table and ("F.SilkS", None) in table  # standard user name dropped
    canon, unknown = normalise_layers(
        ["f.cu", "F.Silkscreen", "top_copper", "In1.Cu", "F.Mask", "B.Paste", "Edge.Cuts"], table
    )
    assert canon == ["F.Cu", "F.SilkS", "In1.Cu", "F.Mask", "Edge.Cuts"] and unknown == ["B.Paste"]
    assert normalise_layers(["Anything"], []) == (["Anything"], [])  # no reader: pass through


def test_gerber_and_drill_names_follow_kicad():
    """The names kicad-cli 10.0.6 / 10.99 wrote for the video demo (scratchpad spike_names.py)."""
    table = [("F.Cu", "top_copper"), ("In1.Cu", None), ("B.Cu", None), ("F.SilkS", None), ("Dwgs.User", None)]
    assert gerber_files("video", ["F.Cu", "In1.Cu", "B.Cu", "F.SilkS", "Dwgs.User"], table) == [
        "video-top_copper.gtl",
        "video-In1_Cu.g1",
        "video-B_Cu.gbl",
        "video-F_Silkscreen.gto",
        "video-User_Drawings.gbr",
        "video-job.gbrjob",
    ]
    assert gerber_files("v", ["F.Mask"], [], protel=False) == ["v-F_Mask.gbr", "v-job.gbrjob"]
    assert drill_files("v", {}) == ["v.drl"]
    assert drill_files(
        "v", {"excellon_separate_th": True, "generate_map": True, "map_format": "gerberx2"}
    ) == [
        "v-PTH.drl",
        "v-NPTH.drl",
        "v-PTH-drl_map.gbr",
        "v-NPTH-drl_map.gbr",
    ]


# ---- outputs: per-call directories and the sweep (B4) --------------------------------------------------------


class _P:
    def __init__(self, d: Path, name: str):
        self.dir, self.name = d, name


def test_per_call_dirs_are_unique_and_keyed_by_path(tmp_path: Path):
    a = outputs.per_call_dir(tmp_path, _P(tmp_path / "x" / "demo", "demo"), "pcb")
    b = outputs.per_call_dir(tmp_path, _P(tmp_path / "x" / "demo", "demo"), "pcb")
    c = outputs.per_call_dir(tmp_path, _P(tmp_path / "y" / "demo", "demo"), "pcb")
    assert a != b and a.parent == b.parent and a.parent.parent != c.parent.parent
    assert a.parent.parent.name.startswith("demo-") and a.is_dir() and b.is_dir()


def test_sweep_keeps_new_and_recent_drops_old_and_surplus(tmp_path: Path):
    kind = tmp_path / "render" / "demo-1234abcd" / "pcb"
    now = time.time()
    dirs = []
    for i in range(25):
        d = kind / f"call{i:02d}"
        d.mkdir(parents=True)
        age = 120 + i * 10  # newest first by name order
        os.utime(d, (now - age, now - age))
        dirs.append(d)
    old = kind / "ancient"
    old.mkdir()
    os.utime(old, (now - 2 * 86400, now - 2 * 86400))
    young = kind / "young"
    young.mkdir()
    os.utime(young, (now - 5, now - 5))
    removed = set(outputs.sweep(tmp_path / "render", keep=20, now=now))
    assert old in removed and young not in removed and young.exists()
    kept = sorted(p.name for p in kind.iterdir())
    # the young one is not counted against the 20: it is skipped, never deleted
    assert len(kept) == 20 and "call00" in kept and "call24" not in kept


# ---- the region rasteriser (item 38) -------------------------------------------------------------------------


def _pdf_with_rect(x0_mm: float, y0_mm: float, x1_mm: float, y1_mm: float) -> bytes:
    """An A4 page (KiCad's 1:1 plot frame) with one black rectangle at board mm (y down)."""
    k = 72 / 25.4
    h = 841.89
    content = f"0 0 0 rg {x0_mm * k:.3f} {h - y1_mm * k:.3f} {(x1_mm - x0_mm) * k:.3f} {(y1_mm - y0_mm) * k:.3f} re f"
    stream = content.encode()
    objs = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        f"<</Type/Page/Parent 2 0 R/MediaBox[0 0 595.28 {h}]/Contents 4 0 R>>".encode(),
        b"<</Length " + str(len(stream)).encode() + b">>stream\n" + stream + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj".encode() + body + b"endobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer<</Size {len(objs) + 1}/Root 1 0 R>>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def _ink(png: Path) -> tuple[int, int, int, int]:
    w, h, ch, rows = backend_render.decode_png(png.read_bytes())
    xs, ys = [], []
    for y, r in enumerate(rows):
        for x in range(w):
            if r[x * ch] < 128:
                xs.append(x)
                ys.append(y)
    return min(xs), min(ys), max(xs), max(ys)


def test_rasterise_pdf_region_maps_board_mm_and_mirrors(tmp_path: Path):
    pytest.importorskip("pypdfium2")
    pdf = tmp_path / "r.pdf"
    pdf.write_bytes(_pdf_with_rect(10, 5, 15, 8))
    out = backend_render.rasterise_pdf_region(pdf, tmp_path / "a.png", bbox_mm=(0, 0, 30, 10), px_per_mm=10)
    assert backend_render.png_size(out.read_bytes()) == (300, 100)
    x0, y0, x1, y1 = _ink(out)
    assert abs(x0 - 100) <= 1 and abs(x1 - 149) <= 1 and abs(y0 - 50) <= 1 and abs(y1 - 79) <= 1
    m = backend_render.rasterise_pdf_region(
        pdf, tmp_path / "m.png", bbox_mm=(0, 0, 30, 10), px_per_mm=10, mirror=True
    )
    mx0, _, mx1, _ = _ink(m)
    assert abs(mx0 - (300 - 1 - x1)) <= 1 and abs(mx1 - (300 - 1 - x0)) <= 1
    # a region past the paper's top-left corner still renders (MediaBox moved, not cropped)
    neg = backend_render.rasterise_pdf_region(
        pdf, tmp_path / "n.png", bbox_mm=(-10, -10, 20, 20), px_per_mm=4
    )
    nx0, ny0, _, _ = _ink(neg)
    assert abs(nx0 - 80) <= 1 and abs(ny0 - 60) <= 1
    assert backend_render.is_blank_png(
        backend_render.rasterise_pdf_region(pdf, tmp_path / "b.png", bbox_mm=(40, 40, 50, 50), px_per_mm=4)
    )


def test_plan_raster_caps_density_not_the_region():
    p = backend_render.plan_raster((0, 0, 100, 50), px_per_mm=60, max_px=4096)
    assert p.capped and p.width <= 4096 and p.height <= 2048 and p.px_per_mm < 41
    q = backend_render.plan_raster((0, 0, 20, 10), px_per_mm=40, max_px=4096)
    assert (q.width, q.height, q.capped) == (800, 400, False)
    fit = backend_render.plan_raster((70.12, 54.88, 229.6, 148.32), max_px=1024)
    assert fit.width <= 1024 and not fit.capped
    with pytest.raises(ValueError):
        backend_render.plan_raster((10, 0, 5, 10))


def test_row_helpers():
    row = bytes([1, 2, 3, 4, 5, 6, 7, 8, 9])
    assert backend_render._mirror_row(row) == bytes([7, 8, 9, 4, 5, 6, 1, 2, 3])
    assert backend_render._drop_alpha(bytes([1, 2, 3, 255, 4, 5, 6, 255])) == bytes([1, 2, 3, 4, 5, 6])


# ---- kw_pcb_render / kw_sch_render through the tool layer ------------------------------------------------------


def test_renders_never_delete_each_other(tmp_path: Path):
    """B4: a bottom render deleted the top PNG; a same-name project deleted the other's."""
    h = _h(tmp_path)
    proj = h.create("demo")
    top = h.ok("kw_pcb_render", project=proj)["data"]
    bottom = h.ok("kw_pcb_render", project=proj, side="bottom")["data"]
    sch1 = h.ok("kw_sch_render", project=proj)["data"]
    sch2 = h.ok("kw_sch_render", project=proj)["data"]
    proj2 = h.ok("kw_project", action="create", dir=str(tmp_path / "elsewhere" / "demo"), name="demo")[
        "data"
    ]["dir"]
    third = h.ok("kw_pcb_render", project=proj2)["data"]
    pngs = [Path(d["png"]) for d in (top, bottom, sch1, sch2, third)]
    assert len(set(pngs)) == 5 and all(p.is_file() for p in pngs)
    assert Path(third["png"]).parents[2] != Path(top["png"]).parents[2]  # <name>-<sha8> differs
    for d in (top, sch1):
        assert d["png_sha256"] == hashlib.sha256(Path(d["png"]).read_bytes()).hexdigest()
        assert d["kept_until"]
    assert not (Path(proj) / ".kiwrap" / "tmp").exists()


def test_render_out_path_rules(tmp_path: Path):
    h = _h(tmp_path)
    proj = h.create()
    target = tmp_path / "review" / "top.png"
    target.parent.mkdir()
    d = h.ok("kw_pcb_render", project=proj, out_path=str(target))["data"]
    assert Path(d["png"]) == target and target.is_file() and d["kept_until"] is None
    s = h.ok("kw_sch_render", project=proj, out_path=str(tmp_path / "review" / "sch.png"))["data"]
    assert Path(s["png"]).is_file() and s["kept_until"] is None
    h.fail("kw_pcb_render", "E_BAD_ARGS", project=proj, out_path="relative/top.png")
    h.fail("kw_pcb_render", "E_BAD_ARGS", project=proj, out_path=str(tmp_path / "x.kicad_pcb"))
    h.fail("kw_pcb_render", "E_BAD_ARGS", project=proj, out_path=str(tmp_path / "top.jpg"))
    h.fail("kw_pcb_render", "E_BAD_ARGS", project=proj, out_path=str(tmp_path / "missing" / "top.png"))


def test_pcb_render_region_layers_and_mirror_reach_the_backend(tmp_path: Path):
    h = _h(tmp_path)
    proj = _with_mini(h)
    sc = h.ok("kw_pcb_render", project=proj, layers=["top_copper", "F.Silkscreen", "In1.Cu"])
    kw = [k for n, k in h.backend.calls if n == "render_pcb_2d"][-1]
    assert sc["data"]["mode"] == "2d"  # layers select the 2d plot (was silently ignored with 3d)
    assert kw["layers"] == ["F.Cu", "F.SilkS", "In1.Cu"] and kw["max_px"] == 1024
    assert kw["bbox_mm"] == [-1.0, -1.0, 71.0, 21.0] and kw["mirror"] is False  # outline + 1 mm
    assert sc["data"]["region"]["bbox_mm"] == [-1.0, -1.0, 71.0, 21.0]
    sc = h.ok(
        "kw_pcb_render", project=proj, bbox_mm=[5, 5, 25, 15], px_per_mm=40, side="bottom", mirror_bottom=True
    )
    kw = [k for n, k in h.backend.calls if n == "render_pcb_2d"][-1]
    assert kw["bbox_mm"] == [5, 5, 25, 15] and kw["px_per_mm"] == 40 and kw["mirror"] is True
    assert kw["max_px"] == 4096 and sc["data"]["region"]["width"] == 800 and sc["data"]["mirrored"]
    sc = h.ok("kw_pcb_render", project=proj, bbox_mm=[0, 0, 100, 100], px_per_mm=60)
    assert sc["data"]["region"]["capped"] and any("would exceed 4096" in w for w in sc["warnings"])
    sc = h.ok("kw_pcb_render", project=proj, mirror_bottom=True)
    assert any("side='bottom' only" in w for w in sc["warnings"])
    sc = h.ok("kw_pcb_render", project=proj, view="3d", layers=["F.Cu"])
    assert sc["data"]["mode"] == "3d" and any("2d plot only" in w for w in sc["warnings"])
    err = h.fail("kw_pcb_render", "E_BAD_ARGS", project=proj, layers=["F.Cu", "In7.Cu"])
    assert "In7.Cu" in err["message"] and "In1.Cu" in err["candidates"]
    h.fail("kw_pcb_render", "E_BAD_ARGS", project=proj, bbox_mm=[10, 10, 5, 20])
    h.fail("kw_pcb_render", "E_BAD_ARGS", project=proj, bbox_mm=[1, 2, 3])
    h.fail("kw_pcb_render", "E_BAD_ARGS", project=proj, px_per_mm=61)


class _BigRender(Readers):
    def render_pcb_2d(self, install: Any, config: Any, board: Path, out_png: Path, **kw: Any) -> Path:
        self._log("render_pcb_2d", **kw)
        out_png = Path(out_png)
        out_png.parent.mkdir(parents=True, exist_ok=True)
        out_png.write_bytes(png_bytes(3000, 20))
        return out_png


def test_large_crop_is_returned_by_path_not_inlined(tmp_path: Path):
    h = _h(tmp_path, _BigRender())
    proj = h.create()
    err, sc, res = h.call("kw_pcb_render", project=proj, bbox_mm=[0, 0, 60, 1], px_per_mm=50)
    assert not err and sc["data"]["inline"] is False and sc["image_info"] is None
    assert not [c for c in res.content if type(c).__name__ == "ImageContent"]
    assert Path(sc["data"]["png"]).is_file() and any("inline budget" in w for w in sc["warnings"])


# ---- kw_fab_export for reviewers (item 4, D22) -------------------------------------------------------------


def test_fab_export_dry_run_writes_nothing_and_previews_positions(tmp_path: Path):
    h = _h(tmp_path)
    proj = _with_mini(h)
    h.ok("kw_snapshot", project=proj, action="list")
    before = _tree(Path(proj))
    calls = len(h.backend.calls)
    sc = h.ok(
        "kw_fab_export",
        project=proj,
        targets=["gerbers", "drill", "pos", "bom"],
        profile="jlcpcb",
        dry_run=True,
    )
    d = sc["data"]
    assert _tree(Path(proj)) == before and not (Path(proj) / "fab").exists()
    assert not [c for c in h.backend.calls[calls:] if c[0].startswith(("export", "render", "run_"))]
    assert d["dry_run"] and d["persisted"] is False and sc["snapshot"] is None
    assert d["gate"] == {
        "required_stage": "drc_clean",
        "stage": "created",
        "would_block": True,
        "forced": None,
    }
    g = d["plan"]["gerbers"]
    assert g["predicted"] and "demo-top_copper.gtl" in g["files"] and "demo-In1_Cu.g1" in g["files"]
    assert g["options"]["check_zones"] is True and g["zones"] == "refill"
    assert d["plan"]["drill"]["files"][:2] == ["demo-PTH.drl", "demo-NPTH.drl"]
    assert d["plan"]["pos"]["files"] == ["demo.csv", "demo-cpl-jlcpcb.csv"]
    assert d["plan"]["bom"]["files"] == ["demo-bom-jlcpcb.csv"]
    pp = d["positions_preview"]
    # jlcpcb: side both, no smd_only, exclude DNP -> H1 (exclude_from_pos) and R2 (dnp) are out
    assert pp["included"] == ["R1", "C1", "AE1", "G1"]
    assert {e["ref"]: e["reason"] for e in pp["excluded"]} == {"H1": "exclude_from_pos", "R2": "dnp"}
    assert {f["ref"] for f in pp["flags"] if f["reason"] == "attr_unspecified"} == {"AE1", "G1"}
    extra = {e["ref"]: e["reason"] for e in pp["bom_diff"]["extra_in_cpl"]}
    assert extra["G1"] == "board_only" and extra["R1"] == "not_in_schematic"
    assert d["bundle"].endswith("demo-jlcpcb-gerbers.zip")
    # force is reported as the gate override that would apply
    d = h.ok("kw_fab_export", project=proj, targets=["pos"], dry_run=True, force={"reason": "review"})["data"]
    assert d["gate"]["would_block"] is False and d["gate"]["forced"] == "review"


def test_fab_export_layers_filter(tmp_path: Path):
    h = _h(tmp_path)
    proj = _with_mini(h)
    force = {"reason": "review"}
    sc = h.ok(
        "kw_fab_export", project=proj, targets=["gerbers", "pdf"], layers=["F.Paste", "f.mask"], force=force
    )
    calls = {k["target"]: k for n, k in h.backend.calls if n == "export_pcb"}
    assert calls["gerbers"]["layers"] == ["F.Paste", "F.Mask"] and calls["pdf"]["layers"] == [
        "F.Paste",
        "F.Mask",
    ]
    assert sc["data"]["layers"] == ["F.Paste", "F.Mask"]
    # a vendor bundle from a partial layer set would be misleading: not built, said so
    sc = h.ok(
        "kw_fab_export", project=proj, targets=["gerbers"], profile="jlcpcb", layers=["F.Paste"], force=force
    )
    assert "bundle" not in sc["data"]["files"] and any("bundle: not built" in w for w in sc["warnings"])
    gcall = [k for n, k in h.backend.calls if n == "export_pcb" and k["target"] == "gerbers"][-1]
    assert gcall["layers"] == ["F.Paste"] and gcall["subtract_soldermask"] is True
    err = h.fail(
        "kw_fab_export", "E_BAD_ARGS", project=proj, targets=["gerbers"], layers=["In3.Cu"], force=force
    )
    assert "In1.Cu" in err["candidates"]
    sc = h.ok("kw_fab_export", project=proj, targets=["drill"], layers=["F.Cu"], force=force)
    assert any("layers applies to" in w for w in sc["warnings"])


def test_fab_export_zones_refill_or_stale(tmp_path: Path):
    """D22: copper plots refill in memory where the KiCad lists --check-zones, else zones_stale."""
    caps = {**CAPS_10, "cli.flags.pcb export gerbers": ["--output", "--layers"]}
    h = _h(tmp_path, Readers(caps={"10.0.0": caps}))
    proj = _with_mini(h)
    sc = h.ok("kw_fab_export", project=proj, targets=["gerbers", "svg"], force={"reason": "t"})
    calls = {k["target"]: k for n, k in h.backend.calls if n == "export_pcb"}
    assert "check_zones" not in calls["gerbers"] and calls["svg"]["check_zones"] is True
    assert sc["data"]["zones"] == {"gerbers": "stale", "svg": "refill"}
    assert "zones_stale" in sc["degraded"] and any("1 zone fill" in w for w in sc["warnings"])


def test_fab_export_real_pos_reports_exclusions(tmp_path: Path):
    class PosBackend(Readers):
        def export_pcb(self, install: Any, config: Any, board: Path, target: str, out_dir: Path, **opts: Any):
            if target != "pos":
                return super().export_pcb(install, config, board, target, out_dir, **opts)
            self._log("export_pcb", target=target, out_dir=str(out_dir), **opts)
            out = Path(out_dir) / f"{Path(board).stem}.pos"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(
                "### Footprint positions ###\n## Unit = mm, Angle = deg.\n"
                "# Ref Val Package PosX PosY Rot Side\n"
                "R1 10k R_0603 10 -10 0 top\nC1 1u C_THT 20 -10 0 top\nAE1 ANT ANT 30 -10 0 bottom\n"
                "R2 1k R_0603 50 -10 0 top\nG1 LOGO LOGO 60 -10 0 top\n## End\n",
                encoding="utf-8",
            )
            return [out]

    h = _h(tmp_path, PosBackend())
    proj = _with_mini(h)
    sc = h.ok("kw_fab_export", project=proj, targets=["pos"], force={"reason": "t"})
    pos = sc["data"]["positions"]
    assert pos["refs"] == ["R1", "C1", "AE1", "R2", "G1"]
    assert [(e["ref"], e["reason"]) for e in pos["excluded"]] == [("H1", "exclude_from_pos")]
    assert {f["ref"] for f in pos["flags"] if f["reason"] == "attr_unspecified"} == {"AE1", "G1"}
    assert any("footprint type 'unspecified'" in w for w in sc["warnings"])


def test_fab_export_out_dir_rules(tmp_path: Path):
    h = _h(tmp_path)
    proj = h.create()
    force = {"reason": "t"}
    h.fail(
        "kw_fab_export", "E_BAD_ARGS", project=proj, targets=["gerbers"], out_dir="relative/dir", force=force
    )
    h.fail(
        "kw_fab_export",
        "E_BAD_ARGS",
        project=proj,
        targets=["gerbers"],
        out_dir=str(tmp_path / "a" / "b" / "c"),
        force=force,
    )
    outside = tmp_path / "review-out"
    sc = h.ok("kw_fab_export", project=proj, targets=["gerbers"], out_dir=str(outside), force=force)
    assert Path(sc["data"]["out_dir"]) == outside and all(
        Path(f).is_relative_to(outside) for f in sc["data"]["files"]["gerbers"]
    )


def test_fab_export_and_bom_read_only_leave_the_project_alone(tmp_path: Path):
    """D23 through the tool layer: persist=false exports run detached; nothing lands in the project."""
    h = _h(tmp_path)
    proj = _with_mini(h)
    h.ok("kw_snapshot", project=proj, action="list")  # settle the snapshot store before hashing
    before = _tree(Path(proj))
    snaps = len(h.ok("kw_snapshot", project=proj, action="list")["data"]["snapshots"])
    sc = h.ok(
        "kw_fab_export",
        project=proj,
        targets=["gerbers", "pos", "bom"],
        persist=False,
        force={"reason": "review"},
    )
    assert sc["data"]["persisted"] is False and sc["snapshot"] is None
    out = Path(sc["data"]["out_dir"])
    assert not out.is_relative_to(Path(proj)) and out.is_relative_to(h.home)
    assert all(Path(f).is_file() for lst in sc["data"]["files"].values() for f in lst)
    sc = h.ok("kw_fab_bom", project=proj, write=True, persist=False)
    assert sc["data"]["file"] and not Path(sc["data"]["file"]).is_relative_to(Path(proj))
    sc = h.ok("kw_pcb_render", project=proj, persist=False)
    assert Path(sc["data"]["png"]).is_relative_to(h.home)
    assert _tree(Path(proj)) == before
    assert len(h.ok("kw_snapshot", project=proj, action="list")["data"]["snapshots"]) == snaps


def test_fab_bom_scratch_is_never_in_the_project(tmp_path: Path):
    h = _h(tmp_path)
    proj = h.create()
    h.ok("kw_fab_bom", project=proj)
    assert not (Path(proj) / ".kiwrap" / "tmp").exists()
    assert (h.home / "tmp" / "work").is_dir()
