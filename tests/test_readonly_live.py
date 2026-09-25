"""Live read-only sessions (docs/DECISIONS.md D23, requests/kiwrap-feature-requests.review.md item 2).

The review diffed a reviewed project before and after kiwrap calls and found a snapshot, `.kiwrap/`
and `<board>.drc.json` in it (§5 item 2), a `<root>.net` left by a READ_ONLY tool (§3) and a
rewritten `.kicad_prl` (B2). Here the real tool handlers run against scratch copies of the KiCad
10.0 demos on stock 10.0.6 and on the 10.99 nightly (explicit install filters: the server's
discovery is limited to one install), read-only - server-wide (`ToolContext.readonly`, what
`kiwrap serve --readonly` sets) and per call (`persist=false`) - and every file of the project
directory, `.kicad_prl` and `.kiwrap/**` included, is hashed before and after: nothing may change,
appear or disappear. Mutating tools must refuse with E_READONLY. No GUI is started; an api-server a
read pooled is released by the call itself (checked: no `~*.lck` remains).
"""

from __future__ import annotations

import contextlib
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
from kiwrap.core.state import readonly
from kiwrap.core.state.project import file_epoch

pytestmark = [pytest.mark.kicad, pytest.mark.kicad_version("10.0"), pytest.mark.kicad_version("10.99")]

PCB_10 = 20260206
VERSIONS = ["10.0", "10.99"]


def _stock(installs: list[Any], major_minor: str) -> Any:
    found = [i for i in installs if i.major_minor == major_minor and not i.is_bench_build]
    if not found:
        pytest.skip(f"no stock KiCad {major_minor}")
    return found[0]


class Server:
    """The real tool handlers with discovery limited to one install."""

    def __init__(self, storage_home: Path, install: Any, *, readonly_server: bool):
        self.svc = Services.real(storage_home)
        self.svc.discover = lambda **kw: [install]
        self.ctx = build_context(storage_home, self.svc)
        self.ctx.readonly = readonly_server  # type: ignore[attr-defined]
        self.server = build_server(context=self.ctx)

    def call(self, tool: str, **args: Any) -> tuple[bool, dict[str, Any]]:
        result = anyio.run(self.server.call_tool, tool, args)
        return bool(result.is_error), dict(result.structured_content or {})

    def ok(self, tool: str, **args: Any) -> dict[str, Any]:
        err, sc = self.call(tool, **args)
        assert not err, (tool, sc)
        return sc

    def fail(self, tool: str, code: str, **args: Any) -> dict[str, Any]:
        err, sc = self.call(tool, **args)
        assert err and sc.get("code") == code, (tool, sc)
        return sc

    def release(self, project: Path) -> None:
        release = getattr(self.svc, "ipc_release_project", None)
        if callable(release):
            with contextlib.suppress(Exception):  # no IPC backend: nothing pooled
                release(project)


def tree(d: Path) -> dict[str, str]:
    """sha256 of every file under the project directory (`.kicad_prl`, `.kiwrap/**` included)."""
    return {
        p.relative_to(d).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(d.rglob("*"))
        if p.is_file()
    }


def _rmtree(path: Path) -> None:
    """`shutil.rmtree` that also removes read-only files on Python 3.11 (`rmtree(onexc=)` is 3.12+,
    and CI runs 3.11): clear the read-only bit first, then remove."""
    if not path.exists():
        return
    for p in path.rglob("*"):
        with contextlib.suppress(OSError):
            p.chmod(stat.S_IREAD | stat.S_IWRITE)
    shutil.rmtree(path)


def _with_prl(d: Path, name: str) -> None:
    (d / f"{name}.kicad_prl").write_text(
        '{\n  "board": {"active_layer": 0, "visible_layers": "ffffffff_ffffffff"},\n'
        f'  "meta": {{"filename": "{name}.kicad_prl", "version": 5}}\n}}\n',
        encoding="utf-8",
    )


@pytest.fixture(scope="module")
def i10(installs: list[Any]) -> Any:
    return _stock(installs, "10.0")


