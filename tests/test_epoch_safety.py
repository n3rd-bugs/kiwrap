"""D21 / D22 / B2 with fakes (no KiCad): which KiCad a project runs on, the consent before an
up-conversion (D24: kw_project_upgrade and the frozen elicitation shortcut), the stamp guard, the
ipc11 write precondition, the writers' epochs, kw_project status / retarget / upgrade_preview and
kw_pcb_drc's no-save default. tests/test_project_upgrade.py covers kw_project_upgrade itself.

requests/kiwrap-feature-requests.review.md item 1 (B1) reproduced four paths that re-stamped a
KiCad 10.0 project as 10.99 on default arguments; every one is covered here against fakes and
again live in tests/test_epoch_safety_live.py.
"""

from __future__ import annotations

import hashlib
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import Client
from mcp.types import ElicitResult

from kiwrap.backends.cli.upgrade import ProjectCopy, UpgradedFile, copy_project, relative_design_path
from kiwrap.core import planner
from kiwrap.core.state import KiwrapError
from kiwrap.core.state.jobs import JobQueue
from kiwrap.core.state.project import Project, ProjectState, file_epoch
from kiwrap.core.tools import templates
from kiwrap.core.tools._author import epoch_for
from kiwrap.core.tools._base import (
    SOURCE_CREATED,
    SOURCE_FILE_EPOCH,
    SOURCE_NEWER_READONLY,
    SOURCE_PINNED,
    ToolContext,
    consent_message,
    effective_target,
    mutation,
    resolve_target,
)
from tests.test_core_fakes import CAPS_10, FakeBackend, FakeCaps, FakeInstall, drc_report
from tests.test_core_tools import Harness

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH (snapshots)")

# ---- the fake machine ---------------------------------------------------------------------------------

SCH_10, PCB_10 = 20260306, 20260206  # what KiCad 10.0.x writes
SCH_10_DEMO = 20260101  # the 10.0 demos' schematics (a 10.0 pre-release stamp, same generation)
SCH_99, PCB_99 = 20260830, 20260901  # the 10.99 nightly
SCH_9, PCB_9 = 20250114, 20241229

CAPS_1099 = {**CAPS_10, "formats.sch": SCH_99, "formats.pcb": PCB_99, "formats.pro": 4, "ipc.headless": True}
CAPS_909 = {**CAPS_10, "formats.sch": SCH_9, "formats.pcb": PCB_9, "cli.has_sch_pcb_upgrade": False}

I1099 = FakeInstall(version="10.99.0", root=Path("C:/fake/KiCad/10.99"))
I1006 = FakeInstall(version="10.0.6", root=Path("C:/fake/KiCad/10.0"))
I909 = FakeInstall(version="9.0.9", root=Path("C:/fake/KiCad/9.0"))
CAPS = {"10.99.0": CAPS_1099, "10.0.6": CAPS_10, "9.0.9": CAPS_909}

#: what `backends.cli.upgrade.format_releases()` answers (the matrix as data)
RELEASES: list[dict[str, Any]] = [
    {
        "version": "10.99.0",
        "line": "10.99",
        "formats": {"sch": SCH_99, "pcb": PCB_99},
        "nightly": True,
        "released": False,
        "install_hint": None,
    },
    {
        "version": "10.0.6",
        "line": "10.0",
        "formats": {"sch": SCH_10, "pcb": PCB_10},
        "nightly": False,
        "released": True,
        "install_hint": "winget install KiCad.KiCad --version 10.0.6",
    },
    {
        "version": "9.0.9",
        "line": "9.0",
        "formats": {"sch": SCH_9, "pcb": PCB_9},
        "nightly": False,
        "released": True,
        "install_hint": "winget install KiCad.KiCad --version 9.0.9",
    },
    {
        "version": "8.0.9",
        "line": "8.0",
        "formats": {"sch": 20231120, "pcb": 20240108},
        "nightly": False,
        "released": True,
        "install_hint": "winget install KiCad.KiCad --version 8.0.9",
    },
]


