"""`kiwrap.platform` seam rules that do not need a KiCad: the short-TEMP guarantee for every storage
home (finding: a long KIWRAP_HOME produced a 142-char TEMP, longer than nng's pipe-path limit) and
the user Documents lookup that `KICAD<major>_3RD_PARTY` resolution relies on."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kiwrap import platform as plat
from kiwrap.platform import MAX_TEMP_DIR_CHARS, current


def test_windows_temp_dir_ignores_a_long_storage_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    local = Path("C:/Users/u/AppData/Local")
    short_home = "C:/kw"
    assert plat._windows_temp_dir(short_home, local) == Path(short_home) / "tmp"
    assert plat._windows_temp_dir(None, local) == local / "kiwrap" / "tmp"
    long_home = "C:/Users/u/AppData/Local/Temp/pytest-of-u/pytest-123/kiwrap-home0/" + "x" * 40
    monkeypatch.setenv("TEMP", "C:/Users/u/AppData/Local/Temp")
    chosen = plat._windows_temp_dir(long_home, local)
    assert chosen == Path("C:/Users/u/AppData/Local/Temp/kiwrap")
    assert len(str(chosen)) <= MAX_TEMP_DIR_CHARS
    monkeypatch.delenv("TEMP", raising=False)
    monkeypatch.delenv("TMP", raising=False)
    assert plat._windows_temp_dir(long_home, local) == local / "kiwrap" / "tmp"


def test_current_temp_dir_is_short_even_under_pytest():
    """The session's KIWRAP_HOME (pytest basetemp, ~100 chars) must not leak into TEMP."""
    p = current()
    assert len(str(p.temp_dir)) <= MAX_TEMP_DIR_CHARS, p.temp_dir
    if p.is_windows and len(os.environ.get("KIWRAP_HOME", "")) + len("/tmp") > MAX_TEMP_DIR_CHARS:
        assert not str(p.temp_dir).startswith(os.environ["KIWRAP_HOME"])


def test_linux_documents_dir_rules(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    xdg_config = tmp_path / "config"
    xdg_config.mkdir()
    monkeypatch.delenv("XDG_DOCUMENTS_DIR", raising=False)
    assert plat._linux_documents_dir(xdg_config) == Path.home() / "Documents"
    (xdg_config / "user-dirs.dirs").write_text(
        'XDG_DESKTOP_DIR="$HOME/Desktop"\nXDG_DOCUMENTS_DIR="$HOME/Docs"\n'
    )
    assert plat._linux_documents_dir(xdg_config) == Path.home() / "Docs"
    monkeypatch.setenv("XDG_DOCUMENTS_DIR", "/srv/docs")
    assert plat._linux_documents_dir(xdg_config) == Path("/srv/docs")


def test_user_kicad_documents_home_is_read_only_and_ends_in_kicad():
    p = current()
    assert p.user_kicad_documents_home is not None and p.user_kicad_documents_home.name == "KiCad"


# ---- child lifetime (kiwrap.platform.lifetime) -------------------------------------------------------

_PARENT = r"""
import subprocess, sys, time
from kiwrap.platform.lifetime import bind_child_lifetime, popen_lifetime_kwargs
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], **popen_lifetime_kwargs())
print("BOUND", bind_child_lifetime(child), child.pid, flush=True)
time.sleep(120)
"""


def test_bound_child_dies_with_its_parent():
    """Phase 1c finding 7: the router's JVM survived a killed kiwrap server as an orphan. A child
    started through `kiwrap.platform.lifetime` (Job Object with kill-on-close on Windows,
    PDEATHSIG on POSIX) is gone shortly after its parent is killed."""
    import subprocess
    import sys
    import time

    parent = subprocess.Popen(
        [sys.executable, "-c", _PARENT], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    try:
        line = parent.stdout.readline() if parent.stdout else ""
        assert line.startswith("BOUND True "), (line, parent.stderr.read() if parent.stderr else "")
        child_pid = int(line.split()[2])
        assert _alive(child_pid)
        parent.kill()
        parent.wait(timeout=10)
        deadline = time.time() + 10
        while _alive(child_pid) and time.time() < deadline:
            time.sleep(0.1)
        assert not _alive(child_pid), f"child {child_pid} outlived its parent"
    finally:
        if parent.poll() is None:
            parent.kill()


def _alive(pid: int) -> bool:
    import ctypes
    import os
    import sys

    if sys.platform.startswith("win"):
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == 259  # STILL_ACTIVE
            return True
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
