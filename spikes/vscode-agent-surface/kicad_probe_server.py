"""Minimal MCP (Python SDK 2.x) stdio server wrapping kicad-cli.

Tools:
  kicad_version(major)             -> text
  render_board_png(pcb_path, ...)  -> ImageContent (PNG) + text
  run_drc(pcb_path)                -> structuredContent (JSON from kicad-cli pcb drc)
  export_pcb_svg(pcb_path)         -> resource_link to produced SVG

Run: python kicad_probe_server.py            (stdio)
     python kicad_probe_server.py --http     (streamable HTTP on 127.0.0.1:8765/mcp)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver import Image
from mcp.types import ResourceLink, TextContent, ToolAnnotations

KICAD_ROOT = Path(os.environ.get("KICAD_ROOT", r"C:\Program Files\KiCad"))


def kicad_cli(major: str) -> Path:
    return KICAD_ROOT / major / "bin" / "kicad-cli.exe"


mcp = MCPServer(
    name="kicad-probe",
    version="0.0.1",
    instructions="Probe server: wraps kicad-cli. Use render_board_png to SEE a board.",
)


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True))
def kicad_version(major: str = "10.0") -> str:
    """Return the version string of the installed kicad-cli for the given major (7.0/8.0/9.0/10.0)."""
    out = subprocess.run([str(kicad_cli(major)), "version"], capture_output=True, text=True, timeout=60)
    return out.stdout.strip() or out.stderr.strip()


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
def render_board_png(pcb_path: str, width: int = 800, height: int = 600, major: str = "10.0",
                     side: str = "top", quality: str = "basic") -> list[TextContent | Image]:
    """Render a .kicad_pcb to a PNG raytrace/raster image via `kicad-cli pcb render` and return it as an image."""
    out_png = Path(tempfile.mkdtemp(prefix="kicad_render_")) / "board.png"
    cmd = [str(kicad_cli(major)), "pcb", "render", "--output", str(out_png), "--width", str(width),
           "--height", str(height), "--side", side, "--quality", quality, pcb_path]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if r.returncode != 0 or not out_png.exists():
        raise RuntimeError(f"kicad-cli render failed rc={r.returncode}: {r.stderr[-800:]}")
    size = out_png.stat().st_size
    return [TextContent(type="text", text=f"Rendered {pcb_path} -> {out_png} ({size} bytes)"),
            Image(path=out_png)]


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True), structured_output=True)
def run_drc(pcb_path: str, major: str = "10.0") -> dict[str, Any]:
    """Run DRC via `kicad-cli pcb drc --format json` and return the parsed JSON report."""
    out_json = Path(tempfile.mkdtemp(prefix="kicad_drc_")) / "drc.json"
    cmd = [str(kicad_cli(major)), "pcb", "drc", "--format", "json", "--output", str(out_json),
           "--severity-all", pcb_path]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if not out_json.exists():
        raise RuntimeError(f"kicad-cli drc failed rc={r.returncode}: {r.stderr[-800:]}")
    data = json.loads(out_json.read_text(encoding="utf-8"))
    return {"exit_code": r.returncode, "violations": len(data.get("violations", [])),
            "unconnected_items": len(data.get("unconnected_items", [])), "report": data}


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
def export_pcb_svg(pcb_path: str, major: str = "10.0", layers: str = "F.Cu,B.Cu,Edge.Cuts") -> list[TextContent | ResourceLink]:
    """Export a .kicad_pcb to SVG (kicad-cli pcb export svg) and return a resource_link to the file."""
    out_dir = Path(tempfile.mkdtemp(prefix="kicad_svg_"))
    out_svg = out_dir / "board.svg"
    cmd = [str(kicad_cli(major)), "pcb", "export", "svg", "--output", str(out_svg), "--layers", layers,
           "--page-size-mode", "2", pcb_path]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if not out_svg.exists():
        raise RuntimeError(f"kicad-cli svg failed rc={r.returncode}: {r.stderr[-800:]}")
    return [TextContent(type="text", text=f"SVG written: {out_svg}"),
            ResourceLink(type="resource_link", uri=out_svg.as_uri(), name="board.svg", mime_type="image/svg+xml")]


if __name__ == "__main__":
    if "--http" in sys.argv:
        mcp.run(transport="streamable-http", host="127.0.0.1", port=8765)
    else:
        mcp.run(transport="stdio")
