"""Live kw_project_upgrade (docs/DECISIONS.md D24) on scratch copies of KiCad's 10.0 demos, through the
real MCP tool handlers with an explicit install list (the server's discovery is replaced by it):

- pic_programmer (two sheets stamped 20260101, a 10.0 board) with only the 10.99 nightly eligible:
  the write refuses with `data.upgrade_args`, status and the preview return the same arguments, the
  preview writes nothing (every project file hashed), a stale `kw_project(action='upgrade')` and a
  tampered file list are teaching E_BAD_ARGS, `kw_project_upgrade(**upgrade_args)` converts exactly
  those files with kicad-cli in the sandbox, pins the nightly, a later write succeeds and the
  snapshot taken first undoes it all;
- royalblue54L_feather (four sheets under `sch/`) with its board re-saved by 10.0.6 (older sheets,
  newer board) and 10.0.6 + the nightly installed: only the five sheets are listed, a version-prefix
  `to_kicad` is accepted, the sub-sheets are converted at their relative paths and the board is
  byte-identical;
- a read-only server does not publish the tool, answers a call to it with E_READONLY and still
  previews.

Every kicad-cli run goes through the session's sandbox roots; nothing starts a GUI or an api-server.
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
from mcp import Client
from mcp.types import CallToolResult

from kiwrap.core.server import build_context, build_server
from kiwrap.core.services import Services
from kiwrap.core.state.project import file_epoch

pytestmark = [pytest.mark.kicad, pytest.mark.kicad_version("10.0"), pytest.mark.kicad_version("10.99")]

SCH_9, PCB_9 = 20250114, 20241229  # royalblue54L_feather as shipped with 10.0
SCH_10_DEMO, PCB_10 = 20260101, 20260206  # pic_programmer as shipped with 10.0
CALL_KEYS = ("project", "from_kicad", "to_kicad", "files")


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


class Server:
    """The real tool handlers with discovery limited to `keep` (read-only when asked)."""

    def __init__(self, storage_home: Path, keep: list[Any], *, readonly_server: bool = False):
        svc = Services.real(storage_home)
        svc.discover = lambda **kw: list(keep)
        ctx = build_context(storage_home, svc)
        if readonly_server:
            ctx.readonly = True  # type: ignore[attr-defined]  # what `serve --readonly` sets
        self.server = build_server(context=ctx)

    def call(self, tool: str, **args: Any) -> tuple[bool, dict[str, Any]]:
        result = anyio.run(self.server.call_tool, tool, args)
        assert isinstance(result, CallToolResult), result  # never an InputRequiredResult here
        return bool(result.is_error), dict(result.structured_content or {})

    def ok(self, tool: str, **args: Any) -> dict[str, Any]:
        err, sc = self.call(tool, **args)
        assert not err, (tool, sc)
        return sc

    def fail(self, tool: str, code: str, **args: Any) -> dict[str, Any]:
        err, sc = self.call(tool, **args)
        assert err and sc.get("code") == code, (tool, sc)
        return sc

    def tool_names(self) -> list[str]:
        async def go() -> list[str]:
            async with Client(self.server) as client:
                return [t.name for t in (await client.list_tools()).tools]

        return anyio.run(go)


def _rmtree(path: Path) -> None:
    if not path.exists():
        return
    for p in path.rglob("*"):
        if p.is_file():
            p.chmod(stat.S_IREAD | stat.S_IWRITE)
    shutil.rmtree(path)


def tree(d: Path, *, kiwrap: bool = False) -> dict[str, str]:
    """sha256 of every file of the project (`.kiwrap/**` only when asked)."""
    return {
        p.relative_to(d).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(d.rglob("*"))
        if p.is_file() and (kiwrap or ".kiwrap" not in p.relative_to(d).parts)
    }


def _demo(install: Any, name: str) -> Path:
    src = install.dir("demos") / name
    if not src.is_dir():
        pytest.skip(f"{name} demo missing")
    return src


@pytest.fixture
def pic(stable: Any, tmp_path: Path) -> Iterator[Path]:
    d = tmp_path / "pic_programmer"
    shutil.copytree(_demo(stable, "pic_programmer"), d)
    (d / "pic_programmer.kicad_prl").write_text(
        '{\n  "board": {"active_layer": 0},\n  "meta": {"filename": "pic_programmer.kicad_prl", "version": 5}\n}\n',
        encoding="utf-8",
    )
    assert (
        file_epoch(d / "pic_sockets.kicad_sch") == SCH_10_DEMO
        and file_epoch(d / "pic_programmer.kicad_pcb") == PCB_10
    )
    yield d
    _rmtree(d)


@pytest.fixture
def royalblue_mixed(stable: Any, storage_home: Path, tmp_path: Path) -> Iterator[Path]:
    """royalblue54L_feather with its board re-saved by 10.0.6 (`pcb upgrade` on the scratch copy):
    9.0 sheets (four under sch/) next to a 10.0 board - kit-dev-coldfire's shape, with sub-sheets."""
    from kiwrap import sandbox
    from kiwrap.backends.cli.runner import run_kicad_cli

    d = tmp_path / "royalblue"
    shutil.copytree(
        _demo(stable, "royalblue54L_feather"), d, ignore=shutil.ignore_patterns("*.3dshapes", "*.STEP", "img")
    )
    board = d / "RoyalBlue54L-Feather.kicad_pcb"
    config = sandbox.ensure(stable, storage_home=storage_home)
    run_kicad_cli(stable, ["pcb", "upgrade", "--force", str(board)], config=config, accept=(0,))
    assert file_epoch(board) == PCB_10 and file_epoch(d / "sch" / "Connectors.kicad_sch") == SCH_9
    yield d
    _rmtree(d)


