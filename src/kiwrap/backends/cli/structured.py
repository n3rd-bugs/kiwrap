"""Structured kicad-cli exports: the data-first feedback surface (docs/DECISIONS.md D17 tier 1).

Every function here runs one kicad-cli export through `run_kicad_cli` (sandboxed, locked,
`.kicad_prl` cleaned up) and returns a parsed model from `kiwrap.formats.reports.*` instead of
a file path. Capability keys decide the path taken (never a version number):

    export_stats     cli.has_stats_json    `pcb export stats --format json`      -> BoardStats
    export_stackup   cli.has_stackup_json  `pcb export stackup --format json`    -> Stackup
                     else `cli.subcommands` has "pcb export stackup" -> `--format csv`
                     else (fallback_to_board) the board file's `(setup (stackup ...))` via the CST
    sch_diff         cli.has_sch_diff      `sch diff --format json A B`          -> SchDiff
    export_ipc2581   "pcb export ipc2581"  (8.0+)                                -> Ipc2581Board
    export_ipcd356   "pcb export ipcd356"  (9.0+)                                -> TestPoints
    export_position  "pcb export pos --format csv"  (all versions)               -> PositionFile
    export_png       cli.has_pcb_png       `pcb export png -o <dir> --layers ..` -> list[Path]
    export_ratsnest  cli.has_ratsnest_export `pcb export ratsnest`                -> RatsnestReport
                     (kiwrap fork addition, docs/UPSTREAM.md G1; KiCad's own airwires with both
                     end items, anchor positions and layers; the pairs equal DRC's unconnected_items)

Verified live (scratchpad structured/probe1-3.py, 2026-09-19) on 10.99.0-4083 and 10.0.6, plus
9.0.9 / 8.0.9 / 7.0.6 for the subcommands they have:
- `pcb export stats --format json` (10.0.6, nightly): `-o` is a FILE; every dimension is a unit
  string ("52.0700 mm", "2413.705 mm²"), see formats.reports.stats;
- `pcb export stackup` (nightly only; "Failed to parse 'stackup', did you mean 'stats'" on
  10.0.6): `--format csv|json` (`report` is rejected with "Invalid format" although MR !2691
  documented it), `-o` FILE, `--units in|mm` applies to the CSV only;
- `sch diff` (nightly only): `--format json|text|png|svg`, JSON/text go to stdout unless `-o`;
  exit 0 = identical, 5 = differences, 3 = "Failed to load <file>" (`ERR_INVALID_INPUT_FILE`);
  `--exit-code-only` suppresses output. Symbol field edits, moves and removed wires are
  reported; edits inside `lib_symbols` (the embedded library copies) are NOT — they are not
  document items;
- `pcb export png` (nightly only): `-o` is a DIRECTORY (the help says "Output directory"), one
  `<stem>-<layer>.png` per `--layers` entry (like svg without `--mode-single`); `--dpi`,
  `--scale`, `--theme`, `--black-and-white`, `--mirror`, `--negative` etc. as in the flag list;
- `pcb export ipc2581` (8.0.9, 9.0.9, 10.0.6, nightly): `-o` FILE; 8.0 prefixes ids
  ("CMP:C1"); `--compress` writes a zip (not parsed here; pass `compress=False`);
- `pcb export ipcd356` (9.0.9, 10.0.6, nightly; absent on 8.0): `-o` FILE only, positions are
  aux-origin relative and y-up, so the board's `(aux_axis_origin)` is read here (regex, no full
  parse) and handed to the parser;
- `pcb export pos --format csv --units mm` (7.0.6 - nightly): y-up, absolute unless
  `--use-drill-file-origin` (then aux-origin relative); `--side both` on every version.

Positions in every returned model are KiCad board coordinates in millimetres (y down).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from kiwrap.backends.cli.export import DEFAULT_PLOT_LAYERS, export_pcb
from kiwrap.backends.cli.runner import (
    EXIT_OK,
    EXIT_VIOLATIONS,
    CliResult,
    has_flag,
    has_subcommand,
    resolve_caps,
    run_kicad_cli,
)
from kiwrap.capability import Capabilities, Unsupported
from kiwrap.formats.reports.ipc2581 import Ipc2581Board, parse_ipc2581
from kiwrap.formats.reports.ipcd356 import TestPoints, parse_ipcd356
from kiwrap.formats.reports.position import PositionFile, parse_pos
from kiwrap.formats.reports.ratsnest import RatsnestReport, parse_ratsnest_json
from kiwrap.formats.reports.schdiff import SchDiff, parse_sch_diff_json
from kiwrap.formats.reports.stackup import Stackup, parse_stackup_board, parse_stackup_csv, parse_stackup_json
from kiwrap.formats.reports.stats import BoardStats, parse_stats_json
from kiwrap.installation import KiCadInstallation
from kiwrap.sandbox import ConfigRoot

__all__ = [
    "OLDEST_WITH",
    "export_ipc2581",
    "export_ipcd356",
    "export_png",
    "export_position",
    "export_ratsnest",
    "export_stackup",
    "export_stats",
    "read_aux_origin",
    "sch_diff",
]

#: First KiCad release with each capability (for `Unsupported.oldest_kicad_with_it`); "11.0.0"
#: means "master / the 10.99 nightly today" (docs/DECISIONS.md D16).
OLDEST_WITH: dict[str, str] = {
    "cli.has_stats_json": "10.0.0",
    "cli.has_stackup_json": "11.0.0",
    "cli.has_sch_diff": "11.0.0",
    "cli.has_pcb_png": "11.0.0",
    "cli.has_ratsnest_export": "11.0.0",  # fork branch mr/g1-ratsnest; not in any release yet
    "cli.subcommands:pcb export ipc2581": "8.0.0",
    "cli.subcommands:pcb export ipcd356": "9.0.0",
    "cli.subcommands:pcb export stackup": "11.0.0",
}

#: How each boolean key is derived from the verified flag lists when a (cached, older) capability
#: record predates the key. Mirrors `kiwrap.capability.flatten_help_tree`.
_DERIVE: dict[str, tuple[str, str | None]] = {
    "cli.has_stats_json": ("pcb export stats", "--format"),
    "cli.has_stackup_json": ("pcb export stackup", "--format"),
    "cli.has_sch_diff": ("sch diff", None),
    "cli.has_pcb_png": ("pcb export png", None),
    "cli.has_ratsnest_export": ("pcb export ratsnest", None),
}

_AUX_ORIGIN_RE = re.compile(r"\(aux_axis_origin\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s*\)")


def capability(caps: Capabilities, key: str) -> bool:
    """`caps.has(key)`, or the derivation from `cli.subcommands` / `cli.flags.*` when the record
    has no such key (records cached before this component existed, hand-built test records)."""
    if key in caps:
        return bool(caps[key])
    sub, flag = _DERIVE.get(key, ("", None))
    if not sub or not caps.get("cli.subcommands"):
        return False
    if not has_subcommand(caps, sub):
        return False
    return True if flag is None else has_flag(caps, sub, flag)


def _require(caps: Capabilities, key: str, *, fallback: str | None) -> None:
    if not capability(caps, key):
        raise Unsupported(key, caps.install, oldest_kicad_with_it=OLDEST_WITH.get(key), fallback=fallback)


def _require_subcommand(caps: Capabilities, sub: str, *, fallback: str | None) -> None:
    if not has_subcommand(caps, sub):
        key = f"cli.subcommands:{sub}"
        raise Unsupported(key, caps.install, oldest_kicad_with_it=OLDEST_WITH.get(key), fallback=fallback)


def read_aux_origin(board: Path) -> tuple[float, float]:
    """`(aux_axis_origin x y)` of a board file in mm, (0, 0) when absent (KiCad omits it at 0,0)."""
    try:
        text = Path(board).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return (0.0, 0.0)
    m = _AUX_ORIGIN_RE.search(text)
    return (float(m.group(1)), float(m.group(2))) if m else (0.0, 0.0)


def _read(path: Path) -> str:
    return Path(path).read_text(encoding="utf-8", errors="replace")


def _out_dir(config: ConfigRoot, board: Path, out_dir: Path | None) -> Path:
    """`out_dir`, or a per-board folder under the sandbox root's short TEMP (never inside the
    user's project: the raw export is an intermediate, the model is the result)."""
    d = Path(out_dir) if out_dir is not None else config.temp_dir() / "structured" / Path(board).stem
    d.mkdir(parents=True, exist_ok=True)
    return d


