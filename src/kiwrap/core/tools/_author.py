"""Shared plumbing for the authoring tools (`sch_author`, `pcb_author`): the writer / resolver /
IPC-twin contracts they code against, the epoch a writer stamps, the post-mutation validators
(ERC for schematic ops; DRC summary, schematic parity and the placement checks for board ops)
and the stage-machine bookkeeping every mutation shares (docs/ARCHITECTURE.md §11).

Contracts (implemented by concurrently built components; every name is resolved lazily, so a
missing component is a teaching `E_UNSUPPORTED`, never an import failure at server start):

    kiwrap.formats.schematic_writer.SchematicWriter(project_dir, *, epoch, lib_resolver)
        load(sheet_path); add_symbol(...) -> SymbolPlacement(uuid, ref, pins[PinEndpoint]);
        add_wire(points_mm, sheet) -> [uuid]; add_label(text, at_mm, kind, rotation, sheet) -> uuid;
        add_power(net, at_mm, rotation, sheet, flag) -> SymbolPlacement; add_no_connect(at_mm, sheet)
        -> uuid; add_sheet(name, file, at_mm, size_mm, pins, sheet) -> SheetPlacement;
        edit(uuid, changes); delete(uuid); annotate(scheme, start_per_sheet) -> {old: new};
        assign_footprints(mapping) -> [ref]; save() -> [Path]; pins_of(ref) -> [PinEndpoint]
    kiwrap.formats.board_writer.BoardWriter(project_dir, *, epoch, lib_resolver)
        load(board_path); setup(layers, rules, netclasses, outline, custom_rules); sync(netlist, *,
        remove_orphans, keep_positions) -> SyncReport; place(ref, at_mm, rotation, side, locked);
        add_zone(net, layers, polygon_mm, priority) -> uuid; add_track(pts, width_mm, layer, net)
        -> [uuid]; add_via(at_mm, size_mm, drill_mm, net, layers) -> uuid; delete(uuid); save() -> Path
    kiwrap.formats.libs.LibResolver(install, config_root, project_dir): symbol(lib_id) -> LibSymbol
        (footprint_filters, units, ...); kiwrap.formats.libs.index.LibIndex(install, config_root,
        storage_home).search(kind, query, filters, limit) -> [LibHit]
    kiwrap.backends.ipc.writes (KiCad 11 twins, called as twin(session, **the writer's kwargs) and
        returning the writer's dataclasses; a twin persists to the session's document):
        sch_add_symbol / sch_add_wire / sch_add_label / sch_add_power / sch_edit / sch_delete /
        sch_annotate; pcb_sync (raises Unsupported(fallback='file') unless the presence probe says
        ImportNetlist is handled headless) / pcb_place / pcb_add_zone / pcb_add_track / pcb_add_via /
        pcb_delete / pcb_refill

A test (or the integrator) may bind any of these as an attribute of the `Services` instance
(`services.schematic_writer = FakeWriter`); `backend()` prefers that over the lazy import.

Provider order (kiwrap.core.planner): `schematic.write`, `board.sync`, `board.place`,
`board.zones`, `board.route_primitives` are ipc11 -> file; `board.setup` is file only. The G13
hang guard (capability `ipc.schematic_update_shared_lib_symbol_hangs`, docs/UPSTREAM.md G13):
on an affected build no schematic write that touches a second unit of a multi-unit symbol is
attempted over IPC - the file engine answers and the receipt says why.
"""

from __future__ import annotations

import contextlib
import importlib
import inspect
import re
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import Any

from kiwrap.core import planner
from kiwrap.core.state import STAGES, KiwrapError
from kiwrap.core.state.project import Project
from kiwrap.core.state.stages import (
    STAGE_INPUTS,
    downgrade_below,
    record_stage,
    sha256_file,
    stage_at_least,
    stage_index,
)
from kiwrap.core.tools._base import Mutation, ToolContext, saves_in_format, scratch_folder, to_kiwrap_error
from kiwrap.core.tools._data import pick, rec
from kiwrap.core.tools.checks import (
    DRC_COSMETIC_PREFIX,
    DRC_MUST_FIX,
    DRC_REVIEW,
    _table,
    _text,
    classify_drc,
    violation_dict,
)
from kiwrap.core.tools.pcb_check import merge_off_board, norm_off_board, norm_overlap
from kiwrap.core.tools.pcb_get import _FileCache, ipc_session, read_connectivity

ProviderArg = Any  # Literal["auto", "ipc11", "file"] - declared on the tools' arg models

