"""Live regressions for requests/kiwrap-feature-requests.review.md item 1 (B1) and B2 (D21, D22).

The review reproduced four paths that re-stamped a KiCad 10.0 project as 10.99 on default arguments
(scratch copies of the 10.0 demos; kit-dev-coldfire re-saved by 10.0.6 `pcb upgrade`,
pic_programmer's two sheets) and a `.kicad_prl` rewrite nobody undid. Each scenario runs twice
through the real MCP tool handlers with EXPLICIT install filters (the server's discovery is
replaced by a filtered list): with only the 10.99 nightly considered (reads run, writes refuse
with E_FORMAT_UPGRADE_REQUIRED - no client to ask here - and nothing on disk changes) and with
10.0.6 installed too (10.0.6 is chosen and every stamp is kept). Then the `kw_project upgrade`
round trip and E_FORMAT_NEWER. Nothing here starts an api-server (the refusals happen before
any, and 10.0.6 has no headless IPC) or a GUI; every process runs in the session's sandbox.
"""

from __future__ import annotations

import hashlib
import shutil
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import anyio
import pytest

from kiwrap.core.server import build_context, build_server
from kiwrap.core.services import Services
from kiwrap.core.state.project import file_epoch

pytestmark = [pytest.mark.kicad, pytest.mark.kicad_version("10.0"), pytest.mark.kicad_version("10.99")]

PCB_10, SCH_10_DEMO = 20260206, 20260101


def _stock(installs: list[Any], major_minor: str) -> Any:
    found = [i for i in installs if i.major_minor == major_minor and not i.is_bench_build]
    if not found:
        pytest.skip(f"no stock KiCad {major_minor}")
    return found[0]


@pytest.fixture(scope="module")
def i10(installs: list[Any]) -> Any:
    return _stock(installs, "10.0")


@pytest.fixture(scope="module")
def i99(installs: list[Any]) -> Any:
    return _stock(installs, "10.99")


class Server:
    """The real tool handlers with discovery limited to `keep`."""

    def __init__(self, storage_home: Path, keep: list[Any]):
        svc = Services.real(storage_home)
        svc.discover = lambda **kw: list(keep)  # type: ignore[method-assign]
        self.server = build_server(context=build_context(storage_home, svc))

    def call(self, tool: str, **args: Any) -> tuple[bool, dict[str, Any]]:
        result = anyio.run(self.server.call_tool, tool, args)
        return bool(result.is_error), dict(result.structured_content or {})

    def ok(self, tool: str, **args: Any) -> dict[str, Any]:
        err, sc = self.call(tool, **args)
        assert not err, sc
        return sc

    def fail(self, tool: str, code: str, **args: Any) -> dict[str, Any]:
        err, sc = self.call(tool, **args)
        assert err and sc.get("code") == code, sc
        return sc


def _rmtree(path: Path) -> None:
    shutil.rmtree(path, onexc=lambda fn, p, _e: (Path(p).chmod(stat.S_IWRITE), fn(p)))


def digest(d: Path) -> dict[str, str]:
    """sha256 of every KiCad file of the project, `.kicad_prl` included (B2)."""
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(d.glob("*.kicad_*"))}


def _with_prl(d: Path, name: str) -> Path:
    prl = d / f"{name}.kicad_prl"
    prl.write_text(
        '{\n  "board": {"active_layer": 0, "visible_layers": "ffffffff_ffffffff"},\n'
        f'  "meta": {{"filename": "{name}.kicad_prl", "version": 5}}\n}}\n',
        encoding="utf-8",
    )
    return prl


