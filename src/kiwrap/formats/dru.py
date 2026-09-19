"""Writer/reader for KiCad custom design-rule files (`<project>.kicad_dru`).

A `.kicad_dru` is a sequence of top-level s-expressions, not one list: `(version 1)` followed by
`(rule "name" [(layer L)] [(severity S)] [(condition "expr")] (constraint TYPE [(min v)] [(opt v)]
[(max v)]) …)`, with `#` line comments allowed between them (the syntax mirrors the shipped
`demos/vme-wren/vme-wren.kicad_dru`; the CST parses such a document into an *implicit* root,
see `kiwrap.formats.sexpr`). kicad-cli DRC picks the file up when it sits next to the board with
the project's stem (`<name>.kicad_dru`); it does **not** surface parse errors of the rules file
(conformance corpus README, `10.0/handwritten/scope.kicad_dru`), which is why `render_dru`
re-parses what it produced and `write_dru` refuses to write text that does not parse.

Rules can be given as ready-made text (verbatim, one or several `(rule …)` blocks) or as
`DruRule` values; both go through the same balance check. Numbers carry units the way KiCad's
rule language expects (`0.2mm`, `10mil`); `DruRule` constraint values are rendered as
`<value>mm` when they are numbers.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from kiwrap.formats.sexpr import Atom, Node, ParseError, format_number, loads, quote

__all__ = ["DRU_VERSION", "DruRule", "parse_dru", "render_dru", "write_dru"]

DRU_VERSION = 1


@dataclass(frozen=True)
class DruRule:
    """One `(rule …)`; `constraints` map a constraint type (`clearance`, `track_width`,
    `hole_size`, `edge_clearance`, `courtyard_clearance`, `disallow`, …) to its argument text
    (`"(min 0.3mm)"`, `"track via"` for disallow) or to a number, rendered as `(min <v>mm)`.
    """

    name: str
    constraints: dict[str, str | float | int] = field(default_factory=dict)
    condition: str | None = None
    layer: str | None = None
    severity: str | None = None

    def to_node(self) -> Node:
        node = Node([Atom.symbol("rule"), Atom.string(self.name)])
        if self.layer:
            node.append(Node([Atom.symbol("layer"), Atom.string(self.layer)]))
        if self.severity:
            node.append(Node([Atom.symbol("severity"), Atom.symbol(self.severity)]))
        if self.condition:
            node.append(Node([Atom.symbol("condition"), Atom.string(self.condition)]))
        for kind, arg in self.constraints.items():
            node.append(loads(f"(constraint {kind} {_constraint_arg(arg)})"))
        return node

    def render(self) -> str:
        lines = [f"(rule {quote(self.name)}"]
        if self.layer:
            lines.append(f"\t(layer {quote(self.layer)})")
        if self.severity:
            lines.append(f"\t(severity {self.severity})")
        if self.condition:
            lines.append(f"\t(condition {quote(self.condition)})")
        for kind, arg in self.constraints.items():
            lines.append(f"\t(constraint {kind} {_constraint_arg(arg)})")
        lines.append(")")
        return "\n".join(lines)


def _constraint_arg(arg: str | float | int) -> str:
    if isinstance(arg, bool):
        raise TypeError("a constraint argument is a number or text, not a bool")
    if isinstance(arg, int | float):
        return f"(min {format_number(arg)}mm)"
    return arg.strip()


def render_dru(rules: str | Iterable[DruRule | str], *, version: int = DRU_VERSION) -> str:
    """The full file text: `(version N)` and the rules, separated by blank lines, LF endings.

    A `str` is taken as verbatim rule text (its own `(version …)` line, when present, is kept
    instead of adding a second one). The result is re-parsed; unbalanced text raises `ValueError`.
    """
    blocks: list[str] = []
    if isinstance(rules, str):
        body = rules.strip()
        if not _has_version(body):
            blocks.append(f"(version {version})")
        if body:
            blocks.append(body)
    else:
        blocks.append(f"(version {version})")
        for r in rules:
            blocks.append(r.render() if isinstance(r, DruRule) else str(r).strip())
    text = "\n\n".join(b for b in blocks if b) + "\n"
    try:
        loads(text)
    except ParseError as exc:
        raise ValueError(f"custom rules do not parse: {exc}") from exc
    return text


def write_dru(path: str | Path, rules: str | Iterable[DruRule | str], *, version: int = DRU_VERSION) -> Path:
    """Write `render_dru(rules)` to `path` (UTF-8, LF) and return it."""
    p = Path(path)
    p.write_text(render_dru(rules, version=version), encoding="utf-8", newline="\n")
    return p


def parse_dru(text: str) -> tuple[int | None, list[DruRule]]:
    """`(version, rules)` read back from a rules file; constraint arguments come back as text.

    Only the fields `DruRule` models are extracted (a rule with several constraints of one
    type keeps the last); unknown children are ignored, so the reader is best-effort and
    never a round-trip guarantee — the CST is, for that.
    """
    root = loads(text)
    version: int | None = None
    rules: list[DruRule] = []
    for child in root.nodes if root.implicit else [root]:
        if child.head == "version":
            atoms = child.atoms
            version = atoms[0].as_int() if atoms else None
        elif child.head == "rule":
            atoms = child.atoms
            name = atoms[0].value if atoms else ""
            constraints: dict[str, str | float | int] = {}
            for c in child.find_all("constraint"):
                kind = c.atoms[0].value if c.atoms else ""
                rest = " ".join(_render_inline(x) for x in c.args[1:])
                constraints[kind] = rest
            layer = child.get("layer")
            sev = child.get("severity")
            cond = child.get("condition")
            rules.append(DruRule(name, constraints, cond, layer, sev))
    return version, rules


def _render_inline(x: Atom | Node) -> str:
    if isinstance(x, Atom):
        return x.raw
    return "(" + " ".join(_render_inline(c) for c in x.children) + ")"


def _has_version(body: str) -> bool:
    return body.lstrip().startswith("(version")
