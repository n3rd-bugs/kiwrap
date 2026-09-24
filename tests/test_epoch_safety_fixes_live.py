"""Live regressions for the fixer findings on the D21 work, reproduced as the reviewer did: scratch
copies of KiCad's own 10.0 demos with stock kicad-cli 10.0.6 and the 10.99 nightly, through the real
MCP tool handlers with an explicit install list (see tests/test_epoch_safety_live.py `Server`).

- finding 1 (e9c / e9): `kw_project create` over pic_programmer whose sheets were re-saved by 10.0.6
  and whose `.kicad_pro` was deleted - same-format and default (newer) target;
- finding 2 (e4 / e4b): royalblue54L_feather keeps four 9.0 sheets under `sch/`;
  `kw_project upgrade(to_kicad='10.0')` must convert them too;
- finding 5 (e3): an upgrade whose board cannot be replaced (read-only) must leave nothing behind;
- finding 6 (e7 / e7b): a mixed 10.99 / 10.0 project must not be called "KiCad 11.0 format".

Nothing here starts an api-server or a GUI; kicad-cli runs in the session's sandbox.
"""

from __future__ import annotations

import hashlib
import shutil
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from kiwrap.core.state.project import file_epoch
from kiwrap.core.state.snapshots import _git
from kiwrap.core.tools.project import REPLACE_TMP_SUFFIX
from tests.test_epoch_safety_live import Server

pytestmark = [pytest.mark.kicad, pytest.mark.kicad_version("10.0"), pytest.mark.kicad_version("10.99")]

SCH_10_DEMO, PCB_10 = 20260101, 20260206
SCH_9 = 20250114


def _stock(installs: list[Any], major_minor: str) -> Any:
    found = [i for i in installs if i.major_minor == major_minor and not i.is_bench_build]
    if not found:
        pytest.skip(f"no stock KiCad {major_minor}")
    return found[0]


@pytest.fixture(scope="module")
def stable(installs: list[Any]) -> Any:
    return _stock(installs, "10.0")


@pytest.fixture(scope="module")
def nightly(installs: list[Any]) -> Any:
    return _stock(installs, "10.99")


def _rmtree(path: Path) -> None:
    if not path.exists():
        return
    for p in path.rglob("*"):
        if p.is_file():
            p.chmod(stat.S_IREAD | stat.S_IWRITE)
    shutil.rmtree(path)


def tree_digest(d: Path) -> dict[str, str]:
    """sha256 of every KiCad file under `d` (sub-sheets included, `.kiwrap` excluded)."""
    return {
        p.relative_to(d).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(d.rglob("*.kicad_*"))
        if ".kiwrap" not in p.relative_to(d).parts
    }


def _resave(install: Any, storage_home: Path, *files: Path) -> None:
    """Re-save files in place with `install`'s kicad-cli (scratch copies only)."""
    from kiwrap import sandbox
    from kiwrap.backends.cli.runner import run_kicad_cli

    config = sandbox.ensure(install, storage_home=storage_home)
    for f in files:
        kind = "sch" if f.suffix == ".kicad_sch" else "pcb"
        run_kicad_cli(install, [kind, "upgrade", "--force", str(f)], config=config, accept=(0,))


def _demo(install: Any, name: str) -> Path:
    src = install.dir("demos") / name
    if not src.is_dir():
        pytest.skip(f"{name} demo missing")
    return src


