"""Review items 3, 6, 19, 23 (+ the kw_sch_get `.net` leftover) against faked lower layers.

requests/kiwrap-feature-requests.review.md:
- item 3: `kw_pcb_drc(action='report', ...)` re-reads the stored result of the last run without
  KiCad; E_STALE when the board changed or was never checked; the result lives under the storage
  home, never in the project.
- item 6 / B3: the shared cursor skipped unrouted airwires (and netlist nets) whenever the two lists
  stopped at different offsets; the composite `i<n>.u<n>@<sha8>` cursor walks both completely, a
  cursor from an older board is E_STALE, and `out_path` / `format='ndjson'` write everything in one
  call. The page is measured as the receipt text renders it, so the text body is never cut.
- item 19: `all_track_errors` reaches kicad-cli (default on where the flag exists).
- item 23: `ignored_checks` with `project` / `kicad_default` attribution.
- §3 small defects: `kw_sch_get(kinds=['netlist'])` left `<root>.net` in the project;
  `provider='cli'` was E_BAD_ARGS; `violations_truncated` counted excluded markers; `class` last.

Each bug is reproduced first (the old algorithm, or the old observable behaviour) and then shown
fixed through the real MCP tool handlers.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from kiwrap.core.state.receipts import MAX_TEXT_CHARS
from kiwrap.core.tools import _data
from kiwrap.core.tools import checks as checks_mod
from kiwrap.core.tools._data import Lane, chunk_by_budget, page_lanes, parse_multi_cursor, rendered_size
from kiwrap.formats.reports import Net as NlNet
from kiwrap.formats.reports import Netlist, NetNode, Position, Report, Violation, ViolationItem
from kiwrap.formats.reports.drc import (
    ignored_checks_from_json,
    ignored_rule_keys,
    included_severities_from_json,
    item_net,
    item_ref,
    parse_drc_text,
)
from tests.test_core_datatools import DataBackend, fake_board, fake_schematic
from tests.test_core_fakes import CAPS_10, FakeBackend, FakeInstall
from tests.test_core_tools import Harness

CORPUS = Path(__file__).resolve().parents[1] / "conformance" / "corpus"

# ---- fixtures -----------------------------------------------------------------------------------


def v(
    vtype: str,
    severity: str = "error",
    *items: tuple[str, str, float, float],
    excluded: bool = False,
    desc: str | None = None,
) -> Violation:
    """A DRC violation whose items are (uuid, description, x, y)."""
    return Violation(
        type=vtype,
        severity=severity,
        description=desc or f"{vtype} violation",
        items=tuple(ViolationItem(uuid=u, description=d, pos=Position(x, y)) for u, d, x, y in items),
        excluded=excluded,
    )


def sample_report(*, raw: dict[str, Any] | None = None) -> Report:
    return Report(
        kind="drc",
        source="demo.kicad_pcb",
        kicad_version="10.0.6",
        violations=(
            v(
                "clearance",
                "error",
                ("u1", "PTH pad 2 [GND] of R4", 10, 10),
                ("u2", "Track [VCC] on F.Cu", 11, 10),
            ),
            v(
                "shorting_items",
                "error",
                ("u3", "Pad 1 [GND] of U1", 50, 50),
                ("u4", "Via [VCC] on F.Cu - B.Cu", 50, 51),
            ),
            v("courtyards_overlap", "error", ("u5", "Footprint U1", 49, 49), ("u6", "Footprint R4", 12, 12)),
            v("silk_overlap", "warning", ("u7", "Reference field of C3", 80, 5)),
            v("hole_clearance", "error", ("u8", "NPTH pad of H1", 5, 90), excluded=True),
            v("lib_footprint_issues", "warning", ("u9", "Footprint J2", 70, 70)),
        ),
        unconnected=(
            v(
                "unconnected_items",
                "error",
                ("u10", "Pad 2 [SIG] of R4", 12, 10),
                ("u11", "Pad 1 [SIG] of C3", 80, 6),
            ),
        ),
        raw=raw
        if raw is not None
        else {
            "ignored_checks": [
                {
                    "key": "footprint_filters_mismatch",
                    "description": "Footprint doesn't match symbol's footprint filters",
                },
                {"key": "track_not_centered_on_via", "description": "Track endpoint not centered on via"},
                {"key": "hole_clearance", "description": "Hole clearance"},
            ],
            "included_severities": ["error", "warning", "exclusion"],
        },
    )


def set_rule_severities(project_dir: Path, severities: dict[str, str]) -> None:
    pro = next(Path(project_dir).glob("*.kicad_pro"))
    doc = json.loads(pro.read_text(encoding="utf-8"))
    doc.setdefault("board", {}).setdefault("design_settings", {})["rule_severities"] = severities
    pro.write_text(json.dumps(doc, indent=2), encoding="utf-8")


@pytest.fixture
def drc_h(tmp_path: Path) -> tuple[Harness, str]:
    backend = FakeBackend()
    backend.drc = sample_report()
    h = Harness(tmp_path, backend)
    return h, h.create()


def run_drc_calls(h: Harness) -> list[dict[str, Any]]:
    return [c[1] for c in h.backend.calls if c[0] == "run_drc"]


# ---- item 3: the stored result and action='report' ------------------------------------------------


def test_drc_run_stores_the_complete_result_outside_the_project(drc_h: tuple[Harness, str]):
    h, proj = drc_h
    real = h.backend.run_drc

    def writes_json(install: Any, config: Any, board: Path, **kw: Any) -> Any:
        # like kicad-cli: the raw JSON lands where -o points (the backend default was <board>.drc.json)
        target = kw.get("out_json") or Path(board).with_suffix(".drc.json")
        Path(target).write_text('{"violations": []}', encoding="utf-8")
        return real(install, config, board, **kw)

    h.ctx.services.run_drc = writes_json  # type: ignore[method-assign]
    sc = h.ok("kw_pcb_drc", project=proj)
    d = sc["data"]
    assert d["result"]["stored"] is True and d["result"]["violations"] == 7
    stored = Path(d["result"]["path"])
    assert stored.is_file() and h.home in stored.parents
    raw = Path(d["result"]["raw_json"])
    assert raw.is_file() and h.home in raw.parents and raw.name.endswith(".drc.json")
    assert not list(Path(proj).glob("*.drc.json")), "no DRC report is left in the project (D23)"
    out_json = run_drc_calls(h)[-1]["out_json"]
    assert h.home in Path(out_json).parents
    doc = json.loads(stored.read_text(encoding="utf-8"))
    assert doc["schema"] == "kiwrap.drc_result.v1" and doc["board_sha256"] == d["result"]["board_sha256"]
    assert len(doc["violations"]) == 7  # every violation incl. the excluded one
    # the run's own list: class first (item 20), refs joined from KiCad's item descriptions
    first = d["violations"][0]
    assert next(iter(first)) == "class" and first["refs"] == ["R4"]
    assert d["excluded"] == 1 and d["violations_truncated"] == 0


def test_drc_report_rereads_filters_and_pages_without_kicad(drc_h: tuple[Harness, str]):
    h, proj = drc_h
    h.ok("kw_pcb_drc", project=proj)
    runs = len(run_drc_calls(h))
    state = Path(proj) / ".kiwrap" / "state.json"
    before = state.read_bytes()

    rep = h.ok("kw_pcb_drc", project=proj, action="report")
    d = rep["data"]
    assert rep["provider_used"].startswith("stored:") and d["fresh"] is True
    assert d["total"] == 6 and [x["class"] for x in d["violations"]][:3] == ["must_fix"] * 3
    assert d["summary"]["must_fix"] == 3 and d["ignored_checks_source"] == "kicad"
    assert len(run_drc_calls(h)) == runs, "report must not run KiCad"
    assert state.read_bytes() == before, "report writes nothing into the project"

    def total(**f: Any) -> int:
        return h.ok("kw_pcb_drc", project=proj, action="report", **f)["data"]["total"]

    assert total(types=["clearance", "shorting_items"]) == 2
    assert total(classes=["review"]) == 2 and total(classes=["cosmetic"]) == 1
    assert total(refs=["R4"]) == 3  # clearance, courtyards_overlap, unconnected_items
    assert total(nets=["VCC"]) == 2
    assert total(bbox_mm=[45, 45, 55, 55]) == 2  # shorting_items + the U1 end of courtyards_overlap
    assert total(severity="warning") == 2
    ex = h.ok("kw_pcb_drc", project=proj, action="report", severity="exclusions")["data"]
    assert ex["total"] == 1 and ex["violations"][0]["type"] == "hole_clearance"
    # paging: one opaque cursor, versioned by the board
    p1 = h.ok("kw_pcb_drc", project=proj, action="report", limit=4)["data"]
    assert p1["returned"] == 4 and p1["next_cursor"].startswith("v4@")
    p2 = h.ok("kw_pcb_drc", project=proj, action="report", limit=4, cursor=p1["next_cursor"])["data"]
    assert p2["returned"] == 2 and p2["next_cursor"] is None
    seen = [json.dumps(x, sort_keys=True) for x in p1["violations"] + p2["violations"]]
    assert len(set(seen)) == 6
    # run-only arguments are named as ignored
    w = h.ok("kw_pcb_drc", project=proj, action="report", save_board=True)["warnings"]
    assert any("save_board is ignored by action='report'" in x for x in w)


def test_drc_report_is_stale_without_a_run_and_after_a_board_edit(drc_h: tuple[Harness, str]):
    h, proj = drc_h
    sc = h.fail("kw_pcb_drc", "E_STALE", project=proj, action="report")
    assert sc["data"]["reason"] == "no_result" and "action='run'" in sc["fix"]
    h.ok("kw_pcb_drc", project=proj)
    cursor = h.ok("kw_pcb_drc", project=proj, action="report", limit=2)["data"]["next_cursor"]
    board = next(Path(proj).glob("*.kicad_pcb"))
    board.write_bytes(board.read_bytes() + b"\n")
    sc = h.fail("kw_pcb_drc", "E_STALE", project=proj, action="report")
    assert sc["data"]["reason"] == "board_changed"
    assert sc["data"]["stored_board_sha256"] != sc["data"]["current_board_sha256"]
    # re-running makes the report fresh again (the edit is picked up as an external edit) ...
    h.ok("kw_pcb_drc", project=proj)
    assert h.ok("kw_pcb_drc", project=proj, action="report")["data"]["fresh"] is True
    # ... and a cursor issued for the old board is stale, not silently shifted
    sc = h.fail("kw_pcb_drc", "E_STALE", project=proj, action="report", limit=2, cursor=cursor)
    assert sc["data"]["reason"] == "cursor_version"


def test_drc_violations_join_item_uuids_to_refs_and_nets(tmp_path: Path):
    """The board file model resolves uuids to footprint refs / nets (language-independent); the
    descriptions are only a fallback."""
    backend = DataBackend()
    backend.drc = Report(
        kind="drc",
        source="demo.kicad_pcb",
        kicad_version="10.0.6",
        violations=(
            v("clearance", "error", ("pad-2-30.8-10.0", "Pad", 30.8, 10.0), ("t-1", "Leiterbahn", 20.0, 9.6)),
        ),
    )
    h = Harness(tmp_path, backend)
    proj = h.create()
    d = h.ok("kw_pcb_drc", project=proj)["data"]
    assert d["violations"][0]["refs"] == ["R10"]
    rep = h.ok("kw_pcb_drc", project=proj, action="report", nets=["SIG"])["data"]
    assert rep["total"] == 1 and rep["violations"][0]["nets"] == ["SIG", "VCC"]
    assert [i["ref"] for i in rep["violations"][0]["items"]] == ["R10", None]


def test_drc_report_warns_when_the_rules_changed_since_the_run(drc_h: tuple[Harness, str]):
    h, proj = drc_h
    h.ok("kw_pcb_drc", project=proj)
    (Path(proj) / "demo.kicad_dru").write_text("(version 1)\n", encoding="utf-8")
    rep = h.ok("kw_pcb_drc", project=proj, action="report")
    assert rep["degraded"] == ["drc_rules_changed"]


def test_drc_store_keeps_a_bounded_number_of_results(
    drc_h: tuple[Harness, str], monkeypatch: pytest.MonkeyPatch
):
    h, proj = drc_h
    monkeypatch.setattr(checks_mod, "DRC_KEEP_RESULTS", 2)
    board = next(Path(proj).glob("*.kicad_pcb"))
    for _ in range(4):
        board.write_bytes(board.read_bytes() + b"\n")
        h.ok("kw_pcb_drc", project=proj)
    folder = Path(h.ok("kw_pcb_drc", project=proj)["data"]["result"]["path"]).parent
    results = [
        p for p in folder.glob("*.json") if p.name != "latest.json" and not p.name.endswith(".drc.json")
    ]
    assert len(results) == 2


def test_violations_truncated_counts_listed_violations_only(
    drc_h: tuple[Harness, str], monkeypatch: pytest.MonkeyPatch
):
    """§3 small defects (item 20): the excluded marker used to count as truncated."""
    h, proj = drc_h
    monkeypatch.setattr(checks_mod, "MAX_VIOLATIONS", 5)
    d = h.ok("kw_pcb_drc", project=proj)["data"]
    # 7 violations, 1 excluded: 6 listed, 5 shown -> 1 truncated (the old count said 2)
    assert len(d["violations"]) == 5 and d["violations_truncated"] == 1


# ---- item 23: ignored checks --------------------------------------------------------------------------


def test_ignored_checks_are_attributed_to_the_project_or_kicads_defaults(drc_h: tuple[Harness, str]):
    h, proj = drc_h
    set_rule_severities(
        Path(proj), {"footprint_filters_mismatch": "ignore", "hole_clearance": "ignore", "clearance": "error"}
    )
    sc = h.ok("kw_pcb_drc", project=proj)
    ic = {c["key"]: c["source"] for c in sc["data"]["ignored_checks"]}
    assert ic == {
        "footprint_filters_mismatch": "project",
        "track_not_centered_on_via": "kicad_default",
        "hole_clearance": "project",
    }
    assert sc["data"]["included_severities"] == ["error", "warning", "exclusion"]
    # hole_clearance is must_fix for kiwrap: KiCad skipping it is worth a warning
    assert any("hole_clearance" in w and "must_fix" in w for w in sc["warnings"])
    rep = h.ok("kw_pcb_drc", project=proj, action="report")["data"]
    assert rep["ignored_checks"] == sc["data"]["ignored_checks"]


def test_ignored_checks_on_a_kicad_that_does_not_report_them(tmp_path: Path):
    """8.0 / 9.0 JSON has no ignored_checks: only the project's own ignores, said so."""
    backend = FakeBackend()
    backend.drc = sample_report(raw={})
    h = Harness(tmp_path, backend)
    proj = h.create()
    set_rule_severities(Path(proj), {"missing_courtyard": "ignore", "clearance": "error"})
    sc = h.ok("kw_pcb_drc", project=proj)
    assert sc["data"]["ignored_checks"] == [
        {"key": "missing_courtyard", "description": "", "source": "project"}
    ]
    assert sc["data"]["ignored_checks_source"] == "project_only"
    assert any("does not report which checks it ignored" in w for w in sc["warnings"])


