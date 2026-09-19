"""The data-first tools (kw_pcb_get, kw_sch_get, kw_pcb_check, kw_sch_stats, kw_sch_diff) against
faked lower layers: happy paths, filters, chunking, detail levels, Unsupported paths, the
ipc11 -> file provider fallback, the MCP stdio catalog with typed outputSchemas, and an
end-to-end run on the nightly's ecc83 demo when the real ipc/file backends are importable.

The fakes implement the reader contract documented in `kiwrap.core.tools._data` (dataclasses
and dicts, mm units) - the same shapes `kiwrap.backends.ipc.readers` and `kiwrap.formats.board/
schematic/analysis` are being built to return.
"""

from __future__ import annotations

import importlib
import json
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import Client, StdioServerParameters

from kiwrap.core.services import Services
from kiwrap.core.state import KiwrapError
from kiwrap.core.state.project import Project, ProjectState
from kiwrap.core.tools import DATA_TOOLS, TOOL_NAMES
from kiwrap.core.tools import help as help_tool
from kiwrap.core.tools import sch_diff as sch_diff_tool
from kiwrap.core.tools._data import Filters, chunk, matches, mm_box, parse_cursor, rec, ref_key
from kiwrap.formats.reports import Component, Net, Netlist, NetNode
from tests.test_core_fakes import CAPS_10, FakeBackend, FakeInstall, drc_report, violation
from tests.test_core_tools import Harness

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH (snapshots)")

#: CAPS_10 plus the subcommands every supported kicad-cli has (the fake's list is abridged)
CAPS_10D = {**CAPS_10, "cli.subcommands": [*CAPS_10["cli.subcommands"], "sch export netlist"]}
CAPS_11 = {
    **CAPS_10,
    "ipc.headless": True,
    "ipc.schematic": True,
    "cli.has_stats": True,
    "cli.has_stats_json": True,
    "cli.subcommands": [
        "sch erc",
        "pcb drc",
        "sch diff",
        "pcb export stats",
        "pcb export stackup",
        "sch export netlist",
    ],
}

# ---- fake reader models (the contract in kiwrap.core.tools._data) -------------------------------------------


@dataclass(frozen=True)
class FPad:
    number: str
    net: str | None
    x_mm: float
    y_mm: float
    size_mm: tuple[float, float] = (1.0, 1.0)
    shape: str = "roundrect"
    layers: tuple[str, ...] = ("F.Cu", "F.Mask", "F.Paste")
    drill_mm: float | None = None
    uuid: str = ""


@dataclass(frozen=True)
class FFootprint:
    ref: str
    value: str
    lib_id: str
    layer: str
    x_mm: float
    y_mm: float
    rotation_deg: float
    bbox_mm: tuple[float, float, float, float]
    courtyard_bbox_mm: tuple[float, float, float, float]
    pads: tuple[FPad, ...]
    uuid: str
    attr: str = "smd"
    dnp: bool = False
    properties: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class FTrack:
    net: str
    layer: str
    start_mm: tuple[float, float]
    end_mm: tuple[float, float]
    width_mm: float
    uuid: str


@dataclass
class FBoard:
    footprints: list[Any]
    tracks: list[Any]
    vias: list[Any]
    zones: list[Any]
    nets: list[Any]
    outline: Any
    rules: Any
    unrouted: list[Any] | None = None


def pad(n: str, net: str | None, x: float, y: float) -> FPad:
    return FPad(number=n, net=net, x_mm=x, y_mm=y, uuid=f"pad-{n}-{x}-{y}")


def fake_board(*, unrouted: list[Any] | None = None) -> FBoard:
    r10 = FFootprint(
        "R10",
        "10k",
        "Resistor_SMD:R_0603",
        "F.Cu",
        30.0,
        10.0,
        0.0,
        (29, 9, 31, 11),
        (28.8, 8.8, 31.2, 11.2),
        (pad("1", "VCC", 29.2, 10.0), pad("2", "SIG", 30.8, 10.0)),
        "fp-r10",
    )
    r1 = FFootprint(
        "R1",
        "1k",
        "Resistor_SMD:R_0603",
        "F.Cu",
        10.0,
        10.0,
        90.0,
        (9, 9, 11, 11),
        (8.8, 8.8, 11.2, 11.2),
        (pad("1", "VCC", 10.0, 9.2), pad("2", "SIG", 10.0, 10.8)),
        "fp-r1",
    )
    r2 = FFootprint(
        "R2",
        "2k2",
        "Resistor_SMD:R_0603",
        "B.Cu",
        10.5,
        10.5,
        0.0,
        (9.5, 9.5, 11.5, 11.5),
        (9.3, 9.3, 11.7, 11.7),
        (pad("1", "SIG", 9.7, 10.5), pad("2", "GND", 11.3, 10.5)),
        "fp-r2",
    )
    c1 = FFootprint(
        "C1",
        "100n",
        "Capacitor_SMD:C_0603",
        "F.Cu",
        60.0,
        5.0,
        0.0,
        (59, 4, 61, 6),
        (58.8, 3.8, 61.2, 6.2),
        (pad("1", "VCC", 59.2, 5.0), pad("2", "GND", 60.8, 5.0)),
        "fp-c1",
    )
    return FBoard(
        footprints=[r10, r1, r2, c1],
        tracks=[
            FTrack("VCC", "F.Cu", (10.0, 9.2), (29.2, 10.0), 0.25, "t-1"),
            {
                "net": "GND",
                "layer": "B.Cu",
                "start": (11.3, 10.5),
                "end": (60.8, 5.0),
                "width": 0.5,
                "uuid": "t-2",
            },
        ],
        vias=[
            {
                "net": "GND",
                "position": {"x_mm": 40.0, "y_mm": 8.0},
                "drill_mm": 0.3,
                "size_mm": 0.6,
                "layers": ["F.Cu", "B.Cu"],
                "uuid": "v-1",
            }
        ],
        zones=[
            {
                "net": "GND",
                "layers": ["B.Cu"],
                "name": "gnd-pour",
                "bbox_mm": (0, 0, 50, 30),
                "filled": True,
                "uuid": "z-1",
            }
        ],
        nets=[
            {"code": 1, "name": "VCC", "pad_count": 3},
            {"code": 2, "name": "SIG", "pad_count": 3},
            {"code": 3, "name": "GND", "pad_count": 3},
            {"code": 0, "name": "", "pad_count": 0},
        ],
        outline={"bbox_mm": (0, 0, 50, 30), "closed": True, "segments": 4},
        rules={
            "min_clearance_mm": 0.2,
            "min_track_width_mm": 0.2,
            "copper_layers": ["F.Cu", "B.Cu"],
            "netclasses": {"Default": {"clearance_mm": 0.2, "track_width_mm": 0.25}},
        },
        unrouted=unrouted,
    )


@dataclass(frozen=True)
class FPin:
    number: str
    name: str
    type: str
    x_mm: float
    y_mm: float
    net: str | None
    connected: bool


@dataclass(frozen=True)
class FSymbol:
    ref: str
    value: str
    lib_id: str
    unit: int
    sheet: str
    x_mm: float
    y_mm: float
    rotation_deg: float
    pins: tuple[FPin, ...]
    uuid: str
    footprint: str | None = None
    power: bool = False
    fields: dict[str, str] = field(default_factory=dict)


@dataclass
class FSchematic:
    symbols: list[Any]
    wires: list[Any]
    labels: list[Any]
    junctions: list[Any]
    no_connects: list[Any]
    sheets: list[Any]
    hierarchy: Any = None
    netlist: Any = None


def fake_schematic(*, with_netlist: bool = False) -> FSchematic:
    r1 = FSymbol(
        "R1",
        "1k",
        "Device:R",
        1,
        "/",
        50.8,
        63.5,
        0.0,
        (
            FPin("1", "~", "passive", 50.8, 59.69, "VCC", True),
            FPin("2", "~", "passive", 50.8, 67.31, "SIG", True),
        ),
        "sym-r1",
        footprint="Resistor_SMD:R_0603",
    )
    r2 = FSymbol(
        "R2",
        "2k2",
        "Device:R",
        1,
        "/sub/",
        20.0,
        20.0,
        90.0,
        (
            FPin("1", "~", "passive", 16.19, 20.0, "SIG", True),
            FPin("2", "~", "passive", 23.81, 20.0, None, False),
        ),
        "sym-r2",
    )
    pwr = FSymbol(
        "#PWR01",
        "VCC",
        "power:VCC",
        1,
        "/",
        50.8,
        55.88,
        0.0,
        (FPin("1", "VCC", "power_in", 50.8, 59.69, "VCC", True),),
        "sym-pwr",
        power=True,
    )
    nl = None
    if with_netlist:
        nl = {
            "nets": [
                {
                    "name": "VCC",
                    "code": 1,
                    "nodes": [{"ref": "R1", "pin": "1"}, {"ref": "#PWR01", "pin": "1"}],
                },
                {"name": "SIG", "code": 2, "nodes": [{"ref": "R2", "pin": "1"}, {"ref": "R1", "pin": "2"}]},
            ],
            "components": [1, 2, 3],
        }
    return FSchematic(
        symbols=[r2, r1, pwr],
        wires=[
            {"sheet": "/", "start": (50.8, 67.31), "end": (60.96, 67.31), "net": "SIG", "uuid": "w-1"},
            {"sheet": "/sub/", "start": (10.0, 20.0), "end": (16.19, 20.0), "net": "SIG", "uuid": "w-2"},
        ],
        labels=[
            {
                "sheet": "/",
                "kind": "global",
                "text": "SIG",
                "x_mm": 60.96,
                "y_mm": 67.31,
                "net": "SIG",
                "uuid": "l-1",
            }
        ],
        junctions=[{"sheet": "/", "x_mm": 50.8, "y_mm": 59.69, "uuid": "j-1"}],
        no_connects=[{"sheet": "/sub/", "x_mm": 23.81, "y_mm": 20.0, "uuid": "nc-1"}],
        sheets=[
            {
                "name": "sub",
                "file": "sub.kicad_sch",
                "sheet": "/",
                "path": "/sub/",
                "x_mm": 100.0,
                "y_mm": 50.0,
                "size_mm": (20, 15),
                "pins": [{"name": "SIG", "x_mm": 100.0, "y_mm": 55.0}],
                "uuid": "sh-1",
            }
        ],
        hierarchy={
            "path": "/",
            "name": "root",
            "file": "demo.kicad_sch",
            "children": [{"path": "/sub/", "name": "sub", "file": "sub.kicad_sch", "children": []}],
        },
        netlist=nl,
    )


