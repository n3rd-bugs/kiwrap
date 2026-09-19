"""Atomic text writes for design files (`.kicad_sch`, `.kicad_pcb`, `.kicad_pro`, `.kicad_dru`).

A design file written in place with a truncating `open("w")` is destroyed when the process dies
between the truncate and the last write (an MCP host's TerminateProcess, pytest-timeout's
`os._exit`): kicad-cli then refuses to load the half-written file and the agent has to know to
restore a snapshot (reviewer finding, 2026-09-20). `atomic_write_text` writes `<name>.kiwrap-tmp`
next to the target, flushes it to disk and `os.replace`s it over the target, so the file on disk
is always either the old or the new complete version. Line endings are written as given
(`newline=""`: the caller's text is the exact byte content, the lossless CST keeps the file's
own endings).

Windows: `os.replace` fails with PermissionError while another process (an antivirus scan, a
KiCad that is reading the file) holds the target; the replace is retried briefly before the
error propagates.
"""

from __future__ import annotations

import contextlib
import os
import time
from pathlib import Path

__all__ = ["TMP_SUFFIX", "atomic_write_text"]

TMP_SUFFIX = ".kiwrap-tmp"
_REPLACE_ATTEMPTS = 20
_REPLACE_DELAY_S = 0.05


def atomic_write_text(path: str | Path, text: str, *, encoding: str = "utf-8", newline: str = "") -> Path:
    """Write `text` to `path` through a same-directory temp file + `os.replace`. Returns `path`."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + TMP_SUFFIX)
    try:
        with tmp.open("w", encoding=encoding, newline=newline) as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        for attempt in range(_REPLACE_ATTEMPTS):
            try:
                tmp.replace(target)
                break
            except PermissionError:
                if attempt == _REPLACE_ATTEMPTS - 1:
                    raise
                time.sleep(_REPLACE_DELAY_S)
    finally:
        if tmp.exists():
            with contextlib.suppress(OSError):
                tmp.unlink()
    return target