# --- stats ----------------------------------------------------------------------------------------


def export_stats(
    install: KiCadInstallation,
    config: ConfigRoot,
    board: Path,
    *,
    out_dir: Path | None = None,
    caps: Capabilities | None = None,
    exclude_footprints_without_pads: bool = False,
    subtract_holes_from_board: bool = False,
    subtract_holes_from_copper: bool = False,
    timeout_s: float = 300,
) -> BoardStats:
    """`pcb export stats --format json` -> `BoardStats` (mm / mm² / percent). Requires
    `cli.has_stats_json` (10.0+), else `Unsupported(fallback="ipc2581")`: `export_ipc2581` gives
    component/pad/hole counts and the outline on 8.0+."""
    caps = resolve_caps(install, caps)
    _require(caps, "cli.has_stats_json", fallback="ipc2581")
    board = Path(board)
    files = export_pcb(
        install,
        config,
        board,
        "stats",
        _out_dir(config, board, out_dir),
        caps=caps,
        timeout_s=timeout_s,
        format="json",
        units="mm",
        exclude_footprints_without_pads=exclude_footprints_without_pads,
        subtract_holes_from_board=subtract_holes_from_board,
        subtract_holes_from_copper=subtract_holes_from_copper,
    )
    path = next((f for f in files if f.suffix == ".json"), files[0])
    return parse_stats_json(_read(path), source=str(board), kicad_version=install.version)


