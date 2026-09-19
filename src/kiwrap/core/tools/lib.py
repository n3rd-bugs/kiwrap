"""kw_lib_search / kw_lib_info: the "parts" stage tools (docs/ARCHITECTURE.md section 10).

`kw_lib_search(kind, query, filters, limit)` runs the FTS index (`kiwrap.formats.libs.index`)
over every symbol / footprint the target KiCad's resolved library tables reach (sandbox global
tables merged with the user's rows, plus the project's own tables when `project` is given) and
answers hits AND near-misses: rows that match the text but fail a filter, with the failing
filter named (`"8 pins (wanted 14)"`), or partial matches when nothing matches every token. The
skill's rule "never guess library ids (search first)" is served by this tool.

`kw_lib_info(id)` resolves one `Nick:Name` through `LibResolver`: for a symbol the flattened
pins in library mm (Y-up; the schematic writer transforms them with the verified rules), units,
properties, footprint filters and the footprints matching those filters (pad counts, pin-count
match); for a footprint the pads (mm, Y-down), courtyard rings and attributes.

Both are read-only, need no project (target_kicad selects the install; `project` adds its
tables and picks its target) and build the index on first use (~7 s for KiCad 10's 22.9k
symbols + 15.5k footprints on the reference machine; 3 ms afterwards; incremental on change).

Provider is always `file` (the library files are read by kiwrap; KiCad is not involved).
Library errors carry the `kiwrap.formats.libs` codes E_LIB_NOT_FOUND / E_LIB_UNSUPPORTED / E_LIB.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from kiwrap.core import planner
from kiwrap.core.state import KiwrapError, Receipt
from kiwrap.core.state.project import Project
from kiwrap.core.state.receipts import ReceiptOut
from kiwrap.core.tools._base import READ_ONLY, ToolContext, ToolSpec
from kiwrap.formats.libs import LibError, LibIndex, LibResolver, split_lib_id

MAX_LIMIT = 100
FILTER_KEYS = (
    "pins",
    "pins_min",
    "pins_max",
    "package",
    "keywords",
    "library",
    "power",
    "units",
    "smd",
    "through_hole",
    "include_hidden",
)


def lib_error(exc: LibError) -> KiwrapError:
    return KiwrapError(
        exc.code,
        exc.message,
        why=exc.why,
        fix=exc.fix,
        candidates=list(exc.candidates),
        data=dict(exc.data),
    )


def _project(spec: str | None) -> Project | None:
    return Project.from_path(spec) if spec else None


def _install(ctx: ToolContext, project: Project | None, target: str | None) -> Any:
    spec = target
    if spec is None and project is not None:
        spec = project.load_state().target_kicad
    return ctx.services.install(spec)


def make_index(ctx: ToolContext, project: Project | None, target_kicad: str | None) -> tuple[LibIndex, Any]:
    """The index for (target install, project) - a `Services.lib_index` hook wins when present."""
    install = _install(ctx, project, target_kicad)
    config = ctx.services.config_root(install)
    project_dir: Path | None = project.dir if project is not None else None
    factory = getattr(ctx.services, "lib_index", None)
    if callable(factory):
        return factory(install, config, ctx.services.storage_home, project_dir=project_dir), install
    resolver_factory = getattr(ctx.services, "lib_resolver", None)
    resolver: LibResolver | None = None
    if callable(resolver_factory):
        resolver = resolver_factory(install, config, project_dir)
    return LibIndex(
        install, config, ctx.services.storage_home, project_dir=project_dir, resolver=resolver
    ), install


class LibSearchArgs(BaseModel):
    kind: Literal["symbol", "footprint"] = Field(description="what to search: symbol | footprint")
    query: str = Field(
        default="",
        description="free text: part names (LM358, ESP32-S3-WROOM-1), package names (R_0603, SOIC-8), "
        "keywords (dual opamp); split on non-alphanumerics, all tokens must match (prefix / partial "
        "fallbacks are reported as near-misses). Empty = browse by filters (library, ...).",
    )
    filters: dict[str, Any] | None = Field(
        default=None,
        description="narrowing filters: pins (exact pin/pad count), pins_min, pins_max, package (substring of "
        "footprint filters / footprint / description for symbols, of name / tags for footprints), keywords "
        "(str or list, all must appear), library (nickname), power (bool, symbols), units (int, symbols), "
        "smd / through_hole (bool, footprints), include_hidden (bool). Rows failing a filter are returned "
        "as near_misses with the reason.",
    )
    limit: int = Field(default=20, ge=1, le=MAX_LIMIT, description="max hits (and max near-misses)")
    project: str | None = Field(
        default=None,
        description="project directory or .kicad_pro: adds its sym-/fp-lib-table rows (${KIPRJMOD}) and "
        "uses its target KiCad",
    )
    target_kicad: str | None = Field(
        default=None,
        description="KiCad to resolve libraries for (major, major.minor or semver); default newest",
    )
    rebuild: bool = Field(default=False, description="force a full index rebuild before searching")


class LibSearchData(BaseModel):
    kind: str
    query: str
    strategy: str = Field(description="exact | prefix | any | fuzzy | browse | none - how the text matched")
    total_text_matches: int
    hits: list[dict[str, Any]]
    near_misses: list[dict[str, Any]]
    filters: dict[str, Any]
    index: dict[str, Any] = Field(
        description="symbols / footprints / libraries counted, built timestamp, path"
    )
    build: dict[str, Any] | None = Field(
        default=None, description="present when this call (re)built the index"
    )
    kicad: str


class LibSearchOut(ReceiptOut):
    data: LibSearchData  # type: ignore[assignment]


def lib_search(ctx: ToolContext, args: LibSearchArgs) -> Receipt:
    filters = dict(args.filters or {})
    unknown = sorted(set(filters) - set(FILTER_KEYS))
    if unknown:
        raise KiwrapError(
            "E_BAD_ARGS",
            f"unknown filter key(s): {', '.join(unknown)}",
            why="filters is a fixed vocabulary",
            fix="use only: " + ", ".join(FILTER_KEYS),
            candidates=list(FILTER_KEYS),
        )
    project = _project(args.project)
    try:
        index, install = make_index(ctx, project, args.target_kicad)
        build = index.build(force=True) if args.rebuild else index.ensure()
        result = index.query(args.kind, args.query, filters, limit=args.limit)
        stats = index.stats()
    except LibError as exc:
        raise lib_error(exc) from exc
    warnings = [f"library index: {p}" for p in build.problems[:10]]
    data = {
        **result.as_dict(),
        "index": stats,
        "build": None if build.skipped else build.as_dict(),
        "kicad": install.version,
    }
    if not result.hits and not result.near_misses:
        warnings.append(
            "no match: try fewer / shorter tokens (the query must match every token), a keyword "
            "(e.g. 'opamp'), or kind='footprint' for package names"
        )
    return Receipt(
        provider_used=planner.provider_label(planner.FILE, install),
        warnings=warnings,
        next_suggested="kw_lib_info",
        data=data,
    )


class LibInfoArgs(BaseModel):
    id: str = Field(description="library id `Nick:Name`, e.g. 'Device:R' or 'Resistor_SMD:R_0603_1608Metric'")
    kind: Literal["auto", "symbol", "footprint"] = Field(
        default="auto", description="what the id names; auto = symbol table first, then footprint table"
    )
    unit: int | None = Field(
        default=None, ge=0, description="symbols: list the pins of this unit only (0 / omitted = every unit)"
    )
    include_node: bool = Field(
        default=False,
        description="symbols: include the flattened `lib_symbols` entry text (large); footprints: the .kicad_mod text",
    )
    footprint_limit: int = Field(
        default=30, ge=0, le=200, description="symbols: max matching footprints listed"
    )
    project: str | None = Field(
        default=None, description="project directory or .kicad_pro (its tables + target)"
    )
    target_kicad: str | None = Field(default=None, description="KiCad to resolve for; default newest")


class LibInfoData(BaseModel):
    kind: str
    id: str
    item: dict[str, Any] = Field(
        description="LibSymbol.as_dict() (pins in mm, Y-up) or LibFootprint.as_dict() (mm)"
    )
    footprint_matches: list[dict[str, Any]] = Field(
        default_factory=list,
        description="symbols: footprints matching ki_fp_filters with pad_count and pin_count_match",
    )
    node_text: str | None = None
    kicad: str


class LibInfoOut(ReceiptOut):
    data: LibInfoData  # type: ignore[assignment]


def lib_info(ctx: ToolContext, args: LibInfoArgs) -> Receipt:
    nick, name = split_lib_id(args.id)
    if not nick or not name:
        raise KiwrapError(
            "E_BAD_ARGS",
            f"{args.id!r} is not a library id",
            why="ids are <library nickname>:<item name>",
            fix="find ids with kw_lib_search",
            example="Device:R",
        )
    project = _project(args.project)
    degraded: list[str] = []
    warnings: list[str] = []
    try:
        index, install = make_index(ctx, project, args.target_kicad)
        resolver = index.resolver
        tables = resolver.library_tables()
        in_sym = tables.row("sym", nick) is not None
        in_fp = tables.row("fp", nick) is not None
        kind = args.kind
        if kind == "auto":
            if in_sym:
                kind = "symbol"
            elif in_fp:
                kind = "footprint"
            else:
                raise KiwrapError(
                    "E_LIB_NOT_FOUND",
                    f"nickname {nick!r} is in neither library table",
                    why="the global (sandbox) and project tables define no such symbol or footprint library",
                    fix="use kw_lib_search to find the id; nicknames are case-sensitive",
                    candidates=sorted(
                        {r.name for r in (*tables.sym, *tables.fp) if nick.lower() in r.name.lower()}
                    )[:20],
                )
        from kiwrap.formats import sexpr

        if kind == "symbol":
            sym = resolver.symbol(args.id)
            matches: list[dict[str, Any]] = []
            if sym.footprint_filters and args.footprint_limit:
                try:
                    index.ensure()
                    matches = index.footprints_for(
                        sym.footprint_filters, pin_count=sym.pin_count, limit=args.footprint_limit
                    )
                except LibError as exc:
                    degraded.append("footprint_matches_unavailable")
                    warnings.append(f"footprint matches unavailable: {exc.message}")
            if args.unit and args.unit > sym.units:
                warnings.append(f"unit {args.unit} > {sym.units} units; listing every unit")
            unit = args.unit if args.unit and args.unit <= sym.units else 0
            item = sym.as_dict(unit=unit)
            data: dict[str, Any] = {
                "kind": "symbol",
                "id": sym.lib_id,
                "item": item,
                "footprint_matches": matches,
                "node_text": sexpr.dumps(sym.node) if args.include_node else None,
                "kicad": install.version,
            }
            if sym.footprint and not matches:
                warnings.append(f"default footprint from the library: {sym.footprint}")
        else:
            fp = resolver.footprint(args.id)
            data = {
                "kind": "footprint",
                "id": fp.fpid,
                "item": fp.as_dict(),
                "footprint_matches": [],
                "node_text": sexpr.dumps(fp.node) if args.include_node else None,
                "kicad": install.version,
            }
            if fp.courtyard_open:
                warnings.append("courtyard has open chains (KiCad reports a malformed courtyard)")
            if not fp.courtyard:
                warnings.append("no closed courtyard on the footprint's side")
    except LibError as exc:
        raise lib_error(exc) from exc
    return Receipt(
        provider_used=planner.provider_label(planner.FILE, install),
        degraded=degraded,
        warnings=warnings,
        data=data,
    )


LIB_SEARCH = ToolSpec(
    name="kw_lib_search",
    title="Library search",
    description=(
        "Search the target KiCad's symbol or footprint libraries (FTS over names, descriptions, keywords, "
        "footprint filters, pin/pad counts). Returns hits AND near-misses (rows matching the text but failing a "
        "filter, with the reason; partial matches when nothing matches every token). Never guess a lib_id: "
        "search here, then kw_lib_info(id) for pins/pads. filters: pins, pins_min/max, package, keywords, "
        "library, power, units, smd, through_hole, include_hidden."
    ),
    input_model=LibSearchArgs,
    output_model=LibSearchOut,
    handler=lib_search,
    annotations=READ_ONLY,
)

LIB_INFO = ToolSpec(
    name="kw_lib_info",
    title="Library item info",
    description=(
        "Resolve one library id: symbols -> units, properties, footprint filters, pins with number / name / "
        "electrical type / library position in mm (Y-up; kw_sch_add_symbol returns the absolute sheet "
        "positions) and the footprints matching the filters (pad counts); footprints -> pads (number, shape, "
        "position/size mm, layers, drill), courtyard rings, attributes (smd / through_hole), bbox."
    ),
    input_model=LibInfoArgs,
    output_model=LibInfoOut,
    handler=lib_info,
    annotations=READ_ONLY,
)

LIB_TOOLS: tuple[ToolSpec, ...] = (LIB_SEARCH, LIB_INFO)
