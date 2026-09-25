"""Live checks of the reviewer outputs (requests/kiwrap-feature-requests.review.md items 4, 7 / B4,
38; D22) against stock KiCad 10.0.6 and the 10.99 nightly, on scratch copies of their demos.

Every scenario runs through the real tool handlers with an EXPLICIT install filter (the server's
discovery is replaced by the one install under test, or by both for the item 1 regression), in
the session's sandboxed config root; no GUI, no api-server. What is locked in:

- the position-file rules kiwrap predicts (`kiwrap.core.fab.assembly`) equal the rows kicad-cli
  writes, per option combination, on pic_programmer (THT + NPTH), kit-dev-coldfire (SMD + THT),
  openair-max (DNP), royalblue54L_feather (exclude_from_pos_files) and a hand-written board that
  holds every rule (unspecified type, board-only, DNP, exclude_from_pos, back side);
- `kw_fab_export(dry_run=true)` names exactly the files the real export writes (video, 4 layers,
  a user-named F.Cu), `--check-zones` leaves the board byte-identical, the layers filter;
- `persist=false` exports leave every byte of the project (and `.kicad_prl`) as it was;
- 2D renders are cropped to the board (no drawing sheet), a `bbox_mm` crop at 40 px/mm, inner /
  paste / mask layers, `mirror_bottom` is an exact left-right flip, and two renders never delete
  each other (B4);
- item 1: on a 10.0-format board with 10.0.6 and 10.99 both installed, renders and exports run on
  10.0.6 and never touch the design files.
"""

from __future__ import annotations

import contextlib
import csv
import hashlib
import io
import shutil
import stat
from pathlib import Path
from typing import Any

import anyio
import pytest

from kiwrap.backends.cli import render as backend_render
from kiwrap.core.fab.assembly import PosOptions, plan_positions
from kiwrap.core.server import build_context, build_server
from kiwrap.core.services import Services
from kiwrap.core.state.project import file_epoch
from kiwrap.formats.board import BoardFile
from kiwrap.formats.reports.position import parse_pos
from tests.test_fab_reviewer_outputs import MINI_BOARD

pytestmark = [pytest.mark.kicad, pytest.mark.slow, pytest.mark.usefixtures("user_config_untouched")]

TARGETS = ("10.0", "10.99")


def _stock(installs: list[Any], major_minor: str) -> Any:
    found = [i for i in installs if i.major_minor == major_minor and not i.is_bench_build]
    if not found:
        pytest.skip(f"no stock KiCad {major_minor}")
    return found[0]


class Server:
    """The real tool handlers with discovery limited to `keep`."""

    def __init__(self, storage_home: Path, keep: list[Any]):
        svc = Services.real(storage_home)
        svc.discover = lambda **kw: list(keep)  # type: ignore[method-assign]
        self.services = svc
        self.server = build_server(context=build_context(storage_home, svc))

    def call(self, tool: str, **args: Any) -> tuple[bool, dict[str, Any]]:
        result = anyio.run(self.server.call_tool, tool, args)
        return bool(result.is_error), dict(result.structured_content or {})

    def ok(self, tool: str, **args: Any) -> dict[str, Any]:
        err, sc = self.call(tool, **args)
        assert not err, sc
        return sc


def _rmtree(path: Path) -> None:
    """`shutil.rmtree` that also removes read-only files on Python 3.11 (`rmtree(onexc=)` is 3.12+,
    and CI runs 3.11): clear the read-only bit first, then remove."""
    if not path.exists():
        return
    for p in path.rglob("*"):
        with contextlib.suppress(OSError):
            p.chmod(stat.S_IREAD | stat.S_IWRITE)
    shutil.rmtree(path)


def _demo(install: Any, name: str, dst: Path) -> Path:
    src = install.dir("demos") / name
    if not src.is_dir():
        pytest.skip(f"demo {name} not shipped with KiCad {install.version}")
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("*.3dshapes", "*.step", "*.wrl"))
    return dst


def _tree(d: Path) -> dict[str, str]:
    return {
        p.relative_to(d).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(d.rglob("*"))
        if p.is_file()
    }


def _prl(d: Path, name: str) -> None:
    (d / f"{name}.kicad_prl").write_text(
        '{\n  "board": {"active_layer": 0},\n  "meta": {"filename": "x.kicad_prl", "version": 5}\n}\n',
        encoding="utf-8",
    )


@pytest.fixture(params=TARGETS)
def inst(request: pytest.FixtureRequest, installs: list[Any]) -> Any:
    return _stock(installs, request.param)


# ---- position-file rules == kicad-cli (item 4) -----------------------------------------------------------------

_COMBOS = [
    {},
    {"smd_only": True},
    {"exclude_fp_th": True},
    {"exclude_dnp": True},
    {"side": "front"},
    {"side": "back", "exclude_dnp": True, "smd_only": True},
]