# --- ratsnest (fork addition, docs/UPSTREAM.md G1) ---------------------------------------------------


def export_ratsnest(
    install: KiCadInstallation,
    config: ConfigRoot,
    board: Path,
    *,
    nets: Sequence[str] | None = None,
    include_routed: bool = False,
    out_dir: Path | None = None,
    caps: Capabilities | None = None,
    timeout_s: float = 300,
) -> RatsnestReport:
    """`pcb export ratsnest --units mm [--nets N1,N2] [--include-routed]` -> `RatsnestReport`
    (mm): KiCad's unrouted connections per net with both end items (uuid, description, anchor
    position, copper layer), lengths and the `excluded` flag of edges a DRC unconnected_items
    exclusion hides (those are listed but not counted). The export is JSON only (no `--format`;
    the schema is resources/schemas/ratsnest.v1.json). Requires `cli.has_ratsnest_export` (the kiwrap fork
    branch `mr/g1-ratsnest`; no release has it), else `Unsupported(fallback="drc")`: `pcb drc
    --format json` `unconnected_items` lists the same item pairs with the item positions.
    `nets` is an exact-name filter; net names containing commas cannot be filtered this way
    (kicad-cli splits `--nets` on commas) and raise `ValueError`."""
    caps = resolve_caps(install, caps)
    _require(caps, "cli.has_ratsnest_export", fallback="drc")
    board = Path(board)
    out = _out_dir(config, board, out_dir) / f"{board.stem}_ratsnest.json"
    args: list[str] = ["pcb", "export", "ratsnest", "--units", "mm"]
    if nets:
        if any("," in n for n in nets):
            raise ValueError("net names containing ',' cannot be passed through --nets; export all nets")
        args += ["--nets", ",".join(nets)]
    if include_routed:
        args.append("--include-routed")
    args += ["-o", str(out), str(board)]
    res = run_kicad_cli(
        install,
        args,
        config=config,
        caps=caps,
        timeout_s=timeout_s,
        watch_dirs=[out.parent],
        accept=(EXIT_OK,),
    )
    if not out.is_file():
        raise FileNotFoundError(
            f"kicad-cli pcb export ratsnest exited 0 but did not write {out}\n{res.stderr}"
        )
    return parse_ratsnest_json(_read(out), source=str(board), kicad_version=install.version)


