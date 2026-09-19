"""Board images: `pcb render` (3D, 9.0+) and a 2D fallback through `pcb export pdf` + pypdfium2.

Verified facts (docs/ARCHITECTURE.md §2, docs/VERIFICATION.md claim 11, spike clihelp):
- `pcb render` exists on 9.0.5 and 10.0.0 only (`cli.has_render`; ~2.5 s on 10.0, PNG output
  with `--width/--height/--side/--background/--quality/--zoom/--rotate/--perspective/--floor`).
  On 7/8 -> `Unsupported(oldest='9.0.0', fallback='svg')`: the caller can use `render_pcb_2d`.
- Decision D8 forbids PyMuPDF; rasterisation is pypdfium2 (optional extra `render`, Apache/BSD)
  on the PDF kicad-cli writes, or the `resvg` CLI for SVG when it is on PATH. On this machine
  pypdfium2 5.x is installed and resvg is not, so the PDF path is the one exercised by the tests;
  `rasterise_svg` is implemented (resvg) but reported as untested when resvg is absent.
- Images for MCP receipts must stay <= 1024 px on the long side (§11 context budget): every
  function here downsamples to `max_px` and writes PNG through the stdlib (`zlib` + `struct`),
  so Pillow is not required.
"""

from __future__ import annotations

import shutil
import struct
import subprocess
import zlib
from collections.abc import Sequence
from pathlib import Path

from kiwrap.backends.cli.export import DEFAULT_PLOT_LAYERS, export_pcb
from kiwrap.backends.cli.runner import has_flag, resolve_caps, run_kicad_cli
from kiwrap.capability import Capabilities, Unsupported
from kiwrap.installation import KiCadInstallation
from kiwrap.sandbox import ConfigRoot

OLDEST_KICAD_WITH_RENDER = "9.0.0"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
RENDER_SIDES = ("top", "bottom", "left", "right", "front", "back")
_INSTALL_RENDER_EXTRA = "uv sync --extra render"


def render_pcb(
    install: KiCadInstallation,
    config: ConfigRoot,
    board: Path,
    out_png: Path,
    *,
    width: int = 1024,
    height: int = 576,
    side: str = "top",
    background: str = "opaque",
    quality: str = "basic",
    zoom: float | None = None,
    rotate: tuple[float, float, float] | None = None,
    perspective: bool = False,
    floor: bool = False,
    max_px: int = 1024,
    caps: Capabilities | None = None,
    timeout_s: float = 300,
) -> Path:
    """Run `kicad-cli pcb render` (3D) to `out_png`, downscaled so the longest side <= `max_px`.

    Raises `Unsupported(fallback="svg")` where `cli.has_render` is false (7/8).
    """
    caps = resolve_caps(install, caps)
    if not caps.has("cli.has_render"):
        raise Unsupported(
            "cli.has_render", install, oldest_kicad_with_it=OLDEST_KICAD_WITH_RENDER, fallback="svg"
        )
    if side not in RENDER_SIDES:
        raise ValueError(f"side must be one of {RENDER_SIDES}, got {side!r}")
    board, out_png = Path(board), Path(out_png)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    sub = "pcb render"
    args: list[str] = [
        "pcb",
        "render",
        "-o",
        str(out_png),
        "--width",
        str(int(width)),
        "--height",
        str(int(height)),
    ]
    args += ["--side", side, "--background", background, "--quality", quality]
    if zoom is not None and has_flag(caps, sub, "--zoom"):
        args += ["--zoom", str(zoom)]
    if rotate is not None and has_flag(caps, sub, "--rotate"):
        args += ["--rotate", ",".join(str(v) for v in rotate)]
    if perspective and has_flag(caps, sub, "--perspective"):
        args.append("--perspective")
    if floor and has_flag(caps, sub, "--floor"):
        args.append("--floor")
    args.append(str(board))
    run_kicad_cli(install, args, config=config, caps=caps, timeout_s=timeout_s, accept=(0,))
    if not out_png.is_file():
        raise FileNotFoundError(f"kicad-cli pcb render exited 0 but wrote no {out_png}")
    if max(width, height) > max_px:
        downscale_png(out_png, out_png, max_px=max_px)
    return out_png


