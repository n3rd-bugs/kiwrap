"""kw_project_upgrade with fakes (no KiCad): docs/DECISIONS.md D24 (amends D21).

The only conversion of a project's design files to a newer KiCad format is
`kw_project_upgrade(project, from_kicad, to_kicad, files)`, published with
`_meta["anthropic/requiresUserInteraction"]: true` and destructive annotations, without
`persist` / `confirm` / `dry_run`. The server rebuilds `upgrade_args()` under the project lock and
refuses any difference; the receipt names the consent record (the client flow that approved the
call, never a human click). `kw_project` keeps create / status / retarget and gains the read-only
`upgrade_preview`; a stale `action='upgrade'` or `confirm` is a teaching E_BAD_ARGS. A read-only
server does not register the tool and answers a call to it with E_READONLY.

The acceptance list of D24 is covered here against fakes (tools/list over an in-process mcp
Client, the consent record per clientInfo, every mismatch); tests/test_project_upgrade_live.py
repeats the round trip on scratch copies of KiCad's demos.
"""

from __future__ import annotations

import hashlib
import re
import shutil
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import Client
from mcp.types import ElicitResult, Implementation

from kiwrap.core.server import INSTRUCTIONS, MOVED_ACTIONS, READONLY_INSTRUCTIONS, build_server
from kiwrap.core.state.project import Project, file_epoch
from kiwrap.core.state.snapshots import list_snapshots
from kiwrap.core.tools import ALL_TOOLS, TOOL_NAMES
from kiwrap.core.tools import project as project_tool
from kiwrap.core.tools._base import CLAUDE_CODE_MIN_CONSENT, consent_source
from tests.test_core_fakes import erc_report, violation
from tests.test_core_tools import Harness
from tests.test_epoch_safety import (
    I909,
    I1006,
    I1099,
    PCB_10,
    PCB_99,
    SCH_9,
    SCH_10,
    SCH_10_DEMO,
    SCH_99,
    FakeInstall,
    backend,
    harness,
    write_project,
)
from tests.test_epoch_safety_fixes import add_sheets, write_sheet

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH (snapshots)")

CALL_KEYS = ("project", "from_kicad", "to_kicad", "files")
FLAG = "anthropic/requiresUserInteraction"
#: a KiCad version literal (D24: none in the tool's description or the server instructions)
VERSION_LITERAL = re.compile(r"\b\d+\.\d+(\.\d+)?\b")


def tree(d: Path) -> dict[str, str]:
    """sha256 of every file under `d` except `.kiwrap/` (design files, sidecars, libraries)."""
    return {
        p.relative_to(d).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(d.rglob("*"))
        if p.is_file() and ".kiwrap" not in p.relative_to(d).parts
    }


def call_args(ua: dict[str, Any]) -> dict[str, Any]:
    return {k: ua[k] for k in CALL_KEYS}


def rich_project(d: Path, *, root: int, child: int, pcb: int) -> Path:
    """demo.kicad_sch -> sch/child.kicad_sch + board, with every sidecar kiwrap must never convert:
    `.kicad_prl`, `.kicad_dru`, the lib tables, a local `.kicad_sym` and a `.pretty` library."""
    write_project(d, sch=root, pcb=pcb)
    write_sheet(d / "sch" / "child.kicad_sch", child)
    add_sheets(d / "demo.kicad_sch", "sch/child.kicad_sch")
    (d / "demo.kicad_prl").write_text('{"meta": {"version": 5}}\n', encoding="utf-8")
    (d / "demo.kicad_dru").write_text("(version 1)\n", encoding="utf-8")
    (d / "sym-lib-table").write_text("(sym_lib_table (version 7))\n", encoding="utf-8")
    (d / "fp-lib-table").write_text("(fp_lib_table (version 7))\n", encoding="utf-8")
    (d / "local.kicad_sym").write_text("(kicad_symbol_lib (version 20241209))\n", encoding="utf-8")
    (d / "local.pretty").mkdir()
    (d / "local.pretty" / "R.kicad_mod").write_text("(footprint R (version 20241229))\n", encoding="utf-8")
    return d


def status_args(h: Harness, d: Path) -> dict[str, Any]:
    ua = h.ok("kw_project", action="status", project=str(d))["data"]["upgrade_args"]
    assert ua
    return ua


# ---- publishing (tools/list over the wire) ------------------------------------------------------------------


async def _list_tools(server: Any) -> list[dict[str, Any]]:
    async with Client(server) as client:
        result = await client.list_tools()
    return [t.model_dump(by_alias=True, mode="json", exclude_none=True) for t in result.tools]


