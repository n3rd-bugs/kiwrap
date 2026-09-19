"""MCP protocol test for the authoring tools: `kiwrap serve` over stdio (the mcp 2.2 client, as in
tests/test_mcp_protocol.py) lists the eleven authoring tools with their schemas in the catalog
order, answers kw_help for each, and round-trips the teaching errors that need no KiCad
(E_PROJECT_NOT_FOUND, schema-level E_BAD_ARGS, the erc_clean gate on kw_pcb_sync and an
`if_match` mismatch on kw_sch_edit on a freshly created project when a KiCad is installed)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import Client, StdioServerParameters

from kiwrap.core.tools import AUTHORING_TOOLS, TOOL_NAMES

AUTHORING_NAMES = tuple(t.name for t in AUTHORING_TOOLS)


@pytest.fixture(scope="module")
def mcp_home(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("kiwrap-mcp-authoring-home")


def server_params(home: Path) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "kiwrap.core.cli_main", "--storage-home", str(home), "serve"],
    )


async def _session(params: StdioServerParameters, tmp_path: Path) -> dict[str, Any]:
    out: dict[str, Any] = {}
    async with Client(params, mode="auto") as c:
        listed = await c.list_tools()
        out["tools"] = [t.name for t in listed.tools]
        out["schemas"] = {
            t.name: (t.input_schema, t.output_schema, t.annotations, t.meta) for t in listed.tools
        }
        out["help"] = {}
        for name in AUTHORING_NAMES:
            r = await c.call_tool("kw_help", {"topic": name})
            out["help"][name] = (r.is_error, r.structured_content)
        missing = str(tmp_path / "does-not-exist")
        r = await c.call_tool(
            "kw_sch_add_symbol", {"project": missing, "lib_id": "Device:R", "at_mm": [10, 10]}
        )
        out["not_found"] = (r.is_error, r.structured_content)
        r = await c.call_tool("kw_sch_connect", {"project": missing, "endpoints": []})
        out["schema_error"] = (r.is_error, r.structured_content)
        r = await c.call_tool("kw_pcb_route", {"project": missing, "ops": [{"op": "wiggle"}]})
        out["enum_error"] = (r.is_error, r.structured_content)
        r = await c.call_tool(
            "kw_sch_add_symbol", {"project": missing, "lib_id": "Device:R", "at_mm": [1, 2], "rotation": 45}
        )
        out["rotation_error"] = (r.is_error, r.structured_content)
        r = await c.call_tool("kw_pcb_sync", {"project": missing, "force": {}})
        out["force_error"] = (r.is_error, r.structured_content)
        # a real project (needs an installed KiCad for the target; otherwise E_NO_KICAD and we stop)
        work = tmp_path / "blinky"
        r = await c.call_tool("kw_project", {"action": "create", "dir": str(work), "name": "blinky"})
        out["create"] = (r.is_error, r.structured_content)
        if not r.is_error:
            r = await c.call_tool("kw_pcb_sync", {"project": str(work)})
            out["gate"] = (r.is_error, r.structured_content)
            r = await c.call_tool(
                "kw_sch_edit",
                {
                    "project": str(work),
                    "if_match": "0000000000000000",
                    "ops": [{"op": "value", "ref": "R1", "value": "1k"}],
                },
            )
            out["if_match"] = (r.is_error, r.structured_content)
            r = await c.call_tool(
                "kw_pcb_place", {"project": str(work), "placements": [{"ref": "R1", "at_mm": [1, 1]}]}
            )
            out["place_unknown"] = (r.is_error, r.structured_content)
    return out


def test_stdio_lists_the_authoring_tools_with_schemas_and_round_trips_errors(tmp_path: Path, mcp_home: Path):
    out = anyio.run(_session, server_params(mcp_home), tmp_path)
    assert out["tools"] == list(TOOL_NAMES)
    for name in AUTHORING_NAMES:
        assert name in out["tools"], name
    names = out["tools"]
    assert (
        names.index("kw_sch_add_symbol")
        < names.index("kw_sch_get")
        < names.index("kw_pcb_setup")
        < names.index("kw_pcb_get")
    )
    for name in AUTHORING_NAMES:
        inp, outp, ann, meta = out["schemas"][name]
        assert inp["type"] == "object" and "project" in inp["required"], name
        assert outp is not None and {"changed", "validators", "stage", "provider_used"} <= set(
            outp["properties"]
        ), name
        assert ann is not None and ann.read_only_hint is False, name
        assert meta and meta.get("anthropic/maxResultSizeChars"), name
    add = out["schemas"]["kw_sch_add_symbol"][0]
    assert set(add["required"]) == {"project", "lib_id", "at_mm"}
    assert add["properties"]["rotation"]["enum"] == [0, 90, 180, 270]
    assert add["properties"]["unit"]["minimum"] == 1
    connect = out["schemas"]["kw_sch_connect"][0]
    assert connect["properties"]["style"]["enum"] == ["wire", "label", "global_label", "power"]
    assert connect["properties"]["endpoints"]["minItems"] == 1
    assert out["schemas"]["kw_sch_edit"][2].destructive_hint is True
    assert out["schemas"]["kw_pcb_sync"][2].destructive_hint is True
    assert out["schemas"]["kw_pcb_zones"][0]["required"] == ["project", "zones"]
    for name, (is_err, sc) in out["help"].items():
        assert not is_err and sc["data"]["markdown"].startswith(f"# {name}"), name

    is_err, sc = out["not_found"]
    assert is_err and sc["code"] == "E_PROJECT_NOT_FOUND" and sc["fix"]
    is_err, sc = out["schema_error"]
    assert is_err and sc["code"] == "E_BAD_ARGS" and "endpoints" in sc["message"]
    is_err, sc = out["enum_error"]
    assert is_err and sc["code"] == "E_BAD_ARGS" and "op" in sc["message"]
    is_err, sc = out["rotation_error"]
    assert is_err and sc["code"] == "E_BAD_ARGS" and "rotation" in sc["message"]
    is_err, sc = out["force_error"]
    assert is_err and sc["code"] in ("E_BAD_ARGS", "E_PROJECT_NOT_FOUND")

    is_err, sc = out["create"]
    if is_err:
        assert sc["code"] == "E_NO_KICAD"
        pytest.skip("no KiCad installed: the gate / if_match round trips need a created project")
    is_err, sc = out["gate"]
    assert is_err and sc["code"] == "E_GATE" and sc["data"]["required_stage"] == "erc_clean"
    assert "kw_sch_check" in sc["fix"]
    is_err, sc = out["if_match"]
    assert is_err and sc["code"] == "E_EXTERNAL_EDIT" and sc["data"]["sheet"] == "blinky.kicad_sch"
    is_err, sc = out["place_unknown"]
    assert is_err and sc["code"] in ("E_BAD_ARGS", "E_UNSUPPORTED"), sc
