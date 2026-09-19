"""Lossless s-expression concrete syntax tree for KiCad text formats.

Every byte of the input survives a `loads` / `dumps` round trip: each `Atom` keeps its original
text (`raw`, including quoting and number formatting) and every element keeps the whitespace
that preceded it (`lead`); every `Node` keeps the whitespace before its closing paren (`tail`)
and the root keeps the text after it (`suffix`). Editing through the API touches only the
nodes it names; everything else is re-emitted verbatim (conformance test T1).

Lexing rules (KiCad's DSNLEXER in non-specctra mode, verified empirically against kicad-cli
10.0.0 `sch export netlist` with escape sequences planted in a property value; see
`docs/VERIFICATION.md` for the sandboxing rules those runs followed):

* A quoted string runs from `"` to the next unescaped `"`; raw tabs and newlines inside are
  legal. Escapes decoded by KiCad's reader: `\\a \\b \\f \\n \\r \\t \\v`, `\\\\`, `\\"`,
  `\\xH[H]` (one or two hex digits) and octal `\\N[N[N]]`; any other `\\X` is kept literally
  as backslash + X (KiCad 10 reads `\\{` back as `\\{`, `\\/` as `\\/`).
* KiCad's writer (`OUTPUTFORMATTER::Quotes`) escapes only `\\`, `"`, LF and CR; tabs are
  written raw. `quote()` does exactly that. In-band `{slash}` / `{dblquote}`-style escapes are
  application-level (net and lib-id names) and are left alone by this module.
* A bare token is anything up to whitespace, `(`, `)` or `"`. It is a number when it matches
  `[+-]?(\\d+\\.?\\d*|\\.\\d+)([eE][+-]?\\d+)?`, otherwise a symbol.

Formatting of NEW nodes (`build()` and anything appended to a tree) follows KiCad's
`KICAD_FORMAT::Prettify` (common/io/kicad/kicad_io_utils.cpp, the algorithm behind every
.kicad_sch / .kicad_pcb / .kicad_sym / .kicad_mod written since 8.0, also what kicad-cli 10
`sch upgrade` emits), derived from the files under `<install>/share/kicad/demos`:

* one tab per nesting level; the head atom and every scalar argument stay on the list's line,
  separated by single spaces: `(at 1 2 90)`;
* every child list starts on its own line at depth+1, except `(xy …)` lists which pack onto the
  previous `(xy …)` line while the column (tabs counting 1) is below 99 — that is why demo
  polygons show 3–5 points per line;
* when the run of scalar arguments reaches column 72 the next scalar wraps to a new line at
  depth+1 (long `(members …)` / image `(data …)` lists);
* the closing paren goes on its own line at the list's depth when the last thing written was a
  child list or a wrap happened, otherwise it closes inline;
* the document is `(root …)` followed by a single newline.

Files written by KiCad 7 use two-space indents; when a fresh node is inserted into such a tree
the indent unit is taken from the tree so it blends in. Numbers are written like KiCad's
`FormatDouble2Str`: integers without a decimal point, otherwise `%.10g` (up to 10 significant
digits, no trailing zeros) with an `%.10f`-then-strip fallback for tiny magnitudes so no
exponent ever appears.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Union, overload

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

# One match per token; group 1 = "(", 2 = ")", 3 = quoted string, 4 = a `#` comment (only
# honoured at the start of a line, see loads()), 5 = bare token. The whitespace in front of the
# token is part of match group 0 and becomes the element's `lead`.
_TOKEN = re.compile(r'\s*(?:(\()|(\))|("(?:[^"\\]|\\.)*")|(#[^\n]*)|([^\s()"]+))', re.S)
_BARE = re.compile(r'[^\s()"]+')
_NUMBER = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?\Z")
_ESCAPE = re.compile(r"\\(x[0-9a-fA-F]{1,2}|[0-7]{1,3}|.)", re.S)
_SIMPLE_ESCAPES = {
    "a": "\a",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "v": "\v",
    "\\": "\\",
    '"': '"',
}

# Prettify constants (KICAD_FORMAT::Prettify).
_XY_COLUMN_LIMIT = 99
_TOKEN_WRAP_COLUMN = 72
_DEFAULT_INDENT = "\t"

Value = Union[str, int, float, bool, "Atom", "Node"]


class ParseError(ValueError):
    """Malformed s-expression text. `line` and `col` are 1-based."""

    def __init__(self, message: str, line: int, col: int) -> None:
        super().__init__(f"{message} at line {line}, column {col}")
        self.message = message
        self.line = line
        self.col = col


# ----------------------------------------------------------------------------------------------
# strings and numbers
# ----------------------------------------------------------------------------------------------


def quote(s: str) -> str:
    """Wrap `s` in double quotes the way KiCad's OUTPUTFORMATTER::Quotes does.

    Only backslash, double quote, LF and CR are escaped; tabs and every other character are
    written raw (verified: KiCad 10 writes a property containing a tab as a raw tab).
    """
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r") + '"'


def _decode_escape(m: re.Match[str]) -> str:
    body = m.group(1)
    if body in _SIMPLE_ESCAPES:
        return _SIMPLE_ESCAPES[body]
    if body[0] == "x" and len(body) > 1:
        return chr(int(body[1:], 16))
    if body[0] in "01234567":
        return chr(int(body, 8))
    return "\\" + body  # unknown escape: KiCad keeps the backslash


def unquote(s: str) -> str:
    """Decode a quoted token the way KiCad's DSNLEXER does; unquoted input is returned as-is.

    Decodes `\\a \\b \\f \\n \\r \\t \\v \\\\ \\"`, `\\xHH` and octal `\\NNN`; keeps unknown
    escapes verbatim (all verified against kicad-cli 10.0.0, see module docstring).
    """
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        s = s[1:-1]
    if "\\" not in s:
        return s
    return _ESCAPE.sub(_decode_escape, s)


def format_number(v: int | float) -> str:
    """Format a number as KiCad's `FormatDouble2Str` / `FormatInternalUnits` do.

    Integers print without a decimal point. Other values use `%.10g` (what the demos show:
    `1.27`, `-0.898426`, `172.085`); magnitudes at or below 1e-4 use `%.10f` with trailing zeros
    stripped so that no exponent is ever written; `-0` is normalised to `0`.
    """
    if isinstance(v, bool):
        raise TypeError("bool is not a number; use Atom.symbol('yes'/'no')")
    if isinstance(v, int):
        return str(v)
    if v != v or v in (float("inf"), float("-inf")):
        raise ValueError(f"cannot format {v!r} as a KiCad number")
    if v == 0.0:
        return "0"
    if v.is_integer() and abs(v) < 1e15:
        return str(int(v))
    if abs(v) <= 1e-4:
        text = f"{v:.10f}".rstrip("0").rstrip(".")
    else:
        text = f"{v:.10g}"
        if "e" in text or "E" in text:
            text = f"{v:.10f}".rstrip("0").rstrip(".")
    if text in ("-0", ""):
        return "0"
    return text


# ----------------------------------------------------------------------------------------------
# tree
# ----------------------------------------------------------------------------------------------


class Atom:
    """A scalar token. `raw` is the exact source text; `value` the decoded content.

    `kind` is "string" (was quoted), "number", "symbol" or "comment" (a `#…` line, only legal
    where KiCad's lexer accepts one: at the start of a line, e.g. in .kicad_dru). Assigning
    `value` re-encodes `raw` for the atom's kind (a str assigned to a number atom is taken as
    already formatted; an int or float turns the atom into a number).
    """

    __slots__ = ("kind", "lead", "parent", "raw")

    def __init__(self, raw: str, kind: str | None = None, lead: str = "") -> None:
        self.raw = raw
        self.kind = kind or _kind_of(raw)
        self.lead = lead
        self.parent: Node | None = None

    # constructors -----------------------------------------------------------------------------
    @classmethod
    def string(cls, value: str) -> Atom:
        return cls(quote(value), "string")

    @classmethod
    def symbol(cls, name: str) -> Atom:
        if not name or re.search(r'[\s()"]', name):
            raise ValueError(f"not a valid bare symbol: {name!r}")
        return cls(name, "symbol")

    @classmethod
    def comment(cls, text: str) -> Atom:
        """A `# text` line comment (KiCad only reads these at the start of a line)."""
        if "\n" in text or "\r" in text:
            raise ValueError("a comment cannot span lines")
        return cls(text if text.startswith("#") else "#" + text, "comment")

    @classmethod
    def number(cls, v: int | float | str) -> Atom:
        raw = v if isinstance(v, str) else format_number(v)
        if not _NUMBER.match(raw):
            raise ValueError(f"not a KiCad number: {raw!r}")
        return cls(raw, "number")

    # value ------------------------------------------------------------------------------------
    @property
    def value(self) -> str:
        return unquote(self.raw) if self.kind == "string" else self.raw

    @value.setter
    def value(self, v: str | int | float) -> None:
        if isinstance(v, bool):
            raise TypeError("assign 'yes'/'no' to a symbol atom instead of a bool")
        if isinstance(v, int | float):
            self.raw = format_number(v)
            self.kind = "number"
        elif self.kind == "string":
            self.raw = quote(v)
        elif self.kind == "symbol":
            self.raw = Atom.symbol(v).raw
        elif self.kind == "comment":
            self.raw = Atom.comment(v).raw
        else:
            self.raw = Atom.number(v).raw

    def as_str(self) -> str:
        return self.value

    def as_float(self) -> float:
        try:
            return float(self.value)
        except ValueError:
            raise ValueError(f"atom {self.raw!r} is not numeric") from None

    def as_int(self) -> int:
        v = self.value
        try:
            return int(v)
        except ValueError:
            f = self.as_float()
            if f.is_integer():
                return int(f)
            raise ValueError(f"atom {self.raw!r} is not an integer") from None

    def copy(self) -> Atom:
        return Atom(self.raw, self.kind, self.lead)

    def __repr__(self) -> str:
        return f"Atom({self.raw!r})"

    def __eq__(self, other: object) -> bool:  # value equality (whitespace ignored)
        return isinstance(other, Atom) and other.raw == self.raw

    def __hash__(self) -> int:
        return hash(self.raw)


def _kind_of(raw: str) -> str:
    if raw.startswith('"'):
        return "string"
    if _NUMBER.match(raw):
        return "number"
    return "symbol"


class Node:
    """A parenthesised list. `children` holds every element including the head atom.

    Whitespace bookkeeping: `lead` is the text before this node's `(`, `tail` the text before its
    `)`, `suffix` the text after `)` (only ever non-empty on the document root).

    A document with several top-level expressions (`.kicad_dru`: `(version 1)` then one
    `(rule …)` after another, plus `#` comment lines) parses into an *implicit* node: no parens
    of its own, `head == ""`, `children` are the top-level elements, `tail` the trailing text.
    """

    __slots__ = ("_fresh", "children", "implicit", "lead", "parent", "suffix", "tail")

    def __init__(
        self, children: Iterable[Atom | Node] = (), lead: str = "", tail: str = "", *, implicit: bool = False
    ) -> None:
        self.children: list[Atom | Node] = []
        self.lead = lead
        self.tail = tail
        self.suffix = ""
        self.parent: Node | None = None
        self._fresh = False
        self.implicit = implicit
        for c in children:
            self._adopt(c)
            self.children.append(c)

    # structure --------------------------------------------------------------------------------
    @property
    def head(self) -> str:
        """The first atom's value when it is a symbol (`"symbol"`, `"at"`), else ""."""
        if self.children:
            first = self.children[0]
            if isinstance(first, Atom) and first.kind == "symbol":
                return first.raw
        return ""

    @property
    def args(self) -> list[Atom | Node]:
        """Everything after the head atom."""
        return self.children[1:] if self.head else list(self.children)

    @property
    def atoms(self) -> list[Atom]:
        """Scalar arguments (after the head, comments skipped), in order."""
        return [c for c in self.args if isinstance(c, Atom) and c.kind != "comment"]

    @property
    def nodes(self) -> list[Node]:
        """Child lists, in order."""
        return [c for c in self.children if isinstance(c, Node)]

    def values(self) -> list[str]:
        """Decoded values of the scalar arguments."""
        return [a.value for a in self.atoms]

    def find(self, head: str) -> Node | None:
        """First child list whose head is `head`."""
        for c in self.children:
            if isinstance(c, Node) and c.head == head:
                return c
        return None

    def find_all(self, head: str) -> list[Node]:
        return [c for c in self.children if isinstance(c, Node) and c.head == head]

    def walk(self) -> Iterator[Node]:
        """Depth-first iteration over this node and every descendant list."""
        yield self
        for c in self.children:
            if isinstance(c, Node):
                yield from c.walk()

    def get(self, head: str, default: str | None = None) -> str | None:
        """Value of the first scalar argument of child `(head …)`: `(version 20260306)` -> "20260306"."""
        child = self.find(head)
        if child is None:
            return default
        atoms = child.atoms
        return atoms[0].value if atoms else default

    def depth(self) -> int:
        """Nesting depth used for indentation (an implicit document is -1 so its rules sit at 0)."""
        d = -1 if self.implicit else 0
        p = self.parent
        while p is not None:
            d += 1
            p = p.parent
        return d

    def root(self) -> Node:
        n = self
        while n.parent is not None:
            n = n.parent
        return n

    def index(self, child: Atom | Node) -> int:
        for i, c in enumerate(self.children):
            if c is child:
                return i
        raise ValueError("child not in node")

    # mutation ---------------------------------------------------------------------------------
    @overload
    def append(self, *items: Node) -> Node: ...
    @overload
    def append(self, *items: Value) -> Atom | Node: ...

    def append(self, *items: Value) -> Atom | Node:
        """Append elements (raw values are coerced like `build()`); returns the last one added."""
        last: Atom | Node | None = None
        for item in items:
            last = self.insert(len(self.children), item)
        if last is None:
            raise ValueError("append() needs at least one item")
        return last

    @overload
    def insert(self, index: int, item: Node) -> Node: ...
    @overload
    def insert(self, index: int, item: Value) -> Atom | Node: ...

    def insert(self, index: int, item: Value) -> Atom | Node:
        elem = _coerce(item)
        if elem.parent is not None:
            elem.parent.remove(elem)
        if index < 0:
            index += len(self.children)
        index = max(0, min(index, len(self.children)))
        self.children.insert(index, elem)
        self._adopt(elem)
        self._layout_inserted(index)
        return elem

    def remove(self, child: Atom | Node) -> None:
        i = self.index(child)
        del self.children[i]
        child.parent = None
        child.lead = ""  # a detached element carries no context whitespace
        if i == 0 and self.children:
            self.children[0].lead = ""
        if not self.nodes and "\n" in self.tail:
            self.tail = ""  # KiCad closes a scalar-only list inline

    def replace(self, child: Atom | Node, new: Value) -> Atom | Node:
        """Swap `child` for `new` in place; `new` inherits the old element's leading whitespace."""
        i = self.index(child)
        elem = _coerce(new)
        if elem.parent is not None:
            elem.parent.remove(elem)
        self.children[i] = elem
        child.parent = None
        self._adopt(elem)
        style = _style_of(self.root())
        if isinstance(elem, Node) and elem._fresh:
            elem._layout(self.depth() + 1, style)
        elem.lead = child.lead
        became_compound = isinstance(elem, Node) and not isinstance(child, Node) and len(self.nodes) == 1
        if became_compound and "\n" not in self.tail:
            # the list just turned from scalar-only into compound: close it on its own line
            self.tail = style.newline + style.indent * self.depth()
            if "\n" not in elem.lead:
                elem.lead = style.newline + style.indent * (self.depth() + 1)
        return elem

    def set(self, head: str, *values: Value) -> Node:
        """Set `(head values…)`: rewrite the scalar arguments of the existing child or add it.

        Only the atoms whose text actually changes are touched, so an unchanged file stays
        byte-identical.
        """
        child = self.find(head)
        if child is None:
            node = build(head, *values)
            self.append(node)
            return node
        new_atoms = [_coerce(v) for v in values]
        if any(isinstance(a, Node) for a in new_atoms):
            raise TypeError("set() takes scalar values only; use replace() for lists")
        old = child.atoms
        last: Atom | None = old[-1] if old else None
        for i, atom in enumerate(new_atoms):
            assert isinstance(atom, Atom)
            if i < len(old):
                if old[i].raw != atom.raw:
                    old[i].raw = atom.raw
                    old[i].kind = atom.kind
            else:
                pos = child.index(last) + 1 if last is not None else 1
                inserted = child.insert(pos, atom)
                assert isinstance(inserted, Atom)
                last = inserted
        for extra in old[len(new_atoms) :]:
            child.remove(extra)
        return child

    def reformat(self) -> Node:
        """Re-lay out this subtree KiCad-style (Prettify rules) at its current depth.

        Moved or copied subtrees keep their original whitespace; call this to make one match its
        new position. A detached node is laid out the next time it is inserted.
        """
        if self.parent is None:
            self._fresh = True
            return self
        style = _style_of(self.root())
        self._layout(self.depth(), style)
        i = self.parent.index(self)
        if i > 0:
            self.lead = style.newline + style.indent * self.depth()
        return self

    # helpers ----------------------------------------------------------------------------------
    def _adopt(self, elem: Atom | Node) -> None:
        elem.parent = self

    def _layout_inserted(self, index: int) -> None:
        """Give a newly inserted element KiCad-style whitespace at its position."""
        elem = self.children[index]
        style = _style_of(self.root())
        indent, nl = style.indent, style.newline
        depth = self.depth()
        if isinstance(elem, Atom):
            if index == 0:
                elem.lead = ""
            elif elem.kind == "comment":
                elem.lead = nl + indent * (depth + 1)
            else:
                elem.lead = " "
                before = self.children[index - 1]
                if isinstance(before, Node) and "\n" in before.lead:
                    elem.lead = nl + indent * (depth + 1)
            if index + 1 < len(self.children):
                self.children[index + 1].lead = self.children[index + 1].lead or " "
            return
        if elem._fresh:
            elem._layout(depth + 1, style)
        prev: Atom | Node | None = self.children[index - 1] if index > 0 else None
        if index == 0:
            elem.lead = ""
        elif (
            isinstance(prev, Node)
            and prev.head == "xy"
            and elem.head == "xy"
            and _column_after(prev) < _XY_COLUMN_LIMIT
        ):
            elem.lead = " "
        else:
            elem.lead = nl + indent * (depth + 1)
        if len(self.nodes) == 1 and "\n" not in self.tail:
            # The list just turned from scalar-only into compound: close it on its own line. A
            # list that already had sub-lists keeps its closing style (7.0 files close inline) so
            # that remove() restores the original bytes.
            self.tail = nl + indent * depth if not self.implicit else nl
        nxt = self.children[index + 1] if index + 1 < len(self.children) else None
        if isinstance(nxt, Atom) and nxt.lead == " ":
            nxt.lead = nl + indent * (depth + 1)

    def _layout(self, depth: int, style: Style) -> None:
        """Format this subtree from scratch per KICAD_FORMAT::Prettify at nesting `depth`."""
        self._fresh = False
        indent, nl = style.indent, style.newline
        column = len(indent) * depth + 1  # after "("
        last_was_node = False
        multiline = False
        prev_xy = False
        for i, c in enumerate(self.children):
            if isinstance(c, Atom):
                if c.kind == "comment":
                    c.lead = nl + indent * (depth + 1)
                    column = len(indent) * (depth + 1) + len(c.raw)
                    multiline = True
                    last_was_node = False
                    prev_xy = False
                    continue
                if i == 0:
                    c.lead = ""
                elif last_was_node:
                    c.lead = " "
                elif column < _TOKEN_WRAP_COLUMN:
                    c.lead = " "
                    column += 1
                else:
                    c.lead = nl + indent * (depth + 1)
                    column = len(indent) * (depth + 1)
                    multiline = True
                column += len(c.raw)
                last_was_node = False
                prev_xy = False
            else:
                is_xy = c.head == "xy"
                if i == 0:
                    c.lead = ""
                elif prev_xy and is_xy and column < _XY_COLUMN_LIMIT:
                    c.lead = " "
                    column += 1
                else:
                    c.lead = nl + indent * (depth + 1)
                    column = len(indent) * (depth + 1)
                c._layout(depth + 1, style)
                column = column + _inline_len(c) if is_xy else len(indent) * (depth + 1) + 1
                last_was_node = True
                prev_xy = is_xy
        self.tail = nl + indent * depth if (last_was_node or multiline) else ""

    def copy(self) -> Node:
        """Deep copy, detached (inner formatting kept, no leading whitespace of its own)."""
        n = self._clone()
        n.lead = ""
        return n

    def _clone(self) -> Node:
        n = Node(
            [c.copy() if isinstance(c, Atom) else c._clone() for c in self.children],
            self.lead,
            self.tail,
            implicit=self.implicit,
        )
        n.suffix = self.suffix
        return n

    def __repr__(self) -> str:
        text = dumps(self)
        if len(text) > 60:
            text = text[:57] + "..."
        return f"Node({text!r})"

    def __iter__(self) -> Iterator[Atom | Node]:
        return iter(self.children)

    def __len__(self) -> int:
        return len(self.children)


