"""`run_kicad_cli`: the single choke point through which every kicad-cli process is started.

Guarantees (docs/ARCHITECTURE.md §6, docs/VERIFICATION.md claims 9 and 10):
- every run happens inside `config.lock()` with `config.env()` (KICAD_CONFIG_HOME under kiwrap's
  storage; the user's `%APPDATA%/kicad` is never seen by the process), and `config.restore_seed()`
  runs afterwards because kicad-cli rewrites `kicad_common.json` on exit (claim 10);
- any `.kicad_prl` that the run created next to an input file is deleted again (KiCad 10.0 writes
  one even on read-only operations — capability `cli.writes_prl_on_read`; the check is unconditional
  because it is a no-op on versions that do not);
- exit codes are mapped from KiCad's `include/cli/exit_codes.h` plus the observed deviations in
  claim 9: 0 ok; 5 = rule violations (`--exit-code-violations`), returned rather than raised;
  3 = ERR_INVALID_INPUT_FILE ("Failed to load board/schematic" on 9/10); the C `return -1` of the
  uncaught IO_ERROR path of kicad-cli 7/8 for boards, which the OS shows as 0xFFFFFFFF (Windows,
  verified live), 127 (Git Bash) or 255 (POSIX truncates -1 to 8 bits; Linux is designed-only) ->
  all of those raise `CliLoadError`; anything else raises `CliError`. Because 127/255 are also
  ordinary exit statuses, the -1 family only counts as a load failure when the command named an
  existing KiCad input file or stderr says so.

The `--help` parser is `flagmap.parse_help` (the same parser the capability layer probes with).
"""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from kiwrap.capability import Capabilities, capabilities_for
from kiwrap.installation import KiCadInstallation
from kiwrap.sandbox import ConfigRoot

#: `--exit-code-violations` makes `sch erc` / `pcb drc` return this when violations exist
#: (ERR_RC_VIOLATIONS in include/cli/exit_codes.h; verified on 8.0.1/9.0.5/10.0.0, claim 1 runs).
EXIT_OK = 0
EXIT_VIOLATIONS = 5
#: "failed to load" codes: 3 on every version; the `return -1` family for boards on 7/8 (claim 9).
LOAD_FAILURE_CODES: frozenset[int] = frozenset({3, -1, 127, 255, 0xFFFFFFFF})
#: C `return -1` as Windows (0xFFFFFFFF), Git Bash (127) and POSIX (255, -1 truncated to 8 bits) show it.
_UNCAUGHT_IO_ERROR_CODES: frozenset[int] = frozenset({-1, 127, 255, 0xFFFFFFFF})
_LOAD_FAILURE_MARKERS = ("unable to open", "failed to load", "io_error", "cannot open", "does not exist")
#: Files whose sibling `.kicad_prl` may be created by kicad-cli on read (claim: matrix row
#: "Writes .kicad_prl on read-only ops": 10.0 only).
_PRL_INPUT_SUFFIXES = (".kicad_pcb", ".kicad_sch", ".kicad_pro")


class CliError(RuntimeError):
    """kicad-cli exited with an unexpected status. Carries argv, exit code and both streams."""

    def __init__(self, message: str, *, argv: Sequence[str], returncode: int, stdout: str, stderr: str):
        super().__init__(message)
        self.argv, self.returncode, self.stdout, self.stderr = tuple(argv), returncode, stdout, stderr


class CliLoadError(CliError):
    """kicad-cli could not load the input file (exit 3, or the `return -1` family on 7/8 for boards)."""


class CliTimeout(CliError):
    """kicad-cli did not finish within `timeout_s` and was killed."""


@dataclass(frozen=True)
class CliResult:
    """Outcome of one kicad-cli process.

    `violations` is True iff the process exited with `EXIT_VIOLATIONS` (5), which only happens for
    `sch erc` / `pcb drc` with `--exit-code-violations` and means "ran fine, found violations".
    `created_files` lists files that appeared under `watch_dirs` (or next to the output path)
    during the run; `updated_files` lists pre-existing ones whose size or mtime changed.
    """

    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    duration_s: float
    created_files: list[Path] = field(default_factory=list)
    updated_files: list[Path] = field(default_factory=list)
    removed_prl: list[Path] = field(default_factory=list)

    @property
    def violations(self) -> bool:
        return self.returncode == EXIT_VIOLATIONS

    @property
    def ok(self) -> bool:
        return self.returncode == EXIT_OK


