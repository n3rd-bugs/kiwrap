"""kw_sch_render / kw_pcb_render: pictures the agent can look at.

docs/ARCHITECTURE.md §9: images <= 1024 px and target < 100 KB (Claude Code counts image tokens
inside its result cap with no file spill), always accompanied by a numeric summary so a client
that drops images still gets something. Board renders use `kicad-cli pcb render` where
`cli.has_render` (docs/VERIFICATION.md claim 11 rendered ecc83 at 320x240 in ~10 KB) and fall
back to the backend's 2D preview (`pcb export pdf` + pypdfium2, every KiCad version);
schematics go through `sch export pdf` + pypdfium2. When the rasteriser is missing (`render`
extra not installed) the tool returns the summary with `degraded: ["render_unavailable"]` and the
path of the PDF/SVG kicad-cli produced instead of failing.

Review items (requests/kiwrap-feature-requests.review.md):
- item 7 / B4: every render writes into its own per-call directory
  (`kiwrap.core.fab.outputs.per_call_dir`, keyed by project name + sha8 of its path), or to the
  caller's `out_path`; the next render never deletes it. Old per-call directories are swept by
  age (24 h) and count (20 per project and kind). The receipt carries `png_sha256` and
  `kept_until` (null for a caller path, which is never swept).
- item 38: `kw_pcb_render` takes `bbox_mm` (a board region), `px_per_mm` (up to 60), any layer
  list (`In1.Cu`, `F.Paste`, `B.Mask`, ...) and `mirror_bottom`; giving any of them selects the 2D
  plot (the old default silently ignored `layers` with `view='3d'`). The 2D plot is cropped to the
  board outline + 1 mm instead of rasterising the whole drawing sheet. With `bbox_mm` and
  `px_per_mm` the 1024 px cap is lifted to 4096 px; an image above `INLINE_MAX_PX` / `INLINE_MAX_BYTES` is not
  inlined - the receipt returns its path (`data.inline=false`).
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from kiwrap.core.fab.outputs import check_caller_path, kept_until, per_call_dir
from kiwrap.core.state import KiwrapError, Receipt
from kiwrap.core.state.project import Project
from kiwrap.core.state.receipts import png_size
from kiwrap.core.tools._base import (
    READ_ONLY,
    ToolContext,
    ToolSpec,
    effective_target,
    read_state,
    to_kiwrap_error,
)

MAX_PX = 1024
#: region crops (`bbox_mm`) may be this large on the long edge (review item 38)
MAX_CROP_PX = 4096
#: an image larger than this (either edge / bytes) is written but not inlined in the result
INLINE_MAX_PX = 2048
INLINE_MAX_BYTES = 1_500_000
MAX_PX_PER_MM = 60.0
TARGET_BYTES = 100_000
SIZES_3D = ((1024, 768), (800, 600), (640, 480), (480, 360))
DEFAULT_LAYERS = {"top": ["F.Cu", "F.SilkS", "Edge.Cuts"], "bottom": ["B.Cu", "B.SilkS", "Edge.Cuts"]}
BOARD_MARGIN_MM = 1.0


def render_dir(ctx: ToolContext, project: Project, kind: str) -> Path:
    """A fresh per-call directory for one render (review B4: the old version `rmtree`d the shared
    `<storage>/tmp/render/<name>/<kind>` on every call)."""
    return per_call_dir(ctx.services.storage_home, project, kind, area="render")


def _out_file(
    ctx: ToolContext, project: Project, kind: str, out_path: str | None, default_name: str
) -> tuple[Path, Path, bool]:
    """(png path, working directory for intermediates, owned-by-kiwrap). A caller `out_path` is
    validated (absolute, `.png`, existing directory, never a design file / KiCad config); the
    intermediates (PDF) still go to a per-call directory under the storage home."""
    work = render_dir(ctx, project, kind)
    if out_path:
        return check_caller_path(ctx, project, out_path, what="out_path", suffixes=(".png",)), work, False
    return work / default_name, work, True


def image_summary(png: Path, *, mode: str, cap: int = MAX_PX) -> tuple[bytes, dict[str, Any]]:
    data = png.read_bytes()
    size = png_size(data)
    if size and max(size) > cap:
        raise KiwrapError(
            "E_INTERNAL",
            f"render is {size[0]}x{size[1]} px, larger than the {cap} px cap",
            why="the rasteriser ignored the size request",
            fix="report this; the file is at " + png.as_posix(),
        )
    return data, {
        "ok": True,
        "mode": mode,
        "width": size[0] if size else None,
        "height": size[1] if size else None,
        "bytes": len(data),
        "png": png.as_posix(),
        "png_sha256": hashlib.sha256(data).hexdigest(),
    }


def _keep_info(summary: dict[str, Any], owned: bool) -> None:
    if owned:
        summary["kept_until"] = kept_until()
    else:
        summary["kept_until"] = None  # the caller's file: never swept


def _deliver(data: bytes, summary: dict[str, Any], warnings: list[str]) -> bytes | None:
    """The PNG bytes to inline, or None when the image exceeds the inline budget (the path is in
    `summary['png']`)."""
    w, h = summary.get("width") or 0, summary.get("height") or 0
    if max(w, h) > INLINE_MAX_PX or len(data) > INLINE_MAX_BYTES:
        summary["inline"] = False
        warnings.append(
            f"image {w}x{h} px / {len(data)} bytes is above the inline budget ({INLINE_MAX_PX} px, "
            f"{INLINE_MAX_BYTES} bytes): not attached - open {summary['png']} or request a smaller bbox_mm / px_per_mm"
        )
        return None
    summary["inline"] = True
    return data


def _unsupported_or_raise(exc: Exception) -> KiwrapError:
    err = to_kiwrap_error(exc)
    if err.code != "E_UNSUPPORTED":
        raise err from exc
    return err


# ---- kw_pcb_render ----------------------------------------------------------------------------


class PcbRenderArgs(BaseModel):
    project: str = Field(description="project directory or .kicad_pro path")
    view: Literal["3d", "2d"] | None = Field(
        default=None,
        description=(
            "3d: kicad-cli pcb render when the install has cli.has_render (falls back to 2d); 2d: layer plot. "
            "Default: 2d when layers / bbox_mm / px_per_mm / mirror_bottom is given, else 3d"
        ),
    )
    side: Literal["top", "bottom"] = Field(default="top", description="which side to look at")
    layers: list[str] | None = Field(
        default=None,
        description=(
            "2d: layer names to plot, any of the board's (e.g. ['In1.Cu', 'F.Paste', 'F.Mask', 'Edge.Cuts']); "
            "default copper + silk + Edge.Cuts of the side"
        ),
    )
    bbox_mm: list[float] | None = Field(
        default=None,
        min_length=4,
        max_length=4,
        description="2d: board region [x0, y0, x1, y1] in mm (KiCad coordinates, y down); default the board outline + 1 mm",
    )
    px_per_mm: float | None = Field(
        default=None,
        gt=0,
        le=MAX_PX_PER_MM,
        description=(
            f"2d: pixels per millimetre (<= {MAX_PX_PER_MM:g}); with bbox_mm the image may reach {MAX_CROP_PX} px, "
            "else it is reduced to fit max_px. Default: the region fitted to max_px"
        ),
    )
    mirror_bottom: bool = Field(
        default=False,
        description="2d with side='bottom': flip left-right so the image is the view from below",
    )
    max_px: int = Field(
        default=MAX_PX,
        ge=128,
        le=MAX_PX,
        description=(
            "longest edge in pixels for a full view or a region without px_per_mm (3d renders use the largest 4:3 "
            f"size at or below it); bbox_mm + px_per_mm may go up to {MAX_CROP_PX} px"
        ),
    )
    out_path: str | None = Field(
        default=None,
        description="absolute .png path to write (kept, never swept); default a per-call file under the storage home",
    )


def _board_bbox(ctx: ToolContext, project: Project) -> tuple[float, float, float, float] | None:
    try:
        model = ctx.services.file_read_board(project.board)
        box = model.outline_bbox()
    except Exception:  # noqa: BLE001 - optional reader; the backend falls back on its own
        return None
    if box is None:
        return None
    m = BOARD_MARGIN_MM
    return (box.min_x - m, box.min_y - m, box.max_x + m, box.max_y + m)


def _board_layers(ctx: ToolContext, project: Project) -> list[tuple[str, str | None]]:
    from kiwrap.core.fab.plan import board_layer_names

    try:
        return board_layer_names(ctx.services.file_read_board(project.board))
    except Exception:  # noqa: BLE001
        return []


def _check_layers(ctx: ToolContext, project: Project, layers: list[str]) -> list[str]:
    from kiwrap.core.fab.plan import normalise_layers

    table = _board_layers(ctx, project)
    canon, unknown = normalise_layers(layers, table)
    if unknown:
        raise KiwrapError(
            "E_BAD_ARGS",
            f"unknown layer name(s) {', '.join(repr(u) for u in unknown)} for this board",
            why="the 2d plot takes the board's layer names (untranslated)",
            fix="pick from the board's layers",
            candidates=[n for n, _ in table],
            example='kw_pcb_render(project=..., layers=["In1.Cu", "Edge.Cuts"], bbox_mm=[100, 50, 120, 70], px_per_mm=40)',
        )
    return canon


def _plan(
    region: tuple[float, float, float, float] | None, args: PcbRenderArgs, cap: int, warnings: list[str]
) -> dict[str, Any] | None:
    from kiwrap.backends.cli.render import plan_raster

    if region is None:
        return None
    try:
        plan = plan_raster(region, px_per_mm=args.px_per_mm, max_px=cap)
    except ValueError as exc:
        raise KiwrapError(
            "E_BAD_ARGS",
            str(exc),
            fix="pass bbox_mm as [x0, y0, x1, y1] with x1 > x0 and y1 > y0 (mm, y down)",
            example="kw_pcb_render(project=..., bbox_mm=[100, 50, 120, 70], px_per_mm=40)",
        ) from exc
    if plan.capped:
        warnings.append(
            f"px_per_mm {args.px_per_mm:g} would exceed {cap} px for this region; rendered at {plan.px_per_mm:.2f} px/mm"
            + (" - pass a smaller bbox_mm to zoom in" if args.bbox_mm is None else "")
        )
    return plan.to_dict()


def pcb_render(ctx: ToolContext, args: PcbRenderArgs) -> Receipt:
    project = Project.from_path(args.project)
    state, _ = read_state(project)  # in memory only inside a non-persisting call (D23)
    if not project.board.exists():
        raise KiwrapError(
            "E_PROJECT_NOT_FOUND", f"board {project.board.name} is missing", fix="kw_project create writes it"
        )
    install = effective_target(ctx, project, state, for_write=False).install
    caps = ctx.services.caps(install)
    config = ctx.services.config_root(install)
    provider = ctx.services.provider(install)
    degraded: list[str] = []
    warnings: list[str] = []
    wants_2d = bool(args.layers or args.bbox_mm or args.px_per_mm or args.mirror_bottom)
    mode = args.view or ("2d" if wants_2d else "3d")
    if mode == "3d" and wants_2d:
        warnings.append(
            "layers / bbox_mm / px_per_mm / mirror_bottom apply to the 2d plot only; ignored with view='3d'"
        )
    # review item 38: a region crop at an explicit density lifts the 1024 px cap (to 4096 px);
    # everything else is fitted to max_px (<= 1024, docs/ARCHITECTURE.md §9)
    crop = args.bbox_mm is not None and args.px_per_mm is not None
    max_px = args.max_px
    raster_cap = MAX_CROP_PX if crop else max_px
    png_out, work, owned = _out_file(ctx, project, "pcb", args.out_path, f"board_{args.side}.png")
    png: Path | None = None

    if mode == "3d":
        if caps.has("cli.has_render"):
            # always at least one attempt: below the smallest preset the request's own 4:3 size is used
            sizes = [s for s in SIZES_3D if s[0] <= max_px] or [(max_px, round(max_px * 0.75))]
            for w, h in sizes:
                candidate = work / f"board_{args.side}_{w}x{h}.png"
                png = ctx.services.render_pcb(
                    install,
                    config,
                    project.board,
                    candidate,
                    width=w,
                    height=h,
                    side=args.side,
                    max_px=max_px,
                )
                if png.stat().st_size <= TARGET_BYTES:
                    break
                warnings.append(
                    f"{w}x{h} render is {png.stat().st_size} bytes (> {TARGET_BYTES}); trying smaller"
                )
            if png is not None and png != png_out:
                shutil.copyfile(png, png_out)
                png = png_out
        else:
            degraded.append("render_3d_unavailable")
            warnings.append("kicad-cli pcb render is not available on this KiCad; using the 2d layer plot")
            mode = "2d"

    layers = _check_layers(ctx, project, args.layers) if args.layers else DEFAULT_LAYERS[args.side]
    region: dict[str, Any] | None = None
    mirror = bool(args.mirror_bottom and args.side == "bottom")
    if args.mirror_bottom and args.side != "bottom":
        warnings.append("mirror_bottom applies to side='bottom' only")
    if png is None:
        bbox: tuple[float, float, float, float] | None
        if args.bbox_mm is not None:
            b = [float(v) for v in args.bbox_mm]
            bbox = (b[0], b[1], b[2], b[3])
        else:
            bbox = _board_bbox(ctx, project)
        region = _plan(bbox, args, raster_cap, warnings)
        try:
            png = ctx.services.render_pcb_2d(
                install,
                config,
                project.board,
                png_out,
                layers=layers,
                max_px=raster_cap,
                bbox_mm=list(bbox) if bbox is not None else None,
                px_per_mm=args.px_per_mm,
                mirror=mirror,
            )
        except Exception as exc:  # noqa: BLE001 - only E_UNSUPPORTED degrades; everything else propagates as an error
            err = _unsupported_or_raise(exc)
            degraded.append("render_unavailable")
            warnings.append(f"no rasteriser: {err.message}")
            files = ctx.services.export_pcb(install, config, project.board, "svg", work, layers=layers)
            return Receipt(
                provider_used=provider,
                degraded=degraded,
                warnings=warnings,
                stage=state.stage,
                data={
                    "ok": True,
                    "mode": "svg",
                    "files": [Path(p).as_posix() for p in files],
                    "png": None,
                    "layers": layers,
                    "side": args.side,
                },
            )

    data, summary = image_summary(png, mode=mode, cap=raster_cap if mode == "2d" else max_px)
    summary["side"] = args.side
    if mode == "2d":
        summary["layers"] = layers
        summary["mirrored"] = mirror
        if region is not None:
            summary["region"] = region
        if _blank(png):
            summary["blank"] = True
            warnings.append("the rendered region holds no board content (outside the board, or empty layers)")
    _keep_info(summary, owned)
    image = _deliver(data, summary, warnings)
    return Receipt(
        provider_used=provider,
        degraded=degraded,
        warnings=warnings,
        stage=state.stage,
        image=image,
        data=summary,
    )


def _blank(png: Path) -> bool:
    try:
        from kiwrap.backends.cli.render import is_blank_png

        return is_blank_png(png)
    except Exception:  # noqa: BLE001 - an image kiwrap cannot decode is not claimed blank
        return False


PCB_RENDER = ToolSpec(
    name="kw_pcb_render",
    title="Board render",
    description=(
        "Secondary; prefer kw_pcb_get/kw_pcb_check for feedback. PNG of the board: 3d via kicad-cli pcb render where "
        "supported, else (or with layers / bbox_mm / px_per_mm / mirror_bottom) a 2d layer plot cropped to the board. "
        "bbox_mm + px_per_mm (<= 60) zooms into a region (up to 4096 px; large images are returned by path). Every "
        "render gets its own file (png, png_sha256, kept_until) or goes to out_path. Always returns the numeric summary."
    ),
    input_model=PcbRenderArgs,
    handler=pcb_render,
    annotations=READ_ONLY,
)


# ---- kw_sch_render ----------------------------------------------------------------------------


class SchRenderArgs(BaseModel):
    project: str = Field(description="project directory or .kicad_pro path")
    sheet: str | None = Field(default=None, description="sheet file name (default: root schematic)")
    page: int = Field(
        default=1,
        ge=1,
        description="page of the exported PDF (hierarchical designs export one page per sheet)",
    )
    max_px: int = Field(default=MAX_PX, ge=128, le=MAX_PX, description="longest edge in pixels")
    out_path: str | None = Field(
        default=None,
        description="absolute .png path to write (kept, never swept); default a per-call file under the storage home",
    )


def sch_render(ctx: ToolContext, args: SchRenderArgs) -> Receipt:
    project = Project.from_path(args.project)
    state, _ = read_state(project)  # in memory only inside a non-persisting call (D23)
    sheet = project.root_sch if not args.sheet else project.dir / args.sheet
    if not sheet.exists():
        raise KiwrapError(
            "E_PROJECT_NOT_FOUND",
            f"schematic {sheet.name} is missing",
            candidates=[p.name for p in project.schematic_files()],
        )
    install = effective_target(ctx, project, state, for_write=False).install
    config = ctx.services.config_root(install)
    provider = ctx.services.provider(install)
    png_out, work, owned = _out_file(ctx, project, "sch", args.out_path, f"{sheet.stem}.png")
    pdfs = ctx.services.export_sch(install, config, sheet, "pdf", work / f"{sheet.stem}.pdf")
    pdf = next((Path(p) for p in pdfs if Path(p).suffix.lower() == ".pdf"), None)
    if pdf is None:
        raise KiwrapError(
            "E_KICAD_FAILED", "kicad-cli produced no PDF", data={"files": [Path(p).as_posix() for p in pdfs]}
        )
    try:
        png = ctx.services.rasterise_pdf(pdf, png_out, max_px=args.max_px, page=args.page - 1)
    except ValueError as exc:  # the backend's PdfPageError: the page is outside the exported PDF
        count = getattr(exc, "page_count", None)
        raise KiwrapError(
            "E_BAD_ARGS",
            f"page {args.page} does not exist; the PDF has {count if count is not None else 'fewer'} page(s)",
            why="hierarchical designs export one page per sheet; the root schematic alone is one page",
            fix="pass a page in range (1 = the first sheet)",
            candidates=[str(i) for i in range(1, count + 1)] if count else [],
            data={"pdf": pdf.as_posix(), "page_count": count},
        ) from exc
    except Exception as exc:  # noqa: BLE001 - only E_UNSUPPORTED degrades
        err = _unsupported_or_raise(exc)
        return Receipt(
            provider_used=provider,
            degraded=["render_unavailable"],
            warnings=[f"no rasteriser: {err.message}"],
            stage=state.stage,
            data={"ok": True, "mode": "pdf", "pdf": pdf.as_posix(), "png": None, "sheet": sheet.name},
        )
    data, summary = image_summary(png, mode="2d")
    summary["sheet"] = sheet.name
    summary["page"] = args.page
    _keep_info(summary, owned)
    return Receipt(provider_used=provider, stage=state.stage, image=data, data=summary)


SCH_RENDER = ToolSpec(
    name="kw_sch_render",
    title="Schematic render",
    description=(
        "Secondary; prefer kw_sch_get/kw_sch_stats for feedback. PNG of a schematic sheet (<= 1024 px) via "
        "kicad-cli PDF export + rasteriser; each render gets its own file (or out_path). Always returns the numeric summary."
    ),
    input_model=SchRenderArgs,
    handler=sch_render,
    annotations=READ_ONLY,
)