@dataclass
class SafetyBackend(FakeBackend):
    """FakeBackend plus the D21 services: a fake `sch/pcb upgrade` (rewrites the stamps of a
    copy), the release table, and a DRC that can misbehave like a newer kicad-cli
    (`upconvert_on_save`: `--save-board` re-stamps the board)."""

    upconvert_on_save: int | None = None

    def run_drc(self, install: Any, config: Any, board: Path, **kw: Any) -> Any:
        report = super().run_drc(install, config, board, **kw)
        if kw.get("save_board") and self.upconvert_on_save:
            text = Path(board).read_text(encoding="utf-8")
            Path(board).write_text(
                re.sub(r"\(version \d+\)", f"(version {self.upconvert_on_save})", text, count=1),
                encoding="utf-8",
            )
        return report

    def upgrade_project(
        self,
        install: Any,
        config: Any,
        project_dir: Path,
        files: list[Path],
        work_dir: Path,
        *,
        caps: Any = None,
        convert: list[Path] | None = None,
    ) -> ProjectCopy:
        """`backends.cli.upgrade.upgrade_project_copy` in miniature: copy every design file, re-stamp
        only `convert` (D24: exactly the listed files)."""
        targets = list(files) if convert is None else [Path(c) for c in convert]
        self._log(
            "upgrade_project",
            version=install.version,
            files=[Path(f).name for f in files],
            convert=[relative_design_path(project_dir, Path(f)).as_posix() for f in targets],
        )
        values = self.caps.get(install.version, CAPS_10)
        work = copy_project(project_dir, files, work_dir)
        out = ProjectCopy(work_dir=work)
        for src in targets:
            src = Path(src)
            kind = "sch" if src.suffix == ".kicad_sch" else "pcb"
            rel = relative_design_path(project_dir, src)  # sub-sheets keep their subdirectory
            dst = work / rel
            before = file_epoch(dst)
            new = int(values[f"formats.{kind}"])
            text = dst.read_text(encoding="utf-8")
            dst.write_text(re.sub(r"\(version \d+\)", f"(version {new})", text, count=1), encoding="utf-8")
            out.files.append(
                UpgradedFile(src, dst, kind, before, new, changed=before != new, rel=rel.as_posix())
            )
        return out

    def find(self, spec: str | None, installs: list[FakeInstall] | None = None) -> FakeInstall:
        """The fake find plus install roots (kw_env lists them; D24 accepts one as `to_kicad`)."""
        pool = installs if installs is not None else self.installs
        for i in pool:
            if spec and Path(spec) == Path(i.root):
                return i
        return super().find(spec, installs)

    def services(self, storage_home: Path):  # type: ignore[no-untyped-def]
        svc = super().services(storage_home)
        svc.upgrade_project = self.upgrade_project
        svc.copy_project = copy_project
        svc.format_releases = lambda: list(RELEASES)
        return svc


def backend(*installs: FakeInstall, **knobs: Any) -> SafetyBackend:
    return SafetyBackend(installs=list(installs), caps=dict(CAPS), **knobs)


def write_project(
    directory: Path, *, sch: int | None = SCH_10_DEMO, pcb: int | None = PCB_10, child: int | None = None
) -> Path:
    """A KiCad project written by hand with the given stamps (a child sheet when `child`)."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "demo.kicad_pro").write_text("{}\n", encoding="utf-8")
    if sch is not None:
        (directory / "demo.kicad_sch").write_text(
            templates.schematic_text(epoch=sch, generator_version="10.0"), encoding="utf-8"
        )
    if child is not None:
        (directory / "child.kicad_sch").write_text(
            templates.schematic_text(epoch=child, generator_version="10.0"), encoding="utf-8"
        )
    if pcb is not None:
        (directory / "demo.kicad_pcb").write_text(
            templates.board_text(epoch=pcb, generator_version="10.0", layer_ids_v2=True, outline_mm=(50, 30)),
            encoding="utf-8",
        )
    return directory


def ctx_of(tmp_path: Path, be: SafetyBackend) -> ToolContext:
    home = tmp_path / "home"
    return ToolContext(services=be.services(home), jobs=JobQueue(home / "jobs"), server_version="test")


def digest(d: Path) -> dict[str, str]:
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(d.glob("*.kicad_*"))}


# ---- resolve_target / effective_target --------------------------------------------------------------------


def test_file_epoch_picks_the_install_writing_the_files_format(tmp_path: Path):
    """Review item 1 fix layer 1: not pinned -> the newest install whose format generation equals
    the files' (10.0.6 for a 10.0 demo whose schematic says 20260101), never simply the newest."""
    ctx = ctx_of(tmp_path, backend(I1099, I1006, I909))
    project = Project.from_path(write_project(tmp_path / "p"))
    choice = effective_target(ctx, project, ProjectState(), for_write=True)
    assert choice.version == "10.0.6" and choice.source == SOURCE_FILE_EPOCH and choice.writable
    assert choice.project_epoch == {"sch": SCH_10_DEMO, "pcb": PCB_10}
    assert choice.install_epoch == {"sch": SCH_10, "pcb": PCB_10} and choice.project_kicad == "10.0"
    assert {c["version"]: c["matches"] for c in choice.candidates} == {
        "10.99.0": False,
        "10.0.6": True,
        "9.0.9": False,
    }


def test_only_a_newer_install_reads_but_a_write_needs_consent(tmp_path: Path):
    ctx = ctx_of(tmp_path, backend(I1099))
    project = Project.from_path(write_project(tmp_path / "p"))
    choice = effective_target(ctx, project, ProjectState(), for_write=False)
    assert choice.version == "10.99.0" and choice.source == SOURCE_NEWER_READONLY
    assert choice.readable and not choice.writable and "D21" in " ".join(choice.notes)
    with pytest.raises(KiwrapError) as exc:
        effective_target(ctx, project, ProjectState(), for_write=True)
    err = exc.value
    # D24: `example` is a placeholder kw_project_upgrade call; the retired consent flag is gone
    assert err.code == "E_FORMAT_UPGRADE_REQUIRED"
    assert err.example is not None and err.example.startswith("kw_project_upgrade(")
    assert err.fix is not None
    for text in (err.message, err.fix, err.example):
        assert "confirm" not in text, text
    assert "upgrade_preview" in err.fix and "kw_project_upgrade with data.upgrade_args" in err.fix
    assert err.data["upgrade_args"] == {
        "project": project.pro_path.as_posix(),
        "from_kicad": "10.0",
        "to_kicad": "10.99.0",
        "to_format": {"sch": SCH_99, "pcb": PCB_99},
        "files": ["demo.kicad_pcb", "demo.kicad_sch"],
    }
    d = err.data
    assert d["upgrade_to"] == "10.99.0" and d["project_kicad"] == "10.0"
    assert d["install_hint"] == "winget install KiCad.KiCad --version 10.0.6"
    assert d["project_epoch"] == {"sch": SCH_10_DEMO, "pcb": PCB_10}
    assert {f["path"] for f in d["files"]} == {"demo.kicad_sch", "demo.kicad_pcb"}
    assert d["candidates"] == [{"version": "10.99.0", "formats": {"sch": SCH_99, "pcb": PCB_99}}]
    assert "kw_project" in err.fix and "install KiCad 10.0" in err.fix


