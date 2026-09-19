"""Injectable operating-system access used by installation discovery and capability probes.

Discovery must be unit-testable on a machine without KiCad (and Linux discovery must be testable
on Windows), so every filesystem, PATH, registry and subprocess touch goes through a `System`
object. `RealSystem` talks to the real machine; tests pass a fake with the same surface.

This module lives in `kiwrap.platform` because `RealSystem.run` is the one place that needs to
know about Windows-only subprocess flags (no console window for the spawned kicad-cli).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RunResult:
    """Outcome of one subprocess run (text mode, UTF-8 with replacement)."""

    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


@dataclass(frozen=True)
class RegistryEntry:
    """One Windows Uninstall key (docs/research/07-versioning-strategy.md: no HKLM\\SOFTWARE\\KiCad key
    exists; installs register under `Uninstall\\KiCad X.Y` with DisplayName/DisplayVersion/InstallLocation,
    7.x under WOW6432Node)."""

    hive: str  # "HKLM" | "HKCU"
    key: str  # full sub-key path
    display_name: str
    display_version: str
    install_location: str


class System:
    """Real machine access. Subclass or duck-type this in tests (see tests/test_installation.py)."""

    def environ(self) -> Mapping[str, str]:
        return os.environ

    def exists(self, path: Path) -> bool:
        return path.exists()

    def is_file(self, path: Path) -> bool:
        return path.is_file()

    def is_dir(self, path: Path) -> bool:
        return path.is_dir()

    def listdir(self, path: Path) -> list[Path]:
        try:
            return sorted(path.iterdir())
        except OSError:
            return []

    def glob(self, base: Path, pattern: str) -> list[Path]:
        try:
            return sorted(base.glob(pattern))
        except OSError:
            return []

    def which(self, name: str) -> Path | None:
        found = shutil.which(name)
        return Path(found) if found else None

    def path_entries(self) -> list[Path]:
        raw = self.environ().get("PATH", "")
        return [Path(p) for p in raw.split(os.pathsep) if p]

    def home(self) -> Path:
        return Path.home()

    def run(
        self, argv: Iterable[str], *, env: Mapping[str, str], timeout_s: float = 60, cwd: Path | None = None
    ) -> RunResult:
        """Run a process without a console window on Windows; never raises on non-zero exit."""
        args = [str(a) for a in argv]
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform.startswith("win") else 0
        try:
            proc = subprocess.run(
                args,
                env=dict(env),
                cwd=str(cwd) if cwd else None,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_s,
                check=False,
                creationflags=creationflags,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return RunResult(tuple(args), -1, "", f"{type(exc).__name__}: {exc}")
        return RunResult(tuple(args), proc.returncode, proc.stdout, proc.stderr)

    def read_text(self, path: Path) -> str:
        """Text of a small settings file (`installs.json`, `kiwrap-install.json`); raises OSError.
        `utf-8-sig`: PowerShell's `Out-File -Encoding utf8` writes a BOM, which `json.loads` rejects."""
        return path.read_text(encoding="utf-8-sig")

    def write_text(self, path: Path, text: str, *, executable: bool = False) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        if executable:
            path.chmod(path.stat().st_mode | 0o111)

    def mkdir(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)

    def registry_uninstall_entries(self) -> list[RegistryEntry]:
        """Windows only; returns [] elsewhere or when `winreg` is unavailable."""
        try:
            from kiwrap.platform.windows import read_uninstall_entries
        except ImportError:  # pragma: no cover - non-Windows
            return []
        return read_uninstall_entries()