def test_tools_list_publishes_the_flag_and_no_consent_arguments(tmp_path: Path):
    """D24 acceptance: `_meta.anthropic/requiresUserInteraction` is the JSON boolean true, the
    annotations are destructive, the four arguments are required and nothing else is published;
    kw_project's action enum lost `upgrade`; no tool publishes `confirm`."""
    h = harness(tmp_path, I1099)
    tools = anyio.run(_list_tools, h.server)
    names = [t["name"] for t in tools]
    assert names == list(TOOL_NAMES) and names.index("kw_project_upgrade") == names.index("kw_project") + 1
    up = next(t for t in tools if t["name"] == "kw_project_upgrade")
    assert up["_meta"][FLAG] is True
    assert up["annotations"]["destructiveHint"] is True and up["annotations"]["readOnlyHint"] is False
    schema = up["inputSchema"]
    assert schema["required"] == list(CALL_KEYS)
    assert set(schema["properties"]) == set(CALL_KEYS)
    assert schema["properties"]["files"]["type"] == "array"
    assert len(up["description"]) <= 1500 and not VERSION_LITERAL.search(up["description"])
    project = next(t for t in tools if t["name"] == "kw_project")
    assert project["inputSchema"]["properties"]["action"]["enum"] == [
        "create",
        "status",
        "retarget",
        "upgrade_preview",
    ]
    assert FLAG not in (project.get("_meta") or {})
    for t in tools:
        props = t["inputSchema"]["properties"]
        assert "confirm" not in props and ("dry_run" not in props or t["name"] != "kw_project"), t["name"]
        if t["name"] != "kw_project_upgrade":
            assert "persist" in props and FLAG not in (t.get("_meta") or {}), t["name"]


def test_instructions_teach_the_tool_without_version_literals():
    assert "kw_project_upgrade" in INSTRUCTIONS and "upgrade_args" in INSTRUCTIONS
    assert "upgrade_preview" in INSTRUCTIONS and "confirm" not in INSTRUCTIONS
    assert not VERSION_LITERAL.search(INSTRUCTIONS)
    assert len(INSTRUCTIONS) < 2048 and len(INSTRUCTIONS + READONLY_INSTRUCTIONS) < 2048


def test_the_catalog_order_and_the_spec():
    spec = project_tool.UPGRADE_SPEC
    assert ALL_TOOLS[TOOL_NAMES.index("kw_project_upgrade")] is spec
    assert spec.meta == {FLAG: True} and spec.common_persist is False
    assert CLAUDE_CODE_MIN_CONSENT == (2, 1, 199)
    assert set(MOVED_ACTIONS) >= {("kw_project", "upgrade"), ("kw_project", "confirm")}


# ---- upgrade_args: one source for the refusal, status, preview and the recompute ----------------------------


def test_refusal_status_and_preview_carry_the_same_upgrade_args(tmp_path: Path):
    """A write on a project older than its pinned target: E_FORMAT_UPGRADE_REQUIRED carries
    project, from_kicad, to_kicad, to_format and files (root, a sub-sheet under sch/, the board);
    the example is a placeholder kw_project_upgrade call and nothing mentions the retired flag;
    status and the preview return the same arguments; nothing in the project changes."""
    h = harness(tmp_path, I1099, I1006)
    d = rich_project(h.work / "p", root=SCH_10_DEMO, child=SCH_10_DEMO, pcb=PCB_10)
    h.ok("kw_project", action="retarget", project=str(d), target_kicad="10.99")
    before = tree(d)
    err = h.fail("kw_pcb_drc", "E_FORMAT_UPGRADE_REQUIRED", project=str(d), save_board=True)
    ua = err["data"]["upgrade_args"]
    assert ua == {
        "project": (d / "demo.kicad_pro").resolve().as_posix(),
        "from_kicad": "10.0",
        "to_kicad": "10.99.0",
        "to_format": {"sch": SCH_99, "pcb": PCB_99},
        "files": ["demo.kicad_pcb", "demo.kicad_sch", "sch/child.kicad_sch"],
    }
    assert err["example"].startswith("kw_project_upgrade(")
    for text in (err["message"], err["fix"], err["example"]):
        assert "confirm" not in text, text
    assert "retarget" in err["fix"] and err["candidates"] == ["10.0.6"]
    st = h.ok("kw_project", action="status", project=str(d))
    assert st["data"]["upgrade_args"] == ua and st["data"]["retarget_candidates"] == ["10.0.6"]
    assert any("kw_project_upgrade" in w for w in st["warnings"])
    pre = h.ok("kw_project", action="upgrade_preview", project=str(d))
    assert pre["data"]["upgrade_args"] == ua and pre["next_suggested"] == "kw_project_upgrade"
    assert pre["data"]["checks"]["before"] and pre["data"]["checks"]["after"]
    assert "demo.kicad_prl" in pre["data"]["not_converted"] and pre["data"]["alternatives"]
    assert tree(d) == before
    assert not list((h.home / "tmp" / "upgrade").glob("*-preview-*"))  # the preview's copy is gone


def test_mixed_generation_board_write_args_are_accepted_verbatim(tmp_path: Path):
    """kit-dev-coldfire's shape (older sheets, board re-saved by a newer KiCad): the board-only
    write's refusal carries kinds-independent arguments that kw_project_upgrade accepts (the old
    kinds-dependent from_kicad never matched, D24)."""
    h = harness(tmp_path, I1099)
    d = rich_project(h.work / "p", root=SCH_9, child=SCH_9, pcb=PCB_10)
    err = h.fail("kw_pcb_drc", "E_FORMAT_UPGRADE_REQUIRED", project=str(d), save_board=True)
    ua = err["data"]["upgrade_args"]
    assert ua["from_kicad"] == f"format sch={SCH_9},pcb={PCB_10}"
    assert ua == status_args(h, d)
    sc = h.ok("kw_project_upgrade", **call_args(ua))
    assert sorted(sc["data"]["replaced"]) == ["demo.kicad_pcb", "demo.kicad_sch", "sch/child.kicad_sch"]
    # the write proceeds now
    h.ok("kw_pcb_drc", project=str(d), save_board=True, refill_zones=True)