def cli_netlist() -> Netlist:
    return Netlist(
        source="demo.kicad_sch",
        kicad_version="10.0.0",
        components=(
            Component("R1", "1k", "Resistor_SMD:R_0603", "Device:R", "/"),
            Component("R2", "2k2", None, "Device:R", "/sub/"),
        ),
        nets=(Net(1, "VCC", (NetNode("R1", "1"),)), Net(2, "SIG", (NetNode("R1", "2"), NetNode("R2", "1")))),
    )


# the JSON shape `kicad-cli sch diff --format json` produced on the installed nightly (probe_cli.py, 2026-09-19)
DIFF_JSON = {
    "docType": "kicad_sch",
    "path": "b.kicad_sch",
    "changes": [
        {
            "bbox": [1535890, 784251, 75740, 132283],
            "children": [],
            "id": "/28f865a0-4433-4a53-bbd7-b62f276848e4/00000000-0000-0000-0000-00004549f38a",
            "kind": "modified",
            "refdes": "R1",
            "typeName": "SCH_SYMBOL",
            "properties": [
                {
                    "name": "Position X",
                    "before": {"type": "int", "v": 1574800},
                    "after": {"type": "int", "v": 1600200},
                }
            ],
        },
        {
            "bbox": [1435100, 1320800, 25400, 25400],
            "children": [],
            "id": "/28f865a0-4433-4a53-bbd7-b62f276848e4/j-1",
            "kind": "removed",
            "typeName": "SCH_JUNCTION",
            "properties": [
                {"name": "Position X", "before": {"type": "int", "v": 1435100}, "after": {"type": "none"}}
            ],
        },
        {
            "bbox": [0, 0, 100, 100],
            "children": [],
            "id": "/28f865a0-4433-4a53-bbd7-b62f276848e4/l-9",
            "kind": "added",
            "typeName": "SCH_LINE",
            "properties": [
                {
                    "name": "Layer",
                    "before": {"type": "none"},
                    "after": {"type": "enum", "v": 1, "label": "Wire"},
                }
            ],
        },
    ],
}


class FakeSession:
    """`HeadlessSession(install, file, config_root=...)` double: records open/close."""

    log: list[str] = []

    def __init__(self, install: Any, file: Path, *, config_root: Any = None):
        self.install, self.file, self.config_root = install, Path(file), config_root
        self.closed = False
        FakeSession.log.append(f"open:{self.file.name}")

    def __enter__(self) -> FakeSession:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        self.closed = True
        FakeSession.log.append(f"close:{self.file.name}")

    net_names: list[str] = []  # what `board().get_nets()` reports (the ipc11 net-count threshold)

    def board(self) -> Any:
        names = list(FakeSession.net_names)

        class _Board:
            def get_nets(self):
                return [type("N", (), {"name": n})() for n in names]

        return _Board()


@dataclass(frozen=True)
class FAnchor:
    """`kiwrap.backends.ipc.queries.ConnectedItem` shape (a pad or via on the net)."""

    id: str
    kind: str
    net: str
    position: tuple[float, float]
    ref: str = ""
    number: str = ""


@dataclass(frozen=True)
class FAirwire:
    """`kiwrap.backends.ipc.queries.Airwire` shape: end points as mm points, items as KIIDs."""

    net: str
    start: tuple[float, float]
    end: tuple[float, float]
    from_item: str
    to_item: str
    length: float


@dataclass(frozen=True)
class FNetConnectivity:
    net: str
    anchors: tuple[FAnchor, ...]
    clusters: tuple[tuple[str, ...], ...]
    airwires: tuple[FAirwire, ...]


@dataclass(frozen=True)
class FRatsnest:
    """`kiwrap.backends.ipc.queries.Ratsnest` shape (per-net clusters + spanning airwires)."""

    nets: tuple[FNetConnectivity, ...]
    requests: int = 3


def fake_ipc_ratsnest() -> FRatsnest:
    """GND: two clusters (C1.2 alone, R2.2 + a via) -> one airwire C1.2 -> R2.2; SIG routed."""
    a = FAnchor("p-c1-2", "pad", "GND", (60.8, 5.0), "C1", "2")
    b = FAnchor("p-r2-2", "pad", "GND", (11.3, 10.5), "R2", "2")
    v = FAnchor("v-1", "via", "GND", (12.0, 12.0))
    return FRatsnest(
        nets=(
            FNetConnectivity(
                "GND",
                (a, b, v),
                (("p-c1-2",), ("p-r2-2", "v-1", "t-1")),
                (FAirwire("GND", a.position, b.position, a.id, b.id, 49.8),),
            ),
            FNetConnectivity("SIG", (), (("p-r1-2", "p-r2-1", "t-2"),), ()),
        )
    )


@dataclass
class DataBackend(FakeBackend):
    caps: dict[str, dict[str, Any]] = field(default_factory=lambda: {"10.0.0": CAPS_10D})
    board: Any = field(default_factory=fake_board)
    schematic: Any = field(default_factory=fake_schematic)
    ipc_board: Any = None  # None -> same as `board`
    ipc_schematic: Any = None
    ipc_raises: Exception | None = None
    file_raises: Exception | None = None
    overlaps: list[Any] = field(
        default_factory=lambda: [
            {"a": "R1", "b": "R2", "bbox_mm": (9.3, 9.3, 11.2, 11.2), "area_mm2": 3.61, "layer": "F.CrtYd"}
        ]
    )
    off: list[Any] = field(
        default_factory=lambda: [
            {"ref": "C1", "kind": "pad", "detail": "1", "bbox_mm": (58.8, 3.8, 61.2, 6.2), "outside_by_mm": 11.2},
            {"ref": "C1", "kind": "courtyard", "bbox_mm": (58.5, 3.5, 61.5, 6.5), "outside_by_mm": 11.5},
            {"ref": "J1", "kind": "courtyard", "bbox_mm": (-1.0, 20.0, 5.0, 30.0), "outside_by_mm": 1.0},
        ]
    )
    ratsnest: list[Any] = field(
        default_factory=lambda: [
            {
                "net": "SIG",
                "a": {"ref": "R1", "pad": "2", "x_mm": 10.0, "y_mm": 10.8},
                "b": {"ref": "R2", "pad": "1", "x_mm": 9.7, "y_mm": 10.5},
            },
            {
                "net": "SIG",
                "a": {"ref": "R10", "pad": "2", "x_mm": 30.8, "y_mm": 10.0},
                "b": {"ref": "R1", "pad": "2", "x_mm": 10.0, "y_mm": 10.8},
            },
        ]
    )
    connectivity: Any = field(
        default_factory=lambda: {
            "dangling_ends": [{"sheet": "/sub/", "x_mm": 10.0, "y_mm": 20.0, "uuid": "w-2"}],
            "unconnected_pins": [
                {"ref": "R2", "pin": "2", "sheet": "/sub/", "x_mm": 23.81, "y_mm": 20.0, "type": "passive"}
            ],
            "label_only_nets": ["SIG_ORPHAN"],
        }
    )
    analysis_missing: bool = False
    #: the IPC connectivity query's answer; None -> the backend lacks it (E_UNSUPPORTED like `_missing`)
    ratsnest_ipc: Any = field(default_factory=fake_ipc_ratsnest)
    diff_json: dict[str, Any] | Exception = field(default_factory=lambda: json.loads(json.dumps(DIFF_JSON)))

    def _unsupported(self, what: str) -> KiwrapError:
        return KiwrapError("E_UNSUPPORTED", f"{what} is not available in this build", data={"module": what})

    # ipc
    def ipc_session(self, install: Any, file: Path, *, config_root: Any = None) -> FakeSession:
        self._log("ipc_session", file=Path(file).name)
        if self.ipc_raises:
            raise self.ipc_raises
        return FakeSession(install, file, config_root=config_root)

    def ipc_read_board(self, session: FakeSession) -> Any:
        self._log("ipc_read_board", file=session.file.name)
        return self.ipc_board if self.ipc_board is not None else self.board

    def ipc_read_schematic(self, session: FakeSession) -> Any:
        self._log("ipc_read_schematic", file=session.file.name)
        return self.ipc_schematic if self.ipc_schematic is not None else self.schematic

    def ipc_ratsnest(self, session: FakeSession) -> Any:
        self._log("ipc_ratsnest", file=session.file.name)
        if self.ratsnest_ipc is None:
            raise self._unsupported("kiwrap.backends.ipc.queries.ratsnest")
        return self.ratsnest_ipc

    # file
    def file_read_board(self, path: Path) -> Any:
        self._log("file_read_board", file=Path(path).name)
        if self.file_raises:
            raise self.file_raises
        return self.board

    def file_read_schematic(self, path: Path) -> Any:
        self._log("file_read_schematic", file=Path(path).name)
        if self.file_raises:
            raise self.file_raises
        return self.schematic

    def courtyard_overlaps(self, board: Any) -> list[Any]:
        if self.analysis_missing:
            raise self._unsupported("kiwrap.formats.analysis")
        return list(self.overlaps)

    def off_board(self, board: Any) -> list[Any]:
        if self.analysis_missing:
            raise self._unsupported("kiwrap.formats.analysis")
        return list(self.off)

    def ratsnest_estimate(self, board: Any) -> list[Any]:
        self._log("ratsnest_estimate")
        if self.analysis_missing:
            raise self._unsupported("kiwrap.formats.analysis")
        return list(self.ratsnest)

    def pin_connectivity(self, sch: Any) -> Any:
        if self.analysis_missing:
            raise self._unsupported("kiwrap.formats.analysis")
        return self.connectivity

    def netlist_from_file(self, sch: Any, *, pin_function_style: str = "name_number") -> Any:
        self._log("netlist_from_file", style=pin_function_style)
        if self.analysis_missing:
            raise self._unsupported("kiwrap.formats.analysis")
        return {"nets": [{"name": "VCC", "code": 1, "nodes": [{"ref": "R1", "pin": "1"}]}]}

    # cli structured
    def sch_diff(self, install: Any, config: Any, a: Path, b: Path) -> dict[str, Any]:
        self._log("sch_diff", a=Path(a).name, b=Path(b).name, version=install.version)
        if isinstance(self.diff_json, Exception):
            raise self.diff_json
        return self.diff_json

    def services(self, storage_home: Path) -> Services:
        svc = super().services(storage_home)
        for name in (
            "ipc_session",
            "ipc_read_board",
            "ipc_read_schematic",
            "ipc_ratsnest",
            "file_read_board",
            "file_read_schematic",
            "courtyard_overlaps",
            "off_board",
            "ratsnest_estimate",
            "pin_connectivity",
            "netlist_from_file",
            "sch_diff",
        ):
            setattr(svc, name, getattr(self, name))
        return svc


