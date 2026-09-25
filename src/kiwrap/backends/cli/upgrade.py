"""`sch upgrade` / `pcb upgrade` / `sym upgrade` / `fp upgrade` — always on a COPY.

Verified facts (docs/ARCHITECTURE.md §2 and §6, docs/VERIFICATION.md claim 9,
spikes/fmt-compat + spikes/verify-format-compat):
- `sch upgrade` / `pcb upgrade` exist only on 10.0 (`cli.has_sch_pcb_upgrade`); they rewrite the
  file IN PLACE (`--force` re-saves regardless of the stamp) and segfaulted on malformed input, so
  kiwrap never points them at the user's file: `upgrade_copy` copies `src` into `dst_dir` first,
  upgrades the copy, and leaves `src` byte-identical.
- `sym upgrade` / `fp upgrade` exist on 7.0-10.0; without `-o` they rewrite the input in place,
  which is what kiwrap wants because the input is already the copy.
- Files upgraded by KiCad 10 (stamps 20260306 sch / 20260206 pcb) are rejected by 7/8/9
  (exit 3, or -1/127 for boards on 7/8), which is why `kw_project_upgrade` (D24) semantic-diffs and
  re-runs ERC/DRC on the copy before replacing anything (§6 emit policy).
- `sch upgrade --force <root>` re-saves the ROOT sheet only: a child sheet keeps its old stamp
  (verified 2026-09-24 on a copy of the 10.0 pic_programmer demo with 10.99.0: root 20260101 ->
  20260830, `pic_sockets.kicad_sch` stayed 20260101). A sheet upgraded on its own keeps its symbol
  instance data (the diff is the header plus library-symbol normalisation), so
  `upgrade_project_copy` upgrades every sheet file separately. Every `sch/pcb upgrade` run also
  rewrote an existing `.kicad_prl` (restored by the runner, review B2); `.kicad_pro` stayed
  byte-identical.

`format_releases()` exposes the version matrix as "which release writes which stamps" so the core
can name the KiCad a project belongs to and the release to install (D21 `install_hint`) without
holding a version number itself.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import shutil
import stat
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kiwrap.backends.cli.runner import has_subcommand, resolve_caps, run_kicad_cli
from kiwrap.capability import Capabilities, Unsupported, load_matrix
from kiwrap.installation import KiCadInstallation, version_tuple
from kiwrap.sandbox import ConfigRoot

OLDEST_KICAD_WITH_SCH_PCB_UPGRADE = "10.0.0"
#: how a missing release is installed on the supported host (docs/DECISIONS.md D18: Windows only)
INSTALL_HINT_TEMPLATE = "winget install KiCad.KiCad --version {version}"
_VERSION_RE = re.compile(rb"\(version\s+(\d+)\s*\)")
#: files `upgrade_project_copy` copies next to the design files (what kicad-cli loads with them);
#: never `.kicad_prl` (per-user view state) and never the 3D models / libraries directories
_PROJECT_SIDECARS = (".kicad_pro", ".kicad_dru", ".kicad_sym")
_TABLES = ("sym-lib-table", "fp-lib-table", "design-block-lib-table")

#: File suffix -> kicad-cli subcommand group.
_KIND_BY_SUFFIX: Mapping[str, str] = {
    ".kicad_sch": "sch",
    ".kicad_pcb": "pcb",
    ".kicad_sym": "sym",
    ".kicad_mod": "fp",
    ".pretty": "fp",
}


def upgrade_kind(path: Path) -> str:
    """'sch' | 'pcb' | 'sym' | 'fp' for a file (or `.pretty` directory), else ValueError."""
    suffix = Path(path).suffix.lower()
    if suffix not in _KIND_BY_SUFFIX:
        raise ValueError(
            f"cannot upgrade {path}: expected .kicad_sch/.kicad_pcb/.kicad_sym/.kicad_mod/.pretty"
        )
    return _KIND_BY_SUFFIX[suffix]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def upgrade_copy(
    install: KiCadInstallation,
    config: ConfigRoot,
    src: Path,
    dst_dir: Path,
    *,
    force: bool = True,
    caps: Capabilities | None = None,
    timeout_s: float = 300,
) -> Path:
    """Copy `src` into `dst_dir` and upgrade the COPY with this install's kicad-cli; return its path.

    `sch`/`pcb` need `cli.has_sch_pcb_upgrade` (10.0+) -> otherwise `Unsupported` whose fallback
    names the file-level path (kiwrap's own epoch serializers, docs/ARCHITECTURE.md §6 layer 3).
    `sym`/`fp` work on every version. `src` is verified unchanged (sha256) after the run.
    """
    caps = resolve_caps(install, caps)
    src, dst_dir = Path(src), Path(dst_dir)
    kind = upgrade_kind(src)
    if kind in ("sch", "pcb"):
        if not caps.has("cli.has_sch_pcb_upgrade"):
            raise Unsupported(
                "cli.has_sch_pcb_upgrade",
                install,
                oldest_kicad_with_it=OLDEST_KICAD_WITH_SCH_PCB_UPGRADE,
                fallback="kiwrap.formats epoch serializer (re-emit at the target epoch)",
            )
    elif not has_subcommand(caps, f"{kind} upgrade"):
        raise Unsupported(
            f"cli.subcommands:{kind} upgrade", install, oldest_kicad_with_it=None, fallback=None
        )

    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / src.name
    if dst.resolve() == src.resolve():
        raise ValueError(f"dst_dir {dst_dir} would upgrade {src} in place; choose another directory")
    if src.is_dir():
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst)
        before = None
    else:
        shutil.copy2(src, dst)
        before = _sha256(src)

    args: list[str] = [kind, "upgrade"]
    if force:
        args.append("--force")
    # Every upgrade subcommand rewrites its input in place when `-o` is omitted (verified on 10.0:
    # `sym upgrade --force x.kicad_sym` -> stamp 20251024, `fp upgrade --force x.pretty` ->
    # 20260206 in every .kicad_mod); `-o` equal to the input is refused with exit 4 ("Output path
    # must not conflict with existing path"), so the copy is upgraded in place.
    args.append(str(dst))
    run_kicad_cli(
        install, args, config=config, caps=caps, timeout_s=timeout_s, accept=(0,), watch_dirs=[dst_dir]
    )

    if (
        before is not None and _sha256(src) != before
    ):  # pragma: no cover - the whole point is that it never happens
        raise RuntimeError(f"kicad-cli {kind} upgrade modified the source file {src}; this must never happen")
    return dst


# --------------------------------------------------------------------------------------------
# project upgrade on a copy (kw_project_upgrade / upgrade_preview, docs/DECISIONS.md D21, D24)
# --------------------------------------------------------------------------------------------


def read_stamp(path: Path) -> int | None:
    """`(version N)` of a KiCad s-expression file (first 4 KB), None when absent."""
    try:
        with Path(path).open("rb") as fh:
            m = _VERSION_RE.search(fh.read(4096))
    except OSError:
        return None
    return int(m.group(1)) if m else None


@dataclass(frozen=True)
class UpgradedFile:
    """One design file of an upgraded copy: `source` (the project's file), `copy` (the upgraded
    file in the work directory), the stamps before/after and whether the bytes changed. `rel` is
    the file's path relative to the project directory (posix: `sch/Connectors.kicad_sch` for a
    sub-sheet in a subdirectory; empty on a result built by hand, then the name is used)."""

    source: Path
    copy: Path
    kind: str
    stamp_before: int | None
    stamp_after: int | None
    changed: bool
    rel: str = ""

    @property
    def relpath(self) -> str:
        return self.rel or self.source.name

    def to_dict(self) -> dict[str, Any]:
        return {
            "file": self.relpath,
            "kind": self.kind,
            "stamp_before": self.stamp_before,
            "stamp_after": self.stamp_after,
            "changed": self.changed,
        }


@dataclass
class ProjectCopy:
    """`upgrade_project_copy`'s result: the work directory and every upgraded design file."""

    work_dir: Path
    files: list[UpgradedFile] = field(default_factory=list)

    def copy_of(self, source: Path) -> Path:
        for f in self.files:
            if Path(f.source) == Path(source):
                return Path(f.copy)
        return self.work_dir / Path(source).name


