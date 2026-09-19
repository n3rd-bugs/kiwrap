"""`pcb render` (9.0/10.0) -> PNG bytes with the PNG signature, downscaled to `max_px`;
`Unsupported(fallback="svg")` on 7/8; the 2D fallback (`pcb export pdf` + pypdfium2) on every
version; the stdlib PNG codec offline."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from kiwrap.backends.cli import render
from kiwrap.capability import Unsupported
from tests._fakes import (
    demo_project,
    env_for,
    spike_board,
)

# --------------------------------------------------------------------------------------------
# Offline: PNG codec + downscale
# --------------------------------------------------------------------------------------------


def _gradient(width: int, height: int, channels: int) -> list[bytes]:
    rows = []
    for y in range(height):
        row = bytearray()
        for x in range(width):
            px = [(x * 7) & 0xFF, (y * 5) & 0xFF, (x + y) & 0xFF, 0xFF]
            row += bytes(px[:channels])
        rows.append(bytes(row))
    return rows


@pytest.mark.parametrize("channels", [3, 4])
def test_png_roundtrip(channels):
    rows = _gradient(37, 11, channels)
    data = render.encode_png(37, 11, rows, channels=channels)
    assert data.startswith(render.PNG_SIGNATURE)
    assert render.png_size(data) == (37, 11)
    w, h, c, back = render.decode_png(data)
    assert (w, h, c) == (37, 11, channels) and back == rows


def test_downscale_box_filter():
    rows = _gradient(64, 32, 3)
    w, h, small = render.downscale_rows(rows, 64, 32, 3, max_px=16)
    assert (w, h) == (16, 8) and len(small) == 8 and len(small[0]) == 16 * 3
    same_w, same_h, same = render.downscale_rows(rows, 64, 32, 3, max_px=64)
    assert (same_w, same_h) == (64, 32) and same == rows


def test_downscale_png_file(tmp_path: Path):
    src = tmp_path / "big.png"
    src.write_bytes(render.encode_png(100, 50, _gradient(100, 50, 3)))
    dst = render.downscale_png(src, tmp_path / "small.png", max_px=40)
    assert render.png_size(dst.read_bytes()) == (33, 16)


def test_rasterise_svg_without_resvg_is_unsupported(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(render.shutil, "which", lambda _name: None)
    with pytest.raises(Unsupported) as ei:
        render.rasterise_svg(tmp_path / "x.svg", tmp_path / "x.png")
    assert isinstance(ei.value, render.RenderDependencyMissing) and "resvg" in ei.value.capability


def test_rasterise_pdf_requires_pypdfium2(tmp_path: Path, monkeypatch):
    import builtins

    real_import = builtins.__import__

    def _no_pdfium(name, *a, **k):
        if name == "pypdfium2":
            raise ImportError("gone")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _no_pdfium)
    with pytest.raises(Unsupported) as ei:
        render.rasterise_pdf(tmp_path / "x.pdf", tmp_path / "x.png")
    assert ei.value.fix == "uv sync --extra render"


#: A minimal one-page PDF pypdfium2 opens (no xref table; pdfium reconstructs it).
_ONE_PAGE_PDF = b"\n".join(
    [
        b"%PDF-1.1",
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj",
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj",
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 100]>>endobj",
        b"trailer<</Root 1 0 R>>",
    ]
)


def test_rasterise_pdf_page_out_of_range_is_a_value_error_with_the_count(tmp_path: Path):
    """Finding: an out-of-range page escaped as pypdfium2's bare PdfiumError (E_INTERNAL upstream)."""
    pytest.importorskip("pypdfium2")
    pdf = tmp_path / "one.pdf"
    pdf.write_bytes(_ONE_PAGE_PDF)
    with pytest.raises(ValueError) as ei:
        render.rasterise_pdf(pdf, tmp_path / "x.png", page=4)
    assert isinstance(ei.value, render.PdfPageError) and ei.value.page_count == 1
    assert "page 5 does not exist" in str(ei.value)
    out = render.rasterise_pdf(pdf, tmp_path / "ok.png", max_px=100, page=0)
    assert out.is_file() and out.stat().st_size > 0