def test_mixed_generation_converts_only_the_older_kind(tmp_path: Path):
    """The same shape with the board's own KiCad installed: the target is that KiCad, the board is
    already in its format, so only the sheets are listed and the board stays byte-identical."""
    h = harness(tmp_path, I1099, I1006)
    d = rich_project(h.work / "p", root=SCH_9, child=SCH_9, pcb=PCB_10)
    h.ok("kw_pcb_drc", project=str(d), save_board=True, refill_zones=True)  # board writes run
    err = h.fail(
        "kw_sch_edit",
        "E_FORMAT_UPGRADE_REQUIRED",
        project=str(d),
        ops=[{"op": "property", "ref": "R1", "name": "Value", "value": "1k"}],
        provider="file",
    )
    ua = err["data"]["upgrade_args"]
    assert ua["to_kicad"] == "10.0.6" and ua["files"] == ["demo.kicad_sch", "sch/child.kicad_sch"]
    board = (d / "demo.kicad_pcb").read_bytes()
    sc = h.ok("kw_project_upgrade", **call_args(ua))
    assert (d / "demo.kicad_pcb").read_bytes() == board and "demo.kicad_pcb" in sc["data"]["not_converted"]
    assert file_epoch(d / "sch" / "child.kicad_sch") == SCH_10


# ---- the conversion ----------------------------------------------------------------------------------------


def test_upgrade_converts_exactly_the_listed_files(tmp_path: Path):
    """D24 acceptance: exactly the listed files are replaced and carry to_format; the unlisted design
    file, .kicad_pro, .kicad_prl, .kicad_dru, the lib tables and the project libraries are
    byte-identical; the sub-sheet keeps its relative path; the receipt carries the D24 fields; the
    snapshot restore brings the old stamps back past the stamp guard; a later write succeeds."""
    h = harness(tmp_path, I1099)
    d = rich_project(h.work / "p", root=SCH_99, child=SCH_10_DEMO, pcb=PCB_10)
    h.ok("kw_project", action="status", project=str(d))
    ua = status_args(h, d)
    assert ua["files"] == ["demo.kicad_pcb", "sch/child.kicad_sch"]  # the root is already 10.99
    before = tree(d)
    sc = h.ok("kw_project_upgrade", **call_args(ua))
    data = sc["data"]
    assert sorted(data["replaced"]) == ua["files"] and sorted(sc["changed"]["updated"]) == ua["files"]
    after = tree(d)
    changed = sorted(k for k in before if before[k] != after.get(k))
    assert changed == ua["files"] and set(after) == set(before)
    assert file_epoch(d / "sch" / "child.kicad_sch") == SCH_99 and file_epoch(d / "demo.kicad_pcb") == PCB_99
    files = {f["path"]: f for f in data["files"]}
    assert files["sch/child.kicad_sch"]["stamp_before"] == SCH_10_DEMO
    assert (
        files["sch/child.kicad_sch"]["stamp_after"] == SCH_99 and files["demo.kicad_pcb"]["changed"] is True
    )
    for kept in (
        "demo.kicad_sch",
        "demo.kicad_pro",
        "demo.kicad_prl",
        "demo.kicad_dru",
        "sym-lib-table",
        "fp-lib-table",
        "local.kicad_sym",
        "local.pretty",
    ):
        assert kept in data["not_converted"], kept
    assert data["target"] == {"kicad": "10.99.0", "source": "pinned", "epoch_match": True}
    assert data["from_kicad"] == ua["from_kicad"] and data["to_kicad"] == "10.99.0"
    assert data["checks"]["before"]["erc"] and data["checks"]["after"]["drc"]
    assert {v["name"] for v in sc["validators"]} == {"erc", "drc"}
    assert data["consent"] == {
        "gate": "none",
        "via": "in_process",
        "client": {"name": None, "version": None},
        "entrypoint": None,
    }
    assert data["stage_dropped"] == [] and sc["next_suggested"] == "kw_sch_check"
    assert any(
        w.startswith("KiCad 10.0 can no longer open sch/child.kicad_sch, demo.kicad_pcb; kw_snapshot restore")
        for w in sc["warnings"]
    )
    state = Project.from_path(d).load_state()
    assert state.target_kicad == "10.99.0" and state.target_source == "pinned"
    assert (
        state.history[-1]["tool"] == "kw_project_upgrade"
        and state.history[-1]["consent"]["via"] == "in_process"
    )
    assert any(s.message.startswith("[kiwrap] kw_project_upgrade") for s in list_snapshots(d))
    # a later write succeeds
    h.ok("kw_pcb_drc", project=str(d), save_board=True, refill_zones=True)
    # the snapshot taken first undoes it, past the stamp guard
    h.ok("kw_snapshot", project=str(d), action="restore", ref=data["snapshot_before"])
    assert (
        file_epoch(d / "sch" / "child.kicad_sch") == SCH_10_DEMO
        and file_epoch(d / "demo.kicad_pcb") == PCB_10
    )
    assert {k: v for k, v in tree(d).items() if k != "demo.kicad_prl"} == {
        k: v for k, v in before.items() if k != "demo.kicad_prl"
    }