def relative_design_path(project_dir: Path, path: Path) -> Path:
    """`path` relative to `project_dir` (a sub-sheet keeps its subdirectory, `sch/x.kicad_sch`); a
    file outside the project directory is copied by name."""
    try:
        return Path(path).resolve().relative_to(Path(project_dir).resolve())
    except (OSError, ValueError):
        return Path(Path(path).name)


def _norm_key(path: Path) -> str:
    """A comparison key for one file path (resolved, case-folded on Windows)."""
    try:
        resolved = Path(path).resolve()
    except OSError:
        resolved = Path(path)
    return os.path.normcase(str(resolved))


def copy_project(project_dir: Path, design_files: Iterable[Path], work_dir: Path) -> Path:
    """Copy the design files plus the project's sidecars (`.kicad_pro`, `.kicad_dru`, local
    `.kicad_sym`, the lib tables) into `work_dir` (emptied first); `.kicad_prl` is never copied.
    Each design file keeps its path relative to the project directory, so a hierarchy with sheets
    in subdirectories (KiCad's 10.0 royalblue54L_feather demo: `sch/*.kicad_sch`) loads from the
    copy exactly as from the project (fixer finding 2: the copy used to flatten them away, so the
    ERC before/after of the upgrade ran without the sub-sheets)."""
    project_dir, work_dir = Path(project_dir), Path(work_dir)
    _remove_tree(work_dir)
    work_dir.mkdir(parents=True)
    for p in project_dir.iterdir():
        if p.is_file() and (p.suffix.lower() in _PROJECT_SIDECARS or p.name in _TABLES):
            _copy_writable(p, work_dir / p.name)
    for src in design_files:
        dst = work_dir / relative_design_path(project_dir, Path(src))
        dst.parent.mkdir(parents=True, exist_ok=True)
        _copy_writable(Path(src), dst)
    return work_dir