@pytest.fixture(scope="module")
def pic_template(i10: Any, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The 10.0 pic_programmer demo (two sheets stamped 20260101, board 20260206) + a user .kicad_prl."""
    src = i10.dir("demos") / "pic_programmer"
    if not src.is_dir():
        pytest.skip("pic_programmer demo missing")
    dst = tmp_path_factory.mktemp("tpl") / "pic_programmer"
    shutil.copytree(src, dst)
    _with_prl(dst, "pic_programmer")
    assert file_epoch(dst / "pic_programmer.kicad_pcb") == PCB_10
    assert file_epoch(dst / "pic_sockets.kicad_sch") == SCH_10_DEMO
    return dst


@pytest.fixture(scope="module")
def coldfire_template(i10: Any, storage_home: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """kit-dev-coldfire with its board re-saved by 10.0.6 `pcb upgrade` - the review's exact
    reproduction board (`version 20260206`); its schematics stay in the demo's 9.0 format."""
    from kiwrap import sandbox
    from kiwrap.backends.cli.runner import run_kicad_cli

    src = i10.dir("demos") / "kit-dev-coldfire-xilinx_5213"
    if not src.is_dir():
        pytest.skip("kit-dev-coldfire demo missing")
    dst = tmp_path_factory.mktemp("tpl") / "coldfire"
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("*.3dshapes"))
    board = dst / "kit-dev-coldfire-xilinx_5213.kicad_pcb"
    config = sandbox.ensure(i10, storage_home=storage_home)
    run_kicad_cli(i10, ["pcb", "upgrade", "--force", str(board)], config=config, accept=(0,))
    assert file_epoch(board) == PCB_10
    _with_prl(dst, "kit-dev-coldfire-xilinx_5213")
    return dst


@pytest.fixture
def pic(pic_template: Path, tmp_path: Path) -> Iterator[Path]:
    d = tmp_path / "pic_programmer"
    shutil.copytree(pic_template, d)
    yield d
    _rmtree(d)


# ---- (a) kw_pcb_drc with its defaults + B2 ---------------------------------------------------------------


@pytest.mark.parametrize("scenario", ["only-10.99", "with-10.0.6"])
def test_pcb_drc_defaults_keep_the_board_and_the_prl(
    scenario: str,
    coldfire_template: Path,
    i10: Any,
    i99: Any,
    storage_home: Path,
    tmp_path: Path,
    user_config_untouched: None,
):
    d = tmp_path / "coldfire"
    shutil.copytree(coldfire_template, d)
    before = digest(d)
    srv = Server(storage_home, [i99] if scenario == "only-10.99" else [i99, i10])
    sc = srv.ok("kw_pcb_drc", project=str(d))
    assert sc["data"]["options"]["save_board"] is False
    if scenario == "only-10.99":
        # the nearest newer KiCad checks in memory; the receipt says why
        assert sc["provider_used"] == f"cli:{i99.version}"
        assert sc["data"]["target"]["source"] == "file_epoch_newer_readonly"
    else:
        # the board is 10.0 format: 10.0.6 runs it (the schematics are older, reads only)
        assert sc["provider_used"] == f"cli:{i10.version}"
    assert digest(d) == before  # board AND .kicad_prl byte-identical
    # an explicit save with only 10.99 would up-convert: refused (no client to ask), nothing changed
    if scenario == "only-10.99":
        err = srv.fail("kw_pcb_drc", "E_FORMAT_UPGRADE_REQUIRED", project=str(d), save_board=True)
        assert err["data"]["upgrade_to"] == i99.version and "winget install" in (
            err["data"]["install_hint"] or ""
        )
        assert digest(d) == before
    _rmtree(d)


# ---- (c) ipc11 board write ---------------------------------------------------------------------------------


@pytest.mark.parametrize("scenario", ["only-10.99", "with-10.0.6"])
def test_pcb_place_ipc11_never_restamps_a_10_0_board(
    scenario: str, pic: Path, i10: Any, i99: Any, storage_home: Path, user_config_untouched: None
):
    srv = Server(storage_home, [i99] if scenario == "only-10.99" else [i99, i10])
    fp = srv.ok("kw_pcb_get", project=str(pic), kinds=["footprints"], refs=["C1"], provider="file")
    c1 = next(i for i in fp["data"]["items"] if i.get("ref") == "C1")
    at = [c1["x_mm"], c1["y_mm"]]  # a no-op move, as in the review
    before = digest(pic)
    args = {"project": str(pic), "placements": [{"ref": "C1", "at_mm": at}], "provider": "ipc11"}
    if scenario == "only-10.99":
        err = srv.fail("kw_pcb_place", "E_FORMAT_UPGRADE_REQUIRED", **args)
        assert err["data"]["project_kicad"] == "10.0"
        assert digest(pic) == before
    else:
        sc = srv.ok("kw_pcb_place", **args)
        assert sc["provider_used"] == "file" and "ipc11_unavailable" in sc["degraded"]
        assert file_epoch(pic / "pic_programmer.kicad_pcb") == PCB_10
        assert digest(pic)["pic_programmer.kicad_prl"] == before["pic_programmer.kicad_prl"]


# ---- (c)/(d) schematic writes: ipc11 and file --------------------------------------------------------------


