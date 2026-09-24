"""Fixer findings on the D21 / D23 work, with fakes (no KiCad): kw_project create never overwrites a
design (finding 1), sub-sheets in subdirectories are part of the project (finding 2), a failed
upgrade or a rolled-back call leaves nothing behind and says what it undid (finding 5), the upgrade
prompt names the files it converts and never a KiCad newer than the target (finding 6), kw_project's
annotations are explained (finding 8) and E_FORMAT_UPGRADE_REQUIRED's example is the dry run
(finding 9). The live counterparts are in tests/test_epoch_safety_fixes_live.py.
"""

from __future__ import annotations

import hashlib
import re
import shutil
from pathlib import Path
from typing import Any

import pytest

from kiwrap.backends.cli.upgrade import copy_project
from kiwrap.core.state import KiwrapError
from kiwrap.core.state.project import Project, ProjectState, file_epoch
from kiwrap.core.state.snapshots import _git
from kiwrap.core.tools import help as help_tool
from kiwrap.core.tools import project as project_tool
from kiwrap.core.tools import templates
from kiwrap.core.tools._base import consent_message, effective_target, mutation, resolve_target
from tests.test_epoch_safety import (
    I1006,
    I1099,
    PCB_9,
    PCB_10,
    PCB_99,
    RELEASES,
    SCH_9,
    SCH_10,
    SCH_10_DEMO,
    SCH_99,
    backend,
    ctx_of,
    harness,
    write_project,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH (snapshots)")

#: the matrix's seed of the next major: unreleased, same epochs as the 10.99 nightly
ELEVEN: dict[str, Any] = {
    "version": "11.0.0",
    "line": "11.0",
    "formats": {"sch": SCH_99, "pcb": PCB_99},
    "nightly": False,
    "released": False,
    "install_hint": None,
}


def tree_digest(d: Path) -> dict[str, str]:
    """sha256 of every KiCad file under `d` (subdirectories included, `.kiwrap` excluded)."""
    return {
        p.relative_to(d).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(d.rglob("*.kicad_*"))
        if ".kiwrap" not in p.relative_to(d).parts
    }


def restamp(path: Path, stamp: int) -> None:
    text = path.read_text(encoding="utf-8")
    path.write_text(re.sub(r"\(version \d+\)", f"(version {stamp})", text, count=1), encoding="utf-8")


def sheet_node(file: str, name: str = "Child") -> str:
    return (
        f'\t(sheet (at 50 50) (size 20 10) (uuid "00000000-0000-0000-0000-{abs(hash(file)) % 10**12:012d}")\n'
        f'\t\t(property "Sheetname" "{name}" (at 50 49 0))\n'
        f'\t\t(property "Sheetfile" "{file}" (at 50 61 0))\n'
        "\t)\n"
    )


def add_sheets(sheet: Path, *files: str) -> None:
    """Place hierarchical sheets referencing `files` in `sheet` (before its closing paren)."""
    text = sheet.read_text(encoding="utf-8").rstrip()
    assert text.endswith(")")
    sheet.write_text(
        text[:-1] + "".join(sheet_node(f, f"S{i}") for i, f in enumerate(files)) + ")\n", encoding="utf-8"
    )


def write_sheet(path: Path, stamp: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(templates.schematic_text(epoch=stamp, generator_version="10.0"), encoding="utf-8")
    return path


def hier_project(d: Path, *, root: int, child: int, pcb: int) -> Path:
    """demo.kicad_sch -> sch/child.kicad_sch (the royalblue54L_feather layout) + a board."""
    write_project(d, sch=root, pcb=pcb)
    write_sheet(d / "sch" / "child.kicad_sch", child)
    add_sheets(d / "demo.kicad_sch", "sch/child.kicad_sch")
    return d


# ---- finding 1: kw_project create never overwrites a design ---------------------------------------------


def test_create_refuses_a_directory_holding_the_design_of_the_same_name(tmp_path: Path):
    """The review's e9c: pic_programmer's sheets and board without its .kicad_pro; create returned ok
    and replaced both with the templates. Now E_BAD_ARGS before anything is written."""
    h = harness(tmp_path, I1099, I1006)
    d = write_project(h.work / "pic", child=SCH_10)
    (d / "demo.kicad_pro").unlink()
    before = tree_digest(d)
    for target in ("10.0", None):  # same format and the (newer) default target
        args = {"action": "create", "dir": str(d), "name": "demo"}
        if target:
            args["target_kicad"] = target
        sc = h.fail("kw_project", "E_BAD_ARGS", **args)
        assert "never overwrites" in sc["message"] and "demo.kicad_sch" in sc["candidates"]
        assert "status" in sc["fix"]
        assert tree_digest(d) == before
        assert not list(d.glob("*.kicad_pro")) and not (d / ".kiwrap").exists()


def test_create_refuses_any_design_file_in_the_directory(tmp_path: Path):
    h = harness(tmp_path, I1006)
    d = h.work / "other"
    d.mkdir()
    (d / "board.kicad_pcb").write_text(
        templates.board_text(epoch=PCB_10, generator_version="10.0", layer_ids_v2=True, outline_mm=None),
        encoding="utf-8",
    )
    before = tree_digest(d)
    sc = h.fail("kw_project", "E_BAD_ARGS", action="create", dir=str(d), name="fresh")
    assert sc["candidates"] == ["board.kicad_pcb"] and tree_digest(d) == before
    assert not (d / "fresh.kicad_pro").exists()
    # an empty directory still works
    h.ok("kw_project", action="create", dir=str(h.work / "empty"), name="fresh")


def test_create_never_clobbers_a_file_that_appears_mid_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Exclusive create: a board written by someone else after the existence check survives, and
    the files this call had written are removed again."""
    h = harness(tmp_path, I1006)
    d = h.work / "race"
    racing = '(kicad_pcb (version 20260206) (generator "someone else"))\n'
    real_board_text = templates.board_text

    def board_text(**kw: Any) -> str:
        (d / "race.kicad_pcb").write_text(racing, encoding="utf-8")
        return real_board_text(**kw)

    monkeypatch.setattr(templates, "board_text", board_text)
    sc = h.fail("kw_project", "E_BAD_ARGS", action="create", dir=str(d), name="race")
    assert "race.kicad_pcb" in sc["message"] and "nothing was overwritten" in sc["message"]
    assert (d / "race.kicad_pcb").read_text(encoding="utf-8") == racing
    assert not (d / "race.kicad_pro").exists() and not (d / "race.kicad_sch").exists()


# ---- finding 2: sub-sheets in subdirectories ---------------------------------------------------------------


def test_schematic_files_follow_the_sheet_hierarchy(tmp_path: Path):
    d = write_project(tmp_path / "p", sch=SCH_10)
    write_sheet(d / "orphan.kicad_sch", SCH_10)  # top-level: kept, as before
    write_sheet(d / "sch" / "child.kicad_sch", SCH_9)
    write_sheet(d / "sch" / "grand.kicad_sch", SCH_9)
    write_sheet(d / "old" / "stale.kicad_sch", SCH_9)  # not referenced: left out
    write_sheet(d / "demo-backups" / "b.kicad_sch", SCH_9)
    write_sheet(tmp_path / "outside.kicad_sch", SCH_9)
    add_sheets(
        d / "demo.kicad_sch",
        "sch/child.kicad_sch",
        "demo-backups/b.kicad_sch",
        "../outside.kicad_sch",
        "sch/missing.kicad_sch",
    )
    # relative to the referencing sheet's directory; a cycle back to the root is harmless
    add_sheets(d / "sch" / "child.kicad_sch", "grand.kicad_sch", "../demo.kicad_sch", "grand.kicad_sch")
    project = Project.from_path(d)
    assert [project.relpath(p) for p in project.schematic_files()] == [
        "demo.kicad_sch",
        "orphan.kicad_sch",
        "sch/child.kicad_sch",
        "sch/grand.kicad_sch",
    ]
    stamps = {f.path: f.stamp for f in project.format_stamps()}
    assert stamps["sch/child.kicad_sch"] == SCH_9 and stamps["demo.kicad_pcb"] == PCB_10
    assert project.project_epochs() == {"sch": SCH_10, "pcb": PCB_10}


def test_status_and_target_see_a_sub_sheet_in_another_format(tmp_path: Path):
    """royalblue54L_feather after the old upgrade: root and board 10.0, sch/ still 9.0 - status must
    not report epoch_match=true with no warning."""
    h = harness(tmp_path, I1099, I1006)
    d = hier_project(h.work / "rb", root=SCH_10, child=SCH_9, pcb=PCB_10)
    st = h.ok("kw_project", action="status", project=str(d))["data"]
    assert "sch/child.kicad_sch" in st["files"]["sch"]
    assert {f["path"]: f["stamp"] for f in st["file_epochs"]}["sch/child.kicad_sch"] == SCH_9
    assert st["epoch_match"] is False and any("sch/child.kicad_sch" in w for w in st["warnings"])


def test_upgrade_converts_sub_sheets_and_the_copy_keeps_the_hierarchy(tmp_path: Path):
    """The review's e4: kw_project upgrade(confirm, to_kicad='10.0') on a 9.0 project whose sheets
    live under sch/ replaced only the root and the board; now every sheet is converted, the
    ERC/DRC copies carry sch/, status reports a uniform project and a second upgrade is a no-op."""
    h = harness(tmp_path, I1099, I1006)
    d = hier_project(h.work / "rb", root=SCH_9, child=SCH_9, pcb=PCB_9)
    sc = h.ok("kw_project", action="upgrade", project=str(d), to_kicad="10.0", confirm=True)
    up = sc["data"]["upgrade"]
    assert sorted(up["replaced"]) == ["demo.kicad_pcb", "demo.kicad_sch", "sch/child.kicad_sch"]
    assert up["from"]["sch/child.kicad_sch"] == SCH_9 and up["to"]["sch/child.kicad_sch"] == SCH_10
    assert file_epoch(d / "sch" / "child.kicad_sch") == SCH_10 and file_epoch(d / "demo.kicad_sch") == SCH_10
    assert file_epoch(d / "demo.kicad_pcb") == PCB_10
    work = Path(up["work_dir"])
    assert file_epoch(work / "sch" / "child.kicad_sch") == SCH_10
    assert file_epoch(work.parent / "before" / "sch" / "child.kicad_sch") == SCH_9
    st = h.ok("kw_project", action="status", project=str(d))["data"]
    assert st["epoch_match"] is True and st["target_effective"] == "10.0.6"
    assert not any("mix" in w for w in st["warnings"])
    again = h.ok("kw_project", action="upgrade", project=str(d), to_kicad="10.0", confirm=True)
    assert again["data"]["upgraded"] is False


def test_copy_project_mirrors_relative_paths(tmp_path: Path):
    d = hier_project(tmp_path / "p", root=SCH_10, child=SCH_10, pcb=PCB_10)
    files = Project.from_path(d).design_files()
    work = copy_project(d, files, tmp_path / "work")
    assert sorted(p.relative_to(work).as_posix() for p in work.rglob("*.kicad_*")) == [
        "demo.kicad_pcb",
        "demo.kicad_pro",
        "demo.kicad_sch",
        "sch/child.kicad_sch",
    ]


def test_stamp_guard_sees_a_sub_sheet_whose_stamp_rises(tmp_path: Path):
    ctx = ctx_of(tmp_path, backend(I1006))
    d = hier_project(tmp_path / "p", root=SCH_10, child=SCH_10, pcb=PCB_10)
    project = Project.from_path(d)
    before = tree_digest(d)
    with pytest.raises(KiwrapError) as exc, mutation(ctx, project, "kw_sch_edit", gate=False):
        restamp(d / "sch" / "child.kicad_sch", SCH_99)
    assert exc.value.code == "E_FORMAT_UPGRADE_REQUIRED"
    assert [f["path"] for f in exc.value.data["guard"]["files"]] == ["sch/child.kicad_sch"]
    assert tree_digest(d) == before


# ---- finding 5: nothing left behind, and the error says what was undone ------------------------------------


def test_upgrade_failing_midway_puts_everything_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The review's e3: the board could not be replaced (read-only); the replaced sheets must come
    back, the temp file must not stay in the project nor enter the FAILED snapshot, and the error
    must say what was rolled back."""
    h = harness(tmp_path, I1099)
    d = write_project(h.work / "p", child=SCH_10_DEMO)
    h.ok("kw_project", action="status", project=str(d))
    before = tree_digest(d)
    real_replace = Path.replace

    def replace(self: Path, target: Any) -> Path:
        if self.name.endswith(project_tool.REPLACE_TMP_SUFFIX) and Path(target).suffix == ".kicad_pcb":
            raise PermissionError(13, "Access is denied", str(target))
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", replace)
    sc = h.fail("kw_project", "E_FILE_LOCKED", action="upgrade", project=str(d), confirm=True)
    monkeypatch.setattr(Path, "replace", real_replace)
    rollback = sc["data"]["upgrade_rollback"]
    assert sorted(rollback["restored"]) == ["child.kicad_sch", "demo.kicad_sch"]
    assert rollback["not_restored"] == [] and rollback["snapshot_before"]
    assert "undone" in sc["fix"]
    assert tree_digest(d) == before
    assert not list(d.rglob(f"*{project_tool.REPLACE_TMP_SUFFIX}"))
    tracked = _git(d, "ls-tree", "-r", "--name-only", "HEAD").stdout.split()
    assert not [t for t in tracked if t.endswith(project_tool.REPLACE_TMP_SUFFIX)]
    assert "FAILED E_FILE_LOCKED" in _git(d, "log", "-1", "--format=%s").stdout
    # the project is still in its own format and upgradable
    assert h.ok("kw_project", action="upgrade", project=str(d), confirm=True)["data"]["upgraded"] is True


def test_stamp_guard_rollback_removes_the_files_the_call_added(tmp_path: Path):
    ctx = ctx_of(tmp_path, backend(I1006))
    d = write_project(tmp_path / "p", sch=SCH_10, pcb=PCB_10)
    project = Project.from_path(d)
    before = tree_digest(d)
    with pytest.raises(KiwrapError) as exc, mutation(ctx, project, "kw_pcb_place", gate=False):
        (d / "stray.kicad_pro").write_text("{}\n", encoding="utf-8")
        (d / "demo.kicad_pcb.kiwrap-upgrade.tmp").write_text(
            "(kicad_pcb (version 20260901))\n", encoding="utf-8"
        )
        restamp(d / "demo.kicad_pcb", PCB_99)
    guard = exc.value.data["guard"]
    assert guard["restored"] is True
    assert sorted(guard["removed"]) == ["demo.kicad_pcb.kiwrap-upgrade.tmp", "stray.kicad_pro"]
    assert tree_digest(d) == before and not (d / "stray.kicad_pro").exists()


def test_a_non_kiwrap_failure_reports_the_rollback(tmp_path: Path):
    """A PermissionError after a stamp rose: the caller gets E_FILE_LOCKED with data.format_guard."""
    ctx = ctx_of(tmp_path, backend(I1006))
    d = write_project(tmp_path / "p", sch=SCH_10, pcb=PCB_10)
    project = Project.from_path(d)
    before = tree_digest(d)
    with pytest.raises(KiwrapError) as exc, mutation(ctx, project, "kw_pcb_place", gate=False):
        restamp(d / "demo.kicad_pcb", PCB_99)
        raise PermissionError(13, "Access is denied", str(d / "demo.kicad_pcb"))
    assert exc.value.code == "E_FILE_LOCKED" and isinstance(exc.value.__cause__, PermissionError)
    assert exc.value.data["format_guard"]["restored"] is True
    assert tree_digest(d) == before


# ---- finding 6: the prompt names what is converted, never a KiCad newer than the target -------------------


def _mixed_ctx(tmp_path: Path) -> tuple[Any, Project]:
    ctx = ctx_of(tmp_path, backend(I1099, I1006))
    ctx.services.format_releases = lambda: [ELEVEN, *RELEASES]  # type: ignore[method-assign]
    d = write_project(tmp_path / "pic", sch=SCH_99, pcb=PCB_10, child=SCH_10_DEMO)
    return ctx, Project.from_path(d)


@pytest.mark.parametrize("kinds", [("sch",), ("pcb",), None])
def test_mixed_project_prompt_names_the_files_it_converts(tmp_path: Path, kinds: Any):
    """The review's e7: root re-saved by 10.99, child sheet and board still 10.0; kw_sch_edit said
    'is in KiCad 11.0 format ... install KiCad 11.0', kw_pcb_place 'from file format pcb ..., sch ...
    format'."""
    ctx, project = _mixed_ctx(tmp_path)
    choice = resolve_target(ctx, project, ProjectState())
    assert choice.mixed and choice.project_kicad is None
    with pytest.raises(KiwrapError) as exc:
        effective_target(ctx, project, ProjectState(), for_write=True, kinds=kinds)
    err = exc.value
    texts = [err.message, err.fix, consent_message(project, choice, kinds)]
    for text in texts:
        assert "11.0" not in text, text
        assert "file format pcb" not in text and "format format" not in text, text
        assert "child.kicad_sch (KiCad 10.0 format)" in text, text
        assert "demo.kicad_pcb (KiCad 10.0 format)" in text, text
        assert "KiCad 10.99 format" in text, text
    assert "install KiCad" not in err.fix and "snapshot" in err.fix
    assert (
        err.data["project_kicad"] is None and err.data["mixed"] is True and err.data["install_hint"] is None
    )
    assert consent_message(project, choice, kinds).startswith(
        "Upgrade demo to the KiCad 10.99 format (KiCad 10.99.0)?"
    )


def test_nightly_files_are_named_by_the_nightly_not_the_next_major(tmp_path: Path):
    ctx = ctx_of(tmp_path, backend(I1099))
    ctx.services.format_releases = lambda: [ELEVEN, *RELEASES]  # type: ignore[method-assign]
    project = Project.from_path(write_project(tmp_path / "n", sch=SCH_99, pcb=PCB_99))
    assert resolve_target(ctx, project, ProjectState()).project_kicad == "10.99"
    old = ctx_of(tmp_path / "old", backend(I1006))
    old.services.format_releases = lambda: [ELEVEN, *RELEASES]  # type: ignore[method-assign]
    with pytest.raises(KiwrapError) as exc:
        effective_target(old, project, ProjectState(), for_write=False)
    assert exc.value.code == "E_FORMAT_NEWER" and exc.value.data["needed_kicad"] == "10.99"


def test_uniform_prompt_keeps_the_contract_wording_with_the_next_major_in_the_matrix(tmp_path: Path):
    ctx = ctx_of(tmp_path, backend(I1099))
    ctx.services.format_releases = lambda: [ELEVEN, *RELEASES]  # type: ignore[method-assign]
    project = Project.from_path(write_project(tmp_path / "p"))
    assert consent_message(project, resolve_target(ctx, project, ProjectState())) == (
        "Upgrade demo from KiCad 10.0 format to KiCad 10.99.0? KiCad 10.0 will no longer open it. "
        "Alternatively install KiCad 10.0 (winget install KiCad.KiCad --version 10.0.6)."
    )


# ---- findings 8 and 9 -------------------------------------------------------------------------------------


def test_upgrade_error_example_is_the_dry_run_everywhere(tmp_path: Path):
    """The stamp guard's error and the consent refusal both give the dry run as `example`."""
    h = harness(tmp_path, I1099)
    d = write_project(h.work / "p")
    sc = h.fail("kw_pcb_drc", "E_FORMAT_UPGRADE_REQUIRED", project=str(d), save_board=True)
    assert "dry_run=true" in sc["example"] and "confirm=true" not in sc["example"]
    assert sc["fix"].index("if yes:") < sc["fix"].index("confirm=true")
    dry = h.ok("kw_project", action="upgrade", project=str(d), dry_run=True)["data"]
    assert dry["upgraded"] is False and dry["upgrade"]["checks"]["before"] is not None


def test_kw_project_annotations_are_explained():
    """MCP annotations are per tool: kw_project stays destructive (upgrade replaces files), and the
    description and kw_help(topic='readonly') say status and the dry run are read-only."""
    spec = project_tool.SPEC
    assert spec.annotations.destructive_hint is True and spec.annotations.read_only_hint is False
    assert "status" in spec.description and "read-only" in spec.description
    assert "destructive" in help_tool.READONLY_PAGE and "kw_project" in help_tool.READONLY_PAGE