def harness(tmp_path: Path, **knobs: Any) -> tuple[Harness, DataBackend, str]:
    backend = DataBackend(**knobs)
    h = Harness(tmp_path, backend)
    project = h.create()
    FakeSession.log.clear()
    return h, backend, project


def with_11(**knobs: Any) -> dict[str, Any]:
    return {"installs": [FakeInstall(version="11.0.0")], "caps": {"11.0.0": CAPS_11}, **knobs}


# ---- catalog / protocol -------------------------------------------------------------------------------------


def test_catalog_has_the_data_tools_with_typed_output_schemas_in_process(tmp_path: Path):
    h, _, _ = harness(tmp_path)
    tools = anyio.run(h.server.list_tools)
    names = [t.name for t in tools]
    assert names == list(TOOL_NAMES)
    assert {t.name for t in DATA_TOOLS} == {
        "kw_sch_get",
        "kw_sch_stats",
        "kw_sch_diff",
        "kw_pcb_get",
        "kw_pcb_check",
    }
    by_name = {t.name: t for t in tools}
    for name, model in (
        ("kw_pcb_get", "PcbGetData"),
        ("kw_sch_get", "SchGetData"),
        ("kw_pcb_check", "PcbCheckData"),
        ("kw_sch_stats", "SchStatsData"),
        ("kw_sch_diff", "SchDiffData"),
    ):
        schema = by_name[name].output_schema
        assert schema["properties"]["data"]["$ref"].endswith(model), name
        assert "stage" in schema["properties"] and "provider_used" in schema["properties"]
        assert by_name[name].annotations.read_only_hint is True
        assert by_name[name].meta["anthropic/maxResultSizeChars"] == 60_000
    assert "secondary" in by_name["kw_pcb_render"].description.lower()
    assert "secondary" in by_name["kw_sch_render"].description.lower()
    assert by_name["kw_pcb_get"].input_schema["properties"]["limit"]["maximum"] == 2000
    assert by_name["kw_pcb_get"].input_schema["properties"]["provider"]["enum"] == ["auto", "ipc11", "file"]


async def _stdio_catalog(home: Path) -> dict[str, Any]:
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "kiwrap.core.cli_main", "--storage-home", str(home), "serve"]
    )
    async with Client(params, mode="auto") as c:
        listed = await c.list_tools()
        again = await c.list_tools()
        r = await c.call_tool("kw_help", {"topic": "providers"})
        return {
            "tools": [t.name for t in listed.tools],
            "again": [t.name for t in again.tools],
            "schemas": {t.name: t.output_schema for t in listed.tools},
            "providers": (r.is_error, r.structured_content),
        }


def test_stdio_catalog_lists_the_data_tools_with_output_schemas(tmp_path: Path):
    out = anyio.run(_stdio_catalog, tmp_path / "home")
    assert out["tools"] == list(TOOL_NAMES) == out["again"]
    for name in ("kw_pcb_get", "kw_sch_get", "kw_pcb_check", "kw_sch_stats", "kw_sch_diff"):
        schema = out["schemas"][name]
        assert schema is not None and "data" in schema["properties"], name
        data_ref = schema["properties"]["data"]["$ref"].rsplit("/", 1)[-1]
        assert (
            "items" in schema["$defs"][data_ref]["properties"]
            or "changes" in schema["$defs"][data_ref]["properties"]
            or name == "kw_sch_stats"
        )
    is_err, sc = out["providers"]
    assert not is_err and "board.read" in sc["data"]["markdown"] and "ipc11" in sc["data"]["markdown"]


def test_help_has_a_page_for_every_tool_and_the_providers_topic():
    assert set(help_tool.TOOL_PAGES) == set(TOOL_NAMES)
    text, _ = help_tool.page("providers")
    assert "`diff`: cli [needs cli.has_sch_diff]" in text and "kw_sch_get on both" in text
    assert "SECONDARY" in help_tool.TOOL_PAGES["kw_pcb_render"]
    assert "Data first" in help_tool.overview()


# ---- _data helpers ------------------------------------------------------------------------------------------


def test_data_helpers():
    assert ref_key("R10") > ref_key("R2") > ref_key("R1") and ref_key(None) > ref_key("Z9")
    assert mm_box((3, 4, 1, 2)) == [1, 2, 3, 4] and mm_box({"x1": 0, "y1": 0, "x2": 5, "y2": 5}) == [
        0,
        0,
        5,
        5,
    ]
    assert rec({"a": 1}) == {"a": 1}
    assert chunk([1, 2, 3, 4, 5], None, 2) == ([1, 2], "2") and chunk([1, 2, 3], "2", 2) == ([3], None)
    with pytest.raises(KiwrapError) as ei:
        parse_cursor("abc")
    assert ei.value.code == "E_BAD_ARGS"
    f = Filters(refs=("R1",), bbox_mm=(0, 0, 20, 20))
    assert matches({"kind": "footprint", "ref": "R1", "bbox_mm": [9, 9, 11, 11]}, f)
    assert not matches({"kind": "footprint", "ref": "R1", "bbox_mm": [30, 30, 31, 31]}, f)
    assert not matches({"kind": "footprint", "ref": "R2", "bbox_mm": [9, 9, 11, 11]}, f)
    assert matches({"kind": "track", "net": "SIG", "layer": "F.Cu"}, Filters(layers=("F.Cu",), nets=("SIG",)))
    assert not matches({"kind": "track", "net": "SIG", "layer": "B.Cu"}, Filters(layers=("F.Cu",)))
    assert matches({"kind": "symbol", "ref": "R2", "sheet": "/sub/"}, Filters(sheets=("/sub/",)))
    assert not matches({"kind": "symbol", "ref": "R2", "sheet": "/sub/"}, Filters(sheets=("/",)))
    # bbox_mm reaches nested positions and airwire end points (kw_pcb_check items)
    far = Filters(bbox_mm=(100, 100, 120, 120))
    assert not matches({"check": "off_board", "pos": {"x_mm": 301.0, "y_mm": 298.5}}, far)
    assert not matches(
        {"check": "unrouted", "a": {"x_mm": 300, "y_mm": 300}, "b": {"x_mm": 154.8, "y_mm": 111.9}}, far
    )
    assert matches({"check": "unrouted", "a": {"x_mm": 300, "y_mm": 300}, "b": {"x_mm": 110, "y_mm": 110}}, far)


