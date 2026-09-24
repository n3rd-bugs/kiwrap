"""`SchematicWriter` — the file-tier schematic authoring engine (every KiCad version, 7 best-effort).

The rules it encodes are the verified ones of docs/ARCHITECTURE.md §7 and docs/VERIFICATION.md
claims 2 and 3:

* every used symbol is embedded in `lib_symbols` once (dedup by lib_id), copied from the target
  install's library, top-level name rewritten to `<Nickname>:<Name>`, `extends` flattened exactly
  as KiCad does, sub-units left unprefixed (claim 2, correction (a));
* absolute pin end points come from the claim-3 transform (`kiwrap.formats.schematic.
  pin_sheet_position`), so callers never compute geometry;
* wires end exactly on pin end points / junction sites on the 1.27 mm grid: `add_wire` snaps
  every point, splits each segment at every pin end point, sheet pin, existing wire end and
  junction it passes over, and splits existing wires that a new end lands on; `add_symbol`
  likewise splits existing wires under its pins (a pin resting on a wire's interior is NOT
  connected — claim 2 negative control B);
* references are written to the `Reference` property AND the `instances` block, one `path`
  entry per instance path of the sheet file (`/ROOT_UUID[/SHEET_UUID…]`), `sheet_instances`
  only in the root (claim 2, correction (b));
* junctions are added where KiCad's `SCH_SCREEN::IsExplicitJunctionNeeded` would want one (≥ 3
  wire ends, or ≥ 2 non-parallel wire ends plus a pin/label/sheet pin at the point);
* tokens are spelled per epoch by `kiwrap.formats.epoch_dialects` (v7 bare hide … v11 empty
  unit blocks) and the document keeps whatever whitespace/tokens it already had: untouched nodes
  are re-emitted byte-for-byte by the lossless CST (conformance test T1).

Sheets are addressed by a *key*: `""` (the root), a sheet file name (`"led.kicad_sch"` or
`"led"`), a sheet name (`Sheetname` property) or a sheet element uuid. A sheet file instantiated
twice is one document: an edit through any of its keys touches both instances, and every symbol
in it carries one reference per instance path.
"""

from __future__ import annotations

import json
import re
import uuid as _uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from kiwrap.formats.epoch_dialects import SchDialect, convert_lib_symbol, dialect_for, dialect_for_stamp
from kiwrap.formats.epochs import read_generator, read_stamp, set_stamp
from kiwrap.formats.fileio import atomic_write_text
from kiwrap.formats.geometry import Vec2
from kiwrap.formats.schematic import LibPin, pin_sheet_position, unit_letter
from kiwrap.formats.sexpr import Atom, Node, build, dump, load

from . import emit
from .libs import FallbackLibResolver, ResolvedSymbol, describe_symbol
from .model import PinEndpoint, SheetPinEndpoint, SheetPlacement, SymbolPlacement, WriterError

__all__ = ["SchematicWriter"]

_EPS = 1e-4
_SIDES = {"left": 180, "right": 0, "top": 90, "bottom": 270}
_LABEL_HEADS = {"local": "label", "global": "global_label", "hierarchical": "hierarchical_label"}
_SHAPES = ("input", "output", "bidirectional", "tri_state", "passive")
_EDITABLE = (
    "symbol",
    "wire",
    "bus",
    "junction",
    "no_connect",
    "label",
    "global_label",
    "hierarchical_label",
    "sheet",
)
#: KiCad's writer order of top-level items (observed on every 8.0–10.99 demo, see
#: epoch_dialects); fresh nodes are inserted after the last node of the same or an earlier rank.
_RANK = {
    "version": 0,
    "generator": 0,
    "generator_version": 0,
    "uuid": 0,
    "paper": 0,
    "title_block": 0,
    "lib_symbols": 1,
    "bus_alias": 2,
    "rectangle": 3,
    "circle": 3,
    "arc": 3,
    "bezier": 3,
    "text": 3,
    "text_box": 3,
    "junction": 4,
    "no_connect": 5,
    "bus_entry": 6,
    "wire": 7,
    "bus": 7,
    "polyline": 7,
    "image": 8,
    "label": 9,
    "global_label": 10,
    "hierarchical_label": 11,
    "rule_area": 12,
    "netclass_flag": 12,
    "directive_label": 12,
    "symbol": 13,
    "sheet": 14,
    "sheet_instances": 15,
    "embedded_fonts": 16,
}


def _new_uuid() -> str:
    return str(_uuid.uuid4())


def _f(v: float) -> float:
    return round(float(v), 4)


def _pt(node: Node | None) -> tuple[float, float]:
    if node is None:
        return (0.0, 0.0)
    a = node.atoms
    return (_f(a[0].as_float()), _f(a[1].as_float())) if len(a) >= 2 else (0.0, 0.0)


def _same(a: tuple[float, float], b: tuple[float, float]) -> bool:
    return abs(a[0] - b[0]) < _EPS and abs(a[1] - b[1]) < _EPS


