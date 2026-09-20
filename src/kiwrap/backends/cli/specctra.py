"""Specctra DSN export / SES import through the patched kicad-cli (kiwrap fork patch `specctra-jobs`).

KiCad 11 has no SWIG, so `ExportSpecctraDSN` / `ImportSpecctraSES` of the bundled Python
(docs/VERIFICATION.md claim 7, 8.0-10.0 only) are gone there. The fork branch `mr/specctra-jobs`
(docs/upstream/drafts/specctra-jobs-*.md, manifest `src/kiwrap/data/kiwrap_patches.json`) adds
KiCad's own exporter and importer as headless jobs behind two commands:

    kicad-cli pcb export specctra [-o FILE] BOARD          cli.has_specctra_export
    kicad-cli pcb import-ses --ses FILE [-o OUT] BOARD     cli.has_specctra_import

Both keys are derived from the `--help` tree by presence (`kiwrap.capability.flatten_help_tree`),
never from the version string; stock installs raise `Unsupported(fallback="codec")` so the
routing ops fall back to kiwrap's own DSN writer / SES reader (`kiwrap.formats.specctra`).

Verified 2026-09-20 on the bench build `C:/kiwrap-builds/6dd893f7d1-5` (10.99.0 + ratsnest-export
+ specctra-jobs; tests/test_cli_specctra.py, `kicad_patch("specctra-jobs")`):

- the export is `DSN::ExportBoardToSpecctraFile`, i.e. byte-for-byte what the editor's File >
  Export > Specctra DSN writes: `(resolution um 10)` declared but plain micrometres printed with
  six significant digits (a coordinate past 100 mm keeps no sub-um part), unlocked tracks as
  `(type protect)`, locked ones as `(type fix)` (Freerouting returns protect wires in the SES and
  drops fix ones), zones as `(wire (polygon ...))`; a board without a valid outline exports its
  bounding box with `Warning: Board outline is malformed` on stderr (no dialog: kicad-cli is a
  console app, unlike the SWIG helper's wx message box of claim 7 caveat b);
- the import is `DSN::ImportSpecctraSession` in a board-editor `BOARD_COMMIT` on the headless tool
  manager, so the outcome equals the editor's: every unlocked track/arc/via is removed, locked ones
  stay, DRC markers are dropped, the session's `(placement)` moves every footprint it names
  (locked footprints too, as the editor does), routes/vias are added with the session's widths and
  padstacks (the via drill comes from the `Via[..]_<size>:<drill>_um` padstack name), unresolved
  references/layers/padstacks are skipped with a counted warning on stderr, and a session missing
  its `session`/`routes`/`library_out` sections exits 3 with the board untouched (the commit is
  reverted; claim 7's "partial mutation on False" was the 8.0/9.0 SWIG path);
- without `-o` the board is saved in place; with `-o` only the board file is written, so when the
  output lands in another directory this module copies the project file (`.kicad_pro`, plus
  `.kicad_dru` when present) next to it unless one is already there - `kicad-cli pcb drc` on the
  routed copy would otherwise judge it by default rules (seen: 25 phantom clearance errors on
  issue8883 without its project);
- DSN export of the ecc83 demo is accepted by Freerouting 2.4.1 under the managed JRE 25 and the
  resulting SES imports (59 -> 83 track items, 0 unconnected after `pcb drc --refill-zones`).
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from kiwrap.backends.cli.runner import EXIT_OK, CliResult, has_subcommand, resolve_caps, run_kicad_cli
from kiwrap.capability import Capabilities, Unsupported
from kiwrap.installation import KiCadInstallation
from kiwrap.sandbox import ConfigRoot

__all__ = [
    "EXPORT_KEY",
    "EXPORT_SUBCOMMAND",
    "IMPORT_KEY",
    "IMPORT_SUBCOMMAND",
    "PROJECT_SIDECARS",
    "SpecctraRun",
    "export_dsn",
    "export_dsn_run",
    "has_specctra_export",
    "has_specctra_import",
    "import_ses",
    "import_ses_run",
]

EXPORT_KEY = "cli.has_specctra_export"
IMPORT_KEY = "cli.has_specctra_import"
EXPORT_SUBCOMMAND = "pcb export specctra"
IMPORT_SUBCOMMAND = "pcb import-ses"
#: The provider the routing ops fall back to on stock installs (`kiwrap.formats.specctra`).
FALLBACK = "codec"
#: Sidecars copied next to an `import_ses` output that lands in another directory (see docstring).
PROJECT_SIDECARS = (".kicad_pro", ".kicad_dru")

#: First KiCad release with the commands: none - the fork branch only (docs/DECISIONS.md D16/D17).
_OLDEST: str | None = None

_WARNING_RE = re.compile(r"^(?:\d\d:\d\d:\d\d: )?Warning: (?P<text>.+)$", re.MULTILINE)
_SKIPPED_RE = re.compile(r"(?P<n>\d+) session item\(s\) were skipped")
_REPLACED_RE = re.compile(r"Replaced (?P<before>\d+) track items with (?P<after>\d+) from the session")


@dataclass(frozen=True)
class SpecctraRun:
    """One export / import run: the written file plus what KiCad said on the way.

    `warnings` are the `Warning:` lines of stderr without wx's time stamp (export: "Board outline
    is malformed. Run DRC for a full analysis."; import: "<n> session item(s) were skipped due to
    unresolved reference, layer, or padstack."), `skipped` that count (import), `tracks_before` /
    `tracks_after` the track+arc+via counts the import reports (-1 for an export).
    """

    path: Path
    duration_s: float
    stdout: str = ""
    stderr: str = ""
    warnings: list[str] = field(default_factory=list)
    skipped: int = 0
    tracks_before: int = -1
    tracks_after: int = -1


def _run_info(path: Path, res: CliResult) -> SpecctraRun:
    warnings = [m.group("text").strip() for m in _WARNING_RE.finditer(res.stderr)]
    skipped = sum(int(m.group("n")) for m in _SKIPPED_RE.finditer(res.stderr))
    replaced = _REPLACED_RE.search(res.stdout) or _REPLACED_RE.search(res.stderr)
    return SpecctraRun(
        path=path,
        duration_s=res.duration_s,
        stdout=res.stdout,
        stderr=res.stderr,
        warnings=warnings,
        skipped=skipped,
        tracks_before=int(replaced.group("before")) if replaced else -1,
        tracks_after=int(replaced.group("after")) if replaced else -1,
    )


def _capability(caps: Capabilities, key: str, subcommand: str) -> bool:
    """`caps.has(key)`, or the presence of the subcommand in `cli.subcommands` when the record
    predates the key (cached records, hand-built test records)."""
    if key in caps:
        return bool(caps[key])
    return bool(caps.get("cli.subcommands")) and has_subcommand(caps, subcommand)


def has_specctra_export(caps: Capabilities) -> bool:
    return _capability(caps, EXPORT_KEY, EXPORT_SUBCOMMAND)


def has_specctra_import(caps: Capabilities) -> bool:
    return _capability(caps, IMPORT_KEY, IMPORT_SUBCOMMAND)


def _require(caps: Capabilities, key: str, subcommand: str) -> None:
    if not _capability(caps, key, subcommand):
        raise Unsupported(key, caps.install, oldest_kicad_with_it=_OLDEST, fallback=FALLBACK)


def export_dsn(
    install: KiCadInstallation,
    config: ConfigRoot,
    board: Path,
    out: Path,
    caps: Capabilities | None = None,
    *,
    timeout_s: float = 300,
) -> Path:
    """`kicad-cli pcb export specctra -o <out> <board>` -> `out` (the contract of the routing ops;
    `export_dsn_run` returns the warnings as well).

    Requires `cli.has_specctra_export`, else `Unsupported(fallback="codec")`. `out`'s directory is
    created. A malformed outline is not an error (KiCad exports the bounding box and warns on
    stderr; see the module docstring). Raises `CliLoadError` when the board cannot be loaded and
    `CliError` for any other failure.
    """
    return export_dsn_run(install, config, board, out, caps, timeout_s=timeout_s).path


def export_dsn_run(
    install: KiCadInstallation,
    config: ConfigRoot,
    board: Path,
    out: Path,
    caps: Capabilities | None = None,
    *,
    timeout_s: float = 300,
) -> SpecctraRun:
    """`export_dsn` with KiCad's warnings (`SpecctraRun.warnings`)."""
    caps = resolve_caps(install, caps)
    _require(caps, EXPORT_KEY, EXPORT_SUBCOMMAND)
    board, out = Path(board), Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    res = run_kicad_cli(
        install,
        ["pcb", "export", "specctra", "-o", str(out), str(board)],
        config=config,
        caps=caps,
        timeout_s=timeout_s,
        watch_dirs=[out.parent],
        accept=(EXIT_OK,),
    )
    if not out.is_file():
        raise FileNotFoundError(
            f"kicad-cli pcb export specctra exited 0 but did not write {out}\n{res.stderr}"
        )
    return _run_info(out, res)


