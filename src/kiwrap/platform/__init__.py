"""Platform seam: everything that differs between Windows and Linux lives behind this module.

Rules
- No other module may call `sys.platform`, `os.name`, `platform.system()` or hard-code a Windows or
  Linux path. They ask `current()` instead.
- macOS is not supported yet; `current()` raises `UnsupportedPlatformError` there.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


class UnsupportedPlatformError(RuntimeError):
    pass


@dataclass(frozen=True)
class Platform:
    name: str  # "windows" | "linux"
    exe_suffix: str  # ".exe" | ""
    storage_home: Path  # kiwrap's own state dir (override with KIWRAP_HOME)
    user_kicad_config_home: Path  # the user's real KiCad config root; READ ONLY, never written
    temp_dir: Path  # a short temp dir usable for KiCad's IPC socket path (< ~100 chars)

    @property
    def is_windows(self) -> bool:
        return self.name == "windows"

    def python_name(self) -> str:
        return "python" + self.exe_suffix

    def kicad_cli_name(self) -> str:
        return "kicad-cli" + self.exe_suffix


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
            temp_dir=(Path(home) if home else local / "kiwrap") / "tmp",
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
        )
    raise UnsupportedPlatformError(f"kiwrap does not support {sys.platform} yet (Windows and Linux only)")
