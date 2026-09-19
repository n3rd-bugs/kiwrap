"""The FTS index on the fixture libraries: scanning (cross-checked against the CST parsers),
incremental rebuilds, scopes, search strategies, filters and near-misses. No KiCad needed."""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

import pytest

from kiwrap.formats.libs import LibIndex, LibResolver, SymbolLibrary, load_footprint
from kiwrap.formats.libs.index import (
    filter_regex,
    footprint_filter_matches,
    scan_footprint_text,
    scan_symbol_lib_text,
    tokens_of,
)
from tests.test_libs_tables import FIXTURES, StubInstall, env_for


def make_index(tmp_path: Path, project: Path | None = None) -> LibIndex:
    resolver = LibResolver(StubInstall(), FIXTURES, project, env=env_for(project))
    return LibIndex(StubInstall(), FIXTURES, tmp_path / "home", project_dir=project, resolver=resolver)


# ---- scanners ---------------------------------------------------------------------------------------


def test_symbol_scanner_matches_the_cst_parser():
    text = (FIXTURES / "Test.kicad_sym").read_text(encoding="utf-8")
    recs = {r.name: r for r in scan_symbol_lib_text(text)}
    lib = SymbolLibrary.load(FIXTURES / "Test.kicad_sym", nickname="Test")
    assert list(recs) == lib.names()
    for name in ("Base", "Derived", "Chain", "Pwr"):
        sym = lib.symbol(name)
        rec = recs[name]
        assert (rec.pin_count, rec.units, rec.power) == (sym.pin_count, sym.units, sym.is_power), name
        assert rec.description == sym.description and rec.keywords == sym.keywords, name
        assert tuple(rec.fp_filters.split()) == sym.footprint_filters, name
        assert rec.properties.get("Footprint", "") == sym.footprint and rec.extends == sym.extends, name
    assert recs["Derived"].properties["MPN"] == "DER-1" and recs["Chain"].properties["Color"] == "blue"
    assert recs["Orphan"].extends == "Missing" and recs["Orphan"].pin_count == 0
    assert recs["Cyc1"].pin_count == 0  # cycle: no crash


def test_footprint_scanner_matches_the_cst_parser():
    for name in ("TP_2", "TH_4"):
        text = (FIXTURES / "Test.pretty" / f"{name}.kicad_mod").read_text(encoding="utf-8")
        rec = scan_footprint_text(text, name)
        fp = load_footprint(FIXTURES / "Test.pretty", name)
        assert rec.name == fp.name and rec.pad_count == fp.pad_count and rec.pads_total == len(fp.pads)
        assert rec.description == fp.description and rec.tags == fp.tags and rec.attributes == fp.attributes
    assert scan_footprint_text('(footprint "Lib:X" (layer "F.Cu"))').name == "X"


def test_tokens_and_filter_regex():
    assert tokens_of("R_0603_1608Metric") == ["r", "0603", "1608metric"]
    assert tokens_of("ESP32-S3-WROOM-1") == ["esp32", "s3", "wroom", "1"]
    assert tokens_of("  ") == [] and tokens_of("a a A") == ["a"]
    rx, with_nick = filter_regex("SOIC*3.9x4.9mm*P1.27mm*")
    assert (
        not with_nick and rx.match("soic-8_3.9x4.9mm_p1.27mm") and not rx.match("xsoic-8_3.9x4.9mm_p1.27mm")
    )
    rx2, with_nick2 = filter_regex("Package_SO:SOIC-?_*")
    assert with_nick2 and rx2.match("package_so:soic-8_3.9x4.9mm") and not rx2.match("package_so:soic-16_x")
    ids = [
        "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm",
        "Resistor_SMD:R_0603_1608Metric",
        "Package_DIP:DIP-8_W7.62mm",
    ]
    assert footprint_filter_matches(["DIP*W7.62mm*", "R_*"], ids) == ids[1:]
    assert footprint_filter_matches([], ids) == []


# ---- build / incremental ------------------------------------------------------------------------------


def test_build_is_incremental_and_ensure_is_cheap(tmp_path: Path):
    idx = make_index(tmp_path)
    rep = idx.build()
    assert not rep.skipped
    # unusable rows are reported, not fatal
    assert any(p.startswith("Gone:") for p in rep.problems) and any(
        "NO_SUCH_VAR_KIWRAP" in p for p in rep.problems
    )
    # usable rows only: Test, Hidden, Inner (sym) + Test (fp); disabled / legacy / missing rows skipped
    assert rep.libraries == 4 and rep.scanned == 4 and rep.symbols == 21 and rep.footprints == 2
    assert idx.exists() and idx.stats()["symbols"] == 21 and idx.stats()["libraries"] == 4
    rep2 = idx.ensure()
    assert rep2.skipped
    rep3 = idx.build()
    assert rep3.scanned == 0 and rep3.unchanged == 4 and rep3.removed == 0
    rep4 = idx.build(force=True)
    assert rep4.scanned == 4 and rep4.symbols == 21


