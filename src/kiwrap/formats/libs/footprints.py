"""`.kicad_mod` footprints on the CST: pads, courtyard rings, attributes, 3D models.

A `.pretty` library is a directory of `<name>.kicad_mod` files (one footprint each, head
`footprint` - 7.0 files use `module`); the file name IS the footprint name (KiCad's
`PCB_IO_KICAD_SEXPR::FootprintEnumerate` lists `*.kicad_mod` and uses the stem). The geometry
comes from `kiwrap.formats.board`'s footprint parser (the same code that models placed
footprints, so courtyard chaining and pad shapes agree with the board reader): with no `(at)`
the footprint sits at the origin, so board coordinates == footprint-local coordinates.
"""

from __future__ import annotations

from pathlib import Path

from kiwrap.formats import board as _board
from kiwrap.formats.libs.models import LibFootprint, LibPad, NotFound
from kiwrap.formats.sexpr import Node, ParseError, loads

__all__ = ["footprint_names", "load_footprint", "parse_footprint"]

_HEADS = ("footprint", "module")


def footprint_names(pretty_dir: Path) -> list[str]:
    """Footprint names of a `.pretty` directory (file stems, sorted)."""
    try:
        return sorted(p.stem for p in Path(pretty_dir).iterdir() if p.suffix == ".kicad_mod" and p.is_file())
    except OSError:
        return []


def _pad(p: _board.Pad) -> LibPad:
    drill: float | None = None
    slot: tuple[float, float] | None = None
    if p.drill is not None:
        drill = p.drill.diameter
        if p.drill.is_slot and p.drill.width is not None:
            slot = (drill, p.drill.width)
    return LibPad(
        number=p.number,
        shape=p.shape,
        at_mm=(p.local_position.x, p.local_position.y),
        size_mm=(p.size.x, p.size.y),
        layers=tuple(p.layers),
        drill=drill,
        drill_slot_mm=slot,
        type=p.type,
        rotation=p.rotation,
        pin_function=p.pin_function,
        pin_type=p.pin_type,
    )


def _ring(poly: _board.Polygon) -> tuple[tuple[float, float], ...]:
    return tuple((pt.x, pt.y) for pt in poly.points)


def parse_footprint(node: Node, *, nickname: str = "", path: Path | None = None) -> LibFootprint:
    if node.head not in _HEADS:
        raise ValueError(f"{path or '<footprint>'}: not a footprint document (head {node.head!r})")
    fp = _board._parse_footprint(node, _board._NetTable(node))
    if path is not None:
        name = path.stem  # the file stem is the name KiCad lists, whatever the header says
    else:
        # a footprint saved from a board keeps `Lib:Name` in its header
        name = fp.fpid.split(":", 1)[1] if ":" in fp.fpid else fp.fpid
    pads = tuple(_pad(p) for p in fp.pads)
    rings = fp.courtyard_back if fp.layer == "B.Cu" else fp.courtyard_front
    courtyard = tuple(_ring(r) for r in rings)
    open_chains = tuple(tuple((pt.x, pt.y) for pt in chain) for chain in fp.courtyard_open)
    xs: list[float] = []
    ys: list[float] = []
    for ring in courtyard:
        for x, y in ring:
            xs.append(x)
            ys.append(y)
    for p in fp.pads:
        box = p.bbox()
        xs.extend((box.min_x, box.max_x))
        ys.extend((box.min_y, box.max_y))
    bbox = (min(xs), min(ys), max(xs), max(ys)) if xs else None
    models = tuple(m.atoms[0].value for m in node.find_all("model") if m.atoms)
    tags = node.get("tags") or ""
    return LibFootprint(
        fpid=f"{nickname}:{name}" if nickname else name,
        name=name,
        nickname=nickname,
        node=node,
        pads=pads,
        courtyard=courtyard,
        description=fp.description,
        tags=tags,
        attributes=tuple(a for a in fp.attributes if a != "dnp"),
        properties=dict(fp.properties),
        layer=fp.layer,
        path=path,
        courtyard_open=open_chains,
        bbox_mm=bbox,
        models=models,
    )


def load_footprint(pretty_dir: Path, name: str, *, nickname: str = "") -> LibFootprint:
    path = Path(pretty_dir) / f"{name}.kicad_mod"
    if not path.is_file():
        raise NotFound(
            f"footprint {name!r} is not in {nickname or pretty_dir}",
            why=f"{path.name} does not exist in that .pretty directory",
            fix="search with kw_lib_search(kind='footprint') first; names are case-sensitive",
            candidates=[n for n in footprint_names(pretty_dir) if name.lower() in n.lower()][:20],
        )
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        text = path.read_text(encoding="utf-8", errors="replace")
    try:
        node = loads(text)
    except ParseError as e:
        raise ValueError(f"{path}: {e}") from e
    return parse_footprint(node, nickname=nickname, path=path)