@pytest.fixture(scope="module")
def pic_resaved(stable: Any, storage_home: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The review's e9c layout: pic_programmer with both sheets re-saved by 10.0.6, no .kicad_pro."""
    dst = tmp_path_factory.mktemp("tpl") / "pic_programmer"
    shutil.copytree(_demo(stable, "pic_programmer"), dst)
    _resave(stable, storage_home, dst / "pic_programmer.kicad_sch", dst / "pic_sockets.kicad_sch")
    (dst / "pic_programmer.kicad_pro").unlink()
    for prl in dst.glob("*.kicad_prl"):
        prl.unlink()
    assert (dst / "pic_programmer.kicad_sch").stat().st_size > 100_000
    return dst


@pytest.fixture
def pic(stable: Any, tmp_path: Path) -> Iterator[Path]:
    d = tmp_path / "pic_programmer"
    shutil.copytree(_demo(stable, "pic_programmer"), d)
    assert file_epoch(d / "pic_sockets.kicad_sch") == SCH_10_DEMO
    yield d
    _rmtree(d)


# ---- finding 1 ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("target", ["10.0", None], ids=["same-format", "default-newer"])
def test_create_never_overwrites_an_existing_design(
    target: str | None,
    pic_resaved: Path,
    stable: Any,
    nightly: Any,
    storage_home: Path,
    tmp_path: Path,
    user_config_untouched: None,
):
    d = tmp_path / "pic_programmer"
    shutil.copytree(pic_resaved, d)
    before = tree_digest(d)
    srv = Server(storage_home, [nightly, stable])
    args: dict[str, Any] = {"action": "create", "dir": str(d), "name": "pic_programmer"}
    if target:
        args["target_kicad"] = target
    err = srv.fail("kw_project", "E_BAD_ARGS", **args)
    assert "pic_programmer.kicad_sch" in err["candidates"] and "pic_programmer.kicad_pcb" in err["candidates"]
    assert tree_digest(d) == before  # the 243 KB schematic and the 665 KB board are untouched
    assert not list(d.glob("*.kicad_pro")) and not (d / ".kiwrap").exists()
    _rmtree(d)


# ---- finding 2 ---------------------------------------------------------------------------------------------


@pytest.fixture
def royalblue(stable: Any, tmp_path: Path) -> Iterator[Path]:
    d = tmp_path / "royalblue"
    shutil.copytree(
        _demo(stable, "royalblue54L_feather"), d, ignore=shutil.ignore_patterns("*.3dshapes", "*.STEP", "img")
    )
    assert sorted(p.name for p in (d / "sch").glob("*.kicad_sch")) == [
        "Connectors.kicad_sch",
        "Debugger.kicad_sch",
        "nPM1300.kicad_sch",
        "nRF54L15.kicad_sch",
    ]
    yield d
    _rmtree(d)


def test_upgrade_converts_the_sheets_under_sch(
    royalblue: Path, stable: Any, nightly: Any, storage_home: Path, user_config_untouched: None
):
    srv = Server(storage_home, [nightly, stable])
    st = srv.ok("kw_project", action="status", project=str(royalblue))["data"]
    stamps = {f["path"]: f["stamp"] for f in st["file_epochs"]}
    assert (
        stamps["sch/Connectors.kicad_sch"] == SCH_9 and len([p for p in stamps if p.startswith("sch/")]) == 4
    )
    sc = srv.ok("kw_project", action="upgrade", project=str(royalblue), to_kicad="10.0", confirm=True)
    up = sc["data"]["upgrade"]
    subs = sorted(p for p in up["replaced"] if p.startswith("sch/"))
    assert subs == [
        "sch/Connectors.kicad_sch",
        "sch/Debugger.kicad_sch",
        "sch/nPM1300.kicad_sch",
        "sch/nRF54L15.kicad_sch",
    ]
    formats = next(c for c in st["candidates"] if c["version"] == stable.version)["formats"]
    for sheet in [royalblue / "RoyalBlue54L-Feather.kicad_sch", *(royalblue / "sch").glob("*.kicad_sch")]:
        assert file_epoch(sheet) == formats["sch"], sheet
    assert file_epoch(royalblue / "RoyalBlue54L-Feather.kicad_pcb") == formats["pcb"]
    # the ERC/DRC copies carried the hierarchy, and the conversion kept the design (same ERC)
    before_copy = Path(up["work_dir"]).parent / "before" / "sch"
    assert len(list(before_copy.glob("*.kicad_sch"))) == 4
    checks = up["checks"]
    assert checks["before"]["erc"]["errors"] == checks["after"]["erc"]["errors"]
    st = srv.ok("kw_project", action="status", project=str(royalblue))["data"]
    assert st["epoch_match"] is True and st["target_effective"] == stable.version
    assert not any("mix" in w for w in st["warnings"])
    again = srv.ok("kw_project", action="upgrade", project=str(royalblue), to_kicad="10.0", confirm=True)
    assert again["data"]["upgraded"] is False
    # a sub-sheet write is no longer refused as a mixed-era project, and keeps the 10.0 format
    sc = srv.ok(
        "kw_sch_edit",
        project=str(royalblue),
        ops=[{"op": "property", "ref": "J1", "name": "Value", "value": "kiwrap-test"}],
        provider="file",
    )
    assert sc["provider_used"] == "file"
    assert file_epoch(royalblue / "sch" / "Connectors.kicad_sch") == formats["sch"]
    assert "kiwrap-test" in (royalblue / "sch" / "Connectors.kicad_sch").read_text(encoding="utf-8")


# ---- finding 5 ---------------------------------------------------------------------------------------------


def test_upgrade_with_a_read_only_board_leaves_nothing_behind(
    pic: Path, nightly: Any, storage_home: Path, user_config_untouched: None
):
    srv = Server(storage_home, [nightly])
    srv.ok("kw_project", action="status", project=str(pic))
    before = tree_digest(pic)
    board = pic / "pic_programmer.kicad_pcb"
    board.chmod(stat.S_IREAD)
    try:
        err = srv.fail("kw_project", "E_FILE_LOCKED", action="upgrade", project=str(pic), confirm=True)
    finally:
        board.chmod(stat.S_IREAD | stat.S_IWRITE)
    rollback = err["data"]["upgrade_rollback"]
    assert sorted(rollback["restored"]) == ["pic_programmer.kicad_sch", "pic_sockets.kicad_sch"]
    assert rollback["not_restored"] == []
    assert tree_digest(pic) == before
    assert file_epoch(pic / "pic_sockets.kicad_sch") == SCH_10_DEMO and file_epoch(board) == PCB_10
    assert not list(pic.rglob(f"*{REPLACE_TMP_SUFFIX}"))
    tracked = _git(pic, "ls-tree", "-r", "--name-only", "HEAD").stdout.split()
    assert not [t for t in tracked if t.endswith(REPLACE_TMP_SUFFIX)]
    # with the board writable again the upgrade goes through
    sc = srv.ok("kw_project", action="upgrade", project=str(pic), confirm=True)
    assert sorted(sc["data"]["upgrade"]["replaced"]) == [
        "pic_programmer.kicad_pcb",
        "pic_programmer.kicad_sch",
        "pic_sockets.kicad_sch",
    ]


# ---- finding 6 ---------------------------------------------------------------------------------------------


def test_mixed_project_prompt_names_files_not_kicad_11(
    pic: Path, stable: Any, nightly: Any, storage_home: Path, user_config_untouched: None
):
    _resave(nightly, storage_home, pic / "pic_programmer.kicad_sch")  # the root only, as in e7
    assert file_epoch(pic / "pic_sockets.kicad_sch") == SCH_10_DEMO
    before = tree_digest(pic)
    srv = Server(storage_home, [nightly, stable])
    edit = srv.fail(
        "kw_sch_edit",
        "E_FORMAT_UPGRADE_REQUIRED",
        project=str(pic),
        ops=[{"op": "property", "ref": "C1", "name": "Value", "value": "22nF"}],
        provider="file",
    )
    fp = srv.ok("kw_pcb_get", project=str(pic), kinds=["footprints"], refs=["C1"], provider="file")
    c1 = next(i for i in fp["data"]["items"] if i.get("ref") == "C1")
    place = srv.fail(
        "kw_pcb_place",
        "E_FORMAT_UPGRADE_REQUIRED",
        project=str(pic),
        placements=[{"ref": "C1", "at_mm": [c1["x_mm"], c1["y_mm"]]}],
        provider="file",
    )
    for err in (edit, place):
        for text in (err["message"], err["fix"]):
            assert "11.0" not in text, text
            assert "file format pcb" not in text and "format format" not in text, text
        assert "pic_sockets.kicad_sch (KiCad 10.0 format)" in err["message"]
        assert "pic_programmer.kicad_pcb (KiCad 10.0 format)" in err["message"]
        assert "install KiCad" not in err["fix"] and err["data"]["mixed"] is True
        assert "dry_run=true" in err["example"] and "confirm=true" not in err["example"]
    assert tree_digest(pic) == before
