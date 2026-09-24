"""The file plan of a fabrication export without running KiCad (review item 4 `dry_run`), and the
layer-name handling of the `layers` filter.

File names are KiCad's own naming, verified with kicad-cli 10.0.6 and 10.99 on the 4-layer
`video` demo (scratchpad spike_names.py; tests/test_fab_reviewer_live.py asserts plan == real run):

- gerbers: `<stem>-<layer name, '.' -> '_'>.<ext>` where the layer name is the board's user name
  for the layer when it has one (video's F.Cu is `top_copper` -> `video-top_copper.gtl`), else
  KiCad's standard name (`F.SilkS` -> `F.Silkscreen`, `Dwgs.User` -> `User.Drawings`, ...);
  Protel extensions unless `--no-protel-ext` (then every layer is `.gbr`); plus `<stem>-job.gbrjob`.
  Without `--layers`, kicad-cli plots every layer of the board's layer table.
- drill: `<stem>.drl`, or `<stem>-PTH.drl` + `<stem>-NPTH.drl` with `--excellon-separate-th`
  (both files even without NPTH holes); maps `<base>-drl_map.<ext>` (`gerberx2` -> `.gbr`).

Every planned name carries `predicted: true`: kiwrap did not run KiCad to get it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

#: KiCad's standard (English) layer names where they differ from the canonical token.
STANDARD_NAMES: Mapping[str, str] = {
    "F.Adhes": "F.Adhesive",
    "B.Adhes": "B.Adhesive",
    "F.SilkS": "F.Silkscreen",
    "B.SilkS": "B.Silkscreen",
    "F.CrtYd": "F.Courtyard",
    "B.CrtYd": "B.Courtyard",
    "Dwgs.User": "User.Drawings",
    "Cmts.User": "User.Comments",
    "Eco1.User": "User.Eco1",
    "Eco2.User": "User.Eco2",
}
#: the reverse, so `layers=["F.Silkscreen"]` means F.SilkS (kicad-cli wants the canonical token)
CANONICAL_OF: Mapping[str, str] = {v: k for k, v in STANDARD_NAMES.items()}

PROTEL_EXT: Mapping[str, str] = {
    "F.Cu": "gtl",
    "B.Cu": "gbl",
    "F.Adhes": "gta",
    "B.Adhes": "gba",
    "F.Paste": "gtp",
    "B.Paste": "gbp",
    "F.SilkS": "gto",
    "B.SilkS": "gbo",
    "F.Mask": "gts",
    "B.Mask": "gbs",
    "Edge.Cuts": "gm1",
}

#: layers a board may use even when its layer table does not list them (kicad-cli accepts them)
_ALWAYS_KNOWN = ("F.Cu", "B.Cu", "Edge.Cuts")
_INNER = re.compile(r"^In(\d+)\.Cu$")
_ILLEGAL = re.compile(r'[\\/:*?"<>|]')


def protel_ext(layer: str) -> str:
    m = _INNER.match(layer)
    if m:
        return f"g{int(m.group(1))}"
    return PROTEL_EXT.get(layer, "gbr")


def layer_file_token(layer: str, user_name: str | None = None) -> str:
    """The part of a plot file name KiCad derives from a layer."""
    name = user_name or STANDARD_NAMES.get(layer, layer)
    return _ILLEGAL.sub("_", name).replace(".", "_")


def board_layer_names(model: Any) -> list[tuple[str, str | None]]:
    """`[(canonical name, user name or None)]` of the board's layer table (file reader model)."""
    out: list[tuple[str, str | None]] = []
    for layer in getattr(model, "layers", ()) or ():
        name = str(getattr(layer, "name", "") or "")
        if not name:
            continue
        user = getattr(layer, "user_name", None)
        out.append((name, str(user) if user and str(user) != STANDARD_NAMES.get(name, name) else None))
    return out


def normalise_layers(
    requested: Sequence[str], table: Sequence[tuple[str, str | None]]
) -> tuple[list[str], list[str]]:
    """Map the caller's layer names onto kicad-cli's canonical tokens: exact, case-insensitive,
    KiCad's standard name (`F.Silkscreen`) or the board's own user name (`top_copper`). Returns
    (canonical names in the caller's order without duplicates, names that match nothing). With an
    empty `table` (no reader) every name passes through unchanged."""
    if not table:
        return list(dict.fromkeys(requested)), []
    lookup: dict[str, str] = {}
    for name, user in table:
        for key in (name, STANDARD_NAMES.get(name, name), user):
            if key:
                lookup.setdefault(key.lower(), name)
    for name in _ALWAYS_KNOWN:
        lookup.setdefault(name.lower(), name)
    out: list[str] = []
    unknown: list[str] = []
    for raw in requested:
        key = str(raw).strip()
        canon = lookup.get(key.lower()) or lookup.get(CANONICAL_OF.get(key, "").lower())
        if canon is None:
            unknown.append(key)
        elif canon not in out:
            out.append(canon)
    return out, unknown


def gerber_files(
    stem: str,
    layers: Iterable[str],
    table: Sequence[tuple[str, str | None]],
    *,
    protel: bool = True,
) -> list[str]:
    users = dict(table)
    names = [
        f"{stem}-{layer_file_token(layer, users.get(layer))}.{protel_ext(layer) if protel else 'gbr'}"
        for layer in layers
    ]
    return [*names, f"{stem}-job.gbrjob"]


_MAP_EXT = {"gerberx2": "gbr", "pdf": "pdf", "ps": "ps", "dxf": "dxf", "svg": "svg", "hpgl": "plt"}


def drill_files(stem: str, opts: Mapping[str, Any]) -> list[str]:
    fmt = str(opts.get("format") or "excellon")
    separate = bool(opts.get("excellon_separate_th")) or fmt == "gerber"
    bases = [f"{stem}-PTH", f"{stem}-NPTH"] if separate else [stem]
    files = [f"{b}-drl.gbr" if fmt == "gerber" else f"{b}.drl" for b in bases]
    if opts.get("generate_map"):
        ext = _MAP_EXT.get(str(opts.get("map_format") or "pdf"), "pdf")
        files += [f"{b}-drl_map.{ext}" for b in bases]
    return files


#: single-file board targets: extension of `<stem>.<ext>` (`backends.cli.export._EXT`)
SINGLE_FILE_EXT: Mapping[str, str] = {
    "pdf": "pdf",
    "svg": "svg",
    "dxf": "dxf",
    "step": "step",
    "ipc2581": "xml",
    "odb": "zip",
    "glb": "glb",
    "stats": "json",
}


def pos_file(stem: str, opts: Mapping[str, Any]) -> str:
    ext = {"csv": "csv", "gerber": "gbr"}.get(str(opts.get("format") or "ascii"), "pos")
    return f"{stem}.{ext}"


__all__ = [
    "CANONICAL_OF",
    "PROTEL_EXT",
    "SINGLE_FILE_EXT",
    "STANDARD_NAMES",
    "board_layer_names",
    "drill_files",
    "gerber_files",
    "layer_file_token",
    "normalise_layers",
    "pos_file",
    "protel_ext",
]