def test_chunk_by_budget_closes_the_page_before_the_result_budget():
    """Finding 7: a 200-item page overflowed the 60 KB text cap with next_cursor null."""
    from kiwrap.core.tools._data import PAGE_BUDGET_CHARS, chunk_by_budget

    items = [{"i": i, "blob": "x" * 5000} for i in range(50)]
    page = chunk_by_budget(items, None, 200)
    assert 0 < len(page.items) < 50 and page.shortened and page.chars <= PAGE_BUDGET_CHARS
    assert page.next_cursor == str(len(page.items)) and "cursor=" in (page.warning(200) or "")
    rest = chunk_by_budget(items, page.next_cursor, 200)
    assert rest.items[0]["i"] == len(page.items)
    # walking the cursors visits every item exactly once
    seen, cursor = [], None
    while True:
        pg = chunk_by_budget(items, cursor, 7)
        seen += [it["i"] for it in pg.items]
        cursor = pg.next_cursor
        if cursor is None:
            break
    assert seen == list(range(50))
    small = chunk_by_budget(items[:3], None, 200)
    assert len(small.items) == 3 and not small.shortened and small.next_cursor is None
    # an item bigger than the budget is still returned alone (never an empty page)
    huge = chunk_by_budget([{"blob": "y" * (PAGE_BUDGET_CHARS * 2)}, {"b": 1}], None, 10)
    assert len(huge.items) == 1 and huge.next_cursor == "1"
    rendered = chunk_by_budget([{"a": 1, "b": 2}], None, 10, render=lambda it: {"a": it["a"]})
    assert rendered.items == [{"a": 1}]


def test_provider_vocabularies_are_canonical():
    """ipc11 enum words and the file's s-expression tokens land on ONE vocabulary (findings 16/25)."""
    from kiwrap.core.tools._data import sorted_layers
    from kiwrap.core.tools.pcb_get import norm_pad
    from kiwrap.core.tools.sch_get import norm_pin

    ipc = norm_pad({"number": "1", "type": "pth", "shape": "rectangle", "layers": ["F.Cu", "F.Mask", "B.Cu", "B.Mask"]}, "C1")
    file = norm_pad({"number": "1", "type": "thru_hole", "shape": "rect", "layers": ["F.Cu", "B.Cu", "F.Mask", "B.Mask"]}, "C1")
    assert ipc["pad_type"] == file["pad_type"] == "thru_hole" and ipc["shape"] == file["shape"] == "rect"
    assert ipc["layers"] == file["layers"] == ["F.Cu", "B.Cu", "F.Mask", "B.Mask"]
    assert norm_pad({"type": "edge_connector", "shape": "chamferedrect"}, "J1")["pad_type"] == "connect"
    assert norm_pad({"type": "npth", "shape": "roundrect"}, "H1")["pad_type"] == "np_thru_hole"
    assert sorted_layers(["B.Cu", "In2.Cu", "F.SilkS", "In1.Cu", "F.Cu", "B.Paste", "F.Paste"]) == [
        "F.Cu",
        "In1.Cu",
        "In2.Cu",
        "B.Cu",
        "F.Paste",
        "B.Paste",
        "F.SilkS",
    ]
    ipc_pin = norm_pin({"number": "1", "electrical_type": "power_output", "net": "+5V"})
    file_pin = norm_pin({"number": "1", "type": "power_out", "net": None})
    assert ipc_pin["type"] == file_pin["type"] == "power_out"
    assert ipc_pin["connected"] is True and file_pin["connected"] is None
    assert norm_pin({"number": "2", "type": "tristate", "net": "unconnected-(U1-Pad2)"})["connected"] is False
    assert norm_pin({"number": "2", "type": "power_input"})["type"] == "power_in"


# ---- kw_pcb_get -------------------------------------------------------------------------------------------


def test_pcb_get_file_provider_default_kinds_and_ordering(tmp_path: Path):
    h, backend, project = harness(tmp_path)
    sc = h.ok("kw_pcb_get", project=project)
    assert sc["provider_used"] == "file" and sc["degraded"] == [] and sc["stage"] == "created"
    d = sc["data"]
    assert d["kinds"] == ["footprints", "outline"] and d["counts"] == {"footprint": 4}
    assert [i["ref"] for i in d["items"]] == ["C1", "R1", "R2", "R10"]  # natural order, not the reader's
    r1 = d["items"][1]
    assert r1["kind"] == "footprint" and r1["x_mm"] == 10.0 and r1["rotation_deg"] == 90.0
    assert r1["bbox_mm"] == [9, 9, 11, 11] and r1["pad_count"] == 2 and r1["nets"] == ["SIG", "VCC"]
    assert "pads" not in r1 and "properties" not in r1  # detail=normal
    assert d["outline"] == {"bbox_mm": [0, 0, 50, 30], "closed": True, "segments": 4}
    assert d["rules"] is None and d["connectivity"] is None and d["next_cursor"] is None
    assert d["plan"]["candidates"] == ["file"] and d["plan"]["skipped"][0]["provider"] == "ipc11"
    assert not any(c[0] == "ipc_session" for c in backend.calls)


def test_pcb_get_detail_levels(tmp_path: Path):
    h, _, project = harness(tmp_path)
    s = h.ok("kw_pcb_get", project=project, kinds=["footprints"], refs=["R1"], detail="summary")["data"][
        "items"
    ][0]
    assert set(s) == {"kind", "ref", "value", "layer", "x_mm", "y_mm", "rotation_deg"}
    f = h.ok("kw_pcb_get", project=project, kinds=["footprints"], refs=["R1"], detail="full")["data"][
        "items"
    ][0]
    assert (
        [p["number"] for p in f["pads"]] == ["1", "2"]
        and f["pads"][0]["net"] == "VCC"
        and f["pads"][0]["x_mm"] == 10.0
    )
    assert f["properties"] == {} and f["uuid"] == "fp-r1"


def test_pcb_get_all_kinds_filters_and_rules(tmp_path: Path):
    h, _, project = harness(tmp_path)
    d = h.ok(
        "kw_pcb_get",
        project=project,
        kinds=["footprints", "pads", "tracks", "vias", "zones", "nets", "rules"],
    )["data"]
    # the file's unconnected net ("", code 0) is not listed: both providers answer 3 nets
    assert d["counts"] == {"footprint": 4, "net": 3, "pad": 8, "track": 2, "via": 1, "zone": 1}
    assert all(i["name"] for i in d["items"] if i["kind"] == "net")
    kinds = [i["kind"] for i in d["items"]]
    assert kinds == sorted(kinds, key=["footprint", "pad", "track", "via", "zone", "net"].index)
    tracks = [i for i in d["items"] if i["kind"] == "track"]
    assert (
        tracks[0]["layer"] == "B.Cu" and tracks[1]["length_mm"] == 19.2167
    )  # ordered by layer then geometry
    assert (
        d["rules"]["min_clearance_mm"] == 0.2
        and d["rules"]["netclasses"]["Default"]["track_width_mm"] == 0.25
    )
    # nets filter: tracks/pads/vias/zones on GND plus the GND net itself
    g = h.ok("kw_pcb_get", project=project, kinds=["pads", "tracks", "vias", "zones", "nets"], nets=["GND"])[
        "data"
    ]
    assert g["counts"] == {"net": 1, "pad": 2, "track": 1, "via": 1, "zone": 1}
    # layers filter
    lay = h.ok("kw_pcb_get", project=project, kinds=["footprints", "tracks"], layers=["B.Cu"])["data"]
    assert [(i["kind"], i.get("ref") or i.get("net")) for i in lay["items"]] == [
        ("footprint", "R2"),
        ("track", "GND"),
    ]
    # bbox filter: anything intersecting the left 20 mm
    box = h.ok("kw_pcb_get", project=project, kinds=["footprints", "tracks", "vias"], bbox_mm=[0, 0, 20, 20])[
        "data"
    ]
    assert box["counts"] == {"footprint": 2, "track": 2}
    # refs filter restricts nets to those the refs touch
    n = h.ok("kw_pcb_get", project=project, kinds=["nets"], refs=["C1"])["data"]
    assert [i["name"] for i in n["items"]] == ["GND", "VCC"]


def test_pcb_get_chunking_is_deterministic(tmp_path: Path):
    h, _, project = harness(tmp_path)
    kinds = ["footprints", "pads", "tracks"]
    p1 = h.ok("kw_pcb_get", project=project, kinds=kinds, limit=5)["data"]
    assert p1["total"] == 14 and p1["returned"] == 5 and p1["next_cursor"] == "5"
    p2 = h.ok("kw_pcb_get", project=project, kinds=kinds, limit=5, cursor=p1["next_cursor"])["data"]
    p3 = h.ok("kw_pcb_get", project=project, kinds=kinds, limit=5, cursor=p2["next_cursor"])["data"]
    assert p3["returned"] == 4 and p3["next_cursor"] is None
    ids = [i.get("uuid") for i in p1["items"] + p2["items"] + p3["items"]]
    assert len(ids) == 14 and len(set(ids)) == 14
    assert p1 == h.ok("kw_pcb_get", project=project, kinds=kinds, limit=5)["data"]
    h.fail("kw_pcb_get", "E_BAD_ARGS", project=project, cursor="zz")
    h.fail("kw_pcb_get", "E_BAD_ARGS", project=project, bbox_mm=[1, 2, 3])
    h.fail("kw_pcb_get", "E_BAD_ARGS", project=project, provider="cli")
    h.fail("kw_pcb_get", "E_BAD_ARGS", project=project, kinds=["pads", "bogus"])