def test_files_newer_than_every_install_are_E_FORMAT_NEWER(tmp_path: Path):
    ctx = ctx_of(tmp_path, backend(I1006))
    project = Project.from_path(write_project(tmp_path / "p", sch=SCH_99, pcb=PCB_99))
    for kw in ({"for_write": True}, {"for_write": False}):
        with pytest.raises(KiwrapError) as exc:
            effective_target(ctx, project, ProjectState(), **kw)
        assert exc.value.code == "E_FORMAT_NEWER" and exc.value.data["project_epoch"] == {
            "sch": SCH_99,
            "pcb": PCB_99,
        }
    # a read the file provider can serve still gets an install (and knows it cannot load the files)
    choice = effective_target(ctx, project, ProjectState(), for_write=False, allow_newer=True)
    assert choice.version == "10.0.6" and not choice.readable and not choice.writable


def test_a_pin_is_honoured_and_a_pin_newer_than_the_files_asks_first(tmp_path: Path):
    ctx = ctx_of(tmp_path, backend(I1099, I1006))
    project = Project.from_path(write_project(tmp_path / "p"))
    pinned = ProjectState(target_kicad="10.99.0", target_source="pinned")
    choice = effective_target(ctx, project, pinned, for_write=False)
    assert choice.version == "10.99.0" and choice.source == SOURCE_PINNED and not choice.writable
    with pytest.raises(KiwrapError) as exc:
        effective_target(ctx, project, pinned, for_write=True)
    # a KiCad writing the files' own format is installed: the fix names the retarget
    assert exc.value.candidates == ["10.0.6"] and "retarget" in exc.value.fix
    # a legacy / extension-written pin without a source counts as pinned
    legacy = ProjectState.from_dict({"target_kicad": "10.0.6"})
    assert legacy.target_source is None
    assert resolve_target(ctx, project, legacy).source == SOURCE_PINNED


def test_an_uninstalled_pin_falls_back_to_the_files_format(tmp_path: Path):
    """Created with 10.0.5, which a point-release update replaced by 10.0.6: the format decides."""
    ctx = ctx_of(tmp_path, backend(I1099, I1006))
    project = Project.from_path(write_project(tmp_path / "p", sch=SCH_10, pcb=PCB_10))
    state = ProjectState(target_kicad="10.0.5", target_source="created")
    choice = effective_target(ctx, project, state, for_write=True)
    assert choice.version == "10.0.6" and choice.writable and "10.0.5 is not installed" in choice.notes[0]
    with pytest.raises(KiwrapError) as exc:  # an explicit per-call choice is never swapped
        resolve_target(ctx, project, state, prefer="10.0.5")
    assert exc.value.code == "E_NO_KICAD"


def test_created_projects_and_projects_without_stamps(tmp_path: Path):
    ctx = ctx_of(tmp_path, backend(I1099, I1006))
    created = Project.from_path(write_project(tmp_path / "c", sch=SCH_10, pcb=PCB_10))
    choice = resolve_target(ctx, created, ProjectState(target_kicad="10.0.6", target_source="created"))
    assert choice.source == SOURCE_CREATED and choice.writable
    bare = tmp_path / "bare"
    bare.mkdir()
    (bare / "bare.kicad_pro").write_text("{}", encoding="utf-8")
    choice = resolve_target(ctx, Project.from_path(bare), ProjectState())
    assert choice.version == "10.99.0" and choice.writable and choice.project_epoch == {}


def test_mixed_generation_sheets_are_not_writable_and_are_named(tmp_path: Path):
    """The review's kw_sch_edit(provider='file') result: root re-stamped 10.99, child still 10.0."""
    ctx = ctx_of(tmp_path, backend(I1099, I1006))
    project = Project.from_path(write_project(tmp_path / "p", sch=SCH_99, pcb=PCB_10, child=SCH_10_DEMO))
    choice = resolve_target(ctx, project, ProjectState())
    assert choice.version == "10.99.0" and not choice.writable
    assert any("mix" in n and "child.kicad_sch" in n for n in choice.notes)


def test_writable_per_kind(tmp_path: Path):
    """A schematic saved by 10.0 next to a 9.0 board: 10.0.6 writes the schematic's format, so a
    schematic write proceeds, a board write asks for the upgrade."""
    ctx = ctx_of(tmp_path, backend(I1099, I1006, I909))
    project = Project.from_path(write_project(tmp_path / "p", sch=SCH_10, pcb=PCB_9))
    choice = effective_target(ctx, project, ProjectState(), for_write=True, kinds=("sch",))
    assert choice.version == "10.0.6" and choice.source == SOURCE_NEWER_READONLY
    assert choice.writable_kinds >= {"sch"} and "pcb" not in choice.writable_kinds
    with pytest.raises(KiwrapError) as exc:
        effective_target(ctx, project, ProjectState(), for_write=True, kinds=("pcb",))
    assert exc.value.code == "E_FORMAT_UPGRADE_REQUIRED"
    assert [f["path"] for f in exc.value.data["files"]] == ["demo.kicad_pcb"]