def _caps(install: KiCadInstallation, caps: Capabilities | None) -> Capabilities:
    return caps if caps is not None else capabilities_for(install)


def _snapshot(dirs: Iterable[Path]) -> dict[Path, tuple[int, int]]:
    out: dict[Path, tuple[int, int]] = {}
    for d in dirs:
        if not d.is_dir():
            continue
        for p in d.rglob("*"):
            if p.is_file():
                try:
                    st = p.stat()
                except OSError:
                    continue
                out[p] = (st.st_size, st.st_mtime_ns)
    return out


def _input_files(argv: Sequence[str], cwd: Path | None = None) -> list[Path]:
    """Existing board/schematic/project files named by positional arguments. Relative paths are
    resolved against `cwd` (the directory the process runs in), never the Python process cwd."""
    base = Path(cwd) if cwd else Path.cwd()
    out: list[Path] = []
    for a in argv:
        if a.startswith("-"):
            continue
        p = Path(a)
        if not p.is_absolute():
            p = base / p
        if p.suffix.lower() in _PRL_INPUT_SUFFIXES and p.is_file() and p not in out:
            out.append(p)
    return out


def _prl_candidates(argv: Sequence[str], cwd: Path | None = None) -> list[Path]:
    """`.kicad_prl` paths KiCad would write next to each board/schematic/project argument."""
    out: list[Path] = []
    for p in _input_files(argv, cwd):
        prl = p.with_suffix(".kicad_prl")
        if prl not in out:
            out.append(prl)
    return out


def is_load_failure(returncode: int, argv: Sequence[str], stderr: str, cwd: Path | None = None) -> bool:
    """Whether an exit status means "could not load the input" (claim 9).

    3 (ERR_INVALID_INPUT_FILE) always does. The `return -1` family (-1 / 127 / 255 / 0xFFFFFFFF)
    is ambiguous - 127 is also "command not found", 255 any failure on POSIX - so it counts only
    when the command named an existing KiCad input file or stderr carries a load-failure marker.
    """
    if returncode == 3:
        return True
    if returncode not in _UNCAUGHT_IO_ERROR_CODES:
        return False
    if _input_files(argv[1:], cwd):
        return True
    low = (stderr or "").lower()
    return any(marker in low for marker in _LOAD_FAILURE_MARKERS)


