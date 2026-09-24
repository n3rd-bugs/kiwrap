"""Board images: `pcb render` (3D, 9.0+) and a 2D fallback through `pcb export pdf` + pypdfium2.

Verified facts (docs/ARCHITECTURE.md §2, docs/VERIFICATION.md claim 11, spike clihelp):
- `pcb render` exists on 9.0.5 and 10.0.0 only (`cli.has_render`; ~2.5 s on 10.0, PNG output
  with `--width/--height/--side/--background/--quality/--zoom/--rotate/--perspective/--floor`).
  On 7/8 -> `Unsupported(oldest='9.0.0', fallback='svg')`: the caller can use `render_pcb_2d`.
- Decision D8 forbids PyMuPDF; rasterisation is pypdfium2 (optional extra `render`, Apache/BSD)
  on the PDF kicad-cli writes, or the `resvg` CLI for SVG when it is on PATH. On this machine
  pypdfium2 5.x is installed and resvg is not, so the PDF path is the one exercised by the tests;
  `rasterise_svg` is implemented (resvg) but reported as untested when resvg is absent.
- Images for MCP receipts stay <= 1024 px on the long side (§11 context budget) unless the caller
  asks for a region crop (review item 38: `bbox_mm` + `px_per_mm`, up to `CROP_MAX_PX`); every
  function here caps at `max_px` and writes PNG through the stdlib (`zlib` + `struct`), so Pillow
  is not required.
- The 2D preview is cut to a board region (default: Edge.Cuts outline + 1 mm) instead of the whole
  drawing sheet (review §3 "smaller defects": `--page-size-mode` does not exist for
  `pcb export pdf` on 10.0/10.99, so the old path rasterised the full A4/A3 page).
"""

from __future__ import annotations

import shutil
import struct
import subprocess
import zlib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from kiwrap.backends.cli.export import DEFAULT_PLOT_LAYERS, export_pcb
from kiwrap.backends.cli.runner import has_flag, resolve_caps, run_kicad_cli
from kiwrap.capability import Capabilities, Unsupported
from kiwrap.formats.board import BoardFile
from kiwrap.formats.geometry import BBox
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


#: Longest edge of a full-board 2D preview (docs/ARCHITECTURE.md §9: images <= 1024 px).
FULL_BOARD_MAX_PX = 1024
#: Longest edge of a region crop (review item 38: `bbox_mm` + `px_per_mm` up to ~60 lifts the
#: 1024 px cap so 0.2 mm features stay readable; the tool decides whether the file is inlined).
CROP_MAX_PX = 4096
#: Margin around the Edge.Cuts outline when no region is given (review §2 item 38: "default
#: outline + 1 mm").
BOARD_MARGIN_MM = 1.0
_PT_PER_MM = 72.0 / 25.4


@dataclass(frozen=True)
class RasterPlan:
    """What a region raster will be: the board-mm region, the effective pixels per mm (reduced
    when the region at the requested density would exceed `max_px`) and the pixel size."""

    bbox_mm: tuple[float, float, float, float]
    px_per_mm: float
    width: int
    height: int
    requested_px_per_mm: float | None
    capped: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "bbox_mm": [round(v, 4) for v in self.bbox_mm],
            "px_per_mm": round(self.px_per_mm, 4),
            "requested_px_per_mm": self.requested_px_per_mm,
            "width": self.width,
            "height": self.height,
            "capped": self.capped,
        }


def plan_raster(
    bbox_mm: Sequence[float], *, px_per_mm: float | None = None, max_px: int = FULL_BOARD_MAX_PX
) -> RasterPlan:
    """Pixel size of `bbox_mm` (x0, y0, x1, y1 in board millimetres, KiCad's y-down frame) at
    `px_per_mm`, capped so the longest edge is <= `max_px`. Without `px_per_mm` the region is
    fitted to `max_px`."""
    x0, y0, x1, y1 = (float(v) for v in bbox_mm)
    if not (x1 > x0 and y1 > y0):
        raise ValueError(f"bbox_mm must be [x0, y0, x1, y1] with x1 > x0 and y1 > y0, got {list(bbox_mm)}")
    longest_mm = max(x1 - x0, y1 - y0)
    # a hair below the exact fit: PDFium rounds the bitmap size up, and 1024.0000001 px is 1025
    fit = max_px / longest_mm * (1.0 - 1e-6)
    ppm = fit if px_per_mm is None else min(float(px_per_mm), fit)
    capped = px_per_mm is not None and ppm < float(px_per_mm) - 1e-9
    width = max(1, min(max_px, round((x1 - x0) * ppm)))
    height = max(1, min(max_px, round((y1 - y0) * ppm)))
    return RasterPlan((x0, y0, x1, y1), ppm, width, height, px_per_mm, capped)