# --------------------------------------------------------------------------------------------
# Live
# --------------------------------------------------------------------------------------------

_RENDER = [
    pytest.param("9.0", marks=pytest.mark.kicad_version("9.0")),
    pytest.param("10.0", marks=pytest.mark.kicad_version("10.0")),
]
_NO_RENDER = [
    pytest.param("7.0", marks=pytest.mark.kicad_version("7.0")),
    pytest.param("8.0", marks=pytest.mark.kicad_version("8.0")),
]
_ALL = _NO_RENDER + _RENDER


@pytest.mark.kicad
@pytest.mark.usefixtures("user_config_untouched")
@pytest.mark.parametrize("mm", _RENDER)
def test_render_pcb_png(cli_envs, mm, tmp_path: Path):
    env = env_for(cli_envs, mm)
    proj = demo_project(env.install, tmp_path)
    out = render.render_pcb(
        env.install,
        env.config,
        proj / "ecc83-pp.kicad_pcb",
        tmp_path / "r" / "top.png",
        caps=env.caps,
        width=1600,
        height=900,
        max_px=800,
    )
    data = out.read_bytes()
    assert data.startswith(render.PNG_SIGNATURE)
    w, h = render.png_size(data)
    assert max(w, h) <= 800 and w > h > 0
    assert len(data) < 400_000
    assert not list(proj.glob("*.kicad_prl"))


@pytest.mark.kicad
@pytest.mark.usefixtures("user_config_untouched")
@pytest.mark.parametrize("mm", _RENDER)
def test_render_pcb_bottom_small_no_downscale(cli_envs, mm, tmp_path: Path):
    env = env_for(cli_envs, mm)
    board = tmp_path / "hb.kicad_pcb"
    shutil.copy(spike_board(mm, "test_inplace.kicad_pcb"), board)
    out = render.render_pcb(
        env.install,
        env.config,
        board,
        tmp_path / "bottom.png",
        caps=env.caps,
        width=400,
        height=300,
        side="bottom",
        zoom=1.2,
    )
    assert out.read_bytes().startswith(render.PNG_SIGNATURE)
    assert max(render.png_size(out.read_bytes())) <= 400


@pytest.mark.kicad
@pytest.mark.usefixtures("user_config_untouched")
@pytest.mark.parametrize("mm", _NO_RENDER)
def test_render_unsupported_before_9(cli_envs, mm, tmp_path: Path):
    env = env_for(cli_envs, mm)
    with pytest.raises(Unsupported) as ei:
        render.render_pcb(
            env.install,
            env.config,
            spike_board(mm, "test_inplace.kicad_pcb"),
            tmp_path / "x.png",
            caps=env.caps,
        )
    assert ei.value.capability == "cli.has_render" and ei.value.fallback == "svg"
    assert ei.value.oldest_kicad_with_it == "9.0.0"


@pytest.mark.kicad
@pytest.mark.usefixtures("user_config_untouched")
@pytest.mark.parametrize("mm", _ALL)
def test_render_pcb_2d_fallback(cli_envs, mm, tmp_path: Path):
    pytest.importorskip("pypdfium2")
    env = env_for(cli_envs, mm)
    board = tmp_path / "hb.kicad_pcb"
    shutil.copy(spike_board(mm, "test_inplace.kicad_pcb"), board)
    out = render.render_pcb_2d(
        env.install, env.config, board, tmp_path / "2d" / "hb.png", caps=env.caps, max_px=512
    )
    data = out.read_bytes()
    assert data.startswith(render.PNG_SIGNATURE)
    assert max(render.png_size(data)) <= 512
    assert not (tmp_path / "2d" / "hb.preview.pdf").exists(), "intermediate PDF removed"
    # not a blank page: some pixels differ from white
    _, _, _, rows = render.decode_png(data)
    assert any(b != 0xFF for r in rows for b in r)
