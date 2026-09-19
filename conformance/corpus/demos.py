"""Locate KiCad's own demo projects at test time (never committed to the corpus).

`collect_demo_files(share_dir)` yields the text-format files of the demo projects under
`<install>/share/kicad/demos` that the conformance suite exercises. It knows nothing about
KiCad versions: callers iterate `C:/Program Files/KiCad/*/share/kicad` (or whatever the
platform layer reports) and pass each share directory in turn.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

# Demo projects present in every install 7.0 - 10.0 (flat_hierarchy ships with 7.0 - 9.0 only).
DEMO_PROJECTS: tuple[str, ...] = (
    "ecc83",
    "complex_hierarchy",
    "pic_programmer",
    "flat_hierarchy",
    "video",
    "interf_u",
    "kit-dev-coldfire-xilinx_5213",
)

# Every KiCad s-expression text format the CST must round-trip.
TEXT_SUFFIXES: frozenset[str] = frozenset(
    {".kicad_sch", ".kicad_pcb", ".kicad_mod", ".kicad_sym", ".kicad_dru", ".kicad_wks", ".net"}
)
TABLE_NAMES: frozenset[str] = frozenset({"sym-lib-table", "fp-lib-table", "design-block-lib-table"})

# Demo files whose text is not a well-formed s-expression, keyed by path relative to `demos/`
# (forward slashes). KiCad 10.0.0 ships these two with every teardrops block garbled as
# `(curved_edges no)filter_ratio 0.9)` (a missing `(`), so each pad closes one list too many;
# kicad-cli 10 "loads" the board only because its parser stops at the premature `)` of
# `kicad_pcb` and silently drops every footprint after it. No lossless parser can round-trip
# them, so T1 skips them and says so.
KNOWN_MALFORMED: dict[str, str] = {
    "royalblue54L_feather/RoyalBlue54L-Feather.kicad_pcb": "unbalanced parens: '(curved_edges no)filter_ratio 0.9)' in every pad",
    "royalblue54L_feather/lib/footprints/RoyalBlue54L-Feather-Connector_FFC-FPC.pretty/"
    "CONN-SMD_5P-P0.50_HCTL_HC-FPC-05-09-5RLTAG.kicad_mod": "same garbled teardrops block",
}


@dataclass(frozen=True)
class DemoFile:
    project: str
    path: Path

    @property
    def kind(self) -> str:
        """File family: the suffix without the dot, or the lib-table name."""
        return self.path.name if self.path.name in TABLE_NAMES else self.path.suffix.lstrip(".")


def is_text_format(path: Path) -> bool:
    return path.suffix in TEXT_SUFFIXES or path.name in TABLE_NAMES


def is_known_malformed(demos_dir: Path, path: Path) -> bool:
    return path.relative_to(demos_dir).as_posix() in KNOWN_MALFORMED


def collect_demo_files(
    share_dir: str | Path,
    *,
    projects: tuple[str, ...] = DEMO_PROJECTS,
    all_projects: bool = False,
    include_malformed: bool = False,
) -> Iterator[DemoFile]:
    """Yield the text-format files of the selected demo projects under `share_dir/demos`.

    Missing projects are skipped silently (not every version ships every demo). With
    `all_projects=True` every subdirectory of `demos` is walked instead of the fixed list.
    Files listed in `KNOWN_MALFORMED` are left out unless `include_malformed=True`.
    """
    demos = Path(share_dir) / "demos"
    if not demos.is_dir():
        return
    names = sorted(p.name for p in demos.iterdir() if p.is_dir()) if all_projects else projects
    for name in names:
        root = demos / name
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if not (path.is_file() and is_text_format(path)):
                continue
            if not include_malformed and is_known_malformed(demos, path):
                continue
            yield DemoFile(project=name, path=path)
