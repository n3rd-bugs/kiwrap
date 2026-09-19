"""Windows discovery sources [verified live on this machine, 2026-09-19].

Facts encoded (docs/research/07-versioning-strategy.md "Windows install discovery", verified again
by enumerating the hives while writing this module):
- No `HKLM\\SOFTWARE\\KiCad` key exists. Installs register under
  `HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\KiCad <major.minor>` with
  `DisplayName 'KiCad X.Y'`, `DisplayVersion` (e.g. '7.0.6', '10.0.0-1') and
  `InstallLocation 'C:\\Program Files\\KiCad\\X.Y'`; KiCad 7 registers under the
  `WOW6432Node` hive (32-bit installer), 8/9/10 under the 64-bit hive.
- The default install root is `C:\\Program Files\\KiCad\\<major.minor>` with
  `bin\\kicad-cli.exe`, `bin\\python.exe`, `bin\\Lib\\site-packages\\_pcbnew.pyd` and `share\\kicad`.
`DisplayVersion` is deliberately NOT trusted for the semver ('10.0.0-1' is an installer revision);
`kicad-cli version --format plain` is the source of truth.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from kiwrap.platform import Platform
from kiwrap.platform.discovery import Candidate, prefix_candidate
from kiwrap.platform.system import RegistryEntry, System

UNINSTALL_KEYS = (
    ("HKLM", r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ("HKLM", r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    ("HKCU", r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
)


def read_uninstall_entries() -> list[RegistryEntry]:
    """Enumerate every Uninstall sub-key whose DisplayName starts with 'KiCad'."""
    import winreg  # Windows-only stdlib module; the import is guarded by the caller

    hives = {"HKLM": winreg.HKEY_LOCAL_MACHINE, "HKCU": winreg.HKEY_CURRENT_USER}
    out: list[RegistryEntry] = []
    for hive_name, sub in UNINSTALL_KEYS:
        try:
            base = winreg.OpenKey(hives[hive_name], sub)
        except OSError:
            continue
        with base:
            index = 0
            while True:
                try:
                    child = winreg.EnumKey(base, index)
                except OSError:
                    break
                index += 1
                try:
                    with winreg.OpenKey(base, child) as key:
                        name = _reg_str(key, "DisplayName")
                        if not name.startswith("KiCad"):
                            continue
                        out.append(
                            RegistryEntry(
                                hive=hive_name,
                                key=f"{sub}\\{child}",
                                display_name=name,
                                display_version=_reg_str(key, "DisplayVersion"),
                                install_location=_reg_str(key, "InstallLocation"),
                            )
                        )
                except OSError:
                    continue
    return out


def _reg_str(key: Any, name: str) -> str:
    import winreg

    try:
        value, _kind = winreg.QueryValueEx(key, name)
    except OSError:
        return ""
    return str(value) if value is not None else ""


def registry_candidates(platform: Platform, system: System) -> list[Candidate]:
    """Uninstall keys (both hives) -> `<InstallLocation>/bin/kicad-cli.exe`."""
    out: list[Candidate] = []
    for entry in system.registry_uninstall_entries():
        if not entry.display_name.startswith("KiCad ") or not entry.install_location:
            continue
        root = Path(entry.install_location)
        kicad_cli = root / "bin" / platform.kicad_cli_name()
        if not system.is_file(kicad_cli):
            continue
        out.append(
            prefix_candidate(
                root,
                kicad_cli,
                "registry",
                platform,
                system,
                registry_key=f"{entry.hive}\\{entry.key}",
                display_version=entry.display_version,
            )
        )
    return out


def program_files_candidates(platform: Platform, system: System) -> list[Candidate]:
    """`%ProgramFiles%/KiCad/*/bin/kicad-cli.exe` (and the x86 variant, harmless when absent)."""
    env = system.environ()
    bases = [env.get("ProgramFiles", r"C:\Program Files"), env.get("ProgramFiles(x86)", "")]
    out: list[Candidate] = []
    for base in bases:
        if not base:
            continue
        for kicad_cli in system.glob(Path(base) / "KiCad", f"*/bin/{platform.kicad_cli_name()}"):
            if system.is_file(kicad_cli):
                out.append(
                    prefix_candidate(kicad_cli.parent.parent, kicad_cli, "program_files", platform, system)
                )
    return out


def windows_candidates(platform: Platform, system: System) -> list[Candidate]:
    return registry_candidates(platform, system) + program_files_candidates(platform, system)
