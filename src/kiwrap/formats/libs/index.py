"""`LibIndex`: an sqlite FTS5 index over every symbol and footprint the resolved tables reach.

    index = LibIndex(install, config_root, storage_home, project_dir=...)
    index.ensure()                       # build once, then only when the tables changed
    hits = index.search("symbol", "LM358", {"pins": 8}, limit=20)   # list[LibHit]
    res = index.query("footprint", "R_0603", {"smd": True})         # hits + near-misses

Storage: `<storage_home>/lib-index/<version>-<install root digest>.sqlite`, one database per
install (the stdlib `sqlite3` is the one dependency exception the phase brief allows for the
index; `kiwrap.formats` still needs nothing third-party). Rows carry a `scope`: `global` for
the sandbox/template tables and `p:<digest of the project dir>` for a project's own tables, so
one database serves every project and a project row that shadows a global nickname hides the
global rows at query time. `build()` is incremental: every library keeps a digest (symbol file
size + mtime; for a `.pretty` the hash of its `(name, size, mtime)` listing) and only changed,
new or removed libraries are rescanned. `ensure()` skips even the stat pass when the resolved
tables' digest is unchanged since the last build.

Scanning uses a regex pass, not the CST (234 MB of symbol libraries parse in ~25 s on the CST
and in a few seconds this way); the fields it extracts (names, `extends`, properties, pin and
unit counts, `(power)`, pad numbers/types/layers, `descr`/`tags`/`attr`) are cross-checked
against the CST parsers in tests/test_libs_index.py. `pin_count` follows
`LIB_SYMBOL::GetPinCount()` (all units, body style 1) and inherits through `extends`;
`pad_count` follows KiCad 11's `FOOTPRINT::GetNumberedPadCount()` (distinct electrical
numbers on copper, NPTH excluded).

Search: the query is split into unicode61 tokens (so `R_0603` finds `R_0603_1608Metric` and
`ESP32-S3-WROOM-1` its own name); the FTS MATCH runs exact tokens first, then prefix tokens,
then an OR of the tokens (partial matches are reported as near-misses with the reason).
Ranking = bm25 with column weights (name >> keywords > description) plus exact / prefix name
boosts. `filters` narrow the result and rows that match the text but fail a filter come back
as near-misses with the failing filter named, so an agent sees "LM358 has 8 pins, you asked
for 14" instead of an empty list.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from kiwrap.formats.libs.models import ConfigRootLike, InstallLike, LibHit, LibTableRow
from kiwrap.formats.libs.resolver import LibResolver
from kiwrap.formats.sexpr import unquote

__all__ = [
    "BuildReport",
    "FootprintRecord",
    "LibIndex",
    "SearchResult",
    "SymbolRecord",
    "filter_regex",
    "footprint_filter_matches",
    "scan_footprint_text",
    "scan_symbol_lib_text",
    "tokens_of",
]

SCHEMA_VERSION = 3
_QSTR = r'"((?:[^"\\]|\\.)*)"'
_SYMBOL_RE = re.compile(r"\(symbol\s+" + _QSTR)
_EXTENDS_RE = re.compile(r"\(extends\s+" + _QSTR)
_PROPERTY_RE = re.compile(r"\(property\s+" + _QSTR + r"\s+" + _QSTR)
_PIN_RE = re.compile(r"\(pin\s+[a-z_]+")
_POWER_RE = re.compile(r"\(power[\s)]")
_UNIT_NAME_RE = re.compile(r"^(?P<base>.*)_(?P<unit>\d+)_(?P<style>\d+)$")
_PAD_RE = re.compile(r"\(pad\s+" + _QSTR + r"\s+([a-z_]+)")
_LAYERS_RE = re.compile(r"\(layers\s+([^)]*)\)")
_DESCR_RE = re.compile(r"\(descr\s+" + _QSTR)
_TAGS_RE = re.compile(r"\(tags\s+" + _QSTR)
_ATTR_RE = re.compile(r"\(attr\s+([^)]*)\)")
_HEAD_RE = re.compile(r"^\s*\((footprint|module)\s+" + _QSTR)
_TOKEN_RE = re.compile(r"[0-9A-Za-z" + chr(0xC0) + "-" + chr(0xFFFF) + "]+")  # unicode61-like word chars
_MANDATORY = ("Reference", "Value", "Footprint", "Datasheet", "Description")


# ----------------------------------------------------------------------------------------------
# scanning
# ----------------------------------------------------------------------------------------------


@dataclass
class SymbolRecord:
    name: str
    extends: str | None = None
    properties: dict[str, str] = field(default_factory=dict)
    pin_count: int = 0
    units: int = 1
    power: bool = False

    @property
    def description(self) -> str:
        return self.properties.get("Description") or self.properties.get("ki_description", "")

    @property
    def keywords(self) -> str:
        return self.properties.get("ki_keywords", "")

    @property
    def fp_filters(self) -> str:
        return self.properties.get("ki_fp_filters", "")


@dataclass
class FootprintRecord:
    name: str
    description: str = ""
    tags: str = ""
    attributes: tuple[str, ...] = ()
    pad_count: int = 0
    pads_total: int = 0


def _electrical(num: str) -> bool:
    i = 0
    while i < len(num) and num[i].isalpha():
        i += 1
    return len(num) > 0 and i <= 2 and i < len(num) and num[i:].isdigit()


def _unit_of(subname: str, parent: str) -> tuple[int, int] | None:
    if subname.startswith(parent + "_"):
        m = re.match(r"^(\d+)_(\d+)$", subname[len(parent) + 1 :])
        if m:
            return int(m.group(1)), int(m.group(2))
    m2 = _UNIT_NAME_RE.match(subname)
    return (int(m2.group("unit")), int(m2.group("style"))) if m2 else None


def scan_symbol_lib_text(text: str) -> list[SymbolRecord]:
    """Top-level symbols of a `.kicad_sym` text with `extends` resolved for the index fields
    (pins/units/power from the root of the chain; properties: child overrides non-empty)."""
    raw: list[tuple[str, int, int, list[tuple[str, int]]]] = []  # name, start, end, [(subname, pos)]
    current: str | None = None
    for m in _SYMBOL_RE.finditer(text):
        name = unquote(m.group(1))
        if current is not None and _unit_of(name, current) is not None:
            raw[-1][3].append((name, m.start()))
            continue
        if raw:
            prev = raw[-1]
            raw[-1] = (prev[0], prev[1], m.start(), prev[3])
        raw.append((name, m.start(), len(text), []))
        current = name
    records: dict[str, SymbolRecord] = {}
    order: list[str] = []
    for name, start, end, subs in raw:
        header_end = subs[0][1] if subs else end
        head = text[start:header_end]
        rec = SymbolRecord(name=name)
        ext = _EXTENDS_RE.search(head)
        rec.extends = unquote(ext.group(1)) if ext else None
        for pm in _PROPERTY_RE.finditer(head):
            rec.properties[unquote(pm.group(1))] = unquote(pm.group(2))
        rec.power = _POWER_RE.search(head) is not None
        pins = len(_PIN_RE.findall(head))  # pins declared outside unit blocks (unit 0)
        units = 1
        for i, (subname, pos) in enumerate(subs):
            nxt = subs[i + 1][1] if i + 1 < len(subs) else end
            us = _unit_of(subname, name) or (0, 0)
            units = max(units, us[0])
            if us[1] in (0, 1):
                pins += len(_PIN_RE.findall(text, pos, nxt))
        rec.pin_count = pins
        rec.units = units
        records[name] = rec
        order.append(name)
    # resolve extends (root supplies pins/units/power; properties inherit, child non-empty wins)
    resolved: dict[str, SymbolRecord] = {}

    def resolve(name: str, chain: tuple[str, ...]) -> SymbolRecord:
        if name in resolved:
            return resolved[name]
        rec = records[name]
        parent_name = rec.extends
        if parent_name and parent_name in records and parent_name not in chain:
            parent = resolve(parent_name, (*chain, name))
            props = dict(parent.properties)
            for k, v in rec.properties.items():
                if v != "" or (
                    k not in _MANDATORY and k not in ("ki_keywords", "ki_fp_filters", "ki_description")
                ):
                    props[k] = v
            out = SymbolRecord(
                name=name,
                extends=parent_name,
                properties=props,
                pin_count=parent.pin_count,
                units=parent.units,
                power=parent.power,
            )
        else:
            out = rec
        resolved[name] = out
        return out

    return [resolve(n, ()) for n in order]


def scan_footprint_text(text: str, name: str | None = None) -> FootprintRecord:
    """Index fields of one `.kicad_mod` text (see module docstring for the pad-count rule)."""
    hm = _HEAD_RE.match(text)
    fname = name or (unquote(hm.group(2)) if hm else "")
    if ":" in fname and name is None:
        fname = fname.split(":", 1)[1]
    dm = _DESCR_RE.search(text)
    tm = _TAGS_RE.search(text)
    am = _ATTR_RE.search(text)
    attrs = tuple(a for a in am.group(1).split() if a) if am else ()
    counted: set[str] = set()
    total = 0
    pads = list(_PAD_RE.finditer(text))
    for i, pm in enumerate(pads):
        total += 1
        ptype = pm.group(2)
        if ptype == "np_thru_hole":
            continue
        nxt = pads[i + 1].start() if i + 1 < len(pads) else len(text)
        lm = _LAYERS_RE.search(text, pm.end(), nxt)
        if lm is None or ".Cu" not in lm.group(1):
            continue
        num = unquote(pm.group(1))
        if _electrical(num):
            counted.add(num)
        elif "_" in num:
            a, b = num.split("_", 1)
            if _electrical(a) and _electrical(b):
                counted.add(a)
                counted.add(b)
    return FootprintRecord(
        name=fname,
        description=unquote(dm.group(1)) if dm else "",
        tags=unquote(tm.group(1)) if tm else "",
        attributes=attrs,
        pad_count=len(counted),
        pads_total=total,
    )


# ----------------------------------------------------------------------------------------------
# footprint filters
# ----------------------------------------------------------------------------------------------


def filter_regex(pattern: str) -> tuple[re.Pattern[str], bool]:
    """`ki_fp_filters` entry -> (anchored case-insensitive regex, matches against `nick:name`).
    Mirrors `EDA_PATTERN_MATCH_WILDCARD_ANCHORED` + `FOOTPRINT_FILTER_IT::FootprintFilterMatch`:
    `*` any run, `?` one char, everything else literal, lowercase both sides."""
    out = ["^"]
    for ch in pattern.lower():
        if ch == "*":
            out.append(".*")
        elif ch == "?":
            out.append(".")
        else:
            out.append(re.escape(ch))
    out.append("$")
    return re.compile("".join(out)), ":" in pattern


def footprint_filter_matches(filters: Iterable[str], ids: Iterable[str]) -> list[str]:
    """The footprint ids (`nick:name`) matching any of the filters, in input order."""
    pats = [filter_regex(f) for f in filters if f]
    if not pats:
        return []
    out: list[str] = []
    for fid in ids:
        low = fid.lower()
        name = low.split(":", 1)[1] if ":" in low else low
        if any(rx.match(low if with_nick else name) for rx, with_nick in pats):
            out.append(fid)
    return out


# ----------------------------------------------------------------------------------------------
# the index
# ----------------------------------------------------------------------------------------------


@dataclass
class BuildReport:
    libraries: int = 0
    scanned: int = 0
    removed: int = 0
    unchanged: int = 0
    symbols: int = 0
    footprints: int = 0
    seconds: float = 0.0
    problems: list[str] = field(default_factory=list)
    skipped: bool = False  # ensure(): tables unchanged, nothing looked at

    def as_dict(self) -> dict[str, Any]:
        return {
            "libraries": self.libraries,
            "scanned": self.scanned,
            "removed": self.removed,
            "unchanged": self.unchanged,
            "symbols": self.symbols,
            "footprints": self.footprints,
            "seconds": round(self.seconds, 2),
            "problems": list(self.problems),
            "skipped": self.skipped,
        }


@dataclass
class SearchResult:
    kind: str
    query: str
    hits: list[LibHit]
    near_misses: list[LibHit]
    total_text_matches: int
    strategy: str  # exact | prefix | any | fuzzy | none
    filters: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "query": self.query,
            "strategy": self.strategy,
            "total_text_matches": self.total_text_matches,
            "hits": [h.as_dict() for h in self.hits],
            "near_misses": [h.as_dict() for h in self.near_misses],
            "filters": dict(self.filters),
        }


_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS libs(
    kind TEXT NOT NULL, scope TEXT NOT NULL, nickname TEXT NOT NULL, path TEXT NOT NULL,
    digest TEXT NOT NULL, hidden INTEGER NOT NULL DEFAULT 0, item_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(kind, scope, nickname));
CREATE TABLE IF NOT EXISTS symbols(
    id TEXT NOT NULL, scope TEXT NOT NULL, nickname TEXT NOT NULL, name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '', keywords TEXT NOT NULL DEFAULT '', fp_filters TEXT NOT NULL DEFAULT '',
    pin_count INTEGER NOT NULL DEFAULT 0, units INTEGER NOT NULL DEFAULT 1, power INTEGER NOT NULL DEFAULT 0,
    ref_prefix TEXT NOT NULL DEFAULT '', footprint TEXT NOT NULL DEFAULT '', datasheet TEXT NOT NULL DEFAULT '',
    hidden INTEGER NOT NULL DEFAULT 0, extends TEXT, PRIMARY KEY(scope, id));
CREATE INDEX IF NOT EXISTS symbols_nick ON symbols(scope, nickname);
CREATE INDEX IF NOT EXISTS symbols_name ON symbols(name);
CREATE VIRTUAL TABLE IF NOT EXISTS symbols_fts USING fts5(
    scope UNINDEXED, id UNINDEXED, name, description, keywords, fp_filters, footprint, tokenize='unicode61');
CREATE TABLE IF NOT EXISTS footprints(
    id TEXT NOT NULL, scope TEXT NOT NULL, nickname TEXT NOT NULL, name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '', tags TEXT NOT NULL DEFAULT '', pad_count INTEGER NOT NULL DEFAULT 0,
    pads_total INTEGER NOT NULL DEFAULT 0, attributes TEXT NOT NULL DEFAULT '', hidden INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(scope, id));
CREATE INDEX IF NOT EXISTS footprints_nick ON footprints(scope, nickname);
CREATE INDEX IF NOT EXISTS footprints_name ON footprints(name);
CREATE VIRTUAL TABLE IF NOT EXISTS footprints_fts USING fts5(
    scope UNINDEXED, id UNINDEXED, name, description, tags, tokenize='unicode61');
INSERT OR IGNORE INTO meta(key, value) VALUES ('schema', '{SCHEMA_VERSION}');
"""


