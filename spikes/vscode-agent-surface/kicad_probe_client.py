"""Drive kicad_probe_server.py over stdio with the mcp 2.x Client and verify image round-trip."""
from __future__ import annotations

import asyncio
import base64
import sys
from pathlib import Path

from mcp.client import Client
from mcp.client.stdio import StdioServerParameters
import mcp.types as t

HERE = Path(__file__).parent
PCB = str(HERE / "demo" / "ecc83" / "ecc83-pp.kicad_pcb")


async def main() -> None:
    params = StdioServerParameters(command=sys.executable, args=[str(HERE / "kicad_probe_server.py")])
    async with Client(params) as client:
        print("negotiated protocol_version:", client.protocol_version)
        print("server_info:", client.server_info)
        print("server_capabilities:", client.server_capabilities)
        tools = await client.list_tools()
        print("tools:", [(tl.name, tl.annotations.model_dump(exclude_none=True) if tl.annotations else None) for tl in tools.tools])
        print("tools/list resultType/ttlMs/cacheScope:", getattr(tools, "result_type", None), getattr(tools, "ttl_ms", None), getattr(tools, "cache_scope", None))

        r = await client.call_tool("kicad_version", {"major": "10.0"})
        print("kicad_version ->", [c.text for c in r.content if isinstance(c, t.TextContent)])

        r = await client.call_tool("render_board_png", {"pcb_path": PCB, "width": 640, "height": 480})
        print("render_board_png is_error:", r.is_error, "resultType:", r.result_type)
        for c in r.content:
            if isinstance(c, t.ImageContent):
                raw = base64.b64decode(c.data)
                out = HERE / "roundtrip.png"
                out.write_bytes(raw)
                print(f"  ImageContent mime={c.mime_type} base64_len={len(c.data)} decoded_bytes={len(raw)} png_magic={raw[:8]!r} -> {out}")
            elif isinstance(c, t.TextContent):
                print("  text:", c.text)

        r = await client.call_tool("run_drc", {"pcb_path": PCB})
        sc = r.structured_content
        print("run_drc structured keys:", list(sc.keys()) if isinstance(sc, dict) else sc)
        print("  violations:", sc.get("violations"), "unconnected:", sc.get("unconnected_items"), "exit_code:", sc.get("exit_code"))
        print("  content[0] type:", type(r.content[0]).__name__, "len(text)=", len(r.content[0].text))

        r = await client.call_tool("export_pcb_svg", {"pcb_path": PCB})
        for c in r.content:
            print("  ", type(c).__name__, getattr(c, "uri", None) or getattr(c, "text", None))


asyncio.run(main())
