"""The JLCPCB profile (docs/DECISIONS.md D9): capability rules presets for `kw_pcb_setup`,
kicad-cli export options and file names for the gerber / drill / position / BOM bundle, the
rotation-correction table for the CPL and the BOM columns JLCPCB's SMT order form reads.

Sources (all read 2026-09-20; re-check before a real order - the vendor changes them):
- Gerber / drill settings: jlcpcb.com/help/article/how-to-generate-gerber-and-drill-files-in-kicad-8
  (layers F/B.Cu + inner, F/B.Paste, F/B.SilkS, F/B.Mask, Edge.Cuts; "Use Protel filename
  extensions", "Subtract soldermask from silkscreen", "Check zone fills before plotting"; drill:
  Excellon, "Use alternate drill mode" for ovals, absolute origin, millimetres, decimal zeros,
  drill map "optional, but highly recommended"). X2 attributes are accepted (Bouni/kicad-jlcpcb-tools
  plots X2 + netlist attributes and JLCPCB reads them) so kicad-cli's default X2 output is kept.
  PTH and NPTH go to separate files (`--excellon-separate-th`), as that plugin does.
- Capabilities: jlcpcb.com/capabilities/pcb-capabilities (2-layer 1 oz: 0.10/0.10 mm trace/space,
  multilayer 0.09/0.09 mm; via hole 0.15 mm / diameter 0.25 mm minimum; PTH annular ring
  recommended >= 0.25 mm, absolute minimum 0.18 mm; copper to routed edge >= 0.2 mm; via
  hole-to-hole 0.2 mm, pad hole-to-hole 0.45 mm; mask bridge 0.10 mm; silk line >= 0.15 mm,
  text >= 1.0 mm). The presets below are SAFE defaults above those minimums (what the old
  5/5 mil, 0.3/0.6 via generation of designs used and JLCPCB still charges as standard), not the
  absolute limits: an agent that needs the limits changes `rules` explicitly.
- BOM / CPL columns: JLCPCB's SMT order form ("Comment, Designator, Footprint, LCSC Part #" and
  "Designator, Mid X, Mid Y, Layer, Rotation"; the same columns as Bouni/kicad-jlcpcb-tools and
  matthewlai/JLCKicadTools write, with Val/Package kept for review).
- Rotation corrections: `jlcpcb_rotations.json` next to this module (source note inside).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from kiwrap.core.fab.position import RotationTable
from kiwrap.core.fab.profiles import FabProfile, RulesPreset

ROTATIONS_PATH = Path(__file__).with_name("jlcpcb_rotations.json")

CAPABILITIES_SOURCE = "https://jlcpcb.com/capabilities/pcb-capabilities (read 2026-09-20)"

#: Non-copper layers JLCPCB wants in every gerber set (untranslated KiCad names, stable 7-11).
JLC_NON_COPPER_LAYERS: tuple[str, ...] = (
    "F.Paste",
    "B.Paste",
    "F.SilkS",
    "B.SilkS",
    "F.Mask",
    "B.Mask",
    "Edge.Cuts",
)

PRESET_2LAYER = RulesPreset(
    name="jlcpcb-2layer",
    layers=2,
    rules={
        # Default netclass (what new tracks / vias get)
        "clearance_mm": 0.15,
        "track_width_mm": 0.25,
        "via_size_mm": 0.6,
        "via_drill_mm": 0.3,
        # board minimums (DRC)
        "min_track_mm": 0.127,
        "min_via_mm": 0.5,
        "min_via_annular_mm": 0.13,
        "min_hole_mm": 0.2,
        "hole_to_hole_mm": 0.45,
        "hole_clearance_mm": 0.25,
        "copper_edge_clearance_mm": 0.3,
        "silk_clearance_mm": 0.15,
    },
    note=(
        "JLCPCB 2-layer 1 oz, safe defaults: 5/5 mil track/space minimum, 0.3/0.6 mm via, 0.2 mm min hole, "
        "0.3 mm copper-to-edge; the vendor's absolute minimums are tighter (0.10/0.10 mm, 0.15/0.25 mm via)"
    ),
    source=CAPABILITIES_SOURCE,
)

PRESET_4LAYER = RulesPreset(
    name="jlcpcb-4layer",
    layers=4,
    rules={
        "clearance_mm": 0.127,
        "track_width_mm": 0.2,
        "via_size_mm": 0.45,
        "via_drill_mm": 0.2,
        "min_track_mm": 0.09,
        "min_via_mm": 0.4,
        "min_via_annular_mm": 0.1,
        "min_hole_mm": 0.15,
        "hole_to_hole_mm": 0.45,
        "hole_clearance_mm": 0.2,
        "copper_edge_clearance_mm": 0.3,
        "silk_clearance_mm": 0.15,
    },
    note=(
        "JLCPCB 4-layer (JLC04161H-7628 stackup by default), safe defaults: 3.5/3.5 mil track/space minimum, "
        "0.2/0.45 mm via, 0.15 mm min hole, 0.3 mm copper-to-edge"
    ),
    source=CAPABILITIES_SOURCE,
)

PRESETS: Mapping[str, RulesPreset] = {PRESET_2LAYER.name: PRESET_2LAYER, PRESET_4LAYER.name: PRESET_4LAYER}


class JlcpcbProfile(FabProfile):
    name = "jlcpcb"
    description = (
        "JLCPCB gerber/drill bundle (Protel extensions, X2, separate PTH/NPTH, drill map), CPL with rotation "
        "corrections, BOM with 'LCSC Part #'; DNP parts left out of BOM and CPL."
    )
    exclude_dnp = True
    bom_columns = (
        ("Comment", "value"),
        ("Designator", "refs"),
        ("Footprint", "package"),
        ("LCSC Part #", "lcsc"),
        ("Qty", "qty"),
    )
    cpl_columns = ("Designator", "Val", "Package", "Mid X", "Mid Y", "Rotation", "Layer")
    cpl_side_names = {"top": "Top", "bottom": "Bottom"}
    plot_layers = JLC_NON_COPPER_LAYERS
    notes = (
        "upload <name>-jlcpcb-gerbers.zip as the gerber file, <name>-bom-jlcpcb.csv and <name>-cpl-jlcpcb.csv for SMT assembly",
        "check every rotation in JLCPCB's order preview: the corrections table is a community table, not the vendor's",
        "parts without an LCSC Part # cannot be assembled by JLCPCB (kw_fab_bom lists them in data.missing_lcsc)",
    )

    def __init__(self) -> None:
        self.rotation_table = RotationTable.load(ROTATIONS_PATH)

    def gerber_options(self, copper_layers: Sequence[str]) -> dict[str, Any]:
        copper = [str(n) for n in copper_layers] or ["F.Cu", "B.Cu"]
        return {
            "layers": [*copper, *self.plot_layers],
            "no_protel_ext": False,  # Protel extensions ARE wanted (the flag switches them off)
            "subtract_soldermask": True,
            "check_zones": True,
            "use_drill_file_origin": False,  # absolute: the same origin as the position file
        }

    def drill_options(self) -> dict[str, Any]:
        return {
            "format": "excellon",
            "drill_origin": "absolute",
            "excellon_units": "mm",
            "excellon_zeros_format": "decimal",
            "excellon_oval_format": "alternate",
            "excellon_separate_th": True,
            "generate_map": True,
            "map_format": "gerberx2",
        }

    def pos_options(self) -> dict[str, Any]:
        return {
            "format": "csv",
            "units": "mm",
            "side": "both",
            "use_drill_file_origin": False,
            "exclude_dnp": True,
            "smd_only": False,
        }

    def file_name(self, target: str, project_name: str) -> str | None:
        if target == "pos":
            return f"{project_name}-cpl-jlcpcb.csv"
        if target == "bom":
            return f"{project_name}-bom-jlcpcb.csv"
        return None

    def bundle_name(self, project_name: str) -> str | None:
        return f"{project_name}-jlcpcb-gerbers.zip"

    def describe(self) -> dict[str, Any]:
        d = super().describe()
        d["presets"] = {k: v.to_dict() for k, v in PRESETS.items()}
        return d