def test_pcb_get_connectivity_estimated_on_file_provider(tmp_path: Path):
    h, backend, project = harness(tmp_path)
    sc = h.ok("kw_pcb_get", project=project, kinds=["connectivity"], nets=["SIG"])
    assert sc["degraded"] == ["ratsnest_estimated"] and sc["provider_used"] == "file"
    c = sc["data"]["connectivity"]
    assert c["provider"] == "file" and c["unrouted_total"] == 2 and c["nets_with_unrouted"] == ["SIG"]
    assert c["unrouted"][0]["a"]["ref"] == "R1" and c["unrouted"][0]["length_mm"] == 0.4243
    assert sum(1 for c in backend.calls if c[0] == "file_read_board") == 1  # the file model is loaded once
    r = h.ok("kw_pcb_get", project=project, kinds=["connectivity"], refs=["R10"])["data"]["connectivity"]
    assert r["unrouted_total"] == 1 and r["unrouted"][0]["b"]["ref"] == "R1"


def test_pcb_get_ipc11_provider_opens_and_closes_the_session(tmp_path: Path):
    h, backend, project = harness(tmp_path, **with_11())
    sc = h.ok("kw_pcb_get", project=project, kinds=["footprints", "connectivity"])
    assert sc["provider_used"] == "ipc11:11.0.0"
    # the ipc reader carried no unrouted list -> a second session runs KiCad's connectivity query
    # (GetConnectedItems clusters); every session is closed
    assert FakeSession.log == ["open:demo.kicad_pcb", "close:demo.kicad_pcb"] * 2
    assert sc["degraded"] == ["airwire_endpoints_estimated"] and sc["warnings"] == []
    conn = sc["data"]["connectivity"]
    assert conn["provider"] == "ipc11:11.0.0" and conn["unrouted_total"] == 1
    assert conn["unrouted"] == [
        {
            "net": "GND",
            "a": {"ref": "C1", "pad": "2", "x_mm": 60.8, "y_mm": 5.0},
            "b": {"ref": "R2", "pad": "2", "x_mm": 11.3, "y_mm": 10.5},
            "length_mm": 49.8,
        }
    ]
    assert conn["nets_with_unrouted"] == ["GND"]
    assert sc["data"]["plan"]["candidates"] == ["ipc11", "file"]


def test_pcb_get_ipc11_without_the_connectivity_query_falls_back_to_the_file_estimate(tmp_path: Path):
    """A build whose IPC backend has no ratsnest query: the file estimate answers, tagged, with
    the reason in the warnings; the session opened for the query is closed."""
    h, backend, project = harness(tmp_path, **with_11(ratsnest_ipc=None))
    sc = h.ok("kw_pcb_get", project=project, kinds=["footprints", "connectivity"])
    assert sc["provider_used"] == "ipc11:11.0.0"
    assert FakeSession.log == ["open:demo.kicad_pcb", "close:demo.kicad_pcb"] * 2
    assert sc["degraded"] == ["ipc11_unavailable", "ratsnest_estimated"]
    assert sc["data"]["connectivity"]["provider"] == "file"
    assert any("KiCad exposes no ratsnest over IPC" in w for w in sc["warnings"])


def test_pcb_get_ipc11_reader_with_kicad_computed_unrouted(tmp_path: Path):
    board = fake_board(
        unrouted=[
            {
                "net": "GND",
                "a": {"ref": "C1", "pad": "2", "x_mm": 60.8, "y_mm": 5.0},
                "b": {"ref": "R2", "pad": "2", "x_mm": 11.3, "y_mm": 10.5},
                "length_mm": 49.8,
            }
        ]
    )
    h, _, project = harness(tmp_path, **with_11(ipc_board=board))
    sc = h.ok("kw_pcb_get", project=project, kinds=["connectivity"])
    assert sc["degraded"] == [] and sc["data"]["connectivity"]["provider"] == "ipc11:11.0.0"
    assert sc["data"]["connectivity"]["unrouted"][0]["net"] == "GND"


def test_pcb_get_falls_back_to_file_when_the_ipc_session_fails(tmp_path: Path):
    h, backend, project = harness(tmp_path, **with_11(ipc_raises=TimeoutError("api-server did not start")))
    sc = h.ok("kw_pcb_get", project=project)
    assert sc["provider_used"] == "file" and sc["degraded"] == ["ipc11_unavailable"]
    assert sc["warnings"][0].startswith("ipc11 unavailable for board.read: E_TIMEOUT")
    assert sc["data"]["counts"] == {"footprint": 4}
    # a forced provider that is eligible but fails still falls through, with the same tag
    backend.ipc_raises = KiwrapError("E_UNSUPPORTED", "kipy not installed", data={"capability": "ipc.client"})
    sc = h.ok("kw_pcb_get", project=project, provider="ipc11")
    assert sc["provider_used"] == "file" and "ipc11_unavailable" in sc["degraded"]
    # forcing the file provider on a KiCad 11 never opens a session
    FakeSession.log.clear()
    sc = h.ok("kw_pcb_get", project=project, provider="file")
    assert sc["provider_used"] == "file" and sc["degraded"] == [] and FakeSession.log == []


def test_pcb_get_unsupported_when_no_reader_exists_and_missing_board(tmp_path: Path):
    h, backend, project = harness(
        tmp_path,
        file_raises=KiwrapError("E_UNSUPPORTED", "no file reader", data={"module": "kiwrap.formats.board"}),
    )
    err = h.fail("kw_pcb_get", "E_UNSUPPORTED", project=project)
    assert "file -> E_UNSUPPORTED" in err["message"] and err["data"]["attempts"][0]["provider"] == "file"
    Path(project, "demo.kicad_pcb").unlink()
    h.fail("kw_pcb_get", "E_PROJECT_NOT_FOUND", project=project)


def test_pcb_get_with_the_default_services_reports_the_missing_component(tmp_path: Path):
    """A `Services` built without data backends (like FakeBackend.services) is E_UNSUPPORTED, not a crash."""
    h = Harness(tmp_path, FakeBackend())
    project = h.create()
    err = h.fail("kw_pcb_get", "E_UNSUPPORTED", project=project)
    assert "kiwrap.formats.board.BoardFile.load" in err["message"]


# ---- kw_sch_get ---------------------------------------------------------------------------------------------


def test_sch_get_symbols_with_absolute_pins_and_hierarchy(tmp_path: Path):
    h, _, project = harness(tmp_path)
    sc = h.ok("kw_sch_get", project=project)
    assert sc["provider_used"] == "file"
    d = sc["data"]
    assert d["counts"] == {"symbol": 3} and [s["ref"] for s in d["items"]] == ["#PWR01", "R1", "R2"]
    r1 = d["items"][1]
    assert r1["pins"][0] == {
        "number": "1",
        "name": "~",
        "type": "passive",
        "x_mm": 50.8,
        "y_mm": 59.69,
        "net": "VCC",
        "connected": True,
        "hidden": False,
    }
    assert r1["nets"] == ["SIG", "VCC"] and r1["sheet"] == "/" and "fields" not in r1
    assert d["hierarchy"]["children"][0]["path"] == "/sub/"
    assert d["items"][0]["power"] is True


def test_sch_get_kinds_filters_and_detail(tmp_path: Path):
    h, _, project = harness(tmp_path)
    d = h.ok(
        "kw_sch_get",
        project=project,
        kinds=["symbols", "wires", "labels", "junctions", "no_connects", "sheets"],
        sheets=["/sub/"],
    )["data"]
    assert d["counts"] == {"no_connect": 1, "symbol": 1, "wire": 1}
    assert d["items"][0]["ref"] == "R2" and d["items"][1]["start_mm"] == [10.0, 20.0]
    n = h.ok("kw_sch_get", project=project, kinds=["symbols", "labels", "wires"], nets=["SIG"])["data"]
    assert [i["kind"] for i in n["items"]] == ["symbol", "symbol", "wire", "wire", "label"]
    assert n["items"][4]["label_kind"] == "global" and n["items"][4]["text"] == "SIG"
    s = h.ok("kw_sch_get", project=project, kinds=["symbols"], refs=["R1"], detail="summary")["data"][
        "items"
    ][0]
    assert "pins" not in s and s["x_mm"] == 50.8
    f = h.ok("kw_sch_get", project=project, kinds=["symbols"], refs=["R1"], detail="full")["data"]["items"][0]
    assert f["fields"] == {} and f["footprint"] == "Resistor_SMD:R_0603"
    b = h.ok("kw_sch_get", project=project, kinds=["symbols", "junctions"], bbox_mm=[40, 50, 60, 70])["data"]
    assert b["counts"] == {"junction": 1, "symbol": 2}
    sh = h.ok("kw_sch_get", project=project, kinds=["sheets"])["data"]["items"][0]
    assert sh["file"] == "sub.kicad_sch" and sh["pins"] == [{"name": "SIG", "x_mm": 100.0, "y_mm": 55.0}]


def test_sch_get_netlist_prefers_kicad_cli_over_the_file_walk(tmp_path: Path):
    h, backend, project = harness(tmp_path, net=cli_netlist())
    sc = h.ok("kw_sch_get", project=project, kinds=["netlist"])
    assert sc["degraded"] == [] and sc["data"]["netlist"]["provider"] == "cli:10.0.0"
    nets = sc["data"]["netlist"]["nets"]
    assert [n["name"] for n in nets] == ["SIG", "VCC"] and nets[0]["nodes"] == [
        {"ref": "R1", "pin": "2", "sheet": None},
        {"ref": "R2", "pin": "1", "sheet": None},
    ]
    assert sc["data"]["netlist"]["components"] == 2
    # the CLI export failing on a missing component -> the file estimate, tagged
    backend.net = KiwrapError("E_UNSUPPORTED", "netlist export missing", data={"module": "x"})
    sc = h.ok("kw_sch_get", project=project, kinds=["netlist"], refs=["R1"])
    assert (
        sc["degraded"] == ["cli_unavailable", "netlist_estimated"]
        and sc["data"]["netlist"]["provider"] == "file"
    )
    assert [n["name"] for n in sc["data"]["netlist"]["nets"]] == ["VCC"]
    # nets filter on the netlist
    sc = h.ok("kw_sch_get", project=project, kinds=["netlist"], nets=["nope"])
    assert sc["data"]["netlist"]["nets"] == []


