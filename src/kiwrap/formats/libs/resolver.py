"""`LibResolver`: lib_id / fpid -> parsed library symbol / footprint through the resolved tables.

    resolver = LibResolver(install, config_root, project_dir)
    sym = resolver.symbol("Device:R")            # LibSymbol (flattened node, pins in mm, Y-up)
    fp = resolver.footprint("Resistor_SMD:R_0603_1608Metric")
    tables = resolver.tables()                   # {"sym": [rows], "fp": [rows]}

`install` is anything shaped like `kiwrap.installation.KiCadInstallation` (`share`, `major`,
`version`, `root`); `config_root` a `kiwrap.sandbox.ConfigRoot` (its `versioned` dir holds the
merged global tables and `env()` the KiCad process environment), a plain directory holding the
tables, or None (the install's template tables). `project_dir` adds the project's own tables
and `${KIPRJMOD}`; `env` overrides the variable map (tests, or a caller that already built it).

Parsed libraries are cached per file and invalidated on size/mtime change, so a resolver can
live for the whole server process. Nothing here touches the user's KiCad config.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from kiwrap.formats.libs.footprints import footprint_names, load_footprint
from kiwrap.formats.libs.models import (
    ConfigRootLike,
    InstallLike,
    LibError,
    LibFootprint,
    LibraryTables,
    LibSymbol,
    LibTableRow,
    NotFound,
    UnsupportedLibrary,
    split_lib_id,
)
from kiwrap.formats.libs.symbols import SymbolLibrary
from kiwrap.formats.libs.tables import library_env, load_tables

__all__ = ["LibResolver", "project_text_vars"]


def project_text_vars(project_dir: Path | None) -> dict[str, str]:
    """`text_variables` of the project's `.kicad_pro` (KiCad resolves `${NAME}` in URIs through the
    project's TextVarResolver before the environment)."""
    if project_dir is None:
        return {}
    try:
        pros = sorted(Path(project_dir).glob("*.kicad_pro"))
    except OSError:
        return {}
    for pro in pros:
        try:
            doc = json.loads(pro.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        vars_ = doc.get("text_variables") if isinstance(doc, dict) else None
        if isinstance(vars_, dict):
            return {str(k): str(v) for k, v in vars_.items()}
    return {}


class LibResolver:
    def __init__(
        self,
        install: InstallLike,
        config_root: ConfigRootLike | Path | None = None,
        project_dir: Path | None = None,
        *,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.install = install
        self.config_root = config_root
        self.project_dir = Path(project_dir) if project_dir is not None else None
        self._env_override = dict(env) if env is not None else None
        self._tables: LibraryTables | None = None
        self._tables_key: tuple[tuple[str, int, int], ...] | None = None
        self._sym_cache: dict[str, tuple[tuple[int, int], SymbolLibrary]] = {}
        self._fp_cache: dict[str, tuple[tuple[int, int], LibFootprint]] = {}
        self.problems: list[str] = []

    # ---- tables -------------------------------------------------------------------------------

    @property
    def global_dir(self) -> Path | None:
        cr = self.config_root
        if cr is None:
            return None
        if isinstance(cr, Path):
            return cr
        versioned = getattr(cr, "versioned", None)
        return Path(versioned) if versioned is not None else None

    def env(self) -> dict[str, str]:
        base: Mapping[str, str] | None = self._env_override
        if base is None:
            cr = self.config_root
            env_fn = getattr(cr, "env", None) if cr is not None and not isinstance(cr, Path) else None
            if callable(env_fn):
                try:
                    base = env_fn()
                except Exception:  # noqa: BLE001 - a sandbox that cannot build its env is not fatal here
                    base = None
        return library_env(self.install, base, self.project_dir, extra=project_text_vars(self.project_dir))

    def _sources_key(self, tables: LibraryTables) -> tuple[tuple[str, int, int], ...]:
        out = []
        for p in tables.sources:
            try:
                st = p.stat()
                out.append((str(p), st.st_size, st.st_mtime_ns))
            except OSError:
                out.append((str(p), -1, -1))
        return tuple(out)

    def library_tables(self, *, refresh: bool = False) -> LibraryTables:
        """Both resolved tables (cached until one of the table files changes)."""
        if self._tables is not None and not refresh and self._sources_key(self._tables) == self._tables_key:
            return self._tables
        problems: list[str] = []
        tables = load_tables(
            self.install,
            global_dir=self.global_dir,
            project_dir=self.project_dir,
            env=self.env(),
            problems=problems,
        )
        self.problems = problems
        self._tables = tables
        self._tables_key = self._sources_key(tables)
        return tables

    def tables(self) -> dict[str, list[dict[str, Any]]]:
        """Contract form: `{"sym": [row dicts], "fp": [row dicts]}`."""
        return self.library_tables().as_dict()

    def rows(self, kind: str) -> tuple[LibTableRow, ...]:
        t = self.library_tables()
        return t.sym if kind == "sym" else t.fp

    def tables_digest(self) -> str:
        """Digest of the resolved rows (nickname, type, path, flags): the index cache key."""
        t = self.library_tables()
        h = hashlib.sha256()
        h.update(f"{self.install.version}|{self.install.root}".encode())
        for kind in ("sym", "fp"):
            for r in t.sym if kind == "sym" else t.fp:
                h.update(f"{kind}|{r.name}|{r.type}|{r.path}|{r.hidden}|{r.disabled}\n".encode())
        return h.hexdigest()[:16]

    def _row(self, kind: str, nickname: str) -> LibTableRow:
        table = self.rows(kind)
        row = next((r for r in table if r.name == nickname), None)
        if row is None:
            low = nickname.lower()
            cands = [r.name for r in table if low in r.name.lower() or r.name.lower() in low][:10]
            what = "symbol library" if kind == "sym" else "footprint library"
            raise NotFound(
                f"{what} nickname {nickname!r} is not in the {'sym' if kind == 'sym' else 'fp'}-lib-table",
                why="neither the global table (sandbox root) nor the project table defines it",
                fix="use a nickname from kw_lib_search / LibResolver.tables(), or add the library to the "
                "project's table",
                candidates=cands,
            )
        if row.disabled:
            raise UnsupportedLibrary(
                f"library {nickname!r} is disabled in its table",
                why="KiCad does not load disabled rows",
                fix="enable the row in the library table or pick another library",
            )
        if not row.is_kicad:
            raise UnsupportedLibrary(
                f"library {nickname!r} has type {row.type!r}, which the file tier cannot read",
                why="only KiCad-format libraries (.kicad_sym / .pretty) are parsed by kiwrap",
                fix="convert the library with KiCad (Symbol Editor > Save As) or pick a KiCad-format library",
                data={"row": row.as_dict()},
            )
        if row.path is None or not row.exists:
            raise NotFound(
                f"library {nickname!r} resolves to {row.path or row.uri}, which does not exist",
                why=row.error or "the expanded URI is not on disk",
                fix="check the environment variables (KICAD<major>_SYMBOL_DIR / _FOOTPRINT_DIR, KIPRJMOD) "
                "and the table's uri",
                data={"row": row.as_dict()},
            )
        return row

    # ---- symbols ------------------------------------------------------------------------------

    def symbol_library(self, nickname: str) -> SymbolLibrary:
        row = self._row("sym", nickname)
        assert row.path is not None
        key = os.path.normcase(str(row.path))
        try:
            st = row.path.stat()
            stamp = (st.st_size, st.st_mtime_ns)
        except OSError as e:
            raise NotFound(f"cannot stat {row.path}: {e}", fix="check the library path") from e
        cached = self._sym_cache.get(key)
        if cached is not None and cached[0] == stamp and cached[1].nickname == nickname:
            return cached[1]
        try:
            lib = SymbolLibrary.load(row.path, nickname=nickname)
        except (OSError, ValueError) as e:
            raise LibError(
                f"cannot read symbol library {nickname!r} ({row.path}): {e}",
                why="the file is missing, unreadable or not a .kicad_sym document",
                fix="open it in KiCad's Symbol Editor to repair it, or remove the table row",
            ) from e
        self._sym_cache[key] = (stamp, lib)
        return lib

    def symbol(self, lib_id: str) -> LibSymbol:
        """Resolve `Nick:Name` -> flattened `LibSymbol` (its `node` is a fresh copy per call)."""
        nick, name = split_lib_id(lib_id)
        if not nick or not name:
            raise NotFound(
                f"{lib_id!r} is not a lib_id",
                why="a symbol id is <library nickname>:<symbol name>",
                fix="e.g. 'Device:R' or 'power:GND'; find ids with kw_lib_search",
            )
        return self.symbol_library(nick).symbol(name)

    def symbol_names(self, nickname: str) -> list[str]:
        return self.symbol_library(nickname).names()

    # ---- footprints ---------------------------------------------------------------------------

    def footprint(self, fpid: str) -> LibFootprint:
        nick, name = split_lib_id(fpid)
        if not nick or not name:
            raise NotFound(
                f"{fpid!r} is not a footprint id",
                why="a footprint id is <library nickname>:<footprint name>",
                fix="e.g. 'Resistor_SMD:R_0603_1608Metric'; find ids with kw_lib_search(kind='footprint')",
            )
        row = self._row("fp", nick)
        assert row.path is not None
        path = row.path / f"{name}.kicad_mod"
        key = os.path.normcase(str(path))
        try:
            st = path.stat()
        except OSError:
            self._fp_cache.pop(key, None)
            return load_footprint(row.path, name, nickname=nick)  # raises NotFound with candidates
        stamp = (st.st_size, st.st_mtime_ns)
        cached = self._fp_cache.get(key)
        if cached is not None and cached[0] == stamp:
            return cached[1]
        try:
            fp = load_footprint(row.path, name, nickname=nick)
        except ValueError as e:
            raise LibError(
                f"cannot read footprint {fpid!r} ({path}): {e}",
                why="the .kicad_mod file does not parse",
                fix="open it in KiCad's Footprint Editor to repair it",
            ) from e
        self._fp_cache[key] = (stamp, fp)
        return fp

    def footprint_names(self, nickname: str) -> list[str]:
        row = self._row("fp", nickname)
        assert row.path is not None
        return footprint_names(row.path)

    # ---- footprint filters --------------------------------------------------------------------

    def matching_footprints(
        self, filters: list[str] | tuple[str, ...], *, pin_count: int | None = None, limit: int = 50
    ) -> list[str]:
        """Footprint ids matching a symbol's `ki_fp_filters` (KiCad `FOOTPRINT_FILTER_IT`:
        case-insensitive anchored wildcards; a filter containing `:` is matched against
        `nick:name`, else against the name). Walks the `.pretty` directories of every usable
        row - cheap (directory listings only); the index does the same faster with pad counts."""
        from kiwrap.formats.libs.index import filter_regex  # local import: index is the heavier module

        patterns = [filter_regex(f) for f in filters if f]
        if not patterns:
            return []
        out: list[str] = []
        for row in self.rows("fp"):
            if not row.usable or row.path is None:
                continue
            nick_low = row.name.lower()
            for name in footprint_names(row.path):
                low = name.lower()
                full = f"{nick_low}:{low}"
                if any(rx.match(full if with_nick else low) for rx, with_nick in patterns):
                    out.append(f"{row.name}:{name}")
                    if len(out) >= limit:
                        return out
        return out
