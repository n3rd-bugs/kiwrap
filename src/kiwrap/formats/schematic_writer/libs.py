"""Library symbols for the writer: the resolver protocol, the verified `extends` flattening and a
minimal fallback resolver over `.kicad_sym` files.

The writer consumes any object with `symbol(lib_id) -> obj` where `obj.node` is the library
symbol as a `kiwrap.formats.sexpr.Node` with its `extends` chain already flattened (the
`kiwrap.formats.libs.LibResolver` contract). Pins, units and properties are re-derived from that
node here, so the writer does not depend on the resolver's own pin dataclass.

`FallbackLibResolver` is what tests and callers without `kiwrap.formats.libs` use: it maps a
library nickname to a `.kicad_sym` file by file stem under one or more symbol directories (the
install's `share/kicad/symbols`, the project directory, explicit dirs), plus the rows of a
`sym-lib-table` (project-local and/or a config root's) with `${VAR}` / `${KIPRJMOD}` expansion.

Flattening follows what KiCad's `LIB_SYMBOL::Flatten()` produces, verified byte-for-byte against
`kicad-cli sch upgrade --force` re-saves (docs/VERIFICATION.md claim 2, correction (a);
spikes/erc_verify/gen.py `flatten()`): the flattened parent's body, top-level name replaced by the
child's, unit blocks `Parent_U_S` renamed `Child_U_S`, the child's properties replacing the
parent's same-named ones in the parent's order, child-only properties inserted before the first
unit block, the child's own top-level flags (`exclude_from_sim`, `in_bom`, `on_board`,
`in_pos_files`, `power`, `pin_numbers`, `pin_names`) taking precedence when the child declares
them, and no `extends` left. An unflattened derived symbol is pinless in KiCad (claim 2).
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from kiwrap.formats.geometry import Vec2
from kiwrap.formats.schematic import LibPin, LibSymbol, parse_lib_symbols
from kiwrap.formats.sexpr import Atom, Node, load

from .model import WriterError

__all__ = [
    "FallbackLibResolver",
    "LibResolverLike",
    "ResolvedSymbol",
    "SymbolLibrary",
    "flatten_symbol",
    "parse_sym_lib_table",
    "split_lib_id",
]

_UNIT_RE = re.compile(r"^(?P<base>.*)_(?P<unit>\d+)_(?P<style>\d+)$")
_VAR_RE = re.compile(r"\$\{([^}]+)\}")
#: top-level flags of a derived symbol that override the parent's (LIB_SYMBOL::Flatten copies the
#: child's exclusion flags; pin_numbers / pin_names / power are per-symbol settings)
_CHILD_FLAGS = (
    "pin_numbers",
    "pin_names",
    "exclude_from_sim",
    "in_bom",
    "on_board",
    "in_pos_files",
    "duplicate_pin_numbers_are_jumpers",
    "power",
)


def split_lib_id(lib_id: str) -> tuple[str, str]:
    """`"Device:R"` -> `("Device", "R")`; a bare name has nickname ""."""
    if ":" in lib_id:
        nick, name = lib_id.split(":", 1)
        return nick, name
    return "", lib_id


@runtime_checkable
class LibResolverLike(Protocol):
    """What the writer needs from a library resolver (`kiwrap.formats.libs.LibResolver` or the
    fallback below). `symbol()` returns an object whose `.node` is the flattened symbol."""

    def symbol(self, lib_id: str) -> Any: ...


@dataclass(frozen=True)
class ResolvedSymbol:
    """A library symbol ready to embed: `node` is flattened, still named as in its library."""

    lib_id: str
    nickname: str
    name: str
    node: Node
    units: int
    pins: tuple[LibPin, ...]
    properties: dict[str, str]
    model: LibSymbol | None = field(default=None, repr=False, compare=False)

    @property
    def is_power(self) -> bool:
        return self.model is not None and self.model.is_power

    @property
    def reference_prefix(self) -> str:
        return self.properties.get("Reference", "U")

    def pins_for(self, unit: int, body_style: int = 1) -> list[LibPin]:
        return [p for p in self.pins if p.unit in (0, unit) and p.body_style in (0, body_style)]


def describe_symbol(node: Node, lib_id: str) -> ResolvedSymbol:
    """Build a `ResolvedSymbol` from a flattened node (pins via the schematic reader's parser)."""
    nickname, name = split_lib_id(lib_id)
    atoms = node.atoms
    node_name = atoms[0].value if atoms else name
    wrapper = Node([Atom.symbol("lib_symbols")])
    copy = node.copy()
    wrapper.append(copy)
    parsed = parse_lib_symbols(wrapper)
    model = parsed.get(node_name)
    if model is None:
        raise WriterError("E_LIB_SYMBOL", f"cannot parse library symbol {lib_id!r}")
    return ResolvedSymbol(
        lib_id=lib_id,
        nickname=nickname,
        name=name,
        node=node,
        units=model.unit_count,
        pins=model.pins,
        properties=dict(model.properties),
        model=model,
    )


# ----------------------------------------------------------------------------------------------
# flattening
# ----------------------------------------------------------------------------------------------


def _symbol_name(node: Node) -> str:
    atoms = node.atoms
    return atoms[0].value if atoms else ""


def flatten_symbol(name: str, lookup: Callable[[str], Node | None], *, _chain: tuple[str, ...] = ()) -> Node:
    """Return a detached, flattened copy of library symbol `name` (see the module docstring).

    `lookup(name)` returns the raw `(symbol "name" …)` node of the same library or None."""
    raw = lookup(name)
    if raw is None:
        raise WriterError("E_LIB_SYMBOL", f"library symbol {name!r} not found", candidates=list(_chain))
    if name in _chain:
        raise WriterError("E_LIB_SYMBOL", f"extends cycle at {name!r}: {' -> '.join((*_chain, name))}")
    parent_name = raw.get("extends")
    if not parent_name:
        return raw.copy()
    parent = flatten_symbol(parent_name, lookup, _chain=(*_chain, name))
    child = raw.copy()
    out = Node([Atom.symbol("symbol"), Atom.string(name)])
    child_props: dict[str, Node] = {}
    for p in child.find_all("property"):
        pa = p.atoms
        if pa:
            child_props[pa[0].value] = p
    child_flags: dict[str, Node] = {}
    for head in _CHILD_FLAGS:
        node = child.find(head)
        if node is not None:
            child_flags[head] = node
    parent_base = _symbol_name(parent)
    for c in parent.nodes:
        head = c.head
        if head == "property":
            pname = c.atoms[0].value if c.atoms else ""
            if pname in child_props:
                out.append(child_props.pop(pname).copy())
            else:
                out.append(c.copy())
        elif head == "symbol":
            unit = c.copy()
            ua = unit.atoms
            uname = ua[0].value if ua else ""
            if uname.startswith(parent_base + "_"):
                ua[0].value = name + uname[len(parent_base) :]
            out.append(unit)
        elif head == "extends":
            continue
        elif head in child_flags:
            out.append(child_flags.pop(head).copy())
        else:
            out.append(c.copy())
    # flags the child declares but the parent did not: after the last flag-ish child, before properties
    for node in child_flags.values():
        idx = next(
            (i for i, c in enumerate(out.children) if isinstance(c, Node) and c.head == "property"), None
        )
        out.insert(idx if idx is not None else len(out.children), node.copy())
    # child-only properties (ki_fp_filters …) go before the first unit block
    for p in child_props.values():
        idx = next(
            (i for i, c in enumerate(out.children) if isinstance(c, Node) and c.head == "symbol"),
            len(out.children),
        )
        out.insert(idx, p.copy())
    out.reformat()
    return out


# ----------------------------------------------------------------------------------------------
# fallback resolver
# ----------------------------------------------------------------------------------------------


@dataclass
class SymbolLibrary:
    """One `.kicad_sym` file, parsed on first use."""

    nickname: str
    path: Path
    _root: Node | None = field(default=None, repr=False)
    _index: dict[str, Node] = field(default_factory=dict, repr=False)

    @property
    def root(self) -> Node:
        if self._root is None:
            self._root = load(self.path)
            for sym in self._root.find_all("symbol"):
                a = sym.atoms
                if a:
                    self._index[a[0].value] = sym
        return self._root

    @property
    def stamp(self) -> int | None:
        text = self.root.get("version")
        return int(text) if text and text.isdigit() else None

    def raw(self, name: str) -> Node | None:
        self.root  # noqa: B018 - ensure parsed
        return self._index.get(name)

    def names(self) -> list[str]:
        self.root  # noqa: B018
        return list(self._index)

    def flattened(self, name: str) -> Node:
        return flatten_symbol(name, self.raw)


def _expand_vars(uri: str, env: Mapping[str, str]) -> str:
    def sub(m: re.Match[str]) -> str:
        key = m.group(1)
        return env.get(key, m.group(0))

    return _VAR_RE.sub(sub, uri)


def parse_sym_lib_table(path: Path, env: Mapping[str, str] | None = None) -> dict[str, Path]:
    """`sym-lib-table` rows -> {nickname: resolved .kicad_sym path}; nested `(table …)` rows
    (KiCad 10+) are followed. `${VAR}` names come from `env` (defaults to `os.environ`)."""
    env = dict(os.environ if env is None else env)
    out: dict[str, Path] = {}
    if not path.is_file():
        return out
    root = load(path)

    def walk(node: Node) -> None:
        for lib in node.find_all("lib"):
            nick = lib.get("name")
            uri = lib.get("uri")
            if lib.get("disabled") in ("true", "yes"):
                continue
            if nick and uri:
                out[nick] = Path(_expand_vars(uri, env))
        for table in node.find_all("table"):
            uri = table.get("uri")
            if uri:
                nested = Path(_expand_vars(uri, env))
                out.update(parse_sym_lib_table(nested, env))
            walk(table)

    walk(root)
    return out


class FallbackLibResolver:
    """Nickname -> `.kicad_sym` by file stem (plus lib-table rows); `symbol(lib_id)` flattens.

    `install` may be a `KiCadInstallation`-like object (`.dir("symbols")`), a `Path` to a
    symbols directory or an install root, or None. `config_root` may be a `ConfigRoot`-like
    object (`.versioned` dir holding `sym-lib-table`) or a `Path` to such a directory.
    `project_dir` contributes `<project>/sym-lib-table` (with `KIPRJMOD` set) and any
    `.kicad_sym` files directly inside it."""

    def __init__(
        self,
        install: Any = None,
        config_root: Any = None,
        project_dir: Path | None = None,
        *,
        symbol_dirs: Iterable[Path] = (),
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._libs: dict[str, SymbolLibrary] = {}
        self._cache: dict[str, ResolvedSymbol] = {}
        env_map = dict(os.environ if env is None else env)
        dirs: list[Path] = [Path(d) for d in symbol_dirs]
        for d in self._install_symbol_dirs(install):
            dirs.append(d)
        if project_dir is not None:
            project_dir = Path(project_dir)
            env_map.setdefault("KIPRJMOD", str(project_dir))
            dirs.append(project_dir)
        for d in dirs:
            self._add_dir(d)
        tables: list[Path] = []
        if project_dir is not None:
            tables.append(Path(project_dir) / "sym-lib-table")
        if config_root is not None:
            versioned = getattr(config_root, "versioned", None)
            base = Path(versioned) if versioned is not None else Path(config_root)
            tables.append(base / "sym-lib-table")
        for t in tables:
            for nick, p in parse_sym_lib_table(t, env_map).items():
                if p.is_file():
                    self._libs[nick] = SymbolLibrary(nick, p)

    @staticmethod
    def _install_symbol_dirs(install: Any) -> list[Path]:
        if install is None:
            return []
        if isinstance(install, str | Path):
            p = Path(install)
            candidates = [p, p / "symbols", p / "share" / "kicad" / "symbols"]
            return [c for c in candidates if c.is_dir() and any(c.glob("*.kicad_sym"))][:1]
        dir_fn = getattr(install, "dir", None)
        if callable(dir_fn):
            try:
                d = Path(dir_fn("symbols"))
            except Exception:  # noqa: BLE001 - duck typing
                d = None
            if d is not None and d.is_dir():
                return [d]
        share = getattr(install, "share", None)
        if share is not None and (Path(share) / "symbols").is_dir():
            return [Path(share) / "symbols"]
        return []

    def _add_dir(self, d: Path) -> None:
        if not d.is_dir():
            return
        for f in sorted(d.glob("*.kicad_sym")):
            self._libs.setdefault(f.stem, SymbolLibrary(f.stem, f))

    # public --------------------------------------------------------------------------------------
    @property
    def nicknames(self) -> list[str]:
        return sorted(self._libs)

    def library(self, nickname: str) -> SymbolLibrary:
        lib = self._libs.get(nickname)
        if lib is None:
            raise WriterError(
                "E_LIB_NOT_FOUND",
                f"symbol library {nickname!r} is not known",
                fix="use a nickname from the install's symbol libraries or add a sym-lib-table row",
                candidates=self.nicknames[:20],
            )
        return lib

    def symbol(self, lib_id: str) -> ResolvedSymbol:
        if lib_id in self._cache:
            return self._cache[lib_id]
        nickname, name = split_lib_id(lib_id)
        if not nickname:
            raise WriterError("E_LIB_ID", f"lib_id {lib_id!r} needs a library nickname: 'Device:R'")
        lib = self.library(nickname)
        if lib.raw(name) is None:
            close = [n for n in lib.names() if name.lower() in n.lower()][:20]
            raise WriterError(
                "E_SYMBOL_NOT_FOUND",
                f"symbol {name!r} is not in library {nickname!r}",
                fix="search the library for the exact name",
                candidates=close,
            )
        node = lib.flattened(name)
        resolved = describe_symbol(node, lib_id)
        self._cache[lib_id] = resolved
        return resolved

    def search(self, query: str, *, limit: int = 20) -> list[str]:
        """Case-insensitive substring search over every known library -> lib_ids."""
        q = query.lower()
        out: list[str] = []
        for nick in self.nicknames:
            for n in self._libs[nick].names():
                if q in n.lower():
                    out.append(f"{nick}:{n}")
                    if len(out) >= limit:
                        return out
        return out


def lib_pin_position(pin: LibPin) -> Vec2:
    return pin.position