# --- stackup --------------------------------------------------------------------------------------


def export_stackup(
    install: KiCadInstallation,
    config: ConfigRoot,
    board: Path,
    *,
    out_dir: Path | None = None,
    caps: Capabilities | None = None,
    fallback_to_board: bool = True,
    timeout_s: float = 300,
) -> Stackup:
    """The board stackup as KiCad reports it.

    `cli.has_stackup_json` -> `pcb export stackup --format json` (`Stackup.source == "json"`);
    a version that lists the subcommand without json -> `--format csv` (`"csv"`); otherwise,
    with `fallback_to_board` (default), the board file's own `(stackup ...)` section through the
    CST (`"board"`, `has_stackup_section` says whether KiCad ever wrote one) - the same data,
    so callers report it as a degraded provider rather than an error. `fallback_to_board=False`
    raises `Unsupported(fallback="board")` instead.
    """
    caps = resolve_caps(install, caps)
    board = Path(board)
    sub = "pcb export stackup"
    if capability(caps, "cli.has_stackup_json") or has_subcommand(caps, sub) and caps.get("cli.subcommands"):
        fmt = "json" if capability(caps, "cli.has_stackup_json") else "csv"
        out = _out_dir(config, board, out_dir) / f"{board.stem}_stackup.{fmt}"
        res = run_kicad_cli(
            install,
            ["pcb", "export", "stackup", "--format", fmt, "-o", str(out), str(board)],
            config=config,
            caps=caps,
            timeout_s=timeout_s,
            watch_dirs=[out.parent],
            accept=(EXIT_OK,),
        )
        if not out.is_file():
            raise FileNotFoundError(f"kicad-cli {sub} exited 0 but did not write {out}\n{res.stderr}")
        text = _read(out)
        return parse_stackup_json(text) if fmt == "json" else parse_stackup_csv(text)
    if not fallback_to_board:
        _require(caps, "cli.has_stackup_json", fallback="board")
    return parse_stackup_board(_read(board))


# --- sch diff -------------------------------------------------------------------------------------


def sch_diff(
    install: KiCadInstallation,
    config: ConfigRoot,
    reference: Path,
    comparison: Path,
    *,
    out: Path | None = None,
    caps: Capabilities | None = None,
    timeout_s: float = 300,
) -> SchDiff:
    """`sch diff --format json <reference> <comparison>` -> `SchDiff` (11 / nightly).

    Exit 5 means "differences found" and is the normal outcome; both files must load (exit 3 ->
    `CliLoadError`). With `out` the JSON is also kept on disk (`-o`). Requires `cli.has_sch_diff`,
    else `Unsupported(fallback="netlist")`: compare `export_netlist` results of both files, or
    the CST (`kiwrap.formats.sexpr`) token-wise.
    """
    caps = resolve_caps(install, caps)
    _require(caps, "cli.has_sch_diff", fallback="netlist")
    reference, comparison = Path(reference), Path(comparison)
    args: list[str] = ["sch", "diff", "--format", "json"]
    if out is not None:
        out = Path(out)
        out.parent.mkdir(parents=True, exist_ok=True)
        args += ["-o", str(out)]
    args += [str(reference), str(comparison)]
    res: CliResult = run_kicad_cli(
        install,
        args,
        config=config,
        caps=caps,
        timeout_s=timeout_s,
        watch_dirs=[out.parent] if out is not None else (),
        accept=(EXIT_OK, EXIT_VIOLATIONS),
    )
    text = _read(out) if out is not None and out.is_file() else res.stdout
    if not text.strip():
        raise RuntimeError(f"kicad-cli sch diff exited {res.returncode} without JSON output\n{res.stderr}")
    return parse_sch_diff_json(
        text, reference=str(reference), comparison=str(comparison), exit_code=res.returncode
    )