def _formats(status: dict[str, Any], install: Any) -> dict[str, int]:
    return next(c for c in status["candidates"] if c["version"] == install.version)["formats"]


# ---- pic_programmer with only the nightly ---------------------------------------------------------------


def test_pic_programmer_round_trip_with_only_the_nightly(
    pic: Path, nightly: Any, storage_home: Path, user_config_untouched: None
):
    srv = Server(storage_home, [nightly])
    original = tree(pic)
    # the write refuses and carries the arguments
    err = srv.fail("kw_pcb_drc", "E_FORMAT_UPGRADE_REQUIRED", project=str(pic), save_board=True)
    ua = err["data"]["upgrade_args"]
    assert ua["from_kicad"] == "10.0" and ua["to_kicad"] == nightly.version
    assert ua["files"] == ["pic_programmer.kicad_pcb", "pic_programmer.kicad_sch", "pic_sockets.kicad_sch"]
    assert err["example"].startswith("kw_project_upgrade(")
    assert all("confirm" not in err[k] for k in ("message", "fix", "example"))
    st = srv.ok("kw_project", action="status", project=str(pic))["data"]
    assert st["upgrade_args"] == ua and "winget install" in (st["install_hint"] or "")
    formats = _formats(st, nightly)
    assert ua["to_format"] == {"sch": formats["sch"], "pcb": formats["pcb"]}
    # the preview converts a copy and writes nothing in the project
    before = tree(pic, kiwrap=True)
    pre = srv.ok("kw_project", action="upgrade_preview", project=str(pic))
    assert tree(pic, kiwrap=True) == before
    assert pre["data"]["upgrade_args"] == ua and pre["next_suggested"] == "kw_project_upgrade"
    checks = pre["data"]["checks"]
    assert checks["before"]["erc"]["errors"] == checks["after"]["erc"]["errors"]
    assert {f["path"]: f["stamp_after"] for f in pre["data"]["files"]} == {
        "pic_programmer.kicad_pcb": formats["pcb"],
        "pic_programmer.kicad_sch": formats["sch"],
        "pic_sockets.kicad_sch": formats["sch"],
    }
    # the retired action and a tampered list are teaching errors; nothing changes
    old = srv.fail("kw_project", "E_BAD_ARGS", action="upgrade", project=str(pic), confirm=True)
    assert "kw_project_upgrade" in old["candidates"]
    tampered = {**{k: ua[k] for k in CALL_KEYS}, "files": ua["files"][:2]}
    bad = srv.fail("kw_project_upgrade", "E_BAD_ARGS", **tampered)
    assert bad["data"]["upgrade_args"] == ua and bad["data"]["mismatch"]["files"]["missing"] == [
        "pic_sockets.kicad_sch"
    ]
    assert {k: v for k, v in tree(pic).items()} == original
    # the conversion: exactly the listed files, pinned, undoable
    sc = srv.ok("kw_project_upgrade", **{k: ua[k] for k in CALL_KEYS})
    data = sc["data"]
    assert sorted(data["replaced"]) == ua["files"]
    after = tree(pic)
    assert sorted(k for k in original if original[k] != after[k]) == ua["files"]
    for kept in ("pic_programmer.kicad_pro", "pic_programmer.kicad_prl", "pic_programmer.kicad_sym"):
        assert after[kept] == original[kept] and kept in data["not_converted"], kept
    assert file_epoch(pic / "pic_sockets.kicad_sch") == formats["sch"]
    assert file_epoch(pic / "pic_programmer.kicad_pcb") == formats["pcb"]
    assert data["target"] == {"kicad": nightly.version, "source": "pinned", "epoch_match": True}
    assert data["consent"]["via"] == "in_process" and {v["name"] for v in sc["validators"]} == {"erc", "drc"}
    assert data["checks"]["before"]["drc"]["errors"] == data["checks"]["after"]["drc"]["errors"]
    assert any(w.startswith("KiCad 10.0 can no longer open") for w in sc["warnings"])
    st = srv.ok("kw_project", action="status", project=str(pic))["data"]
    assert st["target_source"] == "pinned" and st["epoch_match"] is True and st["upgrade_args"] is None
    # a later write succeeds and keeps the new format
    edit = srv.ok(
        "kw_sch_edit",
        project=str(pic),
        ops=[{"op": "property", "ref": "C1", "name": "Value", "value": "22nF"}],
        provider="file",
    )
    assert edit["provider_used"] == "file" and file_epoch(pic / "pic_programmer.kicad_sch") == formats["sch"]
    # the snapshot taken first brings the 10.0 files back (a restore that lowers the stamps passes the guard)
    srv.ok("kw_snapshot", project=str(pic), action="restore", ref=data["snapshot_before"])
    restored = tree(pic)
    for name in ua["files"]:
        assert restored[name] == original[name], name
    assert file_epoch(pic / "pic_sockets.kicad_sch") == SCH_10_DEMO
    # ...but restoring the converted state again is refused and rolled back: only kw_project_upgrade
    # converts, with the user's approval (review finding 1)
    redo = srv.fail(
        "kw_snapshot", "E_FORMAT_UPGRADE_REQUIRED", project=str(pic), action="restore", ref=edit["snapshot"]
    )
    assert "nothing was restored" in redo["message"] and tree(pic) == restored
    assert redo["data"]["upgrade_args"]["files"] == ua["files"]
    assert file_epoch(pic / "pic_programmer.kicad_pcb") == PCB_10