def import_ses(
    install: KiCadInstallation,
    config: ConfigRoot,
    board: Path,
    ses: Path,
    out: Path | None,
    caps: Capabilities | None = None,
    *,
    copy_project: bool = True,
    timeout_s: float = 300,
) -> Path:
    """`kicad-cli pcb import-ses --ses <ses> [-o <out>] <board>` -> the written board (the
    contract of the routing ops; `import_ses_run` returns the counts and warnings as well).

    `out=None` (or `out == board`) saves the board in place, exactly like the editor's Import
    Specctra Session followed by Save. Otherwise `out` is written and, with `copy_project`, the
    board's project sidecars (`PROJECT_SIDECARS`) are copied next to it when it lands in another
    directory that has none, so DRC on the result keeps the board's rules. Requires
    `cli.has_specctra_import`, else `Unsupported(fallback="codec")`.

    Raises `CliLoadError` when the board or the session cannot be loaded or the session lacks its
    required sections (exit 3; the board file is untouched in that case) and `CliError` otherwise.
    """
    return import_ses_run(
        install, config, board, ses, out, caps, copy_project=copy_project, timeout_s=timeout_s
    ).path


def import_ses_run(
    install: KiCadInstallation,
    config: ConfigRoot,
    board: Path,
    ses: Path,
    out: Path | None,
    caps: Capabilities | None = None,
    *,
    copy_project: bool = True,
    timeout_s: float = 300,
) -> SpecctraRun:
    """`import_ses` with the track counts KiCad reports and its warnings (`skipped` items)."""
    caps = resolve_caps(install, caps)
    _require(caps, IMPORT_KEY, IMPORT_SUBCOMMAND)
    board, ses = Path(board), Path(ses)
    target = board if out is None else Path(out)
    in_place = target.resolve() == board.resolve()
    args: list[str] = ["pcb", "import-ses", "--ses", str(ses)]
    if not in_place:
        target.parent.mkdir(parents=True, exist_ok=True)
        if copy_project and target.parent.resolve() != board.parent.resolve():
            _copy_sidecars(board, target)
        args += ["-o", str(target)]
    args.append(str(board))
    res = run_kicad_cli(
        install,
        args,
        config=config,
        caps=caps,
        timeout_s=timeout_s,
        watch_dirs=[target.parent],
        accept=(EXIT_OK,),
    )
    if not target.is_file():
        raise FileNotFoundError(f"kicad-cli pcb import-ses exited 0 but did not write {target}\n{res.stderr}")
    return _run_info(target, res)


def _copy_sidecars(board: Path, target: Path) -> list[Path]:
    """Copy the board's project sidecars next to `target` (named after `target`) when none exist."""
    copied: list[Path] = []
    for suffix in PROJECT_SIDECARS:
        src = board.with_suffix(suffix)
        dst = target.with_suffix(suffix)
        if src.is_file() and not dst.exists():
            shutil.copyfile(src, dst)
            copied.append(dst)
    return copied