def test_consent_message_wording(tmp_path: Path):
    ctx = ctx_of(tmp_path, backend(I1099))
    project = Project.from_path(write_project(tmp_path / "p"))
    msg = consent_message(project, resolve_target(ctx, project, ProjectState()))
    assert msg == (
        "Upgrade demo from KiCad 10.0 format to KiCad 10.99.0? KiCad 10.0 will no longer open it. "
        "Alternatively install KiCad 10.0 (winget install KiCad.KiCad --version 10.0.6)."
    )


# ---- tools through the MCP server -------------------------------------------------------------------------


def harness(tmp_path: Path, *installs: FakeInstall, **knobs: Any) -> Harness:
    tmp_path.mkdir(parents=True, exist_ok=True)
    return Harness(tmp_path, backend(*installs, **knobs))


def test_pcb_drc_does_not_save_by_default_and_runs_on_the_matching_kicad(tmp_path: Path):
    """Review item 1 (a) / D22: kw_pcb_drc with its defaults never writes the board; on a 10.0
    project with 10.0.6 installed it runs on 10.0.6, with only 10.99 it runs on 10.99 in memory."""
    for installs, expect in (((I1099, I1006), "10.0.6"), ((I1099,), "10.99.0")):
        h = harness(tmp_path / expect, *installs)
        d = write_project(h.work / "p")
        before = digest(d)
        sc = h.ok("kw_pcb_drc", project=str(d))
        call = [kw for n, kw in h.backend.calls if n == "run_drc"][-1]
        assert call["save_board"] is False and sc["provider_used"] == f"cli:{expect}"
        assert sc["data"]["options"]["save_board"] is False and sc["data"]["target"]["kicad"] == expect
        assert digest(d) == before
    # only 10.99 and an explicit save: the write asks first; without a client to ask it teaches
    sc = h.fail("kw_pcb_drc", "E_FORMAT_UPGRADE_REQUIRED", project=str(d), save_board=True)
    assert sc["data"]["upgrade_to"] == "10.99.0" and digest(d) == before


def test_pcb_drc_explicit_save_on_the_matching_kicad(tmp_path: Path):
    h = harness(tmp_path, I1006)
    d = write_project(h.work / "p")
    sc = h.ok("kw_pcb_drc", project=str(d), save_board=True, refill_zones=True)
    call = [kw for n, kw in h.backend.calls if n == "run_drc"][-1]
    assert call["save_board"] is True and sc["data"]["options"]["save_board"] is True


def test_stamp_guard_rolls_back_an_unexpected_upconversion(tmp_path: Path):
    """Review item 1 fix layer 3: a write path nobody anticipated (here: a kicad-cli that
    re-stamps the board on --save-board although the install claims the board's format) is
    undone from the call's baseline snapshot and reported as E_FORMAT_UPGRADE_REQUIRED."""
    h = harness(tmp_path, I1006, upconvert_on_save=PCB_99)
    d = write_project(h.work / "p")
    h.ok("kw_project", action="status", project=str(d))
    before = digest(d)
    sc = h.fail("kw_pcb_drc", "E_FORMAT_UPGRADE_REQUIRED", project=str(d), save_board=True, refill_zones=True)
    guard = sc["data"]["guard"]
    assert guard["restored"] is True and guard["files"] == [
        {"path": "demo.kicad_pcb", "stamp_before": PCB_10, "stamp_after": PCB_99}
    ]
    assert digest(d) == before and file_epoch(d / "demo.kicad_pcb") == PCB_10
    # nothing is left half-converted: the project still runs on 10.0.6 in its own format
    st = h.ok("kw_project", action="status", project=str(d))["data"]
    assert st["epoch_match"] is True and st["target_effective"] == "10.0.6"


def test_stamp_guard_allows_a_resave_inside_the_same_kicad_line(tmp_path: Path):
    """KiCad 10.0.6 saving a board stamped by a 10.0 pre-release (20260101) writes 20260206: the
    same KiCad line (every 10.0.x reads both), so the guard lets it through."""
    h = harness(tmp_path, I1006, upconvert_on_save=PCB_10)
    d = write_project(h.work / "p", pcb=20260101)
    sc = h.ok("kw_pcb_drc", project=str(d), save_board=True, refill_zones=True)
    assert file_epoch(d / "demo.kicad_pcb") == PCB_10
    assert sc["data"]["epochs"]["before"]["demo.kicad_pcb"] == 20260101
    assert sc["data"]["epochs"]["after"]["demo.kicad_pcb"] == PCB_10


def upgrade_args_of(h: Harness, d: Path) -> dict[str, Any]:
    """`kw_project status`'s upgrade_args, as the four kw_project_upgrade arguments (D24)."""
    ua = h.ok("kw_project", action="status", project=str(d))["data"]["upgrade_args"]
    assert ua, "no upgrade needed"
    return {k: ua[k] for k in ("project", "from_kicad", "to_kicad", "files")}


