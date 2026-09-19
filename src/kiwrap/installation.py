"""Layer 1 — `KiCadInstallation`: one installed KiCad and how to reach its binaries.

Discovery never runs anything except `kicad-cli version --format plain` (to read the semver).
It must work with zero installs (returns []) and must never write anywhere.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# Set by `discover()`; see kiwrap/data/kicad_version_matrix.json for what is known per version.
SOURCES = ("explicit", "registry", "program_files", "path", "apt", "flatpak", "snap", "appimage")


@dataclass(frozen=True)
class KiCadInstallation:
    root: Path  # e.g. C:/Program Files/KiCad/10.0 or /usr (distro) or the flatpak sandbox root
    kicad_cli: Path  # absolute path to kicad-cli(.exe); for flatpak a wrapper script kiwrap generates
    share: Path  # <root>/share/kicad on Windows; /usr/share/kicad on distro Linux
    version: str  # full semver as printed by `kicad-cli version --format plain`, e.g. "10.0.0"
    source: str  # one of SOURCES
    python: Path | None = None  # interpreter that can `import pcbnew` for THIS install; None if unknown
    python_dll_dir: Path | None = None  # Windows: <root>/bin (must be on PATH / add_dll_directory)
    extra: dict[str, str] = field(default_factory=dict)  # e.g. {"flatpak_id": "org.kicad.KiCad"}

    @property
    def major_minor(self) -> str:
        parts = self.version.split(".")
        return f"{parts[0]}.{parts[1]}" if len(parts) >= 2 else self.version

    @property
    def major(self) -> int:
        return int(self.version.split(".")[0])

    def dir(self, kind: str) -> Path:
        """`symbols` | `footprints` | `3dmodels` | `template` | `schemas` | `demos` under `share`."""
        return self.share / kind

    def key(self) -> str:
        """Stable cache key: full semver + root."""
        return f"{self.version}@{self.root}"


def discover(explicit: list[Path] | tuple[Path, ...] = ()) -> list[KiCadInstallation]:
    """Find every KiCad install on this machine, newest first, de-duplicated by root.

    Sources, in order: `explicit` roots or kicad-cli paths (settings), the platform's native
    package registry (Windows Uninstall keys incl. WOW6432Node for 7.x; Linux distro packages),
    well-known directories (C:/Program Files/KiCad/*; /usr, /usr/local, /opt/kicad*), Flatpak
    (`org.kicad.KiCad`), Snap, AppImage on PATH, and finally `kicad-cli` on PATH.
    """
    raise NotImplementedError  # implemented by the installation/capability component


def find(spec: str | None = None, installs: list[KiCadInstallation] | None = None) -> KiCadInstallation:
    """Pick one install. `spec` is None (newest), "10", "10.0", "10.0.6", or a root/kicad-cli path.

    Raises `NoKiCadError` with the list of installs that *were* found.
    """
    raise NotImplementedError


class NoKiCadError(RuntimeError):
    def __init__(self, spec: str | None, found: list[KiCadInstallation]):
        self.spec, self.found = spec, found
        super().__init__(f"no KiCad matching {spec!r}; found: {[i.version for i in found]}")