def test_a_uniform_project_warns_that_its_kicad_line_can_no_longer_open_it(tmp_path: Path):
    h = harness(tmp_path, I1099)
    d = write_project(h.work / "p")
    sc = h.ok("kw_project_upgrade", **call_args(status_args(h, d)))
    snap = sc["data"]["snapshot_before"]
    assert any(
        w.startswith("KiCad 10.0 can no longer open")
        and w.endswith(f"kw_snapshot restore {snap[:12]} undoes it")
        for w in sc["warnings"]
    )


def test_an_error_count_that_rises_drops_the_stage(tmp_path: Path):
    h = harness(tmp_path, I1099)
    d = write_project(h.work / "p")
    state = Project.from_path(d).load_state()
    state.stage = "erc_clean"
    Project.from_path(d).save_state(state)
    ua = status_args(h, d)
    real = h.ctx.services.run_erc
    calls: list[int] = []
    worse = erc_report(*(violation("pin_not_connected", "error", 10.0 * i) for i in range(3)))

    def run_erc(*a: Any, **kw: Any) -> Any:
        calls.append(1)
        return worse if len(calls) == 2 else real(*a, **kw)  # the 2nd: the upgraded copy

    h.ctx.services.run_erc = run_erc
    sc = h.ok("kw_project_upgrade", **call_args(ua))
    assert sc["data"]["stage_dropped"] == ["erc_clean"] and sc["stage"] != "erc_clean"
    erc = next(v for v in sc["validators"] if v["name"] == "erc")
    assert erc["before"] == 0 and erc["after"] == 3
    assert any("stage dropped" in w for w in sc["warnings"])


# ---- the recompute refuses every mismatch -------------------------------------------------------------------


@pytest.mark.parametrize("tamper", ["from_kicad", "file_missing", "file_extra", "to_kicad_other_format"])
def test_every_mismatch_is_refused_with_the_true_arguments(tmp_path: Path, tamper: str):
    h = harness(tmp_path, I1099, I1006, I909)
    d = rich_project(h.work / "p", root=SCH_10_DEMO, child=SCH_10_DEMO, pcb=PCB_10)
    h.ok("kw_project", action="retarget", project=str(d), target_kicad="10.99")
    truth = status_args(h, d)
    args = call_args(truth)
    if tamper == "from_kicad":
        args["from_kicad"] = "9.0"
    elif tamper == "file_missing":
        args["files"] = args["files"][:-1]
    elif tamper == "file_extra":
        args["files"] = [*args["files"], "other.kicad_sch"]
    else:
        args["to_kicad"] = "10.0.6"
    before = tree(d)
    sc = h.fail("kw_project_upgrade", "E_BAD_ARGS", **args)
    assert sc["data"]["upgrade_args"] == truth and tree(d) == before
    mismatch = sc["data"]["mismatch"]
    key = {"file_missing": "files", "file_extra": "files", "to_kicad_other_format": "to_kicad"}.get(
        tamper, tamper
    )
    assert list(mismatch) == [key]
    if tamper == "file_missing":
        assert mismatch["files"] == {"missing": ["sch/child.kicad_sch"], "extra": []}
    if tamper == "to_kicad_other_format":
        assert mismatch["to_kicad"]["resolved"] == "10.0.6"
    assert "kw_project_upgrade" in sc["candidates"] and "confirm" not in sc["fix"]


def test_to_kicad_as_a_prefix_or_a_root_resolving_to_the_same_formats_is_accepted(tmp_path: Path):
    h = harness(tmp_path, I1099, I1006)
    d = write_project(h.work / "a")
    h.ok("kw_project", action="retarget", project=str(d), target_kicad="10.99")
    args = call_args(status_args(h, d))
    assert args["to_kicad"] == "10.99.0"
    h.ok("kw_project_upgrade", **{**args, "to_kicad": "10.99"})
    e = write_project(h.work / "b")
    h.ok("kw_project", action="retarget", project=str(e), target_kicad="10.99")
    args = call_args(status_args(h, e))
    sc = h.ok("kw_project_upgrade", **{**args, "to_kicad": str(I1099.root)})
    assert sc["data"]["target"]["kicad"] == "10.99.0"


def test_files_are_compared_after_normcase(tmp_path: Path):
    h = harness(tmp_path, I1099)
    d = rich_project(h.work / "p", root=SCH_10_DEMO, child=SCH_10_DEMO, pcb=PCB_10)
    args = call_args(status_args(h, d))
    args["files"] = list(reversed(args["files"]))
    if Path("A") == Path("a"):  # a case-insensitive file system (Windows)
        args["files"] = [f.upper() for f in args["files"]]
    h.ok("kw_project_upgrade", **args)


def test_nothing_to_convert_names_retarget(tmp_path: Path):
    h = harness(tmp_path, I1099)
    d = write_project(h.work / "p", sch=SCH_99, pcb=PCB_99)
    before = tree(d)
    sc = h.fail(
        "kw_project_upgrade",
        "E_BAD_ARGS",
        project=str(d),
        from_kicad="10.99",
        to_kicad="10.99.0",
        files=["demo.kicad_pcb", "demo.kicad_sch"],
    )
    assert "nothing to convert" in sc["message"] and "kw_project(action='retarget'" in sc["fix"]
    assert sc["data"]["upgrade_args"]["files"] == [] and tree(d) == before


