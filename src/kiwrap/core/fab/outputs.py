"""Where kiwrap's output files go: per-call render directories, detached export directories and
their retention sweep (review items 4 and 7, B4; theme T2 `resolve_out_path`).

Review B4 (requests/kiwrap-feature-requests.review.md §3): `render_dir()` used to `rmtree`
`<storage>/tmp/render/<project.name>/<kind>` at the start of EVERY render, so a bottom render
deleted the top PNG and a second project with the same name deleted the first one's. Now:

- every call gets its own directory `<storage>/tmp/<area>/<name>-<sha8(project dir)>/<kind>/<ts>-<rand>/`
  (`per_call_dir`); nothing a call wrote is touched by the next one;
- `sweep()` is the only deleter: per `<name>-<sha8>/<kind>` it keeps the newest `KEEP_PER_KIND`
  directories, deletes anything older than `MAX_AGE_S`, and never deletes a directory younger
  than `MIN_AGE_S` (a concurrent agent may still be writing into it);
- a caller-given path (`out_path` / `out_dir`) is the caller's: never swept.

Read-only mode (docs/DECISIONS.md D23) is `kiwrap.core.state.readonly`: `is_readonly` and the
output-path rules (`resolve_out_path`: absolute, never `.kicad_*`, never KiCad's user directories,
existing parent; inside a non-persisting call the caller's path is exempt from the project guard
and an unset one is the call's results directory) come from there; this module adds the render /
scratch directories under `<storage>/tmp/` and their sweep.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import hashlib
import secrets
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kiwrap.core.state import KiwrapError, readonly

#: newest per-call directories kept per project and kind
KEEP_PER_KIND = 20
#: anything older is deleted by the next sweep (24 h, review B4)
MAX_AGE_S = 24 * 3600
#: never delete a directory younger than this (another call may still be writing into it)
MIN_AGE_S = 60


def project_key(project: Any) -> str:
    """`<name>-<sha8(resolved dir)>`: two projects with the same name never share a directory."""
    d = Path(project.dir)
    with contextlib.suppress(OSError):
        d = d.resolve()
    digest = hashlib.sha1(str(d).lower().encode("utf-8")).hexdigest()[:8]
    return f"{project.name}-{digest}"


def is_readonly(ctx: Any, args: Any = None) -> bool:
    """D23: the call must not write into the project (`kiwrap.core.state.readonly.is_readonly`)."""
    return readonly.is_readonly(ctx, args)


def per_call_dir(storage_home: Path, project: Any, kind: str, *, area: str = "render") -> Path:
    """A fresh directory for one call: `<storage>/tmp/<area>/<name>-<sha8>/<kind>/<utc ts>-<rand>/`.
    Sweeps the older siblings first (`sweep`)."""
    parent = Path(storage_home) / "tmp" / area / project_key(project) / kind
    sweep(Path(storage_home) / "tmp" / area)
    stamp = _dt.datetime.now(_dt.UTC).strftime("%Y%m%dT%H%M%S")
    d = parent / f"{stamp}-{secrets.token_hex(3)}"
    d.mkdir(parents=True, exist_ok=False)
    return d


def kept_until() -> str:
    """ISO time (UTC) after which `sweep` may delete a per-call directory created now."""
    t = _dt.datetime.now(_dt.UTC) + _dt.timedelta(seconds=MAX_AGE_S)
    return t.isoformat(timespec="seconds")


def sweep(
    root: Path,
    *,
    keep: int = KEEP_PER_KIND,
    max_age_s: float = MAX_AGE_S,
    min_age_s: float = MIN_AGE_S,
    now: float | None = None,
) -> list[Path]:
    """Delete old per-call directories under `root` (`<root>/<project key>/<kind>/<call dir>`):
    beyond the newest `keep` per project+kind, or older than `max_age_s`; never one younger than
    `min_age_s`. Returns what was deleted. Errors (a file still open on Windows) are skipped: the
    next sweep retries."""
    now = time.time() if now is None else now
    removed: list[Path] = []
    if not root.is_dir():
        return removed
    for proj in _dirs(root):
        for kind in _dirs(proj):
            calls = sorted(_dirs(kind), key=_mtime, reverse=True)
            for i, d in enumerate(calls):
                age = now - _mtime(d)
                if age < min_age_s:
                    continue
                if i >= keep or age > max_age_s:
                    shutil.rmtree(d, ignore_errors=True)
                    if not d.exists():
                        removed.append(d)
    return removed


def _dirs(p: Path) -> list[Path]:
    try:
        return [c for c in p.iterdir() if c.is_dir()]
    except OSError:
        return []


def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


@dataclass(frozen=True)
class OutPath:
    """A resolved output location and whether kiwrap owns it (swept) or the caller does."""

    path: Path
    owned: bool  # True: a per-call directory under the storage home (swept); False: caller's


def check_caller_path(
    ctx: Any, project: Any, requested: str, *, what: str, suffixes: tuple[str, ...] | None = None
) -> Path:
    """Validate a caller-given output path with the read-only module's rules (theme T2:
    absolute, never a `.kicad_*` file, never KiCad's user directories, existing parent; inside a
    non-persisting call the path is exempted from the project guard) and, for a FILE, one of
    `suffixes`."""
    if suffixes is not None and Path(requested).suffix.lower() not in suffixes:
        raise KiwrapError(
            "E_BAD_ARGS",
            f"{what} must end in {' or '.join(suffixes)}, got {Path(requested).name!r}",
            fix=f"use a {suffixes[0]} file name",
        )
    path = readonly.resolve_out_path(ctx, project, requested, what)
    if suffixes is None and path.exists() and not path.is_dir():
        raise KiwrapError(
            "E_BAD_ARGS", f"{what} {path.as_posix()} exists and is a file", fix="pass a directory"
        )
    return path


def resolve_out_dir(
    ctx: Any, project: Any, requested: str | None, kind: str, *, readonly_call: bool, default: Path | None
) -> OutPath:
    """The output DIRECTORY of an export (review item 4: anywhere, outside the project allowed).
    A caller `requested` directory is validated and used; unset -> `default` (e.g. `<project>/fab`)
    unless the call is read-only (or there is no default): then the call's results directory
    under the storage home (`readonly.resolve_out_path`)."""
    if requested and requested.strip().lower() != "auto":
        return OutPath(check_caller_path(ctx, project, requested, what="out_dir"), owned=False)
    if not readonly_call and default is not None:
        return OutPath(default, owned=False)
    return OutPath(readonly.resolve_out_path(ctx, project, None, kind), owned=True)


def scratch_dir(ctx: Any, project: Any, kind: str) -> Path:
    """A per-call working directory under the storage home for intermediate kicad-cli output
    (never inside the project: read-only mode, D23)."""
    return per_call_dir(ctx.services.storage_home, project, kind, area="work")


__all__ = [
    "KEEP_PER_KIND",
    "MAX_AGE_S",
    "MIN_AGE_S",
    "OutPath",
    "check_caller_path",
    "is_readonly",
    "kept_until",
    "per_call_dir",
    "project_key",
    "resolve_out_dir",
    "scratch_dir",
    "sweep",
]
