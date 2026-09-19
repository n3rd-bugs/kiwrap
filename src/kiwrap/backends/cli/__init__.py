"""kicad-cli backend: the ONLY module family that knows kicad-cli's argument surface and quirks.

Every function takes a `KiCadInstallation` + `ConfigRoot` (from `kiwrap.sandbox.ensure`) and
consults `kiwrap.capability.capabilities_for(install)` before choosing flags. Missing capability
-> raise `kiwrap.capability.Unsupported` (tools convert it to E_UNSUPPORTED). Never branch on a
version number here either: branch on capability keys.

Modules and their public functions (signatures are the contract; see each module's docstring):
    runner.py    run_kicad_cli(install, args, *, config, cwd=None, timeout_s=300, env_extra=None) -> CliResult
                 CliResult: argv, returncode, stdout, stderr, duration_s, created_files (list[Path])
                 - always runs inside `config.lock()`, with `config.env()`, and calls
                   `config.restore_seed()` afterwards;
                 - deletes any `.kicad_prl` the run created next to the input (cli.writes_prl_on_read);
                 - maps exit codes: 0 ok, 5 violations (ERC/DRC with --exit-code-violations),
                   3 / -1 / 127 / 0xFFFFFFFF "failed to load" -> CliLoadError, others -> CliError.
    flagmap.py   parse_help(text) -> {"subcommands": [...], "flags": [...]};
                 help_tree(install, config) -> nested dict (cached per install key) used by capability probes
    erc.py       run_erc(install, config, schematic: Path, *, severity="all", units="mm") -> Report
                 - `--format json`, positions AND description strings rescaled by cli.erc_json_pos_scale
    drc.py       run_drc(install, config, board: Path, *, schematic_parity=False, refill_zones=False,
                         save_board=False, severity="all") -> Report
    netlist.py   export_netlist(install, config, schematic: Path, out: Path | None = None) -> Netlist
                 (format kicadsexpr; parsed with kiwrap.formats.reports.netlist)
    export.py    export_pcb(install, config, board, target: str, out_dir: Path, **opts) -> list[Path]
                 target in {"gerbers","drill","pos","pdf","svg","step","dxf","stats","ipc2581","odb","glb"}
                 export_sch(install, config, schematic, target: str, out: Path, **opts) -> list[Path]
                 target in {"pdf","svg","bom","netlist","dxf"}
                 - creates out_dir itself (cli.gerbers_creates_outdir is false on 7/8)
    render.py    render_pcb(install, config, board, out_png, *, width, height, side="top", ...) -> Path
                 (cli.has_render; else Unsupported with fallback "svg")
                 rasterise_svg(svg: Path, out_png: Path, *, max_px=1024) -> Path   (pypdfium2/resvg, never PyMuPDF)
    upgrade.py   upgrade_copy(install, config, src: Path, dst_dir: Path) -> Path   (sch/pcb/sym/fp upgrade on a COPY)
"""
