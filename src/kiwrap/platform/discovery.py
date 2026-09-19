"""Discovery candidates: where a KiCad *might* be, before `kicad-cli version` confirms it.

`kiwrap.installation.discover()` asks this module for candidates (dispatching to
`kiwrap.platform.windows` or `kiwrap.platform.linux` on `Platform.name`), then runs each
candidate's kicad-cli once to read the semver and turn it into a `KiCadInstallation`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from kiwrap.platform import Platform
from kiwrap.platform.system import System


@dataclass(frozen=True)
class Candidate:
    root: Path
    kicad_cli: Path
    share: Path
    source: str  # one of kiwrap.installation.SOURCES
    python: Path | None = None
    python_dll_dir: Path | None = None
    extra: dict[str, str] = field(default_factory=dict)


def explicit_candidate(spec: Path, platform: Platform, system: System) -> Candidate | None:
    """Turn a user-supplied path (install root, `bin` dir or kicad-cli executable) into a candidate.

    Accepts `C:/Program Files/KiCad/10.0`, `.../10.0/bin`, `.../10.0/bin/kicad-cli.exe`, `/usr`,
    `/usr/bin/kicad-cli`, `/opt/kicad9`. Returns None when no kicad-cli can be found there.

    A file is accepted only when it is named `kicad-cli(.exe)`: the candidate is EXECUTED
    (`version --format plain`) and, once accepted, receives the user's design files as arguments,
    so an arbitrary script (`notkicad.cmd`) handed in as a path must never become "the kicad-cli".
    """
    cli_name = platform.kicad_cli_name()
    spec = Path(spec)
    if system.is_file(spec):
        if spec.name.lower() != cli_name.lower():
            return None
        kicad_cli = spec
    elif system.is_file(spec / cli_name):
        kicad_cli = spec / cli_name
    elif system.is_file(spec / "bin" / cli_name):
        kicad_cli = spec / "bin" / cli_name
    else:
        return None
    bin_dir = kicad_cli.parent
    root = bin_dir.parent if bin_dir.name == "bin" else bin_dir
    return prefix_candidate(root, kicad_cli, "explicit", platform, system)


def prefix_candidate(
    root: Path, kicad_cli: Path, source: str, platform: Platform, system: System, **extra: str
) -> Candidate:
    """Candidate for the conventional `<root>/bin/kicad-cli`, `<root>/share/kicad` layout.

    Windows [verified]: the installer puts `python.exe` and the SWIG DLLs in `<root>/bin`, so the
    bundled interpreter is `<root>/bin/python.exe` and `<root>/bin` is the DLL directory.
    Linux [designed]: distro packages have no bundled interpreter; `pcbnew` lives in the system
    `python3` site-packages, so `<root>/bin/python3` (i.e. /usr/bin/python3) is the driver
    interpreter and there is no DLL dir (docs/ARCHITECTURE.md §6).
    """
    bin_dir = kicad_cli.parent
    share = root / "share" / "kicad"
    python: Path | None = None
    dll_dir: Path | None = None
    if platform.is_windows:
        cand = bin_dir / platform.python_name()
        if system.is_file(cand):
            python, dll_dir = cand, bin_dir
    else:
        for name in ("python3", "python"):
            cand = bin_dir / name
            if system.is_file(cand):
                python = cand
                break
    return Candidate(root, kicad_cli, share, source, python, dll_dir, dict(extra))


def path_candidates(platform: Platform, system: System) -> list[Candidate]:
    """`kicad-cli` on PATH (every platform). Resolves symlinks so a distro symlink or a
    Flatpak/Snap export still maps to its real prefix."""
    found = system.which(platform.kicad_cli_name())
    if found is None:
        return []
    try:
        real = found.resolve()
    except OSError:
        real = found
    bin_dir = real.parent
    root = bin_dir.parent if bin_dir.name == "bin" else bin_dir
    return [prefix_candidate(root, real, "path", platform, system)]


def find_candidates(platform: Platform, system: System, storage_home: Path) -> list[Candidate]:
    """All native candidates for this platform, in the contract's source order (PATH last)."""
    if platform.is_windows:
        from kiwrap.platform.windows import windows_candidates

        return windows_candidates(platform, system) + path_candidates(platform, system)
    from kiwrap.platform.linux import linux_candidates

    return linux_candidates(platform, system, storage_home) + path_candidates(platform, system)