# --- IPC-2581 -------------------------------------------------------------------------------------


def export_ipc2581(
    install: KiCadInstallation,
    config: ConfigRoot,
    board: Path,
    *,
    out_dir: Path | None = None,
    caps: Capabilities | None = None,
    precision: int | None = None,
    version: str | None = None,
    timeout_s: float = 600,
    **opts: Any,
) -> Ipc2581Board:
    """`pcb export ipc2581` -> parsed `Ipc2581Board` (components, pads+nets, vias, tracks, holes,
    outline; board frame, mm). The XML stays next to the model (`Ipc2581Board.source`). 8.0+;
    else `Unsupported(fallback="pos")`. Extra `opts` are `export_pcb` keyword options
    (`define_vars`, `variant`, `bom_col_*` are not mapped; use `export_pcb` directly for those)."""
    caps = resolve_caps(install, caps)
    _require_subcommand(caps, "pcb export ipc2581", fallback="pos")
    board = Path(board)
    if precision is not None:
        opts["precision"] = precision
    if version is not None:
        opts["version"] = version
    opts.setdefault("units", "mm")
    files = export_pcb(
        install,
        config,
        board,
        "ipc2581",
        _out_dir(config, board, out_dir),
        caps=caps,
        timeout_s=timeout_s,
        **opts,
    )
    path = next((f for f in files if f.suffix.lower() == ".xml"), files[0])
    return parse_ipc2581(str(path), source_name=str(path))


# --- IPC-D-356 ------------------------------------------------------------------------------------


def export_ipcd356(
    install: KiCadInstallation,
    config: ConfigRoot,
    board: Path,
    *,
    out_dir: Path | None = None,
    caps: Capabilities | None = None,
    timeout_s: float = 300,
) -> TestPoints:
    """`pcb export ipcd356` -> `TestPoints` (every pad and via with net, position, drill, access).
    9.0+; else `Unsupported(fallback="ipc2581")` (the IPC-2581 pads carry the same nets)."""
    caps = resolve_caps(install, caps)
    sub = "pcb export ipcd356"
    _require_subcommand(caps, sub, fallback="ipc2581")
    board = Path(board)
    out = _out_dir(config, board, out_dir) / f"{board.stem}.d356"
    res = run_kicad_cli(
        install,
        ["pcb", "export", "ipcd356", "-o", str(out), str(board)],
        config=config,
        caps=caps,
        timeout_s=timeout_s,
        watch_dirs=[out.parent],
        accept=(EXIT_OK,),
    )
    if not out.is_file():
        raise FileNotFoundError(f"kicad-cli {sub} exited 0 but did not write {out}\n{res.stderr}")
    return parse_ipcd356(_read(out), source=str(out), aux_origin_mm=read_aux_origin(board))


# --- position file --------------------------------------------------------------------------------