def test_a_kicad_without_the_upgrade_command_is_unsupported(tmp_path: Path):
    h = harness(tmp_path, I909)
    d = write_project(h.work / "p", sch=20231120, pcb=20240108)
    sc = h.fail("kw_project_upgrade", "E_UNSUPPORTED", **call_args(status_args(h, d)))
    assert sc["data"]["capability"] == "cli.has_sch_pcb_upgrade"


def test_a_bound_kw_live_session_is_refused_before_anything_changes(tmp_path: Path):
    h = harness(tmp_path, I1099)
    d = write_project(h.work / "p")
    args = call_args(status_args(h, d))
    before = tree(d)
    h.ctx.services.ipc_release_project = lambda project_dir: {
        "bound": {"session_id": "g1", "pid": 4242, "locks": [], "ipc11": True}
    }
    sc = h.fail("kw_project_upgrade", "E_FILE_LOCKED", **args)
    assert "kw_live(action='detach'" in sc["fix"] and sc["data"]["live_session"]["session_id"] == "g1"
    assert tree(d) == before


# ---- retired actions and arguments, read-only mode -------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        {"action": "upgrade", "confirm": True},
        {"action": "upgrade", "dry_run": True},
        {"action": "status", "confirm": True},
        {"action": "upgrade", "to_kicad": "10"},
    ],
)
def test_the_retired_upgrade_action_is_a_teaching_error(tmp_path: Path, args: dict[str, Any]):
    h = harness(tmp_path, I1099)
    d = write_project(h.work / "p")
    before = tree(d)
    sc = h.fail("kw_project", "E_BAD_ARGS", project=str(d), **args)
    assert sc["candidates"][:2] == ["kw_project_upgrade", "kw_project(action='upgrade_preview')"]
    assert "kw_project_upgrade" in sc["message"] and sc["data"]["moved"]["tool"] == "kw_project"
    assert tree(d) == before and not (d / ".kiwrap").exists()


def test_the_upgrade_tool_takes_no_consent_flag_and_no_persist(tmp_path: Path):
    h = harness(tmp_path, I1099)
    d = write_project(h.work / "p")
    args = call_args(status_args(h, d))
    before = tree(d)
    h.fail("kw_project_upgrade", "E_BAD_ARGS", **args, confirm=True)
    h.fail("kw_project_upgrade", "E_BAD_ARGS", **args, dry_run=True)
    sc = h.fail("kw_project_upgrade", "E_READONLY", **args, persist=False)
    assert sc["data"]["mode"] == "call" and "upgrade_preview" in sc["candidates"][0]
    assert tree(d) == before


def _readonly_harness(tmp_path: Path) -> Harness:
    h = harness(tmp_path, I1099)
    h.ctx.readonly = True  # type: ignore[attr-defined]  # what `serve --readonly` does per process
    h.server = build_server(context=h.ctx)
    return h


def test_a_readonly_server_does_not_publish_the_tool(tmp_path: Path):
    """D24 acceptance: absent from tools/list; a call by name is E_READONLY (not a bare ToolError)
    with the preview and status as candidates; the preview works, the digest is unchanged and no
    .kiwrap/ is created."""
    h = _readonly_harness(tmp_path)
    tools = anyio.run(_list_tools, h.server)
    assert "kw_project_upgrade" not in [t["name"] for t in tools]
    d = write_project(h.work / "p")
    before = tree(d)
    sc = h.fail(
        "kw_project_upgrade",
        "E_READONLY",
        project=str(d),
        from_kicad="10.0",
        to_kicad="10.99.0",
        files=["demo.kicad_pcb", "demo.kicad_sch"],
    )
    assert sc["candidates"][:2] == ["kw_project(action='upgrade_preview')", "kw_project(action='status')"]
    assert sc["data"]["mode"] == "server"
    # a stale consent flag does not turn the missing tool into an argument lesson: still E_READONLY
    stale = h.fail("kw_project_upgrade", "E_READONLY", project=str(d), from_kicad="10.0", confirm=True)
    assert stale["data"]["mode"] == "server"
    # kw_project itself is registered, so its retired action still teaches
    h.fail("kw_project", "E_BAD_ARGS", action="upgrade", project=str(d), confirm=True)
    pre = h.ok("kw_project", action="upgrade_preview", project=str(d))
    assert pre["data"]["upgrade_args"]["files"] == ["demo.kicad_pcb", "demo.kicad_sch"]
    assert pre["data"]["checks"]["before"] and pre["data"]["readonly"]["mode"] == "server"
    assert pre["next_suggested"] is None  # a read-only session cannot run kw_project_upgrade
    assert h.ok("kw_project", action="status", project=str(d))["data"]["upgrade_args"]
    assert tree(d) == before and not (d / ".kiwrap").exists()


def test_the_preview_writes_nothing_on_a_writable_server(tmp_path: Path):
    h = harness(tmp_path, I1099)
    d = write_project(h.work / "p")
    before = tree(d)
    sc = h.ok("kw_project", action="upgrade_preview", project=str(d))
    assert sc["snapshot"] is None and sc["data"]["files"]
    assert tree(d) == before and not (d / ".kiwrap").exists()


