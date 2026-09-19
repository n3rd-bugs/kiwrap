"""`pcb export *` / `sch export *` adapters.

Verified facts encoded here (docs/ARCHITECTURE.md §2, spikes/versioning-strategy/clihelp/*.txt and
cli_flag_drift.txt — the `--help` of every subcommand on 7.0.6 / 8.0.1 / 9.0.5 / 10.0.0):
- `pcb export gerbers -o <dir>` and `pcb export drill -o <dir>` take a DIRECTORY on every version
  but only 9.0+ create it (`cli.gerbers_creates_outdir`); kiwrap always creates it and passes the
  path with a trailing separator so 7/8 do not treat it as a file-name prefix.
- `pcb export svg|pdf|dxf -o` is a FILE on 7/8; on 9/10 the same is obtained with `--mode-single`
  (present in `cli.flags.pcb export svg`), so a single file per call is produced everywhere.
  `--layers` takes a comma list of untranslated names; F.Cu, B.Cu, Edge.Cuts, F.SilkS are stable
  on 7-10.
- `sch export svg|dxf -o` is a DIRECTORY on 7-10 ("Output directory"); `sch export pdf|bom|netlist
  -o` is a FILE ("Output file"). The record may override this with `cli.output_kind.<subcommand>`.
- Subcommand availability: `sch export bom`, `pcb export glb/ipc2581` arrived in 8.0; `odb` in 9.0;
  `stats` in 10.0 (`cli.subcommands`, cli_flag_drift.txt). A flag that a version's `--help` does not
  list (e.g. `--exclude-dnp` for pos on 7.0, `--no-dnp` for step on 7.0) raises `Unsupported`
  rather than being passed blindly.
- KiCad 10.0 writes a `.kicad_prl` next to the board on read-only exports; the runner removes it.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kiwrap.backends.cli.netlist import export_netlist_file
from kiwrap.backends.cli.runner import CliResult, has_flag, has_subcommand, resolve_caps, run_kicad_cli
from kiwrap.capability import Capabilities, Unsupported
from kiwrap.installation import KiCadInstallation
from kiwrap.sandbox import ConfigRoot

PCB_TARGETS: tuple[str, ...] = (
    "gerbers",
    "drill",
    "pos",
    "pdf",
    "svg",
    "step",
    "dxf",
    "stats",
    "ipc2581",
    "odb",
    "glb",
)
SCH_TARGETS: tuple[str, ...] = ("pdf", "svg", "bom", "netlist", "dxf")

#: Layers whose untranslated names are identical on 7.0-10.0 (`--layers` help text on every version).
DEFAULT_PLOT_LAYERS: tuple[str, ...] = ("F.Cu", "B.Cu", "Edge.Cuts", "F.SilkS")

#: First kicad-cli release with the subcommand (cli_flag_drift.txt); absent = available since 7.0.
_OLDEST_WITH: Mapping[str, str] = {
    "pcb export glb": "8.0.0",
    "pcb export ipc2581": "8.0.0",
    "pcb export odb": "9.0.0",
    "pcb export stats": "10.0.0",
    "sch export bom": "8.0.0",
}

#: What `-o` means per subcommand on every verified version ("dir" | "file"); svg/pdf/dxf for
#: boards are "file" because `--mode-single` is added whenever the version lists it.
_OUTPUT_KIND: Mapping[str, str] = {
    "pcb export gerbers": "dir",
    "pcb export drill": "dir",
    "pcb export pos": "file",
    "pcb export pdf": "file",
    "pcb export svg": "file",
    "pcb export dxf": "file",
    "pcb export step": "file",
    "pcb export stats": "file",
    "pcb export ipc2581": "file",
    "pcb export odb": "file",
    "pcb export glb": "file",
    "sch export pdf": "file",
    "sch export svg": "dir",
    "sch export dxf": "dir",
    "sch export bom": "file",
    "sch export netlist": "file",
}

#: Default file extension for single-file outputs; `pos` and `stats` depend on `format`.
_EXT: Mapping[str, str] = {
    "pdf": ".pdf",
    "svg": ".svg",
    "dxf": ".dxf",
    "step": ".step",
    "ipc2581": ".xml",
    "odb": ".zip",
    "glb": ".glb",
    "bom": ".csv",
    "netlist": ".net",
}


@dataclass(frozen=True)
class _Opt:
    """How a Python keyword option maps to a kicad-cli flag."""

    flag: str
    kind: str  # "bool" | "value" | "csv" | "repeat"


# Options accepted by `export_pcb(**opts)` / `export_sch(**opts)`. Every flag below was read from
# the version help texts; availability is still checked per version at call time.
_PCB_OPTS: Mapping[str, _Opt] = {
    "layers": _Opt("--layers", "csv"),
    "common_layers": _Opt("--common-layers", "csv"),
    "drawing_sheet": _Opt("--drawing-sheet", "value"),
    "define_vars": _Opt("--define-var", "repeat"),
    "exclude_refdes": _Opt("--exclude-refdes", "bool"),
    "exclude_value": _Opt("--exclude-value", "bool"),
    "include_border_title": _Opt("--include-border-title", "bool"),
    "exclude_drawing_sheet": _Opt("--exclude-drawing-sheet", "bool"),
    "page_size_mode": _Opt("--page-size-mode", "value"),
    "fit_page_to_board": _Opt("--fit-page-to-board", "bool"),
    "mirror": _Opt("--mirror", "bool"),
    "negative": _Opt("--negative", "bool"),
    "black_and_white": _Opt("--black-and-white", "bool"),
    "theme": _Opt("--theme", "value"),
    "drill_shape_opt": _Opt("--drill-shape-opt", "value"),
    "subtract_soldermask": _Opt("--subtract-soldermask", "bool"),
    "sketch_pads_on_fab_layers": _Opt("--sketch-pads-on-fab-layers", "bool"),
    "hide_dnp_on_fab": _Opt("--hide-DNP-footprints-on-fab-layers", "bool"),
    "check_zones": _Opt("--check-zones", "bool"),
    "scale": _Opt("--scale", "value"),
    "bg_color": _Opt("--bg-color", "value"),
    # gerbers
    "no_x2": _Opt("--no-x2", "bool"),
    "no_netlist": _Opt("--no-netlist", "bool"),
    "disable_aperture_macros": _Opt("--disable-aperture-macros", "bool"),
    "use_drill_file_origin": _Opt("--use-drill-file-origin", "bool"),
    "precision": _Opt("--precision", "value"),
    "no_protel_ext": _Opt("--no-protel-ext", "bool"),
    "board_plot_params": _Opt("--board-plot-params", "bool"),
    # drill
    "format": _Opt("--format", "value"),
    "drill_origin": _Opt("--drill-origin", "value"),
    "excellon_zeros_format": _Opt("--excellon-zeros-format", "value"),
    "excellon_oval_format": _Opt("--excellon-oval-format", "value"),
    "excellon_units": _Opt("--excellon-units", "value"),
    "excellon_mirror_y": _Opt("--excellon-mirror-y", "bool"),
    "excellon_min_header": _Opt("--excellon-min-header", "bool"),
    "excellon_separate_th": _Opt("--excellon-separate-th", "bool"),
    "generate_map": _Opt("--generate-map", "bool"),
    "map_format": _Opt("--map-format", "value"),
    "gerber_precision": _Opt("--gerber-precision", "value"),
    # pos
    "side": _Opt("--side", "value"),
    "units": _Opt("--units", "value"),
    "bottom_negate_x": _Opt("--bottom-negate-x", "bool"),
    "smd_only": _Opt("--smd-only", "bool"),
    "exclude_fp_th": _Opt("--exclude-fp-th", "bool"),
    "exclude_dnp": _Opt("--exclude-dnp", "bool"),
    "gerber_board_edge": _Opt("--gerber-board-edge", "bool"),
    # step / glb
    "force": _Opt("--force", "bool"),
    "no_dnp": _Opt("--no-dnp", "bool"),
    "no_unspecified": _Opt("--no-unspecified", "bool"),
    "grid_origin": _Opt("--grid-origin", "bool"),
    "subst_models": _Opt("--subst-models", "bool"),
    "board_only": _Opt("--board-only", "bool"),
    "no_components": _Opt("--no-components", "bool"),
    "include_tracks": _Opt("--include-tracks", "bool"),
    "include_pads": _Opt("--include-pads", "bool"),
    "include_zones": _Opt("--include-zones", "bool"),
    "include_silkscreen": _Opt("--include-silkscreen", "bool"),
    "include_soldermask": _Opt("--include-soldermask", "bool"),
    "min_distance": _Opt("--min-distance", "value"),
    "no_optimize_step": _Opt("--no-optimize-step", "bool"),
    "user_origin": _Opt("--user-origin", "value"),
    # stats
    "exclude_footprints_without_pads": _Opt("--exclude-footprints-without-pads", "bool"),
    "subtract_holes_from_board": _Opt("--subtract-holes-from-board", "bool"),
    "subtract_holes_from_copper": _Opt("--subtract-holes-from-copper", "bool"),
    # ipc2581 / odb
    "compress": _Opt("--compress", "bool"),
    "compression": _Opt("--compression", "value"),
    "variant": _Opt("--variant", "repeat"),
}

_SCH_OPTS: Mapping[str, _Opt] = {
    "drawing_sheet": _Opt("--drawing-sheet", "value"),
    "define_vars": _Opt("--define-var", "repeat"),
    "theme": _Opt("--theme", "value"),
    "black_and_white": _Opt("--black-and-white", "bool"),
    "exclude_drawing_sheet": _Opt("--exclude-drawing-sheet", "bool"),
    "no_background_color": _Opt("--no-background-color", "bool"),
    "default_font": _Opt("--default-font", "value"),
    "pages": _Opt("--pages", "csv"),
    "exclude_pdf_property_popups": _Opt("--exclude-pdf-property-popups", "bool"),
    "exclude_pdf_hierarchical_links": _Opt("--exclude-pdf-hierarchical-links", "bool"),
    "exclude_pdf_metadata": _Opt("--exclude-pdf-metadata", "bool"),
    "draw_hop_over": _Opt("--draw-hop-over", "bool"),
    # bom
    "preset": _Opt("--preset", "value"),
    "format_preset": _Opt("--format-preset", "value"),
    "fields": _Opt("--fields", "csv"),
    "labels": _Opt("--labels", "csv"),
    "group_by": _Opt("--group-by", "csv"),
    "sort_field": _Opt("--sort-field", "value"),
    "sort_asc": _Opt("--sort-asc", "bool"),
    "filter": _Opt("--filter", "value"),
    "exclude_dnp": _Opt("--exclude-dnp", "bool"),
    "include_excluded_from_bom": _Opt("--include-excluded-from-bom", "bool"),
    "field_delimiter": _Opt("--field-delimiter", "value"),
    "string_delimiter": _Opt("--string-delimiter", "value"),
    "ref_delimiter": _Opt("--ref-delimiter", "value"),
    "ref_range_delimiter": _Opt("--ref-range-delimiter", "value"),
    "keep_tabs": _Opt("--keep-tabs", "bool"),
    "keep_line_breaks": _Opt("--keep-line-breaks", "bool"),
    # netlist
    "format": _Opt("--format", "value"),
    "variant": _Opt("--variant", "repeat"),
}


def _csv(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, Iterable):
        return ",".join(str(v) for v in value)
    return str(value)


def _opts_to_args(
    caps: Capabilities, subcommand: str, table: Mapping[str, _Opt], opts: Mapping[str, Any]
) -> list[str]:
    """Translate keyword options into argv, refusing flags this version's `--help` does not list."""
    args: list[str] = []
    for name, value in opts.items():
        if name not in table:
            raise ValueError(f"{subcommand}: unknown option {name!r}; known: {sorted(table)}")
        opt = table[name]
        if value is None or (opt.kind == "bool" and not value):
            continue
        if not has_flag(caps, subcommand, opt.flag):
            raise Unsupported(
                f"cli.flags.{subcommand}:{opt.flag}",
                caps.install,
                oldest_kicad_with_it=None,
                fallback=f"omit {name!r}",
            )
        if opt.kind == "bool":
            args.append(opt.flag)
        elif opt.kind == "csv":
            args += [opt.flag, _csv(value)]
        elif opt.kind == "repeat":
            items = value.items() if isinstance(value, Mapping) else [(None, v) for v in value]
            for k, v in items:
                args += [opt.flag, f"{k}={v}" if k is not None else str(v)]
        else:
            args += [opt.flag, str(value)]
    return args