@pytest.mark.parametrize("provider", ["ipc11", "file"])
@pytest.mark.parametrize("scenario", ["only-10.99", "with-10.0.6"])
def test_sch_edit_never_restamps_the_sheets(
    scenario: str,
    provider: str,
    pic: Path,
    i10: Any,
    i99: Any,
    storage_home: Path,
    user_config_untouched: None,
):
    srv = Server(storage_home, [i99] if scenario == "only-10.99" else [i99, i10])
    before = digest(pic)
    args = {
        "project": str(pic),
        "ops": [{"op": "property", "ref": "C1", "name": "Value", "value": "22nF"}],
        "provider": provider,
    }
    if scenario == "only-10.99":
        srv.fail("kw_sch_edit", "E_FORMAT_UPGRADE_REQUIRED", **args)
        assert digest(pic) == before
        return
    sc = srv.ok("kw_sch_edit", **args)
    assert sc["provider_used"] == "file"
    assert file_epoch(pic / "pic_programmer.kicad_sch") == SCH_10_DEMO
    assert file_epoch(pic / "pic_sockets.kicad_sch") == SCH_10_DEMO
    assert "pic_programmer.kicad_sch" in sc["changed"]["updated"]
    assert digest(pic)["pic_programmer.kicad_prl"] == before["pic_programmer.kicad_prl"]


# ---- kw_project upgrade round trip + E_FORMAT_NEWER -------------------------------------------------------


def test_project_upgrade_round_trip_and_format_newer(
    pic: Path, i10: Any, i99: Any, storage_home: Path, tmp_path: Path, user_config_untouched: None
):
    original = digest(pic)
    srv = Server(storage_home, [i99])
    st = srv.ok("kw_project", action="status", project=str(pic))["data"]
    assert (
        st["target_effective"] == i99.version and st["epoch_match"] is False and st["project_kicad"] == "10.0"
    )
    srv.fail("kw_project", "E_FORMAT_UPGRADE_REQUIRED", action="upgrade", project=str(pic))
    assert digest(pic) == original
    sc = srv.ok("kw_project", action="upgrade", project=str(pic), confirm=True)
    up = sc["data"]["upgrade"]
    assert sc["data"]["upgraded"] is True and sc["data"]["target_kicad"] == i99.version
    assert sorted(up["replaced"]) == [
        "pic_programmer.kicad_pcb",
        "pic_programmer.kicad_sch",
        "pic_sockets.kicad_sch",
    ]
    formats = st["candidates"][0]["formats"]
    # every sheet (the child too: `sch upgrade` of the root alone leaves it) and the board
    assert file_epoch(pic / "pic_programmer.kicad_sch") == formats["sch"]
    assert file_epoch(pic / "pic_sockets.kicad_sch") == formats["sch"]
    assert file_epoch(pic / "pic_programmer.kicad_pcb") == formats["pcb"]
    checks = up["checks"]
    assert checks["before"]["erc"]["errors"] == checks["after"]["erc"]["errors"]
    assert checks["before"]["drc"]["errors"] == checks["after"]["drc"]["errors"]
    assert digest(pic)["pic_programmer.kicad_prl"] == original["pic_programmer.kicad_prl"]
    st = srv.ok("kw_project", action="status", project=str(pic))["data"]
    assert st["target_source"] == "pinned" and st["epoch_match"] is True

    # the upgraded files are newer than 10.0.6: with only it considered, KiCad reads are refused
    newer = tmp_path / "newer"
    shutil.copytree(pic, newer, ignore=shutil.ignore_patterns(".kiwrap"))
    old = Server(storage_home, [i10])
    err = old.fail("kw_pcb_drc", "E_FORMAT_NEWER", project=str(newer))
    assert err["data"]["project_epoch"] == {"sch": formats["sch"], "pcb": formats["pcb"]}
    # ...while the file provider still answers reads
    assert old.ok("kw_pcb_get", project=str(newer), kinds=["footprints"], limit=1, provider="file")[
        "provider_used"
    ]
    _rmtree(newer)

    # the pre-upgrade snapshot brings the original bytes back
    srv.ok("kw_snapshot", project=str(pic), action="restore", ref=up["snapshot_before"])
    restored = digest(pic)
    assert {k: v for k, v in restored.items() if k != "pic_programmer.kicad_prl"} == {
        k: v for k, v in original.items() if k != "pic_programmer.kicad_prl"
    }
