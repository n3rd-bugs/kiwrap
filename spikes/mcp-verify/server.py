import base64, subprocess, sys, os, tempfile, pathlib
from pydantic import BaseModel
from mcp.server import MCPServer
from mcp.types import ImageContent, CallToolResult, TextContent

KICAD_CLI = os.environ.get("KICAD_CLI", r"C:\Program Files\KiCad\10.0\bin\kicad-cli.exe")
HERE = pathlib.Path(__file__).parent

mcp = MCPServer("kicad-verify")

class RenderInfo(BaseModel):
    board: str
    width: int
    height: int
    png_bytes: int
    kicad_version: str

@mcp.tool()
def render_board(width: int = 400, height: int = 300) -> CallToolResult:
    """Render the ECC83 demo board to PNG with kicad-cli and return image + structured info."""
    out = HERE / f"render_{width}x{height}.png"
    board = HERE / "ecc83-pp.kicad_pcb"
    subprocess.run([KICAD_CLI, "pcb", "render", "-o", str(out), "-w", str(width), "-h", str(height), str(board)],
                   check=True, capture_output=True)
    ver = subprocess.run([KICAD_CLI, "version"], capture_output=True, text=True).stdout.strip()
    data = out.read_bytes()
    info = RenderInfo(board=board.name, width=width, height=height, png_bytes=len(data), kicad_version=ver)
    return CallToolResult(
        content=[ImageContent(type="image", data=base64.b64encode(data).decode(), mime_type="image/png"),
                 TextContent(type="text", text=f"rendered {len(data)} bytes")],
        structured_content=info.model_dump(),
    )

if __name__ == "__main__":
    transport = sys.argv[1] if len(sys.argv) > 1 else "stdio"
    if transport == "stdio":
        mcp.run("stdio")
    else:
        mcp.run("streamable-http", host="127.0.0.1", port=int(sys.argv[2]) if len(sys.argv) > 2 else 8765)