def test_stamp_guard_lets_the_undo_through_but_not_the_redo(tmp_path: Path):
    """Restoring snapshot_before only lowers the stamps and passes; restoring the post-upgrade
    snapshot again would raise them without the user's approval, so the guard rolls it back (review
    finding 1, D24: only kw_project_upgrade converts) - the forward way is the tool, then the restore."""
    h = harness(tmp_path, I1099)
    d = write_project(h.work / "p")
    first = h.ok("kw_project_upgrade", **upgrade_args_of(h, d))
    assert file_epoch(d / "demo.kicad_pcb") == PCB_99
    pre = first["data"]["snapshot_before"]
    h.ok("kw_snapshot", project=str(d), action="restore", ref=pre)
    assert file_epoch(d / "demo.kicad_pcb") == PCB_10
    refused = h.fail(
        "kw_snapshot", "E_FORMAT_UPGRADE_REQUIRED", project=str(d), action="restore", ref=first["snapshot"]
    )
    assert file_epoch(d / "demo.kicad_pcb") == PCB_10 and refused["data"]["guard"]["restored"] is True
    h.ok("kw_project_upgrade", **upgrade_args_of(h, d))
    h.ok("kw_snapshot", project=str(d), action="restore", ref=first["snapshot"])
    assert file_epoch(d / "demo.kicad_pcb") == PCB_99


def test_project_status_reports_the_effective_target(tmp_path: Path):
    h = harness(tmp_path, I1099)
    d = write_project(h.work / "p")
    sc = h.ok("kw_project", action="status", project=str(d))
    data = sc["data"]
    assert data["target_effective"] == data["target_kicad_effective"] == "10.99.0"
    assert data["target_source"] == SOURCE_NEWER_READONLY and data["epoch_match"] is False
    assert data["epoch_match_kinds"] == {"pcb": False, "sch": False} and data["project_kicad"] == "10.0"
    assert data["install_hint"] == "winget install KiCad.KiCad --version 10.0.6"
    assert data["warnings"] and data["warnings"][0] in sc["warnings"]


def test_retarget_pins_without_touching_files_and_auto_unpins(tmp_path: Path):
    h = harness(tmp_path, I1099, I1006)
    d = write_project(h.work / "p")
    before = digest(d)
    sc = h.ok("kw_project", action="retarget", project=str(d), target_kicad="10.99")
    assert sc["data"]["target_kicad"] == "10.99.0" and sc["data"]["epoch_match"] is False
    assert any("upgrade" in w for w in sc["warnings"]) and digest(d) == before
    st = h.ok("kw_project", action="status", project=str(d))["data"]
    assert st["target_effective"] == "10.99.0" and st["target_source"] == SOURCE_PINNED
    assert any("retarget" in w for w in st["warnings"])
    # a write now asks first; the fix names the KiCad that writes the files' format
    err = h.fail("kw_pcb_drc", "E_FORMAT_UPGRADE_REQUIRED", project=str(d), save_board=True)
    assert err["candidates"] == ["10.0.6"]
    sc = h.ok("kw_project", action="retarget", project=str(d), target_kicad="auto")
    assert sc["data"]["target_kicad"] is None and sc["data"]["target_effective"] == "10.0.6"
    assert digest(d) == before
    # a KiCad that cannot read the files cannot be pinned
    newer = write_project(h.work / "n", sch=SCH_99, pcb=PCB_99)
    h.fail("kw_project", "E_FORMAT_NEWER", action="retarget", project=str(newer), target_kicad="10.0.6")


def test_preview_reports_and_the_upgrade_tool_converts(tmp_path: Path):
    """D24: the write refuses with upgrade_args, upgrade_preview converts a copy and changes nothing,
    kw_project_upgrade(**upgrade_args) converts and pins; writes proceed afterwards."""
    h = harness(tmp_path, I1099)
    d = write_project(h.work / "p", child=SCH_10_DEMO)
    before = digest(d)
    sc = h.fail("kw_pcb_drc", "E_FORMAT_UPGRADE_REQUIRED", project=str(d), save_board=True)
    ua = sc["data"]["upgrade_args"]
    assert ua["files"] == ["child.kicad_sch", "demo.kicad_pcb", "demo.kicad_sch"] and digest(d) == before
    pre = h.ok("kw_project", action="upgrade_preview", project=str(d))
    assert pre["next_suggested"] == "kw_project_upgrade" and pre["data"]["upgrade_args"] == ua
    assert pre["data"]["checks"]["before"]["erc"] == {"errors": 0, "warnings": 0}
    assert pre["data"]["checks"]["after"]["drc"]["errors"] == 0 and digest(d) == before
    assert sorted(f["path"] for f in pre["data"]["files"]) == ua["files"]
    sc = h.ok("kw_project_upgrade", **{k: ua[k] for k in ("project", "from_kicad", "to_kicad", "files")})
    data = sc["data"]
    assert sorted(data["replaced"]) == ["child.kicad_sch", "demo.kicad_pcb", "demo.kicad_sch"]
    assert {Path(p).name: s for p, s in data["epochs"]["after"].items()} == {
        "demo.kicad_sch": SCH_99,
        "child.kicad_sch": SCH_99,
        "demo.kicad_pcb": PCB_99,
    }
    assert data["snapshot_before"] and sorted(sc["changed"]["updated"]) == sorted(data["replaced"])
    assert data["target"] == {"kicad": "10.99.0", "source": SOURCE_PINNED, "epoch_match": True}
    st = h.ok("kw_project", action="status", project=str(d))["data"]
    assert st["target_source"] == SOURCE_PINNED and st["epoch_match"] is True and st["upgrade_args"] is None
    # writes proceed now
    h.ok("kw_pcb_drc", project=str(d), save_board=True, refill_zones=True)