def test_readonly_server_does_not_publish_the_upgrade(
    pic: Path, nightly: Any, storage_home: Path, user_config_untouched: None
):
    srv = Server(storage_home, [nightly], readonly_server=True)
    names = srv.tool_names()
    assert "kw_project" in names and "kw_project_upgrade" not in names
    before = tree(pic, kiwrap=True)
    sc = srv.fail(
        "kw_project_upgrade",
        "E_READONLY",
        project=str(pic),
        from_kicad="10.0",
        to_kicad=nightly.version,
        files=["pic_programmer.kicad_pcb", "pic_programmer.kicad_sch", "pic_sockets.kicad_sch"],
    )
    assert sc["candidates"][:2] == ["kw_project(action='upgrade_preview')", "kw_project(action='status')"]
    pre = srv.ok("kw_project", action="upgrade_preview", project=str(pic))
    assert pre["data"]["upgrade_args"]["files"] and pre["data"]["checks"]["after"]
    assert pre["data"]["readonly"]["mode"] == "server"
    assert tree(pic, kiwrap=True) == before and not (pic / ".kiwrap").exists()


# ---- royalblue54L_feather: sub-sheets, older sheets next to a newer board ------------------------------------


def test_royalblue_converts_only_the_sheets_at_their_relative_paths(
    royalblue_mixed: Path, stable: Any, nightly: Any, storage_home: Path, user_config_untouched: None
):
    d = royalblue_mixed
    srv = Server(storage_home, [nightly, stable])
    original = tree(d)
    op = [{"op": "property", "ref": "J1", "name": "Value", "value": "kiwrap-test"}]
    err = srv.fail("kw_sch_edit", "E_FORMAT_UPGRADE_REQUIRED", project=str(d), ops=op, provider="file")
    ua = err["data"]["upgrade_args"]
    assert ua["to_kicad"] == stable.version and ua["from_kicad"] == f"format sch={SCH_9},pcb={PCB_10}"
    assert ua["files"] == [
        "RoyalBlue54L-Feather.kicad_sch",
        "sch/Connectors.kicad_sch",
        "sch/Debugger.kicad_sch",
        "sch/nPM1300.kicad_sch",
        "sch/nRF54L15.kicad_sch",
    ]
    assert srv.ok("kw_project", action="status", project=str(d))["data"]["upgrade_args"] == ua
    sc = srv.ok("kw_project_upgrade", **{**{k: ua[k] for k in CALL_KEYS}, "to_kicad": "10.0"})
    data = sc["data"]
    assert sorted(data["replaced"]) == ua["files"]
    after = tree(d)
    assert sorted(k for k in original if original[k] != after[k]) == ua["files"]
    assert after["RoyalBlue54L-Feather.kicad_pcb"] == original["RoyalBlue54L-Feather.kicad_pcb"]
    assert "RoyalBlue54L-Feather.kicad_pcb" in data["not_converted"]
    formats = _formats(srv.ok("kw_project", action="status", project=str(d))["data"], stable)
    for sheet in ua["files"]:
        assert file_epoch(d / sheet) == formats["sch"], sheet
    checks = data["checks"]
    assert checks["before"]["erc"]["errors"] == checks["after"]["erc"]["errors"]
    st = srv.ok("kw_project", action="status", project=str(d))["data"]
    assert (
        st["epoch_match"] is True and st["target_effective"] == stable.version and st["upgrade_args"] is None
    )
    # the schematic write that was refused now proceeds, in the 10.0 format, in the sub-sheet
    sc = srv.ok("kw_sch_edit", project=str(d), ops=op, provider="file")
    assert file_epoch(d / "sch" / "Connectors.kicad_sch") == formats["sch"]
    assert "kiwrap-test" in (d / "sch" / "Connectors.kicad_sch").read_text(encoding="utf-8")