# ---- item 19: all_track_errors --------------------------------------------------------------------


def test_all_track_errors_defaults_on_where_the_flag_exists(drc_h: tuple[Harness, str]):
    h, proj = drc_h
    assert h.ok("kw_pcb_drc", project=proj)["data"]["all_track_errors"] is True
    assert run_drc_calls(h)[-1]["all_track_errors"] is True
    h.ok("kw_pcb_drc", project=proj, all_track_errors=False)
    assert run_drc_calls(h)[-1]["all_track_errors"] is False


def test_all_track_errors_is_capability_gated(tmp_path: Path):
    caps = {**CAPS_10, "cli.flags.pcb drc": ["--format", "--output", "--refill-zones"]}
    backend = FakeBackend(installs=[FakeInstall()], caps={"10.0.0": caps})
    backend.drc = sample_report()
    h = Harness(tmp_path, backend)
    proj = h.create()
    sc = h.ok("kw_pcb_drc", project=proj)
    assert sc["data"]["all_track_errors"] is False and "all_track_errors_unsupported" not in sc["degraded"]
    sc = h.ok("kw_pcb_drc", project=proj, all_track_errors=True)
    assert "all_track_errors_unsupported" in sc["degraded"]
    assert run_drc_calls(h)[-1]["all_track_errors"] is False


