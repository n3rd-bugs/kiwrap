"""Lossless s-expression concrete syntax tree (CST) for every KiCad text format.

Contract (conformance test T1): `dumps(loads(text)) == text` byte-for-byte for every file KiCad
7.0 - 10.0 writes (.kicad_sch, .kicad_pcb, .kicad_mod, .kicad_sym, .kicad_dru, *-lib-table,
netlist exports), and after any edit through this API only the touched nodes change. Unknown
tokens are preserved untouched. Whitespace, comments (none in practice), quoting style and number
formatting of untouched nodes are preserved exactly.

Public surface (implemented in `cst.py`, re-exported here):
    loads(text) -> Node                # parse; raises ParseError(line, col, message)
    load(path) -> Node
    dumps(node) -> str                 # exact round-trip for untouched nodes
    dump(node, path)
    Node                               # a list node: .head (first atom, e.g. "symbol"), .children,
                                       #   .find(head) / .find_all(head) / .get(head) helpers,
                                       #   .append(...), .insert(i, ...), .remove(child), .replace(child, new)
    Atom                               # .value (str), .kind ("symbol" | "string" | "number"),
                                       #   .as_str() / .as_float() / .as_int()
    build(head, *args)                 # convenience constructor producing KiCad-style formatting
    quote(s) / unquote(s)              # KiCad string escaping rules

Formatting of NEW nodes follows KiCad's own writer for that format (one child per line, tab
indentation, quoted strings) so that `kicad-cli ... upgrade` re-saves produce minimal diffs.
Numbers are written the way KiCad writes them (no trailing zeros, no exponent, up to 6 decimals).
"""

from __future__ import annotations

from kiwrap.formats.sexpr.cst import (
    Atom,
    Node,
    ParseError,
    Style,
    build,
    dump,
    dumps,
    format_number,
    load,
    loads,
    quote,
    unquote,
)

__all__ = [
    "Atom",
    "Node",
    "ParseError",
    "Style",
    "build",
    "dump",
    "dumps",
    "format_number",
    "load",
    "loads",
    "quote",
    "unquote",
]