def _require_subcommand(caps: Capabilities, subcommand: str) -> None:
    if not has_subcommand(caps, subcommand):
        raise Unsupported(
            f"cli.subcommands:{subcommand}",
            caps.install,
            oldest_kicad_with_it=_OLDEST_WITH.get(subcommand),
            fallback=None,
        )


def output_kind(caps: Capabilities, subcommand: str) -> str:
    """ "dir" or "file": the record's `cli.output_kind.<subcommand>` if present, else the verified table."""
    override = caps.get(f"cli.output_kind.{subcommand}")
    if override in ("dir", "file"):
        return str(override)
    return _OUTPUT_KIND.get(subcommand, "file")


def _dir_arg(path: Path) -> str:
    """Directory argument with a trailing separator (7/8 need it to treat `-o` as a directory)."""
    s = str(path)
    return s if s.endswith(("/", "\\")) else s + os.sep


def _collect(res: CliResult, out_dir: Path, expected: Path | None) -> list[Path]:
    """Files this run produced under `out_dir` (created or rewritten), plus `expected` if present."""
    files: list[Path] = []
    for p in [*res.created_files, *res.updated_files]:
        try:
            p.relative_to(out_dir)
        except ValueError:
            continue
        if p not in files:
            files.append(p)
    if expected is not None and expected.is_file() and expected not in files:
        files.append(expected)
    return files


