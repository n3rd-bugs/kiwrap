"""Bill of materials: rows grouped by (value, footprint, vendor part number, DNP) from either
kicad-cli's own `sch export bom` CSV (KiCad 8+, `--fields` / `--group-by`, the preferred source:
KiCad resolves variants, text variables and per-instance fields itself) or, where that
subcommand does not exist, from the kicadsexpr netlist kicad-cli exports on every version
(`kiwrap.formats.reports.Netlist`: `Component.properties` carries every symbol field,
`Component.dnp` the DNP flag - docs/VERIFICATION.md claim on the netlist parser).

The two sources produce the same `BomTable`, so a profile's CSV (`bom_csv`) and `kw_fab_bom`'s
rows do not depend on the KiCad version. Cost and stock are hooks (`None`) for the parts
component (`kw_parts_*`, jlcparts / EasyEDA - docs/ARCHITECTURE.md §10) to fill in later.

DNP handling: a DNP symbol is grouped separately (never merged with its fitted twins), carries
`dnp=True`, and is dropped from a vendor BOM/CPL unless the caller asks for it; symbols excluded
from the BOM (`in_bom no`) are dropped by KiCad's exporter in the CLI path and by
`exclude_refs` in the netlist path (the netlist does not carry the flag on every version).
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from kiwrap.formats.reports import Netlist

#: Symbol field names that hold a vendor (LCSC / JLCPCB) part number, most common first.
LCSC_FIELD_CANDIDATES: tuple[str, ...] = (
    "LCSC Part #",
    "LCSC",
    "LCSC Part",
    "LCSC#",
    "LCSC Part Number",
    "JLCPCB Part #",
    "JLCPCB",
    "JLC",
)
MPN_FIELD_CANDIDATES: tuple[str, ...] = (
    "MPN",
    "Manufacturer Part Number",
    "Mfr. Part #",
    "Part Number",
    "PN",
)
MANUFACTURER_FIELD_CANDIDATES: tuple[str, ...] = ("Manufacturer", "Mfr", "Mfr.", "MFG")

_REF_RE = re.compile(r"^([A-Za-z#_]+)(\d+)?(.*)$")


def ref_sort_key(ref: str) -> tuple[str, int, str]:
    m = _REF_RE.match(ref or "")
    if not m:
        return (ref, 0, "")
    return (m.group(1), int(m.group(2) or 0), m.group(3) or "")


@dataclass
class BomRow:
    refs: list[str]
    value: str
    footprint: str  # `Lib:Name` as the symbol carries it ("" when unassigned)
    lcsc: str | None = None
    dnp: bool = False
    mpn: str | None = None
    manufacturer: str | None = None
    fields: dict[str, str] = field(default_factory=dict)  # the extra fields the caller asked for
    cost: float | None = None  # hook: unit price from the parts component
    stock: int | None = None  # hook: vendor stock from the parts component

    @property
    def qty(self) -> int:
        return len(self.refs)

    @property
    def package(self) -> str:
        """The footprint name without its library nickname (what an assembly house reads)."""
        return self.footprint.split(":", 1)[1] if ":" in self.footprint else self.footprint

    def to_dict(self) -> dict[str, Any]:
        return {
            "refs": list(self.refs),
            "qty": self.qty,
            "value": self.value,
            "footprint": self.footprint,
            "package": self.package,
            "lcsc": self.lcsc,
            "dnp": self.dnp,
            "mpn": self.mpn,
            "manufacturer": self.manufacturer,
            "fields": dict(self.fields),
            "cost": self.cost,
            "stock": self.stock,
        }


@dataclass
class BomTable:
    rows: list[BomRow]
    source: str  # "cli" | "netlist"
    kicad_version: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def fitted(self) -> list[BomRow]:
        return [r for r in self.rows if not r.dnp]

    @property
    def dnp(self) -> list[BomRow]:
        return [r for r in self.rows if r.dnp]

    def missing_lcsc(self) -> list[str]:
        return [ref for r in self.fitted if not r.lcsc for ref in r.refs]

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "kicad_version": self.kicad_version,
            "rows": [r.to_dict() for r in self.rows],
            "groups": len(self.rows),
            "components": sum(r.qty for r in self.rows),
            "fitted_components": sum(r.qty for r in self.fitted),
            "dnp_refs": [ref for r in self.dnp for ref in r.refs],
            "missing_lcsc": self.missing_lcsc(),
            "warnings": list(self.warnings),
        }


def _first(props: Mapping[str, str], names: Iterable[str]) -> str | None:
    lower = {k.lower(): v for k, v in props.items()}
    for n in names:
        v = lower.get(n.lower())
        if v is not None and v.strip():
            return v.strip()
    return None


def _group(
    parts: Iterable[tuple[str, str, str, bool, Mapping[str, str]]],
    *,
    lcsc_fields: Sequence[str],
    extra_fields: Sequence[str],
) -> list[BomRow]:
    """(ref, value, footprint, dnp, properties) -> rows keyed by (value, footprint, lcsc, dnp)."""
    groups: dict[tuple[str, str, str, bool], BomRow] = {}
    for ref, value, footprint, dnp, props in parts:
        lcsc = _first(props, lcsc_fields)
        key = (value, footprint, lcsc or "", dnp)
        row = groups.get(key)
        if row is None:
            row = BomRow(
                refs=[],
                value=value,
                footprint=footprint,
                lcsc=lcsc,
                dnp=dnp,
                mpn=_first(props, MPN_FIELD_CANDIDATES),
                manufacturer=_first(props, MANUFACTURER_FIELD_CANDIDATES),
                fields={f: (_first(props, [f]) or "") for f in extra_fields},
            )
            groups[key] = row
        row.refs.append(ref)
    rows = list(groups.values())
    for r in rows:
        r.refs.sort(key=ref_sort_key)
    rows.sort(key=lambda r: (r.dnp, ref_sort_key(r.refs[0]) if r.refs else ("", 0, "")))
    return rows


def _is_virtual(ref: str) -> bool:
    """Power symbols / flags (`#PWR01`, `#FLG01`) never appear in a BOM."""
    return ref.startswith("#")


def bom_from_netlist(
    nl: Netlist,
    *,
    lcsc_fields: Sequence[str] = LCSC_FIELD_CANDIDATES,
    extra_fields: Sequence[str] = (),
    exclude_refs: Iterable[str] = (),
) -> BomTable:
    """Group the netlist's components. `exclude_refs` = symbols the schematic marks `in_bom no`
    (the caller reads them from the schematic files when it can; a KiCad netlist keeps such
    symbols in `components`)."""
    skip = set(exclude_refs)
    parts = [
        (c.ref, c.value, c.footprint or "", bool(c.dnp), c.properties)
        for c in nl.components
        if c.ref and not _is_virtual(c.ref) and c.ref not in skip
    ]
    return BomTable(
        rows=_group(parts, lcsc_fields=lcsc_fields, extra_fields=extra_fields),
        source="netlist",
        kicad_version=nl.kicad_version,
    )


#: kicad-cli's generated fields (`--fields`, with or without `${}`; sch export bom --help).
CLI_REF, CLI_VALUE, CLI_FOOTPRINT, CLI_DNP, CLI_QTY = (
    "Reference",
    "Value",
    "Footprint",
    "${DNP}",
    "${QUANTITY}",
)


def cli_bom_options(
    *, lcsc_fields: Sequence[str] = LCSC_FIELD_CANDIDATES, extra_fields: Sequence[str] = ()
) -> dict[str, Any]:
    """Keyword options for `backends.cli.export.export_sch(..., "bom", ...)` that make kicad-cli
    write one row per (Value, Footprint, vendor part, DNP) group with the references listed
    individually (no `R1-R3` ranges) - the shape `bom_from_cli_csv` parses. DNP rows are kept
    (flagged by the DNP column) so the caller decides."""
    fields = [CLI_REF, CLI_VALUE, CLI_FOOTPRINT, CLI_DNP, CLI_QTY, *lcsc_fields]
    for f in (*MPN_FIELD_CANDIDATES, *MANUFACTURER_FIELD_CANDIDATES, *extra_fields):
        if f not in fields:
            fields.append(f)
    labels = [f.strip("${}") for f in fields]
    group_by = [CLI_VALUE, CLI_FOOTPRINT, CLI_DNP, *lcsc_fields]
    return {
        "fields": fields,
        "labels": labels,
        "group_by": group_by,
        "ref_range_delimiter": "",
        "ref_delimiter": ",",
        "exclude_dnp": False,
    }


def bom_from_cli_csv(
    text: str,
    *,
    lcsc_fields: Sequence[str] = LCSC_FIELD_CANDIDATES,
    extra_fields: Sequence[str] = (),
    kicad_version: str = "",
) -> BomTable:
    """Parse the CSV `sch export bom` wrote with `cli_bom_options()` (labels = field names)."""
    reader = csv.reader(io.StringIO(text.lstrip("﻿")))
    header: list[str] = []
    parts: list[tuple[str, str, str, bool, dict[str, str]]] = []
    warnings: list[str] = []
    for cells in reader:
        if not cells or not any(c.strip() for c in cells):
            continue
        if not header:
            header = [c.strip() for c in cells]
            continue
        rec = {h: (cells[i].strip() if i < len(cells) else "") for i, h in enumerate(header)}
        refs = [r.strip() for r in rec.get("Reference", "").split(",") if r.strip()]
        if not refs:
            warnings.append(f"row without references skipped: {cells[:3]}")
            continue
        dnp = bool(rec.get("DNP", "").strip())
        props = {
            k: v for k, v in rec.items() if k not in ("Reference", "Value", "Footprint", "DNP", "QUANTITY")
        }
        for ref in refs:
            if _is_virtual(ref):
                continue
            parts.append((ref, rec.get("Value", ""), rec.get("Footprint", ""), dnp, props))
    return BomTable(
        rows=_group(parts, lcsc_fields=lcsc_fields, extra_fields=extra_fields),
        source="cli",
        kicad_version=kicad_version,
        warnings=warnings,
    )


#: Column -> how to render it from a row (a profile picks and labels them).
COLUMN_GETTERS: dict[str, Any] = {
    "refs": lambda r: ",".join(r.refs),
    "qty": lambda r: str(r.qty),
    "value": lambda r: r.value,
    "footprint": lambda r: r.footprint,
    "package": lambda r: r.package,
    "lcsc": lambda r: r.lcsc or "",
    "dnp": lambda r: "DNP" if r.dnp else "",
    "mpn": lambda r: r.mpn or "",
    "manufacturer": lambda r: r.manufacturer or "",
}


def bom_csv(
    rows: Sequence[BomRow],
    *,
    columns: Sequence[tuple[str, str]],
    extra_fields: Sequence[str] = (),
) -> str:
    """Render rows as CSV. `columns` = [(label, key)] with keys from COLUMN_GETTERS; every name in
    `extra_fields` is appended as its own column from `row.fields`."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow([label for label, _ in columns] + list(extra_fields))
    for r in rows:
        w.writerow(
            [COLUMN_GETTERS[key](r) for _, key in columns] + [r.fields.get(f, "") for f in extra_fields]
        )
    return buf.getvalue()
