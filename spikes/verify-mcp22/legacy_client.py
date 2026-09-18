import asyncio, base64, os
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client
V2PY = os.path.abspath("v2/Scripts/python.exe")
def show(label, init, r):
    kinds=[c.type for c in r.content]; img=[c for c in r.content if c.type=="image"][0]
    magic = base64.b64decode(img.data)[:8]==bytes([0x89,0x50,0x4E,0x47,0x0D,0x0A,0x1A,0x0A])
    print(f"[{label}] negotiated={init.protocolVersion} content={kinds} mime={img.mimeType} png={magic} structuredContent={r.structuredContent} isError={r.isError}")
async def main():
    async with stdio_client(StdioServerParameters(command=V2PY, args=["server.py","stdio"])) as (r,w):
        async with ClientSession(r,w) as s:
            init = await s.initialize(); res = await s.call_tool("render", {"name":"v1-stdio"}); show("mcp1.12.4 stdio", init, res)
    async with streamablehttp_client("http://127.0.0.1:8765/mcp") as (r,w,_):
        async with ClientSession(r,w) as s:
            init = await s.initialize(); res = await s.call_tool("render", {"name":"v1-http"}); show("mcp1.12.4 http", init, res)
asyncio.run(main())