#: contract name -> (module, attribute, capability hint)
AUTHOR_BACKENDS: dict[str, tuple[str, str, str]] = {
    "schematic_writer": ("kiwrap.formats.schematic_writer", "SchematicWriter", "the schematic writer"),
    "board_writer": ("kiwrap.formats.board_writer", "BoardWriter", "the board writer"),
    "lib_resolver": ("kiwrap.formats.libs", "LibResolver", "the library resolver"),
    "lib_index": ("kiwrap.formats.libs.index", "LibIndex", "the library index"),
    "ipc_sch_add_symbol": ("kiwrap.backends.ipc.writes", "sch_add_symbol", "IPC schematic symbol placement"),
    "ipc_sch_add_wire": ("kiwrap.backends.ipc.writes", "sch_add_wire", "IPC schematic wires"),
    "ipc_sch_add_label": ("kiwrap.backends.ipc.writes", "sch_add_label", "IPC schematic labels"),
    "ipc_sch_add_power": ("kiwrap.backends.ipc.writes", "sch_add_power", "IPC schematic power symbols"),
    "ipc_sch_edit": ("kiwrap.backends.ipc.writes", "sch_edit", "IPC schematic edits"),
    "ipc_sch_delete": ("kiwrap.backends.ipc.writes", "sch_delete", "IPC schematic deletes"),
    "ipc_sch_annotate": ("kiwrap.backends.ipc.writes", "sch_annotate", "IPC schematic annotation"),
    "ipc_pcb_sync": ("kiwrap.backends.ipc.writes", "pcb_sync", "IPC ImportNetlist"),
    "ipc_pcb_place": ("kiwrap.backends.ipc.writes", "pcb_place", "IPC footprint placement"),
    "ipc_pcb_add_zone": ("kiwrap.backends.ipc.writes", "pcb_add_zone", "IPC zones"),
    "ipc_pcb_add_track": ("kiwrap.backends.ipc.writes", "pcb_add_track", "IPC tracks"),
    "ipc_pcb_add_via": ("kiwrap.backends.ipc.writes", "pcb_add_via", "IPC vias"),
    "ipc_pcb_delete": ("kiwrap.backends.ipc.writes", "pcb_delete", "IPC board deletes"),
    "ipc_pcb_refill": ("kiwrap.backends.ipc.writes", "pcb_refill", "IPC zone refill"),
    "ipc_sch_save": ("kiwrap.backends.ipc.writes", "sch_save", "IPC schematic save"),
    "ipc_pcb_save": ("kiwrap.backends.ipc.writes", "pcb_save", "IPC board save"),
    # the live GUI (kw_live) bound to the project reloads a file written on disk behind it
    "ipc_resync_bound": ("kiwrap.backends.ipc.sessions", "resync_bound_session", "the live GUI resync"),
}

G13_KEY = "ipc.schematic_update_shared_lib_symbol_hangs"
G16_KEY = "ipc.footprint_update_rotation_noise"
MAX_LISTED = 50  # violations / items listed inline in an authoring receipt