def test_upgrade_to_a_kicad_without_the_upgrade_command(tmp_path: Path):
    h = harness(tmp_path, I909)
    d = write_project(h.work / "p", sch=20231120, pcb=20240108)
    sc = h.fail("kw_project_upgrade", "E_UNSUPPORTED", **upgrade_args_of(h, d))
    assert sc["data"]["capability"] == "cli.has_sch_pcb_upgrade"
    sc = h.fail("kw_project", "E_UNSUPPORTED", action="upgrade_preview", project=str(d))
    assert sc["data"]["capability"] == "cli.has_sch_pcb_upgrade"


# ---- consent over MCP elicitation -------------------------------------------------------------------------


async def _call_with_client(
    server: Any, mode: str, answer: str | None, tool: str, args: dict[str, Any]
) -> Any:
    asked: list[str] = []

    async def elicit(context: Any, params: Any) -> ElicitResult:
        asked.append(params.message)
        if answer == "accept":
            return ElicitResult(action="accept", content={"upgrade": True})
        return ElicitResult(action=answer or "decline")

    async with Client(server, mode=mode, elicitation_callback=elicit if answer else None) as client:
        result = await client.call_tool(tool, args)
    return result, asked


@pytest.mark.parametrize("mode", ["legacy", "auto"])
def test_elicitation_accept_upgrades_then_the_call_continues(tmp_path: Path, mode: str):
    """A write that needs an upgrade asks the user through MCP elicitation (the 2025 server
    request in legacy mode, the 2026-07-28 InputRequiredResult round trip in auto mode); on
    accept the project is upgraded and pinned and the original call completes."""
    h = harness(tmp_path, I1099)
    d = write_project(h.work / "p")
    result, asked = anyio.run(
        _call_with_client,
        h.server,
        mode,
        "accept",
        "kw_pcb_drc",
        {"project": str(d), "save_board": True, "refill_zones": True},
    )
    assert asked and asked[0].startswith("Upgrade demo from KiCad 10.0 format to KiCad 10.99.0?")
    assert not result.is_error, result.structured_content
    sc = result.structured_content
    assert sc["data"]["upgrade"]["kicad"] == "10.99.0" and sc["data"]["options"]["save_board"] is True
    assert file_epoch(d / "demo.kicad_pcb") == PCB_99 and file_epoch(d / "demo.kicad_sch") == SCH_99
    state = Project.from_path(d).load_state()
    assert state.target_kicad == "10.99.0"
    assert any("upgraded to KiCad 10.99.0 format (elicitation)" in w for w in sc["warnings"])
    # D24: the accept ran the same conversion core - checks on, the consent recorded
    up = sc["data"]["upgrade"]
    assert up["checks"]["before"] is not None and up["checks"]["after"] is not None
    assert up["consent"]["gate"] == "elicitation" and up["consent"]["via"] == "elicitation"
    assert any((e.get("consent") or {}).get("via") == "elicitation" for e in state.history)


@pytest.mark.parametrize("mode", ["legacy", "auto"])
@pytest.mark.parametrize("answer", ["decline", "cancel"])
def test_elicitation_decline_leaves_the_files_alone(tmp_path: Path, mode: str, answer: str):
    h = harness(tmp_path, I1099)
    d = write_project(h.work / "p")
    before = digest(d)
    result, asked = anyio.run(
        _call_with_client, h.server, mode, answer, "kw_pcb_drc", {"project": str(d), "save_board": True}
    )
    assert asked and result.is_error
    sc = result.structured_content
    assert sc["code"] == "E_FORMAT_UPGRADE_REQUIRED" and sc["data"]["answer"] == answer
    assert digest(d) == before and Project.from_path(d).load_state().target_kicad is None


@pytest.mark.parametrize("mode", ["legacy", "auto"])
def test_no_elicitation_under_the_vscode_panel(tmp_path: Path, mode: str, monkeypatch: pytest.MonkeyPatch):
    """D24: under the Claude Code VS Code panel (it declines every elicitation) kiwrap sends none and
    answers E_FORMAT_UPGRADE_REQUIRED without claiming the user declined."""
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "claude-vscode")
    h = harness(tmp_path, I1099)
    d = write_project(h.work / "p")
    before = digest(d)
    result, asked = anyio.run(
        _call_with_client, h.server, mode, "accept", "kw_pcb_drc", {"project": str(d), "save_board": True}
    )
    assert not asked and result.is_error
    sc = result.structured_content
    assert sc["code"] == "E_FORMAT_UPGRADE_REQUIRED" and "answer" not in sc["data"]
    assert "declined" not in sc["why"] and sc["data"]["upgrade_args"]["files"]
    assert digest(d) == before