def _inline_len(node: Node) -> int:
    """Length of a scalar-only list as written inline: `(xy 1 2)` -> 8."""
    n = 2
    for c in node.children:
        n += len(c.lead) + (len(c.raw) if isinstance(c, Atom) else _inline_len(c))
    return n + len(node.tail)


def _body_last_line(elem: Atom | Node) -> tuple[str, bool]:
    """(text of the last line of `elem` without its lead, whether the element spans lines)."""
    if isinstance(elem, Atom):
        return elem.raw, False
    text = dumps(elem)
    if "\n" in text:
        return text.rsplit("\n", 1)[-1], True
    return text, False


def _column_after(elem: Atom | Node) -> int:
    """Column just after `elem` on its line, tabs counting one (Prettify's `column`).

    Walks back over siblings only as far as the previous newline, so it stays cheap when many
    `(xy …)` points are appended one after another.
    """
    col = 0
    node: Atom | Node = elem
    while True:
        parent = node.parent
        if parent is None:
            last, _ = _body_last_line(node)
            return len(node.lead.rsplit("\n", 1)[-1]) + len(last)
        i = parent.index(node)
        for j in range(i, -1, -1):
            c = parent.children[j]
            last, multiline = _body_last_line(c)
            if multiline:
                return col + len(last)
            lead_last = c.lead.rsplit("\n", 1)[-1]
            col += len(lead_last) + len(last)
            if "\n" in c.lead:
                return col
        col += 1  # the parent's "("
        node = parent