def test_sch_get_ipc11_provider_and_fallback(tmp_path: Path):
    h, backend, project = harness(tmp_path, **with_11(schematic=fake_schematic(with_netlist=True)))
    sc = h.ok("kw_sch_get", project=project, kinds=["symbols", "netlist"])
    assert sc["provider_used"] == "ipc11:11.0.0" and sc["degraded"] == []
    assert sc["data"]["netlist"]["provider"] == "ipc11:11.0.0" and sc["data"]["netlist"]["net_count"] == 2
    assert FakeSession.log == ["open:demo.kicad_sch", "close:demo.kicad_sch"]  # one session for both
    backend.ipc_raises = RuntimeError("nng connect failed")  # not an "unavailable" code: propagates
    h.fail("kw_sch_get", "E_INTERNAL", project=project)
    backend.ipc_raises = KiwrapError("E_KICAD_FAILED", "api-server exited 1")
    sc = h.ok("kw_sch_get", project=project)
    assert sc["provider_used"] == "file" and sc["degraded"] == ["ipc11_unavailable"]
    # the schematic read needs ipc.schematic too
    backend.ipc_raises = None
    backend.caps["11.0.0"] = {**CAPS_11, "ipc.schematic": False}
    h.ctx.services._caps.clear()
    sc = h.ok("kw_sch_get", project=project)
    assert sc["provider_used"] == "file" and sc["data"]["plan"]["skipped"] == [
        {"provider": "ipc11", "reason": "missing capability ipc.schematic"}
    ]


def test_sch_get_missing_root_schematic(tmp_path: Path):
    h, _, project = harness(tmp_path)
    Path(project, "demo.kicad_sch").unlink()
    h.fail("kw_sch_get", "E_PROJECT_NOT_FOUND", project=project)


# ---- kw_pcb_check -------------------------------------------------------------------------------------------


def test_pcb_check_actionable_items(tmp_path: Path):
    h, _, project = harness(tmp_path)
    sc = h.ok("kw_pcb_check", project=project)
    assert (
        sc["data"]["ok"] is False
        and sc["degraded"] == ["ratsnest_estimated"]
        and sc["provider_used"] == "file"
    )
    d = sc["data"]
    assert d["summary"] == {
        "courtyard_overlap": 1,
        "off_board": 2,
        "off_board_copper": 1,
        "unrouted": 2,
        "nets_with_unrouted": ["SIG"],
        "matched": 5,
    }
    assert [i["check"] for i in d["items"]] == [
        "off_board",
        "off_board",
        "courtyard_overlap",
        "unrouted",
        "unrouted",
    ]
    off = d["items"][0]  # C1: pad copper outside (+ its courtyard) -> ONE must_fix item
    assert off["refs"] == ["C1"] and off["severity"] == "must_fix" and off["pads"] == ["1"]
    assert off["courtyard_bbox_mm"] == [58.5, 3.5, 61.5, 6.5] and off["outside_by_mm"] == 11.5
    assert "pad 1 outside the board outline by 11.5 mm" in off["description"] and "move C1" in off["fix"]
    assert "kw_pcb_setup" not in off["fix"] and "kw_pcb_route" not in d["items"][3]["fix"]
    crt = d["items"][1]  # J1: only the courtyard overhangs -> review, does not break ok
    assert crt["refs"] == ["J1"] and crt["severity"] == "review" and crt["outside_by_mm"] == 1.0
    assert crt["courtyard_bbox_mm"] == [-1.0, 20.0, 5.0, 30.0] and "by design" in crt["fix"]
    ov = d["items"][2]
    assert (
        ov["refs"] == ["R1", "R2"] and ov["area_mm2"] == 3.61 and ov["pos"] == {"x_mm": 10.25, "y_mm": 10.25}
    )
    un = d["items"][3]
    assert (
        un["net"] == "SIG"
        and un["a"]["ref"] == "R1"
        and un["length_mm"] == 0.4243
        and "R1.2 -> R2.1" in un["description"]
    )
    assert d["drc_summary"] is None and "no kw_pcb_drc result" in sc["warnings"][0]
    assert d["providers"] == {"overlaps": "file", "off_board": "file", "unrouted": "file"}
    assert sc["next_suggested"] is None
    # filters never change the verdict: summary / ok describe the whole board, matched what was kept
    net_only = h.ok("kw_pcb_check", project=project, nets=["SIG"])["data"]
    assert net_only["ok"] is False and net_only["summary"]["off_board"] == 2
    assert net_only["summary"]["matched"] == net_only["total"] == 2
    assert all(i["check"] == "unrouted" for i in net_only["items"])
    far = h.ok("kw_pcb_check", project=project, bbox_mm=[100, 100, 120, 120])["data"]
    assert far["ok"] is False and far["total"] == 0  # off_board items are excluded by their position
    near = h.ok("kw_pcb_check", project=project, bbox_mm=[55, 0, 65, 10])["data"]
    assert [i["refs"] for i in near["items"]] == [["C1"]]


def test_pcb_check_filters_paging_and_ok(tmp_path: Path):
    h, backend, project = harness(tmp_path)
    d = h.ok("kw_pcb_check", project=project, refs=["R10"])["data"]
    assert [i["check"] for i in d["items"]] == ["unrouted"] and d["items"][0]["refs"] == ["R1", "R10"]
    d = h.ok("kw_pcb_check", project=project, checks=["unrouted"], nets=["GND"])["data"]
    assert d["total"] == 0 and d["ok"] is True  # only unrouted was checked: nothing off-board known
    d = h.ok("kw_pcb_check", project=project, limit=1)["data"]
    assert d["returned"] == 1 and d["next_cursor"] == "1" and d["total"] == 5
    d2 = h.ok("kw_pcb_check", project=project, limit=1, cursor="4")["data"]
    assert d2["items"][0]["check"] == "unrouted" and d2["next_cursor"] is None
    backend.off = [backend.off[2]]  # only the courtyard-overhang review item stays
    backend.overlaps = []
    sc = h.ok("kw_pcb_check", project=project)
    assert sc["data"]["ok"] is True and sc["next_suggested"] == "kw_pcb_drc"
    assert sc["data"]["summary"]["off_board"] == 1 and sc["data"]["summary"]["off_board_copper"] == 0
    # drc_summary alone: no provider ran, the history answered; a fresh clean DRC points at fab export
    backend.drc = drc_report()
    h.ok("kw_pcb_drc", project=project)
    sc = h.ok("kw_pcb_check", project=project, checks=["drc_summary"])
    assert sc["provider_used"] == "history" and sc["next_suggested"] == "kw_fab_export"
    sc = h.ok("kw_pcb_check", project=project)
    assert sc["next_suggested"] == "kw_fab_export" and sc["provider_used"] == "file"


def test_pcb_check_reuses_the_last_drc_and_reports_missing_analysis(tmp_path: Path):
    h, backend, project = harness(tmp_path)
    backend.drc = drc_report(violation("clearance"), violation("silk_overlap", "warning"))
    h.ok("kw_pcb_drc", project=project)
    d = h.ok("kw_pcb_check", project=project, checks=["drc_summary"])["data"]
    # a FAILED DRC is fresh too: kw_pcb_drc records the board hash in the history entry
    assert d["drc_summary"]["must_fix"] == 1 and d["drc_summary"]["fresh"] is True and d["items"] == []
    assert d["drc_summary"]["capped"] == []
    Path(project, "demo.kicad_pcb").write_text("(kicad_pcb (version 20260206) (generator edited2))\n")
    d = h.ok("kw_pcb_check", project=project, checks=["drc_summary"])["data"]
    assert d["drc_summary"]["fresh"] is False
    backend.drc = drc_report()
    h.ok("kw_pcb_drc", project=project)
    d = h.ok("kw_pcb_check", project=project, checks=["drc_summary"])["data"]
    assert d["drc_summary"]["must_fix"] == 0 and d["drc_summary"]["fresh"] is True
    Path(project, "demo.kicad_pcb").write_text("(kicad_pcb (version 20260206) (generator edited))\n")
    d = h.ok("kw_pcb_check", project=project, checks=["drc_summary"])["data"]
    assert d["drc_summary"]["fresh"] is False

    backend.analysis_missing = True
    sc = h.ok("kw_pcb_check", project=project)
    assert sc["data"]["ok"] is True and sc["data"]["items"] == []
    assert [s["check"] for s in sc["data"]["skipped"]] == ["overlaps", "off_board", "unrouted"]
    assert sc["degraded"] == ["overlaps_unavailable", "off_board_unavailable", "connectivity_unavailable"]


