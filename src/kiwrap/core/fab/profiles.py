"""Fabrication profiles: what a vendor expects from the bundle, in one object per vendor.

`FabProfile` is the generic profile (KiCad's defaults, no post-processing); `jlcpcb.py`
subclasses it. `kw_fab_export` asks the profile for the kicad-cli options of every target
(`gerber_options` / `drill_options` / `pos_options` / `bom_options`), for the output file names
(`file_name`) and for the post-processing of the position file and the BOM
(`process_positions` / `bom_columns`). Options are filtered against the target KiCad's `--help`
(`caps.flags(subcommand)`, probed live by `kiwrap.capability`) so a flag a version lacks is
dropped and reported instead of failing the export (`backends.cli.export` raises `Unsupported`
for an unlisted flag; a profile must degrade, not refuse).

Rules presets (`preset_rules`) are the capability rules a vendor publishes, as the
`kw_pcb_setup(rules=..., layers=...)` dict, so an agent can start a board with
`kw_pcb_setup(preset="jlcpcb-2layer")`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from kiwrap.core.fab.bom import LCSC_FIELD_CANDIDATES, BomRow, BomTable, bom_csv, cli_bom_options
from kiwrap.core.fab.position import CorrectedPositions, RotationTable, correct_positions, cpl_csv
from kiwrap.formats.reports.position import PositionFile

#: The kicad-cli subcommand each fab target runs (for the flag filter).
TARGET_SUBCOMMANDS: Mapping[str, str] = {
    "gerbers": "pcb export gerbers",
    "drill": "pcb export drill",
    "pos": "pcb export pos",
    "bom": "sch export bom",
}

#: `export_pcb` / `export_sch` keyword -> kicad-cli flag (the subset a profile may set; mirrors
#: `backends.cli.export._PCB_OPTS` / `_SCH_OPTS`, which is the source of truth at call time).
OPTION_FLAGS: Mapping[str, str] = {
    "layers": "--layers",
    "no_x2": "--no-x2",
    "no_netlist": "--no-netlist",
    "subtract_soldermask": "--subtract-soldermask",
    "use_drill_file_origin": "--use-drill-file-origin",
    "precision": "--precision",
    "no_protel_ext": "--no-protel-ext",
    "check_zones": "--check-zones",
    "disable_aperture_macros": "--disable-aperture-macros",
    "format": "--format",
    "drill_origin": "--drill-origin",
    "excellon_zeros_format": "--excellon-zeros-format",
    "excellon_oval_format": "--excellon-oval-format",
    "excellon_units": "--excellon-units",
    "excellon_separate_th": "--excellon-separate-th",
    "excellon_mirror_y": "--excellon-mirror-y",
    "generate_map": "--generate-map",
    "map_format": "--map-format",
    "side": "--side",
    "units": "--units",
    "bottom_negate_x": "--bottom-negate-x",
    "smd_only": "--smd-only",
    "exclude_fp_th": "--exclude-fp-th",
    "exclude_dnp": "--exclude-dnp",
    "fields": "--fields",
    "labels": "--labels",
    "group_by": "--group-by",
    "ref_delimiter": "--ref-delimiter",
    "ref_range_delimiter": "--ref-range-delimiter",
    "sort_field": "--sort-field",
}


@dataclass(frozen=True)
class RulesPreset:
    name: str
    layers: int
    rules: dict[str, float]  # kw_pcb_setup `rules` keys (…_mm)
    netclasses: dict[str, dict[str, Any]] = field(default_factory=dict)
    note: str = ""
    source: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "layers": self.layers,
            "rules": dict(self.rules),
            "netclasses": dict(self.netclasses),
            "note": self.note,
            "source": self.source,
        }


@dataclass
class FilteredOptions:
    """A profile's options after the capability filter."""

    options: dict[str, Any]
    dropped: list[str]  # "no_x2 (--no-x2)" - flags this KiCad's --help does not list


