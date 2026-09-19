"""Read and write the format-epoch header of a KiCad s-expression document.

Every KiCad file starts `(kicad_xxx (version N) (generator G) [(generator_version V)] …)`:

* `(version N)` is the date-coded format stamp (e.g. sch 20260306, pcb 20260206). KiCad 7
  writes `(generator eeschema)` as a bare symbol; 8.0+ writes `(generator "eeschema")` quoted and
  adds `(generator_version "8.0")`. This module keeps whichever spelling the file already uses.
* Which stamp belongs to which KiCad release is **not** known here — that table lives in
  `kiwrap/data` and the backends branch on capability keys.
* The stamp alone does not gate loading everywhere: on 8.0–10.0 the schematic parser runs its
  future-format check only when `generator_version` is present (docs/VERIFICATION.md claim 9,
  "sch_stamp_gate_needs_generator_version"), so callers that want a file to be rejected by
  older KiCads the way a genuine file is must set both fields with `set_stamp`.
"""

from __future__ import annotations

from kiwrap.formats.sexpr import Atom, Node, build

__all__ = ["read_generator", "read_stamp", "set_stamp"]


def read_stamp(node: Node) -> int:
    """Integer value of `(version N)`; raises ValueError when the header is missing or malformed."""
    text = node.get("version")
    if text is None:
        raise ValueError(f"({node.head or '?'} …) has no (version N) header")
    try:
        return int(text)
    except ValueError:
        raise ValueError(f"(version {text!r}) is not an integer stamp") from None


def read_generator(node: Node) -> tuple[str, str | None]:
    """`(generator, generator_version)`; the generator is "" when absent, the version None."""
    generator = node.get("generator")
    return generator if generator is not None else "", node.get("generator_version")


def set_stamp(node: Node, stamp: int, generator: str, generator_version: str | None) -> None:
    """Rewrite the header in place, touching only the atoms that change.

    Missing `(version …)`, `(generator …)` and `(generator_version …)` lists are inserted in
    KiCad's order right after the head; `generator_version=None` removes that list (KiCad 7
    rejects the token with "Unknown token 'generator_version'", claim 9). The existing quoting
    style of `generator` (bare in 7-era files, quoted in 8.0+) is preserved.
    """
    version = node.find("version")
    if version is None:
        # after the head and any leading scalars: `(footprint "Name" (version …)`
        first_list = next((i for i, c in enumerate(node.children) if isinstance(c, Node)), len(node.children))
        version = node.insert(first_list, build("version", stamp))
    else:
        _set_scalar(version, str(stamp), "number")

    gen = node.find("generator")
    if gen is None:
        gen = node.insert(node.index(version) + 1, build("generator", generator))
    else:
        _set_scalar(gen, generator, None)

    gv = node.find("generator_version")
    if generator_version is None:
        if gv is not None:
            node.remove(gv)
        return
    if gv is None:
        node.insert(node.index(gen) + 1, build("generator_version", generator_version))
    else:
        _set_scalar(gv, generator_version, "string")


def _set_scalar(child: Node, value: str, kind: str | None) -> None:
    atoms = child.atoms
    if not atoms:
        child.append(Atom.number(value) if kind == "number" else Atom.string(value))
        return
    atom = atoms[0]
    if atom.value == value and (kind is None or atom.kind == kind):
        return
    if kind is not None:
        atom.kind = kind
    atom.value = value