def test_backend_refuses_all_track_errors_without_the_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from kiwrap.backends.cli import drc as cli_drc
    from kiwrap.capability import Unsupported
    from tests.test_core_fakes import FakeCaps

    install = FakeInstall()
    without = FakeCaps({**CAPS_10, "cli.flags.pcb drc": ["--format", "--output"]}, install)
    with_flag = FakeCaps({**CAPS_10, "cli.flags.pcb drc": ["--format", "--all-track-errors"]}, install)
    assert not cli_drc.supports_all_track_errors(without) and cli_drc.supports_all_track_errors(with_flag)
    with pytest.raises(Unsupported):
        cli_drc.run_drc(install, None, tmp_path / "b.kicad_pcb", all_track_errors=True, caps=without)  # type: ignore[arg-type]
    seen: list[list[str]] = []

    def fake_cli(install: Any, args: list[str], **kw: Any) -> Any:
        seen.append(list(args))
        out = Path(args[args.index("-o") + 1])
        out.write_text(json.dumps({"coordinate_units": "mm", "violations": []}), encoding="utf-8")
        return type("R", (), {"returncode": 0, "violations": False})()

    monkeypatch.setattr(cli_drc, "run_kicad_cli", fake_cli)
    rep = cli_drc.run_drc(
        install,
        None,
        tmp_path / "b.kicad_pcb",
        all_track_errors=True,
        caps=with_flag,
        out_json=tmp_path / "o.json",  # type: ignore[arg-type]
    )
    assert "--all-track-errors" in seen[-1] and rep.raw["_cli"]["all_track_errors"] is True