# ---- the consent record per client (in-process mcp Client) ----------------------------------------------


async def _upgrade_as(
    server: Any, client_info: Implementation | None, mode: str, args: dict[str, Any]
) -> Any:
    async with Client(server, client_info=client_info, mode=mode) as client:
        return await client.call_tool("kw_project_upgrade", args)


@pytest.mark.parametrize("mode", ["legacy", "auto"])
@pytest.mark.parametrize(
    ("name", "version", "gate", "via"),
    [
        ("claude-code", "2.1.199", FLAG, "client_permission_flow"),
        ("claude-code", "2.1.281", FLAG, "client_permission_flow"),
        ("kiwrap-vscode", "0.1.0", "kiwrap_vscode_modal", "kiwrap_vscode_modal"),
        ("some-other-client", "1.0", "none", "unknown_client"),
    ],
)
def test_the_consent_record_names_the_client_flow(
    tmp_path: Path, mode: str, name: str, version: str, gate: str, via: str
):
    h = harness(tmp_path, I1099)
    d = write_project(h.work / "p")
    args = call_args(status_args(h, d))
    result = anyio.run(_upgrade_as, h.server, Implementation(name=name, version=version), mode, args)
    assert not result.is_error, result.structured_content
    sc = result.structured_content
    assert sc["data"]["consent"]["gate"] == gate and sc["data"]["consent"]["via"] == via
    assert sc["data"]["consent"]["client"] == {"name": name, "version": version}
    assert file_epoch(d / "demo.kicad_pcb") == PCB_99
    unknown = [w for w in sc["warnings"] if "is not known to ask the user" in w]
    assert bool(unknown) == (via == "unknown_client")
    history = Project.from_path(d).load_state().history
    assert history[-1]["consent"]["via"] == via


@pytest.mark.parametrize("mode", ["legacy", "auto"])
def test_an_old_claude_code_is_unsupported_and_nothing_changes(tmp_path: Path, mode: str):
    h = harness(tmp_path, I1099)
    d = write_project(h.work / "p")
    args = call_args(status_args(h, d))
    before = tree(d)
    result = anyio.run(
        _upgrade_as, h.server, Implementation(name="claude-code", version="2.1.198"), mode, args
    )
    assert result.is_error
    sc = result.structured_content
    assert sc["code"] == "E_UNSUPPORTED" and "2.1.199" in sc["why"] and tree(d) == before


def test_consent_source_without_a_request_and_the_entrypoint(monkeypatch: pytest.MonkeyPatch):
    assert consent_source(None) == {
        "gate": "none",
        "via": "in_process",
        "client": {"name": None, "version": None},
        "entrypoint": None,
    }
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    assert consent_source(None)["entrypoint"] == "cli"


# ---- the frozen elicitation shortcut runs the same core ------------------------------------------------------


@pytest.mark.parametrize("mode", ["legacy", "auto"])
def test_elicitation_accept_converts_only_the_upgrade_files(tmp_path: Path, mode: str):
    """D24: an accept runs run_upgrade with upgrade_args - exactly upgrade_files(), checks on - and
    records consent via 'elicitation' (it used to convert every design file unchecked)."""
    h = harness(tmp_path, I1099)
    d = rich_project(h.work / "p", root=SCH_99, child=SCH_10_DEMO, pcb=PCB_10)
    root = (d / "demo.kicad_sch").read_bytes()
    asked: list[str] = []

    async def elicit(context: Any, params: Any) -> ElicitResult:
        asked.append(params.message)
        return ElicitResult(action="accept", content={"upgrade": True})

    async def go() -> Any:
        async with Client(h.server, mode=mode, elicitation_callback=elicit) as client:
            return await client.call_tool("kw_pcb_drc", {"project": str(d), "save_board": True})

    result = anyio.run(go)
    assert asked and not result.is_error, result.structured_content
    up = result.structured_content["data"]["upgrade"]
    assert sorted(up["replaced"]) == ["demo.kicad_pcb", "sch/child.kicad_sch"]
    assert up["consent"]["via"] == "elicitation" and up["checks"]["after"] is not None
    assert (d / "demo.kicad_sch").read_bytes() == root
    assert file_epoch(d / "sch" / "child.kicad_sch") == SCH_99
    history = Project.from_path(d).load_state().history
    assert any((e.get("consent") or {}).get("gate") == "elicitation" for e in history)


# ---- fakes sanity ------------------------------------------------------------------------------------------


def test_the_fake_find_accepts_an_install_root(tmp_path: Path):
    be = backend(I1099, I1006)
    assert be.find(str(I1006.root)) is I1006 and be.find("10.99") is I1099
    assert isinstance(I909, FakeInstall)


# ---- review findings of the S3 fixer pass ------------------------------------------------------------------