def tokens_of(query: str) -> list[str]:
    """unicode61-style tokens of a query, lowercased, in order, without duplicates."""
    seen: dict[str, None] = {}
    for t in _TOKEN_RE.findall(query):
        seen.setdefault(t.lower(), None)
    return list(seen)


def _digest_file(path: Path) -> str:
    st = path.stat()
    return f"f:{st.st_size}:{st.st_mtime_ns}"


def _digest_dir(path: Path) -> str:
    h = hashlib.sha1()
    try:
        entries = sorted(p for p in path.iterdir() if p.suffix == ".kicad_mod")
    except OSError:
        return "d:missing"
    for p in entries:
        try:
            st = p.stat()
        except OSError:
            continue
        h.update(f"{p.name}:{st.st_size}:{st.st_mtime_ns}\n".encode())
    return "d:" + h.hexdigest()[:20]


def _scope_of(project_dir: Path | None) -> str:
    if project_dir is None:
        return "global"
    key = os.path.normcase(str(Path(project_dir).resolve()))
    return "p:" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


class LibIndex:
    def __init__(
        self,
        install: InstallLike,
        config_root: ConfigRootLike | Path | None,
        storage_home: Path,
        *,
        project_dir: Path | None = None,
        resolver: LibResolver | None = None,
    ) -> None:
        self.install = install
        self.storage_home = Path(storage_home)
        self.project_dir = Path(project_dir) if project_dir is not None else None
        self.resolver = resolver or LibResolver(install, config_root, self.project_dir)
        digest = hashlib.sha1(str(install.root).encode("utf-8")).hexdigest()[:10]
        safe_version = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in install.version)
        self.db_path = self.storage_home / "lib-index" / f"{safe_version}-{digest}.sqlite"
        self.project_scope = _scope_of(self.project_dir)

    # ---- storage ------------------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(self.db_path), timeout=30)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        con.executescript(_SCHEMA)
        row = con.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
        if row is None or str(row[0]) != str(SCHEMA_VERSION):
            con.close()
            self.db_path.unlink(missing_ok=True)
            for suffix in ("-wal", "-shm"):
                Path(str(self.db_path) + suffix).unlink(missing_ok=True)
            con = sqlite3.connect(str(self.db_path), timeout=30)
            con.execute("PRAGMA journal_mode=WAL")
            con.executescript(_SCHEMA)
        return con

    def exists(self) -> bool:
        return self.db_path.is_file()

    def _meta(self, con: sqlite3.Connection, key: str) -> str | None:
        row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return None if row is None else str(row[0])

    def _set_meta(self, con: sqlite3.Connection, key: str, value: str) -> None:
        con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))

    def _scopes(self) -> tuple[str, ...]:
        return ("global",) if self.project_scope == "global" else ("global", self.project_scope)

    def _rows_by_scope(self) -> dict[str, list[tuple[str, LibTableRow]]]:
        """The table rows as (kind, row) grouped by index scope (project rows under the project scope)."""
        tables = self.resolver.library_tables()
        out: dict[str, list[tuple[str, LibTableRow]]] = {s: [] for s in self._scopes()}
        for kind in ("sym", "fp"):
            for r in tables.sym if kind == "sym" else tables.fp:
                scope = "global" if r.scope == "global" else self.project_scope
                if scope not in out:
                    continue
                out[scope].append((kind, r))
        return out

    # ---- build --------------------------------------------------------------------------------

    def ensure(self, *, progress: Callable[[str], None] | None = None) -> BuildReport:
        """Build when the database is missing or the resolved tables changed; else a no-op."""
        digest = self.resolver.tables_digest()
        if self.exists():
            con = self._connect()
            try:
                if self._meta(con, f"tables:{self.project_scope}") == digest and self._meta(con, "built"):
                    return BuildReport(skipped=True)
            finally:
                con.close()
        return self.build(progress=progress)

    def build(self, *, force: bool = False, progress: Callable[[str], None] | None = None) -> BuildReport:
        """Incremental (or `force` full) rebuild of every scope this index serves."""
        t0 = time.perf_counter()
        report = BuildReport()
        by_scope = self._rows_by_scope()
        con = self._connect()
        try:
            for scope, rows in by_scope.items():
                self._sync_scope(con, scope, rows, force=force, report=report, progress=progress)
                self._set_meta(con, f"tables:{scope}", self.resolver.tables_digest())
            self._set_meta(con, "built", time.strftime("%Y-%m-%dT%H:%M:%S"))
            self._set_meta(con, "install", f"{self.install.version}@{self.install.root}")
            con.commit()
            report.symbols = int(con.execute("SELECT count(*) FROM symbols").fetchone()[0])
            report.footprints = int(con.execute("SELECT count(*) FROM footprints").fetchone()[0])
        finally:
            con.close()
        report.problems.extend(self.resolver.problems)
        report.seconds = time.perf_counter() - t0
        return report

    def _sync_scope(
        self,
        con: sqlite3.Connection,
        scope: str,
        rows: list[tuple[str, LibTableRow]],
        *,
        force: bool,
        report: BuildReport,
        progress: Callable[[str], None] | None,
    ) -> None:
        known = {
            (k, n): (p, d)
            for k, n, p, d in con.execute(
                "SELECT kind, nickname, path, digest FROM libs WHERE scope=?", (scope,)
            )
        }
        wanted: set[tuple[str, str]] = set()
        for kind, row in rows:
            if not row.usable or row.path is None:
                if row.is_kicad and not row.disabled and row.error:
                    report.problems.append(f"{row.name}: {row.error}")
                continue
            wanted.add((kind, row.name))
            report.libraries += 1
            try:
                digest = _digest_file(row.path) if kind == "sym" else _digest_dir(row.path)
            except OSError as e:
                report.problems.append(f"{row.name}: {e}")
                continue
            digest += f"|h{int(row.hidden)}"
            prev = known.get((kind, row.name))
            if prev is not None and prev[0] == str(row.path) and prev[1] == digest and not force:
                report.unchanged += 1
                continue
            if progress:
                progress(f"{kind} {row.name}")
            self._delete_lib(con, scope, kind, row.name)
            try:
                count = self._scan_lib(con, scope, kind, row)
            except (OSError, ValueError) as e:
                report.problems.append(f"{row.name}: {e}")
                count = 0
            con.execute(
                "INSERT OR REPLACE INTO libs(kind, scope, nickname, path, digest, hidden, item_count) VALUES (?,?,?,?,?,?,?)",
                (kind, scope, row.name, str(row.path), digest, int(row.hidden), count),
            )
            report.scanned += 1
            con.commit()
        for kind, nick in set(known) - wanted:
            self._delete_lib(con, scope, kind, nick)
            report.removed += 1
        con.commit()

    def _delete_lib(self, con: sqlite3.Connection, scope: str, kind: str, nickname: str) -> None:
        table, fts = ("symbols", "symbols_fts") if kind == "sym" else ("footprints", "footprints_fts")
        con.execute(
            f"DELETE FROM {fts} WHERE scope=? AND id IN (SELECT id FROM {table} WHERE scope=? AND nickname=?)",
            (scope, scope, nickname),
        )
        con.execute(f"DELETE FROM {table} WHERE scope=? AND nickname=?", (scope, nickname))
        con.execute("DELETE FROM libs WHERE scope=? AND kind=? AND nickname=?", (scope, kind, nickname))

    def _scan_lib(self, con: sqlite3.Connection, scope: str, kind: str, row: LibTableRow) -> int:
        assert row.path is not None
        hidden = int(row.hidden)
        if kind == "sym":
            try:
                text = row.path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                text = row.path.read_text(encoding="utf-8", errors="replace")
            recs = list({r.name: r for r in scan_symbol_lib_text(text)}.values())
            con.executemany(
                "INSERT OR REPLACE INTO symbols(id, scope, nickname, name, description, keywords, fp_filters, pin_count, "
                "units, power, ref_prefix, footprint, datasheet, hidden, extends) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        f"{row.name}:{r.name}",
                        scope,
                        row.name,
                        r.name,
                        r.description,
                        r.keywords,
                        r.fp_filters,
                        r.pin_count,
                        r.units,
                        int(r.power),
                        r.properties.get("Reference", ""),
                        r.properties.get("Footprint", ""),
                        r.properties.get("Datasheet", ""),
                        hidden,
                        r.extends,
                    )
                    for r in recs
                ],
            )
            con.executemany(
                "INSERT INTO symbols_fts(scope, id, name, description, keywords, fp_filters, footprint) VALUES (?,?,?,?,?,?,?)",
                [
                    (
                        scope,
                        f"{row.name}:{r.name}",
                        r.name,
                        r.description,
                        r.keywords,
                        r.fp_filters,
                        r.properties.get("Footprint", ""),
                    )
                    for r in recs
                ],
            )
            return len(recs)
        count = 0
        sym_rows: list[tuple[Any, ...]] = []
        fts_rows: list[tuple[Any, ...]] = []
        try:
            files = sorted(p for p in row.path.iterdir() if p.suffix == ".kicad_mod" and p.is_file())
        except OSError:
            files = []
        for p in files:
            try:
                text = p.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            rec = scan_footprint_text(text, p.stem)
            fid = f"{row.name}:{rec.name}"
            sym_rows.append(
                (
                    fid,
                    scope,
                    row.name,
                    rec.name,
                    rec.description,
                    rec.tags,
                    rec.pad_count,
                    rec.pads_total,
                    " ".join(rec.attributes),
                    hidden,
                )
            )
            fts_rows.append((scope, fid, rec.name, rec.description, rec.tags))
            count += 1
        con.executemany(
            "INSERT OR REPLACE INTO footprints(id, scope, nickname, name, description, tags, pad_count, pads_total, attributes, hidden) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            sym_rows,
        )
        con.executemany(
            "INSERT INTO footprints_fts(scope, id, name, description, tags) VALUES (?,?,?,?,?)", fts_rows
        )
        return count

    # ---- queries ------------------------------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        if not self.exists():
            return {"built": None, "symbols": 0, "footprints": 0, "libraries": 0, "path": str(self.db_path)}
        con = self._connect()
        try:
            scopes = self._scopes()
            q = ",".join("?" * len(scopes))
            return {
                "built": self._meta(con, "built"),
                "symbols": int(
                    con.execute(f"SELECT count(*) FROM symbols WHERE scope IN ({q})", scopes).fetchone()[0]
                ),
                "footprints": int(
                    con.execute(f"SELECT count(*) FROM footprints WHERE scope IN ({q})", scopes).fetchone()[0]
                ),
                "libraries": int(
                    con.execute(f"SELECT count(*) FROM libs WHERE scope IN ({q})", scopes).fetchone()[0]
                ),
                "path": str(self.db_path),
            }
        finally:
            con.close()

    def _shadowed(self, con: sqlite3.Connection, kind: str) -> set[str]:
        """Global nicknames a project row of the same name hides."""
        if self.project_scope == "global":
            return set()
        return {
            n
            for (n,) in con.execute(
                "SELECT nickname FROM libs WHERE scope=? AND kind=?", (self.project_scope, kind)
            )
        }

    def _visible_sql(self, con: sqlite3.Connection, kind: str, alias: str) -> tuple[str, list[Any]]:
        shadow = sorted(self._shadowed(con, kind))
        if self.project_scope == "global":
            return f"{alias}.scope='global'", []
        if not shadow:
            return f"{alias}.scope IN ('global', ?)", [self.project_scope]
        marks = ",".join("?" * len(shadow))
        return (
            f"(({alias}.scope='global' AND {alias}.nickname NOT IN ({marks})) OR {alias}.scope=?)",
            [*shadow, self.project_scope],
        )

    @staticmethod
    def _match_expr(tokens: list[str], mode: str) -> str:
        if mode == "exact":
            return " AND ".join(f'"{t}"' for t in tokens)
        if mode == "prefix":
            return " AND ".join(f'"{t}"*' for t in tokens)
        if mode == "any":
            return " OR ".join(f'"{t}"*' for t in tokens)
        if mode == "fuzzy":
            parts = []
            for t in tokens:
                cut = t[: max(3, len(t) - 2)] if len(t) > 3 else t
                parts.append(f'"{cut}"*')
            return " OR ".join(parts)
        raise ValueError(mode)

    def query(
        self,
        kind: str,
        query: str,
        filters: Mapping[str, Any] | None = None,
        limit: int = 20,
    ) -> SearchResult:
        """Text search + filters; near-misses are text matches failing a filter (or partial text
        matches when nothing matched every token)."""
        if kind not in ("symbol", "footprint"):
            raise ValueError(f"kind must be 'symbol' or 'footprint', not {kind!r}")
        f = dict(filters or {})
        tokens = tokens_of(query)
        limit = max(1, int(limit))
        if not self.exists():
            self.build()
        con = self._connect()
        try:
            if not tokens:
                rows = self._browse(con, kind, f, limit * 5)
                strategy = "browse"
            else:
                rows, strategy = self._text_search(con, kind, tokens, limit * 10)
            hits: list[LibHit] = []
            misses: list[LibHit] = []
            for hit in rows:
                reason = _filter_reason(hit, f, kind)
                if reason is None and (strategy in ("exact", "prefix", "browse") or hit.miss is None):
                    hits.append(hit)
                else:
                    misses.append(hit if reason is None else _with_miss(hit, reason))
            hits.sort(key=lambda h: (-h.score, h.id))
            # near-misses: a wrong library / hidden row is the least interesting reason
            misses.sort(key=lambda h: (_soft_miss(h.miss), -h.score, h.id))
            return SearchResult(
                kind=kind,
                query=query,
                hits=hits[:limit],
                near_misses=misses[:limit],
                total_text_matches=len(rows),
                strategy=strategy,
                filters=f,
            )
        finally:
            con.close()

    def search(
        self, kind: str, query: str, filters: Mapping[str, Any] | None = None, limit: int = 20
    ) -> list[LibHit]:
        """Contract form: the hits only (`query()` also returns the near-misses)."""
        return self.query(kind, query, filters, limit).hits

    def _text_search(
        self, con: sqlite3.Connection, kind: str, tokens: list[str], cap: int
    ) -> tuple[list[LibHit], str]:
        for mode in ("exact", "prefix", "any", "fuzzy"):
            if mode == "any" and len(tokens) < 2:
                continue
            rows = self._fts(con, kind, self._match_expr(tokens, mode), tokens, cap, mode)
            if rows:
                return rows, mode
        return [], "none"

    def _fts(
        self, con: sqlite3.Connection, kind: str, match: str, tokens: list[str], cap: int, mode: str
    ) -> list[LibHit]:
        joined = " ".join(tokens)
        if kind == "symbol":
            vis, params = self._visible_sql(con, "sym", "s")
            sql = (
                "SELECT s.id, s.nickname, s.name, s.description, s.keywords, s.fp_filters, s.pin_count, s.units, "
                "s.power, s.hidden, s.ref_prefix, s.footprint, bm25(symbols_fts, 0, 0, 12.0, 1.0, 4.0, 1.5, 2.0) AS r "
                "FROM symbols_fts JOIN symbols s ON s.scope = symbols_fts.scope AND s.id = symbols_fts.id "
                f"WHERE symbols_fts MATCH ? AND {vis} ORDER BY r LIMIT ?"
            )
            out: list[LibHit] = []
            for row in con.execute(sql, [match, *params, cap]):
                (fid, nick, name, desc, kw, filt, pins, units, power, hidden, ref, fp, rank) = row
                score = -float(rank) + _name_boost(name, tokens, joined)
                out.append(
                    LibHit(
                        id=fid,
                        name=name,
                        description=desc,
                        keywords=kw,
                        pin_count=int(pins),
                        footprint_filters=tuple(filt.split()),
                        score=score,
                        kind="symbol",
                        nickname=nick,
                        units=int(units),
                        power=bool(power),
                        hidden=bool(hidden),
                        reference_prefix=ref,
                        footprint=fp,
                        miss=None if mode in ("exact", "prefix") else f"partial match ({mode})",
                    )
                )
            return out
        vis, params = self._visible_sql(con, "fp", "f")
        sql = (
            "SELECT f.id, f.nickname, f.name, f.description, f.tags, f.pad_count, f.attributes, f.hidden, "
            "bm25(footprints_fts, 0, 0, 12.0, 1.0, 4.0) AS r "
            "FROM footprints_fts JOIN footprints f ON f.scope = footprints_fts.scope AND f.id = footprints_fts.id "
            f"WHERE footprints_fts MATCH ? AND {vis} ORDER BY r LIMIT ?"
        )
        out2: list[LibHit] = []
        for row in con.execute(sql, [match, *params, cap]):
            (fid, nick, name, desc, tags, pads, attrs, hidden, rank) = row
            score = -float(rank) + _name_boost(name, tokens, joined)
            out2.append(
                LibHit(
                    id=fid,
                    name=name,
                    description=desc,
                    keywords=tags,
                    pin_count=None,
                    footprint_filters=(),
                    score=score,
                    kind="footprint",
                    nickname=nick,
                    pad_count=int(pads),
                    hidden=bool(hidden),
                    attributes=tuple(attrs.split()),
                    miss=None if mode in ("exact", "prefix") else f"partial match ({mode})",
                )
            )
        return out2

    def _browse(self, con: sqlite3.Connection, kind: str, f: Mapping[str, Any], cap: int) -> list[LibHit]:
        """No query text: list by filters only (library / pins / package), name order."""
        clauses: list[str] = []
        params: list[Any] = []
        nick = f.get("library") or f.get("nickname")
        if nick:
            clauses.append("lower(nickname) = ?")
            params.append(str(nick).lower())
        if kind == "symbol":
            vis, vparams = self._visible_sql(con, "sym", "s")
            sql = (
                "SELECT id, nickname, name, description, keywords, fp_filters, pin_count, units, power, hidden, "
                f"ref_prefix, footprint FROM symbols s WHERE {vis}"
                + "".join(f" AND {c}" for c in clauses)
                + " ORDER BY name LIMIT ?"
            )
            return [
                LibHit(
                    id=r[0],
                    name=r[2],
                    description=r[3],
                    keywords=r[4],
                    pin_count=int(r[6]),
                    footprint_filters=tuple(r[5].split()),
                    score=0.0,
                    kind="symbol",
                    nickname=r[1],
                    units=int(r[7]),
                    power=bool(r[8]),
                    hidden=bool(r[9]),
                    reference_prefix=r[10],
                    footprint=r[11],
                )
                for r in con.execute(sql, [*vparams, *params, cap])
            ]
        vis, vparams = self._visible_sql(con, "fp", "f")
        sql = (
            "SELECT id, nickname, name, description, tags, pad_count, attributes, hidden FROM footprints f "
            f"WHERE {vis}" + "".join(f" AND {c}" for c in clauses) + " ORDER BY name LIMIT ?"
        )
        return [
            LibHit(
                id=r[0],
                name=r[2],
                description=r[3],
                keywords=r[4],
                pin_count=None,
                footprint_filters=(),
                score=0.0,
                kind="footprint",
                nickname=r[1],
                pad_count=int(r[5]),
                hidden=bool(r[7]),
                attributes=tuple(r[6].split()),
            )
            for r in con.execute(sql, [*vparams, *params, cap])
        ]

    # ---- lookups ------------------------------------------------------------------------------

    def symbol_row(self, lib_id: str) -> dict[str, Any] | None:
        con = self._connect()
        try:
            vis, params = self._visible_sql(con, "sym", "s")
            row = con.execute(
                "SELECT id, nickname, name, description, keywords, fp_filters, pin_count, units, power, hidden, "
                f"ref_prefix, footprint, datasheet, extends FROM symbols s WHERE s.id=? AND {vis} "
                "ORDER BY CASE WHEN s.scope='global' THEN 1 ELSE 0 END LIMIT 1",
                [lib_id, *params],
            ).fetchone()
        finally:
            con.close()
        if row is None:
            return None
        keys = (
            "id",
            "nickname",
            "name",
            "description",
            "keywords",
            "fp_filters",
            "pin_count",
            "units",
            "power",
            "hidden",
            "ref_prefix",
            "footprint",
            "datasheet",
            "extends",
        )
        return dict(zip(keys, row, strict=True))

    def footprint_ids(self) -> list[str]:
        con = self._connect()
        try:
            vis, params = self._visible_sql(con, "fp", "f")
            return [r[0] for r in con.execute(f"SELECT id FROM footprints f WHERE {vis} ORDER BY id", params)]
        finally:
            con.close()

    def footprints_for(
        self, filters: Iterable[str], *, pin_count: int | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Footprints matching a symbol's `ki_fp_filters`, each with its pad count and whether it
        equals `pin_count` (KiCad's chooser "filter by pin count"). Exact pad-count matches first."""
        pats = [filter_regex(f) for f in filters if f]
        if not pats:
            return []
        con = self._connect()
        try:
            vis, params = self._visible_sql(con, "fp", "f")
            rows = con.execute(
                f"SELECT id, nickname, name, description, pad_count, attributes FROM footprints f WHERE {vis} ORDER BY id",
                params,
            ).fetchall()
        finally:
            con.close()
        out: list[dict[str, Any]] = []
        for fid, nick, name, desc, pads, attrs in rows:
            low_name = name.lower()
            full = f"{nick.lower()}:{low_name}"
            if any(rx.match(full if with_nick else low_name) for rx, with_nick in pats):
                out.append(
                    {
                        "id": fid,
                        "description": desc,
                        "pad_count": int(pads),
                        "attributes": attrs.split(),
                        "pin_count_match": (pin_count is None) or int(pads) == pin_count,
                    }
                )
        out.sort(key=lambda d: (not d["pin_count_match"], d["id"]))
        return out[:limit]

    def to_json(self) -> str:
        return json.dumps(self.stats())


# ----------------------------------------------------------------------------------------------
# ranking + filters
# ----------------------------------------------------------------------------------------------


def _name_boost(name: str, tokens: list[str], joined: str) -> float:
    low = name.lower()
    name_tokens = tokens_of(name)
    boost = 0.0
    if low == joined or name_tokens == tokens:
        boost += 40.0
    elif name_tokens[: len(tokens)] == tokens:
        boost += 20.0
    elif tokens and tokens[0] in name_tokens:
        boost += 6.0
    if tokens and low.startswith(tokens[0]):
        boost += 4.0
    boost -= min(len(name), 60) / 30.0  # shorter names first among equals
    return boost


def _soft_miss(reason: str | None) -> int:
    return 1 if reason and (reason.startswith("library ") or "hidden" in reason) else 0


def _with_miss(hit: LibHit, reason: str) -> LibHit:
    return replace(hit, miss=reason)


def _as_int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _filter_reason(hit: LibHit, f: Mapping[str, Any], kind: str) -> str | None:
    """None when `hit` passes every filter, else the first failing one, phrased for the agent."""
    nick = f.get("library") or f.get("nickname")
    if nick and hit.nickname.lower() != str(nick).lower():
        return f"library {hit.nickname} (wanted {nick})"
    if hit.hidden and not f.get("include_hidden"):
        return "library row is hidden in the table"
    count = hit.pin_count if kind == "symbol" else hit.pad_count
    label = "pins" if kind == "symbol" else "pads"
    pins = _as_int(f.get("pins"))
    if pins is not None and count != pins:
        return f"{count} {label} (wanted {pins})"
    lo, hi = _as_int(f.get("pins_min")), _as_int(f.get("pins_max"))
    if lo is not None and (count or 0) < lo:
        return f"{count} {label} (wanted >= {lo})"
    if hi is not None and (count or 0) > hi:
        return f"{count} {label} (wanted <= {hi})"
    if kind == "symbol":
        power = f.get("power")
        if power is not None and bool(power) != hit.power:
            return "power symbol" if hit.power else "not a power symbol"
        units = _as_int(f.get("units"))
        if units is not None and hit.units != units:
            return f"{hit.units} units (wanted {units})"
        package = f.get("package")
        if package:
            pk = str(package).lower()
            hay = (" ".join(hit.footprint_filters) + " " + hit.footprint + " " + hit.description).lower()
            if pk not in hay:
                return f"no footprint filter / footprint mentioning {package!r}"
    else:
        smd = f.get("smd")
        if smd is not None and bool(smd) != ("smd" in hit.attributes):
            return "through-hole footprint" if "smd" not in hit.attributes else "SMD footprint"
        th = f.get("through_hole")
        if th is not None and bool(th) != ("through_hole" in hit.attributes):
            return "SMD footprint" if "through_hole" not in hit.attributes else "through-hole footprint"
        package = f.get("package")
        if package:
            pk = str(package).lower()
            if pk not in (hit.name + " " + hit.keywords + " " + hit.description).lower():
                return f"name/tags do not mention {package!r}"
    kws = f.get("keywords")
    if kws:
        words = [str(w).lower() for w in (kws if isinstance(kws, (list, tuple)) else str(kws).split())]
        hay = (hit.keywords + " " + hit.description + " " + hit.name).lower()
        missing = [w for w in words if w not in hay]
        if missing:
            return "keywords missing: " + ", ".join(missing)
    return None
