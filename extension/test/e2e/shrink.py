"""Shrink the end-to-end evidence PNGs under a byte budget (the repo rule for
docs/research/display/img: small PNGs, < 300 KB): first palette-quantise (UI screenshots lose
nothing visible at 256 colours), then downscale in steps until the file fits.

    uv run --with pillow python shrink.py <dir-or-files...> [--max-kb 290] [--min-px 640]

Pillow is a tool-time dependency only (not in kiwrap's dependency set).
"""

from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

from PIL import Image


def shrink(path: Path, max_bytes: int, min_px: int) -> tuple[int, int, tuple[int, int]]:
    before = path.stat().st_size
    img = Image.open(path).convert("RGB")
    size = img.size
    if before <= max_bytes:
        return before, before, size

    def encode(im: Image.Image, colors: int) -> bytes:
        q = im.quantize(colors=colors, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.FLOYDSTEINBERG)
        buf = io.BytesIO()
        q.save(buf, format="PNG", optimize=True)
        return buf.getvalue()

    work = img
    while True:
        for colors in (256, 128, 64):
            data = encode(work, colors)
            if len(data) <= max_bytes:
                path.write_bytes(data)
                return before, len(data), work.size
        if max(work.size) <= min_px:
            path.write_bytes(data)  # the smallest we allow; report it over budget
            return before, len(data), work.size
        w, h = work.size
        scale = 0.85
        work = work.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.Resampling.LANCZOS)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--max-kb", type=int, default=290)
    ap.add_argument("--min-px", type=int, default=640)
    ap.add_argument("--glob", default="phase2-*.png")
    args = ap.parse_args()
    files: list[Path] = []
    for p in args.paths:
        path = Path(p)
        files.extend(sorted(path.glob(args.glob)) if path.is_dir() else [path])
    over = 0
    for f in files:
        before, after, size = shrink(f, args.max_kb * 1024, args.min_px)
        flag = "" if after <= args.max_kb * 1024 else "  OVER BUDGET"
        over += bool(flag)
        print(f"{f.name}: {before // 1024} KB -> {after // 1024} KB @ {size[0]}x{size[1]}{flag}")
    return 1 if over else 0


if __name__ == "__main__":
    sys.exit(main())