def _pos_ext(fmt: str) -> str:
    return {"csv": ".csv", "gerber": ".gbr"}.get(fmt, ".pos")


def export_pcb(
    install: KiCadInstallation,
    config: ConfigRoot,
    board: Path,
    target: str,
    out_dir: Path,
    *,
    caps: Capabilities | None = None,
    timeout_s: float = 600,
    out_name: str | None = None,
    **opts: Any,
) -> list[Path]:
    """Run `kicad-cli pcb export <target> ... <board>` into `out_dir` and return the files written.

    `target` is one of PCB_TARGETS. Single-file targets (pos, svg, pdf, dxf, step, stats, ipc2581,
    odb, glb) are named `<board stem><ext>` unless `out_name` is given; gerbers and drill fill the
    directory. Options are keyword arguments named after the flags (`layers=[...]`, `no_dnp=True`,
    `format="csv"`); an option a version does not support raises `Unsupported`.
    """
    if target not in PCB_TARGETS:
        raise ValueError(f"unknown pcb export target {target!r}; expected one of {PCB_TARGETS}")
    caps = resolve_caps(install, caps)
    sub = f"pcb export {target}"
    _require_subcommand(caps, sub)
    board, out_dir = Path(board), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)  # cli.gerbers_creates_outdir is false on 7/8

    opts = dict(opts)
    args: list[str] = ["pcb", "export", target]
    expected: Path | None = None
    if target in ("svg", "pdf", "dxf"):
        opts.setdefault("layers", list(DEFAULT_PLOT_LAYERS))
        if has_flag(caps, sub, "--mode-single") and caps.flags(sub):
            args.append("--mode-single")
    if target == "step":
        opts.setdefault("force", True)  # otherwise kicad-cli refuses to overwrite an existing file
    if target == "stats":
        opts.setdefault("format", "json")
    if output_kind(caps, sub) == "dir":
        args += ["-o", _dir_arg(out_dir)]
    else:
        if target == "pos":
            ext = _pos_ext(str(opts.get("format", "ascii")))
        elif target == "stats":
            ext = ".json" if opts.get("format") == "json" else ".txt"
        else:
            ext = _EXT[target]
        expected = out_dir / (out_name or (board.stem + ext))
        args += ["-o", str(expected)]
    args += _opts_to_args(caps, sub, _PCB_OPTS, opts)
    args.append(str(board))
    res = run_kicad_cli(
        install, args, config=config, caps=caps, timeout_s=timeout_s, watch_dirs=[out_dir], accept=(0,)
    )
    files = _collect(res, out_dir, expected)
    if not files:
        raise FileNotFoundError(f"kicad-cli {sub} exited 0 but wrote nothing under {out_dir}\n{res.stderr}")
    return files