class FabProfile:
    """The generic profile: kicad-cli defaults, no vendor post-processing."""

    name = "generic"
    description = "KiCad's own defaults for every target; no renaming, no rotation corrections."
    #: symbol fields that hold the vendor part number (BOM column `lcsc`)
    lcsc_fields: tuple[str, ...] = LCSC_FIELD_CANDIDATES
    #: whether DNP parts are dropped from the vendor BOM / CPL
    exclude_dnp: bool = False
    #: (label, key) columns of the profile's BOM CSV (keys from `bom.COLUMN_GETTERS`)
    bom_columns: tuple[tuple[str, str], ...] = (
        ("Reference", "refs"),
        ("Qty", "qty"),
        ("Value", "value"),
        ("Footprint", "footprint"),
        ("LCSC Part #", "lcsc"),
        ("DNP", "dnp"),
        ("MPN", "mpn"),
        ("Manufacturer", "manufacturer"),
    )
    #: header labels of the profile's CPL CSV, for (ref, value, package, x, y, rotation, side)
    cpl_columns: tuple[str, ...] = ("Ref", "Val", "Package", "PosX", "PosY", "Rot", "Side")
    cpl_side_names: Mapping[str, str] = {"top": "top", "bottom": "bottom"}
    rotation_table: RotationTable | None = None
    #: extra copper layers are always plotted; these non-copper layers complete the set
    plot_layers: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    # ---- kicad-cli options per target -----------------------------------------------------------------

    def gerber_options(self, copper_layers: Sequence[str]) -> dict[str, Any]:
        return {}

    def drill_options(self) -> dict[str, Any]:
        return {}

    def pos_options(self) -> dict[str, Any]:
        return {"format": "csv", "units": "mm"}

    def bom_options(self, extra_fields: Sequence[str] = ()) -> dict[str, Any]:
        return cli_bom_options(lcsc_fields=self.lcsc_fields, extra_fields=extra_fields)

    def options_for(self, target: str, caps: Any, **kw: Any) -> FilteredOptions:
        """The target's options with every flag the KiCad's --help does not list removed."""
        if target == "gerbers":
            opts = self.gerber_options(kw.get("copper_layers") or ("F.Cu", "B.Cu"))
        elif target == "drill":
            opts = self.drill_options()
        elif target == "pos":
            opts = self.pos_options()
        elif target == "bom":
            opts = self.bom_options(kw.get("extra_fields") or ())
        else:
            opts = {}
        return filter_options(opts, caps, TARGET_SUBCOMMANDS.get(target, ""))

    # ---- file naming ---------------------------------------------------------------------------------------

    def file_name(self, target: str, project_name: str) -> str | None:
        """The single-file name of `target` (pos / bom); None = KiCad's default (`<stem>.csv`)."""
        return None

    def bundle_name(self, project_name: str) -> str | None:
        """Name of a zip of gerbers + drill files, or None when the profile makes no bundle."""
        return None

    # ---- post-processing -------------------------------------------------------------------------------------

    def process_positions(self, pos: PositionFile, *, skip_refs: Iterable[str] = ()) -> CorrectedPositions:
        return correct_positions(pos, self.rotation_table, skip_refs=skip_refs)

    def cpl_csv(self, corrected: CorrectedPositions) -> str:
        return cpl_csv(corrected.rows, columns=self.cpl_columns, side_names=dict(self.cpl_side_names))

    def bom_rows(self, table: BomTable, *, include_dnp: bool | None = None) -> list[BomRow]:
        keep_dnp = (not self.exclude_dnp) if include_dnp is None else include_dnp
        return list(table.rows) if keep_dnp else table.fitted

    def bom_csv(
        self, table: BomTable, *, include_dnp: bool | None = None, extra_fields: Sequence[str] = ()
    ) -> str:
        return bom_csv(
            self.bom_rows(table, include_dnp=include_dnp), columns=self.bom_columns, extra_fields=extra_fields
        )

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "lcsc_fields": list(self.lcsc_fields),
            "exclude_dnp": self.exclude_dnp,
            "bom_columns": [label for label, _ in self.bom_columns],
            "cpl_columns": list(self.cpl_columns),
            "rotation_rules": len(self.rotation_table.rules) if self.rotation_table else 0,
            "rotation_source": self.rotation_table.source if self.rotation_table else "",
            "notes": list(self.notes),
        }


def filter_options(opts: Mapping[str, Any], caps: Any, subcommand: str) -> FilteredOptions:
    """Drop options whose flag is absent from `caps.flags(subcommand)` (an empty / missing list
    means "unknown": keep everything, `backends.cli.export` decides)."""
    listed: list[str] = []
    try:
        listed = list(caps.flags(subcommand)) if caps is not None and subcommand else []
    except Exception:  # noqa: BLE001 - a fake / partial record keeps every option
        listed = []
    kept: dict[str, Any] = {}
    dropped: list[str] = []
    for name, value in opts.items():
        flag = OPTION_FLAGS.get(name)
        if listed and flag and flag not in listed:
            if value not in (False, None):  # a switch left off needs no flag: nothing was lost
                dropped.append(f"{name} ({flag})")
            continue
        kept[name] = value
    return FilteredOptions(options=kept, dropped=dropped)


# ---- registry ----------------------------------------------------------------------------------------------------


def _profiles() -> dict[str, FabProfile]:
    from kiwrap.core.fab.jlcpcb import JlcpcbProfile

    return {"generic": FabProfile(), "jlcpcb": JlcpcbProfile()}


PROFILE_NAMES: tuple[str, ...] = ("generic", "jlcpcb")


def get_profile(name: str) -> FabProfile:
    """The profile by name; ValueError (-> E_BAD_ARGS at the tool layer) for an unknown one."""
    key = (name or "generic").strip().lower()
    table = _profiles()
    if key not in table:
        raise ValueError(f"unknown fabrication profile {name!r}; known: {sorted(table)}")
    return table[key]


def _presets() -> dict[str, RulesPreset]:
    from kiwrap.core.fab.jlcpcb import PRESETS

    return dict(PRESETS)


PRESET_NAMES: tuple[str, ...] = ("jlcpcb-2layer", "jlcpcb-4layer")


def preset_rules(name: str) -> RulesPreset:
    """The rules preset by name (`jlcpcb-2layer`, `jlcpcb-4layer`); ValueError for an unknown one."""
    key = (name or "").strip().lower()
    table = _presets()
    if key not in table:
        raise ValueError(f"unknown rules preset {name!r}; known: {sorted(table)}")
    return table[key]