@pytest.mark.parametrize("mode", ["legacy", "auto"])
def test_no_elicitation_capability_is_the_teaching_error(tmp_path: Path, mode: str):
    h = harness(tmp_path, I1099)
    d = write_project(h.work / "p")
    result, asked = anyio.run(
        _call_with_client, h.server, mode, None, "kw_pcb_drc", {"project": str(d), "save_board": True}
    )
    assert not asked and result.is_error and result.structured_content["code"] == "E_FORMAT_UPGRADE_REQUIRED"


def test_the_mcp_context_parameter_is_not_in_any_input_schema(tmp_path: Path):
    h = harness(tmp_path, I1006)
    tools = anyio.run(h.server.list_tools)
    for tool in tools:
        assert not any("mcp" in k for k in tool.input_schema.get("properties", {})), tool.name


# ---- planner: the ipc11 write precondition -----------------------------------------------------------------


def test_ipc11_write_falls_back_to_file_when_it_would_restamp(tmp_path: Path):
    """Review item 1 (c): every ipc11 write ends in SaveDocument; on a document older than the
    api-server's format the planner skips ipc11 (degraded ipc11_epoch_mismatch) even when forced."""
    ctx = ctx_of(tmp_path, backend(I1099))
    caps = FakeCaps(CAPS_1099, I1099)
    older = Project.from_path(write_project(tmp_path / "old", sch=SCH_10, pcb=PCB_10))
    attempts = {planner.IPC11: lambda: "ipc", planner.FILE: lambda: "file"}
    for prefer in (None, "ipc11"):
        with mutation(ctx, older, "kw_pcb_place", gate=False):
            out = planner.run("board.place", caps, attempts, install=I1099, prefer=prefer)
        assert out.value == "file" and planner.EPOCH_MISMATCH_TAG in out.degraded
        assert any(p == planner.IPC11 and "newer file format" in r for p, r in out.plan.skipped)
    same = Project.from_path(write_project(tmp_path / "new", sch=SCH_99, pcb=PCB_99))
    with mutation(ctx, same, "kw_pcb_place", gate=False):
        out = planner.run("board.place", caps, attempts, install=I1099)
    assert out.value == "ipc" and planner.EPOCH_MISMATCH_TAG not in out.degraded
    # reads are never gated, and outside a mutation nothing is known about the document
    out = planner.run("board.read", caps, {planner.IPC11: lambda: "ipc"}, install=I1099)
    assert out.value == "ipc"


def test_ipc11_write_through_a_bound_gui_judges_the_guis_formats(tmp_path: Path):
    """Review finding 7: with a kw_live GUI bound to the project the pool hands every ipc11 attempt
    to THAT GUI whatever install was requested, so the precondition must judge the GUI's formats,
    not the effective target's: a newer-format GUI next to a target writing the files' own format
    would re-stamp them (and the stamp guard only warns while a GUI is bound)."""
    ctx = ctx_of(tmp_path, backend(I1099, I1006))
    target_caps = FakeCaps({**CAPS_10, "ipc.headless": True}, I1006)  # a target with a headless provider
    older = Project.from_path(write_project(tmp_path / "p", sch=SCH_10, pcb=PCB_10))
    attempts = {planner.IPC11: lambda: "ipc", planner.FILE: lambda: "file"}

    def bound_to(install: FakeInstall, sch: int, pcb: int) -> None:
        ctx.services.ipc_release_project = lambda project_dir: {  # type: ignore[attr-defined]
            "bound": {
                "session_id": "g1",
                "pid": 4242,
                "locks": [],
                "ipc11": True,
                "install": install.key(),
                "kicad": install.version,
                "formats": {"sch": sch, "pcb": pcb},
            }
        }

    bound_to(I1099, SCH_99, PCB_99)  # the user's GUI is the newer KiCad
    with mutation(ctx, older, "kw_pcb_place", gate=False):
        out = planner.run("board.place", target_caps, attempts, install=I1006)
    assert out.value == "file" and planner.EPOCH_MISMATCH_TAG in out.degraded
    assert any("live KiCad 10.99.0 GUI" in r for p, r in out.plan.skipped if p == planner.IPC11)
    bound_to(I1006, SCH_10, PCB_10)  # a GUI writing the files' own format writes through
    with mutation(ctx, older, "kw_pcb_place", gate=False):
        out = planner.run("board.place", target_caps, attempts, install=I1006)
    assert out.value == "ipc" and planner.EPOCH_MISMATCH_TAG not in out.degraded


# ---- writers' epochs -----------------------------------------------------------------------------------------


def test_epoch_for_never_exceeds_the_files_stamp(tmp_path: Path):
    project = Project.from_path(write_project(tmp_path / "p", sch=SCH_10_DEMO, pcb=PCB_10))
    caps = FakeCaps(CAPS_1099, I1099)
    assert epoch_for(caps) == {"sch": SCH_99, "sym": CAPS_1099["formats.sym"], "pcb": PCB_99, "pro": 4}
    assert epoch_for(caps, project) == {
        "sch": SCH_10_DEMO,
        "sym": CAPS_1099["formats.sym"],
        "pcb": PCB_10,
        "pro": 4,
    }


def test_board_writer_keeps_an_older_boards_stamp(tmp_path: Path):
    from kiwrap.formats.board_writer import BoardWriter

    d = write_project(tmp_path / "p", pcb=PCB_10)
    w = BoardWriter(d, epoch={"pcb": PCB_99})
    w.load(d / "demo.kicad_pcb")
    w.add_track([(5.0, 5.0), (15.0, 5.0)], 0.25, "F.Cu", "")
    w.save()
    text = (d / "demo.kicad_pcb").read_text(encoding="utf-8")
    assert file_epoch(d / "demo.kicad_pcb") == PCB_10 and "(segment" in text