class Style:
    """Whitespace conventions of a document: indent unit and newline sequence.

    KiCad 8.0+ indents with one tab per level, 7.0 with two spaces. KiCad on Windows writes
    CRLF line endings (the shipped footprints and every kicad-cli 10 `upgrade` output do; the
    demo projects checked in from git use LF), so fresh nodes copy whatever the tree uses.
    """

    __slots__ = ("indent", "newline")

    def __init__(self, indent: str = _DEFAULT_INDENT, newline: str = "\n") -> None:
        self.indent = indent
        self.newline = newline


_DEFAULT_STYLE = Style()


def _style_of(root: Node) -> Style:
    """Detect indent unit and newline sequence from the first indented child of `root`."""
    for c in root.children:
        if "\n" in c.lead:
            ws = c.lead.rsplit("\n", 1)[-1]
            nl = "\r\n" if "\r\n" in c.lead else "\n"
            return Style(ws or _DEFAULT_INDENT, nl)
    if "\r\n" in root.tail or "\r\n" in root.suffix:
        return Style(_DEFAULT_INDENT, "\r\n")
    return _DEFAULT_STYLE


def _coerce(item: Value) -> Atom | Node:
    if isinstance(item, Atom | Node):
        return item
    if isinstance(item, bool):
        return Atom.symbol("yes" if item else "no")
    if isinstance(item, int | float):
        return Atom.number(item)
    if isinstance(item, str):
        return Atom.string(item)
    raise TypeError(f"cannot place {type(item).__name__} in an s-expression")


