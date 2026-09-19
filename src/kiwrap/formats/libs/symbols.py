"""`.kicad_sym` libraries: symbols, units, pins and `extends` flattening on the lossless CST.

Flattening follows `LIB_SYMBOL::Flatten()` (eeschema/lib_symbol.cpp at the bench commit) and
is the rule docs/VERIFICATION.md claim 2 verified against kicad-cli 10 (`sch upgrade` re-save
of a hand-flattened symbol: 0 diff lines; an unflattened derived symbol embeds pinless and
reports `lib_symbol_mismatch`):

* start from a copy of the ROOT of the `extends` chain (its header flags `power`,
  `pin_names`, `pin_numbers`, `exclude_from_sim`, `in_bom`, `on_board`, `in_pos_files`,
  `duplicate_pin_numbers_are_jumpers`, `embedded_fonts` and its drawing units);
* walk the chain from the root's child down to the derived symbol; at every step the mandatory
  fields Reference / Value / Footprint / Datasheet / Description override the inherited ones
  only when the derived text is non-empty, every other user property overrides by name (KiCad
  removes the inherited field and appends the new one after the remaining fields),
  `ki_keywords` / `ki_fp_filters` (and v7's `ki_description`) override when non-empty,
  `ki_locked` stays whatever the root said;
* the result is renamed to the derived symbol's name and every unit block `<root>_U_S` to
  `<derived>_U_S`.

Pin coordinates stay in library units (mm, Y-up). Both dialects of the hide flag are read
(`hide` bare atom in 7.0, `(hide yes)` in 8.0+), likewise `pin_names`/`pin_numbers`.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path

from kiwrap.formats.libs.models import LibPin, LibSymbol, NotFound
from kiwrap.formats.sexpr import Node, ParseError, loads

__all__ = [
    "MANDATORY_FIELDS",
    "SymbolLibrary",
    "flatten",
    "parse_symbol",
    "symbol_names",
]

MANDATORY_FIELDS = ("Reference", "Value", "Footprint", "Datasheet", "Description")
_DCM_FIELDS = {"ki_keywords", "ki_fp_filters", "ki_description", "ki_locked"}
_UNIT_RE = re.compile(r"^(?P<base>.*)_(?P<unit>\d+)_(?P<style>\d+)$")


def _name_of(sym: Node) -> str:
    atoms = sym.atoms
    return atoms[0].value if atoms else ""


def _prop_name(prop: Node) -> str:
    atoms = prop.atoms
    return atoms[0].value if atoms else ""


def _prop_value(prop: Node) -> str:
    atoms = prop.atoms
    return atoms[1].value if len(atoms) > 1 else ""


def _hidden(node: Node) -> bool:
    """`hide` as a bare atom (7.0) or `(hide yes)` (8.0+)."""
    for a in node.atoms:
        if a.kind == "symbol" and a.value == "hide":
            return True
    h = node.find("hide")
    if h is None:
        return False
    atoms = h.atoms
    return not atoms or atoms[0].value.lower() in ("yes", "true", "1")


def _yes(node: Node, head: str, default: bool) -> bool:
    child = node.find(head)
    if child is None:
        return default
    atoms = child.atoms
    if not atoms:
        return True
    return atoms[0].value.lower() in ("yes", "true", "1")


def symbol_names(root: Node) -> list[str]:
    """Top-level symbol names of a `(kicad_symbol_lib ...)` document, in file order."""
    return [_name_of(s) for s in root.find_all("symbol")]


def _unit_of(subname: str, parent_name: str) -> tuple[int, int] | None:
    if not subname.startswith(parent_name + "_"):
        m = _UNIT_RE.match(subname)
        return (int(m.group("unit")), int(m.group("style"))) if m else None
    rest = subname[len(parent_name) + 1 :]
    m = re.match(r"^(\d+)_(\d+)$", rest)
    return (int(m.group(1)), int(m.group(2))) if m else None


def _parse_pin(node: Node, unit: int, style: int) -> LibPin:
    atoms = node.atoms
    etype = atoms[0].value if atoms else "passive"
    shape = atoms[1].value if len(atoms) > 1 and atoms[1].value != "hide" else "line"
    at = node.find("at")
    x = y = 0.0
    angle = 0
    if at is not None:
        nums = [a for a in at.atoms if a.kind == "number"]
        if len(nums) >= 2:
            x, y = nums[0].as_float(), nums[1].as_float()
        if len(nums) >= 3:
            angle = int(round(nums[2].as_float()))
    name_node, number_node = node.find("name"), node.find("number")
    name = name_node.atoms[0].value if name_node is not None and name_node.atoms else ""
    number = number_node.atoms[0].value if number_node is not None and number_node.atoms else ""
    length_text = node.get("length")
    try:
        length = float(length_text) if length_text is not None else 2.54
    except ValueError:
        length = 2.54
    return LibPin(
        number=number,
        name=name,
        x_mm=x,
        y_mm=y,
        orientation=angle % 360,
        electrical_type=etype,
        unit=unit,
        body_style=style,
        length_mm=length,
        shape=shape,
        hidden=_hidden(node),
    )


def _pins_of(sym: Node) -> list[LibPin]:
    name = _name_of(sym)
    pins = [_parse_pin(p, 0, 0) for p in sym.find_all("pin")]
    for sub in sym.find_all("symbol"):
        us = _unit_of(_name_of(sub), name) or (0, 0)
        pins.extend(_parse_pin(p, us[0], us[1]) for p in sub.find_all("pin"))
    return pins


def _unit_info(sym: Node) -> tuple[int, int, dict[int, str]]:
    """(unit count, body style count, unit display names) from the `<name>_U_S` blocks."""
    name = _name_of(sym)
    units = 1
    styles = 1
    names: dict[int, str] = {}
    for sub in sym.find_all("symbol"):
        us = _unit_of(_name_of(sub), name)
        if us is None:
            continue
        units = max(units, us[0])
        styles = max(styles, us[1])
        uname = sub.get("unit_name")
        if uname and us[0] > 0:
            names[us[0]] = uname
    return units, styles, names


# ----------------------------------------------------------------------------------------------
# flattening
# ----------------------------------------------------------------------------------------------


def _properties(sym: Node) -> list[Node]:
    return sym.find_all("property")


def _last_field_index(sym: Node) -> int:
    """Index after the last non-dcm `property` (where KiCad's appended user field lands)."""
    last = -1
    for i, c in enumerate(sym.children):
        if isinstance(c, Node) and c.head == "property" and _prop_name(c) not in _DCM_FIELDS:
            last = i
    if last >= 0:
        return last + 1
    # no properties at all: before the first unit block / embedded_fonts, after header flags
    for i, c in enumerate(sym.children):
        if isinstance(c, Node) and c.head in ("symbol", "embedded_fonts", "embedded_files"):
            return i
    return len(sym.children)


_DCM_ORDER = ("ki_locked", "ki_keywords", "ki_description", "ki_fp_filters")


def _dcm_insert_index(sym: Node, name: str) -> int:
    """Where KiCad's writer puts dcm info: after the fields, in the fixed order ki_locked,
    ki_keywords, (ki_description), ki_fp_filters (`saveDcmInfoAsFields`)."""
    rank = _DCM_ORDER.index(name)
    idx = _last_field_index(sym)
    for i, c in enumerate(sym.children):
        if isinstance(c, Node) and c.head == "property":
            pname = _prop_name(c)
            if pname in _DCM_ORDER and _DCM_ORDER.index(pname) < rank:
                idx = i + 1
    return idx


def _apply_overrides(target: Node, derived: Node) -> None:
    """Apply one derived symbol's fields/dcm info onto `target` (a flattened copy of its parent)."""
    by_name = {_prop_name(p): p for p in _properties(target)}
    for prop in _properties(derived):
        name = _prop_name(prop)
        value = _prop_value(prop)
        if name in MANDATORY_FIELDS:
            if value == "":
                continue
            existing = by_name.get(name)
            if existing is not None:
                new = target.replace(existing, prop.copy())
            else:
                new = target.insert(_last_field_index(target), prop.copy())
            assert isinstance(new, Node)
            by_name[name] = new
        elif name in ("ki_keywords", "ki_fp_filters", "ki_description"):
            if value == "":
                continue
            existing = by_name.get(name)
            if existing is not None:
                new = target.replace(existing, prop.copy())
            else:
                new = target.insert(_dcm_insert_index(target, name), prop.copy())
            assert isinstance(new, Node)
            by_name[name] = new
        elif name == "ki_locked":
            continue
        else:
            existing = by_name.get(name)
            if existing is not None:
                target.remove(existing)
            new = target.insert(_last_field_index(target), prop.copy())
            assert isinstance(new, Node)
            by_name[name] = new


def _rename(sym: Node, new_name: str) -> None:
    old = _name_of(sym)
    atoms = sym.atoms
    if atoms:
        atoms[0].value = new_name
    for sub in sym.find_all("symbol"):
        sa = sub.atoms
        if not sa:
            continue
        subname = sa[0].value
        us = _unit_of(subname, old)
        if us is not None:
            sa[0].value = f"{new_name}_{us[0]}_{us[1]}"


def flatten(by_name: dict[str, Node], name: str) -> Node:
    """A detached, flattened copy of symbol `name` (see the module docstring). Raises `NotFound`
    when `name` or a parent in its `extends` chain is missing; a cyclic chain flattens like
    KiCad's cycle guard (the symbol itself, `extends` dropped)."""
    node = by_name.get(name)
    if node is None:
        raise NotFound(
            f"symbol {name!r} is not in the library",
            why="no top-level (symbol ...) block carries that name",
            fix="search with kw_lib_search first; names are case-sensitive",
            candidates=sorted(by_name)[:20],
        )
    chain: list[Node] = [node]
    visited = {name}
    cur = node
    while True:
        parent_name = cur.get("extends")
        if not parent_name:
            break
        if parent_name in visited:
            break  # cycle: KiCad copies the symbol itself
        parent = by_name.get(parent_name)
        if parent is None:
            raise NotFound(
                f"symbol {name!r} extends {parent_name!r}, which is not in the library",
                why="a derived symbol needs its parent in the same library to be flattened",
                fix="fix the library or pick a symbol whose parent exists",
                candidates=sorted(by_name)[:20],
            )
        visited.add(parent_name)
        chain.append(parent)
        cur = parent
    root = chain[-1]
    out = root.copy()
    ext = out.find("extends")
    if ext is not None:  # only on a cyclic chain (KiCad resets m_parent on the copy)
        out.remove(ext)
    if len(chain) == 1:
        return out
    for derived in reversed(chain[:-1]):
        _apply_overrides(out, derived)
    _rename(out, name)
    return out


# ----------------------------------------------------------------------------------------------
# symbol model
# ----------------------------------------------------------------------------------------------


def parse_symbol(
    flat: Node,
    *,
    nickname: str,
    name: str | None = None,
    path: Path | None = None,
    raw: Node | None = None,
) -> LibSymbol:
    """Build a `LibSymbol` from a FLATTENED symbol node. `flat` is renamed in place to
    `<nickname>:<name>` (the `lib_symbols` embedding form) when a nickname is given."""
    name = name or _name_of(flat)
    props: dict[str, str] = {}
    for p in _properties(flat):
        props[_prop_name(p)] = _prop_value(p)
    if nickname and _name_of(flat) != f"{nickname}:{name}":
        atoms = flat.atoms
        if atoms:
            atoms[0].value = f"{nickname}:{name}"
    pins = _pins_of(flat)
    units, styles, unit_names = _unit_info(flat)
    if units == 1 and any(p.unit > 1 for p in pins):
        units = max(p.unit for p in pins)
    power_node = flat.find("power")
    power: str | None = None
    if power_node is not None:
        pa = power_node.atoms
        power = pa[0].value if pa and pa[0].value in ("global", "local") else "global"
    pin_names = flat.find("pin_names")
    offset: float | None = None
    names_hidden = False
    if pin_names is not None:
        off = pin_names.get("offset")
        try:
            offset = float(off) if off is not None else None
        except ValueError:
            offset = None
        names_hidden = _hidden(pin_names)
    pin_numbers = flat.find("pin_numbers")
    numbers_hidden = pin_numbers is not None and _hidden(pin_numbers)
    description = props.get("Description") or props.get("ki_description", "")
    filters = tuple(t for t in props.get("ki_fp_filters", "").split() if t)
    lib_id = f"{nickname}:{name}" if nickname else name
    return LibSymbol(
        lib_id=lib_id,
        name=name,
        nickname=nickname,
        node=flat,
        units=units,
        pins=tuple(pins),
        properties=props,
        footprint_filters=filters,
        keywords=props.get("ki_keywords", ""),
        description=description,
        extends=raw.get("extends") if raw is not None else None,
        power=power,
        path=path,
        unit_names=unit_names,
        body_styles=styles,
        pin_names_hidden=names_hidden,
        pin_numbers_hidden=numbers_hidden,
        pin_name_offset_mm=offset,
        exclude_from_sim=_yes(flat, "exclude_from_sim", False),
        in_bom=_yes(flat, "in_bom", True),
        on_board=_yes(flat, "on_board", True),
        raw_node=raw,
    )


@dataclass
class SymbolLibrary:
    """One parsed `.kicad_sym` file: symbol lookup by name and flattening."""

    path: Path | None
    root: Node
    nickname: str = ""
    by_name: dict[str, Node] = field(default_factory=dict)
    version: int | None = None
    _cache: dict[str, LibSymbol] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if not self.by_name:
            self.by_name = {_name_of(s): s for s in self.root.find_all("symbol")}
        if self.version is None:
            v = self.root.get("version")
            self.version = int(v) if v and v.isdigit() else None

    @classmethod
    def load(cls, path: Path, nickname: str = "") -> SymbolLibrary:
        path = Path(path)
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = path.read_text(encoding="utf-8", errors="replace")
        return cls.loads(text, path=path, nickname=nickname)

    @classmethod
    def loads(cls, text: str, *, path: Path | None = None, nickname: str = "") -> SymbolLibrary:
        try:
            root = loads(text)
        except ParseError as e:
            raise ValueError(f"{path or '<library>'}: {e}") from e
        if root.head != "kicad_symbol_lib":
            raise ValueError(f"{path or '<library>'}: not a .kicad_sym document (head {root.head!r})")
        return cls(path=path, root=root, nickname=nickname)

    def names(self) -> list[str]:
        return list(self.by_name)

    def __contains__(self, name: str) -> bool:
        return name in self.by_name

    def __iter__(self) -> Iterator[str]:
        return iter(self.by_name)

    def raw(self, name: str) -> Node:
        node = self.by_name.get(name)
        if node is None:
            raise NotFound(
                f"symbol {name!r} is not in {self.nickname or self.path}",
                why="no top-level (symbol ...) block carries that name",
                fix="search with kw_lib_search first; names are case-sensitive",
                candidates=self.suggest(name),
            )
        return node

    def suggest(self, name: str, limit: int = 10) -> list[str]:
        low = name.lower()
        hits = [n for n in self.by_name if low in n.lower() or n.lower() in low]
        return sorted(hits, key=lambda n: (abs(len(n) - len(name)), n))[:limit]

    def symbol(self, name: str) -> LibSymbol:
        """The flattened symbol (cached); its `node` is a fresh detached copy each call."""
        cached = self._cache.get(name)
        if cached is None:
            raw = self.raw(name)
            flat = flatten(self.by_name, name)
            cached = parse_symbol(flat, nickname=self.nickname, name=name, path=self.path, raw=raw)
            self._cache[name] = cached
        return replace(cached, node=cached.node.copy())

    def is_derived(self, name: str) -> bool:
        node = self.by_name.get(name)
        return bool(node is not None and node.get("extends"))