def export_sch(
    install: KiCadInstallation,
    config: ConfigRoot,
    schematic: Path,
    target: str,
    out: Path,
    *,
    caps: Capabilities | None = None,
    timeout_s: float = 600,
    **opts: Any,
) -> list[Path]:
    """Run `kicad-cli sch export <target> ... <schematic>` and return the files written.

    `out` is the output FILE for pdf/bom/netlist and the output DIRECTORY for svg/dxf (kicad-cli
    names one file per sheet inside it). Passing a `.svg` path for a directory target uses its
    parent directory. Options are keyword arguments named after the flags (`pages=[1]`,
    `black_and_white=True`, `fields=[...]`).
    """
    if target not in SCH_TARGETS:
        raise ValueError(f"unknown sch export target {target!r}; expected one of {SCH_TARGETS}")
    caps = resolve_caps(install, caps)
    schematic, out = Path(schematic), Path(out)
    sub = f"sch export {target}"
    _require_subcommand(caps, sub)
    if target == "netlist":
        fmt = str(opts.pop("format", "kicadsexpr"))
        if opts:
            raise ValueError(f"{sub}: unsupported options {sorted(opts)}")
        return [export_netlist_file(install, config, schematic, out, fmt=fmt, caps=caps, timeout_s=timeout_s)]

    args: list[str] = ["sch", "export", target]
    if output_kind(caps, sub) == "dir":
        out_dir = out.parent if out.suffix.lower() == f".{target}" else out
        out_dir.mkdir(parents=True, exist_ok=True)
        args += ["-o", _dir_arg(out_dir)]
        expected = None
    else:
        out_dir = out.parent
        out_dir.mkdir(parents=True, exist_ok=True)
        expected = out
        args += ["-o", str(out)]
    args += _opts_to_args(caps, sub, _SCH_OPTS, opts)
    args.append(str(schematic))
    res = run_kicad_cli(
        install, args, config=config, caps=caps, timeout_s=timeout_s, watch_dirs=[out_dir], accept=(0,)
    )
    files = _collect(res, out_dir, expected)
    if not files:
        raise FileNotFoundError(f"kicad-cli {sub} exited 0 but wrote nothing under {out_dir}\n{res.stderr}")
    return files
