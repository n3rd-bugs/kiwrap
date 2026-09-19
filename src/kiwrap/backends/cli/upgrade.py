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
  (exit 3, or -1/127 for boards on 7/8), which is why `kw_project upgrade` semantic-diffs and
  re-runs ERC/DRC on the copy before replacing anything (§6 emit policy).
"""

from __future__ import annotations

import hashlib
import shutil
from collections.abc import Mapping
from pathlib import Path

from kiwrap.backends.cli.runner import has_subcommand, resolve_caps, run_kicad_cli
from kiwrap.capability import Capabilities, Unsupported
from kiwrap.installation import KiCadInstallation
from kiwrap.sandbox import ConfigRoot

OLDEST_KICAD_WITH_SCH_PCB_UPGRADE = "10.0.0"

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
