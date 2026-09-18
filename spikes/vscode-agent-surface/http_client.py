import asyncio, mcp.types as t
from mcp.client import Client
async def main():
    async with Client("http://127.0.0.1:8765/mcp") as c:
        print("HTTP negotiated:", c.protocol_version, "server:", c.server_info.name)
        tools = await c.list_tools(); print("tools:", [x.name for x in tools.tools])
        r = await c.call_tool("kicad_version", {"major": "7.0"}); print("kicad 7 ->", r.content[0].text)
        r = await c.call_tool("kicad_version", {"major": "8.0"}); print("kicad 8 ->", r.content[0].text)
asyncio.run(main())