def _kicad_refs(srv: Server, install: Any, board: Path, out: Path, opts: dict[str, Any]) -> list[str]:
    svc = srv.services
    files = svc.export_pcb(
        install,
        svc.config_root(install),
        board,
        "pos",
        out,
        caps=svc.caps(install),
        format="csv",
        units="mm",
        **({"side": "both"} | opts),
    )
    text = next(p for p in files if p.suffix == ".csv").read_text(encoding="utf-8")
    return [r.ref for r in parse_pos(text).rows]


@pytest.mark.parametrize(
    "demo",
    ["pic_programmer", "kit-dev-coldfire-xilinx_5213", "openair-max", "royalblue54L_feather", "mini"],
)
def test_position_rules_match_kicad(inst: Any, demo: str, storage_home: Path, tmp_path: Path):
    srv = Server(storage_home, [inst])
    if demo == "mini":
        d = tmp_path / "mini"
        d.mkdir()
        board = d / "mini.kicad_pcb"
        board.write_text(MINI_BOARD, encoding="utf-8")
    else:
        d = _demo(inst, demo, tmp_path / demo)
        board = next(d.glob("*.kicad_pcb"))
    before = hashlib.sha256(board.read_bytes()).hexdigest()
    fps = BoardFile.load(board).footprints
    seen_reasons: set[str] = set()
    for i, opts in enumerate(_COMBOS):
        kicad = _kicad_refs(srv, inst, board, tmp_path / f"out{i}", opts)
        plan = plan_positions(fps, PosOptions.from_options(opts | {"side": opts.get("side", "both")}))
        assert sorted(plan.included) == sorted(kicad), (opts, set(plan.included) ^ set(kicad))
        seen_reasons |= {e.reason for e in plan.excluded}
    assert hashlib.sha256(board.read_bytes()).hexdigest() == before
    if demo == "mini":
        assert seen_reasons == {"exclude_from_pos", "attr_unspecified", "tht", "dnp", "side"}
    if demo == "openair-max":
        assert "dnp" in seen_reasons
    if demo == "royalblue54L_feather":
        assert "exclude_from_pos" in seen_reasons


# ---- dry run == real export; check-zones; layers (item 4, D22) ---------------------------------------------


def test_dry_run_plan_is_what_the_export_writes(inst: Any, storage_home: Path, tmp_path: Path):
    d = _demo(inst, "video", tmp_path / "video")
    _prl(d, "video")
    srv = Server(storage_home, [inst])
    before = {p.name: p.read_bytes() for p in d.glob("*.kicad_*")}
    force = {"reason": "live test"}
    for profile, layers in (
        ("jlcpcb", None),
        ("generic", None),
        ("generic", ["In1.Cu", "F.Paste", "F.Mask"]),
    ):
        targets = ["gerbers", "drill", "pos", "bom"]
        plan = srv.ok(
            "kw_fab_export",
            project=str(d),
            targets=targets,
            profile=profile,
            layers=layers,
            dry_run=True,
            force=force,
        )["data"]
        out = tmp_path / f"out-{profile}-{bool(layers)}"
        out.mkdir()
        real = srv.ok(
            "kw_fab_export",
            project=str(d),
            targets=targets,
            profile=profile,
            layers=layers,
            out_dir=str(out),
            force=force,
        )
        files = real["data"]["files"]
        for t in targets:
            planned = set(plan["plan"][t]["files"])
            written = {Path(f).name for f in files[t]}
            assert planned == written, (profile, t, planned ^ written)
        assert real["data"]["zones"]["gerbers"] == "refill" and "zones_stale" not in real["degraded"]
        assert plan["plan"]["gerbers"]["options"].get("check_zones") is True
        if layers:
            assert {Path(f).name for f in files["gerbers"]} == {
                "video-In1_Cu.g1",
                "video-F_Paste.gtp",
                "video-F_Mask.gts",
                "video-job.gbrjob",
            }
        pos = real["data"]["positions"]
        assert pos["excluded"] == [] or all(e["reason"] != "unknown" for e in pos["excluded"])
        if plan.get("positions_preview"):
            assert sorted(plan["positions_preview"]["included"]) == sorted(pos["refs"])
    # --check-zones refilled in memory only: every design file (and the .kicad_prl, B2) unchanged
    assert {p.name: p.read_bytes() for p in d.glob("*.kicad_*")} == before


def test_read_only_export_leaves_the_project_byte_identical(inst: Any, storage_home: Path, tmp_path: Path):
    d = _demo(inst, "pic_programmer", tmp_path / "pic_programmer")
    _prl(d, "pic_programmer")
    before = _tree(d)
    srv = Server(storage_home, [inst])
    sc = srv.ok(
        "kw_fab_export",
        project=str(d),
        targets=["gerbers", "drill", "pos", "bom"],
        profile="jlcpcb",
        persist=False,
        force={"reason": "review"},
    )
    data = sc["data"]
    assert data["persisted"] is False and sc["snapshot"] is None
    assert Path(data["out_dir"]).is_relative_to(storage_home)
    cpl = Path(data["positions"]["file"])
    rows = list(csv.reader(io.StringIO(cpl.read_text(encoding="utf-8"))))
    assert len(rows) - 1 == data["positions"]["rows"] > 0
    assert _tree(d) == before and not (d / ".kiwrap").exists() and not (d / "fab").exists()
    _rmtree(d)