def test_pcb_drc_reports_kicad_reporting_caps(tmp_path: Path):
    """kicad-cli stops reporting a violation type after 199 items (499 for clearance /
    unconnected_items): the counts are lower bounds and the receipt must say so."""
    h, backend, project = harness(tmp_path)
    backend.drc = drc_report(
        *[violation("missing_courtyard", x=float(i)) for i in range(199)],
        violation("clearance"),
        unconnected=tuple(violation("unconnected_items", x=float(i)) for i in range(499)),
    )
    r = h.ok("kw_pcb_drc", project=project)
    assert r["data"]["capped"] == ["missing_courtyard", "unconnected_items"]
    assert "drc_counts_capped" in r["degraded"]
    assert any("LOWER BOUNDS" in w and "kw_pcb_check" in w for w in r["warnings"])
    d = h.ok("kw_pcb_check", project=project, checks=["drc_summary"])["data"]
    assert d["drc_summary"]["capped"] == ["missing_courtyard", "unconnected_items"]
    backend.drc = drc_report(*[violation("missing_courtyard", x=float(i)) for i in range(198)])
    r = h.ok("kw_pcb_drc", project=project)
    assert r["data"]["capped"] == [] and "drc_counts_capped" not in r["degraded"]


def test_pcb_check_ipc11_connectivity(tmp_path: Path):
    h, _, project = harness(tmp_path, **with_11())
    sc = h.ok("kw_pcb_check", project=project, checks=["unrouted"])
    assert sc["degraded"] == ["airwire_endpoints_estimated"]
    assert sc["data"]["providers"] == {"unrouted": "ipc11:11.0.0"} and sc["data"]["total"] == 1
    item = sc["data"]["items"][0]
    assert item["check"] == "unrouted" and item["net"] == "GND" and set(item["refs"]) == {"C1", "R2"}
    assert FakeSession.log == ["open:demo.kicad_pcb", "close:demo.kicad_pcb"]


def test_pcb_check_skips_the_ipc11_cluster_walk_above_the_net_threshold(tmp_path: Path, monkeypatch):
    """Finding 14: GetItemsByNet + GetConnectedItems per net takes minutes on big boards and
    answers what the file estimate answers; above KIWRAP_IPC_MAX_NETS the file provider is used
    unless provider='ipc11' forces the walk."""
    h, _, project = harness(tmp_path, **with_11())
    monkeypatch.setenv("KIWRAP_IPC_MAX_NETS", "2")
    FakeSession.net_names = ["GND", "VCC", "SIG"]
    try:
        sc = h.ok("kw_pcb_check", project=project, checks=["unrouted"])
        assert sc["data"]["providers"] == {"unrouted": "file"}
        assert "ipc11_unavailable" in sc["degraded"] and "ratsnest_estimated" in sc["degraded"]
        assert any("exceed KIWRAP_IPC_MAX_NETS=2" in w for w in sc["warnings"])
        sc = h.ok("kw_pcb_check", project=project, checks=["unrouted"], provider="ipc11")
        assert sc["data"]["providers"] == {"unrouted": "ipc11:11.0.0"}
        FakeSession.net_names = ["GND", "VCC"]
        sc = h.ok("kw_pcb_check", project=project, checks=["unrouted"])
        assert sc["data"]["providers"] == {"unrouted": "ipc11:11.0.0"}
    finally:
        FakeSession.net_names = []


def test_pcb_check_ipc11_reader_airwires_when_the_query_is_missing(tmp_path: Path):
    """Without the connectivity query the reader model's own airwires (a future GetRatsnest) are
    KiCad's answer: nothing is tagged."""
    board = fake_board(unrouted=[])
    h, _, project = harness(tmp_path, **with_11(ipc_board=board, ratsnest_ipc=None))
    sc = h.ok("kw_pcb_check", project=project, checks=["unrouted"])
    assert (
        sc["degraded"] == []
        and sc["data"]["providers"] == {"unrouted": "ipc11:11.0.0"}
        and sc["data"]["total"] == 0
    )
    assert FakeSession.log == ["open:demo.kicad_pcb", "close:demo.kicad_pcb"]


# ---- kw_sch_stats -------------------------------------------------------------------------------------------


def test_sch_stats_counts_lists_and_per_sheet(tmp_path: Path):
    h, _, project = harness(tmp_path, net=cli_netlist())
    sc = h.ok("kw_sch_stats", project=project)
    d = sc["data"]
    assert d["ok"] is False and sc["next_suggested"] == "kw_sch_check"
    assert (d["components"], d["power_symbols"], d["symbols_total"], d["sheets"]) == (2, 1, 3, 1)
    assert d["nets"] == 2 and d["single_node_nets"] == ["VCC"]
    assert (d["dangling_ends"], d["unconnected_pins"], d["label_only_nets"]) == (1, 1, 1)
    assert (
        d["lists"]["unconnected_pins"][0]["ref"] == "R2"
        and d["lists"]["dangling_ends"][0]["sheet"] == "/sub/"
    )
    assert d["missing_footprint"] == ["R2"]
    assert d["per_sheet"] == {
        "/": {"junctions": 1, "labels": 1, "sheets": 1, "symbols": 2, "wires": 1},
        "/sub/": {"no_connects": 1, "symbols": 1, "wires": 1},
    }
    assert d["providers"] == {"schematic": "file", "netlist": "cli:10.0.0", "connectivity": "file"}
    assert sc["degraded"] == []


def test_sch_stats_degrades_without_analysis_and_netlist(tmp_path: Path):
    h, backend, project = harness(
        tmp_path,
        analysis_missing=True,
        net=KiwrapError("E_UNSUPPORTED", "no netlist export", data={"module": "x"}),
    )
    sc = h.ok("kw_sch_stats", project=project)
    d = sc["data"]
    assert d["ok"] is True and d["nets"] is None and d["dangling_ends"] is None and d["lists"] == {}
    assert sc["degraded"] == ["netlist_unavailable", "connectivity_unavailable"]
    assert len(sc["warnings"]) == 2
    backend.connectivity = {"dangling_ends": [], "unconnected_pins": [], "label_only_nets": []}
    backend.analysis_missing = False
    sc = h.ok("kw_sch_stats", project=project)
    assert sc["data"]["ok"] is True and sc["data"]["dangling_ends"] == 0


# ---- kw_sch_diff --------------------------------------------------------------------------------------------


def test_sch_diff_normalises_kicad_output(tmp_path: Path):
    h, backend, project = harness(tmp_path, **with_11())
    a, b = Path(project, "demo.kicad_sch"), Path(project, "demo_v2.kicad_sch")
    b.write_text(a.read_text())
    sc = h.ok("kw_sch_diff", a=str(a), b=str(b))
    assert sc["provider_used"] == "cli:11.0.0" and backend.calls[-1][0] == "sch_diff"
    d = sc["data"]
    assert d["identical"] is False and d["counts"] == {"added": 1, "modified": 1, "removed": 1}
    assert d["by_type"] == {"SCH_JUNCTION": 1, "SCH_LINE": 1, "SCH_SYMBOL": 1}
    mod = d["changes"][0]  # modified first, then added, then removed
    assert mod["kind"] == "modified" and mod["ref"] == "R1" and mod["type"] == "SCH_SYMBOL"
    assert mod["bbox_mm"] == [153.589, 78.4251, 161.163, 91.6534]
    assert (
        mod["sheet_uuid"] == "28f865a0-4433-4a53-bbd7-b62f276848e4"
        and mod["uuid"] == "00000000-0000-0000-0000-00004549f38a"
    )
    assert mod["properties"][0]["before_mm"] == 157.48 and mod["properties"][0]["after_mm"] == 160.02
    assert mod["changed_properties"] == ["Position X"]
    assert d["changes"][1]["kind"] == "added" and d["changes"][1]["properties"][0]["after"]["label"] == "Wire"
    assert "before_mm" not in d["changes"][1]["properties"][0]
    # filters + paging
    d = h.ok("kw_sch_diff", a=str(a), b=str(b), kinds=["removed"])["data"]
    assert d["total"] == 1 and d["changes"][0]["type"] == "SCH_JUNCTION"
    d = h.ok("kw_sch_diff", a=str(a), b=str(b), refs=["R1"], types=["SCH_SYMBOL"])["data"]
    assert d["total"] == 1
    d = h.ok("kw_sch_diff", a=str(a), b=str(b), limit=2)["data"]
    assert d["returned"] == 2 and d["next_cursor"] == "2"
    backend.diff_json = {"docType": "kicad_sch", "path": str(b), "changes": []}
    d = h.ok("kw_sch_diff", a=str(a), b=str(b))["data"]
    assert d["identical"] is True and d["total"] == 0


def test_sch_diff_is_unsupported_without_the_subcommand_and_names_the_fallback(tmp_path: Path):
    h, backend, project = harness(tmp_path)  # KiCad 10 only
    a = Path(project, "demo.kicad_sch")
    err = h.fail("kw_sch_diff", "E_UNSUPPORTED", a=str(a), b=str(a))
    assert (
        err["data"]["fallback"] == "kw_sch_get on both files + compare"
        and err["data"]["capability"] == "cli.has_sch_diff"
    )
    assert "kw_sch_get" in err["fix"] and err["candidates"] == []
    assert not any(c[0] == "sch_diff" for c in backend.calls)
    # an explicit target that cannot diff says so too; a bad path / non-schematic is the caller's error
    h.fail("kw_sch_diff", "E_UNSUPPORTED", a=str(a), b=str(a), target_kicad="10")
    h.fail("kw_sch_diff", "E_PROJECT_NOT_FOUND", a=str(a), b=str(Path(project, "nope.kicad_sch")))
    h.fail("kw_sch_diff", "E_BAD_ARGS", a=str(a), b=str(Path(project, "demo.kicad_pro")))
    h.fail("kw_sch_diff", "E_NO_KICAD", a=str(a), b=str(a), target_kicad="99")


