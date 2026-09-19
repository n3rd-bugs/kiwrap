"""Per-epoch token dialects of the schematic (`.kicad_sch`) and symbol (`.kicad_sym`) formats.

A *dialect* is the set of spelling rules KiCad's writer applied at a given format stamp. The
schematic writer (`kiwrap.formats.schematic_writer`) asks `dialect_for(epoch)` how to spell a
property, a hidden pin, a power flag or the document header for the stamps it was given, and
`convert_lib_symbol` rewrites a library symbol copied from a `.kicad_sym` of one stamp so that a
schematic of another stamp parses it (older parsers reject unknown tokens; newer ones accept the
old spellings, so only the downgrade direction rewrites anything).

Everything below was read off files KiCad itself wrote (`<install>/share/kicad/demos`, the
shipped `.kicad_sym` libraries, and `kicad-cli sch|sym upgrade --force` re-saves of the spike
schematics; spikes/schematic-authoring/gen_dialect.py + dialects/matrix.txt hold the cross-version
load matrix: each dialect loads cleanly on its own kicad-cli, newer kicad-clis load older dialects,
older ones refuse newer stamps with "Failed to load schematic"):

* 7-era (sch 20230121 / sym 20220914): `(generator eeschema)` bare, no `generator_version`, bare
  `(uuid xxxx)`, hidden things spelled as a bare `hide` inside `(effects …)` / after `(length …)`,
  `(pin_numbers hide)`, `(power)`, no `exclude_from_sim`, no `embedded_fonts`.
* 8-era (20231120 / 20231120): quoted generator + `generator_version`, quoted uuids,
  `(exclude_from_sim no)` on symbols and library symbols, property hide as `(hide yes)` INSIDE
  `(effects …)`; pins / `pin_numbers` / `pin_names` still use the bare `hide`.
* 9-era (20250114 / 20241209): `(hide yes)` lists on pins, `(pin_numbers (hide yes))`,
  `(pin_names (offset x) (hide yes))`, `(embedded_fonts no)` at the document root and inside every
  library symbol, `(power)` unchanged; property hide still inside `(effects …)`; sheet elements
  gain `(exclude_from_sim no) (in_bom yes) (on_board yes) (dnp no)` (kicad-cli 8.0.9 refuses a
  sheet carrying them: "Failed to load schematic file").
* 10-era (20260306 / 20251024; 20260101 is the early 10.0 stamp): property-level `(hide yes)`
  `(show_name no)` `(do_not_autoplace no)` (in a placed symbol KiCad writes hide first, in a
  library symbol show_name / do_not_autoplace first), `(body_style 1)`, `(in_pos_files yes)`,
  `(duplicate_pin_numbers_are_jumpers no)`, `(power global|local)`, Datasheet defaults to "".
* 11-preview (20260830 / 20260830, the 10.99 nightly): identical to the 10-era dialect except
  that the writer emits the whole unit x body-style matrix of `(symbol "<Name>_U_S")` blocks,
  empty ones included: `_1_1` for library symbols whose only block is `_0_1` (power symbols) and
  `_5_2` for the power unit of a De Morgan symbol such as 74xx:74LS00 (observed in `sch upgrade`
  / `sym upgrade` re-saves, the only diff between the 10.0.6 and 10.99.0 outputs of the same
  files).

`generator_version` is part of the dialect because KiCad 8–10 run the future-format gate only
when that token is present (docs/VERIFICATION.md claim 9): a file that claims a stamp must also
carry the generator tag KiCad pairs with it, or an older kicad-cli would try to load a newer file
instead of refusing it. The strings are the format's own tags, not a capability decision — the
backends decide *which* epoch a given install gets (from `kiwrap/data/kicad_version_matrix.json`).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from kiwrap.formats.sexpr import Atom, Node, build

__all__ = [
    "SchDialect",
    "convert_lib_symbol",
    "dialect_for",
    "dialect_for_stamp",
    "unit_block_name",
]

_UNIT_RE = re.compile(r"^(?P<base>.*)_(?P<unit>\d+)_(?P<style>\d+)$")


@dataclass(frozen=True)
class SchDialect:
    """Spelling rules for one schematic epoch (see the module docstring for the evidence)."""

    name: str  # "7" | "8" | "9" | "10" | "11" — the era, used in messages only
    sch_stamp: int
    sym_stamp: int
    generator_version: str | None  # the tag KiCad pairs with the stamp; None = no token (7-era)
    quoted_generator: bool  # `(generator "eeschema")` vs bare `(generator eeschema)`
    quoted_uuid: bool  # `(uuid "…")` vs bare `(uuid …)`
    exclude_from_sim: bool  # `(exclude_from_sim no)` on symbols / sheets / lib symbols (8+)
    property_level_hide: bool  # `(hide yes)` + show_name / do_not_autoplace on the property (10+)
    hide_yes_lists: bool  # `(hide yes)` on pins, pin_numbers, pin_names (9+); else bare `hide`
    effects_hide_as_list: bool  # inside effects: `(hide yes)` (8, 9) vs bare `hide` (7)
    body_style_token: bool  # `(body_style 1)` on placed symbols (10+)
    in_pos_files: bool  # `(in_pos_files yes)` on placed / library symbols (10+)
    duplicate_pin_numbers_are_jumpers: bool  # library symbol flag (10+)
    power_scope: bool  # `(power global)` / `(power local)` (10+) vs bare `(power)`
    embedded_fonts: bool  # `(embedded_fonts no)` at the root and inside library symbols (9+)
    empty_unit_blocks: bool  # writer emits `(symbol "X_1_1")` even when empty (11 preview)
    datasheet_default: str  # "~" before 10, "" from 10 on
    description_property: bool  # placed symbols carry a `Description` property (8+)
    sheet_flags: bool  # sheets carry exclude_from_sim / in_bom / on_board / dnp (9+)

    @property
    def uuid_atom(self):  # noqa: ANN201 - Atom
        return Atom.string if self.quoted_uuid else Atom.symbol

    def uuid_node(self, value: str) -> Node:
        return build("uuid", self.uuid_atom(value))


# The five dialects, keyed by the era name. Stamps are the ones KiCad's own writers stamp at each
# era (docs/ARCHITECTURE.md §7 "Dialect tables"; kiwrap/data/kicad_version_matrix.json carries the
# same numbers as `formats.sch` / `formats.sym` per install).
_ERA_7 = SchDialect(
    name="7",
    sch_stamp=20230121,
    sym_stamp=20220914,
    generator_version=None,
    quoted_generator=False,
    quoted_uuid=False,
    exclude_from_sim=False,
    property_level_hide=False,
    hide_yes_lists=False,
    effects_hide_as_list=False,
    body_style_token=False,
    in_pos_files=False,
    duplicate_pin_numbers_are_jumpers=False,
    power_scope=False,
    embedded_fonts=False,
    empty_unit_blocks=False,
    datasheet_default="~",
    description_property=False,
    sheet_flags=False,
)
_ERA_8 = SchDialect(
    name="8",
    sch_stamp=20231120,
    sym_stamp=20231120,
    generator_version="8.0",
    quoted_generator=True,
    quoted_uuid=True,
    exclude_from_sim=True,
    property_level_hide=False,
    hide_yes_lists=False,
    effects_hide_as_list=True,
    body_style_token=False,
    in_pos_files=False,
    duplicate_pin_numbers_are_jumpers=False,
    power_scope=False,
    embedded_fonts=False,
    empty_unit_blocks=False,
    datasheet_default="~",
    description_property=True,
    sheet_flags=False,
)
_ERA_9 = SchDialect(
    name="9",
    sch_stamp=20250114,
    sym_stamp=20241209,
    generator_version="9.0",
    quoted_generator=True,
    quoted_uuid=True,
    exclude_from_sim=True,
    property_level_hide=False,
    hide_yes_lists=True,
    effects_hide_as_list=True,
    body_style_token=False,
    in_pos_files=False,
    duplicate_pin_numbers_are_jumpers=False,
    power_scope=False,
    embedded_fonts=True,
    empty_unit_blocks=False,
    datasheet_default="~",
    description_property=True,
    sheet_flags=True,
)
_ERA_10 = SchDialect(
    name="10",
    sch_stamp=20260306,
    sym_stamp=20251024,
    generator_version="10.0",
    quoted_generator=True,
    quoted_uuid=True,
    exclude_from_sim=True,
    property_level_hide=True,
    hide_yes_lists=True,
    effects_hide_as_list=True,
    body_style_token=True,
    in_pos_files=True,
    duplicate_pin_numbers_are_jumpers=True,
    power_scope=True,
    embedded_fonts=True,
    empty_unit_blocks=False,
    datasheet_default="",
    description_property=True,
    sheet_flags=True,
)
_ERA_11 = SchDialect(
    name="11",
    sch_stamp=20260830,
    sym_stamp=20260830,
    generator_version="10.99",
    quoted_generator=True,
    quoted_uuid=True,
    exclude_from_sim=True,
    property_level_hide=True,
    hide_yes_lists=True,
    effects_hide_as_list=True,
    body_style_token=True,
    in_pos_files=True,
    duplicate_pin_numbers_are_jumpers=True,
    power_scope=True,
    embedded_fonts=True,
    empty_unit_blocks=True,
    datasheet_default="",
    description_property=True,
    sheet_flags=True,
)

# (first schematic stamp of the era, dialect), newest first. 20260101 is the earliest stamp the
# 10.0 writer used (its shipped demos), so the 10-era starts there.
_ERAS: tuple[tuple[int, SchDialect], ...] = (
    (20260830, _ERA_11),
    (20260101, _ERA_10),
    (20250114, _ERA_9),
    (20231120, _ERA_8),
    (0, _ERA_7),
)


def dialect_for_stamp(
    sch_stamp: int, sym_stamp: int | None = None, generator_version: str | None = None
) -> SchDialect:
    """The dialect whose era contains `sch_stamp`, stamped with the exact stamps requested.

    `sym_stamp` (what `lib_symbols` entries are written against) defaults to the era's own symbol
    stamp; `generator_version` overrides the tag the era pairs with the stamp (backends may pass
    the install's own major.minor)."""
    era = next(d for first, d in _ERAS if sch_stamp >= first)
    changes: dict[str, object] = {"sch_stamp": int(sch_stamp)}
    if sym_stamp is not None:
        changes["sym_stamp"] = int(sym_stamp)
    if generator_version is not None:
        changes["generator_version"] = generator_version
    return _replace(era, **changes)


def dialect_for(epoch: Mapping[str, int | str | None], *, generator_version: str | None = None) -> SchDialect:
    """`dialect_for({"sch": 20260306, "sym": 20251024})` -> the 10-era dialect at those stamps.

    `epoch` is the writer's epoch mapping (`formats.sch` / `formats.sym` of the capability record);
    a missing `sym` falls back to the era default. An optional `generator_version` key or kwarg
    overrides the generator tag."""
    if "sch" not in epoch or epoch["sch"] is None:
        raise ValueError("epoch needs a 'sch' stamp, e.g. {'sch': 20260306, 'sym': 20251024}")
    sym = epoch.get("sym")
    gv = generator_version if generator_version is not None else epoch.get("generator_version")
    return dialect_for_stamp(
        int(epoch["sch"]),
        int(sym) if sym is not None else None,
        str(gv) if gv is not None else None,
    )


def _replace(d: SchDialect, **changes: object) -> SchDialect:
    from dataclasses import replace

    return replace(d, **changes)  # type: ignore[arg-type]


def unit_block_name(symbol_name: str, unit: int, body_style: int) -> str:
    """`("R", 1, 1)` -> `"R_1_1"` (KiCad's `<name>_<unit>_<bodystyle>` sub-symbol naming)."""
    return f"{symbol_name}_{unit}_{body_style}"


# ----------------------------------------------------------------------------------------------
# library symbol conversion
# ----------------------------------------------------------------------------------------------


def _flag_atom(node: Node, name: str) -> Atom | None:
    for a in node.atoms:
        if a.kind == "symbol" and a.value == name:
            return a
    return None


def _set_hide(node: Node, hidden: bool, dialect: SchDialect, *, as_list: bool) -> None:
    """Spell "hidden" on `node` (a pin / pin_numbers / pin_names / effects list) for `dialect`:
    a `(hide yes)` child when `as_list`, a bare `hide` atom otherwise. Removes the other spelling."""
    existing_list = node.find("hide")
    existing_atom = _flag_atom(node, "hide")
    if not hidden:
        if existing_list is not None:
            node.remove(existing_list)
        if existing_atom is not None:
            node.remove(existing_atom)
        return
    if as_list:
        if existing_atom is not None:
            node.remove(existing_atom)
        if existing_list is None:
            node.append(build("hide", Atom.symbol("yes")))
    else:
        if existing_list is not None:
            node.remove(existing_list)
        if existing_atom is None:
            _append_flag(node, "hide")


def _append_flag(node: Node, name: str) -> None:
    """Append a bare flag atom after the scalar arguments (before any child lists) — KiCad 7/8
    write `(pin_numbers hide)`, `(effects (font …) hide)`, `(pin … (length 2.54) hide (name …))`."""
    # bare flags sit after the last scalar / after `(length …)` for pins, before name/number
    if node.head == "pin":
        length = node.find("length")
        idx = node.index(length) + 1 if length is not None else len(node.children)
        node.insert(idx, Atom.symbol(name))
        return
    atom = node.append(Atom.symbol(name))
    # KiCad 7/8 keep the flag on the line of the preceding list: `(pin_names (offset 0) hide)`
    if node.head in ("pin_numbers", "pin_names", "effects"):
        atom.lead = " "


def _is_hidden(node: Node) -> bool:
    hide = node.find("hide")
    if hide is not None:
        atoms = hide.atoms
        return not atoms or atoms[0].value in ("yes", "true", "1")
    return _flag_atom(node, "hide") is not None


def _convert_property(prop: Node, dialect: SchDialect, *, placed: bool) -> None:
    """Move the hide flag between the property level (10+) and `(effects …)` (≤ 9) and add or
    drop `show_name` / `do_not_autoplace`."""
    effects = prop.find("effects")
    hidden = _is_hidden(prop) or (effects is not None and _is_hidden(effects))
    if dialect.property_level_hide:
        if effects is not None:
            _set_hide(effects, False, dialect, as_list=True)
        for name in ("show_name", "do_not_autoplace"):
            if prop.find(name) is None:
                prop.insert(_before_effects(prop), build(name, Atom.symbol("no")))
        if prop.find("hide") is not None or _flag_atom(prop, "hide") is not None:
            if not hidden:
                _set_hide(prop, False, dialect, as_list=True)
            return  # already spelled the 10+ way: leave the node byte-identical
        if hidden:
            # placed symbols: `(hide yes)` right after `(at …)`; library symbols: after
            # show_name / do_not_autoplace (what KiCad's two writers emit)
            at = prop.find("at")
            if placed and at is not None:
                prop.insert(prop.index(at) + 1, build("hide", Atom.symbol("yes")))
            else:
                prop.insert(_before_effects(prop), build("hide", Atom.symbol("yes")))
        return
    _set_hide(prop, False, dialect, as_list=True)
    for name in ("show_name", "do_not_autoplace"):
        child = prop.find(name)
        if child is not None:
            prop.remove(child)
    if effects is None:
        effects = build("effects", build("font", build("size", 1.27, 1.27)))
        prop.append(effects)
    _set_hide(effects, hidden, dialect, as_list=dialect.effects_hide_as_list)


def _before_effects(prop: Node) -> int:
    effects = prop.find("effects")
    return prop.index(effects) if effects is not None else len(prop.children)


def _convert_pin(pin: Node, dialect: SchDialect) -> None:
    hidden = _is_hidden(pin)
    _set_hide(pin, hidden, dialect, as_list=dialect.hide_yes_lists)


def _convert_symbol_body(sym: Node, dialect: SchDialect) -> None:
    for head in ("pin_numbers", "pin_names"):
        child = sym.find(head)
        if child is not None:
            _set_hide(child, _is_hidden(child), dialect, as_list=dialect.hide_yes_lists)
    for pin in sym.find_all("pin"):
        _convert_pin(pin, dialect)
    for sub in sym.find_all("symbol"):
        _convert_symbol_body(sub, dialect)
        for pin in sub.find_all("pin"):
            _convert_pin(pin, dialect)


def _drop(node: Node, head: str) -> None:
    child = node.find(head)
    if child is not None:
        node.remove(child)


def convert_lib_symbol(node: Node, dialect: SchDialect) -> Node:
    """Rewrite a (flattened) library symbol node in place for `dialect` and return it.

    Handles every token that differs between the eras listed in the module docstring; tokens the
    dialect does not know are removed (an older parser would refuse the file), tokens it expects
    are added with KiCad's defaults. Graphics, pins, positions and properties are untouched."""
    props = node.find_all("property")
    for prop in props:
        _convert_property(prop, dialect, placed=False)
    _convert_symbol_body(node, dialect)
    if not dialect.exclude_from_sim:
        _drop(node, "exclude_from_sim")
    if not dialect.in_pos_files:
        _drop(node, "in_pos_files")
    if not dialect.duplicate_pin_numbers_are_jumpers:
        _drop(node, "duplicate_pin_numbers_are_jumpers")
    if not dialect.embedded_fonts:
        _drop(node, "embedded_fonts")
    power = node.find("power")
    if power is not None:
        if dialect.power_scope:
            if not power.atoms:
                power.append(Atom.symbol("global"))
        else:
            for a in list(power.atoms):
                power.remove(a)
    if dialect.empty_unit_blocks:
        _ensure_unit_blocks(node)
    return node


def _ensure_unit_blocks(node: Node) -> None:
    """The 11-preview writer emits every `(symbol "<Name>_U_S")` block of the unit x body-style
    matrix (units 1..N, styles 1..S where S = 2 when the symbol has a De Morgan alternate) even
    when a block has no items of its own: `(symbol "<Name>_1_1")` next to a lone `_0_1` block
    (power symbols), `(symbol "74LS00_5_2")` for the power unit of 74xx:74LS00 (observed in
    `sch upgrade` / `sym upgrade` re-saves on 10.99.0, the only diff against 10.0.6). Adding the
    missing blocks, sorted by (unit, style) as KiCad orders them, keeps the re-save diff empty."""
    atoms = node.atoms
    if not atoms:
        return
    name = atoms[0].value
    bare = name.split(":", 1)[1] if ":" in name else name
    keyed: list[tuple[tuple[int, int], Node]] = []
    for sub in node.find_all("symbol"):
        sa = sub.atoms
        m = _UNIT_RE.match(sa[0].value if sa else "")
        if m:
            keyed.append(((int(m.group("unit")), int(m.group("style"))), sub))
    units = max((k[0] for k, _n in keyed), default=1) or 1
    styles = max((k[1] for k, _n in keyed), default=1) or 1
    present = {k for k, _n in keyed}
    for unit in range(1, units + 1):
        for style in range(1, styles + 1):
            key = (unit, style)
            if key in present:
                continue
            block = build("symbol", unit_block_name(bare, unit, style))
            after = [n for k, n in keyed if k < key]
            before = [n for k, n in keyed if k > key]
            if after:
                node.insert(node.index(after[-1]) + 1, block)
            elif before:
                node.insert(node.index(before[0]), block)
            else:
                # before embedded_fonts if present, else at the end
                ef = node.find("embedded_fonts")
                node.insert(node.index(ef) if ef is not None else len(node.children), block)
            keyed.append((key, block))
            keyed.sort(key=lambda t: t[0])
            present.add(key)
