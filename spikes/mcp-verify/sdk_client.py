import asyncio, base64, sys
from mcp import Client
from mcp.client.stdio import StdioServerParameters
PNG_SIG = bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A])

async def run(target, mode):
    async with Client(target, mode=mode) as c:
        pv = c.protocol_version
        r = await c.call_tool("render_board", {"width": 160, "height": 120})
        img = [b for b in r.content if b.type == "image"]
        raw = base64.b64decode(img[0].data) if img else b""
        is_png = raw[:8] == PNG_SIG
        mime = img[0].mime_type if img else None
        print(f"[{mode:10}] negotiated={pv} image_mime={mime} png={is_png} bytes={len(raw)} structured={r.structured_content} result_type={getattr(r,'result_type',None)}")

async def main():
    kind = sys.argv[1]
    if kind == "stdio":
        target = StdioServerParameters(command=sys.executable, args=["server.py", "stdio"])
    else:
        target = sys.argv[2]
    for mode in ("auto", "legacy", "2026-07-28"):
        try:
            await run(target, mode)
        except Exception as e:
            print(f"[{mode:10}] FAILED: {type(e).__name__}: {e}")

asyncio.run(main())
