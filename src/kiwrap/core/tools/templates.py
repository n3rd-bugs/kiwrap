"""Minimal, valid project files in the target KiCad's format (used by `kw_project create`).

What these templates encode (docs/VERIFICATION.md claim 9 and docs/research/07-versioning-strategy.md):
- a newer KiCad reads every older *token set*, while an older KiCad rejects a newer version
  stamp; kicad-cli 10 even accepts old-dialect content under its own stamp. So each empty file
  is the smallest token set every supported reader accepts, stamped with the epoch the target
  writes (`formats.sch` / `formats.pcb` from the capability record);
- `(generator_version "X.Y")` is rejected by readers that predate the token ("Unknown token",
  docs/VERIFICATION.md claim 9), so it is written only when the capability record says the reader
  has it (`formats.generator_version_token`); its value is the install's major.minor;
- `(embedded_fonts no)` is rejected by older readers and optional for the ones that know it, so
  it is never written;
- board layer ids were renumbered together with the 4-word `layerselection` (B.Cu 31 -> 2,
  docs/research/07-versioning-strategy.md). The dialect comes from `formats.pcb_layer_ids_v2`
  in the capability record (the matrix in kiwrap/data says which epoch flipped it);
- only `(version N)` and `(lib_symbols)` are hard requirements of the schematic reader
  (docs/research/03-schematic-authoring.md); `paper`, `uuid` and `sheet_instances` are
  written so KiCad does not have to invent them on first save;
- `.kicad_pro` is `<install>/share/kicad/template/kicad.kicad_pro` with `meta.filename` fixed
  (the template exists on every supported install), or a built-in copy of it;
- an optional rectangular outline is written as four legacy-form `gr_line` items
  (`(width 0.1)` + `(tstamp ...)`), the form every reader 7-10 accepts under its own stamp
  (checked with `pcb drc`: 0 violations on 8/9/10, `pcb export svg` on 7 - scratchpad
  core/outline_check.py). Without an outline every DRC reports `invalid_outline`.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

# Layer table dialects: (id, name, type) - the minimum a board needs: copper both sides + outline.
LAYERS_V1 = (
    (0, "F.Cu", "signal"),
    (31, "B.Cu", "signal"),
    (44, "Edge.Cuts", "user"),
)
LAYERS_V2 = (
    (0, "F.Cu", "signal"),
    (2, "B.Cu", "signal"),
    (25, "Edge.Cuts", "user"),
)

BUILTIN_PROJECT: dict[str, Any] = {
    "board": {
        "design_settings": {
            "defaults": {},
            "diff_pair_dimensions": [],
            "drc_exclusions": [],
            "rules": {},
            "track_widths": [],
            "via_dimensions": [],
        }
    },
    "boards": [],
    "libraries": {"pinned_footprint_libs": [], "pinned_symbol_libs": []},
    "meta": {"filename": "kicad.kicad_pro", "version": 1},
    "net_settings": {"classes": [], "meta": {"version": 0}},
    "pcbnew": {"page_layout_descr_file": ""},
    "sheets": [],
    "text_variables": {},
}


def new_uuid() -> str:
    return str(uuid.uuid4())


def project_text(name: str, template_dir: Path | None) -> str:
    """The `.kicad_pro` JSON: the install's own template when available, else the built-in copy."""
    data: dict[str, Any] = BUILTIN_PROJECT
    if template_dir is not None:
        candidate = template_dir / "kicad.kicad_pro"
        if candidate.is_file():
            try:
                data = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = BUILTIN_PROJECT
    data = json.loads(json.dumps(data))  # deep copy
    data.setdefault("meta", {})["filename"] = f"{name}.kicad_pro"
    return json.dumps(data, indent=2) + "\n"


def schematic_text(*, epoch: int, generator_version: str | None, sheet_uuid: str | None = None) -> str:
    gv = f'\t(generator_version "{generator_version}")\n' if generator_version else ""
    return (
        "(kicad_sch\n"
        f"\t(version {epoch})\n"
        "\t(generator eeschema)\n"
        f"{gv}"
        f"\t(uuid {sheet_uuid or new_uuid()})\n"
        '\t(paper "A4")\n'
        "\t(lib_symbols)\n"
        "\t(sheet_instances\n"
        '\t\t(path "/"\n'
        '\t\t\t(page "1")\n'
        "\t\t)\n"
        "\t)\n"
        ")\n"
    )


def _num(v: float) -> str:
    s = f"{v:.6f}".rstrip("0").rstrip(".")
    return s if s not in ("", "-0") else "0"


def outline_text(width_mm: float, height_mm: float, *, origin_mm: tuple[float, float] = (0.0, 0.0)) -> str:
    """Four `gr_line` segments on Edge.Cuts forming a `width x height` mm rectangle."""
    x0, y0 = origin_mm
    pts = [(x0, y0), (x0 + width_mm, y0), (x0 + width_mm, y0 + height_mm), (x0, y0 + height_mm)]
    out = ""
    for i in range(4):
        (x1, y1), (x2, y2) = pts[i], pts[(i + 1) % 4]
        out += (
            "\t(gr_line\n"
            f"\t\t(start {_num(x1)} {_num(y1)})\n"
            f"\t\t(end {_num(x2)} {_num(y2)})\n"
            '\t\t(layer "Edge.Cuts")\n'
            "\t\t(width 0.1)\n"
            f"\t\t(tstamp {new_uuid()})\n"
            "\t)\n"
        )
    return out


def board_text(
    *,
    epoch: int,
    generator_version: str | None,
    layer_ids_v2: bool,
    outline_mm: tuple[float, float] | None = None,
) -> str:
    gv = f'\t(generator_version "{generator_version}")\n' if generator_version else ""
    layers = "".join(
        f'\t\t({lid} "{name}" {kind})\n' for lid, name, kind in (LAYERS_V2 if layer_ids_v2 else LAYERS_V1)
    )
    outline = outline_text(*outline_mm) if outline_mm else ""
    return (
        "(kicad_pcb\n"
        f"\t(version {epoch})\n"
        "\t(generator pcbnew)\n"
        f"{gv}"
        "\t(general\n"
        "\t\t(thickness 1.6)\n"
        "\t)\n"
        '\t(paper "A4")\n'
        "\t(layers\n"
        f"{layers}"
        "\t)\n"
        "\t(setup\n"
        "\t\t(pad_to_mask_clearance 0)\n"
        "\t)\n"
        '\t(net 0 "")\n'
        f"{outline}"
        ")\n"
    )


def _flag(caps: Any, key: str) -> bool:
    """A boolean capability; a record without the key (nothing seeded it) counts as False, which
    selects the older dialect every newer reader still accepts (claim 9)."""
    try:
        return bool(caps[key])
    except (KeyError, TypeError):
        return False


def layer_ids_v2_for(caps: Any) -> bool:
    return _flag(caps, "formats.pcb_layer_ids_v2")


def generator_version_for(caps: Any, install: Any) -> str | None:
    return install.major_minor if _flag(caps, "formats.generator_version_token") else None