def export_position(
    install: KiCadInstallation,
    config: ConfigRoot,
    board: Path,
    *,
    out_dir: Path | None = None,
    caps: Capabilities | None = None,
    side: str = "both",
    smd_only: bool = False,
    exclude_fp_th: bool = False,
    exclude_dnp: bool | None = None,
    use_drill_file_origin: bool = False,
    bottom_negate_x: bool = False,
    timeout_s: float = 300,
) -> PositionFile:
    """`pcb export pos --format csv --units mm` -> `PositionFile` rows in the board frame.

    `exclude_dnp=None` passes the flag where the version lists it (8.0+) and omits it otherwise;
    `True` insists (`Unsupported` on 7.0). `use_drill_file_origin` / `bottom_negate_x` are
    forwarded AND undone in the parsed rows so `x_mm`/`y_mm` stay absolute board coordinates.
    """
    caps = resolve_caps(install, caps)
    board = Path(board)
    sub = "pcb export pos"
    opts: dict[str, Any] = {"format": "csv", "units": "mm", "side": side}
    if smd_only:
        opts["smd_only"] = True
    if exclude_fp_th:
        opts["exclude_fp_th"] = True
    if exclude_dnp or (exclude_dnp is None and caps.flags(sub) and has_flag(caps, sub, "--exclude-dnp")):
        opts["exclude_dnp"] = True
    if use_drill_file_origin:
        opts["use_drill_file_origin"] = True
    if bottom_negate_x:
        opts["bottom_negate_x"] = True
    files = export_pcb(
        install,
        config,
        board,
        "pos",
        _out_dir(config, board, out_dir),
        caps=caps,
        timeout_s=timeout_s,
        **opts,
    )
    path = next((f for f in files if f.suffix.lower() == ".csv"), files[0])
    origin = read_aux_origin(board) if use_drill_file_origin else (0.0, 0.0)
    return parse_pos(
        _read(path), source=str(path), units="mm", aux_origin_mm=origin, bottom_negate_x=bottom_negate_x
    )


# --- 2D PNG (kw_pcb_render secondary path) --------------------------------------------------------


def export_png(
    install: KiCadInstallation,
    config: ConfigRoot,
    board: Path,
    out_dir: Path,
    *,
    layers: Sequence[str] = DEFAULT_PLOT_LAYERS,
    common_layers: Sequence[str] = (),
    dpi: int | None = None,
    scale: float | None = None,
    theme: str | None = None,
    black_and_white: bool = False,
    mirror: bool = False,
    negative: bool = False,
    include_border_title: bool = False,
    caps: Capabilities | None = None,
    timeout_s: float = 300,
) -> list[Path]:
    """`pcb export png -o <out_dir> --layers ...` (11 / nightly): one `<stem>-<layer>.png` per
    layer, returned in `--layers` order (files that were not produced are omitted). Requires
    `cli.has_pcb_png`; else `Unsupported(fallback="render")` - `render.render_pcb` (3D, 9.0+) or
    `render.render_pcb_2d` (pdf + pypdfium2) are the existing paths kw_pcb_render already uses.
    """
    caps = resolve_caps(install, caps)
    _require(caps, "cli.has_pcb_png", fallback="render")
    board, out_dir = Path(board), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    sub = "pcb export png"
    args: list[str] = ["pcb", "export", "png", "-o", str(out_dir), "--layers", ",".join(layers)]
    if common_layers:
        args += ["--common-layers", ",".join(common_layers)]
    for flag, value in (("--dpi", dpi), ("--scale", scale), ("--theme", theme)):
        if value is not None:
            if not has_flag(caps, sub, flag):
                raise Unsupported(
                    f"cli.flags.{sub}:{flag}", install, oldest_kicad_with_it=None, fallback=f"omit {flag}"
                )
            args += [flag, str(value)]
    for flag, on in (
        ("--black-and-white", black_and_white),
        ("--mirror", mirror),
        ("--negative", negative),
        ("--include-border-title", include_border_title),
    ):
        if on:
            args.append(flag)
    args.append(str(board))
    res = run_kicad_cli(
        install, args, config=config, caps=caps, timeout_s=timeout_s, watch_dirs=[out_dir], accept=(EXIT_OK,)
    )
    produced = {p.resolve() for p in [*res.created_files, *res.updated_files] if p.suffix.lower() == ".png"}
    ordered: list[Path] = []
    for layer in layers:
        candidate = out_dir / f"{board.stem}-{layer.replace('.', '_')}.png"
        if candidate.resolve() in produced or candidate.is_file():
            ordered.append(candidate)
    for p in sorted(produced):
        if p not in [q.resolve() for q in ordered]:
            ordered.append(Path(p))
    if not ordered:
        raise FileNotFoundError(f"kicad-cli {sub} exited 0 but wrote no PNG under {out_dir}\n{res.stderr}")
    return ordered