def test_rebuild_tracks_file_and_table_changes(tmp_path: Path):
    root = tmp_path / "libs"
    shutil.copytree(FIXTURES, root)
    lib_path = root / "Test.kicad_sym"
    env = {"KIWRAP_TEST_LIBS": str(root)}
    resolver = LibResolver(StubInstall(), root, None, env=env)
    idx = LibIndex(StubInstall(), root, tmp_path / "home", resolver=resolver)
    idx.build()
    assert idx.search("symbol", "Base", {"library": "Test"}, limit=5)[0].id == "Test:Base"
    # edit the library file: only that library is rescanned and the new symbol is searchable
    text = lib_path.read_text(encoding="utf-8").replace(
        '(symbol "Pwr"', '(symbol "Added"\n\t\t(property "Value" "Added" (at 0 0 0))\n\t)\n\t(symbol "Pwr"'
    )
    lib_path.write_text(text, encoding="utf-8")
    future = time.time() + 5
    os.utime(lib_path, (future, future))
    rep = idx.build()
    assert (
        rep.scanned == 3 and rep.unchanged == 1
    )  # the three sym rows share the file; the .pretty is unchanged
    assert idx.search("symbol", "Added", {"library": "Test"}, limit=5)[0].id == "Test:Added"
    # remove a table row: its rows disappear, ensure() notices the table digest change
    table = root / "sym-lib-table"
    table.write_text(
        "\n".join(
            line
            for line in table.read_text(encoding="utf-8").splitlines()
            if '"Inner"' not in line and '"Nested"' not in line
        )
        + "\n",
        encoding="utf-8",
    )
    os.utime(table, (future + 5, future + 5))
    rep = idx.ensure()
    assert not rep.skipped and rep.removed == 1
    assert idx.search("symbol", "Base", {"library": "Inner"}, limit=5) == []
    # a new footprint file changes the .pretty digest
    (root / "Test.pretty" / "New_1.kicad_mod").write_text(
        '(footprint "New_1" (version 20260206) (generator "pcbnew") (layer "F.Cu") (descr "new one") (attr smd)\n'
        '\t(pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu" "F.Mask")))\n',
        encoding="utf-8",
    )
    rep = idx.build()
    assert rep.scanned == 1 and idx.search("footprint", "New_1", limit=3)[0].pad_count == 1


def test_project_scope_rows_and_shadowing(tmp_path: Path):
    project = FIXTURES / "project"
    idx = make_index(tmp_path, project)
    rep = idx.build()
    assert (
        rep.libraries == 3 + 4
    )  # Test, Inner, Test.pretty (global) + Hidden (project, shadowing), Proj, TextVar, ProjFp
    hits = idx.search("symbol", "Base", limit=20)
    nicks = {h.nickname for h in hits}
    assert {"Test", "Inner", "Proj", "TextVar", "Hidden"} <= nicks
    # the project's "Hidden" row (not hidden) replaced the global hidden row -> visible without include_hidden
    hidden_hits = [h for h in hits if h.nickname == "Hidden" and h.name == "Base"]
    assert len(hidden_hits) == 1 and not hidden_hits[0].hidden
    assert idx.search("footprint", "TP_2", {"library": "ProjFp"}, limit=5)[0].id == "ProjFp:TP_2"
    # the global-only index built from the same database does not see project rows
    global_idx = make_index(tmp_path)
    assert global_idx.exists()
    global_idx.build()
    assert {h.nickname for h in global_idx.search("symbol", "Base", limit=20)} == {"Test", "Inner"}
    assert global_idx.stats()["libraries"] == 4


# ---- search -------------------------------------------------------------------------------------------