# ---- item 6 / B3: one cursor for items and airwires -------------------------------------------------


def old_shared_cursor_walk(items: list[Any], unrouted: list[Any], limit: int, budget: int) -> list[Any]:
    """The pre-fix kw_pcb_get: both lists paged with the SAME offset and separate budgets,
    `next_cursor = items_cursor or airwire_cursor` (pcb_get.py:961-977 before the fix)."""
    cursor: str | None = None
    seen: list[Any] = []
    while True:
        p = chunk_by_budget(items, cursor, limit, budget_chars=budget)
        u = chunk_by_budget(unrouted, cursor, limit, budget_chars=budget)
        seen.extend(u.items)
        cursor = p.next_cursor or u.next_cursor
        if cursor is None:
            return seen


def airwire(i: int) -> dict[str, Any]:
    return {
        "net": f"NET_WITH_A_LONG_NAME_{i:03d}",
        "a": {"ref": f"U{i}", "pad": "1", "x_mm": float(i), "y_mm": 1.0},
        "b": {"ref": f"R{i}", "pad": "2", "x_mm": float(i), "y_mm": 2.0},
    }


def test_the_old_shared_cursor_skips_airwires():
    """B3 reproduced: small items fill `limit` while the bigger airwires hit the budget first; the
    next page starts both lists at the ITEMS offset and the airwires in between are never returned."""
    items = [{"kind": "net", "name": f"N{i}"} for i in range(60)]
    unrouted = [airwire(i) for i in range(60)]
    budget = 3 * rendered_size(airwire(0))  # three airwires per page, twenty nets
    seen = old_shared_cursor_walk(items, unrouted, 20, budget)
    assert len(seen) < len(unrouted), "the old algorithm must lose airwires"
    mp_seen: list[Any] = []
    cursor = None
    while True:
        mp = page_lanes(
            [Lane("i", items), Lane("u", unrouted, depth=4)],
            cursor,
            20,
            version="abcd1234",
            budget_chars=budget,
        )
        mp_seen.extend(mp.pages["u"])
        cursor = mp.next_cursor
        if cursor is None:
            break
    assert mp_seen == unrouted