def board_region(
    board: Path, *, margin_mm: float = BOARD_MARGIN_MM
) -> tuple[float, float, float, float] | None:
    """The board's Edge.Cuts bounding box grown by `margin_mm` (else the footprints' extent);
    None when the file has neither (the caller then rasterises the whole page)."""
    try:
        model = BoardFile.load(board, project_rules=False)
    except Exception:  # noqa: BLE001 - a board kiwrap cannot parse is still plotted by KiCad
        return None
    box = model.outline_bbox()
    if box is None:
        pts = [c for fp in model.footprints for c in fp.bbox().corners()]
        box = BBox.of(pts) if pts else None
    if box is None:
        return None
    return (box.min_x - margin_mm, box.min_y - margin_mm, box.max_x + margin_mm, box.max_y + margin_mm)


def render_pcb_2d(
    install: KiCadInstallation,
    config: ConfigRoot,
    board: Path,
    out_png: Path,
    *,
    layers: Sequence[str] = DEFAULT_PLOT_LAYERS,
    max_px: int = FULL_BOARD_MAX_PX,
    bbox_mm: Sequence[float] | None = None,
    px_per_mm: float | None = None,
    mirror: bool = False,
    caps: Capabilities | None = None,
    timeout_s: float = 300,
    keep_pdf: bool = False,
) -> Path:
    """2D preview on every version: `pcb export pdf --layers ... --scale 1` then pypdfium2 -> PNG
    of a board REGION (review item 38 and the page-size bug in review §3 "smaller defects").

    Verified (scratchpad spike_pdf/spike_crop on 10.0.6 coldfire): at `--scale 1` kicad-cli plots
    board millimetres 1:1 from the page's top-left corner, so a board region maps to PDF points
    without knowing the paper size. The region is cut by moving the page's MediaBox/CropBox onto it
    (a crop may reach past the sheet), then rendered at `px_per_mm`. `--page-size-mode 2` does not
    exist for `pcb export pdf` on 10.0/10.99 (only for svg), which is why the old path rasterised
    the whole A4/A3 sheet at ~2.4-3.4 px/mm.

    `bbox_mm` defaults to the Edge.Cuts outline + 1 mm; `mirror` flips the image left-right (a
    bottom view as seen from below). The longest edge stays <= `max_px`; the density is reduced
    to fit, never the region. This is the "svg" fallback named by `render_pcb`'s `Unsupported`.
    """
    caps = resolve_caps(install, caps)
    board, out_png = Path(board), Path(out_png)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    pdf_dir = out_png.parent
    opts: dict[str, object] = {"layers": list(layers)}
    # 7.0's `pcb export pdf` has neither flag; only pass what this version's help lists.
    # `--scale 1` pins the 1:1 page mapping the crop relies on (it is also the default).
    for name, flag, value in (
        ("exclude_drawing_sheet", "--exclude-drawing-sheet", True),
        ("scale", "--scale", 1),
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
    region = tuple(float(v) for v in bbox_mm) if bbox_mm is not None else board_region(board)
    try:
        if region is None:
            rasterise_pdf(pdf, out_png, max_px=max_px)
            if mirror:
                mirror_png(out_png)
        else:
            plan = plan_raster(region, px_per_mm=px_per_mm, max_px=max_px)
            rasterise_pdf_region(
                pdf, out_png, bbox_mm=plan.bbox_mm, px_per_mm=plan.px_per_mm, mirror=mirror, max_px=max_px
            )
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
    out_png.write_bytes(encode_png(width, height, _rgb_rows(raw, width, height, stride, channels)))
    return out_png


def rasterise_pdf_region(
    pdf: Path,
    out_png: Path,
    *,
    bbox_mm: Sequence[float],
    px_per_mm: float,
    mirror: bool = False,
    page: int = 0,
    max_px: int | None = None,
) -> Path:
    """Render the region `bbox_mm` (board mm, y down, measured from the page's top-left corner as
    kicad-cli plots at `--scale 1`) of one PDF page at `px_per_mm` into a PNG.

    The page's MediaBox and CropBox are moved onto the region and the page is re-loaded (PDFium
    caches the box at load), so the region may extend past the paper (spike_crop: a
    (-20, -20)-(80, 70) mm region rendered the board corner in place). `mirror` flips left-right."""
    try:
        import pypdfium2 as pdfium
    except ImportError as exc:
        raise RenderDependencyMissing("render.pypdfium2", fix=_INSTALL_RENDER_EXTRA) from exc
    x0, y0, x1, y1 = (float(v) for v in bbox_mm)
    pdf, out_png = Path(pdf), Path(out_png)
    doc = pdfium.PdfDocument(str(pdf))
    try:
        count = len(doc)
        if page < 0 or page >= count:
            raise PdfPageError(page, count)
        pg = doc[page]
        _, page_h = pg.get_size()
        left, right = x0 * _PT_PER_MM, x1 * _PT_PER_MM
        top, bottom = page_h - y0 * _PT_PER_MM, page_h - y1 * _PT_PER_MM
        pg.set_mediabox(left, bottom, right, top)
        pg.set_cropbox(left, bottom, right, top)
        pg.close()
        pg = doc[page]
        bitmap = pg.render(scale=px_per_mm / _PT_PER_MM, rev_byteorder=True, fill_color=(255, 255, 255, 255))
        try:
            width, height, stride = bitmap.width, bitmap.height, bitmap.stride
            channels = bitmap.n_channels
            raw = bytes(bitmap.buffer)
        finally:
            bitmap.close()
    finally:
        doc.close()
    rows = _rgb_rows(raw, width, height, stride, channels)
    if max_px is not None and max(width, height) > max_px:
        # PDFium's rounding can add a pixel row/column: trim it rather than halve the image
        width, height = min(width, max_px), min(height, max_px)
        rows = [r[: width * 3] for r in rows[:height]]
    if mirror:
        rows = [_mirror_row(r) for r in rows]
    out_png.parent.mkdir(parents=True, exist_ok=True)
    out_png.write_bytes(encode_png(width, height, rows))
    return out_png


def mirror_png(path: Path) -> Path:
    """Flip a PNG left-right in place (RGB or RGBA; written back as RGB)."""
    path = Path(path)
    width, height, channels, rows = decode_png(path.read_bytes())
    if channels == 4:
        rows = [_drop_alpha(r) for r in rows]
    path.write_bytes(encode_png(width, height, [_mirror_row(r) for r in rows]))
    return path


def _rgb_rows(raw: bytes, width: int, height: int, stride: int, channels: int) -> list[bytes]:
    rows = [raw[y * stride : y * stride + width * channels] for y in range(height)]
    if channels == 4:
        return [_drop_alpha(r) for r in rows]
    if channels == 1:
        return [bytes(b for px in r for b in (px, px, px)) for r in rows]
    return rows


def _mirror_row(row: bytes) -> bytes:
    """Reverse the pixel order of one RGB row (slice arithmetic, no per-pixel Python loop)."""
    rev = row[::-1]  # pixels reversed AND each pixel's bytes reversed (B, G, R)
    out = bytearray(len(row))
    out[0::3], out[1::3], out[2::3] = rev[2::3], rev[1::3], rev[0::3]
    return bytes(out)


def is_blank_png(path: Path) -> bool:
    """True when every pixel of an RGB(A) PNG is white (a region that holds no board)."""
    _, _, channels, rows = decode_png(Path(path).read_bytes())
    for r in rows:
        px = _drop_alpha(r) if channels == 4 else r
        if px.count(255) != len(px):
            return False
    return True


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
    """RGBA -> RGB by slice assignment (a 4096 px crop has 16 M pixels; no per-byte loop)."""
    n = len(row) // 4
    out = bytearray(n * 3)
    out[0::3], out[1::3], out[2::3] = row[0::4], row[1::4], row[2::4]
    return bytes(out)


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