def render_pcb_2d(
    install: KiCadInstallation,
    config: ConfigRoot,
    board: Path,
    out_png: Path,
    *,
    layers: Sequence[str] = DEFAULT_PLOT_LAYERS,
    max_px: int = 1024,
    caps: Capabilities | None = None,
    timeout_s: float = 300,
    keep_pdf: bool = False,
) -> Path:
    """2D preview on every version: `pcb export pdf --layers ...` then pypdfium2 -> PNG.

    This is the "svg" fallback named by `render_pcb`'s `Unsupported`; it uses the PDF export
    because pypdfium2 rasterises PDFs natively (SVG would need resvg).
    """
    caps = resolve_caps(install, caps)
    board, out_png = Path(board), Path(out_png)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    pdf_dir = out_png.parent
    opts: dict[str, object] = {"layers": list(layers)}
    # 7.0's `pcb export pdf` has neither flag (8.0+ do); only pass what this version's help lists.
    for name, flag, value in (
        ("exclude_drawing_sheet", "--exclude-drawing-sheet", True),
        ("page_size_mode", "--page-size-mode", 2),
    ):
        if flag in caps.flags("pcb export pdf"):
            opts[name] = value
    files = export_pcb(
        install,
        config,
        board,
        "pdf",
        pdf_dir,
        caps=caps,
        timeout_s=timeout_s,
        out_name=out_png.stem + ".preview.pdf",
        **opts,
    )
    pdf = next(p for p in files if p.suffix.lower() == ".pdf")
    try:
        rasterise_pdf(pdf, out_png, max_px=max_px)
    finally:
        if not keep_pdf:
            pdf.unlink(missing_ok=True)
    return out_png


# --------------------------------------------------------------------------------------------
# Rasterisers
# --------------------------------------------------------------------------------------------


class PdfPageError(ValueError):
    """`page` (0-based) is outside the document; carries `page_count` so a caller can list the range."""

    def __init__(self, page: int, page_count: int):
        super().__init__(f"page {page + 1} does not exist; the PDF has {page_count} page(s)")
        self.page, self.page_count = page, page_count


def rasterise_pdf(pdf: Path, out_png: Path, *, max_px: int = 1024, page: int = 0) -> Path:
    """Render one PDF page with pypdfium2 to a PNG whose longest side is <= `max_px`.

    Raises `PdfPageError` (a ValueError with `page_count`) for a page outside the document instead
    of letting pypdfium2's bare "Failed to load page" escape.
    """
    try:
        import pypdfium2 as pdfium
    except ImportError as exc:
        raise RenderDependencyMissing("render.pypdfium2", fix=_INSTALL_RENDER_EXTRA) from exc
    pdf, out_png = Path(pdf), Path(out_png)
    doc = pdfium.PdfDocument(str(pdf))
    try:
        count = len(doc)
        if page < 0 or page >= count:
            raise PdfPageError(page, count)
        pg = doc[page]
        w_pt, h_pt = pg.get_size()
        scale = max_px / max(w_pt, h_pt)
        bitmap = pg.render(scale=scale, rev_byteorder=True, fill_color=(255, 255, 255, 255))
        try:
            width, height, stride = bitmap.width, bitmap.height, bitmap.stride
            channels = bitmap.n_channels
            raw = bytes(bitmap.buffer)
        finally:
            bitmap.close()
    finally:
        doc.close()
    rows = [raw[y * stride : y * stride + width * channels] for y in range(height)]
    if channels == 4:
        rows = [_drop_alpha(r) for r in rows]
        channels = 3
    elif channels == 1:
        rows = [bytes(b for px in r for b in (px, px, px)) for r in rows]
        channels = 3
    out_png.write_bytes(encode_png(width, height, rows))
    return out_png


def rasterise_svg(svg: Path, out_png: Path, *, max_px: int = 1024) -> Path:
    """Rasterise an SVG with the `resvg` CLI (https://github.com/RazrFalcon/resvg) if installed.

    kiwrap deliberately has no SVG rasteriser of its own (D8: no PyMuPDF; no cairo); when resvg is
    absent this raises `Unsupported` naming `render_pcb_2d` (PDF + pypdfium2) as the fallback.
    """
    exe = shutil.which("resvg")
    if not exe:
        raise RenderDependencyMissing(
            "render.resvg", fix="put resvg on PATH, or use render_pcb_2d (pcb export pdf + pypdfium2)"
        )
    svg, out_png = Path(svg), Path(out_png)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [exe, "-w", str(max_px), str(svg), str(out_png)], check=True, capture_output=True, timeout=120
    )
    downscale_png(out_png, out_png, max_px=max_px)
    return out_png


class RenderDependencyMissing(Unsupported):
    """`Unsupported` variant for the pure-Python rasterisers: no KiCad install is involved, the
    missing piece is a Python extra or an external CLI, and `fix` says how to get it."""

    def __init__(self, capability: str, *, fix: str):
        RuntimeError.__init__(self, f"{capability} is not available; fix: {fix}")
        self.capability, self.install = capability, None  # type: ignore[assignment]
        self.oldest_kicad_with_it, self.fallback, self.fix = None, fix, fix


# --------------------------------------------------------------------------------------------
# Minimal PNG codec (8-bit RGB / RGBA, non-interlaced) — enough to read what kicad-cli /
# pypdfium2 write and to shrink it. No Pillow (keeps the wheel small and the licence list short).
# --------------------------------------------------------------------------------------------


def _chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def encode_png(width: int, height: int, rows: Sequence[bytes], *, channels: int = 3) -> bytes:
    """Encode 8-bit RGB (channels=3) or RGBA (channels=4) rows into PNG bytes."""
    color_type = {3: 2, 4: 6}[channels]
    ihdr = struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
    raw = b"".join(b"\x00" + bytes(r) for r in rows)
    return (
        PNG_SIGNATURE + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", zlib.compress(raw, 6)) + _chunk(b"IEND", b"")
    )