def _copy_writable(src: Path, dst: Path) -> None:
    """Copy bytes and times but never a read-only bit: the copy is upgraded in place and the work
    directory is emptied on the next run (a read-only project file made both fail with 'Access is
    denied' on Windows, fixer finding 5)."""
    shutil.copy2(src, dst)
    dst.chmod(stat.S_IREAD | stat.S_IWRITE)


def _remove_tree(path: Path) -> None:
    """`shutil.rmtree` that also removes read-only files (Python 3.11: no `onexc`)."""
    if not path.exists():
        return
    for p in path.rglob("*"):
        if p.is_file():
            with contextlib.suppress(OSError):
                p.chmod(stat.S_IREAD | stat.S_IWRITE)
    shutil.rmtree(path)


def upgrade_project_copy(
    install: KiCadInstallation,
    config: ConfigRoot,
    project_dir: Path,
    design_files: Iterable[Path],
    work_dir: Path,
    *,
    caps: Capabilities | None = None,
    timeout_s: float = 300,
    convert: Iterable[Path] | None = None,
) -> ProjectCopy:
    """Copy the project into `work_dir` and upgrade the `.kicad_sch` / `.kicad_pcb` files of the
    copy to this install's format (`sch upgrade --force` per sheet: the root's upgrade leaves the
    children alone, see the module docstring). Sheets in subdirectories are upgraded at their
    mirrored relative path (`UpgradedFile.rel`). The project directory is never written; the
    caller replaces its files from `ProjectCopy.files` once it has checked the copy.

    `convert` (docs/DECISIONS.md D24: `kw_project_upgrade` converts exactly its `files`): the design
    files to upgrade; every design file is still copied, so the hierarchy loads for ERC, but only
    these are re-saved and listed in `ProjectCopy.files`. None upgrades every design file."""
    caps = resolve_caps(install, caps)
    if not caps.has("cli.has_sch_pcb_upgrade"):
        raise Unsupported(
            "cli.has_sch_pcb_upgrade",
            install,
            oldest_kicad_with_it=OLDEST_KICAD_WITH_SCH_PCB_UPGRADE,
            fallback=None,
        )
    copied = [Path(p) for p in design_files]
    sources = copied if convert is None else [Path(p) for p in convert]
    known = {_norm_key(p) for p in copied}
    copied.extend(p for p in sources if _norm_key(p) not in known)
    work = copy_project(project_dir, copied, work_dir)
    out = ProjectCopy(work_dir=work)
    for src in sources:
        kind = upgrade_kind(src)
        rel = relative_design_path(project_dir, src)
        dst = work / rel
        before_bytes = dst.read_bytes()
        before = read_stamp(dst)
        run_kicad_cli(
            install,
            [kind, "upgrade", "--force", str(dst)],
            config=config,
            caps=caps,
            timeout_s=timeout_s,
            accept=(0,),
            watch_dirs=[work],
        )
        out.files.append(
            UpgradedFile(
                source=src,
                copy=dst,
                kind=kind,
                stamp_before=before,
                stamp_after=read_stamp(dst),
                changed=dst.read_bytes() != before_bytes,
                rel=rel.as_posix(),
            )
        )
    return out


# --------------------------------------------------------------------------------------------
# which release writes which stamps (the version matrix as data for the core)
# --------------------------------------------------------------------------------------------


#: a matrix note that marks an entry as not (yet) a published release: the 10.0 branch HEAD or the
#: next major seeded from master (kiwrap/data/kicad_version_matrix.json notes)
_UNRELEASED_NOTE = re.compile(r"\bunreleased\b|\bmaster\b", re.IGNORECASE)


def install_hint(version: str, *, released: bool = True) -> str | None:
    """How to install `version` on the supported host; None for a nightly (`x.99`) or a version
    the matrix marks unreleased, which have no installer to point at."""
    parts = version_tuple(version)
    if not released or (len(parts) >= 2 and parts[1] == 99):
        return None
    return INSTALL_HINT_TEMPLATE.format(version=version)


def format_releases() -> list[dict[str, Any]]:
    """Every version the matrix knows, newest first: `{version, line ('10.0'), formats {sch, pcb},
    nightly, released, install_hint}`. A version whose record has no `formats.sch` /
    `formats.pcb` is left out. The core uses it to name the KiCad line of a file stamp and the
    release to install (D21 `install_hint`)."""
    matrix = load_matrix()
    out: list[dict[str, Any]] = []
    for version in reversed(matrix.known()):
        values = matrix.versions[version]
        formats = {k: values.get(f"formats.{k}") for k in ("sch", "pcb")}
        if not all(isinstance(v, int) for v in formats.values()):
            continue
        parts = version_tuple(version)
        nightly = len(parts) >= 2 and parts[1] == 99
        released = not nightly and not _UNRELEASED_NOTE.search(matrix.notes.get(version, ""))
        out.append(
            {
                "version": version,
                "line": ".".join(str(p) for p in parts[:2]),
                "formats": formats,
                "nightly": nightly,
                "released": released,
                "install_hint": install_hint(version, released=released),
            }
        )
    return out