def build(head: str, *args: Value) -> Node:
    """Construct `(head args…)` formatted KiCad-style once inserted into a tree.

    `str` becomes a quoted string, `int`/`float` a number, `bool` the symbols `yes`/`no`,
    `Atom.symbol("left")` a bare symbol; `Node`s nest. `build("at", 1, 2, 90)` -> `(at 1 2 90)`.
    """
    node = Node([Atom.symbol(head), *(_coerce(a) for a in args)])
    node._layout(0, _DEFAULT_STYLE)
    node._fresh = True
    return node


# ----------------------------------------------------------------------------------------------
# parse / serialise
# ----------------------------------------------------------------------------------------------


def _line_col(text: str, pos: int) -> tuple[int, int]:
    line = text.count("\n", 0, pos) + 1
    col = pos - (text.rfind("\n", 0, pos) + 1) + 1
    return line, col


def loads(text: str) -> Node:
    """Parse a document; raises `ParseError` with line/column on malformed input.

    A file with exactly one top-level list returns that list (its `lead`/`suffix` hold the text
    around it). Several top-level lists, or top-level `#` comments, return an implicit document
    node (see `Node`). A `#` starts a comment only when it is the first thing on its line — the
    same rule KiCad's lexer applies (verified: kicad-cli 10 loads a schematic with a comment
    line but rejects one with a comment after a token on the same line).
    """
    stack: list[Node] = []
    top: list[Atom | Node] = []
    pos = 0
    length = len(text)
    match = _TOKEN.match
    while pos < length:
        m = match(text, pos)
        if m is None or m.lastindex is None:
            rest = text[pos:]
            if rest.strip() == "":
                break
            bad = pos + (len(rest) - len(rest.lstrip()))
            what = "unterminated string" if text[bad] == '"' else f"unexpected character {text[bad]!r}"
            raise ParseError(what, *_line_col(text, bad))
        group = m.lastindex
        start = m.start(group)
        lead = text[pos:start]
        pos = m.end()
        if group == 1:
            node = Node(lead=lead)
            if stack:
                stack[-1].children.append(node)
                node.parent = stack[-1]
            else:
                top.append(node)
            stack.append(node)
        elif group == 2:
            if not stack:
                raise ParseError("unbalanced ')'", *_line_col(text, start))
            node = stack.pop()
            node.tail = lead
        else:
            raw = m.group(group)
            if group == 4 and not (start == 0 or "\n" in lead):
                # `#` after a token on the same line is an ordinary bare token to KiCad
                bare = _BARE.match(text, start)
                assert bare is not None
                raw = bare.group()
                pos = bare.end()
                group = 5
            kind = "string" if group == 3 else "comment" if group == 4 else _kind_of(raw)
            atom = Atom(raw, kind, lead)
            if stack:
                atom.parent = stack[-1]
                stack[-1].children.append(atom)
            elif kind == "comment":
                top.append(atom)
            else:
                raise ParseError("atom outside a list", *_line_col(text, start))
    if stack:
        raise ParseError("unterminated list", *_line_col(text, length))
    if not top:
        raise ParseError("empty document", 1, 1)
    if len(top) == 1 and isinstance(top[0], Node):
        root = top[0]
        root.suffix = text[pos:]
        return root
    doc = Node(top, tail=text[pos:], implicit=True)
    return doc


def load(path: str | Path) -> Node:
    """Read a UTF-8 file without newline translation (CRLF files stay CRLF through `dump`)."""
    with Path(path).open(encoding="utf-8", newline="") as fh:
        return loads(fh.read())


def _emit(node: Node, out: list[str]) -> None:
    out.append(node.lead)
    if not node.implicit:
        out.append("(")
    for c in node.children:
        if isinstance(c, Atom):
            out.append(c.lead)
            out.append(c.raw)
        else:
            _emit(c, out)
    out.append(node.tail)
    if not node.implicit:
        out.append(")")


def dumps(node: Node) -> str:
    """Serialise. For a document root this is byte-identical to the parsed text; for a subtree
    the node's own leading whitespace is omitted."""
    out: list[str] = []
    _emit(node, out)
    if node.parent is not None:
        out[0] = ""
    out.append(node.suffix)
    return "".join(out)


def dump(node: Node, path: str | Path) -> None:
    """Write UTF-8 with the tree's own line endings (no translation)."""
    with Path(path).open("w", encoding="utf-8", newline="") as fh:
        fh.write(dumps(node))