def test_multi_cursor_parsing():
    assert parse_multi_cursor(None, ["i", "u"], "v") == {"i": 0, "u": 0}
    assert parse_multi_cursor("12", ["i", "u"], "v") == {"i": 12, "u": 12}  # the legacy shared cursor
    assert parse_multi_cursor("i3.u7@v", ["i", "u"], "v") == {"i": 3, "u": 7}
    assert parse_multi_cursor("i3", ["i", "u"], "v") == {"i": 3, "u": 0}
    from kiwrap.core.state import KiwrapError

    for bad in ("zz", "i3.x4@v", "i-1"):
        with pytest.raises(KiwrapError) as e:
            parse_multi_cursor(bad, ["i", "u"], "v")
        assert e.value.code == "E_BAD_ARGS", bad
    with pytest.raises(KiwrapError) as e:
        parse_multi_cursor("i3.u7@old", ["i", "u"], "new")
    assert e.value.code == "E_STALE" and e.value.data["reason"] == "cursor_version"


def many_board(n_nets: int = 60, n_air: int = 60) -> Any:
    b = fake_board()
    b.nets = [{"code": i + 1, "name": f"N{i:03d}", "pad_count": 2} for i in range(n_nets)]
    return b


def walk_pcb_get(h: Harness, project: str, **args: Any) -> tuple[list[Any], list[Any], int]:
    items: list[Any] = []
    unrouted: list[Any] = []
    cursor: str | None = None
    pages = 0
    while True:
        d = h.ok("kw_pcb_get", project=project, cursor=cursor, **args)["data"]
        pages += 1
        items.extend(d["items"])
        unrouted.extend(d["connectivity"]["unrouted"])
        if d["connectivity"]["next_cursor"] is not None:
            assert d["connectivity"]["next_cursor"] == d["next_cursor"]  # never a second, disagreeing cursor
        cursor = d["next_cursor"]
        if cursor is None:
            return items, unrouted, pages