def test_snapshot_restore_cannot_redo_an_undone_conversion(tmp_path: Path):
    """Review finding 1: kw_snapshot was exempt from the stamp guard, so restoring the post-upgrade
    snapshot after the user undid the conversion re-applied it with no prompt. The undo still
    passes (it only lowers the stamps); the redo is refused, rolled back, and teaches
    kw_project_upgrade with the status upgrade_args - after which the same restore works."""
    h = harness(tmp_path, I1099)
    d = rich_project(h.work / "p", root=SCH_10_DEMO, child=SCH_10_DEMO, pcb=PCB_10)
    original = tree(d)
    up = h.ok("kw_project_upgrade", **call_args(status_args(h, d)))
    post, pre = up["snapshot"], up["data"]["snapshot_before"]
    assert post and pre and file_epoch(d / "demo.kicad_pcb") == PCB_99
    h.ok("kw_snapshot", project=str(d), action="restore", ref=pre)  # the user's undo
    assert tree(d) == original
    undone = tree(d)
    sc = h.fail("kw_snapshot", "E_FORMAT_UPGRADE_REQUIRED", project=str(d), action="restore", ref=post)
    assert tree(d) == undone and file_epoch(d / "demo.kicad_pcb") == PCB_10
    assert "nothing was restored" in sc["message"] and "kw_project_upgrade" in sc["why"]
    assert sc["data"]["guard"]["restored"] is True
    assert {r["path"] for r in sc["data"]["guard"]["files"]} == {
        "demo.kicad_pcb",
        "demo.kicad_sch",
        "sch/child.kicad_sch",
    }
    ua = sc["data"]["upgrade_args"]
    assert ua == status_args(h, d) and sc["example"].startswith("kw_project_upgrade(")
    assert f"ref='{post}'" in sc["fix"]
    # the sanctioned way there: convert (the user is asked), then the restore passes
    h.ok("kw_project_upgrade", **call_args(ua))
    h.ok("kw_snapshot", project=str(d), action="restore", ref=post)
    assert file_epoch(d / "sch" / "child.kicad_sch") == SCH_99


def test_snapshot_restore_of_a_resave_inside_one_kicad_line_passes(tmp_path: Path):
    """The guard compares format generations for kw_snapshot too: bringing back a 10.0.6 re-save
    (20260306) over the 10.0 demo stamp (20260101) is not an upgrade."""
    h = harness(tmp_path, I1006)
    d = write_project(h.work / "p", sch=SCH_10)
    snap = h.ok("kw_snapshot", project=str(d), action="create", message="resaved")["snapshot"]
    write_project(d, sch=SCH_10_DEMO)
    h.ok("kw_snapshot", project=str(d), action="create", message="demo stamp")
    h.ok("kw_snapshot", project=str(d), action="restore", ref=snap)
    assert file_epoch(d / "demo.kicad_sch") == SCH_10


def _live_ctx(h: Harness) -> list[str]:
    launched: list[str] = []

    class Session:
        def status(self) -> dict[str, Any]:
            return {"session_id": "g1", "pid": 4242, "warnings": []}

    svc: Any = h.ctx.services
    svc.gui_launch = lambda install, project_dir, **kw: launched.append(install.version) or Session()
    svc.gui_input_from_events = lambda install, caps: False
    svc.sandbox_ensure = lambda install, **kw: {"install": install.version, **kw}
    return launched


def test_a_kw_live_refusal_with_target_kicad_never_carries_arguments_the_tool_refuses(tmp_path: Path):
    """Review findings 2 and 3: kw_live launch with target_kicad on a project whose own KiCad writes
    its format built data.upgrade_args from the per-call target; kw_project_upgrade recomputes them
    without it and refused them as 'nothing to convert' - after the user approved the card. Now the
    refusal has no upgrade_args, names the pin first (data.retarget_first), and following it works."""
    h = harness(tmp_path, I1099, I1006)
    launched = _live_ctx(h)
    d = rich_project(h.work / "p", root=SCH_10_DEMO, child=SCH_10_DEMO, pcb=PCB_10)
    before = tree(d)
    sc = h.fail("kw_live", "E_FORMAT_UPGRADE_REQUIRED", action="launch", project=str(d), target_kicad="10.99")
    data = sc["data"]
    assert data["upgrade_args"] is None and data["runs_on"] == "10.0.6" and sc["candidates"] == ["10.0.6"]
    first = data["retarget_first"]
    assert first["target_kicad"] == "10.99.0" and first["target_kicad_before"] is None
    assert sc["example"].startswith('kw_project(action="retarget"') and "target_kicad='10.99.0'" in sc["fix"]
    assert launched == [] and tree(d) == before
    # what the old refusal taught fails after the prompt: the proof that no upgrade_args are attached
    stale = h.fail("kw_project_upgrade", "E_BAD_ARGS", **call_args(first["upgrade_args"]))
    assert "nothing to convert" in stale["message"] and tree(d) == before
    # the fix's order: pin (no file changes), status's upgrade_args (== retarget_first's), convert
    h.ok("kw_project", action="retarget", project=str(d), target_kicad=first["target_kicad"])
    assert tree(d) == before
    ua = status_args(h, d)
    assert ua == first["upgrade_args"]
    h.ok("kw_project_upgrade", **call_args(ua))
    h.ok("kw_live", action="launch", project=str(d), target_kicad="10.99")
    assert launched == ["10.99.0"]