# ---- renders (items 7 / B4 and 38) ---------------------------------------------------------------------------


def _flip(png: Path) -> list[bytes]:
    w, h, ch, rows = backend_render.decode_png(png.read_bytes())
    assert ch == 3
    return [backend_render._mirror_row(r) for r in rows]


def test_render_crop_layers_mirror_and_no_deletion(inst: Any, storage_home: Path, tmp_path: Path):
    d = _demo(inst, "kit-dev-coldfire-xilinx_5213", tmp_path / "coldfire")
    board = next(d.glob("*.kicad_pcb"))
    outline = BoardFile.load(board).outline_bbox()
    assert outline is not None
    before = _tree(d)
    srv = Server(storage_home, [inst])
    p = str(d)
    full = srv.ok("kw_pcb_render", project=p, view="2d")["data"]
    region = full["region"]["bbox_mm"]
    assert region == pytest.approx(
        [outline.min_x - 1, outline.min_y - 1, outline.max_x + 1, outline.max_y + 1], abs=1e-3
    )
    # the board fills the image: ~6.4 px/mm on coldfire, where the whole A4 sheet gave ~3.4
    assert full["width"] == 1024 and full["region"]["px_per_mm"] > 6 and not full.get("blank")
    ratio = (region[2] - region[0]) / (region[3] - region[1])
    assert abs(full["width"] / full["height"] - ratio) < 0.01
    crop = srv.ok(
        "kw_pcb_render",
        project=p,
        layers=["In1.Cu", "F.Paste", "F.Mask", "Edge.Cuts"],
        bbox_mm=[100, 80, 120, 100],
        px_per_mm=40,
    )["data"]
    assert (crop["width"], crop["height"]) == (800, 800) and crop["mode"] == "2d" and not crop.get("blank")
    assert crop["layers"] == ["In1.Cu", "F.Paste", "F.Mask", "Edge.Cuts"] and crop["inline"] is True
    plain = srv.ok("kw_pcb_render", project=p, side="bottom", layers=["B.Cu", "B.SilkS", "Edge.Cuts"])["data"]
    mirrored = srv.ok(
        "kw_pcb_render", project=p, side="bottom", layers=["B.Cu", "B.SilkS", "Edge.Cuts"], mirror_bottom=True
    )["data"]
    assert (
        mirrored["mirrored"]
        and _flip(Path(plain["png"])) == backend_render.decode_png(Path(mirrored["png"]).read_bytes())[3]
    )
    # B4: every render is still on disk after the later ones; nothing landed in the project
    for r in (full, crop, plain, mirrored):
        png = Path(r["png"])
        assert png.is_file() and hashlib.sha256(png.read_bytes()).hexdigest() == r["png_sha256"]
    assert _tree(d) == before
    _rmtree(d)


# ---- item 1: a 10.0-format project with 10.99 installed too ----------------------------------------------------


def test_outputs_of_a_10_0_board_run_on_10_0_6(installs: list[Any], storage_home: Path, tmp_path: Path):
    """The review's coldfire reproduction (board re-saved by 10.0.6 `pcb upgrade`, 20260206): with
    both KiCads installed, renders and exports pick 10.0.6 (effective_target) and every design
    file keeps its bytes and stamp."""
    from kiwrap import sandbox
    from kiwrap.backends.cli.runner import run_kicad_cli

    i10, i99 = _stock(installs, "10.0"), _stock(installs, "10.99")
    d = _demo(i10, "kit-dev-coldfire-xilinx_5213", tmp_path / "coldfire")
    board = next(d.glob("*.kicad_pcb"))
    run_kicad_cli(
        i10,
        ["pcb", "upgrade", "--force", str(board)],
        config=sandbox.ensure(i10, storage_home=storage_home),
        accept=(0,),
    )
    stamp = file_epoch(board)
    assert stamp == 20260206
    _prl(d, board.stem)
    before = _tree(d)
    srv = Server(storage_home, [i99, i10])
    sc = srv.ok("kw_pcb_render", project=str(d), layers=["F.Cu", "Edge.Cuts"])
    assert sc["provider_used"] == f"cli:{i10.version}"
    sc = srv.ok(
        "kw_fab_export",
        project=str(d),
        targets=["gerbers", "pos"],
        out_dir=str(tmp_path),
        clean=False,
        force={"reason": "live test"},
    )
    assert sc["provider_used"] == f"cli:{i10.version}" and sc["data"]["zones"]["gerbers"] == "refill"
    after = _tree(d)
    design = {k: v for k, v in before.items() if not k.startswith(".kiwrap/")}
    assert {k: after.get(k) for k in design} == design and file_epoch(board) == stamp
    _rmtree(d)