def backend(ctx: ToolContext, name: str) -> Callable[..., Any]:
    """The contract callable `name`: a binding on the Services instance when a test / the
    integrator set one, else the contract module's attribute imported now (E_UNSUPPORTED when the
    component is absent). The returned wrapper turns a `NotImplementedError` stub into
    E_UNSUPPORTED and exposes the real callable as `__wrapped__` (so `call_with_accepted` can
    read its signature)."""
    bound = getattr(ctx.services, name, None)
    module, attr, hint = AUTHOR_BACKENDS[name]
    fn: Any = bound
    if fn is None:
        try:
            fn = importlib.import_module(module)
            for part in attr.split("."):
                fn = getattr(fn, part)
        except (ImportError, AttributeError) as exc:
            raise KiwrapError(
                "E_UNSUPPORTED",
                f"{hint} is not available in this build ({module}.{attr}: {exc})",
                why="the component implementing it is not present in this kiwrap installation",
                fix=f"update kiwrap; this tool needs the {module} component",
                data={"module": module, "function": attr},
            ) from exc

    def call(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except NotImplementedError as exc:
            raise KiwrapError(
                "E_UNSUPPORTED",
                f"{hint} is not implemented yet ({module}.{attr})",
                why="the component implementing it is still a stub in this build",
                fix=f"update kiwrap; this tool needs the {module} component",
                data={"module": module, "function": attr},
            ) from exc

    call.__name__ = attr.replace(".", "_")
    call.__wrapped__ = fn  # type: ignore[attr-defined]
    return call


def call_with_accepted(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Call `fn` with only the keyword arguments its signature accepts (contracts allow extra
    kwargs, but a writer that landed without an optional one must still work)."""
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return fn(*args, **kwargs)
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return fn(*args, **kwargs)
    return fn(*args, **{k: v for k, v in kwargs.items() if k in params})


# ---- epochs / writers -------------------------------------------------------------------------------


def epoch_for(caps: Any, project: Project | None = None) -> dict[str, int]:
    """The format stamps a writer uses: the target KiCad's (`formats.*` capability keys - never a
    version number; docs/ARCHITECTURE.md §6), lowered to the project's own stamp per design kind
    when the files are older (review item 1 (d): a writer never re-stamps a file upward; only
    `kw_project upgrade` does, with the user's consent - D21). `effective_target` already picks an
    install writing the files' generation, so this only keeps an in-generation older stamp
    (the 10.0 demos' `(version 20260101)` schematics stay 20260101 instead of becoming 20260306)."""
    out: dict[str, int] = {}
    for kind in ("sch", "sym", "pcb", "pro"):
        try:
            val = caps[f"formats.{kind}"]
        except (KeyError, TypeError):
            continue
        if isinstance(val, (int, float)):
            out[kind] = int(val)
    if "sch" not in out and "pcb" not in out:
        raise KiwrapError(
            "E_UNSUPPORTED",
            "the capability record of the target KiCad has no formats.* stamps",
            why="the writers stamp files in the target's own format and need those keys",
            fix="kw_env(refresh=true) re-probes the install; else retarget with kw_project",
            data={"capability": "formats.sch"},
        )
    if project is not None:
        for kind, stamp in project.project_epochs().items():
            if kind in out and stamp < out[kind]:
                out[kind] = stamp
    return out


def lib_resolver(ctx: ToolContext, install: Any, config: Any, project: Project) -> Any:
    return backend(ctx, "lib_resolver")(install, config, project.dir)


def schematic_writer(ctx: ToolContext, install: Any, caps: Any, config: Any, project: Project) -> Any:
    """A loaded `SchematicWriter` on the project's root sheet, in the files' own format epoch."""
    writer = backend(ctx, "schematic_writer")(
        project.dir, epoch=epoch_for(caps, project), lib_resolver=lib_resolver(ctx, install, config, project)
    )
    writer.load(project.root_sch)
    return writer


def board_writer(ctx: ToolContext, install: Any, caps: Any, config: Any, project: Project) -> Any:
    """A loaded `BoardWriter` on the project's board, in the file's own format epoch."""
    writer = backend(ctx, "board_writer")(
        project.dir, epoch=epoch_for(caps, project), lib_resolver=lib_resolver(ctx, install, config, project)
    )
    writer.load(project.board)
    return writer


IpcCall = Callable[..., Any]


def _ipc_failure(exc: BaseException) -> KiwrapError:
    """An IPC twin that blew up (a wedged server, a proto the build lacks, a bare session) is
    "ipc11 unavailable for this write" - E_KICAD_FAILED, which the planner treats as a
    fall-through to the file writer; E_BAD_ARGS / E_UNSUPPORTED keep their meaning."""
    err = to_kiwrap_error(exc)
    if err.code == "E_INTERNAL":
        return KiwrapError(
            "E_KICAD_FAILED",
            f"IPC write failed: {type(exc).__name__}: {exc}",
            why="the headless api-server could not apply the write",
            fix="the file writer answers instead (degraded: ipc11_unavailable)",
        )
    return err


@contextlib.contextmanager
def ipc_batch(
    ctx: ToolContext, install: Any, config: Any, project: Project, path: Path, *, save: str
) -> Iterator[IpcCall]:
    """One headless session of `path` for a group of IPC twins: the yielded `call(twin, **kw)`
    runs `twin(session, **kw)` (only the kwargs the twin accepts); on a clean exit the document
    is saved in place through the `save` twin (`ipc_sch_save` / `ipc_pcb_save`: KiCad writes
    the file), and kiwrap's servers on the project are ALWAYS closed afterwards (PROCESS
    HYGIENE, and so the file on disk - not a live document - is what kicad-cli validates next;
    an unsaved document of a failed batch is simply discarded with the server)."""
    try:
        with ipc_session(ctx, install, config, path) as session:

            def call(twin: str, **kwargs: Any) -> Any:
                return call_with_accepted(backend(ctx, twin), session, **kwargs)

            try:
                yield call
                call(save)
            except KiwrapError:
                raise
            except Exception as exc:  # noqa: BLE001 - classified into the planner's vocabulary
                raise _ipc_failure(exc) from exc
    finally:
        release = getattr(ctx.services, "ipc_release_project", None)
        if callable(release):
            with contextlib.suppress(KiwrapError):
                release(project.dir)


def ipc_write(
    ctx: ToolContext, install: Any, config: Any, project: Project, path: Path, twin: str, **kwargs: Any
) -> Any:
    """One IPC twin in its own batch (session, twin, save, release)."""
    save = "ipc_sch_save" if twin.startswith("ipc_sch_") else "ipc_pcb_save"
    with ipc_batch(ctx, install, config, project, path, save=save) as call:
        return call(twin, **kwargs)


def g13_guard(caps: Any, *, multi_unit: bool) -> str | None:
    """The reason ipc11 must be skipped for this schematic write, or None. `multi_unit`: the
    write touches (or may touch) a second unit of a multi-unit symbol."""
    if multi_unit and caps.has(G13_KEY):
        return (
            "ipc11 skipped: this KiCad wedges its api-server on UpdateItems of a second unit of a "
            "multi-unit symbol (G13 hang guard); the file engine answered"
        )
    return None


def g16_guard(caps: Any, *, rotates: bool) -> str | None:
    """The reason ipc11 must be skipped for this board placement, or None. `rotates`: a
    placement changes a footprint's orientation. On affected builds `UpdateItems` with a changed
    orientation leaves 1 nm noise in the footprint's library coordinates (KiCad re-derives them
    through the inverse transform) and every later DRC reports `lib_footprint_mismatch`
    (docs/UPSTREAM.md G16, verified 10.99.0-4083: moves, flips and locks are exact)."""
    if rotates and caps.has(G16_KEY):
        return (
            "ipc11 skipped: this KiCad leaves 1 nm noise in a footprint rotated through UpdateItems "
            "(G16 guard: DRC would report lib_footprint_mismatch); the file engine answered"
        )
    return None


def run_write(
    op: str,
    caps: Any,
    install: Any,
    *,
    ipc: Callable[[], Any] | None,
    file: Callable[[], Any],
    prefer: str | None,
    skip_ipc_reason: str | None = None,
) -> planner.Outcome[Any]:
    """The planner walk for a mutation: ipc11 (when attempted and not guarded) then file. When
    the file writer itself answers E_UNSUPPORTED (an optional writer method this build lacks),
    THAT teaching error is raised - its `fix` names the alternative - rather than the planner's
    generic "no provider" one."""
    file_error: list[KiwrapError] = []

    def file_attempt() -> Any:
        try:
            return file()
        except KiwrapError as err:
            if err.code == "E_UNSUPPORTED":
                file_error.append(err)
            raise

    attempts: dict[str, Callable[[], Any]] = {planner.FILE: file_attempt}
    if ipc is not None and skip_ipc_reason is None:
        attempts[planner.IPC11] = ipc
    try:
        outcome = planner.run(op, caps, attempts, install=install, prefer=prefer)
    except KiwrapError as err:
        if err.code == "E_UNSUPPORTED" and file_error:
            original = file_error[0]
            original.data.setdefault("plan", err.data.get("plan"))
            raise original from None
        raise
    if skip_ipc_reason and outcome.provider_name == planner.FILE:
        outcome.warnings.append(skip_ipc_reason)
        if "ipc11_guarded" not in outcome.degraded and any(
            c.name == planner.IPC11 for c in outcome.plan.candidates
        ):
            outcome.degraded.append("ipc11_guarded")
    return outcome


def apply_outcome(
    m: Mutation, outcome: planner.Outcome[Any], install: Any, ctx: ToolContext | None = None
) -> None:
    m.receipt.provider_used = outcome.provider_used(install)
    for tag in outcome.degraded:
        if tag not in m.receipt.degraded:
            m.receipt.degraded.append(tag)
    m.receipt.warnings.extend(outcome.warnings)
    m.receipt.data["plan"] = outcome.plan.to_dict()
    if ctx is not None and outcome.provider_name == planner.FILE:
        resync_bound(ctx, m)


def resync_bound(ctx: ToolContext, m: Mutation, *, path: Path | None = None) -> None:
    """After a write that landed on DISK while a live GUI session (kw_live) is bound to the
    project - the file writer behind a guard (G13 / G16), a SES import, a refill save - make
    the GUI reload the file (`sessions.resync_bound_session`: `RevertDocument`) so the live view
    shows the write and the editor's next save cannot undo it (reviewer finding 2, 2026-09-20).
    A failed revert is a warning + `live_view_stale` tag, never an error: the disk is right."""
    if m.bound is None:
        return
    target = path or m.project.board
    try:
        result = backend(ctx, "ipc_resync_bound")(target)
    except KiwrapError as err:
        if err.code == "E_UNSUPPORTED" and "not available" in err.message and "sessions" in err.message:
            return  # no IPC backend in this build (a test double)
        if "live_view_stale" not in m.receipt.degraded:
            m.receipt.degraded.append("live_view_stale")
        m.receipt.warnings.append(
            f"the live KiCad session could not reload the file written on disk ({err.code}: {err.message}); "
            "the file on disk is authoritative - kw_live detach, or the next successful call, brings the view back"
        )
        return
    if isinstance(result, dict) and result.get("changed"):
        m.receipt.data["live_view"] = {"reloaded": result.get("reverted", [])}
        m.receipt.warnings.append(
            "this write went to the file on disk; the live KiCad session (kw_live) reloaded the file "
            f"({', '.join(result.get('reverted', [])) or 'document'}) so the view and its next save carry it"
        )


# ---- sheets --------------------------------------------------------------------------------------------


def sheet_file(project: Project, sheet: str | None) -> Path:
    """The `.kicad_sch` a `sheet` argument names: '' / None / the root's name -> the root; a
    file name (with or without the extension) -> that file in the project directory."""
    if not sheet or sheet in ("/", project.root_sch.name, project.name):
        return project.root_sch
    name = sheet if sheet.endswith(".kicad_sch") else sheet + ".kicad_sch"
    return project.dir / Path(name).name


def sheet_hashes(project: Project) -> dict[str, str]:
    return {p.name: sha256_file(p) for p in project.schematic_files()}


def ipc_sheet(ctx: ToolContext, project: Project, sheet: str | None) -> str:
    """The IPC twins' `sheet` argument for the tools' `sheet` argument. The twins address a sheet
    INSTANCE by its human-readable path (`/`, `/blinker/`; `SheetInfo.path`, the form
    `GetSchematicHierarchy` reports) inside a session of the ROOT schematic, while the tools
    (and the file writer) take the sheet's file name / sheet name / '' = root. A file placed
    several times resolves to its first instance (page order), like the file writer does."""
    if not sheet or sheet in ("/", project.root_sch.name, project.name):
        return "/"
    hier = hierarchy(ctx, project)
    wanted = sheet.strip()
    file_name = wanted if wanted.endswith(".kicad_sch") else wanted + ".kicad_sch"
    if wanted.startswith("/") and wanted.endswith("/"):
        return wanted
    candidates: list[str] = []
    for inst in getattr(hier, "instances", None) or []:
        names_path = str(getattr(inst, "names_path", "") or "")
        path = getattr(getattr(inst, "file", None), "path", None)
        name = str(getattr(inst, "name", "") or "")
        candidates.append(names_path)
        if path is not None and Path(path).name == file_name:
            return names_path
        if name and name == wanted:
            return names_path
        if names_path == f"/{wanted}/":
            return names_path
    raise KiwrapError(
        "E_BAD_ARGS",
        f"no sheet {sheet!r} in the project",
        why="sheets are resolved against the sheet tree on disk (file name, sheet name or '' for the root)",
        fix="use a sheet file name kw_sch_get lists, or '' for the root sheet",
        candidates=[c for c in candidates if c] or [project.root_sch.name],
    )


def check_if_match(project: Project, sheet: str | None, if_match: str | None) -> None:
    """`if_match` is the sha256 of the sheet file the agent last saw (`data.sheets` of every
    schematic receipt / `kw_project status` hashes); a mismatch is E_EXTERNAL_EDIT."""
    if not if_match:
        return
    path = sheet_file(project, sheet)
    if not path.exists():
        return
    current = sha256_file(path)
    if not current.startswith(if_match.strip().lower()):
        raise KiwrapError(
            "E_EXTERNAL_EDIT",
            f"{path.name} changed since if_match {if_match[:12]}... (now {current[:12]}...)",
            why="the edit was conditioned on the sheet content the agent last saw",
            fix="re-read the sheet (kw_sch_get) and retry with the current hash from data.sheets, or drop if_match",
            data={"sheet": path.name, "sha256": current},
        )


def hierarchy(ctx: ToolContext, project: Project) -> Any:
    """The file reader's sheet tree (`services.file_read_schematic`), or None when unavailable."""
    try:
        return ctx.services.file_read_schematic(project.root_sch)
    except KiwrapError as err:
        if err.code == "E_UNSUPPORTED":
            return None
        raise


def symbols_of(hier: Any) -> list[dict[str, Any]]:
    """`[{ref, uuid, lib_id, unit, unit_count, footprint, power, sheet, value}]` from a Hierarchy
    (per instance path) or a plain reader record with `symbols`."""
    out: list[dict[str, Any]] = []
    if hier is None:
        return out
    if callable(getattr(hier, "symbols", None)) and not isinstance(hier, dict):
        for inst, sym in hier.symbols():
            path = getattr(inst, "path", "/")
            r = rec(sym)
            ref = (
                sym.reference_for(path)
                if callable(getattr(sym, "reference_for", None))
                else pick(r, "reference", "ref")
            )
            unit = (
                sym.unit_for(path) if callable(getattr(sym, "unit_for", None)) else pick(r, "unit", default=1)
            )
            out.append(
                {
                    "ref": ref,
                    "uuid": pick(r, "uuid", "id"),
                    "lib_id": pick(r, "lib_id"),
                    "unit": int(unit or 1),
                    "unit_count": int(pick(r, "unit_count", default=1) or 1),
                    "footprint": pick(r, "footprint") or "",
                    "value": pick(r, "value") or "",
                    "power": bool(pick(r, "is_power", "power", default=False)),
                    "sheet": getattr(inst, "names_path", "/"),
                    "path": path,
                }
            )
        return out
    for sym in pick(rec(hier), "symbols", default=[]) or []:
        r = rec(sym)
        out.append(
            {
                "ref": pick(r, "ref", "reference"),
                "uuid": pick(r, "uuid", "id"),
                "lib_id": pick(r, "lib_id"),
                "unit": int(pick(r, "unit", default=1) or 1),
                "unit_count": int(pick(r, "unit_count", default=1) or 1),
                "footprint": pick(r, "footprint") or "",
                "value": pick(r, "value") or "",
                "power": bool(pick(r, "is_power", "power", default=False)),
                "sheet": pick(r, "sheet", default="/"),
                "path": pick(r, "path", default="/"),
            }
        )
    return out


def find_symbol(symbols: list[dict[str, Any]], ref: str) -> dict[str, Any]:
    hits = [s for s in symbols if s["ref"] == ref]
    if not hits:
        raise KiwrapError(
            "E_BAD_ARGS",
            f"no symbol with reference {ref!r} in the schematic",
            why="references are resolved against the sheet tree on disk",
            fix="use a reference kw_sch_get lists, or pass the item's uuid",
            candidates=sorted({s["ref"] for s in symbols if s["ref"]})[:50],
        )
    return hits[0]


def multi_unit_present(symbols: Iterable[dict[str, Any]]) -> bool:
    return any(s["unit_count"] > 1 for s in symbols)


# ---- validators -------------------------------------------------------------------------------------------


def erc_validator(
    ctx: ToolContext, m: Mutation, install: Any, caps: Any, config: Any, project: Project
) -> dict[str, Any] | None:
    """kicad-cli ERC on the root sheet after a schematic write: the `erc` validator delta plus
    the stage transition (`erc_clean` when zero errors, else settle at `sch_written`)."""
    if not caps.has("cli.has_erc"):
        m.receipt.degraded.append("erc_unavailable")
        m.receipt.warnings.append("ERC not available on this KiCad (cli.has_erc); stage stays sch_written")
        downgrade_below(m.state, "erc_clean")
        return None
    try:
        # the JSON goes to a throw-away folder, never `<root>.erc.json` beside the sheet (review item 2)
        with scratch_folder(ctx, "erc") as folder:
            report = ctx.services.run_erc(
                install,
                config,
                project.root_sch,
                severity="all",
                units="mm",
                out_json=folder / f"{project.root_sch.stem}.erc.json",
            )
    except KiwrapError as err:
        if err.code not in ("E_UNSUPPORTED", "E_KICAD_FAILED", "E_TIMEOUT"):
            raise
        m.receipt.degraded.append("erc_unavailable")
        m.receipt.warnings.append(f"ERC skipped: {err.code}: {err.message}")
        downgrade_below(m.state, "erc_clean")
        return None
    errors, warnings = int(report.errors), int(report.warnings)
    ok = bool(report.ok)
    m.validator_delta("erc", errors, {"warnings": warnings})
    if ok:
        record_stage(project, m.state, "erc_clean")
    else:
        downgrade_below(m.state, "erc_clean")
    violations = [violation_dict(v) for v in report.violations]
    violations.sort(key=lambda v: (0 if v["severity"] == "error" else 1, v["type"]))
    return {
        "ok": ok,
        "errors": errors,
        "warnings": warnings,
        "violations": violations[:MAX_LISTED],
        "violations_truncated": max(0, len(violations) - MAX_LISTED),
    }


def note_saved(m: Mutation, paths: Any) -> None:
    """Remember the files a writer's `save()` returned; `after_schematic_write` appends them to
    `changed.updated` after the item names."""
    for p in paths or []:
        name = getattr(p, "name", str(p))
        saved = m.receipt.data.setdefault("_saved", [])
        if name not in saved:
            saved.append(name)


def after_schematic_write(
    ctx: ToolContext, m: Mutation, install: Any, caps: Any, config: Any, project: Project, *, validate: bool
) -> None:
    """Stage bookkeeping + ERC after a schematic mutation: the board is no longer in sync
    (`synced` and later are dropped), the schematic is `sch_written`, and ERC decides
    `erc_clean`. `data.sheets` carries the sheet hashes (the `if_match` values for kw_sch_edit)."""
    for name in m.receipt.data.pop("_saved", []):
        if name not in m.receipt.changed["updated"]:
            m.receipt.changed["updated"].append(name)
    downgrade_below(m.state, "synced")
    record_stage(project, m.state, "sch_written")
    if validate:
        m.receipt.data["erc"] = erc_validator(ctx, m, install, caps, config, project)
    else:
        downgrade_below(m.state, "erc_clean")
        m.receipt.data["erc"] = None
        m.receipt.degraded.append("erc_skipped")
    m.receipt.data["sheets"] = sheet_hashes(project)
    erc = m.receipt.data.get("erc")
    if erc is None:
        m.receipt.next_suggested = "kw_sch_check"
    elif erc["ok"]:
        m.receipt.next_suggested = "kw_sch_stats"
    else:
        m.receipt.next_suggested = "kw_sch_check"


def has_copper_zones(ctx: ToolContext, project: Project) -> bool:
    """True when the board carries at least one filled copper zone (a zone with a net that is
    not a rule area) - the case where a DRC against the fill on disk is only meaningful after a
    refill: tracks added after the last fill are otherwise reported as `clearance` must_fix
    against the pour (reviewer finding, 2026-09-20). Unknown (no file reader) counts as False."""
    try:
        model = ctx.services.file_read_board(project.board)
    except KiwrapError as err:
        if err.code == "E_UNSUPPORTED":
            return False
        raise
    for zone in pick(rec(model), "zones", default=[]) or []:
        zr = rec(zone)
        if pick(zr, "is_rule_area", "keepout", default=False):
            continue
        net = pick(zr, "net")
        name = pick(rec(net), "name", default=net if isinstance(net, str) else "") if net is not None else ""
        if name:
            return True
    return False


ZONE_ITEM_MARK = "Zone"


def _involves_zone(violation: dict[str, Any]) -> bool:
    return any(str(i.get("description") or "").startswith(ZONE_ITEM_MARK) for i in violation.get("items", []))


def drc_validator(
    ctx: ToolContext,
    m: Mutation,
    install: Any,
    caps: Any,
    config: Any,
    project: Project,
    *,
    parity: bool = False,
    refill: bool = False,
    refill_if_zones: bool = False,
    touched_nets: Iterable[str] = (),
) -> dict[str, Any] | None:
    """kicad-cli DRC after a board write, classified like kw_pcb_drc, as the `drc` validator
    delta (+ `parity` when asked, + `drc_touched_nets` for the nets a routing op touched). A
    refill (`cli.drc_refill_flags`) is applied through `--refill-zones --save-board` and the
    board-derived stage hashes follow the save. `refill_if_zones` refills whenever the board
    has copper zones (their fill on disk is stale after a place / route); when that refill is
    impossible the receipt is tagged `zones_stale` and track-vs-zone clearance items are
    classified `review` with a note instead of must_fix."""
    if not caps.has("cli.has_drc"):
        m.receipt.degraded.append("drc_unavailable")
        m.receipt.warnings.append("DRC not available on this KiCad (cli.has_drc)")
        return None
    zones_present = has_copper_zones(ctx, project) if refill_if_zones else False
    do_refill = bool(refill) or zones_present
    stale_zones = False
    if do_refill and not caps.has("cli.drc_refill_flags"):
        do_refill = False
        if zones_present and not refill:
            stale_zones = True
            m.receipt.degraded.append("zones_stale")
            m.receipt.warnings.append(
                "the copper zones' fill on disk predates this change (no zone refill on this KiCad: "
                "cli.drc_refill_flags); track-vs-zone clearance items are listed as review, not must_fix"
            )
        else:
            m.receipt.degraded.append("refill_unavailable")
            m.receipt.warnings.append("zone refill needs cli.drc_refill_flags; zones were written unfilled")
    # a live GUI (kw_live) holds this board: kicad-cli must not rewrite the file under it (the
    # editor would not reload; its next save would undo the refill) - the refill stays in
    # kicad-cli's memory for the check only, like kw_pcb_drc does (reviewer finding 25)
    save_refill = do_refill and m.bound is None
    if do_refill and not save_refill:
        m.receipt.warnings.append(
            "the refilled zones were not written back to the board: a live KiCad session (kw_live) holds it; "
            "refill in the GUI or after kw_live detach (kw_pcb_drc refill_zones=true)"
        )
    if save_refill and not saves_in_format(install, caps, project, "pcb", m):
        # review item 1 (b): `--save-board` would write the board in this KiCad's newer format
        save_refill = False
        m.receipt.degraded.append("refill_not_saved_epoch")
        m.receipt.warnings.append(
            f"the zones were refilled for the check only: saving them with KiCad {getattr(install, 'version', '?')} "
            "would move the board to a newer file format (D21)"
        )
    try:
        # the JSON goes to a throw-away folder, never `<board>.drc.json` beside the board (review item 2)
        with scratch_folder(ctx, "drc") as folder:
            report = ctx.services.run_drc(
                install,
                config,
                project.board,
                schematic_parity=parity,
                refill_zones=do_refill,
                save_board=save_refill,
                severity="all",
                out_json=folder / f"{project.board.stem}.drc.json",
            )
    except KiwrapError as err:
        if err.code not in ("E_UNSUPPORTED", "E_KICAD_FAILED", "E_TIMEOUT"):
            raise
        m.receipt.degraded.append("drc_unavailable")
        m.receipt.warnings.append(f"DRC skipped: {err.code}: {err.message}")
        return None
    must_fix_t = _table(caps, "rules.drc_must_fix", DRC_MUST_FIX)
    review_t = _table(caps, "rules.drc_review", DRC_REVIEW)
    cosmetic_prefix = _text(caps, "rules.drc_cosmetic_prefix", DRC_COSMETIC_PREFIX)
    buckets: dict[str, list[dict[str, Any]]] = {"must_fix": [], "review": [], "cosmetic": []}
    for v in (*report.violations, *report.unconnected, *report.schematic_parity):
        if getattr(v, "excluded", False):
            continue
        d = violation_dict(v)
        d["class"] = classify_drc(v, must_fix=must_fix_t, review=review_t, cosmetic_prefix=cosmetic_prefix)
        if stale_zones and d["class"] == "must_fix" and v.type == "clearance" and _involves_zone(d):
            d["class"] = "review"
            d["note"] = (
                "against a stale zone fill; re-check with kw_pcb_drc(refill_zones=true) on a KiCad 10+"
            )
        buckets[d["class"]].append(d)
    must_fix = len(buckets["must_fix"])
    unconnected = len([v for v in report.unconnected if not getattr(v, "excluded", False)])
    parity_n = len([v for v in report.schematic_parity if not getattr(v, "excluded", False)])
    detail = {
        "review": len(buckets["review"]),
        "cosmetic": len(buckets["cosmetic"]),
        "unconnected": unconnected,
    }
    m.validator_delta("drc", must_fix, detail)
    if parity:
        m.validator_delta("parity", parity_n)
    nets = [n for n in touched_nets if n]
    by_net: dict[str, int] = {}
    if nets:
        for net in nets:
            tag = f"[{net}]"
            by_net[net] = sum(
                1
                for v in buckets["must_fix"]
                if any(tag in str(i.get("description") or "") for i in v["items"]) or tag in v["description"]
            )
        m.validator_delta("drc_touched_nets", sum(by_net.values()), {"by_net": by_net})
    m.history_extra["board_sha256"] = sha256_file(project.board)
    if save_refill:
        if project.board.name not in m.receipt.changed["updated"]:
            m.receipt.changed["updated"].append(project.board.name)
        # the refill wrote the board: re-hash so the fill is not taken for an external edit
        rehash_board_stages(project, m.state, m.state.stage)
    listed = [*buckets["must_fix"], *buckets["review"]]
    if nets:
        listed.sort(key=lambda v: 0 if any(f"[{n}]" in v["description"] for n in nets) else 1)
    # the nets of EVERY must_fix violation, computed before the listing is capped: what
    # kw_pcb_fix rips up (the first MAX_LISTED violations alone would miss nets on a big board)
    nets_with_must_fix: set[str] = set()
    for v in buckets["must_fix"]:
        nets_with_must_fix.update(violation_net_names(v))
    return {
        "ok": must_fix == 0,
        "must_fix": must_fix,
        "review": detail["review"],
        "cosmetic": detail["cosmetic"],
        "unconnected": unconnected,
        "parity": parity_n if parity else None,
        "touched_nets": by_net,
        "nets_with_must_fix": sorted(nets_with_must_fix),
        "refilled": do_refill,
        "refill_saved": save_refill,
        "violations": listed[:MAX_LISTED],
        "violations_truncated": max(0, len(listed) - MAX_LISTED),
    }


_NET_TAG = re.compile(r"\[([^\]]+)\]")


def violation_net_names(v: dict[str, Any]) -> set[str]:
    """The nets a DRC violation names: the `[NET]` tags KiCad puts into the item descriptions
    (`Track [GND] on F.Cu`, `Pad 3 [VCC] of U1`) and the description itself; `unconnected-(…)`
    placeholders and bracketed text with spaces (not a net) are ignored."""
    found: set[str] = set()
    for text in [
        str(v.get("description") or ""),
        *(str(i.get("description") or "") for i in v.get("items", [])),
    ]:
        for m in _NET_TAG.finditer(text):
            name = m.group(1).strip()
            if name and not name.startswith("unconnected-(") and " " not in name.strip("/"):
                found.add(name)
    return found


def placement_summary(
    ctx: ToolContext,
    install: Any,
    caps: Any,
    config: Any,
    project: Project,
    *,
    refs: Iterable[str] = (),
    unrouted: bool = True,
) -> dict[str, Any]:
    """The kw_pcb_check numbers a placement receipt reports before/after: courtyard overlaps,
    off-board footprints (copper outside = must_fix), unrouted connections (the file estimate -
    consistent and fast for a delta; kw_pcb_check gives KiCad's answer). `items` lists the
    overlap / off-board items that involve `refs` (the footprints just moved)."""
    files = _FileCache(ctx, project.board)
    wanted = set(refs)
    out: dict[str, Any] = {
        "courtyard_overlap": None,
        "off_board": None,
        "off_board_copper": None,
        "unrouted": None,
        "items": [],
        "providers": {},
        "degraded": [],
    }
    items: list[dict[str, Any]] = []
    try:
        res = planner.run(
            "overlaps",
            caps,
            {planner.FILE: lambda: ctx.services.courtyard_overlaps(files.get())},
            install=install,
        )
        overlaps = [norm_overlap(o) for o in (res.value or [])]
        out["courtyard_overlap"] = len(overlaps)
        out["providers"]["overlaps"] = res.provider_used(install)
        items.extend(overlaps)
    except KiwrapError as err:
        if err.code != "E_UNSUPPORTED":
            raise
        out["degraded"].append("overlaps_unavailable")
    try:
        raw = ctx.services.off_board(files.get())
        off = merge_off_board([norm_off_board(o) for o in (raw or [])])
        out["off_board"] = len(off)
        out["off_board_copper"] = sum(1 for i in off if i["severity"] == "must_fix")
        out["providers"]["off_board"] = planner.provider_label(planner.FILE, install)
        items.extend(off)
    except KiwrapError as err:
        if err.code != "E_UNSUPPORTED":
            raise
        out["degraded"].append("off_board_unavailable")
    if unrouted:
        try:
            conn = read_connectivity(
                ctx, install, caps, config, project.board, ipc_model=None, files=files, prefer=planner.FILE
            )
            out["unrouted"] = len(conn.value)
            out["providers"]["unrouted"] = conn.provider_used(install)
            out["degraded"].extend(d for d in conn.degraded if d not in out["degraded"])
        except KiwrapError as err:
            if err.code != "E_UNSUPPORTED":
                raise
            out["degraded"].append("connectivity_unavailable")
    if wanted:
        items = [i for i in items if set(i.get("refs") or ()) & wanted]
    out["items"] = items[:MAX_LISTED]
    return out


def placement_ok(summary: dict[str, Any]) -> bool | None:
    """kw_pcb_check's `ok`: no copper off-board and no courtyard overlap; None when unknown."""
    if summary["courtyard_overlap"] is None or summary["off_board_copper"] is None:
        return None
    return summary["courtyard_overlap"] == 0 and summary["off_board_copper"] == 0


def rehash_board_stages(project: Project, state: Any, keep: str) -> None:
    """After a board write: every stage above `keep` is dropped (its result is stale), the
    board-derived stages up to `keep` are re-hashed so the next call does not mistake kiwrap's
    own write for an external edit."""
    above = stage_index(keep) + 1
    if above < len(STAGES):
        downgrade_below(state, STAGES[above])
    for stage in STAGES[:above]:
        if stage in state.hashes and "pcb" in STAGE_INPUTS[stage]:
            record_stage(project, state, stage, advance=False)


def settle_placed(project: Project, state: Any, ok: bool | None) -> None:
    """`placed` = the placement checks pass on a synced board; unknown (no analysis) keeps the
    stage where it is; a failing check settles below `placed`."""
    if ok is True and stage_at_least(state.stage, "synced"):
        record_stage(project, state, "placed")
    elif ok is False:
        downgrade_below(state, "placed")


def note_delta(name: str, before: dict[str, Any], after: dict[str, Any], m: Mutation) -> None:
    b, a = before.get(name), after.get(name)
    if a is None:
        return
    delta = m.validator_delta(name, int(a))
    if b is not None:
        delta.before = int(b)


__all__ = [
    "AUTHOR_BACKENDS",
    "G13_KEY",
    "G16_KEY",
    "MAX_LISTED",
    "after_schematic_write",
    "apply_outcome",
    "backend",
    "board_writer",
    "call_with_accepted",
    "check_if_match",
    "drc_validator",
    "epoch_for",
    "erc_validator",
    "find_symbol",
    "g13_guard",
    "g16_guard",
    "hierarchy",
    "ipc_batch",
    "ipc_sheet",
    "ipc_write",
    "lib_resolver",
    "multi_unit_present",
    "note_delta",
    "note_saved",
    "placement_ok",
    "placement_summary",
    "rehash_board_stages",
    "resync_bound",
    "run_write",
    "schematic_writer",
    "settle_placed",
    "violation_net_names",
    "sheet_file",
    "sheet_hashes",
    "symbols_of",
]
