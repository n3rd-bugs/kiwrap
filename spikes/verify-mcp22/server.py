import base64, struct, zlib, sys
from mcp.server import MCPServer
from mcp.types import CallToolResult, ImageContent, TextContent

def tiny_png() -> bytes:
    # 1x1 red PNG
    def chunk(t, d): return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    raw = b"\x00\xff\x00\x00"
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))

mcp = MCPServer("verify")

@mcp.tool()
def render(name: str) -> CallToolResult:
    """Return PNG image + structured content"""
    png = base64.b64encode(tiny_png()).decode()
    return CallToolResult(
        content=[TextContent(type="text", text=f"rendered {name}"),
                 ImageContent(type="image", data=png, mime_type="image/png")],
        structured_content={"name": name, "width": 1, "height": 1},
    )

if __name__ == "__main__":
    transport = sys.argv[1] if len(sys.argv) > 1 else "stdio"
    if transport == "stdio":
        mcp.run("stdio")
    else:
        mcp.run("streamable-http", host="127.0.0.1", port=8765)