@pytest.fixture(scope="module")
def pic_template(i10: Any, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The 10.0 pic_programmer demo (two sheets + board) with a user `.kicad_prl`."""
    src = i10.dir("demos") / "pic_programmer"
    if not src.is_dir():
        pytest.skip("pic_programmer demo missing")
    dst = tmp_path_factory.mktemp("ro_tpl") / "pic_programmer"
    shutil.copytree(src, dst)
    _with_prl(dst, "pic_programmer")
    assert file_epoch(dst / "pic_programmer.kicad_pcb") == PCB_10
    return dst


@pytest.fixture(scope="module")
def coldfire_template(i10: Any, storage_home: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """kit-dev-coldfire with its board re-saved by 10.0.6 `pcb upgrade` (the review's item 1
    board, `version 20260206`, copper zones) and a user `.kicad_prl`."""
    from kiwrap import sandbox
    from kiwrap.backends.cli.runner import run_kicad_cli

    src = i10.dir("demos") / "kit-dev-coldfire-xilinx_5213"
    if not src.is_dir():
        pytest.skip("kit-dev-coldfire demo missing")
    dst = tmp_path_factory.mktemp("ro_tpl") / "coldfire"
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("*.3dshapes"))
    board = dst / "kit-dev-coldfire-xilinx_5213.kicad_pcb"
    config = sandbox.ensure(i10, storage_home=storage_home)
    run_kicad_cli(i10, ["pcb", "upgrade", "--force", str(board)], config=config, accept=(0,))
    assert file_epoch(board) == PCB_10
    _with_prl(dst, "kit-dev-coldfire-xilinx_5213")
    return dst


@pytest.fixture
def copy_of(tmp_path: Path) -> Iterator[Any]:
    made: list[Path] = []

    def make(template: Path) -> Path:
        d = tmp_path / template.name
        shutil.copytree(template, d)
        made.append(d)
        return d

    yield make
    for d in made:
        _rmtree(d)
    readonly.forget_state()


def _session(s: Server, proj: Path, out_dir: Path, **extra: Any) -> dict[str, dict[str, Any]]:
    """The review's reviewer session: every read, check and export a third party would run."""
    p = str(proj)
    got: dict[str, dict[str, Any]] = {}
    got["kw_env"] = s.ok("kw_env", **extra)
    got["kw_project"] = s.ok("kw_project", action="status", project=p, **extra)
    got["kw_sch_get"] = s.ok("kw_sch_get", project=p, kinds=["symbols", "netlist"], **extra)
    got["kw_sch_stats"] = s.ok("kw_sch_stats", project=p, **extra)
    got["kw_sch_check"] = s.ok("kw_sch_check", project=p, **extra)
    got["kw_pcb_get"] = s.ok("kw_pcb_get", project=p, kinds=["footprints", "nets"], **extra)
    got["kw_pcb_check"] = s.ok("kw_pcb_check", project=p, **extra)
    got["kw_pcb_drc"] = s.ok("kw_pcb_drc", project=p, **extra)
    got["kw_pcb_render"] = s.ok("kw_pcb_render", project=p, view="2d", **extra)
    got["kw_fab_export"] = s.ok(
        "kw_fab_export",
        project=p,
        targets=["gerbers", "drill", "pos"],
        out_dir=str(out_dir),
        force={"reason": "read-only review"},
        **extra,
    )
    got["kw_fab_bom"] = s.ok("kw_fab_bom", project=p, write=True, **extra)
    return got


def _check_receipts(got: dict[str, dict[str, Any]], mode: str, out_dir: Path, storage: Path) -> None:
    for tool, sc in got.items():
        assert sc["snapshot"] is None, tool
        assert sc["data"]["readonly"]["mode"] == mode, tool
    assert got["kw_pcb_drc"]["data"]["options"]["save_board"] is False
    files = [f for lst in got["kw_fab_export"]["data"]["files"].values() for f in lst]
    assert files and all(Path(f).is_file() and out_dir.resolve() in Path(f).resolve().parents for f in files)
    bom = Path(got["kw_fab_bom"]["data"]["file"])
    assert bom.is_file() and storage.resolve() in bom.resolve().parents


@pytest.mark.parametrize("version", VERSIONS)
def test_readonly_server_session_leaves_a_third_party_project_byte_identical(
    version: str, installs: list[Any], pic_template: Path, copy_of: Any, tmp_path: Path
):
    install = _stock(installs, version)
    proj = copy_of(pic_template)
    before = tree(proj)
    out_dir = tmp_path / "review-out"
    out_dir.mkdir()
    s = Server(tmp_path / "home", install, readonly_server=True)
    try:
        got = _session(s, proj, out_dir)
        _check_receipts(got, "server", out_dir, tmp_path / "home")
        # every tool that would write the project refuses with a teaching error
        for tool, args in (
            ("kw_sch_annotate", {}),
            ("kw_pcb_drc", {"save_board": True}),
            (
                "kw_project_upgrade",
                {"from_kicad": "10.0", "to_kicad": install.version, "files": ["pic_programmer.kicad_pcb"]},
            ),
            ("kw_project", {"action": "retarget", "target_kicad": install.version}),
            ("kw_snapshot", {"action": "create"}),
            ("kw_pcb_sync", {}),
        ):
            sc = s.fail(tool, "E_READONLY", project=str(proj), **args)
            assert sc["data"]["mode"] == "server" and "writable kiwrap" in sc["fix"]
    finally:
        s.release(proj)
    assert tree(proj) == before
    assert not (proj / ".kiwrap").exists() and not list(proj.glob("~*.lck"))


@pytest.mark.parametrize("version", VERSIONS)
def test_persist_false_calls_leave_a_kiwrap_project_byte_identical(
    version: str, installs: list[Any], i10: Any, pic_template: Path, copy_of: Any, tmp_path: Path
):
    """A project with kiwrap state and snapshots (a writable DRC first, on 10.0.6): per-call
    `persist=false` on a writable server touches neither the design files nor `.kiwrap/**`."""
    install = _stock(installs, version)
    proj = copy_of(pic_template)
    home = tmp_path / "home"
    writable = Server(home, i10, readonly_server=False)
    writable.ok("kw_pcb_drc", project=str(proj))
    assert (proj / ".kiwrap" / "state.json").is_file()
    before = tree(proj)
    out_dir = tmp_path / "review-out"
    out_dir.mkdir()
    s = Server(home, install, readonly_server=False)
    try:
        got = _session(s, proj, out_dir, persist=False)
        _check_receipts(got, "call", out_dir, home)
        sc = s.fail("kw_sch_annotate", "E_READONLY", project=str(proj), persist=False)
        assert sc["data"]["mode"] == "call" and "without persist=false" in sc["fix"]
    finally:
        s.release(proj)
        writable.release(proj)
    assert tree(proj) == before
    assert not list(proj.glob("~*.lck"))


@pytest.mark.parametrize("version", VERSIONS)
def test_readonly_drc_refills_the_coldfire_board_in_memory(
    version: str, installs: list[Any], coldfire_template: Path, copy_of: Any, tmp_path: Path
):
    """The review's item 1 board (copper zones): a read-only kw_pcb_drc refills in memory on
    either KiCad and leaves board, `.kicad_prl` and directory untouched (no `<board>.drc.json`)."""
    install = _stock(installs, version)
    proj = copy_of(coldfire_template)
    before = tree(proj)
    s = Server(tmp_path / "home", install, readonly_server=True)
    try:
        sc = s.ok("kw_pcb_drc", project=str(proj))
        assert sc["data"]["options"] == {**sc["data"]["options"], "refill_zones": True, "save_board": False}
        assert sc["data"]["readonly"]["mode"] == "server" and sc["snapshot"] is None
        assert sc["provider_used"].endswith(install.version)
    finally:
        s.release(proj)
    assert tree(proj) == before
    assert file_epoch(proj / "kit-dev-coldfire-xilinx_5213.kicad_pcb") == PCB_10
