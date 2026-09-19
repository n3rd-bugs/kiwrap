"""Platform seam: everything that differs between Windows and Linux lives behind this module.

Rules
- No other module may call `sys.platform`, `os.name`, `platform.system()` or hard-code a Windows or
  Linux path. They ask `current()` instead.
- macOS is not supported yet; `current()` raises `UnsupportedPlatformError` there.
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

#: Longest `Platform.temp_dir` that keeps KiCad's IPC pipe path under nng's ~128-char limit
#: (docs/VERIFICATION.md claim 10: 126 chars OK, 146 fails). The per-root suffix
#: `/k<digits>-<6 hex>` (~13) plus `/kicad/api.sock` (~15) leaves ~100 for the directory; 80 keeps
#: margin for longer version strings.
MAX_TEMP_DIR_CHARS = 80


class UnsupportedPlatformError(RuntimeError):
    pass


@dataclass(frozen=True)
class Platform:
    name: str  # "windows" | "linux"
    exe_suffix: str  # ".exe" | ""
    storage_home: Path  # kiwrap's own state dir (override with KIWRAP_HOME)
    user_kicad_config_home: Path  # the user's real KiCad config root; READ ONLY, never written
    temp_dir: Path  # a short temp dir usable for KiCad's IPC socket path (< ~100 chars)
    # The user's real `<Documents>/KiCad` (KiCad's default KICAD_DOCUMENTS_HOME, where the PCM
    # installs libraries under `<major.minor>/3rdparty`); READ ONLY, never created or written.
    # `None` in fakes that have no such directory.
    user_kicad_documents_home: Path | None = None

    @property
    def is_windows(self) -> bool:
        return self.name == "windows"

    def python_name(self) -> str:
        return "python" + self.exe_suffix

    def kicad_cli_name(self) -> str:
        return "kicad-cli" + self.exe_suffix


def _windows_documents_dir() -> Path:
    """The Documents known folder as `wxStandardPaths::GetDocumentsDir()` resolves it for KiCad:
    `HKCU\\...\\Explorer\\User Shell Folders\\Personal` (env-expanded; OneDrive / folder redirection
    move it away from `%USERPROFILE%\\Documents`), falling back to `%USERPROFILE%\\Documents`."""
    try:
        import winreg  # Windows-only stdlib module; only reached on Windows

        key_path = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            value, _kind = winreg.QueryValueEx(key, "Personal")
        if value:
            return Path(os.path.expandvars(str(value)))
    except (OSError, ImportError):
        pass
    return Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Documents"


def _linux_documents_dir(xdg_config: Path) -> Path:
    """`$XDG_DOCUMENTS_DIR`, else the entry in `<xdg_config>/user-dirs.dirs`, else `~/Documents`
    (what wxStandardPaths::GetDocumentsDir() returns through xdg-user-dirs)."""
    env = os.environ.get("XDG_DOCUMENTS_DIR")
    if env:
        return Path(os.path.expandvars(env))
    try:
        for raw_line in (xdg_config / "user-dirs.dirs").read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if line.startswith("XDG_DOCUMENTS_DIR="):
                value = line.split("=", 1)[1].strip().strip('"').replace("$HOME", str(Path.home()))
                if value:
                    return Path(os.path.expandvars(value))
    except OSError:
        pass
    return Path.home() / "Documents"


def _windows_temp_dir(home: str | None, local: Path) -> Path:
    """`<KIWRAP_HOME>/tmp` only while it keeps the IPC pipe path short; a long storage override
    (`--storage-home`, pytest's basetemp) falls back to `%TEMP%/kiwrap` (or `%LOCALAPPDATA%/kiwrap/tmp`),
    which does not follow the override - the "short TEMP" guarantee must hold for every storage home."""
    preferred = (Path(home) if home else local / "kiwrap") / "tmp"
    if len(str(preferred)) <= MAX_TEMP_DIR_CHARS:
        return preferred
    system_temp = os.environ.get("TEMP") or os.environ.get("TMP")
    fallback = Path(system_temp) / "kiwrap" if system_temp else local / "kiwrap" / "tmp"
    if len(str(fallback)) > MAX_TEMP_DIR_CHARS:
        log.warning("temp dir %s is longer than %d chars; KiCad IPC pipe paths may fail", fallback, MAX_TEMP_DIR_CHARS)
    return fallback


def current() -> Platform:
    """Return the platform description for this process."""
    home = os.environ.get("KIWRAP_HOME")
    if sys.platform.startswith("win"):
        local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        roaming = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        return Platform(
            name="windows",
            exe_suffix=".exe",
            storage_home=Path(home) if home else local / "kiwrap",
            user_kicad_config_home=roaming / "kicad",
            temp_dir=_windows_temp_dir(home, local),
            user_kicad_documents_home=_windows_documents_dir() / "KiCad",
        )
    if sys.platform.startswith("linux"):
        xdg_data = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
        xdg_config = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        runtime = os.environ.get("XDG_RUNTIME_DIR")
        return Platform(
            name="linux",
            exe_suffix="",
            storage_home=Path(home) if home else xdg_data / "kiwrap",
            user_kicad_config_home=xdg_config / "kicad",
            temp_dir=Path(runtime) / "kiwrap" if runtime else Path("/tmp") / f"kiwrap-{os.getuid()}",
            user_kicad_documents_home=_linux_documents_dir(xdg_config) / "KiCad",
        )
    raise UnsupportedPlatformError(f"kiwrap does not support {sys.platform} yet (Windows and Linux only)")