def test_pcb_get_walks_items_and_airwires_completely(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    backend = DataBackend(board=many_board(), ratsnest=[airwire(i) for i in range(60)])
    h = Harness(tmp_path, backend)
    project = h.create()
    monkeypatch.setattr(_data, "PAGE_BUDGET_CHARS", 3 * rendered_size(airwire(0), 4) + 400)
    monkeypatch.setattr(_data, "MIN_PAGE_BUDGET_CHARS", 500)
    items, unrouted, pages = walk_pcb_get(h, project, kinds=["nets", "connectivity"], limit=20)
    assert [i["name"] for i in items] == [f"N{i:03d}" for i in range(60)]
    assert sorted(u["net"] for u in unrouted) == sorted(a["net"] for a in (airwire(i) for i in range(60)))
    assert len(unrouted) == 60 and pages >= 20
    # a cursor from before a board edit is stale, not silently shifted
    d = h.ok("kw_pcb_get", project=project, kinds=["nets", "connectivity"], limit=20)["data"]
    board = next(Path(project).glob("*.kicad_pcb"))
    board.write_bytes(board.read_bytes() + b"\n")
    sc = h.fail(
        "kw_pcb_get", "E_STALE", project=project, kinds=["nets", "connectivity"], cursor=d["next_cursor"]
    )
    assert sc["data"]["reason"] == "cursor_version"


def test_pcb_get_text_body_is_never_cut_and_names_the_cursor(tmp_path: Path):
    """The 40k page is measured as the receipt text renders it (it rendered 48.6-49.7k before), so
    the text stays below the 60k cap and carries the same next_cursor as structuredContent."""
    board = fake_board()
    board.nets = [
        {"code": i, "name": f"/very/long/hierarchical/sheet/path/net_{i:05d}", "pad_count": 3}
        for i in range(3000)
    ]
    h = Harness(tmp_path, DataBackend(board=board))
    project = h.create()
    err, sc, result = h.call("kw_pcb_get", project=project, kinds=["nets"], limit=2000, detail="full")
    assert not err
    text = result.content[0].text
    assert "text truncated" not in text and len(text) < MAX_TEXT_CHARS
    nxt = sc["data"]["next_cursor"]
    assert nxt and f"cursor={nxt!r}" in text
    assert sc["data"]["page_chars"] <= _data.PAGE_BUDGET_CHARS
    # page_chars is what the items really add to the text (the old indent-0 measure was ~25% low)
    body = json.dumps(sc, indent=1, default=str)
    without = json.dumps({**sc, "data": {**sc["data"], "items": []}}, indent=1, default=str)
    assert len(body) < MAX_TEXT_CHARS
    assert abs((len(body) - len(without)) - sc["data"]["page_chars"]) <= 16


def test_pcb_get_writes_the_complete_result_to_ndjson(tmp_path: Path):
    backend = DataBackend(board=many_board(), ratsnest=[airwire(i) for i in range(60)])
    h = Harness(tmp_path, backend)
    project = h.create()
    before = sorted(p.name for p in Path(project).iterdir() if p.name != ".kiwrap")
    sc = h.ok(
        "kw_pcb_get", project=project, kinds=["nets", "outline", "connectivity"], format="ndjson", limit=5
    )
    out = sc["data"]["out"]
    path = Path(out["path"])
    assert h.home in path.parents and path.suffix == ".ndjson"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    kinds = [r["kind"] for r in rows]
    assert kinds[0] == "header" and kinds.count("net") == 60 and kinds.count("unrouted") == 60
    assert "outline" in kinds and "connectivity" in kinds
    assert out["counts"] == {"items": 60, "unrouted": 60} and out["lines"] == len(rows)
    assert sc["data"]["items"] == [] and sc["data"]["next_cursor"] is None
    assert sorted(p.name for p in Path(project).iterdir() if p.name != ".kiwrap") == before
    # an explicit json file
    target = tmp_path / "board.json"
    sc = h.ok("kw_pcb_get", project=project, kinds=["nets"], out_path=str(target))
    doc = json.loads(target.read_text(encoding="utf-8"))
    assert len(doc["items"]) == 60 and sc["data"]["out"]["path"] == str(target)
    # a directory gets a generated name
    sc = h.ok("kw_pcb_get", project=project, kinds=["nets"], out_path=str(tmp_path))
    assert Path(sc["data"]["out"]["path"]).parent == tmp_path
    for bad in (
        "relative.json",
        str(tmp_path / "x.kicad_pcb"),
        str(tmp_path / "nope" / "x.json"),
        str(tmp_path / "x.exe"),
    ):
        h.fail("kw_pcb_get", "E_BAD_ARGS", project=project, kinds=["nets"], out_path=bad)


# ---- kw_sch_get: the netlist lane, the .net leftover, provider='cli' -------------------------------


class NetFileBackend(DataBackend):
    """export_netlist behaves like kicad-cli: it writes `out`, or `<root>.net` beside the schematic."""

    def export_netlist(
        self, install: Any, config: Any, schematic: Path, out: Path | None = None, **kw: Any
    ) -> Any:
        target = Path(out) if out else Path(schematic).with_suffix(".net")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("(export (version E))\n", encoding="utf-8")
        self._log("export_netlist", schematic=str(schematic), out=str(target))
        return self.net


def big_netlist(n: int = 50) -> Netlist:
    return Netlist(
        source="root.kicad_sch",
        kicad_version="10.0.0",
        components=(),
        nets=tuple(
            NlNet(code=i + 1, name=f"NET{i:03d}", nodes=(NetNode("R1", "1"), NetNode("R2", str(i))))
            for i in range(n)
        ),
    )


def test_sch_get_netlist_leaves_no_net_file_in_the_project(tmp_path: Path):
    """§3 small defects: the READ_ONLY kw_sch_get left `<root>.net` (319 KB) in the project."""
    backend = NetFileBackend(net=big_netlist(3))
    h = Harness(tmp_path, backend)
    project = h.create()
    # the bug, reproduced with the backend's own default: no `out` -> a .net beside the schematic
    root = next(Path(project).glob("*.kicad_sch"))
    backend.export_netlist(None, None, root)
    assert root.with_suffix(".net").is_file()
    root.with_suffix(".net").unlink()
    sc = h.ok("kw_sch_get", project=project, kinds=["netlist"])
    assert sc["data"]["netlist"]["provider"] == "cli:10.0.0"
    assert not list(Path(project).glob("*.net")), "kw_sch_get must not leave a netlist in the project"
    out = [c[1]["out"] for c in backend.calls if c[0] == "export_netlist"][-1]  # the tool's call
    assert h.home in Path(out).parents and not Path(out).exists() and not Path(out).parent.exists()


def test_sch_get_provider_cli_selects_the_cli_netlist(tmp_path: Path):
    """§3 small defects: kinds=['netlist'], provider='cli' was E_BAD_ARGS (schematic.read has no cli)."""
    h = Harness(tmp_path, NetFileBackend(net=big_netlist(3)))
    project = h.create()
    sc = h.ok("kw_sch_get", project=project, kinds=["netlist"], provider="cli")
    assert sc["data"]["netlist"]["provider"] == "cli:10.0.0" and sc["provider_used"] == "file"


def test_sch_get_walks_items_and_nets_completely(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    backend = NetFileBackend(net=big_netlist(50), schematic=fake_schematic())
    h = Harness(tmp_path, backend)
    project = h.create()
    first = {
        "name": "NET000",
        "code": 1,
        "nodes": [{"ref": "R1", "pin": "1", "sheet": None}, {"ref": "R2", "pin": "0", "sheet": None}],
    }
    monkeypatch.setattr(_data, "PAGE_BUDGET_CHARS", 4 * rendered_size(first, 4) + 400)
    monkeypatch.setattr(_data, "MIN_PAGE_BUDGET_CHARS", 300)
    nets: list[str] = []
    items: list[Any] = []
    cursor = None
    while True:
        d = h.ok(
            "kw_sch_get",
            project=project,
            kinds=["symbols", "netlist"],
            detail="summary",
            limit=10,
            cursor=cursor,
        )["data"]
        nets.extend(n["name"] for n in d["netlist"]["nets"])
        items.extend(d["items"])
        cursor = d["next_cursor"]
        if cursor is None:
            break
        assert cursor.startswith("i") and ".n" in cursor and "@" in cursor
    assert nets == [f"NET{i:03d}" for i in range(50)]
    assert len({json.dumps(i, sort_keys=True) for i in items}) == len(items) > 0


# ---- formats: ignored checks and item descriptions -------------------------------------------------------


def test_formats_ignored_checks_and_severities_from_the_json():
    doc10 = json.loads((CORPUS / "10.0" / "headless_board" / "drc.json").read_text(encoding="utf-8"))
    keys = [c.key for c in ignored_checks_from_json(doc10) or ()]
    assert "missing_courtyard" in keys and "footprint_type_mismatch" in keys
    assert included_severities_from_json(doc10) == ("error", "warning", "exclusion")
    doc9 = json.loads((CORPUS / "9.0" / "tiny_routed" / "drc.json").read_text(encoding="utf-8"))
    assert ignored_checks_from_json(doc9) is None and included_severities_from_json(doc9) is None


def test_formats_text_report_ignored_section_and_rule_keys():
    text = (
        "** Drc report for b.kicad_pcb **\n** Report includes: Errors, Warnings **\n\n"
        "** Found 1 DRC violations **\n[silk_overlap]: Silkscreen overlap\n    Rule: x; warning\n"
        "    @(1.0000 mm, 2.0000 mm): Reference field of U1\n\n** Found 0 unconnected pads **\n\n"
        "** Found 0 Footprint errors **\n\n** Ignored checks **\n    - Footprint has no courtyard defined\n"
        "    - Track endpoint not centered on via\n\n** End of Report **\n"
    )
    r = parse_drc_text(text)
    assert len(r.violations) == 1
    assert [c["description"] for c in r.raw["ignored_checks"]] == [
        "Footprint has no courtyard defined",
        "Track endpoint not centered on via",
    ]
    pro = {"board": {"design_settings": {"rule_severities": {"a": "ignore", "b": "error", "c": "Ignore"}}}}
    assert ignored_rule_keys(pro) == ("a", "c") and ignored_rule_keys({}) == ()


@pytest.mark.parametrize(
    ("text", "ref", "net"),
    [
        ("PTH pad 2 [GND] of R4", "R4", "GND"),
        ("NPTH pad of X", "X", None),
        ("Footprint U3", "U3", None),
        ("Line of U8 on F.SilkS", "U8", None),
        ("Track [GND] on F.Cu, length 1.2000 mm", None, "GND"),
        ("Zone [<no net>] on F.Cu", None, None),
        ("Via [/USB/D+] on F.Cu - B.Cu", None, "/USB/D+"),
    ],
)
def test_formats_item_ref_and_net(text: str, ref: str | None, net: str | None):
    assert item_ref(text) == ref and item_net(text) == net