def _interior(p: tuple[float, float], a: tuple[float, float], b: tuple[float, float]) -> bool:
    """True when `p` lies strictly inside segment a-b (collinear, not at an end)."""
    if _same(p, a) or _same(p, b):
        return False
    cross = (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])
    if abs(cross) > _EPS:
        return False
    dot = (p[0] - a[0]) * (b[0] - a[0]) + (p[1] - a[1]) * (b[1] - a[1])
    length2 = (b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2
    return _EPS < dot < length2 - _EPS


def _parallel(
    a0: tuple[float, float], a1: tuple[float, float], b0: tuple[float, float], b1: tuple[float, float]
) -> bool:
    cross = (a1[0] - a0[0]) * (b1[1] - b0[1]) - (a1[1] - a0[1]) * (b1[0] - b0[0])
    return abs(cross) < _EPS


def _same_quadrant(
    a0: tuple[float, float],
    a1: tuple[float, float],
    b0: tuple[float, float],
    b1: tuple[float, float],
    p: tuple[float, float],
) -> bool:
    """`SCH_LINE::IsSameQuadrant`: both lines end at `p` and leave it in the same direction
    (overlapping collinear wires); a line passing through `p` never qualifies."""

    def away(s: tuple[float, float], e: tuple[float, float]) -> tuple[float, float] | None:
        if _same(s, p):
            return (e[0] - p[0], e[1] - p[1])
        if _same(e, p):
            return (s[0] - p[0], s[1] - p[1])
        return None

    first, second = away(a0, a1), away(b0, b1)
    if first is None or second is None:
        return False

    def sign(v: float) -> int:
        return (v > _EPS) - (v < -_EPS)

    return sign(first[0]) == sign(second[0]) and sign(first[1]) == sign(second[1])


def _uuid_of(node: Node) -> str:
    return node.get("uuid") or node.get("tstamp") or ""


def _prop(node: Node, name: str) -> Node | None:
    for p in node.find_all("property"):
        a = p.atoms
        if a and a[0].value == name:
            return p
    return None


def _prop_value(node: Node, name: str) -> str:
    p = _prop(node, name)
    if p is None or len(p.atoms) < 2:
        return ""
    return p.atoms[1].value


def _set_atom(atom: Atom, value: str) -> None:
    if atom.value != value:
        atom.value = value


def _split_ref(ref: str) -> tuple[str, int | None]:
    """`"R12"` -> ("R", 12); `"R?"` -> ("R", None); `"#PWR01"` -> ("#PWR", 1)."""
    m = re.match(r"^(?P<prefix>.*?)(?P<num>\d+)$", ref)
    if not m:
        return ref.rstrip("?"), None
    return m.group("prefix"), int(m.group("num"))


def _format_ref(prefix: str, number: int, power: bool) -> str:
    """KiCad's `SCH_REFERENCE::GetRefNumber`: power symbols get a leading 0 (`#PWR01`)."""
    return f"{prefix}0{number}" if power else f"{prefix}{number}"


# ----------------------------------------------------------------------------------------------
# per-sheet document
# ----------------------------------------------------------------------------------------------


@dataclass
class _Doc:
    key: str  # "" = root; else the file name as referenced by Sheetfile
    path: Path
    root: Node
    is_root: bool
    touched: bool = False
    libs: dict[str, ResolvedSymbol] = field(default_factory=dict)  # lib_id -> embedded symbol

    @property
    def uuid(self) -> str:
        return self.root.get("uuid") or ""

    def lib_symbols(self) -> Node:
        ls = self.root.find("lib_symbols")
        if ls is None:
            ls = build("lib_symbols")
            self.insert(ls)
        return ls

    def insert(self, node: Node) -> Node:
        """Insert a top-level node where KiCad's writer would put it: items grouped by type in
        KiCad's order and, within a type, sorted by uuid (verified on every `sch upgrade`
        re-save and nightly IPC save: junctions, wires, symbols and sheets each come out in uuid
        order). A file whose existing group is not uuid-sorted (hand-edited) gets the node
        appended to the group instead, so untouched nodes never move."""
        rank = _RANK.get(node.head, 13)
        same = [
            (i, c) for i, c in enumerate(self.root.children) if isinstance(c, Node) and c.head == node.head
        ]
        idx: int | None = None
        if same:
            uuids = [_uuid_of(c) for _i, c in same]
            mine = _uuid_of(node)
            if mine and all(uuids) and uuids == sorted(uuids):
                idx = next((i for (i, _c), u in zip(same, uuids, strict=True) if u > mine), same[-1][0] + 1)
            else:
                idx = same[-1][0] + 1
        if idx is None:
            idx = len(self.root.children)
            for i, c in enumerate(self.root.children):
                if isinstance(c, Node) and _RANK.get(c.head, 13) > rank:
                    idx = i
                    break
        self.root.insert(idx, node)
        self.touched = True
        return node

    def items(self, head: str) -> list[Node]:
        return self.root.find_all(head)


@dataclass
class _Sym:
    node: Node
    lib_id: str
    at: tuple[float, float]
    rotation: int
    mirror: str | None
    unit: int
    uuid: str
    reference: str
    instances: dict[str, tuple[str, int]]  # path -> (reference, unit)
    body_style: int = 1  # De Morgan body style of the placed symbol (`body_style` / v7 `convert`)


# ----------------------------------------------------------------------------------------------
# writer
# ----------------------------------------------------------------------------------------------


class SchematicWriter:
    """Author and edit a KiCad schematic project on disk; see the module docstring.

    `epoch` holds the format stamps (`{"sch": 20260306, "sym": 20251024[, "pro": 3]}`) — what
    the capability record of the target install reports as `formats.sch` / `formats.sym` /
    `formats.pro`. `lib_resolver` is any object with `symbol(lib_id) -> obj` where `obj.node` is
    the flattened library symbol (`kiwrap.formats.libs.LibResolver` or
    `schematic_writer.FallbackLibResolver`).

    The writer never moves a file to a newer format on its own (docs/DECISIONS.md D21,
    requests/kiwrap-feature-requests.review.md item 1 (d): re-stamping the touched root upward left
    a mixed-epoch project KiCad 10.0 could not load): when the loaded sheets are older than
    `epoch`, the writer works in the sheets' own dialect and keeps their stamps. Only
    `allow_upgrade=True` (a sanctioned upgrade) restores the old "re-stamp touched sheets to
    `epoch`" behaviour. Sheets of different stamps are refused (E_EPOCH) unless upgrading."""

    def __init__(
        self,
        project_dir: Path,
        *,
        epoch: Mapping[str, int],
        lib_resolver: object,
        project_name: str | None = None,
        generator_version: str | None = None,
        paper: str = "A4",
        allow_upgrade: bool = False,
    ) -> None:
        self.project_dir = Path(project_dir)
        self.epoch = dict(epoch)
        self.allow_upgrade = allow_upgrade
        self.dialect: SchDialect = dialect_for(epoch, generator_version=generator_version)
        self._requested_dialect = self.dialect
        if not hasattr(lib_resolver, "symbol"):
            raise TypeError("lib_resolver must provide symbol(lib_id)")
        self.libs = lib_resolver
        self.paper = paper
        self._project_name = project_name
        self._docs: dict[str, _Doc] = {}
        self._root: _Doc | None = None
        self._missing: list[tuple[str, str]] = []

    # ---- loading ------------------------------------------------------------------------------
    @property
    def root_path(self) -> Path:
        return self._require_root().path

    @property
    def root_uuid(self) -> str:
        return self._require_root().uuid

    @property
    def project_name(self) -> str:
        if self._project_name:
            return self._project_name
        root = self._require_root()
        for sym_node in root.items("symbol"):
            inst = sym_node.find("instances")
            if inst is not None:
                proj = inst.find("project")
                if proj is not None and proj.atoms:
                    return proj.atoms[0].value
        return root.path.stem

    @property
    def sheet_keys(self) -> list[str]:
        return list(self._docs)

    @property
    def missing_sheets(self) -> list[tuple[str, str]]:
        """(parent key, sheet file) pairs whose file could not be found at load time."""
        return list(self._missing)

    def load(self, sheet_path: Path | str) -> None:
        """Load the root schematic at `sheet_path` (and every sheet file it references); a
        missing file starts a new, empty root document written there by `save()`."""
        path = Path(sheet_path)
        if not path.is_absolute():
            path = self.project_dir / path
        self._docs.clear()
        self._missing.clear()
        self.dialect = self._requested_dialect
        if path.is_file():
            root = _Doc("", path, load(path), is_root=True)
            if root.root.head != "kicad_sch":
                raise WriterError("E_NOT_SCHEMATIC", f"{path} is not a .kicad_sch document")
            self._check_stamp(root)
        else:
            root = _Doc(
                "", path, emit.document(self.dialect, _new_uuid(), root=True, paper=self.paper), is_root=True
            )
            root.touched = True
        self._root = root
        self._docs[""] = root
        self._load_children(root, ())
        self._keep_file_epoch()

    def _keep_file_epoch(self) -> None:
        """Work in the loaded sheets' own dialect when they are older than the requested epoch
        (never re-stamp upward, see the class docstring); refuse a project whose sheets carry
        different stamps (an older KiCad cannot load it as a whole) unless upgrading."""
        if self.allow_upgrade:
            return
        stamped: list[tuple[int, _Doc]] = []
        for doc in self._docs.values():
            if doc.touched and not doc.path.is_file():
                continue  # a new root this load created: stamped in the requested dialect
            try:
                stamped.append((read_stamp(doc.root), doc))
            except ValueError:
                continue
        if not stamped:
            return
        low = min(s for s, _ in stamped)
        high = max(s for s, _ in stamped)
        if low < self.dialect.sch_stamp:
            root = self._require_root()
            _gen, root_gv = read_generator(root.root)
            self.dialect = dialect_for_stamp(low, None, root_gv)
        if _era(high) != _era(low):
            names = ", ".join(f"{d.path.name} ({s})" for s, d in sorted(stamped, key=lambda x: x[0]))
            raise WriterError(
                "E_EPOCH",
                f"the sheets are in different KiCad file-format eras: {names}",
                fix="make them uniform with kw_project(action='upgrade') (or restore the snapshot before the mix); "
                "an older KiCad cannot load a project whose sheets are newer than its own format",
            )

    def _load_children(self, doc: _Doc, chain: tuple[Path, ...]) -> None:
        for sh in doc.items("sheet"):
            file = _prop_value(sh, "Sheetfile") or _prop_value(sh, "Sheet file")
            if not file:
                continue
            key = file
            if key in self._docs:
                continue
            child_path = (doc.path.parent / file).resolve()
            if not child_path.is_file():
                self._missing.append((doc.key, file))
                continue
            if child_path in chain:
                continue
            child = _Doc(key, child_path, load(child_path), is_root=False)
            self._check_stamp(child)
            self._docs[key] = child
            self._load_children(child, (*chain, doc.path.resolve()))

    def _check_stamp(self, doc: _Doc) -> None:
        """A file newer than the epoch cannot be edited for that epoch (an older KiCad would
        refuse it, docs/VERIFICATION.md claim 9); an older one is re-stamped on save the way
        KiCad's own writer does, its old-dialect nodes left as they are (newer parsers accept
        every older spelling)."""
        try:
            stamp = read_stamp(doc.root)
        except ValueError:
            return
        if stamp > self.dialect.sch_stamp:
            raise WriterError(
                "E_EPOCH",
                f"{doc.path.name} is stamped {stamp}, newer than the target epoch {self.dialect.sch_stamp}",
                fix="load the project with the epoch of the KiCad that wrote it (or upgrade the target)",
            )

    def _require_root(self) -> _Doc:
        if self._root is None:
            raise WriterError("E_NOT_LOADED", "call load(<root .kicad_sch>) first")
        return self._root

    def _doc(self, sheet: str) -> _Doc:
        self._require_root()
        if sheet in ("", "/", None):
            return self._docs[""]
        if sheet in self._docs:
            return self._docs[sheet]
        low = sheet.lower()
        for key, doc in self._docs.items():
            if key and (key.lower() == low or Path(key).stem.lower() == low or doc.path.name.lower() == low):
                return doc
        # a sheet name or a sheet element uuid anywhere in the hierarchy
        wanted = sheet.strip("/").split("/")[-1].lower()
        for doc in self._docs.values():
            for sh in doc.items("sheet"):
                name = _prop_value(sh, "Sheetname") or _prop_value(sh, "Sheet name")
                if name.lower() == wanted or _uuid_of(sh) == sheet:
                    file = _prop_value(sh, "Sheetfile") or _prop_value(sh, "Sheet file")
                    if file in self._docs:
                        return self._docs[file]
        raise WriterError(
            "E_SHEET_NOT_FOUND",
            f"no sheet {sheet!r}",
            fix="use '' for the root, a sheet file name or a sheet name",
            candidates=list(self._docs),
        )

    # ---- hierarchy ----------------------------------------------------------------------------
    def instance_paths(self, sheet: str = "") -> list[str]:
        """Every instance path of the sheet file (`/ROOT` for the root; a file placed twice has two)."""
        return self._paths(self._doc(sheet), ())

    def _paths(self, doc: _Doc, chain: tuple[str, ...]) -> list[str]:
        """Instance paths of a sheet file in KiCad's sheet order (page numbers), root first."""
        if doc.is_root:
            return ["/" + doc.uuid]
        if doc.key in chain:
            return []
        return [path for path, d in self._ordered_instances() if d is doc]

    @staticmethod
    def _sheet_page(sh: Node, parent_path: str) -> int:
        inst = sh.find("instances")
        if inst is not None:
            for proj in inst.find_all("project"):
                for p in proj.find_all("path"):
                    if p.atoms and p.atoms[0].value == parent_path:
                        page = p.get("page") or ""
                        return int(page) if page.isdigit() else 10**6
        return 10**6

    def _ordered_instances(self) -> list[tuple[str, _Doc]]:
        """(instance path, doc) depth-first, sheets in page order — KiCad's sheet list order
        (`SCH_SHEET_LIST` sorted by page number), which also decides which instance of a
        twice-placed sheet is "first" when a reference is given explicitly."""
        root = self._require_root()
        out: list[tuple[str, _Doc]] = []

        def walk(doc: _Doc, path: str, chain: tuple[str, ...]) -> None:
            out.append((path, doc))
            sheets = doc.items("sheet")
            ordered = sorted(enumerate(sheets), key=lambda t: (self._sheet_page(t[1], path), t[0]))
            for _i, sh in ordered:
                file = _prop_value(sh, "Sheetfile") or _prop_value(sh, "Sheet file")
                child = self._docs.get(file)
                if child is None or child.key in chain:
                    continue
                walk(child, f"{path}/{_uuid_of(sh)}", (*chain, doc.key))

        walk(root, "/" + root.uuid, ())
        return out

    def _next_page(self) -> str:
        return str(len(self._ordered_instances()) + 1)

    def _page_for(self, path: str) -> str:
        """The page number of instance `path`: its 1-based position in KiCad's sheet list order
        (`_ordered_instances`, which already contains the sheets of a just-placed instance, so
        counting the list would number a nested sheet one too high - reviewer finding, 2026-09-20)."""
        for i, (p, _doc) in enumerate(self._ordered_instances()):
            if p == path:
                return str(i + 1)
        return self._next_page()

    # ---- library symbols ----------------------------------------------------------------------
    def _embedded(self, doc: _Doc, lib_id: str) -> ResolvedSymbol | None:
        if lib_id in doc.libs:
            return doc.libs[lib_id]
        ls = doc.root.find("lib_symbols")
        if ls is None:
            return None
        for s in ls.find_all("symbol"):
            a = s.atoms
            if a and a[0].value == lib_id:
                resolved = describe_symbol(s, lib_id)
                doc.libs[lib_id] = resolved
                return resolved
        return None

    def _ensure_lib_symbol(self, doc: _Doc, lib_id: str) -> ResolvedSymbol:
        """The `lib_symbols` entry for `lib_id` (added from the resolver when missing)."""
        existing = self._embedded(doc, lib_id)
        if existing is not None:
            return existing
        try:
            resolved = getattr(self.libs, "symbol")(lib_id)  # noqa: B009 - duck-typed resolver
        except WriterError:
            raise
        except Exception as e:  # noqa: BLE001 - foreign resolver
            name = lib_id.split(":", 1)[-1]
            raise WriterError(
                "E_SYMBOL_NOT_FOUND",
                f"cannot resolve {lib_id!r}: {e}",
                fix=str(getattr(e, "fix", "") or "")
                or f"search with kw_lib_search(kind='symbol', query={name!r}) and use the id it returns",
                candidates=[str(c) for c in (getattr(e, "candidates", None) or [])],
            ) from e
        node = getattr(resolved, "node", None)
        if not isinstance(node, Node):
            raise WriterError("E_LIB_RESOLVER", f"resolver returned no node for {lib_id!r}")
        copy = node.copy()
        atoms = copy.atoms
        if atoms:
            atoms[0].value = lib_id  # "<Nickname>:<Name>" — claim 2 correction (a)
        else:
            copy.insert(1, Atom.string(lib_id))
        convert_lib_symbol(copy, self.dialect)
        ls = doc.lib_symbols()
        # KiCad keeps lib_symbols in a std::map: insert in sorted order for a minimal re-save diff
        idx = len(ls.children)
        for i, c in enumerate(ls.children):
            if isinstance(c, Node) and c.head == "symbol" and c.atoms and c.atoms[0].value > lib_id:
                idx = i
                break
        ls.insert(idx, copy)
        copy.reformat()  # library indentation depth differs from lib_symbols depth
        doc.touched = True
        described = describe_symbol(copy, lib_id)
        doc.libs[lib_id] = described
        return described

    # ---- symbols ------------------------------------------------------------------------------
    def _symbols(self, doc: _Doc) -> list[_Sym]:
        out: list[_Sym] = []
        for node in doc.items("symbol"):
            at = node.find("at")
            a = at.atoms if at is not None else []
            rot = int(round(a[2].as_float())) if len(a) > 2 else 0
            mirror_node = node.find("mirror")
            mirror = mirror_node.atoms[0].value if mirror_node is not None and mirror_node.atoms else None
            inst: dict[str, tuple[str, int]] = {}
            inst_node = node.find("instances")
            unit = int(node.get("unit") or 1)
            body_style = int(node.get("body_style") or node.get("convert") or 1)
            if inst_node is not None:
                for proj in inst_node.find_all("project"):
                    for path in proj.find_all("path"):
                        pa = path.atoms
                        if pa:
                            inst[pa[0].value] = (path.get("reference") or "", int(path.get("unit") or unit))
            out.append(
                _Sym(
                    node=node,
                    lib_id=node.get("lib_id") or "",
                    at=_pt(at),
                    rotation=rot,
                    mirror=mirror,
                    unit=unit,
                    uuid=_uuid_of(node),
                    reference=_prop_value(node, "Reference"),
                    instances=inst,
                    body_style=body_style,
                )
            )
        return out

    @staticmethod
    def _placed_lib_pins(lib: ResolvedSymbol, body_style: int) -> list[LibPin]:
        """The library pins a placed symbol instantiates: every unit, but only the pins of its
        own De Morgan body style (plus the shared style-0 ones) — `SCH_SYMBOL::UpdatePins` skips a
        library pin whose body style is set and differs from the symbol's, and a `sch upgrade`
        re-save drops any extra `(pin …)` entry (verified on 74xx:74LS00, 10.0.6)."""
        return [p for p in lib.pins if p.body_style in (0, body_style)]

    def _pin_endpoints(self, doc: _Doc, s: _Sym, lib: ResolvedSymbol | None = None) -> list[PinEndpoint]:
        lib = lib or self._embedded(doc, s.lib_id)
        if lib is None:
            return []
        pin_uuids: dict[str, str] = {}
        for p in s.node.find_all("pin"):
            if p.atoms:
                pin_uuids[p.atoms[0].value] = _uuid_of(p)
        out: list[PinEndpoint] = []
        for lp in self._placed_lib_pins(lib, s.body_style):
            pos = pin_sheet_position(Vec2(*s.at), s.rotation, s.mirror, lp.position)
            out.append(
                PinEndpoint(
                    number=lp.number,
                    name=lp.name,
                    x_mm=_f(pos.x),
                    y_mm=_f(pos.y),
                    electrical_type=lp.type,
                    unit=lp.unit,
                    hidden=lp.hidden,
                    uuid=pin_uuids.get(lp.number, ""),
                )
            )
        return out

    def _unit_pins(self, pins: Iterable[PinEndpoint], lib: ResolvedSymbol, unit: int) -> list[PinEndpoint]:
        """The end points of `unit` (shared unit-0 pins included) out of `_pin_endpoints`, which
        are already unique per (number, unit) because they carry one body style only."""
        return [p for p in pins if p.unit in (0, unit)]

    def _all_references(self) -> set[str]:
        refs: set[str] = set()
        for doc in self._docs.values():
            for s in self._symbols(doc):
                if s.reference:
                    refs.add(s.reference)
                for ref, _u in s.instances.values():
                    if ref:
                        refs.add(ref)
        return refs

    def _next_reference(self, prefix: str, *, power: bool, taken: set[str] | None = None) -> str:
        refs = taken if taken is not None else self._all_references()
        used: set[int] = set()
        for r in refs:
            p, n = _split_ref(r)
            if n is None:
                continue
            if p == prefix:  # `_split_ref("#PWR01")` is ("#PWR", 1): the power "0" pad is digits
                used.add(n)
        n = 1
        while n in used:
            n += 1
        return _format_ref(prefix, n, power)

    def add_symbol(
        self,
        lib_id: str,
        ref: str | None,
        value: str | None,
        at_mm: tuple[float, float],
        rotation: int = 0,
        mirror: str | None = None,
        unit: int = 1,
        fields: dict[str, str] | None = None,
        footprint: str | None = None,
        sheet: str = "",
        *,
        dnp: bool = False,
        in_bom: bool = True,
        on_board: bool = True,
        snap_to_grid: bool = True,
    ) -> SymbolPlacement:
        """Place `lib_id` at `at_mm` (mm, snapped to the 1.27 grid) and return its absolute pins.

        `ref=None` allocates the next free reference for the library prefix (`R7`, `#PWR03`);
        `unit` > 1 places another unit of an existing multi-unit reference. Existing wires whose
        interior a new pin end lands on are split so the pin connects."""
        doc = self._doc(sheet)
        rotation = int(rotation) % 360
        if rotation not in (0, 90, 180, 270):
            raise WriterError("E_ROTATION", "symbol rotation must be 0, 90, 180 or 270")
        if mirror not in (None, "x", "y"):
            raise WriterError("E_MIRROR", "mirror must be None, 'x' or 'y'")
        lib = self._ensure_lib_symbol(doc, lib_id)
        if unit < 1 or unit > max(lib.units, 1):
            raise WriterError("E_UNIT", f"{lib_id} has {lib.units} unit(s); unit {unit} does not exist")
        x, y = (emit.snap(at_mm[0]), emit.snap(at_mm[1])) if snap_to_grid else (_f(at_mm[0]), _f(at_mm[1]))
        power = lib.is_power or lib.reference_prefix.startswith("#")
        paths = self._paths(doc, ())
        if not paths:
            raise WriterError("E_SHEET_UNPLACED", f"sheet file {doc.key!r} is not instantiated anywhere")
        taken = self._all_references()
        prefix = lib.reference_prefix or "U"
        if ref is None or ref.endswith("?"):
            ref = self._next_reference(prefix, power=power, taken=taken)
        else:
            self._check_reference(doc, ref, lib_id, unit)
        refs: dict[str, str] = {}
        for i, p in enumerate(paths):
            if i == 0:
                refs[p] = ref
            else:
                existing = self._instance_ref_for(ref, lib_id, unit, p)
                if existing is None:
                    existing = self._next_reference(prefix, power=power, taken=taken)
                refs[p] = existing
            taken.add(refs[p])
        sym_uuid = _new_uuid()
        props = self._property_nodes(lib, ref, value, footprint, fields, (x, y), rotation, mirror, power)
        pin_numbers = [p.number for p in self._placed_lib_pins(lib, 1)]  # body style 1: all units
        pin_uuids = [_new_uuid() for _ in pin_numbers]
        node = emit.symbol(
            self.dialect,
            lib_id=lib_id,
            at=(x, y),
            rotation=rotation,
            mirror=mirror,
            unit=unit,
            uuid=sym_uuid,
            properties=props,
            pin_numbers=pin_numbers,
            pin_uuids=pin_uuids,
            instance_entries=[(p, refs[p], unit) for p in paths],
            project=self.project_name,
            dnp=dnp,
            in_bom=in_bom,
            on_board=on_board,
        )
        doc.insert(node)
        s = _Sym(
            node, lib_id, (x, y), rotation, mirror, unit, sym_uuid, ref, {p: (refs[p], unit) for p in paths}
        )
        all_pins = self._pin_endpoints(doc, s, lib)
        unit_pins = self._unit_pins(all_pins, lib, unit)
        # a pin end landing on an existing wire's interior: split the wire there (claim 2 (B))
        touched_points: list[tuple[float, float]] = []
        for pin in unit_pins:
            if pin.hidden:
                continue
            if self._split_wires_at(doc, pin.at_mm):
                touched_points.append(pin.at_mm)
        self._update_junctions(doc, touched_points)
        return SymbolPlacement(
            uuid=sym_uuid, ref=ref, pins=unit_pins, lib_id=lib_id, unit=unit, sheet=doc.key, references=refs
        )

    def _instance_ref_for(self, ref: str, lib_id: str, unit: int, path: str) -> str | None:
        """For a second unit of a multi-unit symbol on a multiply-instantiated sheet: reuse the
        reference the first unit already carries on `path`."""
        for doc in self._docs.values():
            for s in self._symbols(doc):
                if s.lib_id != lib_id or s.reference != ref:
                    continue
                inst = s.instances.get(path)
                if inst is not None and inst[0]:
                    return inst[0]
        return None

    def _check_reference(self, doc: _Doc, ref: str, lib_id: str, unit: int) -> None:
        for d in self._docs.values():
            for s in self._symbols(d):
                refs = {s.reference} | {r for r, _u in s.instances.values()}
                if ref not in refs:
                    continue
                if s.lib_id != lib_id:
                    raise WriterError(
                        "E_REF_TAKEN",
                        f"reference {ref!r} is already used by {s.lib_id} on sheet {d.key or 'root'!r}",
                        fix="pass ref=None to allocate the next free reference",
                    )
                units = {u for _r, u in s.instances.values()} | {s.unit}
                if unit in units and d is doc:
                    raise WriterError(
                        "E_REF_TAKEN",
                        f"{ref} unit {unit} is already placed on sheet {d.key or 'root'!r}",
                        fix="place another unit (unit=N) or use a new reference",
                    )

    def _property_nodes(
        self,
        lib: ResolvedSymbol,
        ref: str,
        value: str | None,
        footprint: str | None,
        fields: Mapping[str, str] | None,
        at: tuple[float, float],
        rotation: int,
        mirror: str | None,
        power: bool,
    ) -> list[Node]:
        d = self.dialect
        lib_props: dict[str, Node] = {}
        order: list[str] = []
        for p in lib.node.find_all("property"):
            a = p.atoms
            if a:
                lib_props[a[0].value] = p
                order.append(a[0].value)
        values: dict[str, str] = {
            "Reference": ref,
            "Value": value if value is not None else lib.properties.get("Value", lib.name),
            "Footprint": footprint if footprint is not None else lib.properties.get("Footprint", ""),
            "Datasheet": lib.properties.get("Datasheet", d.datasheet_default) or d.datasheet_default,
        }
        if d.description_property:
            values["Description"] = lib.properties.get("Description", "")
        names = ["Reference", "Value", "Footprint", "Datasheet"] + (
            ["Description"] if d.description_property else []
        )
        for n in order:
            if n not in names and not n.startswith("ki_"):
                names.append(n)
                values[n] = lib.properties.get(n, "")
        for k, v in (fields or {}).items():
            if k not in names:
                names.append(k)
            values[k] = v
        out: list[Node] = []
        for n in names:
            lp = lib_props.get(n)
            pos, angle, eff, hidden = self._field_layout(lp, at, rotation, mirror)
            if n in ("Footprint", "Datasheet", "Description") or n.startswith("Sim."):
                hidden = True
            if n == "Reference" and power:
                hidden = True
            if n not in ("Reference", "Value", "Footprint", "Datasheet", "Description") and lp is None:
                hidden = False
            out.append(
                emit.property_node(
                    d,
                    n,
                    values[n],
                    pos,
                    angle,
                    hidden=hidden,
                    effects_node=eff,
                    placed=True,
                )
            )
        return out

    def _field_layout(
        self, lib_prop: Node | None, at: tuple[float, float], rotation: int, mirror: str | None
    ) -> tuple[tuple[float, float], int, Node | None, bool]:
        """Sheet position / angle / effects copy / hidden flag of a field placed from its library
        definition (library field offsets go through the same transform as pins)."""
        if lib_prop is None:
            return (at, 0, None, False)
        lat = lib_prop.find("at")
        la = lat.atoms if lat is not None else []
        lx = la[0].as_float() if len(la) > 0 else 0.0
        ly = la[1].as_float() if len(la) > 1 else 0.0
        langle = int(round(la[2].as_float())) if len(la) > 2 else 0
        pos = pin_sheet_position(Vec2(*at), rotation, mirror, Vec2(lx, ly))
        angle = langle
        if rotation in (90, 270):
            angle = 90 if langle % 180 == 0 else 0
        eff = lib_prop.find("effects")
        eff_copy = eff.copy() if eff is not None else None
        hidden = _hidden(lib_prop) or (eff is not None and _hidden(eff))
        return ((_f(pos.x), _f(pos.y)), angle, eff_copy, hidden)

    # ---- power --------------------------------------------------------------------------------
    def add_power(
        self,
        net: str,
        at_mm: tuple[float, float],
        rotation: int = 0,
        sheet: str = "",
        flag: bool = False,
        *,
        library: str = "power",
        value: str | None = None,
    ) -> SymbolPlacement:
        """Place the `power:<net>` symbol (or `power:PWR_FLAG` when `flag=True`) at `at_mm`.

        Every net driven only by power_in / passive pins needs a PWR_FLAG or ERC reports
        `power_pin_not_driven` (claim 2, negative control A)."""
        lib_id = f"{library}:PWR_FLAG" if flag else f"{library}:{net}"
        return self.add_symbol(lib_id, None, value, at_mm, rotation, None, 1, None, None, sheet)

    # ---- wires --------------------------------------------------------------------------------
    def _wires(self, doc: _Doc) -> list[tuple[tuple[float, float], tuple[float, float], Node]]:
        out = []
        for w in doc.items("wire"):
            pts = w.find("pts")
            xy = pts.find_all("xy") if pts is not None else []
            if len(xy) >= 2:
                out.append((_pt(xy[0]), _pt(xy[-1]), w))
        return out

    def _connection_points(self, doc: _Doc) -> list[tuple[float, float]]:
        """Pin ends, sheet pins, labels, junctions and no-connects: where a wire must be split."""
        pts: list[tuple[float, float]] = []
        for s in self._symbols(doc):
            lib = self._embedded(doc, s.lib_id)
            if lib is None:
                continue
            for p in self._unit_pins(self._pin_endpoints(doc, s, lib), lib, s.unit):
                if not p.hidden:
                    pts.append(p.at_mm)
        for sh in doc.items("sheet"):
            for sp in sh.find_all("pin"):
                pts.append(_pt(sp.find("at")))
        for head in ("junction", "no_connect", "label", "global_label", "hierarchical_label"):
            for n in doc.items(head):
                pts.append(_pt(n.find("at")))
        return pts

    def _pin_like_points(self, doc: _Doc) -> list[tuple[float, float]]:
        """Points that count as a "pin" for KiCad's junction rule: symbol pins and sheet pins
        (`SCH_SCREEN::IsExplicitJunctionNeeded` counts `SCH_SYMBOL_T` / `SCH_SHEET_T` items
        connected at the point). Labels are NOT pins there: a label on a wire's interior needs no
        junction, and an extra dot at such a label breaks the net on 9.0.9 (kicad-cli 9 splits
        `/VIN` into `/VIN` + `Net-(U1-VI)`; verified, tests/test_e2e_authoring.py)."""
        pts: list[tuple[float, float]] = []
        for s in self._symbols(doc):
            lib = self._embedded(doc, s.lib_id)
            if lib is None:
                continue
            for p in self._unit_pins(self._pin_endpoints(doc, s, lib), lib, s.unit):
                if not p.hidden:
                    pts.append(p.at_mm)
        for sh in doc.items("sheet"):
            for sp in sh.find_all("pin"):
                pts.append(_pt(sp.find("at")))
        return pts

    def _split_wires_at(self, doc: _Doc, point: tuple[float, float]) -> bool:
        """Split every existing wire whose interior contains `point`; True when one was split."""
        split = False
        for a, b, node in self._wires(doc):
            if not _interior(point, a, b):
                continue
            pts = node.find("pts")
            assert pts is not None
            xy = pts.find_all("xy")
            # first half keeps the node (and its uuid): a -> point; second half is a fresh wire
            end = xy[-1].atoms
            end[0].value = point[0]
            end[1].value = point[1]
            second = emit.wire(self.dialect, point, b, _new_uuid())
            doc.insert(second)
            doc.touched = True
            split = True
        return split

    def add_wire(
        self, points_mm: Sequence[tuple[float, float]], sheet: str = "", *, snap_to_grid: bool = True
    ) -> list[str]:
        """Draw a polyline of wire segments through `points_mm` and return the uuids created.

        Points snap to 1.27 mm; each segment is split at every pin end / sheet pin / wire end /
        junction / label it passes over, existing wires are split where a new end lands on their
        interior, and junctions are added where KiCad would draw one."""
        doc = self._doc(sheet)
        if len(points_mm) < 2:
            raise WriterError("E_WIRE_POINTS", "add_wire needs at least two points")
        pts = [(emit.snap(x), emit.snap(y)) if snap_to_grid else (_f(x), _f(y)) for x, y in points_mm]
        sites = self._connection_points(doc)
        for a, b, _n in self._wires(doc):
            sites.extend((a, b))
        created: list[str] = []
        touched: list[tuple[float, float]] = []
        for a, b in zip(pts, pts[1:], strict=False):
            if _same(a, b):
                continue
            inner = [s for s in sites if _interior(s, a, b)]
            inner.sort(key=lambda s: (s[0] - a[0]) ** 2 + (s[1] - a[1]) ** 2)
            chain: list[tuple[float, float]] = [a]
            for s in inner:
                if not _same(chain[-1], s):
                    chain.append(s)
            chain.append(b)
            for p0, p1 in zip(chain, chain[1:], strict=False):
                for p in (p0, p1):
                    self._split_wires_at(doc, p)
                node = emit.wire(self.dialect, p0, p1, _new_uuid())
                doc.insert(node)
                created.append(_uuid_of(node))
                touched.extend((p0, p1))
        self._update_junctions(doc, touched)
        return created

    def _junction_needed(
        self, doc: _Doc, p: tuple[float, float], pin_points: Sequence[tuple[float, float]]
    ) -> bool:
        """`SCH_SCREEN::IsExplicitJunctionNeeded` (eeschema/sch_screen.cpp) for wires: every wire
        ending at or passing through `p` counts (a mid-point counts as TWO ends, the junction
        would split it), two overlapping parallel wires in the same quadrant count once; a
        junction is needed with a pin and more than two ends in total, or with non-parallel
        wires and more than two ends."""
        lines: list[tuple[tuple[float, float], tuple[float, float]]] = []
        for a, b, _n in self._wires(doc):
            if _same(a, p) or _same(b, p) or _interior(p, a, b):
                lines.append((a, b))
        end_count = len(lines)
        has_nonparallel = False
        removed_overlapping = False
        for i, (a, b) in enumerate(lines):
            if not (_same(a, p) or _same(b, p)):
                end_count += 1
            for c, d in lines[i + 1 :]:
                if not _parallel(a, b, c, d):
                    has_nonparallel = True
                elif not removed_overlapping and _same_quadrant(a, b, c, d, p):
                    removed_overlapping = True
        if removed_overlapping:
            end_count -= 1
        pins = sum(1 for q in pin_points if _same(q, p))
        if pins and pins + end_count > 2:
            return True
        return has_nonparallel and end_count > 2

    def _update_junctions(self, doc: _Doc, points: Iterable[tuple[float, float]]) -> list[str]:
        seen: list[tuple[float, float]] = []
        for p in points:
            if not any(_same(p, q) for q in seen):
                seen.append(p)
        if not seen:
            return []
        existing = [_pt(j.find("at")) for j in doc.items("junction")]
        pins = self._pin_like_points(doc)
        added: list[str] = []
        for p in seen:
            if any(_same(p, e) for e in existing):
                continue
            if self._junction_needed(doc, p, pins):
                node = emit.junction(self.dialect, p, _new_uuid())
                doc.insert(node)
                existing.append(p)
                added.append(_uuid_of(node))
        return added

    # ---- labels / no-connects -----------------------------------------------------------------
    def add_label(
        self,
        text: str,
        at_mm: tuple[float, float],
        kind: str = "local",
        rotation: int = 0,
        sheet: str = "",
        *,
        shape: str = "input",
        size: float = 1.27,
    ) -> str:
        """Add a local / global / hierarchical label at `at_mm` (snapped); returns its uuid.

        Labels connect anywhere along a wire, so no splitting happens; a hierarchical label
        connects the sheet's pin of the same name (see `add_sheet`)."""
        doc = self._doc(sheet)
        if kind not in _LABEL_HEADS:
            raise WriterError("E_LABEL_KIND", f"kind must be one of {sorted(_LABEL_HEADS)}")
        if shape not in _SHAPES:
            raise WriterError("E_LABEL_SHAPE", f"shape must be one of {_SHAPES}")
        at = (emit.snap(at_mm[0]), emit.snap(at_mm[1]))
        uid = _new_uuid()
        if kind == "local":
            node = emit.label(self.dialect, text, at, rotation, uid, size)
        elif kind == "global":
            node = emit.global_label(self.dialect, text, at, rotation, uid, shape, size)
        else:
            node = emit.hierarchical_label(self.dialect, text, at, rotation, uid, shape, size)
        doc.insert(node)
        self._update_junctions(doc, [at])
        return uid

    def add_no_connect(self, at_mm: tuple[float, float], sheet: str = "") -> str:
        doc = self._doc(sheet)
        at = (emit.snap(at_mm[0]), emit.snap(at_mm[1]))
        node = emit.no_connect(self.dialect, at, _new_uuid())
        doc.insert(node)
        return _uuid_of(node)

    # ---- sheets -------------------------------------------------------------------------------
    def add_sheet(
        self,
        name: str,
        file: str,
        at_mm: tuple[float, float],
        size_mm: tuple[float, float],
        pins: Sequence[tuple[str, str, str]] = (),
        sheet: str = "",
        *,
        pin_pitch_mm: float = 2.54,
    ) -> SheetPlacement:
        """Place a hierarchical sheet symbol for `file` (created empty when it does not exist;
        placing an existing file again makes a second instance of it).

        `pins` are `(name, side, kind)` with side in left/right/top/bottom and kind in
        input/output/bidirectional/tri_state/passive; they are laid out along the side on the
        grid from the top-left corner. The sheet file gets its own instance path
        `<parent path>/<sheet uuid>` and every symbol already in it gains a reference there."""
        parent = self._doc(sheet)
        x, y = emit.snap(at_mm[0]), emit.snap(at_mm[1])
        w, h = emit.snap(size_mm[0]), emit.snap(size_mm[1])
        if w <= 0 or h <= 0:
            raise WriterError("E_SHEET_SIZE", "sheet size must be positive")
        file_name = file if file.endswith(".kicad_sch") else file + ".kicad_sch"
        if (
            not file_name.removesuffix(".kicad_sch")
            or Path(file_name).name != file_name
            or Path(file_name).is_absolute()
            or file_name.startswith(("~", "."))
            or ":" in file_name
        ):
            raise WriterError(
                "E_SHEET_FILE",
                f"sheet file {file!r} must be a bare file name inside the project directory",
                fix="pass the file name only, e.g. 'blinker' or 'blinker.kicad_sch' (no directories, no drive)",
            )
        sheet_uuid = _new_uuid()
        pin_nodes: list[Node] = []
        endpoints: list[SheetPinEndpoint] = []
        per_side: dict[str, int] = {}
        for pname, side, kind in pins:
            if side not in _SIDES:
                raise WriterError("E_SHEET_PIN_SIDE", f"side must be one of {sorted(_SIDES)}")
            if kind not in _SHAPES:
                raise WriterError("E_SHEET_PIN_KIND", f"kind must be one of {_SHAPES}")
            i = per_side.get(side, 0)
            per_side[side] = i + 1
            step = emit.snap(pin_pitch_mm) * (i + 1)
            if side == "left":
                px, py = x, y + step
            elif side == "right":
                px, py = x + w, y + step
            elif side == "top":
                px, py = x + step, y
            else:
                px, py = x + step, y + h
            px, py = _f(px), _f(py)
            if (side in ("left", "right") and py > y + h) or (side in ("top", "bottom") and px > x + w):
                raise WriterError("E_SHEET_PINS", f"too many pins on side {side!r} for a {w} x {h} mm sheet")
            pu = _new_uuid()
            pin_nodes.append(emit.sheet_pin(self.dialect, pname, kind, (px, py), _SIDES[side], pu))
            endpoints.append(SheetPinEndpoint(pname, kind, side, px, py, pu))
        parent_paths = self._paths(parent, ())
        page = self._next_page()
        page_entries = [(p, str(int(page) + i)) for i, p in enumerate(parent_paths)]
        node = emit.sheet(
            self.dialect,
            at=(x, y),
            size=(w, h),
            uuid=sheet_uuid,
            name=name,
            file=file_name,
            pins=pin_nodes,
            project=self.project_name,
            page_entries=page_entries,
        )
        parent.insert(node)
        child = self._docs.get(file_name)
        new_paths = [f"{p}/{sheet_uuid}" for p in parent_paths]
        if child is None:
            child_path = parent.path.parent / file_name
            if child_path.is_file():
                child = _Doc(file_name, child_path, load(child_path), is_root=False)
                self._docs[file_name] = child
                self._load_children(child, (parent.path.resolve(),))
                for p in new_paths:
                    self._add_instance_path(child, p)
            else:
                child = _Doc(
                    file_name,
                    child_path,
                    emit.document(self.dialect, _new_uuid(), root=False, paper=self.paper),
                    is_root=False,
                )
                child.touched = True
                self._docs[file_name] = child
        else:
            for p in new_paths:
                self._add_instance_path(child, p)
        return SheetPlacement(
            uuid=sheet_uuid,
            name=name,
            file=child.path,
            pins=endpoints,
            instance_path=new_paths[0] if new_paths else "",
            page=page,
        )

    def _add_instance_path(self, doc: _Doc, new_path: str, _chain: tuple[str, ...] = ()) -> None:
        """A sheet file just got another instance: give every symbol in it a reference on
        `new_path` and every nested sheet a page entry, recursively."""
        if doc.key in _chain:
            return
        taken = self._all_references()
        renamed: dict[str, str] = {}
        for s in self._symbols(doc):
            if new_path in s.instances:
                continue
            base_ref = s.reference or next((r for r, _u in s.instances.values()), "U?")
            prefix, _n = _split_ref(base_ref)
            lib = self._embedded(doc, s.lib_id)
            power = bool(
                lib and (lib.is_power or lib.reference_prefix.startswith("#"))
            ) or base_ref.startswith("#")
            new_ref = renamed.get(base_ref)
            if new_ref is None:
                new_ref = self._next_reference(prefix, power=power, taken=taken)
                taken.add(new_ref)
                renamed[base_ref] = new_ref
            self._set_instance(s.node, new_path, new_ref, s.unit)
            doc.touched = True
        for sh in doc.items("sheet"):
            inst = sh.find("instances")
            page = self._page_for(f"{new_path}/{_uuid_of(sh)}")
            if inst is None:
                inst = emit.sheet_instances_block(self.dialect, self.project_name, [(new_path, page)])
                sh.append(inst)
            else:
                proj = inst.find("project")
                if proj is None:
                    proj = inst.append(build("project", self.project_name))
                if not any(p.atoms and p.atoms[0].value == new_path for p in proj.find_all("path")):
                    proj.append(build("path", new_path, build("page", page)))
            doc.touched = True
            file = _prop_value(sh, "Sheetfile") or _prop_value(sh, "Sheet file")
            child = self._docs.get(file)
            if child is not None:
                self._add_instance_path(child, f"{new_path}/{_uuid_of(sh)}", (*_chain, doc.key))

    def _set_instance(self, node: Node, path: str, reference: str, unit: int) -> None:
        inst = node.find("instances")
        if inst is None:
            inst = emit.instances(self.dialect, self.project_name, [(path, reference, unit)])
            node.append(inst)
            return
        proj = inst.find("project")
        if proj is None:
            proj = inst.append(build("project", self.project_name))
        for p in proj.find_all("path"):
            if p.atoms and p.atoms[0].value == path:
                p.set("reference", reference)
                p.set("unit", unit)
                return
        proj.append(build("path", path, build("reference", reference), build("unit", unit)))

    def _remove_instance_prefix(self, doc: _Doc, prefix: str, _chain: tuple[str, ...] = ()) -> None:
        if doc.key in _chain:
            return
        for s in self._symbols(doc):
            inst = s.node.find("instances")
            if inst is None:
                continue
            for proj in inst.find_all("project"):
                for p in proj.find_all("path"):
                    if p.atoms and p.atoms[0].value.startswith(prefix):
                        proj.remove(p)
                        doc.touched = True
        for sh in doc.items("sheet"):
            inst = sh.find("instances")
            if inst is not None:
                for proj in inst.find_all("project"):
                    for p in proj.find_all("path"):
                        if p.atoms and p.atoms[0].value.startswith(prefix):
                            proj.remove(p)
                            doc.touched = True
            file = _prop_value(sh, "Sheetfile") or _prop_value(sh, "Sheet file")
            child = self._docs.get(file)
            if child is not None:
                self._remove_instance_prefix(child, prefix, (*_chain, doc.key))

    # ---- edit / delete ------------------------------------------------------------------------
    def find(self, uuid: str) -> tuple[str, Node] | None:
        """(sheet key, node) of the top-level item with `uuid`, or None."""
        for doc in self._docs.values():
            for node in doc.root.nodes:
                if node.head in _EDITABLE and _uuid_of(node) == uuid:
                    return doc.key, node
        return None

    def edit(self, uuid: str, changes: Mapping[str, object]) -> None:
        """Apply `changes` to the item `uuid`, touching only the atoms that differ.

        Keys (symbols unless noted): `at`/`at_mm`/`position` (x, y); `rotation`; `mirror`
        (None|'x'|'y'); `reference`/`ref`; `value`; `footprint`; `dnp`; `in_bom`; `on_board`;
        `exclude_from_sim`; `unit`; `properties`/`fields` ({name: value}); `text` (labels);
        `shape` (global / hierarchical labels); `name` / `file` (sheets); `points` (wires)."""
        found = self.find(uuid)
        if found is None:
            raise WriterError("E_UUID", f"no item with uuid {uuid}")
        key, node = found
        doc = self._docs[key]
        head = node.head
        for k, v in changes.items():
            if k in ("at", "at_mm", "position"):
                assert isinstance(v, Sequence)
                at = node.find("at")
                x, y = emit.snap(float(v[0])), emit.snap(float(v[1]))
                if at is None:
                    node.insert(1, build("at", x, y, 0))
                else:
                    atoms = at.atoms
                    atoms[0].value = x
                    atoms[1].value = y
            elif k == "rotation":
                at = node.find("at")
                if at is None:
                    raise WriterError("E_EDIT", f"{head} has no (at …)")
                atoms = at.atoms
                r = int(v) % 360  # type: ignore[call-overload]
                if len(atoms) > 2:
                    atoms[2].value = r
                else:
                    at.append(Atom.number(r))
            elif k == "mirror":
                m = node.find("mirror")
                if v in (None, "", False):
                    if m is not None:
                        node.remove(m)
                elif v in ("x", "y"):
                    if m is None:
                        at = node.find("at")
                        node.insert(
                            node.index(at) + 1 if at is not None else 1, build("mirror", Atom.symbol(str(v)))
                        )
                    else:
                        m.atoms[0].value = str(v)
                else:
                    raise WriterError("E_MIRROR", "mirror must be None, 'x' or 'y'")
            elif k in ("reference", "ref"):
                if head != "symbol":
                    raise WriterError("E_EDIT", f"{head} has no reference")
                self._set_reference(node, v)  # type: ignore[arg-type]
            elif k == "value":
                self._set_property(node, "Value", str(v))
            elif k == "footprint":
                self._set_property(node, "Footprint", str(v))
            elif k in ("properties", "fields"):
                assert isinstance(v, Mapping)
                for name, val in v.items():
                    self._set_property(node, str(name), str(val))
            elif k in ("dnp", "in_bom", "on_board", "exclude_from_sim"):
                if k == "exclude_from_sim" and not self.dialect.exclude_from_sim:
                    continue
                node.set(k, Atom.symbol("yes" if v else "no"))
            elif k == "unit":
                node.set("unit", int(v))  # type: ignore[call-overload]
                inst = node.find("instances")
                if inst is not None:
                    for proj in inst.find_all("project"):
                        for p in proj.find_all("path"):
                            p.set("unit", int(v))  # type: ignore[call-overload]
            elif k == "text":
                if head not in ("label", "global_label", "hierarchical_label"):
                    raise WriterError("E_EDIT", f"{head} has no text")
                _set_atom(node.atoms[0], str(v))
            elif k == "shape":
                if str(v) not in _SHAPES:
                    raise WriterError("E_LABEL_SHAPE", f"shape must be one of {_SHAPES}")
                node.set("shape", Atom.symbol(str(v)))
            elif k == "name" and head == "sheet":
                self._set_property(node, "Sheetname", str(v))
            elif k == "file" and head == "sheet":
                self._set_property(node, "Sheetfile", str(v))
            elif k == "points" and head in ("wire", "bus"):
                assert isinstance(v, Sequence) and len(v) >= 2
                pts = node.find("pts")
                assert pts is not None
                for xy, (x, y) in zip(pts.find_all("xy"), v, strict=False):
                    xy.atoms[0].value = emit.snap(float(x))
                    xy.atoms[1].value = emit.snap(float(y))
            else:
                raise WriterError("E_EDIT", f"unknown change {k!r} for {head}")
        doc.touched = True

    def _set_property(self, node: Node, name: str, value: str) -> None:
        p = _prop(node, name)
        if p is None:
            at = node.find("at")
            pos = _pt(at)
            last_prop = None
            for c in node.find_all("property"):
                last_prop = c
            new = emit.property_node(
                self.dialect, name, value, pos, 0, hidden=name in ("Footprint", "Datasheet", "Description")
            )
            node.insert(node.index(last_prop) + 1 if last_prop is not None else len(node.children), new)
            return
        _set_atom(p.atoms[1], value)

    def _set_reference(self, node: Node, ref: str | Mapping[str, str]) -> None:
        inst = node.find("instances")
        if isinstance(ref, Mapping):
            for path, r in ref.items():
                self._set_instance(node, path, r, int(node.get("unit") or 1))
            first = next(iter(ref.values()), None)
            if first is not None:
                self._set_property(node, "Reference", first)
            return
        self._set_property(node, "Reference", ref)
        if inst is not None:
            for proj in inst.find_all("project"):
                for p in proj.find_all("path"):
                    p.set("reference", ref)

    def delete(self, uuid: str) -> None:
        """Remove the top-level item with `uuid` (a deleted sheet symbol also drops the instance
        paths under it from the sheet file; the file itself stays on disk)."""
        found = self.find(uuid)
        if found is None:
            raise WriterError("E_UUID", f"no item with uuid {uuid}")
        key, node = found
        doc = self._docs[key]
        if node.head == "sheet":
            file = _prop_value(node, "Sheetfile") or _prop_value(node, "Sheet file")
            child = self._docs.get(file)
            if child is not None:
                for p in self._paths(doc, ()):
                    self._remove_instance_prefix(child, f"{p}/{_uuid_of(node)}")
        doc.root.remove(node)
        doc.touched = True

    # ---- annotate / footprints ----------------------------------------------------------------
    def annotate(
        self, scheme: str = "sheet", start_per_sheet: bool = False, *, keep_existing: bool = False
    ) -> dict[str, str]:
        """Assign references and return `{old: new}` for the ones that changed.

        Traversal is KiCad's: sheet instances depth-first, symbols sorted by X then Y within a
        sheet. `scheme="global"` numbers each prefix continuously across the hierarchy;
        `"sheet"` with `start_per_sheet=True` restarts every sheet at `page × 100 + 1` (KiCad's
        "sheet number × 100" option), without it the sheet order still drives the numbering but
        numbers stay continuous. Units of one multi-unit symbol keep one number. Power symbols
        get `#PWR01`-style numbers. `keep_existing=True` only fills unannotated (`?`) refs."""
        if scheme not in ("sheet", "global"):
            raise WriterError("E_ANNOTATE", "scheme must be 'sheet' or 'global'")
        mapping: dict[str, str] = {}
        counters: dict[str, int] = {}
        taken: set[str] = self._all_references() if keep_existing else set()
        # (path, old ref) -> new ref, so multi-unit symbols share a number per instance path
        assigned: dict[tuple[str, str], str] = {}
        for page_index, (path, doc) in enumerate(self._ordered_instances(), start=1):
            if start_per_sheet:
                counters = {}
            syms = self._symbols(doc)
            syms.sort(key=lambda s: (s.at[0], s.at[1]))
            for s in syms:
                lib = self._embedded(doc, s.lib_id)
                power = bool(lib and (lib.is_power or lib.reference_prefix.startswith("#")))
                old = s.instances.get(path, (s.reference, s.unit))[0] or s.reference
                prefix, num = _split_ref(old)
                if keep_existing and num is not None:
                    assigned[(path, old)] = old
                    continue
                key = (path, old) if num is not None else (path, s.uuid)
                new = assigned.get(key)
                if new is None:
                    base = page_index * 100 if start_per_sheet else 0
                    n = counters.get(prefix, base)
                    while True:
                        n += 1
                        cand = _format_ref(prefix, n, power)
                        if cand not in taken:
                            break
                    counters[prefix] = n
                    taken.add(cand)
                    new = cand
                    assigned[key] = new
                if new != old:
                    mapping[old if old not in mapping or mapping[old] == new else f"{old}@{path}"] = new
                self._set_instance(s.node, path, new, s.instances.get(path, (old, s.unit))[1])
                if path == self._paths(doc, ())[0]:
                    self._set_property(s.node, "Reference", new)
                doc.touched = True
        return mapping

    def assign_footprints(self, mapping: Mapping[str, str]) -> list[str]:
        """Set the Footprint property of the symbols referenced (any unit / instance); returns
        the references found."""
        done: list[str] = []
        for doc in self._docs.values():
            for s in self._symbols(doc):
                refs = {s.reference} | {r for r, _u in s.instances.values()}
                for ref, fpid in mapping.items():
                    if ref in refs:
                        self._set_property(s.node, "Footprint", fpid)
                        doc.touched = True
                        if ref not in done:
                            done.append(ref)
        return done

    # ---- queries ------------------------------------------------------------------------------
    def pins_of(self, ref: str, sheet: str | None = None) -> list[PinEndpoint]:
        """Absolute pin end points of every placed unit carrying reference `ref` (any sheet)."""
        out: list[PinEndpoint] = []
        docs = [self._doc(sheet)] if sheet is not None else list(self._docs.values())
        for doc in docs:
            for s in self._symbols(doc):
                refs = {s.reference} | {r for r, _u in s.instances.values()}
                if ref not in refs:
                    continue
                lib = self._embedded(doc, s.lib_id)
                if lib is None:
                    continue
                out.extend(self._unit_pins(self._pin_endpoints(doc, s, lib), lib, s.unit))
        return out

    def symbols(self, sheet: str = "") -> list[SymbolPlacement]:
        """Placed symbols of a sheet as `SymbolPlacement`s (unit pins only)."""
        doc = self._doc(sheet)
        out: list[SymbolPlacement] = []
        for s in self._symbols(doc):
            lib = self._embedded(doc, s.lib_id)
            pins = self._unit_pins(self._pin_endpoints(doc, s, lib), lib, s.unit) if lib else []
            out.append(
                SymbolPlacement(
                    uuid=s.uuid,
                    ref=s.reference,
                    pins=pins,
                    lib_id=s.lib_id,
                    unit=s.unit,
                    sheet=doc.key,
                    references={p: r for p, (r, _u) in s.instances.items()},
                )
            )
        return out

    def unit_suffix(self, unit: int) -> str:
        """`2` -> `"B"`: KiCad's unit letter as shown in `U1B`."""
        return unit_letter(unit)

    # ---- save ---------------------------------------------------------------------------------
    def save(self, *, write_project: bool = True) -> list[Path]:
        """Write every touched sheet (untouched nodes byte-identical) and return the paths.

        A minimal `.kicad_pro` beside the root is created when none exists and the epoch names
        a `pro` stamp (KiCad only honours project-local lib tables next to a project file)."""
        written: list[Path] = []
        for doc in self._docs.values():
            if not doc.touched:
                continue
            doc.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                old_stamp = read_stamp(doc.root)
            except ValueError:
                old_stamp = 0
            # an older sheet is re-stamped only by a sanctioned upgrade (`allow_upgrade`); without
            # it `_keep_file_epoch` already moved the dialect down to the sheets' own stamp
            if old_stamp < self.dialect.sch_stamp and (self.allow_upgrade or old_stamp == 0):
                set_stamp(doc.root, self.dialect.sch_stamp, "eeschema", self.dialect.generator_version)
            dump(doc.root, doc.path)
            doc.touched = False
            written.append(doc.path)
        if write_project and "pro" in self.epoch:
            pro = self._require_root().path.with_suffix(".kicad_pro")
            if not pro.exists():
                atomic_write_text(
                    pro, json.dumps(self._project_json(pro.name), indent=2) + "\n", newline="\n"
                )
                written.append(pro)
        return written

    def _project_json(self, filename: str) -> dict[str, object]:
        sheets: list[list[str]] = [[self.root_uuid, "Root"]]
        for path, doc in self._ordered_instances()[1:]:
            name = ""
            sheet_uuid = path.rsplit("/", 1)[-1]
            for d in self._docs.values():
                for sh in d.items("sheet"):
                    if _uuid_of(sh) == sheet_uuid:
                        name = _prop_value(sh, "Sheetname") or _prop_value(sh, "Sheet name")
            sheets.append([sheet_uuid, name or doc.key])
        return {
            "board": {
                "design_settings": {"defaults": {}, "rule_severities": {}, "rules": {}},
                "layer_presets": [],
                "viewports": [],
            },
            "boards": [],
            "cvpcb": {"equivalence_files": []},
            "libraries": {"pinned_footprint_libs": [], "pinned_symbol_libs": []},
            "meta": {"filename": filename, "version": int(self.epoch["pro"])},
            "net_settings": {
                "classes": [],
                "meta": {"version": 4},
                "net_colors": None,
                "netclass_assignments": None,
                "netclass_patterns": [],
            },
            "pcbnew": {"last_paths": {}, "page_layout_descr_file": ""},
            "schematic": {"legacy_lib_dir": "", "legacy_lib_list": []},
            "sheets": sheets,
            "text_variables": {},
        }


def _era(sch_stamp: int) -> str:
    """The dialect era a schematic stamp belongs to (`SchDialect.name`: "7" … "11")."""
    return dialect_for_stamp(sch_stamp).name


def _hidden(node: Node) -> bool:
    hide = node.find("hide")
    if hide is not None:
        atoms = hide.atoms
        return not atoms or atoms[0].value in ("yes", "true", "1")
    return any(a.kind == "symbol" and a.value == "hide" for a in node.atoms)


def default_resolver(
    install: object = None, config_root: object = None, project_dir: Path | None = None
) -> FallbackLibResolver:
    """The fallback resolver (used when `kiwrap.formats.libs` is not available)."""
    return FallbackLibResolver(install, config_root, project_dir)


__all__ += ["default_resolver"]
