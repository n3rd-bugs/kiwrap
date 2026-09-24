"""Live regressions for requests/kiwrap-feature-requests.review.md items 3, 6, 19, 23 and the
kw_sch_get `.net` leftover, on stock KiCad 10.0.6 and the 10.99 nightly.

Every scenario runs through the real MCP tool handlers with discovery limited to ONE stock install
(the same `Server` shape as tests/test_epoch_safety_live.py) on scratch copies of the demos. Nothing
starts an api-server: `kw_pcb_get` / `kw_sch_get` are forced to `provider='file'` (and 'cli' for the
netlist), DRC is kicad-cli. Every process runs in the session's sandbox; no GUI.

- item 3: a run stores its complete result under the storage home (never `<board>.drc.json` in the
  project); `action='report'` answers from it with KiCad unavailable; a board edit is E_STALE.
- item 23: ecc83's `.kicad_pro` ignores one check while KiCad reports three (review §5 claim 6):
  `footprint_filters_mismatch` is attributed to the project, `track_not_centered_on_via` and
  `tuning_profile_track_geometries` to KiCad's defaults (verified 2026-09-24 on 10.0.6 and 10.99).
- item 19: on One-Air-Max, `--all-track-errors` finds one more copper error than the default run
  (479 -> 480 violations on 10.0.6, 477 -> 478 on 10.99, spike 2026-09-24); kw_pcb_drc passes it.
- item 6 / B3: the multichannel demo's unrouted twin board (148 airwires in the file estimate)
  walked page by page with a small limit returns exactly what one `format='ndjson'` call writes: no
  airwire skipped, none repeated; a cursor from before a board edit is E_STALE.
- §3 small defects: `kw_sch_get(kinds=['netlist'], provider='cli')` no longer leaves `<root>.net`.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import anyio
import pytest

from kiwrap.core.server import build_context, build_server
from kiwrap.core.services import Services

pytestmark = [pytest.mark.kicad]

LINES = ("10.0", "10.99")


def _stock(installs: list[Any], major_minor: str) -> Any:
    found = [i for i in installs if i.major_minor == major_minor and not i.is_bench_build]
    if not found:
        pytest.skip(f"no stock KiCad {major_minor}")
    return found[0]


class Server:
    """The real tool handlers with discovery limited to `keep`."""

    def __init__(self, storage_home: Path, keep: list[Any]):
        self.services = Services.real(storage_home)
        self.services.discover = lambda **kw: list(keep)  # type: ignore[method-assign]
        self.server = build_server(context=build_context(storage_home, self.services))

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


def demo_copy(install: Any, name: str, dst_root: Path) -> Path:
    src = install.dir("demos") / name
    if not src.is_dir():
        pytest.skip(f"{name} demo missing in KiCad {install.version}")
    dst = dst_root / name
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("*.3dshapes", "*.step", "*.wrl"))
    return dst


def design_digest(d: Path) -> dict[str, str]:
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(d.glob("*.kicad_*"))}


def no_kicad(*_a: Any, **_k: Any) -> Any:
    raise AssertionError("action='report' must not run KiCad")


@pytest.mark.parametrize("line", LINES)
def test_drc_report_and_ignored_checks_live(
    line: str, installs: list[Any], storage_home: Path, tmp_path: Path
):
    install = _stock(installs, line)
    project = demo_copy(install, "ecc83", tmp_path)
    pro = next(project.glob("*.kicad_pro"))
    srv = Server(storage_home / f"drc-{line}", [install])
    before = design_digest(project)

    sc = srv.ok("kw_pcb_drc", project=str(pro))
    d = sc["data"]
    # item 23: KiCad's effective ignore list, attributed
    sources = {c["key"]: c["source"] for c in d["ignored_checks"]}
    assert d["ignored_checks_source"] == "kicad", d["ignored_checks"]
    assert sources.get("footprint_filters_mismatch") == "project", sources
    assert sources.get("track_not_centered_on_via") == "kicad_default", sources
    assert "error" in (d["included_severities"] or [])
    # item 19: on by default where kicad-cli has the flag
    assert d["all_track_errors"] is True
    # item 3: stored outside the project; the project keeps no report and no design change
    result = d["result"]
    assert result["stored"] and Path(result["path"]).is_file() and Path(result["raw_json"]).is_file()
    assert (storage_home / f"drc-{line}") in Path(result["path"]).parents
    assert not list(project.glob("*.drc.json"))
    assert design_digest(project) == before, "kw_pcb_drc must not rewrite design files by default (D22)"

    srv.services.run_drc = no_kicad  # type: ignore[method-assign]
    rep = srv.ok("kw_pcb_drc", project=str(pro), action="report", severity="all")["data"]
    listed = d["must_fix"] + d["review"] + d["cosmetic"]
    assert rep["total"] == listed and rep["summary"]["must_fix"] == d["must_fix"]
    assert rep["ignored_checks"] == d["ignored_checks"] and rep["kicad_version"] == d["kicad_version"]
    if rep["violations"]:
        vtype = rep["violations"][0]["type"]
        only = srv.ok("kw_pcb_drc", project=str(pro), action="report", types=[vtype])["data"]
        assert only["total"] >= 1 and {x["type"] for x in only["violations"]} == {vtype}

    board = next(project.glob("*.kicad_pcb"))
    board.write_bytes(board.read_bytes() + b"\n")
    stale = srv.fail("kw_pcb_drc", "E_STALE", project=str(pro), action="report")
    assert stale["data"]["reason"] == "board_changed"


@pytest.mark.parametrize("line", LINES)
def test_all_track_errors_reports_more_live(
    line: str, installs: list[Any], storage_home: Path, tmp_path: Path
):
    install = _stock(installs, line)
    project = demo_copy(install, "openair-max", tmp_path)
    pro = next(project.glob("*.kicad_pro"))
    srv = Server(storage_home / f"ate-{line}", [install])
    default = srv.ok("kw_pcb_drc", project=str(pro))["data"]
    first_only = srv.ok("kw_pcb_drc", project=str(pro), all_track_errors=False)["data"]
    assert default["all_track_errors"] is True and first_only["all_track_errors"] is False

    def by_type(d: dict[str, Any]) -> dict[str, int]:
        # the receipt's own list (both runs share one board sha, so the store keeps only the last)
        assert d["violations_truncated"] == 0
        out: dict[str, int] = {}
        for x in d["violations"]:
            out[x["type"]] = out.get(x["type"], 0) + 1
        return out

    # without the flag each track stops at its first violation (review §5 claim 12): the default
    # run now reports at least one copper violation the first-violation-only run did not
    every, first = by_type(default), by_type(first_only)
    assert sum(every.values()) > sum(first.values()), (every, first)
    assert default["errors"] > first_only["errors"]
    assert all(every.get(t, 0) >= n for t, n in first.items()), (every, first)


@pytest.mark.parametrize("line", LINES)
def test_pcb_get_cursor_walk_equals_the_ndjson_dump_live(
    line: str, installs: list[Any], storage_home: Path, tmp_path: Path
):
    install = _stock(installs, line)
    project = demo_copy(install, "multichannel", tmp_path)
    # the demo ships an unrouted twin of its board (148 airwires in the file estimate): give it a
    # project file of its own so kw_pcb_get reads it
    unrouted_board = project / "multichannel_mixer-unrouted.kicad_pcb"
    if not unrouted_board.is_file():
        pytest.skip("multichannel_mixer-unrouted.kicad_pcb missing")
    pro_path = unrouted_board.with_suffix(".kicad_pro")
    shutil.copy(next(p for p in project.glob("*.kicad_pro") if p != pro_path), pro_path)
    pro = str(pro_path)
    srv = Server(storage_home / f"page-{line}", [install])
    args: dict[str, Any] = {"kinds": ["pads", "connectivity"], "detail": "summary", "provider": "file"}

    dump = srv.ok("kw_pcb_get", project=pro, format="ndjson", **args)["data"]["out"]
    rows = [json.loads(x) for x in Path(dump["path"]).read_text(encoding="utf-8").splitlines()]
    want_items = [json.dumps(r, sort_keys=True) for r in rows if r["kind"] == "pad"]
    want_air = [json.dumps(r, sort_keys=True) for r in rows if r["kind"] == "unrouted"]
    assert len(want_air) > 50, "multichannel has unrouted airwires"

    items: list[str] = []
    air: list[str] = []
    cursor = None
    pages = 0
    while True:
        d = srv.ok("kw_pcb_get", project=pro, cursor=cursor, limit=37, **args)["data"]
        pages += 1
        items.extend(json.dumps(i, sort_keys=True) for i in d["items"])
        air.extend(
            json.dumps({"kind": "unrouted", **u}, sort_keys=True) for u in d["connectivity"]["unrouted"]
        )
        cursor = d["next_cursor"]
        if cursor is None:
            break
        assert "@" in cursor and pages < 200
    assert items == want_items
    assert air == want_air, "every airwire exactly once (review B3)"

    first = srv.ok("kw_pcb_get", project=pro, limit=37, **args)["data"]["next_cursor"]
    board = next(project.glob("*.kicad_pcb"))
    board.write_bytes(board.read_bytes() + b"\n")
    srv.fail("kw_pcb_get", "E_STALE", project=pro, cursor=first, limit=37, **args)


@pytest.mark.parametrize("line", LINES)
def test_sch_get_netlist_leaves_no_net_file_live(
    line: str, installs: list[Any], storage_home: Path, tmp_path: Path
):
    install = _stock(installs, line)
    project = demo_copy(install, "pic_programmer", tmp_path)
    pro = str(next(project.glob("*.kicad_pro")))
    srv = Server(storage_home / f"net-{line}", [install])
    before = sorted(p.name for p in project.iterdir())
    sc = srv.ok("kw_sch_get", project=pro, kinds=["netlist"], provider="cli", limit=5)
    nl = sc["data"]["netlist"]
    assert nl["provider"].startswith("cli:") and nl["nets_total"] > 5 and nl["truncated"] is True
    assert sc["data"]["next_cursor"] == nl["next_cursor"] and ".n5@" in nl["next_cursor"]
    # the file provider joins pin nets through the same (detached) export
    srv.ok("kw_sch_get", project=pro, kinds=["symbols"], provider="file", limit=5)
    after = sorted(p.name for p in project.iterdir() if p.name != ".kiwrap")
    assert not list(project.glob("*.net")), "the READ_ONLY kw_sch_get must not leave a netlist"
    assert after == [n for n in before if n != ".kiwrap"]