def run_kicad_cli(
    install: KiCadInstallation,
    args: Sequence[str | os.PathLike[str]],
    *,
    config: ConfigRoot,
    cwd: Path | None = None,
    timeout_s: float = 300,
    env_extra: Mapping[str, str] | None = None,
    caps: Capabilities | None = None,
    watch_dirs: Iterable[Path] = (),
    accept: Iterable[int] = (EXIT_OK, EXIT_VIOLATIONS),
) -> CliResult:
    """Run `<install>/bin/kicad-cli <args>` sandboxed and return a `CliResult`.

    Raises `CliLoadError` for the load-failure codes, `CliTimeout` on timeout and `CliError` for
    any other exit status not in `accept`. Exit 5 is accepted by default (see `CliResult.violations`).
    `caps` is optional; nothing here needs it, but callers pass it through so the whole call chain
    can run with a hand-built record while `kiwrap.capability` is being finished.
    """
    argv: list[str] = [str(install.kicad_cli), *(str(a) for a in args)]
    watch = [Path(d) for d in watch_dirs]
    with config.lock(timeout_s=timeout_s):
        env = config.env()
        if env_extra:
            env.update(env_extra)
        prl_before = {p: p.exists() for p in _prl_candidates(argv[1:], cwd)}
        files_before = _snapshot(watch)
        t0 = time.perf_counter()
        try:
            proc = subprocess.run(
                argv,
                cwd=str(cwd) if cwd else None,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            duration = time.perf_counter() - t0
            _cleanup_prl(prl_before)
            config.restore_seed()
            out = (
                exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            )
            err = (
                exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            )
            raise CliTimeout(
                f"kicad-cli timed out after {duration:.0f}s: {' '.join(argv[1:4])}",
                argv=argv,
                returncode=-9,
                stdout=out,
                stderr=err,
            ) from exc
        duration = time.perf_counter() - t0
        removed_prl = _cleanup_prl(prl_before)
        config.restore_seed()
        files_after = _snapshot(watch)

    created = sorted(p for p in files_after if p not in files_before)
    updated = sorted(p for p in files_after if p in files_before and files_after[p] != files_before[p])
    rc = proc.returncode  # Windows reports C `return -1` as 0xFFFFFFFF; LOAD_FAILURE_CODES has every form
    result = CliResult(
        argv=tuple(argv),
        returncode=rc,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
        duration_s=duration,
        created_files=created,
        updated_files=updated,
        removed_prl=removed_prl,
    )
    if rc in set(accept):
        return result
    summary = _first_error_line(result.stderr) or _first_error_line(result.stdout) or f"exit status {rc}"
    if is_load_failure(rc, argv, result.stderr, cwd):
        raise CliLoadError(
            f"kicad-cli {' '.join(argv[1:3])} could not load its input ({summary})",
            argv=argv,
            returncode=rc,
            stdout=result.stdout,
            stderr=result.stderr,
        )
    raise CliError(
        f"kicad-cli {' '.join(argv[1:3])} failed with exit status {rc}: {summary}",
        argv=argv,
        returncode=rc,
        stdout=result.stdout,
        stderr=result.stderr,
    )


def _cleanup_prl(prl_before: Mapping[Path, bool]) -> list[Path]:
    removed: list[Path] = []
    for prl, existed in prl_before.items():
        if not existed and prl.exists():
            try:
                prl.unlink()
                removed.append(prl)
            except OSError:
                pass
    return removed


def _first_error_line(text: str) -> str:
    for line in text.splitlines():
        s = line.strip()
        if s:
            return s[:300]
    return ""


# --------------------------------------------------------------------------------------------
# `--help` support: kicad-cli 7/8 exit 1 from a leaf `--help` (capability `cli.help_exit_code`),
# 9/10 exit 0 (spikes/versioning-strategy/kicad_version_matrix.json: leaf_help_exit_code).
# --------------------------------------------------------------------------------------------


def help_text(
    install: KiCadInstallation,
    subcommand: Sequence[str],
    *,
    config: ConfigRoot,
    caps: Capabilities | None = None,
    timeout_s: float = 60,
) -> str:
    """Return the `--help` text of `kicad-cli <subcommand...>` regardless of its exit status."""
    res = run_kicad_cli(
        install,
        [*subcommand, "--help"],
        config=config,
        caps=caps,
        timeout_s=timeout_s,
        accept=range(-1, 256),
    )
    # 7.0 prints usage to stderr together with "input: 1 argument(s) expected" (see
    # spikes/versioning-strategy/clihelp/7.0__*.txt); combine both streams.
    return (res.stdout or "") + ("\n" + res.stderr if res.stderr else "")


def require(caps: Capabilities, key: str, *, oldest: str | None, fallback: str | None) -> None:
    """Raise `Unsupported` unless `caps.has(key)`. Kept here so every adapter phrases it the same."""
    from kiwrap.capability import Unsupported

    if not caps.has(key):
        raise Unsupported(key, caps.install, oldest_kicad_with_it=oldest, fallback=fallback)


def has_subcommand(caps: Capabilities, subcommand: str) -> bool:
    """True when `subcommand` ("pcb export svg") is listed in `cli.subcommands`; True when the
    record has no list at all (a hand-built record that only sets booleans)."""
    listed = caps.get("cli.subcommands")
    if not listed:
        return True
    return subcommand in listed


def has_flag(caps: Capabilities, subcommand: str, flag: str) -> bool:
    """True when `flag` is in `cli.flags.<subcommand>`, or when that list is absent/empty."""
    listed = caps.flags(subcommand)
    return not listed or flag in listed


def resolve_caps(install: KiCadInstallation, caps: Capabilities | None) -> Capabilities:
    """`caps` if given else `capabilities_for(install)` (public alias of the private helper)."""
    return _caps(install, caps)