def test_a_kw_live_refusal_whose_target_is_the_projects_own_carries_args_passed_verbatim(tmp_path: Path):
    """When the project's own target converts to the same format as kw_live's target_kicad (only
    the newer KiCad installed), the refusal's data.upgrade_args are the ones the tool recomputes:
    passed verbatim they convert, and the launch then runs."""
    h = harness(tmp_path, I1099)
    launched = _live_ctx(h)
    d = write_project(h.work / "p")
    sc = h.fail("kw_live", "E_FORMAT_UPGRADE_REQUIRED", action="launch", project=str(d), target_kicad="10.99")
    ua = sc["data"]["upgrade_args"]
    assert ua and "retarget_first" not in sc["data"] and sc["example"].startswith("kw_project_upgrade(")
    h.ok("kw_project_upgrade", **call_args(ua))
    h.ok("kw_live", action="launch", project=str(d), target_kicad="10.99")
    assert launched == ["10.99.0"]


def test_no_upgrade_without_an_undo_point(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Review finding 4: a failed pre-upgrade snapshot was swallowed and the files replaced anyway
    (snapshot_before null, 'kw_snapshot restore  undoes it'). Now nothing is converted."""
    from kiwrap.core.state import KiwrapError
    from kiwrap.core.tools import _base

    h = harness(tmp_path, I1099)
    d = write_project(h.work / "p")
    args = call_args(status_args(h, d))
    before = tree(d)

    def no_git(*a: Any, **kw: Any) -> Any:
        raise KiwrapError("E_INTERNAL", "git add -A failed (128): error: open(...): Filename too long")

    monkeypatch.setattr(_base, "snapshot_is_dirty", no_git)  # the prologue gets no baseline either
    monkeypatch.setattr(project_tool, "snapshot_create", no_git)
    sc = h.fail("kw_project_upgrade", "E_INTERNAL", **args)
    assert "no undo point" in sc["message"] and "nothing was converted" in sc["message"]
    assert "shorter path" in sc["fix"] and "Filename too long" in sc["data"]["snapshot_error"]
    assert tree(d) == before and file_epoch(d / "demo.kicad_pcb") == PCB_10
    assert not [c for c in h.backend.calls if c[0] == "upgrade_project"], "failed before the conversion"


@pytest.mark.parametrize("value", [False, "false", 0, "0", "no"])
def test_a_false_like_persist_on_the_upgrade_tool_is_read_only(tmp_path: Path, value: Any):
    """Review finding 5: the SDK drops the unpublished `persist`, so "false" / 0 converted the files
    while every other tool coerces them to persist=false and writes nothing (D23)."""
    h = harness(tmp_path, I1099)
    d = write_project(h.work / "p")
    args = call_args(status_args(h, d))
    before = tree(d)
    sc = h.fail("kw_project_upgrade", "E_READONLY", **args, persist=value)
    assert sc["data"]["mode"] == "call" and tree(d) == before


@pytest.mark.parametrize("value", [True, "true", 1, "maybe", None])
def test_any_other_persist_on_the_upgrade_tool_is_a_teaching_error(tmp_path: Path, value: Any):
    h = harness(tmp_path, I1099)
    d = write_project(h.work / "p")
    args = call_args(status_args(h, d))
    before = tree(d)
    sc = h.fail("kw_project_upgrade", "E_BAD_ARGS", **args, persist=value)
    assert "takes no persist" in sc["message"] and sc["data"]["argument"] == "persist"
    assert tree(d) == before


def test_a_guard_rollback_on_the_projects_own_kicad_teaches_the_pin_not_the_upgrade(tmp_path: Path):
    """Review finding 8: the stamp guard caught a write by another install while the project's
    target writes its format; data.upgrade_args had files [] and the example taught
    kw_project_upgrade, which refuses that as 'nothing to convert' after the prompt."""
    h = harness(tmp_path, I1099, I1006, upconvert_on_save=PCB_99)
    d = write_project(h.work / "p")
    sc = h.fail("kw_pcb_drc", "E_FORMAT_UPGRADE_REQUIRED", project=str(d), save_board=True, refill_zones=True)
    assert sc["data"]["guard"]["restored"] is True and sc["data"]["guard"]["written_by"] == "10.99.0"
    assert sc["data"]["upgrade_args"] is None and sc["candidates"] == ["10.0.6"]
    assert sc["example"].startswith('kw_project(action="retarget"') and "10.0.6" in sc["example"]
    assert "kw_project_upgrade" in sc["fix"]  # converting stays possible: pin the writer, then status


def test_to_kicad_on_retarget_is_the_argument_name_lesson_not_the_upgrade(tmp_path: Path):
    """Review finding 10: kw_project(action='retarget', to_kicad=...) was told that the upgrade
    action was retired and pointed at the destructive tool; the SDK would otherwise drop the key
    and the retarget would silently unpin."""
    h = harness(tmp_path, I1099)
    d = write_project(h.work / "p")
    before = tree(d)
    for action in ("retarget", "status", "create"):
        sc = h.fail("kw_project", "E_BAD_ARGS", action=action, project=str(d), to_kicad="10.99")
        assert "target_kicad" in sc["message"] and "retired" not in sc["message"]
        assert sc["candidates"][0] == "kw_project(action='retarget')"
        assert "kw_project_upgrade" not in sc["candidates"]
    assert tree(d) == before and not (d / ".kiwrap").exists()
