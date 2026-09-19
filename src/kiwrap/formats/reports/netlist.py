"""Parser for `kicad-cli sch export netlist --format kicadsexpr` output (`(export (version "E") …)`).

Shape, identical in substance on 7.0.6 – 10.0.0 (samples under spikes/**/*.net; 10 adds empty
`(groups)` / `(variants)` lists and prints one token per line, 7/8 pack several per line):

    (export (version "E")
      (design (source …) (date …) (tool "Eeschema 10.0.0") (sheet …))
      (components
        (comp (ref "C1") (value "10uF") (footprint "Lib:Name")? (datasheet …)? (description …)?
              (fields (field (name "Footprint") "value")…)
              (libsource (lib "Device") (part "C") (description …))
              (property (name "Sheetname") (value ""))… (property (name "dnp"))?
              (sheetpath (names "/") (tstamps "/")) (tstamps "uuid") (units …)?))
      (libparts …) (libraries …)
      (nets (net (code "1") (name "+3V3") (class "Default")?
              (node (ref "C2") (pin "1") (pinfunction "VO_2")? (pintype "passive")?)…)))

Verified with kicad-cli 8.0.1 and 10.0.0 (sandboxed KICAD_CONFIG_HOME): a symbol with
`(dnp yes)` gets a value-less `(property (name "dnp"))`; a symbol with `(on_board no)` is left
out of `components` altogether, so `exclude_from_board` can only be True for a netlist written
by a tool that emits `(property (name "exclude_from_board"))` explicitly.
"""

from __future__ import annotations

from kiwrap.formats.reports import Component, Net, Netlist, NetNode
from kiwrap.formats.sexpr import Atom, Node, loads

__all__ = ["parse_netlist"]

_FLAG_PROPERTIES = {"dnp", "exclude_from_board", "exclude_from_bom"}


def _scalar(node: Node | None) -> str | None:
    if node is None:
        return None
    atoms = node.atoms
    return atoms[0].value if atoms else None


def _named_value(node: Node) -> tuple[str, str | None]:
    """`(property (name X) (value Y))` -> (X, Y); `(field (name X) "Y")` -> (X, Y)."""
    name = _scalar(node.find("name")) or ""
    value_node = node.find("value")
    if value_node is not None:
        return name, _scalar(value_node) or ""
    trailing = [c for c in node.args if isinstance(c, Atom)]
    return name, trailing[0].value if trailing else None


def _component(comp: Node) -> Component:
    properties: dict[str, str] = {}
    dnp = False
    exclude_from_board = False
    fields = comp.find("fields")
    if fields is not None:
        for field in fields.find_all("field"):
            name, value = _named_value(field)
            if name:
                properties[name] = value or ""
    for prop in comp.find_all("property"):
        name, value = _named_value(prop)
        if name in _FLAG_PROPERTIES and value is None:
            dnp = dnp or name == "dnp"
            exclude_from_board = exclude_from_board or name == "exclude_from_board"
            continue
        if name:
            properties[name] = value or ""
    lib_id: str | None = None
    libsource = comp.find("libsource")
    if libsource is not None:
        lib = _scalar(libsource.find("lib")) or ""
        part = _scalar(libsource.find("part")) or ""
        lib_id = f"{lib}:{part}" if lib else part or None
    sheetpath = comp.find("sheetpath")
    footprint = _scalar(comp.find("footprint"))
    return Component(
        ref=_scalar(comp.find("ref")) or "",
        value=_scalar(comp.find("value")) or "",
        footprint=footprint or None,
        lib_id=lib_id,
        sheet_path=_scalar(sheetpath.find("names")) if sheetpath is not None else None,
        properties=properties,
        dnp=dnp,
        exclude_from_board=exclude_from_board,
    )


def _net(net: Node) -> Net:
    code_text = _scalar(net.find("code")) or "0"
    nodes = tuple(
        NetNode(
            ref=_scalar(n.find("ref")) or "",
            pin=_scalar(n.find("pin")) or "",
            pin_function=_scalar(n.find("pinfunction")),
            pin_type=_scalar(n.find("pintype")),
        )
        for n in net.find_all("node")
    )
    return Net(code=int(code_text), name=_scalar(net.find("name")) or "", nodes=nodes)


def parse_netlist(text: str, *, source: str = "", kicad_version: str = "") -> Netlist:
    """Parse a kicadsexpr netlist. `source` falls back to `(design (source …))`, `kicad_version`
    to the version in `(design (tool "Eeschema X.Y.Z"))`."""
    root = loads(text)
    if root.head != "export":
        raise ValueError(f"not a KiCad netlist: top-level list is ({root.head or '?'} …)")
    design = root.find("design")
    if not source and design is not None:
        source = _scalar(design.find("source")) or ""
    if not kicad_version and design is not None:
        tool = _scalar(design.find("tool")) or ""
        kicad_version = tool.rsplit(" ", 1)[-1] if " " in tool else ""
    components_node = root.find("components")
    nets_node = root.find("nets")
    components = tuple(_component(c) for c in components_node.find_all("comp")) if components_node else ()
    nets = tuple(_net(n) for n in nets_node.find_all("net")) if nets_node else ()
    return Netlist(source=source, kicad_version=kicad_version, components=components, nets=nets)
