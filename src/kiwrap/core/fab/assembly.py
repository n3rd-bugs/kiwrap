"""Which footprints a position (CPL) file holds, why the others are missing, and how the CPL
compares with the schematic BOM (review item 4: `kw_fab_export(target='pos', dry_run=true)` returns
the excluded refs with a reason and a BOM diff; triage: `positions.excluded` on real runs too).

KiCad's rules, verified against `kicad-cli pcb export pos` 10.0.6 and 10.99 on the
pic_programmer / kit-dev-coldfire demos (scratchpad spike_pos.py; locked in by
tests/test_fab_reviewer_live.py):

- a footprint with the `exclude_from_pos_files` attribute is never written;
- `--smd-only` keeps only footprints whose type attribute is `smd`: a footprint typed neither
  smd nor through_hole ("unspecified") is dropped too (reason `attr_unspecified`, the doc's
  "30 unspecified-type SMD parts");
- `--exclude-fp-th` drops every footprint with a drilled pad: plated OR non-plated (pic_programmer's
  six NPTH-only mounting holes went with it);
- `--exclude-dnp` drops `dnp` footprints; `--side front|back` the other side.

Review §5 item 5: the jlcpcb profile does NOT pass `--smd-only`, so unspecified parts stay in its
CPL; they are reported as `flags` (an assembler's own SMD filter may still drop them), not as
exclusions. A schematic/board disagreement (DNP not synced, board-only parts, `in_bom no`) shows up
in `bom_diff`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

EXCLUDE_FROM_POS = "exclude_from_pos"
ATTR_UNSPECIFIED = "attr_unspecified"
THT = "tht"
DNP = "dnp"
SIDE = "side"
BOARD_ONLY = "board_only"

REASON_TEXT: Mapping[str, str] = {
    EXCLUDE_FROM_POS: "footprint attribute 'exclude from position files'",
    ATTR_UNSPECIFIED: "footprint type is neither SMD nor through-hole (unspecified); smd_only drops it",
    THT: "through-hole footprint (drilled pads) and the export drops through-hole parts",
    DNP: "DNP (do not populate) and the export drops DNP parts",
    SIDE: "on the side the export leaves out",
    BOARD_ONLY: "board-only footprint: no schematic symbol, so it is in no BOM",
}


@dataclass(frozen=True)
class PosOptions:
    """The `pcb export pos` switches that decide membership (the profile's options after the
    capability filter)."""

    smd_only: bool = False
    exclude_fp_th: bool = False
    exclude_dnp: bool = False
    side: str = "both"  # front | back | both

    @classmethod
    def from_options(cls, opts: Mapping[str, Any], *, extra_dnp: bool = False) -> PosOptions:
        """From `export_pcb(target='pos', **opts)` keywords; `extra_dnp` = kiwrap drops DNP rows
        after the export (the flag was missing on this KiCad, or the caller asked)."""
        return cls(
            smd_only=bool(opts.get("smd_only")),
            exclude_fp_th=bool(opts.get("exclude_fp_th")),
            exclude_dnp=bool(opts.get("exclude_dnp")) or extra_dnp,
            side=str(opts.get("side") or "both"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "smd_only": self.smd_only,
            "exclude_fp_th": self.exclude_fp_th,
            "exclude_dnp": self.exclude_dnp,
            "side": self.side,
        }


@dataclass(frozen=True)
class RefNote:
    ref: str
    reason: str
    reasons: tuple[str, ...] = ()
    attributes: tuple[str, ...] = ()
    side: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "reason": self.reason,
            "reasons": list(self.reasons or (self.reason,)),
            "why": REASON_TEXT.get(self.reason, self.reason),
            "attributes": list(self.attributes),
            "side": self.side,
        }


@dataclass
class PositionPlan:
    included: list[str] = field(default_factory=list)
    excluded: list[RefNote] = field(default_factory=list)
    flags: list[RefNote] = field(default_factory=list)
    options: PosOptions = field(default_factory=PosOptions)

    def reason_of(self, ref: str) -> str | None:
        return next((e.reason for e in self.excluded if e.ref == ref), None)

    def to_dict(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for e in self.excluded:
            counts[e.reason] = counts.get(e.reason, 0) + 1
        return {
            "rows": len(self.included),
            "included": list(self.included),
            "excluded": [e.to_dict() for e in self.excluded],
            "excluded_by_reason": counts,
            "flags": [f.to_dict() for f in self.flags],
            "options": self.options.to_dict(),
        }


def _side(fp: Any) -> str:
    layer = str(getattr(fp, "layer", "F.Cu"))
    return "back" if layer.startswith("B.") else "front"


def _has_drilled_pads(fp: Any) -> bool:
    return any(str(getattr(p, "type", "")) in ("thru_hole", "np_thru_hole") for p in getattr(fp, "pads", ()))


def exclusion_reasons(fp: Any, opts: PosOptions) -> list[str]:
    """Every rule of KiCad's position exporter that drops `fp` under `opts` (in KiCad's order)."""
    attrs = set(getattr(fp, "attributes", ()) or ())
    reasons: list[str] = []
    side = _side(fp)
    if opts.side in ("front", "back") and side != opts.side:
        reasons.append(SIDE)
    if "exclude_from_pos_files" in attrs:
        reasons.append(EXCLUDE_FROM_POS)
    if opts.smd_only and "smd" not in attrs:
        reasons.append(THT if ("through_hole" in attrs or _has_drilled_pads(fp)) else ATTR_UNSPECIFIED)
    if opts.exclude_fp_th and _has_drilled_pads(fp) and THT not in reasons:
        reasons.append(THT)
    if opts.exclude_dnp and ("dnp" in attrs or bool(getattr(fp, "dnp", False))):
        reasons.append(DNP)
    return reasons


def plan_positions(footprints: Iterable[Any], opts: PosOptions) -> PositionPlan:
    """What `pcb export pos` with `opts` writes for these board footprints, and why the rest is
    left out. Included parts typed "unspecified" or marked board-only are listed in `flags`."""
    plan = PositionPlan(options=opts)
    for fp in footprints:
        ref = str(getattr(fp, "reference", "") or "")
        attrs = tuple(getattr(fp, "attributes", ()) or ())
        reasons = exclusion_reasons(fp, opts)
        if reasons:
            plan.excluded.append(RefNote(ref, reasons[0], tuple(reasons), attrs, _side(fp)))
            continue
        plan.included.append(ref)
        if "smd" not in attrs and "through_hole" not in attrs:
            plan.flags.append(RefNote(ref, ATTR_UNSPECIFIED, (ATTR_UNSPECIFIED,), attrs, _side(fp)))
        if "board_only" in attrs:
            plan.flags.append(RefNote(ref, BOARD_ONLY, (BOARD_ONLY,), attrs, _side(fp)))
    return plan


@dataclass(frozen=True)
class SchPart:
    """One schematic reference as the BOM sees it (all units of a symbol share it)."""

    ref: str
    in_bom: bool = True
    on_board: bool = True
    dnp: bool = False

    @property
    def fitted(self) -> bool:
        return self.in_bom and not self.dnp


def schematic_parts(hierarchy: Any) -> dict[str, SchPart]:
    """`{ref: SchPart}` from `kiwrap.formats.schematic.Hierarchy` (power symbols and `#` refs
    skipped). A ref placed several times (units) is fitted when any placement is."""
    out: dict[str, SchPart] = {}
    for inst, sym in hierarchy.symbols():
        if getattr(sym, "is_power", False):
            continue
        path = getattr(inst, "path", "")
        ref_for = getattr(sym, "reference_for", None)
        ref = str(ref_for(path) if callable(ref_for) else getattr(sym, "reference", ""))
        if not ref or ref.startswith("#"):
            continue
        part = SchPart(
            ref=ref,
            in_bom=bool(getattr(sym, "in_bom", True)),
            on_board=bool(getattr(sym, "on_board", True)),
            dnp=bool(getattr(sym, "dnp", False)),
        )
        prev = out.get(ref)
        if prev is None or (part.fitted and not prev.fitted):
            out[ref] = part
    return out


def bom_diff(
    cpl_refs: Iterable[str],
    parts: Mapping[str, SchPart],
    footprints: Iterable[Any],
    plan: PositionPlan | None = None,
) -> dict[str, Any]:
    """The CPL against the schematic BOM (fitted = `in_bom` and not DNP).

    `missing_from_cpl`: fitted BOM parts the CPL lacks, with the reason (the exporter's exclusion,
    `not_on_board` when no footprint carries the ref, `sch_not_on_board` for `on_board no`).
    `extra_in_cpl`: CPL rows the BOM does not fit (`board_only` / `not_in_schematic`, `sch_dnp`
    = the schematic says DNP but the board does not yet (run kw_pcb_sync), `sch_not_in_bom`)."""
    cpl = list(dict.fromkeys(cpl_refs))
    in_cpl = set(cpl)
    fps = {str(getattr(fp, "reference", "")): fp for fp in footprints}
    missing: list[dict[str, Any]] = []
    for ref in sorted(parts, key=_ref_key):
        part = parts[ref]
        if not part.fitted or ref in in_cpl:
            continue
        if ref not in fps:
            reason = "sch_not_on_board" if not part.on_board else "not_on_board"
        else:
            reason = (plan.reason_of(ref) if plan is not None else None) or "unknown"
        missing.append(
            {"ref": ref, "reason": reason, "why": REASON_TEXT.get(reason, _WHY.get(reason, reason))}
        )
    extra: list[dict[str, Any]] = []
    for ref in sorted(in_cpl, key=_ref_key):
        sch = parts.get(ref)
        if sch is not None and sch.fitted:
            continue
        if sch is None:
            attrs = set(getattr(fps.get(ref), "attributes", ()) or ())
            reason = BOARD_ONLY if "board_only" in attrs else "not_in_schematic"
        elif sch.dnp:
            reason = "sch_dnp"
        else:
            reason = "sch_not_in_bom"
        extra.append({"ref": ref, "reason": reason, "why": REASON_TEXT.get(reason, _WHY.get(reason, reason))})
    return {
        "bom_fitted": sum(1 for p in parts.values() if p.fitted),
        "cpl_rows": len(cpl),
        "missing_from_cpl": missing,
        "extra_in_cpl": extra,
        "match": not missing and not extra,
    }


_WHY: Mapping[str, str] = {
    "not_on_board": "the schematic part has no footprint on the board (run kw_pcb_sync)",
    "sch_not_on_board": "the symbol is marked 'exclude from board'",
    "not_in_schematic": "the footprint has no schematic symbol and is not marked board-only",
    "sch_dnp": "the schematic marks it DNP but the board footprint is not DNP yet (run kw_pcb_sync)",
    "sch_not_in_bom": "the symbol is marked 'exclude from BOM'",
    "unknown": "KiCad left it out for a reason kiwrap does not model",
}


def _ref_key(ref: str) -> tuple[str, int, str]:
    i = len(ref)
    while i > 0 and ref[i - 1].isdigit():
        i -= 1
    num = ref[i:]
    return (ref[:i], int(num) if num else -1, ref)


__all__ = [
    "ATTR_UNSPECIFIED",
    "BOARD_ONLY",
    "DNP",
    "EXCLUDE_FROM_POS",
    "REASON_TEXT",
    "SIDE",
    "THT",
    "PosOptions",
    "PositionPlan",
    "RefNote",
    "SchPart",
    "bom_diff",
    "exclusion_reasons",
    "plan_positions",
    "schematic_parts",
]
