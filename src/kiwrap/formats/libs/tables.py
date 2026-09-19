"""sym-lib-table / fp-lib-table resolution: parsing, variable expansion, nested tables, precedence.

What this encodes (KiCad sources at the bench commit, `common/libraries/library_manager.cpp`,
`common/common.cpp` `KIwxExpandEnvVars`, `include/libraries/library_table_grammar.h`):

* A row is `(lib (name N) (type T) (uri U) (options O) (descr D) [(hidden)] [(disabled)])`;
  property values may be quoted or bare tokens. `hidden` / `disabled` are bare flags (8.0+);
  a `(hidden yes)` style is accepted too.
* Row type `Table` (10.0+) nests another table file: its URI is expanded and, when relative,
  taken relative to the parent table's directory; a disabled nested table contributes nothing,
  a hidden one hides every row it contributes; a table already loaded is skipped (cycle guard).
* Rows are keyed by nickname; a later table wins (`rows[nickname] = row`) while the order of
  first appearance is kept - so the project table shadows the global table row by row, and a
  nested table's rows are inlined at the position of the `Table` row.
* URI expansion: `${VAR}`, `$(VAR)`, `$VAR` (and `%VAR%` on Windows) from the process
  environment KiCad would see (the sandbox `ConfigRoot.env()` - user overrides included),
  `${KIPRJMOD}` = the project directory, and the 8.0+ fallback that maps an unmatched
  `KICAD<digits>_SYMBOL_DIR` / `_FOOTPRINT_DIR` / `_3DMODEL_DIR` / `_3RD_PARTY` (plus the legacy
  `KICAD_SYMBOL_DIR` / `KISYS3DMOD`) onto the running version's variable. An unknown variable is
  left verbatim (KiCad does the same; the path then simply does not exist).
* `~` at the start of a URI is the home directory (wxFileName::Normalize / KiCad's file dialogs
  write it that way on Linux; harmless on Windows).

Global tables come from the sandbox root's versioned directory (`kiwrap.sandbox` seeds them
from the install's template and merges the user's rows read-only, decision D7); without a
sandbox root the install's `share/kicad/template/*-lib-table` is read directly.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

from kiwrap.formats.libs.models import InstallLike, LibraryTables, LibTableRow
from kiwrap.formats.sexpr import Node, ParseError, loads

__all__ = [
    "TABLE_FILES",
    "expand_uri",
    "library_env",
    "load_tables",
    "parse_table",
    "read_table",
]

TABLE_FILES = {"sym": "sym-lib-table", "fp": "fp-lib-table"}
TABLE_HEADS = {"sym": "sym_lib_table", "fp": "fp_lib_table"}
_TABLE_TYPE = "table"

#: `KICAD<major>_<suffix>` -> directory under the install's share (kiwrap.sandbox exports the same)
SHARE_VARS: tuple[tuple[str, str], ...] = (
    ("SYMBOL_DIR", "symbols"),
    ("FOOTPRINT_DIR", "footprints"),
    ("3DMODEL_DIR", "3dmodels"),
    ("TEMPLATE_DIR", "template"),
)
_VERSIONED_RE = re.compile(
    r"^KICAD\d*_(?P<suffix>SYMBOL_DIR|FOOTPRINT_DIR|3DMODEL_DIR|3RD_PARTY|TEMPLATE_DIR)$"
)
_LEGACY_NAMES = {"KICAD_SYMBOL_DIR": "SYMBOL_DIR", "KISYS3DMOD": "3DMODEL_DIR", "KISYSMOD": "FOOTPRINT_DIR"}
_VAR_RE = re.compile(
    r"\$\{(?P<curly>[^}]+)\}|\$\((?P<paren>[^)]+)\)|%(?P<win>[^%\s/\\]+)%|\$(?P<bare>[A-Za-z_][A-Za-z0-9_]*)"
)


def library_env(
    install: InstallLike,
    base: Mapping[str, str] | None,
    project_dir: Path | None,
    *,
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """The variable map URIs are expanded with: `base` (the sandbox env, or os.environ) with the
    install's own `KICAD<major>_*` defaults filled in where missing, `KIPRJMOD` for the project
    and `extra` (project text variables from the .kicad_pro) on top."""
    env: dict[str, str] = dict(os.environ if base is None else base)
    prefix = f"KICAD{install.major}_"
    for suffix, sub in SHARE_VARS:
        env.setdefault(prefix + suffix, str(install.share / sub))
    if project_dir is not None:
        env["KIPRJMOD"] = str(Path(project_dir))
    if extra:
        env.update({str(k): str(v) for k, v in extra.items()})
    return env


def _lookup(name: str, env: Mapping[str, str], major: int) -> str | None:
    if name in env:
        return env[name]
    suffix = _LEGACY_NAMES.get(name)
    if suffix is None:
        m = _VERSIONED_RE.match(name)
        suffix = m.group("suffix") if m else None
    if suffix is None:
        return None
    exact = f"KICAD{major}_{suffix}"
    if exact in env:
        return env[exact]
    for key, value in env.items():  # any other KICAD<n>_<suffix> the user defined
        mm = _VERSIONED_RE.match(key)
        if mm and mm.group("suffix") == suffix:
            return value
    return None


def expand_uri(uri: str, env: Mapping[str, str], *, major: int = 0) -> tuple[str, list[str]]:
    """Expand the variables of `uri`; returns (text, unresolved variable names)."""
    unresolved: list[str] = []

    def sub(m: re.Match[str]) -> str:
        name = m.group("curly") or m.group("paren") or m.group("win") or m.group("bare") or ""
        value = _lookup(name, env, major)
        if value is None:
            unresolved.append(name)
            return m.group(0)
        return value

    text = _VAR_RE.sub(sub, uri)
    if text.startswith("~"):
        text = str(Path.home()) + text[1:]
    return text, unresolved


# ----------------------------------------------------------------------------------------------
# parsing
# ----------------------------------------------------------------------------------------------


def _row_fields(node: Node) -> tuple[dict[str, str], bool, bool]:
    fields: dict[str, str] = {}
    hidden = disabled = False
    for child in node.nodes:
        head = child.head
        atoms = child.atoms
        if head in ("hidden", "disabled"):
            flag = not atoms or atoms[0].value.lower() not in ("no", "false", "0")
            if head == "hidden":
                hidden = flag
            else:
                disabled = flag
        elif head and atoms:
            fields[head] = atoms[0].value
        elif head:
            fields[head] = ""
    return fields, hidden, disabled


def parse_table(text: str, *, source: Path | None = None, scope: str = "global") -> list[LibTableRow]:
    """Rows of a table document (unexpanded URIs). Raises `ValueError` on a document that does
    not parse or whose head is not a lib table."""
    try:
        root = loads(text)
    except ParseError as e:
        raise ValueError(f"{source or '<table>'}: {e}") from e
    if root.head not in TABLE_HEADS.values():
        raise ValueError(f"{source or '<table>'}: not a library table (head {root.head!r})")
    rows: list[LibTableRow] = []
    for lib in root.find_all("lib"):
        fields, hidden, disabled = _row_fields(lib)
        rows.append(
            LibTableRow(
                name=fields.get("name", ""),
                type=fields.get("type", ""),
                uri=fields.get("uri", ""),
                options=fields.get("options", ""),
                descr=fields.get("descr", ""),
                hidden=hidden,
                disabled=disabled,
                scope=scope,
                source=source,
            )
        )
    return rows


def read_table(path: Path, *, scope: str = "global") -> list[LibTableRow]:
    return parse_table(path.read_text(encoding="utf-8"), source=path, scope=scope)


# ----------------------------------------------------------------------------------------------
# resolution
# ----------------------------------------------------------------------------------------------

_SUPPORTED_TYPES = {"kicad"}


def _resolve_row(row: LibTableRow, env: Mapping[str, str], major: int, base_dir: Path | None) -> LibTableRow:
    text, unresolved = expand_uri(row.uri, env, major=major)
    error: str | None = None
    path: Path | None = None
    exists = False
    if unresolved:
        error = "unresolved variable(s): " + ", ".join(dict.fromkeys(unresolved))
    else:
        p = Path(text)
        if not p.is_absolute() and base_dir is not None:
            p = base_dir / p
        path = Path(os.path.normpath(p))
        exists = p.exists()
        if not exists:
            error = "path does not exist"
    if row.type.lower() not in _SUPPORTED_TYPES and row.type.lower() != _TABLE_TYPE:
        error = f"library type {row.type!r} is not readable by the file tier (only KiCad .kicad_sym/.pretty)"
    return LibTableRow(
        name=row.name,
        type=row.type,
        uri=row.uri,
        options=row.options,
        descr=row.descr,
        hidden=row.hidden,
        disabled=row.disabled,
        scope=row.scope,
        source=row.source,
        path=path,
        exists=exists,
        error=error,
    )


def _inline(
    rows: list[LibTableRow],
    env: Mapping[str, str],
    major: int,
    seen: set[str],
    sources: list[Path],
    *,
    parent_hidden: bool = False,
    problems: list[str],
) -> list[LibTableRow]:
    out: list[LibTableRow] = []
    for row in rows:
        base_dir = row.source.parent if row.source is not None else None
        resolved = _resolve_row(row, env, major, base_dir)
        if parent_hidden and not resolved.hidden:
            resolved = replace(resolved, hidden=True)
        if row.type.lower() == _TABLE_TYPE:
            if resolved.disabled or resolved.path is None:
                continue
            key = os.path.normcase(os.path.normpath(str(resolved.path)))
            if key in seen or not resolved.exists:
                if not resolved.exists:
                    problems.append(f"nested table {resolved.path} (from {row.source}) does not exist")
                continue
            seen.add(key)
            try:
                child = read_table(resolved.path, scope=row.scope)
            except (OSError, ValueError, UnicodeDecodeError) as e:
                problems.append(f"nested table {resolved.path}: {e}")
                continue
            sources.append(resolved.path)
            out.extend(
                _inline(child, env, major, seen, sources, parent_hidden=resolved.hidden, problems=problems)
            )
            continue
        out.append(resolved)
    return out


def _merge(*groups: list[LibTableRow]) -> tuple[LibTableRow, ...]:
    order: list[str] = []
    by_name: dict[str, LibTableRow] = {}
    for rows in groups:
        for r in rows:
            if r.name not in by_name:
                order.append(r.name)
            by_name[r.name] = r
    return tuple(by_name[n] for n in order)


def load_tables(
    install: InstallLike,
    *,
    global_dir: Path | None,
    project_dir: Path | None,
    env: Mapping[str, str],
    problems: list[str] | None = None,
) -> LibraryTables:
    """Resolve both tables. `global_dir` is the directory holding the global `sym-lib-table` /
    `fp-lib-table` (the sandbox root's versioned dir); None or a missing file falls back to
    the install's template tables. `problems` collects non-fatal issues (unreadable nested
    tables, ...)."""
    probs = problems if problems is not None else []
    sources: list[Path] = []
    resolved: dict[str, tuple[LibTableRow, ...]] = {}
    template_dir = install.share / "template"
    for kind, filename in TABLE_FILES.items():
        groups: list[list[LibTableRow]] = []
        seen: set[str] = set()
        global_path = global_dir / filename if global_dir is not None else None
        if global_path is None or not global_path.is_file():
            global_path = template_dir / filename
        if global_path.is_file():
            seen.add(os.path.normcase(os.path.normpath(str(global_path))))
            try:
                rows = read_table(global_path, scope="global")
            except (OSError, ValueError, UnicodeDecodeError) as e:
                probs.append(f"global {filename}: {e}")
                rows = []
            sources.append(global_path)
            groups.append(_inline(rows, env, install.major, seen, sources, problems=probs))
        if project_dir is not None:
            project_path = Path(project_dir) / filename
            if project_path.is_file():
                seen.add(os.path.normcase(os.path.normpath(str(project_path))))
                try:
                    rows = read_table(project_path, scope="project")
                except (OSError, ValueError, UnicodeDecodeError) as e:
                    probs.append(f"project {filename}: {e}")
                    rows = []
                sources.append(project_path)
                groups.append(_inline(rows, env, install.major, seen, sources, problems=probs))
        resolved[kind] = _merge(*groups)
    return LibraryTables(sym=resolved["sym"], fp=resolved["fp"], env=dict(env), sources=tuple(sources))