def decode_png(data: bytes) -> tuple[int, int, int, list[bytes]]:
    """Decode a non-interlaced 8-bit PNG (grey, RGB, grey+alpha, RGBA, or palette) into
    (width, height, channels, rows) with channels in {3, 4}."""
    if not data.startswith(PNG_SIGNATURE):
        raise ValueError("not a PNG file")
    pos = 8
    width = height = 0
    bit_depth = color_type = interlace = 0
    idat: list[bytes] = []
    palette = b""
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        tag = data[pos + 4 : pos + 8]
        body = data[pos + 8 : pos + 8 + length]
        pos += 12 + length
        if tag == b"IHDR":
            width, height, bit_depth, color_type, _, _, interlace = struct.unpack(">IIBBBBB", body)
        elif tag == b"PLTE":
            palette = body
        elif tag == b"IDAT":
            idat.append(body)
        elif tag == b"IEND":
            break
    if bit_depth != 8 or interlace != 0:
        raise ValueError(f"unsupported PNG (bit depth {bit_depth}, interlace {interlace})")
    src_channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color_type]
    raw = zlib.decompress(b"".join(idat))
    stride = width * src_channels
    rows: list[bytes] = []
    prev = bytearray(stride)
    off = 0
    for _ in range(height):
        filt = raw[off]
        line = bytearray(raw[off + 1 : off + 1 + stride])
        off += 1 + stride
        _unfilter(filt, line, prev, src_channels)
        rows.append(bytes(line))
        prev = line
    if color_type == 3:
        rows = [b"".join(palette[3 * i : 3 * i + 3] for i in r) for r in rows]
        return width, height, 3, rows
    if src_channels == 1:
        return width, height, 3, [bytes(b for px in r for b in (px, px, px)) for r in rows]
    if src_channels == 2:
        return (
            width,
            height,
            4,
            [bytes(b for i in range(0, len(r), 2) for b in (r[i], r[i], r[i], r[i + 1])) for r in rows],
        )
    return width, height, src_channels, rows


def _unfilter(filt: int, line: bytearray, prev: bytearray, bpp: int) -> None:
    n = len(line)
    if filt == 0:
        return
    if filt == 1:
        for i in range(bpp, n):
            line[i] = (line[i] + line[i - bpp]) & 0xFF
    elif filt == 2:
        for i in range(n):
            line[i] = (line[i] + prev[i]) & 0xFF
    elif filt == 3:
        for i in range(n):
            left = line[i - bpp] if i >= bpp else 0
            line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
    elif filt == 4:
        for i in range(n):
            a = line[i - bpp] if i >= bpp else 0
            b = prev[i]
            c = prev[i - bpp] if i >= bpp else 0
            p = a + b - c
            pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
            pred = a if pa <= pb and pa <= pc else (b if pb <= pc else c)
            line[i] = (line[i] + pred) & 0xFF
    else:
        raise ValueError(f"bad PNG filter type {filt}")


def _drop_alpha(row: bytes) -> bytes:
    return bytes(b for i, b in enumerate(row) if i % 4 != 3)


def downscale_rows(
    rows: Sequence[bytes], width: int, height: int, channels: int, max_px: int
) -> tuple[int, int, list[bytes]]:
    """Box-filter downscale so that max(width, height) <= max_px. Returns (w, h, rows)."""
    longest = max(width, height)
    if longest <= max_px:
        return width, height, list(rows)
    factor = -(-longest // max_px)  # ceil
    new_w, new_h = max(1, width // factor), max(1, height // factor)
    out: list[bytes] = []
    area = factor * factor
    for y in range(new_h):
        src_rows = rows[y * factor : (y + 1) * factor]
        line = bytearray(new_w * channels)
        for x in range(new_w):
            x0 = x * factor * channels
            for c in range(channels):
                total = 0
                for r in src_rows:
                    total += sum(r[x0 + c : x0 + factor * channels : channels])
                line[x * channels + c] = total // area
        out.append(bytes(line))
    return new_w, new_h, out


def downscale_png(src: Path, dst: Path, *, max_px: int = 1024) -> Path:
    """Rewrite `src` to `dst` with the longest side <= `max_px` (no-op copy when already small)."""
    src, dst = Path(src), Path(dst)
    data = src.read_bytes()
    width, height, channels, rows = decode_png(data)
    if max(width, height) <= max_px:
        if src != dst:
            dst.write_bytes(data)
        return dst
    new_w, new_h, new_rows = downscale_rows(rows, width, height, channels, max_px)
    dst.write_bytes(encode_png(new_w, new_h, new_rows, channels=channels))
    return dst


def png_size(data: bytes) -> tuple[int, int]:
    """(width, height) from the IHDR of PNG bytes."""
    if not data.startswith(PNG_SIGNATURE):
        raise ValueError("not a PNG file")
    return struct.unpack(">II", data[16:24])
