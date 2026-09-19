"""kw_lib_search / kw_lib_info through the in-process MCP server against the fixture libraries
(faked installs and sandbox; no KiCad)."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent

from kiwrap.core.server import build_server
from kiwrap.core.state.jobs import JobQueue
from kiwrap.core.state.receipts import ReceiptOut
from kiwrap.core.tools import TOOL_NAMES
from kiwrap.core.tools import help as help_tool
from kiwrap.core.tools._base import ToolContext
from kiwrap.formats.libs import LibIndex, LibResolver
from tests.test_core_fakes import FakeBackend
from tests.test_libs_tables import FIXTURES, StubInstall, env_for

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH (kw_project snapshots)")


class Harness:
    def __init__(self, tmp_path: Path):
        self.home = tmp_path / "home"
        backend = FakeBackend()
        services = backend.services(self.home)

        def lib_index(
            install: Any, config: Any, storage_home: Path, *, project_dir: Path | None = None
        ) -> LibIndex:
            resolver = LibResolver(StubInstall(), FIXTURES, project_dir, env=env_for(project_dir))
            return LibIndex(StubInstall(), FIXTURES, storage_home, project_dir=project_dir, resolver=resolver)

        services.lib_index = lib_index  # type: ignore[attr-defined]  # the optional Services hook
        self.ctx = ToolContext(services=services, jobs=JobQueue(self.home / "jobs"), server_version="test")
        self.server = build_server(context=self.ctx)

    def call(self, tool: str, **args: Any) -> tuple[bool, dict[str, Any]]:
        try:
            result = anyio.run(self.server.call_tool, tool, args)
        except ToolError as exc:
            result = CallToolResult(content=[TextContent(type="text", text=str(exc))], is_error=True)
        return result.is_error, result.structured_content

    def ok(self, tool: str, **args: Any) -> dict[str, Any]:
        err, sc = self.call(tool, **args)
        assert not err, sc
        ReceiptOut.model_validate(sc)
        return sc

    def fail(self, tool: str, code: str, **args: Any) -> dict[str, Any]:
        err, sc = self.call(tool, **args)
        assert err, sc
        assert sc["code"] == code, sc
        return sc


@pytest.fixture
def h(tmp_path: Path) -> Harness:
    return Harness(tmp_path)


def test_catalog_and_help_pages_include_the_lib_tools():
    assert "kw_lib_search" in TOOL_NAMES and "kw_lib_info" in TOOL_NAMES
    assert TOOL_NAMES.index("kw_lib_search") < TOOL_NAMES.index("kw_sch_get")
    assert set(help_tool.TOOL_PAGES) >= {"kw_lib_search", "kw_lib_info"}
    assert {"E_LIB_NOT_FOUND", "E_LIB_UNSUPPORTED", "E_LIB"} <= set(help_tool.ERROR_PAGES)
    text, _ = help_tool.page("kw_lib_search")
    assert "near-misses" in text


def test_lib_search_hits_near_misses_and_index_lifecycle(h: Harness):
    sc = h.ok("kw_lib_search", kind="symbol", query="Base", filters={"library": "Test"}, limit=5)
    d = sc["data"]
    assert sc["provider_used"] == "file" and sc["next_suggested"] == "kw_lib_info"
    assert d["kicad"] == "10.0.0" and d["strategy"] == "exact" and d["build"] is not None
    assert d["hits"][0]["id"] == "Test:Base" and d["hits"][0]["pin_count"] == 4 and d["hits"][0]["units"] == 2
    assert d["hits"][0]["footprint_filters"] == ["TP_*", "TH_?"]
    assert d["index"]["symbols"] == 21 and d["index"]["footprints"] == 2
    # the second call finds the index up to date
    sc2 = h.ok("kw_lib_search", kind="symbol", query="Base", filters={"library": "Test", "pins": 3}, limit=5)
    assert sc2["data"]["build"] is None
    assert sc2["data"]["hits"] == [] and sc2["data"]["near_misses"][0]["miss"] == "4 pins (wanted 3)"
    # footprints
    sc3 = h.ok("kw_lib_search", kind="footprint", query="TP_2")
    assert sc3["data"]["hits"][0]["id"] == "Test:TP_2" and sc3["data"]["hits"][0]["pad_count"] == 2
    assert sc3["data"]["hits"][0]["attributes"] == ["smd"]
    # nothing matches -> a hint in warnings
    sc4 = h.ok("kw_lib_search", kind="symbol", query="zzzzzz")
    assert sc4["data"]["strategy"] == "none" and any("no match" in w for w in sc4["warnings"])
    # rebuild
    sc5 = h.ok("kw_lib_search", kind="symbol", query="Pwr", rebuild=True)
    assert sc5["data"]["build"]["scanned"] == 4 and sc5["data"]["hits"][0]["power"] is True


def test_lib_search_argument_errors(h: Harness):
    sc = h.fail("kw_lib_search", "E_BAD_ARGS", kind="symbol", query="x", filters={"bogus": 1})
    assert "bogus" in sc["message"] and "pins" in sc["candidates"]
    h.fail("kw_lib_search", "E_BAD_ARGS", kind="net", query="x")
    h.fail("kw_lib_search", "E_BAD_ARGS", kind="symbol", query="x", limit=0)
    h.fail("kw_lib_search", "E_NO_KICAD", kind="symbol", query="x", target_kicad="99")


def test_lib_info_symbol(h: Harness):
    sc = h.ok("kw_lib_info", id="Test:Derived")
    d = sc["data"]
    assert d["kind"] == "symbol" and d["id"] == "Test:Derived" and d["node_text"] is None
    item = d["item"]
    assert item["extends"] == "Base" and item["units"] == 2 and item["pin_count"] == 4
    assert [p["number"] for p in item["pins"]] == ["1", "2", "3", "4"]  # every unit by default
    assert item["pins"][0] == {
        "number": "1",
        "name": "IN",
        "x_mm": -5.08,
        "y_mm": 2.54,
        "orientation": 0,
        "direction": "R",
        "electrical_type": "input",
        "unit": 1,
        "body_style": 1,
        "length_mm": 2.54,
        "shape": "line",
        "hidden": False,
    }
    assert [m["id"] for m in d["footprint_matches"]] == ["Test:TP_2"]  # Derived's filter is `TP_2`
    assert (
        d["footprint_matches"][0]["pad_count"] == 2 and d["footprint_matches"][0]["pin_count_match"] is False
    )
    sc2 = h.ok("kw_lib_info", id="Test:Base", unit=2, include_node=True)
    assert [p["number"] for p in sc2["data"]["item"]["pins"]] == ["3", "4"]
    assert sc2["data"]["node_text"].startswith('(symbol "Test:Base"')
    assert [m["id"] for m in sc2["data"]["footprint_matches"]] == ["Test:TH_4", "Test:TP_2"]
    sc3 = h.ok("kw_lib_info", id="Test:Base", unit=9)
    assert any("unit 9 > 2" in w for w in sc3["warnings"]) and len(sc3["data"]["item"]["pins"]) == 4
    sc4 = h.ok("kw_lib_info", id="Test:Pwr", footprint_limit=0)
    assert sc4["data"]["item"]["power"] == "global" and sc4["data"]["footprint_matches"] == []


def test_lib_info_footprint_and_kind_resolution(h: Harness):
    sc = h.ok("kw_lib_info", id="Test:TP_2", kind="footprint", include_node=True)
    d = sc["data"]
    assert d["kind"] == "footprint" and d["item"]["pad_count"] == 2 and d["item"]["attributes"] == ["smd"]
    assert d["item"]["pads"][1]["rotation"] == 90 and d["item"]["courtyard"][0][0] == [-1.5, -0.75]
    assert d["node_text"].startswith('(footprint "TP_2"') and sc["warnings"] == []
    # "Test" is a nickname in both tables: auto picks the symbol table first
    h.fail("kw_lib_info", "E_LIB_NOT_FOUND", id="Test:TP_2")
    assert h.ok("kw_lib_info", id="Test:TH_4", kind="footprint")["data"]["item"]["pad_count"] == 5


def test_lib_info_errors(h: Harness):
    sc = h.fail("kw_lib_info", "E_LIB_NOT_FOUND", id="Tes:Base")
    assert "Test" in sc["candidates"] and sc["fix"]
    sc = h.fail("kw_lib_info", "E_LIB_NOT_FOUND", id="Test:Nope")
    assert sc["why"] and "case-sensitive" in sc["fix"]
    sc = h.fail("kw_lib_info", "E_LIB_UNSUPPORTED", id="Old:Base")
    assert "Legacy" in sc["message"]
    h.fail("kw_lib_info", "E_LIB_UNSUPPORTED", id="Off:Base")
    h.fail("kw_lib_info", "E_LIB_NOT_FOUND", id="Gone:Base")
    h.fail("kw_lib_info", "E_BAD_ARGS", id="no-colon")
    h.fail("kw_lib_info", "E_LIB_NOT_FOUND", id="Test:Orphan")  # parent missing in the library
    h.fail("kw_lib_info", "E_LIB_NOT_FOUND", id="Test:Missing", kind="footprint")


def test_project_argument_adds_project_tables(h: Harness, tmp_path: Path):
    project = tmp_path / "proj"
    shutil.copytree(FIXTURES / "project", project)
    shutil.copy(FIXTURES / "Test.kicad_sym", tmp_path / "Test.kicad_sym")  # ${KIPRJMOD}/../Test.kicad_sym
    shutil.copytree(FIXTURES / "Test.pretty", tmp_path / "Test.pretty")
    # the project's own sym-lib-table (Proj, TextVar, a row shadowing the hidden global "Hidden") and fp-lib-table
    sc = h.ok("kw_lib_search", kind="symbol", query="Base", filters={"library": "Proj"}, project=str(project))
    assert sc["data"]["hits"][0]["id"] == "Proj:Base"
    sc = h.ok(
        "kw_lib_search", kind="symbol", query="Base", filters={"library": "Hidden"}, project=str(project)
    )
    assert sc["data"]["hits"][0]["id"] == "Hidden:Base" and sc["data"]["hits"][0]["hidden"] is False
    info = h.ok("kw_lib_info", id="ProjFp:TH_4", project=str(project))
    assert info["data"]["kind"] == "footprint" and info["data"]["item"]["pad_count"] == 5
    info = h.ok("kw_lib_info", id="TextVar:Chain", project=str(project))
    assert info["data"]["item"]["lib_id"] == "TextVar:Chain"
    h.fail(
        "kw_lib_search", "E_PROJECT_NOT_FOUND", kind="symbol", query="Base", project=str(tmp_path / "nope")
    )
