"""kw_sch_render / kw_pcb_render: pictures the agent can look at.

docs/ARCHITECTURE.md §9: images <= 1024 px and target < 100 KB (Claude Code counts image tokens
inside its result cap with no file spill), always accompanied by a numeric summary so a client
that drops images still gets something. Board renders use `kicad-cli pcb render` where
`cli.has_render` (docs/VERIFICATION.md claim 11 rendered ecc83 at 320x240 in ~10 KB) and fall
back to the backend's 2D preview (`pcb export pdf` + pypdfium2, every KiCad version);
schematics go through `sch export pdf` + pypdfium2. When the rasteriser is missing (`render`
extra not installed) the tool returns the summary with `degraded: ["render_unavailable"]` and the
path of the PDF/SVG kicad-cli produced instead of failing.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from kiwrap.core.state import KiwrapError, Receipt
from kiwrap.core.state.project import Project
from kiwrap.core.state.receipts import png_size
from kiwrap.core.tools._base import READ_ONLY, ToolContext, ToolSpec, read_state, to_kiwrap_error

MAX_PX = 1024
TARGET_BYTES = 100_000
SIZES_3D = ((1024, 768), (800, 600), (640, 480), (480, 360))
DEFAULT_LAYERS = {"top": ["F.Cu", "F.SilkS", "Edge.Cuts"], "bottom": ["B.Cu", "B.SilkS", "Edge.Cuts"]}


def render_dir(ctx: ToolContext, project: Project, kind: str) -> Path:
    d = ctx.services.storage_home / "tmp" / "render" / project.name / kind
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True, exist_ok=True)
    return d


def image_summary(png: Path, *, mode: str) -> tuple[bytes, dict[str, Any]]:
    data = png.read_bytes()
    size = png_size(data)
    if size and max(size) > MAX_PX:
        raise KiwrapError(
            "E_INTERNAL",
            f"render is {size[0]}x{size[1]} px, larger than the {MAX_PX} px cap",
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
    }


def _unsupported_or_raise(exc: Exception) -> KiwrapError:
    err = to_kiwrap_error(exc)
    if err.code != "E_UNSUPPORTED":
        raise err from exc
    return err


# ---- kw_pcb_render ----------------------------------------------------------------------------


class PcbRenderArgs(BaseModel):
    project: str = Field(description="project directory or .kicad_pro path")
    view: Literal["3d", "2d"] = Field(
        default="3d",
        description="3d: kicad-cli pcb render when the install has cli.has_render (falls back to 2d); 2d: layer plot",
    )
    side: Literal["top", "bottom"] = Field(default="top", description="which side to look at")
    layers: list[str] | None = Field(
        default=None,
        description="2d only: layer names to plot; default copper + silk + Edge.Cuts of the side",
    )
    max_px: int = Field(
        default=MAX_PX,
        ge=128,
        le=MAX_PX,
        description="longest edge in pixels (3d renders use the largest 4:3 size at or below it)",
    )


def pcb_render(ctx: ToolContext, args: PcbRenderArgs) -> Receipt:
    project = Project.from_path(args.project)
    state, _ = read_state(project)
    if not project.board.exists():
        raise KiwrapError(
            "E_PROJECT_NOT_FOUND", f"board {project.board.name} is missing", fix="kw_project create writes it"
        )
    install = ctx.services.install(state.target_kicad)
    caps = ctx.services.caps(install)
    config = ctx.services.config_root(install)
    out_dir = render_dir(ctx, project, "pcb")
    provider = ctx.services.provider(install)
    degraded: list[str] = []
    warnings: list[str] = []
    png: Path | None = None
    mode = args.view

    if args.view == "3d":
        if caps.has("cli.has_render"):
            # always at least one attempt: below the smallest preset the request's own 4:3 size is used
            sizes = [s for s in SIZES_3D if s[0] <= args.max_px] or [(args.max_px, round(args.max_px * 0.75))]
            for w, h in sizes:
                candidate = out_dir / f"board_{args.side}_{w}x{h}.png"
                png = ctx.services.render_pcb(
                    install,
                    config,
                    project.board,
                    candidate,
                    width=w,
                    height=h,
                    side=args.side,
                    max_px=args.max_px,
                )
                if png.stat().st_size <= TARGET_BYTES:
                    break
                warnings.append(
                    f"{w}x{h} render is {png.stat().st_size} bytes (> {TARGET_BYTES}); trying smaller"
                )
        else:
            degraded.append("render_3d_unavailable")
            warnings.append("kicad-cli pcb render is not available on this KiCad; using the 2d layer plot")
            mode = "2d"

    layers = args.layers or DEFAULT_LAYERS[args.side]
    if png is None:
        try:
            png = ctx.services.render_pcb_2d(
                install,
                config,
                project.board,
                out_dir / f"board_{args.side}.png",
                layers=layers,
                max_px=args.max_px,
            )
        except Exception as exc:  # noqa: BLE001 - only E_UNSUPPORTED degrades; everything else propagates as an error
            err = _unsupported_or_raise(exc)
            degraded.append("render_unavailable")
            warnings.append(f"no rasteriser: {err.message}")
            files = ctx.services.export_pcb(install, config, project.board, "svg", out_dir, layers=layers)
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

    data, summary = image_summary(png, mode=mode)
    summary["side"] = args.side
    if mode == "2d":
        summary["layers"] = layers
    return Receipt(
        provider_used=provider,
        degraded=degraded,
        warnings=warnings,
        stage=state.stage,
        image=data,
        data=summary,
    )


PCB_RENDER = ToolSpec(
    name="kw_pcb_render",
    title="Board render",
    description=(
        "Secondary; prefer kw_pcb_get/kw_pcb_check for feedback. PNG of the board (<= 1024 px): 3d via kicad-cli "
        "pcb render where supported, else a 2d layer plot. Always returns the numeric summary."
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


def sch_render(ctx: ToolContext, args: SchRenderArgs) -> Receipt:
    project = Project.from_path(args.project)
    state, _ = read_state(project)
    sheet = project.root_sch if not args.sheet else project.dir / args.sheet
    if not sheet.exists():
        raise KiwrapError(
            "E_PROJECT_NOT_FOUND",
            f"schematic {sheet.name} is missing",
            candidates=[p.name for p in project.schematic_files()],
        )
    install = ctx.services.install(state.target_kicad)
    config = ctx.services.config_root(install)
    provider = ctx.services.provider(install)
    out_dir = render_dir(ctx, project, "sch")
    pdfs = ctx.services.export_sch(install, config, sheet, "pdf", out_dir / f"{sheet.stem}.pdf")
    pdf = next((Path(p) for p in pdfs if Path(p).suffix.lower() == ".pdf"), None)
    if pdf is None:
        raise KiwrapError(
            "E_KICAD_FAILED", "kicad-cli produced no PDF", data={"files": [Path(p).as_posix() for p in pdfs]}
        )
    try:
        png = ctx.services.rasterise_pdf(
            pdf, out_dir / f"{sheet.stem}.png", max_px=args.max_px, page=args.page - 1
        )
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
    return Receipt(provider_used=provider, stage=state.stage, image=data, data=summary)


SCH_RENDER = ToolSpec(
    name="kw_sch_render",
    title="Schematic render",
    description=(
        "Secondary; prefer kw_sch_get/kw_sch_stats for feedback. PNG of a schematic sheet (<= 1024 px) via "
        "kicad-cli PDF export + rasteriser. Always returns the numeric summary."
    ),
    input_model=SchRenderArgs,
    handler=sch_render,
    annotations=READ_ONLY,
)
