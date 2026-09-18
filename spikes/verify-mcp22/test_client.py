import asyncio, base64, sys
from mcp import Client, StdioServerParameters

async def check(client, label):
    async with client as c:
        print(f"[{label}] negotiated protocol_version = {c.protocol_version}")
        tools = await c.list_tools()
        print(f"[{label}] tools = {[t.name for t in tools.tools]}")
        r = await c.call_tool("render", {"name": "board"})
        kinds = [type(b).__name__ for b in r.content]
        img = [b for b in r.content if type(b).__name__ == "ImageContent"][0]
        raw = base64.b64decode(img.data); magic_ok = raw[:8] == bytes([0x89,0x50,0x4E,0x47,0x0D,0x0A,0x1A,0x0A])
        print(f"[{label}] content types = {kinds}; image mime={img.mime_type}; png magic ok={magic_ok}")
        print(f"[{label}] structured_content = {r.structured_content}; is_error={r.is_error}")

async def main():
    py = sys.executable
    await check(Client(StdioServerParameters(command=py, args=["server.py", "stdio"])), "stdio auto")
    await check(Client(StdioServerParameters(command=py, args=["server.py", "stdio"]), mode="legacy"), "stdio legacy")
    await check(Client("http://127.0.0.1:8765/mcp"), "http auto")
    await check(Client("http://127.0.0.1:8765/mcp", mode="legacy"), "http legacy")

asyncio.run(main())