def test_search_strategies_filters_and_near_misses(tmp_path: Path):
    idx = make_index(tmp_path)
    idx.build()
    res = idx.query("symbol", "Derived")
    # name matches rank above keyword matches (Chain inherits the "derived test" keywords)
    assert res.strategy == "exact" and [h.name for h in res.hits] == ["Derived", "Derived", "Chain", "Chain"]
    assert {h.nickname for h in res.hits} == {"Test", "Inner"}
    assert (
        res.hits[0].pin_count == 4 and res.hits[0].units == 2 and res.hits[0].footprint_filters == ("TP_2",)
    )
    assert res.hits[0].as_dict()["kind"] == "symbol" and "pin_count" in res.hits[0].as_dict()
    # hidden rows are near-misses unless include_hidden
    assert all(h.nickname != "Hidden" for h in res.hits)
    assert any(h.nickname == "Hidden" and h.miss and "hidden" in h.miss for h in res.near_misses)
    assert any(h.nickname == "Hidden" for h in idx.search("symbol", "Derived", {"include_hidden": True}))
    # keyword / description tokens
    assert idx.search("symbol", "two units", {"library": "Test"})[0].id == "Test:Base"
    assert idx.search("symbol", "power-flag", {"library": "Test"})[0].id == "Test:Pwr"
    # prefix fallback
    res = idx.query("symbol", "Deriv", {"library": "Test"})
    assert res.strategy == "prefix" and res.hits[0].id == "Test:Derived"
    # pins filter -> near-miss with the reason
    res = idx.query("symbol", "Base", {"library": "Test", "pins": 3})
    assert res.hits == [] and res.near_misses[0].miss == "4 pins (wanted 3)"
    assert idx.query("symbol", "Base", {"library": "Test", "pins": 4}).hits[0].id == "Test:Base"
    assert (
        idx.query("symbol", "Base", {"library": "Test", "pins_min": 5}).near_misses[0].miss
        == "4 pins (wanted >= 5)"
    )
    # power / units / package / keywords filters
    assert [h.id for h in idx.search("symbol", "", {"library": "Test", "power": True})] == ["Test:Pwr"]
    assert (
        idx.query("symbol", "Pwr", {"library": "Test", "power": False}).near_misses[0].miss == "power symbol"
    )
    assert (
        idx.query("symbol", "Base", {"library": "Test", "units": 1}).near_misses[0].miss
        == "2 units (wanted 1)"
    )
    assert idx.search("symbol", "Base", {"library": "Test", "package": "TH_"})[0].id == "Test:Base"
    miss = idx.query("symbol", "Derived", {"library": "Test", "package": "SOIC"}).near_misses[0].miss
    assert miss is not None and "SOIC" in miss
    miss = idx.query("symbol", "Base", {"library": "Test", "keywords": ["base", "nope"]}).near_misses[0].miss
    assert miss == "keywords missing: nope"
    # partial matches when no strategy matches every token
    res = idx.query("symbol", "Base zzz", {"library": "Test"})
    assert res.strategy == "any" and res.hits == [] and res.near_misses[0].miss == "partial match (any)"
    res = idx.query("symbol", "Derivedx", {"library": "Test"})
    assert res.strategy == "fuzzy" and res.near_misses[0].id == "Test:Derived"
    assert idx.query("symbol", "qqqqqq").strategy == "none"
    # browse (no query): filters only, name order
    browse = idx.query("symbol", "", {"library": "Test"})
    assert browse.strategy == "browse" and [h.name for h in browse.hits][:3] == ["Base", "Chain", "Cyc1"]
    with pytest.raises(ValueError):
        idx.query("net", "x")


def test_footprint_search_and_filters(tmp_path: Path):
    idx = make_index(tmp_path)
    idx.build()
    res = idx.query("footprint", "TP_2")
    assert res.hits[0].id == "Test:TP_2" and res.hits[0].pad_count == 2 and res.hits[0].attributes == ("smd",)
    assert res.hits[0].as_dict()["pad_count"] == 2 and "pin_count" not in res.hits[0].as_dict()
    assert idx.search("footprint", "tht")[0].id == "Test:TH_4"
    assert idx.query("footprint", "TH_4", {"smd": True}).near_misses[0].miss == "through-hole footprint"
    assert idx.query("footprint", "TP_2", {"through_hole": True}).near_misses[0].miss == "SMD footprint"
    assert idx.query("footprint", "TH_4", {"pins": 2}).near_misses[0].miss == "5 pads (wanted 2)"
    assert idx.search("footprint", "TH_4", {"pins": 5})[0].id == "Test:TH_4"
    assert idx.search("footprint", "test", {"package": "TP"})[0].id == "Test:TP_2"
    assert idx.search("footprint", "test", {"package": "nope"}) == []
    assert idx.footprint_ids() == ["Test:TH_4", "Test:TP_2"]
    matches = idx.footprints_for(("TP_*", "TH_?"), pin_count=2)
    assert [m["id"] for m in matches] == ["Test:TP_2", "Test:TH_4"]  # exact pad-count match first
    assert matches[0]["pin_count_match"] and not matches[1]["pin_count_match"]
    assert idx.footprints_for(()) == []
    row = idx.symbol_row("Test:Base")
    assert row is not None and row["pin_count"] == 4 and row["footprint"] == "Test:TP_2"
    assert idx.symbol_row("Test:Nope") is None


def test_query_builds_when_missing_and_schema_mismatch_resets(tmp_path: Path):
    idx = make_index(tmp_path)
    assert not idx.exists() and idx.stats()["symbols"] == 0
    assert idx.search("symbol", "Base", {"library": "Test"})[0].id == "Test:Base"
    assert idx.exists()
    import sqlite3

    con = sqlite3.connect(str(idx.db_path))
    con.execute("UPDATE meta SET value='0' WHERE key='schema'")
    con.commit()
    con.close()
    assert idx.stats()["symbols"] == 0  # reset on open
    assert idx.ensure().symbols == 21
