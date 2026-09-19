"""Node builders for schematic elements, spelled per `SchDialect`.

Every builder returns a detached `kiwrap.formats.sexpr.Node` laid out KiCad-style; the writer
inserts it into the sheet tree. Token order follows what KiCad's own writer emits at that era
(placed symbol: lib_id, at, mirror, unit, body_style, exclude_from_sim, in_bom, on_board,
in_pos_files, dnp, uuid, properties, pins, instances; see `kiwrap.formats.epoch_dialects`).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from kiwrap.formats.epoch_dialects import SchDialect
from kiwrap.formats.sexpr import Atom, Node, build

__all__ = [
    "GRID_MM",
    "document",
    "effects",
    "global_label",
    "hierarchical_label",
    "instances",
    "junction",
    "label",
    "no_connect",
    "property_node",
    "sheet",
    "sheet_pin",
    "snap",
    "symbol",
    "wire",
]

#: KiCad's connection grid (DEFAULT_CONNECTION_GRID_MILS 50 = 1.27 mm; claim 2 (c))
GRID_MM = 1.27


def snap(v: float, grid: float = GRID_MM) -> float:
    """Snap a millimetre coordinate to the connection grid (4-decimal rounding removes float noise)."""
    return round(round(v / grid) * grid, 4)


def sym(name: str) -> Atom:
    return Atom.symbol(name)


def yes_no(flag: bool) -> Atom:
    return Atom.symbol("yes" if flag else "no")


def effects(
    size: float = 1.27,
    justify: Sequence[str] | None = None,
    hidden: bool = False,
    dialect: SchDialect | None = None,
) -> Node:
    node = build("effects", build("font", build("size", size, size)))
    if justify:
        node.append(build("justify", *(sym(j) for j in justify)))
    if hidden and dialect is not None and not dialect.property_level_hide:
        if dialect.effects_hide_as_list:
            node.append(build("hide", sym("yes")))
        else:
            node.append(sym("hide"))
    return node


def property_node(
    dialect: SchDialect,
    name: str,
    value: str,
    at: tuple[float, float],
    angle: float = 0,
    *,
    hidden: bool = False,
    size: float = 1.27,
    justify: Sequence[str] | None = None,
    effects_node: Node | None = None,
    placed: bool = True,
) -> Node:
    """`(property "Name" "value" (at x y a) [hide/show_name/do_not_autoplace] (effects …))`.

    `placed=True` uses the placed-symbol order (hide before show_name), `False` the library
    order. `effects_node` (a copy of the library property's effects) replaces the built one."""
    node = build("property", name, value, build("at", at[0], at[1], angle))
    if dialect.property_level_hide:
        if hidden and placed:
            node.append(build("hide", sym("yes")))
        node.append(build("show_name", sym("no")))
        node.append(build("do_not_autoplace", sym("no")))
        if hidden and not placed:
            node.append(build("hide", sym("yes")))
    eff = effects_node if effects_node is not None else effects(size, justify)
    if not dialect.property_level_hide:
        _strip_hide(eff)
        if hidden:
            if dialect.effects_hide_as_list:
                eff.append(build("hide", sym("yes")))
            else:
                eff.append(sym("hide"))
    else:
        _strip_hide(eff)
    node.append(eff)
    return node


def _strip_hide(eff: Node) -> None:
    child = eff.find("hide")
    if child is not None:
        eff.remove(child)
    for a in list(eff.atoms):
        if a.kind == "symbol" and a.value == "hide":
            eff.remove(a)


def instances(dialect: SchDialect, project: str, entries: Iterable[tuple[str, str, int]]) -> Node:
    """`(instances (project "P" (path "/…" (reference "R1") (unit 1)) …))` for placed symbols."""
    proj = build("project", project)
    for path, reference, unit in entries:
        proj.append(build("path", path, build("reference", reference), build("unit", unit)))
    return build("instances", proj)


def sheet_instances_block(dialect: SchDialect, project: str, entries: Iterable[tuple[str, str]]) -> Node:
    """`(instances (project "P" (path "/…" (page "2")) …))` for sheet elements."""
    proj = build("project", project)
    for path, page in entries:
        proj.append(build("path", path, build("page", page)))
    return build("instances", proj)


def symbol(
    dialect: SchDialect,
    *,
    lib_id: str,
    at: tuple[float, float],
    rotation: int,
    mirror: str | None,
    unit: int,
    uuid: str,
    properties: Sequence[Node],
    pin_numbers: Sequence[str],
    pin_uuids: Sequence[str],
    instance_entries: Iterable[tuple[str, str, int]],
    project: str,
    dnp: bool = False,
    in_bom: bool = True,
    on_board: bool = True,
    exclude_from_sim: bool = False,
    body_style: int = 1,
) -> Node:
    node = build("symbol", build("lib_id", lib_id), build("at", at[0], at[1], int(rotation) % 360))
    if mirror in ("x", "y"):
        node.append(build("mirror", sym(mirror)))
    node.append(build("unit", int(unit)))
    if dialect.body_style_token:
        node.append(build("body_style", int(body_style)))
    if dialect.exclude_from_sim:
        node.append(build("exclude_from_sim", yes_no(exclude_from_sim)))
    node.append(build("in_bom", yes_no(in_bom)))
    node.append(build("on_board", yes_no(on_board)))
    if dialect.in_pos_files:
        node.append(build("in_pos_files", yes_no(True)))
    node.append(build("dnp", yes_no(dnp)))
    node.append(dialect.uuid_node(uuid))
    for p in properties:
        node.append(p)
    for number, pu in zip(pin_numbers, pin_uuids, strict=True):
        node.append(build("pin", number, dialect.uuid_node(pu)))
    node.append(instances(dialect, project, instance_entries))
    return node


def wire(dialect: SchDialect, a: tuple[float, float], b: tuple[float, float], uuid: str) -> Node:
    return build(
        "wire",
        build("pts", build("xy", a[0], a[1]), build("xy", b[0], b[1])),
        build("stroke", build("width", 0), build("type", sym("default"))),
        dialect.uuid_node(uuid),
    )


def junction(dialect: SchDialect, at: tuple[float, float], uuid: str) -> Node:
    return build(
        "junction",
        build("at", at[0], at[1]),
        build("diameter", 0),
        build("color", 0, 0, 0, 0),
        dialect.uuid_node(uuid),
    )


def no_connect(dialect: SchDialect, at: tuple[float, float], uuid: str) -> Node:
    return build("no_connect", build("at", at[0], at[1]), dialect.uuid_node(uuid))


def _label_justify(rotation: int, *, local: bool) -> list[str]:
    r = int(rotation) % 360
    horizontal = "left" if r in (0, 90) else "right"
    return [horizontal, "bottom"] if local else [horizontal]


def label(
    dialect: SchDialect, text: str, at: tuple[float, float], rotation: int, uuid: str, size: float = 1.27
) -> Node:
    return build(
        "label",
        text,
        build("at", at[0], at[1], int(rotation) % 360),
        effects(size, _label_justify(rotation, local=True)),
        dialect.uuid_node(uuid),
    )


def global_label(
    dialect: SchDialect,
    text: str,
    at: tuple[float, float],
    rotation: int,
    uuid: str,
    shape: str = "input",
    size: float = 1.27,
) -> Node:
    node = build(
        "global_label",
        text,
        build("shape", sym(shape)),
        build("at", at[0], at[1], int(rotation) % 360),
        build("fields_autoplaced", sym("yes")),
        effects(size, _label_justify(rotation, local=False)),
        dialect.uuid_node(uuid),
    )
    node.append(
        property_node(
            dialect,
            "Intersheetrefs",
            "${INTERSHEET_REFS}",
            at,
            0,
            hidden=True,
            size=size,
            justify=_label_justify(rotation, local=False),
            placed=True,
        )
    )
    return node


def hierarchical_label(
    dialect: SchDialect,
    text: str,
    at: tuple[float, float],
    rotation: int,
    uuid: str,
    shape: str = "input",
    size: float = 1.27,
) -> Node:
    return build(
        "hierarchical_label",
        text,
        build("shape", sym(shape)),
        build("at", at[0], at[1], int(rotation) % 360),
        effects(size, _label_justify(rotation, local=False)),
        dialect.uuid_node(uuid),
    )


def sheet_pin(
    dialect: SchDialect,
    name: str,
    kind: str,
    at: tuple[float, float],
    rotation: int,
    uuid: str,
    size: float = 1.27,
) -> Node:
    r = int(rotation) % 360
    # KiCad 10 writes (justify left) for pins on the left edge (rot 180) and (justify right) on the
    # right edge (rot 0): the text sits inside the sheet outline.
    justify = ["left"] if r in (180, 90) else ["right"]
    return build(
        "pin",
        name,
        sym(kind),
        build("at", at[0], at[1], r),
        dialect.uuid_node(uuid),
        effects(size, justify),
    )


def sheet(
    dialect: SchDialect,
    *,
    at: tuple[float, float],
    size: tuple[float, float],
    uuid: str,
    name: str,
    file: str,
    pins: Sequence[Node],
    project: str,
    page_entries: Iterable[tuple[str, str]],
) -> Node:
    node = build("sheet", build("at", at[0], at[1]), build("size", size[0], size[1]))
    if dialect.sheet_flags:
        node.append(build("exclude_from_sim", yes_no(False)))
        node.append(build("in_bom", yes_no(True)))
        node.append(build("on_board", yes_no(True)))
        node.append(build("dnp", yes_no(False)))
    node.append(build("stroke", build("width", 0.1524), build("type", sym("solid"))))
    node.append(build("fill", build("color", 0, 0, 0, 0)))
    node.append(dialect.uuid_node(uuid))
    node.append(
        property_node(
            dialect, "Sheetname", name, (at[0], round(at[1] - 0.7116, 4)), 0, justify=["left", "bottom"]
        )
    )
    node.append(
        property_node(
            dialect,
            "Sheetfile",
            file,
            (at[0], round(at[1] + size[1] + 0.5846, 4)),
            0,
            justify=["left", "top"],
        )
    )
    for p in pins:
        node.append(p)
    node.append(sheet_instances_block(dialect, project, page_entries))
    return node


def document(dialect: SchDialect, uuid: str, *, root: bool, paper: str = "A4") -> Node:
    """A fresh, empty `.kicad_sch` document for `dialect`."""
    node = build("kicad_sch", build("version", dialect.sch_stamp))
    node.append(build("generator", "eeschema" if dialect.quoted_generator else sym("eeschema")))
    if dialect.generator_version is not None:
        node.append(build("generator_version", dialect.generator_version))
    node.append(dialect.uuid_node(uuid))
    node.append(build("paper", paper))
    node.append(build("lib_symbols"))
    if root:
        node.append(build("sheet_instances", build("path", "/", build("page", "1"))))
    if dialect.embedded_fonts:
        node.append(build("embedded_fonts", sym("no")))
    node.suffix = "\n"
    return node
