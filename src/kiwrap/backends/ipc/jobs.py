"""Exports over the live headless session: the `RunBoardJobExport*` / `RunSchematicJobExport*`
commands, gated on `ipc.headless_jobs`.

What is verified (10.99.0-4083-g6dd893f7d1 stock nightly AND the pristine bench build
`C:/kiwrap-builds/6dd893f7d1-2`, scratchpad g6/probe_jobs.py + probe_t5_jobs.py, 2026-09-19):
- The 22 job handlers are registered unconditionally in the `API_HANDLER_PCB` / `API_HANDLER_SCH`
  constructors (pcbnew/api/api_handler_pcb.cpp:170-196, eeschema/api/api_handler_sch.cpp:159-172)
  and run through `KIWAY::ProcessJob` on the context's KIWAY, which the headless contexts carry
  (`HEADLESS_PCB_CONTEXT::GetKiway`, `IFACE::handleOpenPcb` passes `m_kiway`). Upstream's own
  `qa/tests/api/test_api_jobs.cpp` (suite ApiJobs, 6 cases) drives them through a real
  `kicad-cli api-server`. There is no headless-vs-GUI difference to patch (docs/UPSTREAM.md G6 was
  a T5 probe artifact, see `handlers._minimal_instance`).
- `RunBoardJobExportStats` (JSON) answers JS_SUCCESS in 0.03 s with the file written;
  `RunSchematicJobExportNetlist` (SNF_KICAD_XML) in 0.14 s; `RunBoardJobExportSvg` with no layers
  answers AS_OK + JS_ERROR "At least one layer must be specified" (the handler ran, the job
  refused: kipy's default `PlotSettings` has no layers, so this module always sends some).
- A request whose `job_settings.document` is empty is answered AS_UNHANDLED ("no handler
  available for request of type ..."): `validateDocument` passes on an unknown document type so
  another editor's handler can answer.
- A relative `output_path` resolves against the api-server's working directory (the sandbox
  temp dir, `HeadlessSession.start`), not the project; an empty one makes the job pick its
  default file name there. This module therefore always sends absolute paths.
- T5 with the nested document filled (`handlers._minimal_instance`, every enum at 0, no output
  path): all 15 board jobs `handled` on the board, all 7 schematic jobs `handled` /
  `bad_request` (netlist: "requires a valid format") on the schematic, each within 0.9 s, on the
  stock nightly and on BOTH bench builds (the Debug one included: the job enum converters do not
  assert on UNKNOWN the way `SetPageSettings`' PageSize one does). The builders below still set
  every enum so a job never silently runs with KiCad's fallback format.

Contract: `run_job(session, request)` sends any kipy `Run*Job*` message with the document filled
from the session and returns a `JobOutcome`; `export_board_stats`, `export_board_svg` and
`export_schematic_netlist` build the requests kiwrap's tools need. kicad-cli stays the default
export provider (`kiwrap.backends.cli.export` / `structured`: every version, no process kept
alive, DRC gate on the saved file); this module serves a tool that already holds a live session
and wants the export of the in-memory document without a save and a second process.
`probe_headless_jobs` is the presence probe behind the capability key (docs/UPSTREAM.md §3.3):
it sends one cheap job and reports whether a handler answered at all.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kiwrap.backends.ipc.sessions import HeadlessSession, IpcError, require_capability
from kiwrap.formats.reports.stats import BoardStats, parse_stats_json
from kiwrap.platform import current

__all__ = [
    "CAPABILITY",
    "DEFAULT_SVG_LAYERS",
    "JobFailed",
    "JobOutcome",
    "export_board_stats",
    "export_board_svg",
    "export_schematic_netlist",
    "layer_value",
    "probe_headless_jobs",
    "run_job",
]

CAPABILITY = "ipc.headless_jobs"
#: Same default as the kicad-cli plot adapter (`backends.cli.export.DEFAULT_PLOT_LAYERS`).
DEFAULT_SVG_LAYERS: tuple[str, ...] = ("F.Cu", "B.Cu", "Edge.Cuts", "F.SilkS")
#: `kicad-cli sch export netlist --format` names -> `SchematicNetlistFormat`.
NETLIST_FORMATS: dict[str, str] = {
    "kicadsexpr": "SNF_KICAD_SEXPR",
    "kicadxml": "SNF_KICAD_XML",
    "orcadpcb2": "SNF_ORCAD_PCB2",
    "cadstar": "SNF_CADSTAR",
    "spice": "SNF_SPICE",
    "spicemodel": "SNF_SPICE_MODEL",
    "pads": "SNF_PADS",
    "allegro": "SNF_ALLEGRO",
}


class JobFailed(IpcError):
    """The handler answered, but the job reported JS_ERROR (the message is KiCad's own text)."""

    code = "E_IPC_JOB"

    def __init__(self, outcome: JobOutcome):
        self.outcome = outcome
        super().__init__(
            f"{outcome.command} failed: {outcome.message.strip() or outcome.status}",
            why="the headless api-server ran the export job and KiCad reported an error",
            fix="fix the request (layers, format, output path) or export through kicad-cli",
            details=outcome.message,
        )


@dataclass(frozen=True)
class JobOutcome:
    """`RunJobResponse`, unpacked: `status` is the `JobStatus` name (JS_SUCCESS / JS_WARNING /
    JS_ERROR), `output_paths` are what the job says it wrote (absolute when the request was)."""

    command: str
    status: str
    output_paths: tuple[Path, ...]
    message: str
    elapsed_ms: int

    @property
    def ok(self) -> bool:
        return self.status in ("JS_SUCCESS", "JS_WARNING")

    def to_json(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "status": self.status,
            "ok": self.ok,
            "output_paths": [str(p) for p in self.output_paths],
            "message": self.message,
            "elapsed_ms": self.elapsed_ms,
        }


def layer_value(name: str) -> int:
    """KiCad canonical layer name -> `BoardLayer` value (`F.Cu` -> `BL_F_Cu`, `Edge.Cuts` ->
    `BL_Edge_Cuts`, `User.1` -> `BL_User_1`); the inverse of `readers.layer_name`."""
    from kipy.proto.board.board_types_pb2 import BoardLayer

    try:
        return int(BoardLayer.Value("BL_" + name.replace(".", "_", 1)))
    except ValueError as e:
        raise IpcError(
            f"unknown board layer {name!r}",
            why="layers are given by their KiCad canonical name",
            fix="use names like F.Cu, B.Cu, In1.Cu, Edge.Cuts, F.SilkS, User.1",
        ) from e


def _document_kind(request: Any) -> str:
    package = request.DESCRIPTOR.full_name
    if package.startswith("kiapi.board."):
        return "pcb"
    if package.startswith("kiapi.schematic."):
        return "schematic"
    raise IpcError(f"{request.DESCRIPTOR.name} is not a board or schematic job request")


def _document_for(session: HeadlessSession, kind: str) -> Any:
    return session.board().document if kind == "pcb" else session.schematic().document


def run_job(
    session: HeadlessSession,
    request: Any,
    *,
    output_path: str | Path,
    check: bool = True,
) -> JobOutcome:
    """Send a kipy `RunBoardJob*` / `RunSchematicJob*` request through the session.

    `job_settings.document` is filled from the session (the open board for `kiapi.board.*`
    requests, the open schematic for `kiapi.schematic.*`) and `job_settings.output_path` from
    `output_path`, made absolute (a file or a directory, as the job expects; relative paths would
    resolve against the server's cwd, never the caller's). An AS_UNHANDLED answer means the
    server has no job handler for this document (`Unsupported`-like, fallback kicad-cli); any
    other API error is re-raised as `IpcError`; `check=True` turns JS_ERROR into `JobFailed`.
    """
    from kipy.errors import ApiError
    from kipy.proto.common import ApiStatusCode
    from kipy.proto.common.types import jobs_pb2

    require_capability(session.capabilities, CAPABILITY, fallback="cli")
    name = request.DESCRIPTOR.name
    request.job_settings.document.CopyFrom(_document_for(session, _document_kind(request)))
    request.job_settings.output_path = str(Path(output_path).resolve())
    t0 = time.monotonic()
    try:
        response = session.client().send(request, jobs_pb2.RunJobResponse)
    except ApiError as e:
        text = e.raw_message or str(e)
        if e.code == ApiStatusCode.AS_UNHANDLED:
            raise IpcError(
                f"{name}: no job handler on this api-server ({text})",
                why="the headless server answered AS_UNHANDLED: the job commands are not registered "
                "for this document (ipc.headless_jobs should be false for this install)",
                fix="export through kicad-cli (kw_fab_export) and report the KiCad version",
                details=text,
            ) from e
        status = ApiStatusCode.Name(e.code) if e.code in ApiStatusCode.values() else str(e.code)
        raise IpcError(
            f"{name} rejected: {text}",
            why=f"the api-server answered {status}",
            fix="check the request options (layers, format, units, output path)",
            details=text,
        ) from e
    outcome = JobOutcome(
        command=name,
        status=jobs_pb2.JobStatus.Name(response.status),
        output_paths=tuple(Path(p) for p in response.output_path),
        message=response.message,
        elapsed_ms=int((time.monotonic() - t0) * 1000),
    )
    if check and not outcome.ok:
        raise JobFailed(outcome)
    return outcome


def probe_headless_jobs(session: HeadlessSession, kind: str | None = None) -> bool:
    """Presence probe for `ipc.headless_jobs`: True when a job handler answers for the session's
    document (any status but AS_UNHANDLED), False when the server has none. Costs one
    `RunBoardJobExportStats` (report, ~30 ms) or `RunSchematicJobExportNetlist` (~150 ms) into
    the sandbox temp dir; the output is removed. `kind` defaults to the session file's type."""
    from kipy.errors import ApiError
    from kipy.proto.common import ApiStatusCode
    from kipy.proto.common.types import jobs_pb2

    kind = kind or (
        "schematic" if session.file is not None and session.file.suffix == ".kicad_sch" else "pcb"
    )
    scratch = session.config.temp_dir(current()) / "probe-jobs"
    scratch.mkdir(parents=True, exist_ok=True)
    target = scratch / (f"probe-{session.pid}.rpt" if kind == "pcb" else f"probe-{session.pid}.xml")
    if kind == "pcb":
        from kipy.proto.board import board_jobs_pb2

        request: Any = board_jobs_pb2.RunBoardJobExportStats()
        request.format = board_jobs_pb2.SOF_REPORT
        request.units = _units_value("mm")
    else:
        from kipy.proto.schematic import schematic_jobs_pb2

        request = schematic_jobs_pb2.RunSchematicJobExportNetlist()
        request.format = schematic_jobs_pb2.SNF_KICAD_XML
    try:
        request.job_settings.document.CopyFrom(_document_for(session, kind))
        request.job_settings.output_path = str(target)
        session.client().send(request, jobs_pb2.RunJobResponse)
        return True
    except ApiError as e:
        return e.code != ApiStatusCode.AS_UNHANDLED
    finally:
        with contextlib.suppress(OSError):
            target.unlink()


def _units_value(units: str) -> Any:
    """The `Units` enum value for `units`. Typed `Any`: the job requests' `units` fields are typed
    `Units.ValueType` by the kicad-python stubs the CI wheel resolves (scripts/ci/
    build_kicad_python_wheel.py rewrites their imports) and plain `int` by unresolved stubs, and
    one annotation cannot satisfy both without a `type: ignore` that `warn_unused_ignores` rejects
    on the other."""
    from kipy.proto.common.types.enums_pb2 import Units

    try:
        return int(Units.Value("U_" + {"mm": "MM", "in": "INCH", "inch": "INCH", "mils": "MILS"}[units]))
    except KeyError as e:
        raise IpcError(f"unknown units {units!r}", fix="use 'mm' or 'in'") from e


def export_board_stats(
    session: HeadlessSession,
    output_path: str | Path,
    *,
    exclude_footprints_without_pads: bool = False,
    subtract_holes_from_board: bool = False,
    subtract_holes_from_copper: bool = False,
) -> BoardStats:
    """`RunBoardJobExportStats` (JSON, mm) on the open board -> `BoardStats`, the same model
    `backends.cli.structured.export_stats` returns for `pcb export stats --format json`."""
    from kipy.proto.board import board_jobs_pb2

    request = board_jobs_pb2.RunBoardJobExportStats()
    request.format = board_jobs_pb2.SOF_JSON
    request.units = _units_value("mm")
    request.exclude_footprints_without_pads = exclude_footprints_without_pads
    request.subtract_holes_from_board_area = subtract_holes_from_board
    request.subtract_holes_from_copper_areas = subtract_holes_from_copper
    outcome = run_job(session, request, output_path=output_path)
    path = next((p for p in outcome.output_paths if p.suffix == ".json"), Path(output_path).resolve())
    return parse_stats_json(
        path.read_text(encoding="utf-8"),
        source=str(session.file or ""),
        kicad_version=session.install.version,
    )


def export_board_svg(
    session: HeadlessSession,
    output_path: str | Path,
    *,
    layers: Sequence[str] = DEFAULT_SVG_LAYERS,
    black_and_white: bool = False,
    plot_drawing_sheet: bool = False,
    each_layer_own_file: bool = False,
    precision: int = 4,
) -> JobOutcome:
    """`RunBoardJobExportSvg`: `layers` by canonical name (never empty: the job refuses),
    `output_path` a file (one page with every layer, the default) or a directory when
    `each_layer_own_file`. Returns the outcome with the SVG paths the job wrote."""
    from kipy.proto.board import board_jobs_pb2

    if not layers:
        raise IpcError("export_board_svg needs at least one layer", fix="pass layers=['F.Cu', ...]")
    request = board_jobs_pb2.RunBoardJobExportSvg()
    request.plot_settings.layers.extend(layer_value(n) for n in layers)
    request.plot_settings.black_and_white = black_and_white
    request.plot_settings.plot_drawing_sheet = plot_drawing_sheet
    request.plot_settings.drill_marks = board_jobs_pb2.PDM_FULL
    request.page_mode = (
        board_jobs_pb2.BJPM_EACH_LAYER_OWN_FILE
        if each_layer_own_file
        else board_jobs_pb2.BJPM_ALL_LAYERS_ONE_PAGE
    )
    request.precision = precision
    return run_job(session, request, output_path=output_path)


def export_schematic_netlist(
    session: HeadlessSession,
    output_path: str | Path,
    *,
    format: str = "kicadxml",
    variant: str = "",
) -> JobOutcome:
    """`RunSchematicJobExportNetlist` on the open schematic; `format` uses the kicad-cli names
    (`kicadxml`, `kicadsexpr`, `spice`, ...). The XML netlist is what `formats.reports.netlist`
    parses (the same file `kicad-cli sch export netlist --format kicadxml` writes)."""
    from kipy.proto.schematic import schematic_jobs_pb2

    try:
        value = schematic_jobs_pb2.SchematicNetlistFormat.Value(NETLIST_FORMATS[format])
    except KeyError as e:
        raise IpcError(
            f"unknown netlist format {format!r}", fix=f"use one of {', '.join(NETLIST_FORMATS)}"
        ) from e
    request = schematic_jobs_pb2.RunSchematicJobExportNetlist()
    request.format = value
    request.variant_name = variant
    return run_job(session, request, output_path=output_path)