def test_schematic_writer_keeps_each_sheets_stamp_within_an_era_and_refuses_mixed_eras(tmp_path: Path):
    """Review item 1 (d): the file writer re-stamped the touched root upward, leaving a
    mixed-epoch project. Now each sheet keeps its stamp; sheets of different eras are refused."""
    from kiwrap.formats.schematic_writer import SchematicWriter, WriterError
    from kiwrap.formats.schematic_writer.libs import FallbackLibResolver

    src = Path(__file__).resolve().parents[1] / "conformance" / "corpus" / "writer" / "hier_10"
    if not src.is_dir():
        pytest.skip("writer corpus missing")
    d = tmp_path / "h"
    shutil.copytree(src, d)
    amp = d / "amp.kicad_sch"
    text = amp.read_text(encoding="utf-8")
    amp.write_text(re.sub(r"\(version \d+\)", f"(version {SCH_10_DEMO})", text, count=1), encoding="utf-8")
    stamps = {p.name: file_epoch(p) for p in d.glob("*.kicad_sch")}
    assert stamps["top.kicad_sch"] == SCH_10 and stamps["amp.kicad_sch"] == SCH_10_DEMO
    # a newer epoch than the files (a 10.99 install): the writer works in the files' own dialect
    w = SchematicWriter(d, epoch={"sch": SCH_99, "sym": 20260830}, lib_resolver=FallbackLibResolver())
    w.load(d / "top.kicad_sch")
    assert w.dialect.sch_stamp == SCH_10_DEMO and w.dialect.name == "10"
    edited = 0
    for key in w.sheet_keys:
        for sym in w.symbols(key):
            if not sym.ref.startswith("#"):
                w.edit(sym.uuid, {"value": "47k"})
                edited += 1
    assert edited
    w.save()
    assert {p.name: file_epoch(p) for p in d.glob("*.kicad_sch")} == stamps
    # a 9.0-era sheet next to 10.0-era ones is a project no single KiCad line wrote: refused
    amp.write_text(
        re.sub(r"\(version \d+\)", f"(version {SCH_9})", amp.read_text(encoding="utf-8"), count=1),
        encoding="utf-8",
    )
    w = SchematicWriter(d, epoch={"sch": SCH_10, "sym": 20251024}, lib_resolver=FallbackLibResolver())
    with pytest.raises(WriterError) as exc:
        w.load(d / "top.kicad_sch")
    assert exc.value.code == "E_EPOCH" and "eras" in str(exc.value)
    # the sanctioned upgrade path still loads it and re-stamps what it touches
    up = SchematicWriter(
        d, epoch={"sch": SCH_10, "sym": 20251024}, lib_resolver=FallbackLibResolver(), allow_upgrade=True
    )
    up.load(d / "top.kicad_sch")
    assert up.dialect.sch_stamp == SCH_10


# ---- kw_live launch -------------------------------------------------------------------------------------------


def test_live_launch_refuses_a_gui_newer_than_the_files(tmp_path: Path):
    """Review item 1 (f): kw_live launch picked the newest GUI; now the project's own KiCad,
    and a GUI writing a newer format is a write that needs the user's consent."""
    from kiwrap.core.tools import live

    launched: list[Any] = []

    class Session:
        def status(self) -> dict[str, Any]:
            return {"session_id": "g1", "warnings": []}

    be = backend(I1099, I1006)
    ctx = ctx_of(tmp_path, be)
    ctx.services.gui_launch = lambda install, project_dir, **kw: launched.append(install.version) or Session()  # type: ignore[attr-defined]
    ctx.services.gui_input_from_events = lambda install, caps: False  # type: ignore[attr-defined]
    ctx.services.sandbox_ensure = lambda install, **kw: {"install": install.version, **kw}  # type: ignore[method-assign]
    d = write_project(tmp_path / "p")
    live.run(ctx, live.LiveArgs(action="launch", project=str(d)))
    assert launched == ["10.0.6"]
    with pytest.raises(KiwrapError) as exc:
        live.run(ctx, live.LiveArgs(action="launch", project=str(d), target_kicad="10.99"))
    assert exc.value.code == "E_FORMAT_UPGRADE_REQUIRED" and launched == ["10.0.6"]


# ---- route-auto asks before queueing --------------------------------------------------------------------------


def test_async_route_asks_the_format_question_before_queueing(tmp_path: Path):
    h = harness(tmp_path, I1099)
    d = write_project(h.work / "p")
    state = Project.from_path(d).load_state()
    state.stage = "placed"
    Project.from_path(d).save_state(state)
    sc = h.fail("kw_pcb_route_auto", "E_FORMAT_UPGRADE_REQUIRED", project=str(d))
    assert sc["data"]["upgrade_to"] == "10.99.0"


def test_drc_report_default_is_still_the_fake(tmp_path: Path):
    """Sanity: the fakes above report what the harness expects (no violation, drc_clean)."""
    h = harness(tmp_path, I1006)
    d = write_project(h.work / "p")
    h.backend.drc = drc_report()
    assert h.ok("kw_pcb_drc", project=str(d))["data"]["ok"] is True