def test_sch_diff_picks_the_newest_install_that_can_diff(tmp_path: Path):
    knobs = {
        "installs": [FakeInstall(version="11.0.0"), FakeInstall(version="10.0.0")],
        "caps": {"11.0.0": CAPS_11, "10.0.0": CAPS_10D},
    }
    h, backend, project = harness(tmp_path, **knobs)
    a = Path(project, "demo.kicad_sch")
    sc = h.ok("kw_sch_diff", a=str(a), b=str(a))
    assert sc["provider_used"] == "cli:11.0.0"
    backend.installs.reverse()
    h.ctx.services.installs(refresh=True)
    sc = h.ok("kw_sch_diff", a=str(a), b=str(a))
    assert sc["provider_used"] == "cli:11.0.0"  # the 10 comes first now but cannot diff


def test_sch_diff_accepts_the_parsed_schdiff_object(tmp_path: Path):
    """The real CLI backend returns `kiwrap.formats.reports.schdiff.SchDiff`, not raw JSON; both
    normalise to the same items."""
    schdiff = pytest.importorskip("kiwrap.formats.reports.schdiff")
    parsed = schdiff.parse_sch_diff_json(
        json.dumps(DIFF_JSON), reference="a.kicad_sch", comparison="b.kicad_sch", exit_code=5
    )
    from_obj, meta_obj = sch_diff_tool.normalise(parsed)
    from_raw, meta_raw = sch_diff_tool.normalise(json.loads(json.dumps(DIFF_JSON)))
    assert from_obj == from_raw and meta_obj == meta_raw
    h, backend, project = harness(tmp_path, **with_11(diff_json=parsed))
    a = Path(project, "demo.kicad_sch")
    d = h.ok("kw_sch_diff", a=str(a), b=str(a))["data"]
    assert (
        d["identical"] is False and d["total"] == 3 and d["changes"][0]["properties"][0]["after_mm"] == 160.02
    )


@pytest.mark.kicad_version("10.99")
def test_sch_diff_on_the_nightly(tmp_path: Path, installs: list[Any], storage_home: Path):
    """`kicad-cli sch diff --format json` through the real CLI backend: identical files -> exit 0 and
    no changes; a symbol moved by one grid step -> one modified SCH_SYMBOL with Position X in mm."""
    pytest.importorskip("kiwrap.backends.cli.structured")
    nightly = next(i for i in installs if i.major_minor == "10.99")
    demo = Path(nightly.root) / "share" / "kicad" / "demos" / "ecc83" / "ecc83-pp.kicad_sch"
    if not demo.is_file():
        pytest.skip(f"no ecc83 demo at {demo}")
    a = tmp_path / "a.kicad_sch"
    shutil.copyfile(demo, a)
    raw = a.read_bytes()
    import re

    m = re.search(rb'\(symbol\s*\(lib_id "ecc83-pp:R"\)\s*\(at ([\d.]+) ([\d.]+) (\d+)\)', raw)
    assert m, "the ecc83 demo no longer has an ecc83-pp:R symbol"
    b = tmp_path / "b.kicad_sch"
    b.write_bytes(raw[: m.start(1)] + str(round(float(m.group(1)) + 2.54, 2)).encode() + raw[m.end(1) :])
    from kiwrap.core.server import build_context, build_server

    server = build_server(context=build_context(storage_home))

    def call(**args: Any) -> dict[str, Any]:
        res = anyio.run(server.call_tool, "kw_sch_diff", args)
        assert not res.is_error, res.structured_content
        return res.structured_content

    same = call(a=str(a), b=str(a), target_kicad=nightly.version)
    assert same["provider_used"] == f"cli:{nightly.version}" and same["data"]["identical"] is True
    moved = call(a=str(a), b=str(b), target_kicad=nightly.version)
    d = moved["data"]
    assert d["identical"] is False and d["counts"] == {"modified": 1} and d["by_type"] == {"SCH_SYMBOL": 1}
    change = d["changes"][0]
    assert change["ref"] and change["changed_properties"] == ["Position X"]
    px = change["properties"][0]
    assert px["before_mm"] == float(m.group(1)) and px["after_mm"] == round(float(m.group(1)) + 2.54, 2)


def test_sch_diff_unit_helpers():
    assert sch_diff_tool.iu_to_mm(1574800) == 157.48 and sch_diff_tool.iu_to_mm("x") is None
    p = sch_diff_tool.norm_property(
        {"name": "Text", "before": {"type": "string", "v": "a"}, "after": {"type": "string", "v": "b"}}
    )
    assert "before_mm" not in p
    c = sch_diff_tool.norm_change({"kind": "added", "id": "", "bbox": None})
    assert c["uuid"] is None and c["sheet_uuid"] is None and c["bbox_mm"] is None


# ---- end to end on the nightly (real backends, only when they exist) ----------------------------------------


def _importable(*modules: str) -> list[str]:
    missing = []
    for m in modules:
        try:
            importlib.import_module(m)
        except Exception:  # noqa: BLE001 - any import failure means "not available"
            missing.append(m)
    return missing


def _api_servers_for(path: Path) -> list[str]:
    """Command lines of running `kicad-cli api-server` processes that mention `path` (Windows:
    CIM; elsewhere `ps`). Other components' kicad-cli runs share this machine, so only servers
    bound to this test's files count."""
    import subprocess

    if sys.platform == "win32":
        cmd = [
            "powershell",
            "-NoProfile",
            "-Command",
            "Get-CimInstance Win32_Process -Filter \"Name='kicad-cli.exe'\" | "
            "ForEach-Object { $_.CommandLine }",
        ]
    else:
        cmd = ["ps", "-eo", "args"]
    out = subprocess.run(cmd, capture_output=True, text=True, check=False).stdout
    needle = str(path).replace("\\", "/").lower()
    return [
        line
        for line in out.splitlines()
        if "api-server" in line and needle in line.replace("\\", "/").lower()
    ]


@pytest.mark.kicad_version("10.99")
@pytest.mark.slow
def test_e2e_nightly_ecc83_pcb_get_sch_get_pcb_check(tmp_path: Path, installs: list[Any], storage_home: Path):
    """kw_pcb_get + kw_sch_get + kw_pcb_check on a copy of the nightly's ecc83 demo through the REAL
    services (headless api-server when `kiwrap.backends.ipc` exists, else the file engine). Every
    api-server the session starts is closed by the tool; the user's KiCad config is never touched."""
    missing = _importable("kiwrap.formats.board", "kiwrap.formats.schematic", "kiwrap.formats.analysis")
    ipc_missing = _importable("kiwrap.backends.ipc.sessions", "kiwrap.backends.ipc.readers")
    if missing:
        pytest.skip("file backends not importable yet: " + ", ".join(missing))
    nightly = next(i for i in installs if i.major_minor == "10.99")
    demo = Path(nightly.root) / "share" / "kicad" / "demos" / "ecc83"
    if not demo.is_dir():
        pytest.skip(f"no ecc83 demo under {demo}")
    work = tmp_path / "ecc83"
    shutil.copytree(demo, work, ignore=shutil.ignore_patterns("*.lck", "*-backups"))
    pro = work / "ecc83-pp.kicad_pro"  # the demo folder also holds ecc83-pp_v2: name the project
    from kiwrap import sandbox

    before = sandbox.user_config_hash(nightly)
    project = Project.from_path(pro)
    project.save_state(ProjectState(target_kicad=nightly.version))
    from kiwrap.core.server import build_context, build_server

    ctx = build_context(storage_home)
    server = build_server(context=ctx)

    def call(tool: str, **args: Any) -> dict[str, Any]:
        res = anyio.run(server.call_tool, tool, args)
        assert not res.is_error, res.structured_content
        return res.structured_content

    pcb = call("kw_pcb_get", project=str(pro), kinds=["footprints", "nets", "outline", "rules"], limit=500)
    assert pcb["data"]["counts"]["footprint"] >= 10 and pcb["data"]["outline"]["bbox_mm"]
    fps = [i for i in pcb["data"]["items"] if i["kind"] == "footprint"]
    assert all(i["x_mm"] is not None and i["bbox_mm"] for i in fps)
    assert any(n["name"] for n in pcb["data"]["items"] if n["kind"] == "net")
    sch = call("kw_sch_get", project=str(pro), kinds=["symbols", "wires", "netlist"], limit=500)
    assert sch["data"]["counts"]["symbol"] >= 10 and sch["data"]["netlist"]["net_count"] > 0
    syms = [i for i in sch["data"]["items"] if i["kind"] == "symbol"]
    assert any(p["x_mm"] is not None for s in syms for p in s["pins"])
    assert all(s["ref"] for s in syms)
    chk = call("kw_pcb_check", project=str(pro))
    assert set(chk["data"]["summary"]) >= {"courtyard_overlap", "off_board", "unrouted"}
    expected = "file" if ipc_missing else f"ipc11:{nightly.version}"
    assert pcb["provider_used"] == expected and sch["provider_used"] == expected
    assert sandbox.user_config_hash(nightly) == before
    if not ipc_missing:
        from kiwrap.backends.ipc.sessions import SESSION_POOL, close_all

        assert SESSION_POOL.reused >= 1  # the board / schematic servers were kept between the calls
        close_all()  # what the server does at shutdown (and the pool after the idle timeout)
    assert _api_servers_for(work) == []  # every api-server the tools started is gone
