"""The authoring tools (kw_sch_add_symbol / kw_sch_connect / kw_sch_add_item / kw_sch_edit /
kw_sch_annotate / kw_sch_assign_footprints / kw_pcb_setup / kw_pcb_sync / kw_pcb_place /
kw_pcb_zones / kw_pcb_route), the planner's authoring operations, the gates and the stage
transitions - against FAKED writer / resolver / index / IPC-twin contracts (the real ones are
built concurrently; `kiwrap.core.tools._author` documents the contract the fakes implement).
No KiCad involved.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anyio
import pytest

from kiwrap.core import planner
from kiwrap.core.state import KiwrapError
from kiwrap.core.state.project import Project
from kiwrap.core.tools import ALL_TOOLS, AUTHORING_TOOLS, TOOL_NAMES
from kiwrap.core.tools import help as help_tool
from kiwrap.core.tools._author import epoch_for, g13_guard
from kiwrap.core.tools.sch_author import wire_points
from kiwrap.formats.reports import (
    Component,
    Net,
    Netlist,
    NetNode,
    Position,
    Report,
    Violation,
    ViolationItem,
)
from tests.test_core_fakes import CAPS_10, FakeBackend, FakeInstall, erc_report, violation
from tests.test_core_tools import Harness

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH (snapshots)")

CAPS_11 = {
    **CAPS_10,
    "ipc.headless": True,
    "ipc.schematic": True,
    "ipc.schematic_update_shared_lib_symbol_hangs": True,
    "cli.subcommands": ["sch erc", "pcb drc", "sch export netlist"],
}
CAPS_11_NO_G13 = {**CAPS_11, "ipc.schematic_update_shared_lib_symbol_hangs": False}

AUTHORING_NAMES = (
    "kw_sch_add_symbol",
    "kw_sch_connect",
    "kw_sch_add_item",
    "kw_sch_edit",
    "kw_sch_annotate",
    "kw_sch_assign_footprints",
    "kw_pcb_setup",
    "kw_pcb_sync",
    "kw_pcb_place",
    "kw_pcb_zones",
    "kw_pcb_route",
)


# ---- fakes of the writer contracts -------------------------------------------------------------------------


@dataclass(frozen=True)
class FPin:
    number: str
    name: str
    x_mm: float
    y_mm: float
    electrical_type: str = "passive"


@dataclass(frozen=True)
class FPlacement:
    uuid: str
    ref: str | None
    pins: list[FPin]


@dataclass(frozen=True)
class FSheetPlacement:
    uuid: str
    pins: list[dict[str, Any]]


@dataclass
class Registry:
    """What the fake writers remember per project directory (shared by writer instances)."""

    symbols: dict[str, dict[str, Any]] = field(default_factory=dict)  # ref -> record
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    counter: int = 0
    footprints: dict[str, dict[str, Any]] = field(default_factory=dict)  # board: ref -> {x, y, rot, layer}
    board_items: dict[str, dict[str, Any]] = field(default_factory=dict)  # uuid -> track/via/zone
    writer_kwargs: list[dict[str, Any]] = field(default_factory=list)

    def uid(self, prefix: str) -> str:
        self.counter += 1
        return f"{prefix}-{self.counter:04d}"


REGISTRIES: dict[str, Registry] = {}


def registry(project_dir: Path) -> Registry:
    return REGISTRIES.setdefault(str(Path(project_dir).resolve()), Registry())


def pins_for(lib_id: str, at: tuple[float, float], unit: int = 1) -> list[FPin]:
    """Two pins 3.81 mm above / below the origin (a resistor), or 4 for a `Quad` symbol."""
    x, y = at
    if "Quad" in lib_id:
        base = (unit - 1) * 4
        return [
            FPin(str(base + 1), "IN", x - 7.62, y - 2.54, "input"),
            FPin(str(base + 2), "OUT", x + 7.62, y, "output"),
            FPin(str(base + 3), "V+", x, y - 7.62, "power_in"),
            FPin(str(base + 4), "V-", x, y + 7.62, "power_in"),
        ]
    return [FPin("1", "~", x, y - 3.81), FPin("2", "~", x, y + 3.81)]


class FakeSchematicWriter:
    """`kiwrap.formats.schematic_writer.SchematicWriter` contract."""

    def __init__(self, project_dir: Path, *, epoch: dict[str, int], lib_resolver: Any):
        self.dir = Path(project_dir)
        self.reg = registry(self.dir)
        self.reg.writer_kwargs.append({"epoch": dict(epoch), "lib_resolver": lib_resolver})
        self.loaded: Path | None = None
        self.touched: set[Path] = set()

    def _log(self, event: str, **kw: Any) -> None:
        self.reg.calls.append((event, kw))

    def load(self, sheet_path: Path) -> None:
        self.loaded = Path(sheet_path)

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
    ) -> FPlacement:
        self._log(
            "add_symbol",
            lib_id=lib_id,
            ref=ref,
            at_mm=at_mm,
            rotation=rotation,
            mirror=mirror,
            unit=unit,
            sheet=sheet,
        )
        uid = self.reg.uid("sym")
        ref = ref or (lib_id.split(":")[-1][:1] + "?")
        pins = pins_for(lib_id, at_mm, unit)
        self.reg.symbols[ref] = {
            "uuid": uid,
            "lib_id": lib_id,
            "unit": unit,
            "unit_count": 4 if "Quad" in lib_id else 1,
            "footprint": footprint or "",
            "value": value or lib_id.split(":")[-1],
            "pins": pins,
            "power": lib_id.startswith("power:"),
        }
        self.touched.add(self.loaded or self.dir)
        return FPlacement(uid, ref, pins)

    def pins_of(self, ref: str) -> list[FPin]:
        rec = self.reg.symbols.get(ref)
        if rec is None:
            raise KeyError(ref)
        return list(rec["pins"])

    def symbols(self, sheet: str = "") -> list[FPlacement]:
        return [FPlacement(r["uuid"], ref, list(r["pins"])) for ref, r in self.reg.symbols.items()]

    def add_wire(self, points_mm: list[tuple[float, float]], sheet: str = "") -> list[str]:
        self._log("add_wire", points_mm=[tuple(p) for p in points_mm], sheet=sheet)
        self.touched.add(self.loaded or self.dir)
        return [self.reg.uid("wire") for _ in range(max(1, len(points_mm) - 1))]

    def add_label(
        self, text: str, at_mm: tuple[float, float], kind: str, rotation: int = 0, sheet: str = ""
    ) -> str:
        self._log("add_label", text=text, at_mm=tuple(at_mm), kind=kind, rotation=rotation, sheet=sheet)
        self.touched.add(self.loaded or self.dir)
        return self.reg.uid("label")

    def add_power(
        self, net: str, at_mm: tuple[float, float], rotation: int = 0, sheet: str = "", flag: bool = False
    ) -> FPlacement:
        self._log("add_power", net=net, at_mm=tuple(at_mm), rotation=rotation, sheet=sheet, flag=flag)
        uid = self.reg.uid("pwr")
        ref = f"#FLG{self.reg.counter}" if flag else f"#PWR{self.reg.counter}"
        self.reg.symbols[ref] = {
            "uuid": uid,
            "lib_id": "power:PWR_FLAG" if flag else f"power:{net}",
            "unit": 1,
            "unit_count": 1,
            "footprint": "",
            "value": net,
            "pins": [FPin("1", net, *at_mm, "power_in")],
            "power": True,
        }
        self.touched.add(self.loaded or self.dir)
        return FPlacement(uid, ref, [FPin("1", net, at_mm[0], at_mm[1], "power_in")])

    def add_no_connect(self, at_mm: tuple[float, float], sheet: str = "") -> str:
        self._log("add_no_connect", at_mm=tuple(at_mm), sheet=sheet)
        self.touched.add(self.loaded or self.dir)
        return self.reg.uid("nc")

    def add_sheet(
        self, name: str, file: str, at_mm: Any, size_mm: Any, pins: list[Any], sheet: str = ""
    ) -> FSheetPlacement:
        self._log(
            "add_sheet",
            name=name,
            file=file,
            at_mm=tuple(at_mm),
            size_mm=tuple(size_mm),
            pins=list(pins),
            sheet=sheet,
        )
        (self.dir / file).write_text("(kicad_sch (version 20260306) (generator fake))\n", encoding="utf-8")
        self.touched.add(self.loaded or self.dir)
        self.touched.add(self.dir / file)
        # the real writer's SheetPinEndpoint(name, kind, side, x_mm, y_mm, uuid): pins laid out
        # along the side at a 2.54 mm pitch
        return FSheetPlacement(
            self.reg.uid("sheet"),
            [
                {
                    "name": p[0],
                    "kind": p[2],
                    "side": p[1],
                    "x_mm": float(at_mm[0]) if p[1] == "left" else float(at_mm[0]) + float(size_mm[0]),
                    "y_mm": float(at_mm[1]) + 2.54 * (i + 1),
                    "uuid": self.reg.uid("sheet-pin"),
                }
                for i, p in enumerate(pins)
            ],
        )

    def edit(self, uuid: str, changes: dict[str, Any]) -> None:
        self._log("edit", uuid=uuid, changes=dict(changes))
        self.touched.add(self.loaded or self.dir)

    def delete(self, uuid: str) -> None:
        self._log("delete", uuid=uuid)
        for ref, rec in list(self.reg.symbols.items()):
            if rec["uuid"] == uuid:
                del self.reg.symbols[ref]
        self.touched.add(self.loaded or self.dir)

    def annotate(self, scheme: str = "sheet", start_per_sheet: bool = True) -> dict[str, str]:
        self._log("annotate", scheme=scheme, start_per_sheet=start_per_sheet)
        mapping: dict[str, str] = {}
        n = 0
        for ref in sorted(self.reg.symbols):
            if ref.endswith("?"):
                n += 1
                new = f"{ref[:-1]}{n}"
                mapping[ref] = new
                self.reg.symbols[new] = self.reg.symbols.pop(ref)
        self.touched.add(self.loaded or self.dir)
        return mapping

    def assign_footprints(self, mapping: dict[str, str]) -> list[str]:
        self._log("assign_footprints", mapping=dict(mapping))
        out = []
        for ref, fp in mapping.items():
            if ref in self.reg.symbols:
                self.reg.symbols[ref]["footprint"] = fp
                out.append(ref)
        self.touched.add(self.loaded or self.dir)
        return out

    def save(self) -> list[Path]:
        self._log("save")
        paths = []
        for p in sorted(self.touched):
            if p.is_file():
                with p.open("a", encoding="utf-8") as fh:
                    fh.write(f"; fake write {self.reg.counter}\n")
                paths.append(p)
        return paths


class BrokenWriter:
    def __init__(self, *a: Any, **kw: Any):
        raise NotImplementedError("schematic writer stub")


@dataclass(frozen=True)
class FLibSymbol:
    lib_id: str
    footprint_filters: tuple[str, ...]
    units: int = 1


class FNotFound(Exception):
    """`kiwrap.formats.libs.NotFound` as the assign tool reads it (message + candidates)."""

    def __init__(self, message: str, candidates: list[str]):
        super().__init__(message)
        self.message, self.candidates = message, candidates


class FakeLibResolver:
    #: fpids the fp-lib-table "knows"; everything else is NotFound with same-library candidates
    known_footprints: set[str] = {
        "Resistor_SMD:R_0603_1608Metric",
        "Capacitor_SMD:C_0603_1608Metric",
        "P:Q",
        "Package_TO_SOT_SMD:SOT-223-3_TabPin2",
    }

    def __init__(self, install: Any, config_root: Any, project_dir: Any):
        self.install, self.config_root, self.project_dir = install, config_root, project_dir

    def symbol(self, lib_id: str) -> FLibSymbol:
        if lib_id == "Device:Bogus":
            raise KeyError(lib_id)
        return FLibSymbol(lib_id, ("R_*",) if lib_id == "Device:R" else ("C_*",))

    def footprint(self, fpid: str) -> dict[str, str]:
        if fpid in self.known_footprints:
            return {"fpid": fpid}
        nick, name = fpid.split(":", 1) if ":" in fpid else ("", fpid)
        if nick not in {k.split(":")[0] for k in self.known_footprints}:
            raise FNotFound(f"footprint library nickname {nick!r} is not in the fp-lib-table", [])
        siblings = [k.split(":", 1)[1] for k in self.known_footprints if k.startswith(nick + ":")]
        raise FNotFound(
            f"footprint {name!r} is not in {nick}", [c for c in siblings if name.lower() in c.lower()]
        )


@dataclass(frozen=True)
class FHit:
    id: str
    name: str
    description: str
    keywords: str
    pad_count: int
    footprint_filters: tuple[str, ...]
    score: float


class FakeLibIndex:
    """`kiwrap.formats.libs.index.LibIndex` as kw_sch_assign_footprints uses it: `footprints_for`
    (a symbol's ki_fp_filters -> footprints with pad counts, exact pin-count matches first) and
    `search` (free text, the fallback when a build lacks footprints_for)."""

    searches: list[tuple[str, str, dict[str, Any] | None, int]] = []
    filter_calls: list[tuple[tuple[str, ...], int | None, int]] = []

    def __init__(self, install: Any, config_root: Any, storage_home: Any):
        self.install = install

    def footprints_for(
        self, filters: Any, *, pin_count: int | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        FakeLibIndex.filter_calls.append((tuple(filters), pin_count, limit))
        if "R_*" in filters:
            return [
                {
                    "id": "Resistor_SMD:R_0603_1608Metric",
                    "description": "Resistor SMD 0603",
                    "pad_count": 2,
                    "attributes": ["smd"],
                    "pin_count_match": pin_count in (None, 2),
                }
            ]
        return []

    def search(self, kind: str, query: str, filters: dict[str, Any] | None, limit: int) -> list[FHit]:
        FakeLibIndex.searches.append((kind, query, dict(filters or {}), limit))
        return []


class FSym:
    """`kiwrap.formats.schematic.SchSymbol` as `symbols_of` reads it (through `rec()`)."""

    def __init__(self, ref: str, r: dict[str, Any]):
        self.reference, self.uuid, self.lib_id = ref, r["uuid"], r["lib_id"]
        self.unit, self.unit_count, self.footprint = r["unit"], r["unit_count"], r["footprint"]
        self.value, self.is_power = r["value"], r["power"]

    def reference_for(self, path: str) -> str:
        return self.reference

    def unit_for(self, path: str) -> int:
        return self.unit


class FInst:
    path = "/root-uuid"
    names_path = "/"


class FHierarchy:
    def __init__(self, reg: Registry):
        self.reg = reg

    def symbols(self):
        for ref, r in self.reg.symbols.items():
            yield FInst(), FSym(ref, r)


# --- board


@dataclass(frozen=True)
class FSyncReport:
    added: list[str]
    updated: list[str]
    removed: list[str]
    footprint_changes: list[dict[str, Any]]
    unresolved: list[tuple[str, str, str]]
    pad_net_changes: list[Any]


class FakeBoardWriter:
    """`kiwrap.formats.board_writer.BoardWriter` contract."""

    unresolved: list[tuple[str, str, str]] = []

    def __init__(self, project_dir: Path, *, epoch: dict[str, int], lib_resolver: Any):
        self.dir = Path(project_dir)
        self.reg = registry(self.dir)
        self.reg.writer_kwargs.append({"epoch": dict(epoch), "lib_resolver": lib_resolver})
        self.board: Path | None = None
        self.pro_touched = False
        self.dru: str | None = None

    def _log(self, event: str, **kw: Any) -> None:
        self.reg.calls.append((event, kw))

    def load(self, board_path: Path) -> None:
        self.board = Path(board_path)

    def setup(
        self,
        layers: Any = None,
        rules: Any = None,
        netclasses: Any = None,
        outline: Any = None,
        custom_rules: Any = None,
    ) -> dict[str, Any]:
        self._log(
            "setup",
            layers=layers,
            rules=rules,
            netclasses=netclasses,
            outline=outline,
            custom_rules=custom_rules,
        )
        self.pro_touched = netclasses is not None
        self.dru = custom_rules
        return {"layers": layers, "rules": rules or {}}

    def sync(self, netlist: Any, *, remove_orphans: bool = True, keep_positions: bool = True) -> FSyncReport:
        self._log(
            "sync",
            remove_orphans=remove_orphans,
            keep_positions=keep_positions,
            components=len(list(netlist.components)),
        )
        added, updated = [], []
        for i, comp in enumerate(netlist.components):
            if comp.ref in self.reg.footprints:
                updated.append(comp.ref)
            else:
                self.reg.footprints[comp.ref] = {
                    "x": 100.0 + 10 * i,
                    "y": 100.0,
                    "rot": 0.0,
                    "layer": "F.Cu",
                    "fpid": comp.footprint,
                }
                added.append(comp.ref)
        return FSyncReport(added, updated, [], [], list(FakeBoardWriter.unresolved), [])

    def place(
        self,
        ref: str,
        at_mm: tuple[float, float],
        rotation: float | None = None,
        side: str | None = None,
        locked: bool | None = None,
    ) -> None:
        self._log("place", ref=ref, at_mm=tuple(at_mm), rotation=rotation, side=side, locked=locked)
        fp = self.reg.footprints[ref]
        fp["x"], fp["y"] = at_mm
        if rotation is not None:
            fp["rot"] = rotation
        if side is not None:
            fp["layer"] = "F.Cu" if side == "top" else "B.Cu"

    def add_zone(
        self, net: str, layers: list[str], polygon_mm: list[Any], priority: int = 0, **extra: Any
    ) -> str:
        self._log(
            "add_zone",
            net=net,
            layers=list(layers),
            polygon_mm=[tuple(p) for p in polygon_mm],
            priority=priority,
            **extra,
        )
        uid = self.reg.uid("zone")
        self.reg.board_items[uid] = {"kind": "zone", "net": net}
        return uid

    def add_track(self, pts: list[Any], width_mm: float, layer: str, net: str) -> list[str]:
        self._log("add_track", pts=[tuple(p) for p in pts], width_mm=width_mm, layer=layer, net=net)
        ids = [self.reg.uid("track") for _ in range(len(pts) - 1)]
        for u in ids:
            self.reg.board_items[u] = {"kind": "track", "net": net}
        return ids

    def add_via(
        self, at_mm: tuple[float, float], size_mm: float, drill_mm: float, net: str, layers: list[str]
    ) -> str:
        self._log(
            "add_via", at_mm=tuple(at_mm), size_mm=size_mm, drill_mm=drill_mm, net=net, layers=list(layers)
        )
        uid = self.reg.uid("via")
        self.reg.board_items[uid] = {"kind": "via", "net": net}
        return uid

    def delete(self, uuid: str) -> None:
        self._log("delete", uuid=uuid)
        self.reg.board_items.pop(uuid, None)

    def save(self) -> Path:
        self._log("save_board")
        assert self.board is not None
        with self.board.open("a", encoding="utf-8") as fh:
            fh.write(f"; fake board write {self.reg.counter}\n")
        if self.dru is not None:
            (self.dir / f"{self.board.stem}.kicad_dru").write_text(self.dru, encoding="utf-8")
        if self.pro_touched:
            pro = self.dir / f"{self.board.stem}.kicad_pro"
            with pro.open("a", encoding="utf-8") as fh:
                fh.write("\n")
        return self.board


BOARD_W, BOARD_H = 50.0, 30.0


class FFootprint:
    def __init__(self, ref: str, r: dict[str, Any]):
        self.reference, self.position, self.rotation = ref, (r["x"], r["y"]), r["rot"]
        self.layer, self.lib_id, self.value, self.uuid = r["layer"], r.get("fpid") or "", "", f"fp-{ref}"
        self.pads: list[Any] = []


@dataclass(frozen=True)
class FZone:
    net: str
    is_rule_area: bool = False


class FBoardModel:
    def __init__(self, reg: Registry):
        self.footprints = [FFootprint(ref, r) for ref, r in reg.footprints.items()]
        self.zones = [
            FZone(it["net"], is_rule_area=not it["net"])
            for it in reg.board_items.values()
            if it["kind"] == "zone"
        ]
        self.nets = [{"code": i + 1, "name": n} for i, n in enumerate(("SIG", "GND", "+3V3", "/LOCAL"))]
        self.rules = {
            "netclasses": {
                "Default": {"track_width_mm": 0.3, "via_size_mm": 0.7, "via_drill_mm": 0.35, "nets": []},
                "Power": {"track_width_mm": 0.6, "nets": ["GND", "+3V3"]},
            },
            "copper_layers": ["F.Cu", "B.Cu"],
        }


def fake_overlaps(model: FBoardModel) -> list[dict[str, Any]]:
    out = []
    fps = list(model.footprints)
    for i, a in enumerate(fps):
        for b in fps[i + 1 :]:
            if abs(a.position[0] - b.position[0]) < 4 and abs(a.position[1] - b.position[1]) < 4:
                x, y = a.position
                out.append(
                    {
                        "a": a.reference,
                        "b": b.reference,
                        "bbox_mm": (x - 1, y - 1, x + 1, y + 1),
                        "area_mm2": 4.0,
                    }
                )
    return out


def fake_off_board(model: FBoardModel) -> list[dict[str, Any]]:
    out = []
    for fp in model.footprints:
        x, y = fp.position
        if not (0 <= x <= BOARD_W and 0 <= y <= BOARD_H):
            by = max(0.0, x - BOARD_W, -x, y - BOARD_H, -y)
            out.append(
                {
                    "ref": fp.reference,
                    "kind": "pad",
                    "detail": "1",
                    "bbox_mm": (x - 1, y - 1, x + 1, y + 1),
                    "outside_by_mm": by,
                }
            )
    return out


def fake_ratsnest(model: FBoardModel) -> list[dict[str, Any]]:
    fps = sorted(model.footprints, key=lambda f: f.reference)
    return [
        {
            "net": "SIG",
            "a": {"ref": a.reference, "pad": "2", "x_mm": a.position[0], "y_mm": a.position[1]},
            "b": {"ref": b.reference, "pad": "1", "x_mm": b.position[0], "y_mm": b.position[1]},
        }
        for a, b in zip(fps, fps[1:], strict=False)
    ]


# ---- the backend ---------------------------------------------------------------------------------------------


def drc_with(
    *violations: Violation, unconnected: tuple[Violation, ...] = (), parity: tuple[Violation, ...] = ()
) -> Report:
    return Report(
        kind="drc",
        source="b.kicad_pcb",
        kicad_version="10.0.0",
        violations=tuple(violations),
        unconnected=unconnected,
        schematic_parity=parity,
    )


def net_violation(vtype: str, net: str, severity: str = "error") -> Violation:
    return Violation(
        type=vtype,
        severity=severity,
        description=f"{vtype} on net",
        items=(ViolationItem(uuid="u-1", description=f"Track [{net}] on F.Cu", pos=Position(1.0, 2.0)),),
        sheet=None,
    )


@dataclass
class AuthorBackend(FakeBackend):
    caps: dict[str, dict[str, Any]] = field(default_factory=lambda: {"10.0.0": CAPS_10})
    drc: Report | Exception = field(default_factory=drc_with)
    analysis_missing: bool = False
    ipc_twins: dict[str, Any] = field(default_factory=dict)  # name -> callable(session, **kw) | Exception
    ipc_log: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    released: list[str] = field(default_factory=list)

    def _unsupported(self, what: str) -> KiwrapError:
        return KiwrapError("E_UNSUPPORTED", f"{what} is not available in this build", data={"module": what})

    def export_netlist(
        self, install: Any, config: Any, schematic: Path, out: Path | None = None, **kw: Any
    ) -> Any:
        nl = super().export_netlist(install, config, schematic, out, **kw)
        if out is not None:  # the real export writes the kicadsexpr file the IPC sync twin imports
            Path(out).parent.mkdir(parents=True, exist_ok=True)
            Path(out).write_text("(export (version E))" + chr(10), encoding="utf-8")
        return nl

    def file_read_schematic(self, path: Path) -> FHierarchy:
        return FHierarchy(registry(Path(path).parent))

    def file_read_board(self, path: Path) -> FBoardModel:
        return FBoardModel(registry(Path(path).parent))

    def courtyard_overlaps(self, model: Any) -> list[Any]:
        if self.analysis_missing:
            raise self._unsupported("kiwrap.formats.analysis")
        return fake_overlaps(model)

    def off_board(self, model: Any) -> list[Any]:
        if self.analysis_missing:
            raise self._unsupported("kiwrap.formats.analysis")
        return fake_off_board(model)

    def ratsnest_estimate(self, model: Any) -> list[Any]:
        if self.analysis_missing:
            raise self._unsupported("kiwrap.formats.analysis")
        return fake_ratsnest(model)

    def ipc_session(self, install: Any, file: Path, *, config_root: Any = None) -> Any:
        self._log("ipc_session", file=Path(file).name)
        return {"file": Path(file)}

    def ipc_release_project(self, directory: Path) -> dict[str, Any]:
        self.released.append(Path(directory).name)
        return {"busy": []}

    def twin(self, name: str) -> Any:
        def call(session: Any, **kw: Any) -> Any:
            self.ipc_log.append((name, kw))
            impl = self.ipc_twins[name]
            if isinstance(impl, Exception):
                raise impl
            return impl(session, **kw)

        return call

    def saves(self) -> list[str]:
        return [n for n, _ in self.ipc_log if n.endswith("_save")]

    def services(self, storage_home: Path):
        svc = super().services(storage_home)
        for name in (
            "file_read_schematic",
            "file_read_board",
            "courtyard_overlaps",
            "off_board",
            "ratsnest_estimate",
            "ipc_session",
            "ipc_release_project",
        ):
            setattr(svc, name, getattr(self, name))
        svc.schematic_writer = FakeSchematicWriter  # type: ignore[attr-defined]
        svc.board_writer = FakeBoardWriter  # type: ignore[attr-defined]
        svc.lib_resolver = FakeLibResolver  # type: ignore[attr-defined]
        svc.lib_index = FakeLibIndex  # type: ignore[attr-defined]
        self.ipc_twins.setdefault("ipc_sch_save", lambda session, **kw: session["file"])
        self.ipc_twins.setdefault("ipc_pcb_save", lambda session, **kw: session["file"])
        for name in self.ipc_twins:
            setattr(svc, name, self.twin(name))
        return svc


def ipc_add_symbol(session: Any, **kw: Any) -> FPlacement:
    """An `sch_add_symbol` twin: KiCad placed it and saved the file, so the file reader (and the
    file writer's `pins_of`) see it afterwards - the registry stands in for the file on disk."""
    reg = registry(Path(session["file"]).parent)
    uid = reg.uid("ipc-sym")
    pins = pins_for(kw["lib_id"], kw["at_mm"], kw.get("unit", 1))
    reg.symbols[kw["ref"]] = {
        "uuid": uid,
        "lib_id": kw["lib_id"],
        "unit": kw.get("unit", 1),
        "unit_count": 4 if "Quad" in kw["lib_id"] else 1,
        "footprint": kw.get("footprint") or "",
        "value": kw.get("value") or "",
        "pins": pins,
        "power": False,
    }
    return FPlacement(uid, kw["ref"], pins)


def harness(tmp_path: Path, **knobs: Any) -> tuple[Harness, AuthorBackend, str]:
    REGISTRIES.clear()
    FakeLibIndex.searches.clear()
    FakeLibIndex.filter_calls.clear()
    FakeBoardWriter.unresolved = []
    backend = AuthorBackend(**knobs)
    h = Harness(tmp_path, backend)
    project = h.create()
    return h, backend, project


def with_11(**knobs: Any) -> dict[str, Any]:
    return {"installs": [FakeInstall(version="11.0.0")], "caps": {"11.0.0": CAPS_11}, **knobs}


def reg_of(project: str) -> Registry:
    return registry(Path(project))


def calls(project: str, name: str) -> list[dict[str, Any]]:
    return [kw for n, kw in reg_of(project).calls if n == name]


def stage_of(project: str) -> str:
    return Project.from_path(project).load_state().stage


def add_r(h: Harness, project: str, ref: str, at: list[float], **kw: Any) -> dict[str, Any]:
    return h.ok("kw_sch_add_symbol", project=project, lib_id="Device:R", ref=ref, value="10k", at_mm=at, **kw)


def to_erc_clean(h: Harness, project: str) -> None:
    sc = h.ok("kw_sch_check", project=project)
    assert sc["stage"] == "erc_clean"


def two_resistors() -> Netlist:
    return Netlist(
        source="demo.kicad_sch",
        kicad_version="10.0.0",
        components=(
            Component(
                ref="R1",
                value="10k",
                footprint="Resistor_SMD:R_0603_1608Metric",
                lib_id="Device:R",
                sheet_path="/",
            ),
            Component(
                ref="R2",
                value="10k",
                footprint="Resistor_SMD:R_0603_1608Metric",
                lib_id="Device:R",
                sheet_path="/",
            ),
        ),
        nets=(Net(code=1, name="SIG", nodes=(NetNode("R1", "2"), NetNode("R2", "1"))),),
    )


def to_synced(h: Harness, project: str) -> dict[str, Any]:
    add_r(h, project, "R1", [50, 50])
    add_r(h, project, "R2", [70, 50])
    to_erc_clean(h, project)
    h.backend.net = two_resistors()  # type: ignore[attr-defined]
    sc = h.ok("kw_pcb_sync", project=project)
    assert sc["stage"] == "synced", sc
    return sc


# ---- catalog / planner / help --------------------------------------------------------------------------------


def test_catalog_lists_the_authoring_tools_in_stage_order_with_schemas(tmp_path: Path):
    h, _, _ = harness(tmp_path)
    assert {t.name for t in AUTHORING_TOOLS} == set(AUTHORING_NAMES)
    names = list(TOOL_NAMES)
    assert names.index("kw_sch_add_symbol") < names.index("kw_sch_get") < names.index("kw_sch_check")
    assert (
        names.index("kw_pcb_setup")
        < names.index("kw_pcb_sync")
        < names.index("kw_pcb_place")
        < names.index("kw_pcb_get")
    )
    assert names.index("kw_sch_assign_footprints") < names.index("kw_pcb_setup")
    assert [t.name for t in ALL_TOOLS] == names
    tools = {t.name: t for t in anyio.run(h.server.list_tools)}
    assert [t for t in tools] == names  # deterministic, same order as the catalog
    for name in AUTHORING_NAMES:
        t = tools[name]
        assert "project" in t.input_schema["required"], name
        assert t.output_schema and "validators" in t.output_schema["properties"], name
        assert t.annotations is not None and t.annotations.read_only_hint is False, name
        assert t.meta and t.meta.get("anthropic/maxResultSizeChars"), name
    add = tools["kw_sch_add_symbol"].input_schema
    assert set(add["required"]) == {"project", "lib_id", "at_mm"}
    assert add["properties"]["rotation"]["enum"] == [0, 90, 180, 270]
    assert add["properties"]["provider"]["enum"] == ["auto", "ipc11", "file"]
    connect = tools["kw_sch_connect"].input_schema
    assert connect["properties"]["style"]["enum"] == ["wire", "label", "global_label", "power"]
    assert tools["kw_sch_edit"].annotations.destructive_hint is True
    assert tools["kw_pcb_sync"].annotations.destructive_hint is True
    assert tools["kw_pcb_route"].input_schema["required"] == ["project", "ops"]
    # help: a page per tool; the overview names the full flow
    for name in AUTHORING_NAMES:
        assert help_tool.page(name)[0].startswith(f"# {name}")
    md = h.ok("kw_help")["data"]["markdown"]
    assert "kw_sch_add_symbol" in md and "kw_pcb_sync" in md and "kw_pcb_route" in md
    assert "if_match" in h.ok("kw_help", topic="E_EXTERNAL_EDIT")["data"]["markdown"]


def test_planner_authoring_operations_are_ipc11_then_file():
    from tests.test_core_fakes import FakeCaps

    for op in ("schematic.write", "board.sync", "board.place", "board.zones", "board.route_primitives"):
        assert planner.operation(op).names() == ["ipc11", "file"], op
    assert planner.operation("board.setup").names() == ["file"]
    c10 = FakeCaps(CAPS_10, FakeInstall())
    c11 = FakeCaps(CAPS_11, FakeInstall(version="11.0.0"))
    assert [c.name for c in planner.plan("schematic.write", c10).candidates] == ["file"]
    assert [c.name for c in planner.plan("schematic.write", c11).candidates] == ["ipc11", "file"]
    assert [c.name for c in planner.plan("board.place", c11).candidates] == ["ipc11", "file"]
    described = planner.describe()
    assert "G13" in described["schematic.write"]["providers"][0]["note"]
    # the guard is a capability decision, never a version one
    assert g13_guard(c11, multi_unit=True) and g13_guard(c11, multi_unit=False) is None
    assert g13_guard(FakeCaps(CAPS_11_NO_G13, FakeInstall(version="11.0.0")), multi_unit=True) is None
    assert epoch_for(c10) == {"sch": 20260306, "sym": 20251024, "pcb": 20260206, "pro": 3}


def test_wire_routing_shapes():
    a, b = {"x_mm": 0.0, "y_mm": 0.0}, {"x_mm": 10.16, "y_mm": 5.08}
    assert wire_points(a, b, "hv") == [(0.0, 0.0), (10.16, 0.0), (10.16, 5.08)]
    assert wire_points(a, b, "vh") == [(0.0, 0.0), (0.0, 5.08), (10.16, 5.08)]
    assert wire_points(a, b, "direct") == [(0.0, 0.0), (10.16, 5.08)]
    assert wire_points(a, {"x_mm": 0.0, "y_mm": 7.62}, "hv") == [(0.0, 0.0), (0.0, 7.62)]


# ---- kw_sch_add_symbol --------------------------------------------------------------------------------------


def test_add_symbol_returns_absolute_pins_runs_erc_and_moves_the_stage(tmp_path: Path):
    h, backend, project = harness(tmp_path)
    sc = add_r(h, project, "R1", [50.0, 50.5])  # snapped to the 1.27 mm grid
    d = sc["data"]
    assert d["ref"] == "R1" and d["uuid"].startswith("sym-") and d["at_mm"] == [49.53, 50.8]
    assert [p["number"] for p in d["pins"]] == ["1", "2"]
    assert d["pins"][0] == {"number": "1", "name": "~", "x_mm": 49.53, "y_mm": 46.99, "type": "passive"}
    assert sc["changed"]["added"] == ["R1"] and "demo.kicad_sch" in sc["changed"]["updated"]
    assert sc["provider_used"] == "file" and sc["degraded"] == []
    assert sc["validators"] == [{"name": "erc", "before": None, "after": 0, "detail": {"warnings": 0}}]
    assert d["erc"]["ok"] is True and sc["stage"] == "erc_clean" and sc["next_suggested"] == "kw_sch_stats"
    assert sc["snapshot"] and d["plan"]["candidates"] == ["file"]
    assert list(d["sheets"]) == ["demo.kicad_sch"] and len(d["sheets"]["demo.kicad_sch"]) == 64
    # the writer was built with the target's epochs and a resolver for the install
    kw = reg_of(project).writer_kwargs[0]
    assert kw["epoch"] == {"sch": 20260306, "sym": 20251024, "pcb": 20260206, "pro": 3}
    assert isinstance(kw["lib_resolver"], FakeLibResolver) and kw["lib_resolver"].install.version == "10.0.0"
    assert calls(project, "add_symbol")[0]["at_mm"] == (49.53, 50.8)
    # ERC ran on the root sheet after the save
    assert [c[0] for c in backend.calls if c[0] in ("run_erc",)] == ["run_erc"]
    # a second write is NOT an external edit (the stage hashes were re-recorded)
    sc2 = add_r(h, project, "R2", [70, 50])
    assert "external_edit" not in sc2["degraded"] and not any("external_edit" in w for w in sc2["warnings"])
    assert sc2["validators"][0]["before"] == 0


def test_add_symbol_erc_errors_keep_sch_written_and_list_violations(tmp_path: Path):
    h, backend, project = harness(tmp_path)
    backend.erc = erc_report(violation("pin_not_connected"), violation("lib_symbol_issues", "warning"))
    sc = add_r(h, project, "R1", [50, 50])
    assert sc["stage"] == "sch_written" and sc["next_suggested"] == "kw_sch_check"
    erc = sc["data"]["erc"]
    assert erc["ok"] is False and erc["errors"] == 1 and erc["warnings"] == 1
    assert erc["violations"][0]["type"] == "pin_not_connected" and erc["violations"][0]["pos"] == {
        "x_mm": 10.0,
        "y_mm": 20.0,
    }
    assert sc["validators"][0]["after"] == 1
    # erc=false: no validator, stage sch_written, degraded says so
    sc = add_r(h, project, "R2", [60, 50], erc=False)
    assert sc["validators"] == [] and sc["degraded"] == ["erc_skipped"] and sc["data"]["erc"] is None
    assert sc["stage"] == "sch_written" and sc["next_suggested"] == "kw_sch_check"
    assert [c[0] for c in backend.calls if c[0] == "run_erc"] == ["run_erc"]


def test_add_symbol_without_erc_capability_degrades(tmp_path: Path):
    h, _, project = harness(tmp_path, caps={"10.0.0": {**CAPS_10, "cli.has_erc": False}})
    sc = add_r(h, project, "R1", [50, 50])
    assert sc["degraded"] == ["erc_unavailable"] and sc["stage"] == "sch_written"
    assert sc["next_suggested"] is None  # kw_sch_check is withheld on a target without ERC


def test_add_symbol_schematic_write_invalidates_sync(tmp_path: Path):
    h, _, project = harness(tmp_path)
    to_synced(h, project)
    sc = add_r(h, project, "R3", [90, 50])
    assert sc["stage"] == "erc_clean"  # the board must be re-synced
    state = Project.from_path(project).load_state()
    assert "synced" not in state.hashes and "erc_clean" in state.hashes


def test_missing_writer_component_is_a_teaching_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from kiwrap.core.tools import _author

    h, _, project = harness(tmp_path)
    h.ctx.services.schematic_writer = BrokenWriter  # type: ignore[attr-defined]
    sc = h.fail("kw_sch_add_symbol", "E_UNSUPPORTED", project=project, lib_id="Device:R", at_mm=[1, 2])
    assert "not implemented" in sc["message"] and "kiwrap.formats.schematic_writer" in sc["fix"]
    # the contract module is absent from this build: the lazy import is the same teaching error
    h.ctx.services.schematic_writer = None  # type: ignore[attr-defined]
    monkeypatch.setitem(
        _author.AUTHOR_BACKENDS,
        "schematic_writer",
        ("kiwrap.formats.no_such_writer", "SchematicWriter", "the schematic writer"),
    )
    sc = h.fail("kw_sch_add_symbol", "E_UNSUPPORTED", project=project, lib_id="Device:R", at_mm=[1, 2])
    assert "kiwrap.formats.no_such_writer" in sc["fix"] and sc["data"]["plan"]["candidates"] == ["file"]


def test_add_symbol_bad_args(tmp_path: Path):
    h, _, project = harness(tmp_path)
    h.fail("kw_sch_add_symbol", "E_BAD_ARGS", project=project, lib_id="Device:R", at_mm=[1])
    h.fail("kw_sch_add_symbol", "E_BAD_ARGS", project=project, lib_id="Device:R", at_mm=[1, 2], rotation=45)
    h.fail(
        "kw_sch_add_symbol",
        "E_PROJECT_NOT_FOUND",
        project=str(tmp_path / "nope"),
        lib_id="Device:R",
        at_mm=[1, 2],
    )


# ---- ipc11 provider + G13 guard ----------------------------------------------------------------------------


def test_ipc_twin_answers_on_kicad11_saves_and_the_server_is_released(tmp_path: Path):
    h, backend, project = harness(tmp_path, **with_11(ipc_twins={"ipc_sch_add_symbol": ipc_add_symbol}))
    sc = add_r(h, project, "R1", [50, 50])
    assert sc["provider_used"] == "ipc11:11.0.0" and sc["data"]["uuid"] == "ipc-sym-0001"
    assert sc["data"]["pins"][1]["y_mm"] == 53.34
    assert [n for n, _ in backend.ipc_log] == ["ipc_sch_add_symbol", "ipc_sch_save"]  # KiCad writes the file
    # the twins address sheet INSTANCES by their human-readable path in a session of the root
    assert backend.ipc_log[0][1]["lib_id"] == "Device:R" and backend.ipc_log[0][1]["sheet"] == "/"
    assert backend.released and backend.released[-1] == "demo"  # PROCESS HYGIENE: closed before ERC
    assert sc["data"]["plan"]["candidates"] == ["ipc11", "file"]
    assert calls(project, "add_symbol") == []  # the file writer was not used
    # a twin that blows up (a wedged server, a bare session) falls through to the file writer
    backend.ipc_twins["ipc_sch_add_symbol"] = AttributeError("no attribute 'document'")
    sc = add_r(h, project, "R2", [60, 50])
    assert sc["provider_used"] == "file" and sc["degraded"] == ["ipc11_unavailable"]
    assert any("E_KICAD_FAILED" in w for w in sc["warnings"])


def test_ipc_twin_unavailable_falls_through_to_file(tmp_path: Path):
    err = KiwrapError(
        "E_UNSUPPORTED", "PlaceSymbolFromLibrary unhandled", data={"capability": "ipc.headless"}
    )
    h, _, project = harness(tmp_path, **with_11(ipc_twins={"ipc_sch_add_symbol": err}))
    sc = add_r(h, project, "R1", [50, 50])
    assert sc["provider_used"] == "file" and sc["degraded"] == ["ipc11_unavailable"]
    assert any("ipc11 unavailable" in w for w in sc["warnings"])
    # no twin bound at all: the real twin meets the fake session and fails -> file
    (tmp_path / "b").mkdir()
    h2, _, project2 = harness(tmp_path / "b", **with_11())
    sc = add_r(h2, project2, "R1", [50, 50])
    assert sc["provider_used"] == "file" and sc["degraded"] == ["ipc11_unavailable"]


def test_g13_guard_keeps_multi_unit_writes_off_ipc(tmp_path: Path):
    twin = ipc_add_symbol
    h, backend, project = harness(
        tmp_path, **with_11(ipc_twins={"ipc_sch_add_symbol": twin, "ipc_sch_edit": lambda s, **kw: None})
    )
    # unit 1: ipc11 is fine
    sc = h.ok("kw_sch_add_symbol", project=project, lib_id="Amplifier:Quad", ref="U1", at_mm=[50, 50], unit=1)
    assert sc["provider_used"] == "ipc11:11.0.0"
    # unit 2 of a multi-unit symbol: guarded -> file, and the receipt says why
    sc = h.ok("kw_sch_add_symbol", project=project, lib_id="Amplifier:Quad", ref="U1", at_mm=[80, 50], unit=2)
    assert sc["provider_used"] == "file" and sc["degraded"] == ["ipc11_guarded"]
    assert any("G13" in w for w in sc["warnings"])
    assert [n for n, _ in backend.ipc_log] == ["ipc_sch_add_symbol", "ipc_sch_save"]  # not attempted
    # editing that multi-unit symbol is guarded too; editing a single-unit one is not
    add_r(h, project, "R1", [10, 10])
    sc = h.ok("kw_sch_edit", project=project, ops=[{"op": "value", "ref": "U1", "value": "LM324"}])
    assert sc["provider_used"] == "file" and sc["degraded"] == ["ipc11_guarded"]
    sc = h.ok("kw_sch_edit", project=project, ops=[{"op": "value", "ref": "R1", "value": "4k7"}])
    assert sc["provider_used"] == "ipc11:11.0.0"
    # on a build without the hang the second unit goes over IPC
    (tmp_path / "ok").mkdir()
    h2, backend2, project2 = harness(
        tmp_path / "ok",
        installs=[FakeInstall(version="11.0.0")],
        caps={"11.0.0": CAPS_11_NO_G13},
        ipc_twins={"ipc_sch_add_symbol": twin},
    )
    sc = h2.ok(
        "kw_sch_add_symbol", project=project2, lib_id="Amplifier:Quad", ref="U1", at_mm=[80, 50], unit=2
    )
    assert sc["provider_used"] == "ipc11:11.0.0" and sc["degraded"] == []


def test_forced_provider_that_is_unavailable_is_named(tmp_path: Path):
    h, _, project = harness(tmp_path)
    sc = add_r(h, project, "R1", [50, 50], provider="ipc11")
    assert sc["provider_used"] == "file" and sc["degraded"] == ["ipc11_unavailable"]
    assert any("provider 'ipc11' was requested" in w for w in sc["warnings"])


# ---- kw_sch_connect ------------------------------------------------------------------------------------------


def test_connect_by_ref_pin_draws_l_shaped_wires_from_the_writer_pins(tmp_path: Path):
    h, _, project = harness(tmp_path)
    add_r(h, project, "R1", [50.8, 50.8])
    add_r(h, project, "R2", [76.2, 63.5])
    sc = h.ok(
        "kw_sch_connect",
        project=project,
        endpoints=[{"ref": "R1", "pin": "2"}, {"ref": "R2", "pin": "1"}],
        net="SIG",
    )
    d = sc["data"]
    assert d["endpoints"] == [
        {"ref": "R1", "pin": "2", "x_mm": 50.8, "y_mm": 54.61, "given": "pin"},
        {"ref": "R2", "pin": "1", "x_mm": 76.2, "y_mm": 59.69, "given": "pin"},
    ]
    wires = calls(project, "add_wire")
    assert wires == [{"points_mm": [(50.8, 54.61), (76.2, 54.61), (76.2, 59.69)], "sheet": ""}]
    assert len(d["wires"]) == 2 and d["labels"] and d["power"] == []
    assert calls(project, "add_label") == [
        {"text": "SIG", "at_mm": (50.8, 54.61), "kind": "local", "rotation": 0, "sheet": ""}
    ]
    assert sc["changed"]["added"] == [*d["wires"], *d["labels"]]
    assert sc["stage"] == "erc_clean" and sc["validators"][0]["name"] == "erc"
    # a corner given as at_mm, a pin given by name, route vh
    sc = h.ok(
        "kw_sch_connect",
        project=project,
        endpoints=[{"ref": "R1", "pin": "1"}, {"at_mm": [30.0, 20.0]}, {"ref": "R2", "pin": "2"}],
        route="vh",
    )
    assert sc["data"]["endpoints"][1] == {
        "ref": None,
        "pin": None,
        "x_mm": 30.48,
        "y_mm": 20.32,
        "given": "at_mm",
    }
    assert len(calls(project, "add_wire")) == 3


def test_connect_refuses_a_route_through_another_pin(tmp_path: Path):
    """R1 at (50.8, 50.8) (pins at y +-3.81), C1 at (63.5, 63.5): the hv route from R1.2 to C1.2
    corners at (63.5, 54.61) and runs down through C1.1 at (63.5, 59.69) - the writer would split
    the wire there and short C1. Refused before any write, naming the pin; vh is clean."""
    h, _, project = harness(tmp_path)
    add_r(h, project, "R1", [50.8, 50.8])
    add_r(h, project, "C1", [63.5, 63.5])
    sc = h.fail(
        "kw_sch_connect",
        "E_BAD_ARGS",
        project=project,
        endpoints=[{"ref": "R1", "pin": "2"}, {"ref": "C1", "pin": "2"}],
    )
    assert "C1 pin 1" in sc["message"] and "route='vh'" in sc["fix"]
    assert sc["data"]["connected_pins"] == [
        {"ref": "C1", "pin": "1", "x_mm": 63.5, "y_mm": 59.69, "endpoint": False}
    ]
    assert calls(project, "add_wire") == []
    sc = h.ok(
        "kw_sch_connect",
        project=project,
        endpoints=[{"ref": "R1", "pin": "2"}, {"ref": "C1", "pin": "2"}],
        route="vh",
    )
    assert sc["data"]["connected_pins"] == [] and len(calls(project, "add_wire")) == 1
    # a corner given as at_mm that lands on a pin is deliberate: reported, not refused
    sc = h.ok(
        "kw_sch_connect",
        project=project,
        endpoints=[{"ref": "R1", "pin": "1"}, {"at_mm": [63.5, 59.69]}],
        route="direct",
    )
    assert sc["data"]["connected_pins"] == [
        {"ref": "C1", "pin": "1", "x_mm": 63.5, "y_mm": 59.69, "endpoint": True}
    ]
    assert any("C1 pin 1" in w for w in sc["warnings"])


def test_connect_reports_wires_junctions_and_split_halves_apart(tmp_path: Path):
    """The ipc11 twin returns a `WireIds` list (new wires) with `junctions` / `split_wires`
    attributes: the receipt keeps the kinds apart so `wires` holds only wire ids the agent can
    delete, and every id lands in `changed.added`."""
    from kiwrap.backends.ipc.sch_writes import WireIds

    h, _, project = harness(tmp_path)
    add_r(h, project, "R1", [50.8, 50.8])
    add_r(h, project, "R2", [76.2, 63.5])
    reg = reg_of(project)
    original = FakeSchematicWriter.add_wire

    def add_wire_like_the_twin(self, points_mm, sheet=""):
        ids = original(self, points_mm, sheet)
        return WireIds(ids, junctions=[reg.uid("junction")], split_wires=[reg.uid("half"), reg.uid("half")])

    FakeSchematicWriter.add_wire = add_wire_like_the_twin  # type: ignore[method-assign]
    try:
        sc = h.ok(
            "kw_sch_connect",
            project=project,
            endpoints=[{"ref": "R1", "pin": "2"}, {"ref": "R2", "pin": "1"}],
        )
    finally:
        FakeSchematicWriter.add_wire = original  # type: ignore[method-assign]
    d = sc["data"]
    assert len(d["wires"]) == 2 and all(i.startswith("wire-") for i in d["wires"])
    assert len(d["junctions"]) == 1 and d["junctions"][0].startswith("junction-")
    assert len(d["split_wires"]) == 2 and all(i.startswith("half-") for i in d["split_wires"])
    assert set(sc["changed"]["added"]) == {*d["wires"], *d["junctions"], *d["split_wires"]}
    ids = WireIds(["w1"], junctions=["j1"], split_wires=["h1", "h2"])
    assert list(ids) == ["w1"] and ids.all_ids == ["w1", "j1", "h1", "h2"]


def test_connect_styles_label_global_power(tmp_path: Path):
    h, _, project = harness(tmp_path)
    add_r(h, project, "R1", [50.8, 50.8])
    add_r(h, project, "R2", [76.2, 63.5])
    ends = [{"ref": "R1", "pin": "1"}, {"ref": "R2", "pin": "1"}]
    sc = h.ok("kw_sch_connect", project=project, endpoints=ends, style="global_label", net="SDA")
    assert [c["kind"] for c in calls(project, "add_label")] == ["global", "global"] and len(
        sc["data"]["labels"]
    ) == 2
    sc = h.ok("kw_sch_connect", project=project, endpoints=ends, style="label", net="SDA")
    assert [c["kind"] for c in calls(project, "add_label")][-2:] == ["local", "local"]
    sc = h.ok(
        "kw_sch_connect", project=project, endpoints=[{"ref": "R2", "pin": "2"}], style="power", net="GND"
    )
    assert calls(project, "add_power") == [
        {"net": "GND", "at_mm": (76.2, 67.31), "rotation": 0, "sheet": "", "flag": False}
    ]
    assert sc["data"]["power"][0]["ref"].startswith("#PWR") and sc["changed"]["added"][0].startswith("#PWR")


def test_connect_errors_teach(tmp_path: Path):
    h, _, project = harness(tmp_path)
    add_r(h, project, "R1", [50.8, 50.8])
    sc = h.fail(
        "kw_sch_connect",
        "E_BAD_ARGS",
        project=project,
        endpoints=[{"ref": "R1", "pin": "3"}, {"ref": "R1", "pin": "1"}],
    )
    assert sc["candidates"] == ["1 (~)", "2 (~)"]
    sc = h.fail(
        "kw_sch_connect",
        "E_BAD_ARGS",
        project=project,
        endpoints=[{"ref": "R9", "pin": "1"}, {"ref": "R1", "pin": "1"}],
    )
    assert "R9" in sc["message"]
    h.fail(
        "kw_sch_connect", "E_BAD_ARGS", project=project, endpoints=[{"ref": "R1", "pin": "1"}], style="label"
    )
    h.fail("kw_sch_connect", "E_BAD_ARGS", project=project, endpoints=[{"ref": "R1", "pin": "1"}])
    h.fail(
        "kw_sch_connect", "E_BAD_ARGS", project=project, endpoints=[{"ref": "R1"}, {"ref": "R1", "pin": "1"}]
    )
    assert calls(project, "add_wire") == []  # nothing was written by the failed calls


# ---- kw_sch_add_item ---------------------------------------------------------------------------------------


def test_add_item_sheet_refuses_files_outside_the_project(tmp_path: Path):
    h, _, project = harness(tmp_path)
    for bad in ("../escaped", "C:/x/y", "a/b", ".kicad_sch"):
        sc = h.fail(
            "kw_sch_add_item",
            "E_BAD_ARGS",
            project=project,
            item={"type": "sheet", "text": "evil", "file": bad, "at_mm": [50, 50], "size_mm": [20, 20]},
        )
        assert "bare file name" in sc["message"], bad
    assert calls(project, "add_sheet") == []
    assert not (tmp_path / "escaped.kicad_sch").exists()


def test_add_item_power_flag_no_connect_labels_sheet(tmp_path: Path):
    h, _, project = harness(tmp_path)
    add_r(h, project, "C1", [50.8, 50.8])
    sc = h.ok(
        "kw_sch_add_item", project=project, item={"type": "power", "net": "GND", "ref": "C1", "pin": "2"}
    )
    assert calls(project, "add_power")[-1] == {
        "net": "GND",
        "at_mm": (50.8, 54.61),
        "rotation": 0,
        "sheet": "",
        "flag": False,
    }
    assert sc["data"]["ref"].startswith("#PWR") and sc["data"]["pins"][0]["x_mm"] == 50.8
    sc = h.ok("kw_sch_add_item", project=project, item={"type": "pwr_flag", "net": "+3V3", "at_mm": [30, 30]})
    assert calls(project, "add_power")[-1]["flag"] is True and sc["data"]["ref"].startswith("#FLG")
    sc = h.ok("kw_sch_add_item", project=project, item={"type": "no_connect", "ref": "C1", "pin": "1"})
    assert calls(project, "add_no_connect") == [{"at_mm": (50.8, 46.99), "sheet": ""}] and sc["data"][
        "uuid"
    ].startswith("nc-")
    sc = h.ok(
        "kw_sch_add_item",
        project=project,
        item={"type": "hierarchical_label", "text": "EN", "at_mm": [10, 10], "rotation": 180},
    )
    assert calls(project, "add_label")[-1] == {
        "text": "EN",
        "at_mm": (10.16, 10.16),
        "kind": "hierarchical",
        "rotation": 180,
        "sheet": "",
    }
    sc = h.ok(
        "kw_sch_add_item",
        project=project,
        item={
            "type": "sheet",
            "text": "power",
            "file": "power.kicad_sch",
            "at_mm": [100, 100],
            "size_mm": [40, 30],
            "pins": [{"name": "VIN", "side": "left", "kind": "input"}],
        },
    )
    assert calls(project, "add_sheet")[0]["pins"] == [("VIN", "left", "input")]
    # the receipt carries the sheet pins with ABSOLUTE positions (what the agent wires to)
    assert sc["data"]["file"] == "power.kicad_sch" and len(sc["data"]["pins"]) == 1
    pin = sc["data"]["pins"][0]
    assert (pin["name"], pin["side"], pin["kind"]) == ("VIN", "left", "input")
    assert (pin["x_mm"], pin["y_mm"]) == (100.33, 102.87) and pin["uuid"]  # at_mm snapped to 1.27
    assert "power.kicad_sch" in sc["changed"]["updated"] and "power.kicad_sch" in sc["data"]["sheets"]
    # sheet_pin / text are optional writer methods: absent here -> E_UNSUPPORTED with the alternative
    sc = h.fail(
        "kw_sch_add_item",
        "E_UNSUPPORTED",
        project=project,
        item={"type": "sheet_pin", "sheet_uuid": "x", "text": "EN"},
    )
    assert "pins" in sc["fix"]
    h.fail(
        "kw_sch_add_item", "E_BAD_ARGS", project=project, item={"type": "power", "at_mm": [1, 2]}
    )  # no net
    h.fail(
        "kw_sch_add_item", "E_BAD_ARGS", project=project, item={"type": "label", "at_mm": [1, 2]}
    )  # no text
    h.fail(
        "kw_sch_add_item", "E_BAD_ARGS", project=project, item={"type": "power", "net": "GND"}
    )  # no position


# ---- kw_sch_edit -----------------------------------------------------------------------------------------------


def test_edit_resolves_refs_applies_ops_and_honours_if_match(tmp_path: Path):
    h, _, project = harness(tmp_path)
    sc = add_r(h, project, "R1", [50.8, 50.8])
    add_r(h, project, "R2", [70, 70])
    uuid_r1 = sc["data"]["uuid"]
    sheets = h.ok(
        "kw_project", action="status", project=project
    )  # any receipt works; use the last write's hash
    sha = add_r(h, project, "R3", [90, 70])["data"]["sheets"]["demo.kicad_sch"]
    assert sheets["data"]
    sc = h.ok(
        "kw_sch_edit",
        project=project,
        if_match=sha[:16],
        ops=[
            {"op": "move", "ref": "R1", "at_mm": [60.0, 60.0]},
            {"op": "rotate", "ref": "R1", "rotation": 90},
            {"op": "property", "ref": "R2", "name": "Datasheet", "value": "http://x"},
            {"op": "footprint", "ref": "R2", "value": "Resistor_SMD:R_0603_1608Metric"},
            {"op": "dnp", "ref": "R2", "dnp": True},
            {"op": "delete", "ref": "R3"},
        ],
    )
    edits = calls(project, "edit")
    assert edits[0] == {"uuid": uuid_r1, "changes": {"at_mm": (59.69, 59.69)}}
    assert edits[1] == {"uuid": uuid_r1, "changes": {"rotation": 90}}
    assert edits[2]["changes"] == {"fields": {"Datasheet": "http://x"}}
    assert edits[3]["changes"] == {"footprint": "Resistor_SMD:R_0603_1608Metric"} and edits[4]["changes"] == {
        "dnp": True
    }
    assert calls(project, "delete") == [{"uuid": "sym-0003"}]
    assert sc["changed"] == {"added": [], "updated": ["R1", "R2", "demo.kicad_sch"], "removed": ["R3"]}
    assert [a["op"] for a in sc["data"]["applied"]] == [
        "move",
        "rotate",
        "property",
        "footprint",
        "dnp",
        "delete",
    ]
    assert sc["stage"] == "erc_clean"
    # the sheet changed: the old hash is refused
    sc = h.fail(
        "kw_sch_edit",
        "E_EXTERNAL_EDIT",
        project=project,
        if_match=sha,
        ops=[{"op": "value", "ref": "R1", "value": "1k"}],
    )
    assert sc["data"]["sheet"] == "demo.kicad_sch" and "kw_sch_get" in sc["fix"]
    # unknown ref -> candidates; missing fields
    sc = h.fail(
        "kw_sch_edit", "E_BAD_ARGS", project=project, ops=[{"op": "move", "ref": "R9", "at_mm": [1, 1]}]
    )
    assert sc["candidates"] == ["R1", "R2"]
    h.fail("kw_sch_edit", "E_BAD_ARGS", project=project, ops=[{"op": "move", "at_mm": [1, 1]}])
    h.fail("kw_sch_edit", "E_BAD_ARGS", project=project, ops=[{"op": "rotate", "ref": "R1"}])
    h.fail("kw_sch_edit", "E_BAD_ARGS", project=project, ops=[{"op": "property", "ref": "R1", "name": "X"}])
    # by uuid (any item)
    sc = h.ok("kw_sch_edit", project=project, ops=[{"op": "delete", "uuid": "wire-77"}])
    assert sc["changed"]["removed"] == ["wire-77"]


# ---- kw_sch_annotate / kw_sch_assign_footprints ------------------------------------------------------------


def test_annotate_and_assign_footprints_with_rules_and_suggestions(tmp_path: Path):
    h, _, project = harness(tmp_path)
    h.ok("kw_sch_add_symbol", project=project, lib_id="Device:R", at_mm=[50, 50])
    h.ok("kw_sch_add_symbol", project=project, lib_id="Device:C", at_mm=[70, 50])
    h.ok("kw_sch_add_symbol", project=project, lib_id="Device:R", ref="R7", at_mm=[90, 50])
    sc = h.ok("kw_sch_annotate", project=project, scheme="global")
    assert sc["data"]["mapping"] == {"C?": "C1", "R?": "R2"} and sc["data"]["renamed"] == 2
    assert calls(project, "annotate") == [{"scheme": "global", "start_per_sheet": False}]
    assert sc["changed"]["updated"][:2] == ["C1", "R2"] and sc["next_suggested"] == "kw_sch_assign_footprints"
    # rules assign the resistors; the capacitor stays unresolved with (no) suggestions from its filter
    sc = h.ok(
        "kw_sch_assign_footprints",
        project=project,
        rules=[{"ref_prefix": "R", "fpid": "Resistor_SMD:R_0603_1608Metric"}],
    )
    d = sc["data"]
    assert d["assigned"] == {"R2": "Resistor_SMD:R_0603_1608Metric", "R7": "Resistor_SMD:R_0603_1608Metric"}
    assert d["ok"] is False and [u["ref"] for u in d["unresolved"]] == ["C1"]
    assert d["unresolved"][0]["suggestions"] == [] and sc["next_suggested"] == "kw_sch_assign_footprints"
    # the capacitor's filter went to footprints_for (no hit), then the free-text fallback
    assert FakeLibIndex.filter_calls[-1] == (("C_*",), None, 3)
    assert FakeLibIndex.searches[-1] == ("footprint", "C_", {}, 3)
    # explicit mapping resolves it; suggestions for a resistor would have come from the R_* filter
    sc = h.ok("kw_sch_assign_footprints", project=project, mapping={"C1": "Capacitor_SMD:C_0603_1608Metric"})
    assert (
        sc["data"]["ok"] is True and sc["data"]["unresolved"] == [] and sc["next_suggested"] == "kw_pcb_sync"
    )
    assert sc["changed"]["updated"] == ["C1", "demo.kicad_sch"]
    sc = h.fail("kw_sch_assign_footprints", "E_BAD_ARGS", project=project, mapping={"R99": "x:y"})
    assert "R99" in sc["message"] and sc["candidates"] == ["C1", "R2", "R7"]
    h.fail("kw_sch_assign_footprints", "E_BAD_ARGS", project=project)


def test_assign_footprints_rejects_fpids_the_tables_cannot_resolve(tmp_path: Path):
    """An fpid the fp-lib-table cannot resolve is not written (it would only fail later at
    kw_pcb_sync): ok=false, unresolved names the requested id, the reason and same-library
    candidates; allow_unknown=true writes it anyway (project-local libraries not on the table)."""
    h, _, project = harness(tmp_path)
    add_r(h, project, "R1", [50.8, 50.8])
    add_r(h, project, "R2", [76.2, 50.8])
    sc = h.ok(
        "kw_sch_assign_footprints",
        project=project,
        mapping={"R1": "Resistor_SMD:R_0603", "R2": "Resistor_SMD:R_0603_1608Metric"},
        overwrite=True,
    )
    d = sc["data"]
    assert d["ok"] is False and d["assigned"] == {"R2": "Resistor_SMD:R_0603_1608Metric"}
    assert d["rejected"] == {"R1": "Resistor_SMD:R_0603"}
    (u,) = d["unresolved"]
    assert (
        u["ref"] == "R1"
        and u["requested_fpid"] == "Resistor_SMD:R_0603"
        and "not in Resistor_SMD" in u["reason"]
    )
    assert u["suggestions"][0]["fpid"] == "Resistor_SMD:R_0603_1608Metric"
    assert calls(project, "assign_footprints") == [{"mapping": {"R2": "Resistor_SMD:R_0603_1608Metric"}}]
    assert (
        any("NOT written" in w for w in sc["warnings"]) and sc["next_suggested"] == "kw_sch_assign_footprints"
    )
    # an unknown nickname: no candidates, still rejected
    sc = h.ok("kw_sch_assign_footprints", project=project, mapping={"R1": "Nope:R_0603"}, overwrite=True)
    assert (
        sc["data"]["rejected"] == {"R1": "Nope:R_0603"}
        and "nickname" in sc["data"]["unresolved"][0]["reason"]
    )
    # the escape hatch for a library not yet on the table
    sc = h.ok(
        "kw_sch_assign_footprints",
        project=project,
        mapping={"R1": "Local:R_custom"},
        overwrite=True,
        allow_unknown=True,
    )
    assert (
        sc["data"]["ok"] is True
        and sc["data"]["assigned"] == {"R1": "Local:R_custom"}
        and sc["data"]["rejected"] == {}
    )


def test_assign_footprint_suggestions_come_from_the_index(tmp_path: Path):
    h, _, project = harness(tmp_path)
    h.ok("kw_sch_add_symbol", project=project, lib_id="Device:R", ref="R1", at_mm=[50, 50])
    h.ok("kw_sch_add_symbol", project=project, lib_id="Device:Bogus", ref="U1", at_mm=[50, 50])
    sc = h.ok("kw_sch_assign_footprints", project=project, mapping={"U1": "Package:X"})
    unresolved = sc["data"]["unresolved"]
    assert (
        unresolved[0]["ref"] == "R1"
        and unresolved[0]["suggestions"][0]["fpid"] == "Resistor_SMD:R_0603_1608Metric"
    )
    assert unresolved[0]["suggestions"][0]["filter"] == "R_*"
    assert (
        unresolved[0]["suggestions"][0]["pad_count"] == 2 and unresolved[0]["suggestions"][0]["score"] == 1.0
    )
    # a symbol the resolver cannot load is reported, never fatal
    sc = h.ok(
        "kw_sch_assign_footprints",
        project=project,
        rules=[{"lib_id": "Device:Bogus", "fpid": "P:Q"}],
        overwrite=True,
    )
    assert sc["data"]["assigned"] == {"U1": "P:Q"}


# ---- kw_pcb_setup --------------------------------------------------------------------------------------------


def test_pcb_setup_writes_rules_netclasses_outline_and_summarises_drc(tmp_path: Path):
    h, backend, project = harness(tmp_path)
    sc = h.ok(
        "kw_pcb_setup",
        project=project,
        layers=4,
        rules={"clearance_mm": 0.2, "track_width_mm": 0.25},
        netclasses={"Power": {"nets": ["GND", "+3V3"], "track_width_mm": 0.5}},
        outline={"w": 50, "h": 30},
        custom_rules="(version 1)\n",
    )
    assert calls(project, "setup") == [
        {
            "layers": 4,
            "rules": {"clearance_mm": 0.2, "track_width_mm": 0.25},
            "netclasses": {"Power": {"nets": ["GND", "+3V3"], "track_width_mm": 0.5}},
            "outline": {"w": 50, "h": 30},
            "custom_rules": "(version 1)\n",
        }
    ]
    assert set(sc["changed"]["updated"]) == {"demo.kicad_pcb", "demo.kicad_pro"} and sc["changed"][
        "added"
    ] == ["demo.kicad_dru"]
    assert sc["provider_used"] == "file" and sc["data"]["plan"]["candidates"] == ["file"]
    assert sc["data"]["rules_now"]["copper_layers"] == ["F.Cu", "B.Cu"] and sc["data"]["netclasses"] == [
        "Power"
    ]
    assert sc["validators"][0]["name"] == "drc" and sc["validators"][0]["after"] == 0
    assert sc["data"]["drc"]["ok"] is True and sc["data"]["checks"]["off_board"] == 0
    assert sc["stage"] == "created" and sc["next_suggested"] == "kw_pcb_sync"
    assert [c for c in backend.calls if c[0] == "run_drc"][0][1]["schematic_parity"] is False
    h.fail("kw_pcb_setup", "E_BAD_ARGS", project=project)


def test_pcb_setup_warns_about_netclass_nets_that_match_nothing_and_get_reads_them_back(tmp_path: Path):
    """`VIN` for a local-label net named `/VIN` matches nothing (KiCad accepts it silently): the
    receipt warns with the real name; kw_pcb_get(kinds=['rules']) reads the netclasses back from
    the .kicad_pro on a provider whose reader carries none."""
    nets = (
        Net(code=1, name="GND", nodes=(NetNode("R1", "2"), NetNode("R2", "1"))),
        Net(code=2, name="/VIN", nodes=(NetNode("R1", "1"),)),
        Net(code=3, name="/blinker/BLINK", nodes=(NetNode("R2", "2"),)),
    )
    nl = Netlist(source="demo.kicad_sch", kicad_version="10.0.0", components=(), nets=nets)
    h, backend, project = harness(tmp_path, net=nl)
    sc = h.ok(
        "kw_pcb_setup",
        project=project,
        netclasses={"Power": {"nets": ["GND", "VIN", "BLINK", "/V*"], "track_width_mm": 0.5}},
    )
    warnings = [w for w in sc["warnings"] if w.startswith("netclass Power")]
    assert len(warnings) == 2, sc["warnings"]
    assert "'VIN' matches no net" in warnings[0] and "/VIN" in warnings[0]
    assert "'BLINK' matches no net" in warnings[1] and "/blinker/BLINK" in warnings[1]
    # written to the .kicad_pro as KiCad stores it -> readable back through kw_pcb_get on every provider
    pro = Path(project) / "demo.kicad_pro"
    doc = json.loads(pro.read_text(encoding="utf-8"))
    doc.setdefault("net_settings", {})["classes"] = [
        {"name": "Default", "clearance": 0.2, "track_width": 0.25, "via_diameter": 0.6, "via_drill": 0.3},
        {
            "name": "Power",
            "clearance": 0.2,
            "track_width": 0.5,
            "via_diameter": 0.8,
            "via_drill": 0.4,
            "priority": 0,
        },
    ]
    doc["net_settings"]["netclass_patterns"] = [
        {"netclass": "Power", "pattern": "GND"},
        {"netclass": "Power", "pattern": "/V*"},
    ]
    pro.write_text(json.dumps(doc), encoding="utf-8")

    def reader_without_netclasses(path: Path) -> FBoardModel:  # the real readers carry none
        model = FBoardModel(registry(Path(path).parent))
        model.rules = {k: v for k, v in model.rules.items() if k != "netclasses"}
        return model

    h.ctx.services.file_read_board = reader_without_netclasses  # type: ignore[method-assign]
    rules = h.ok("kw_pcb_get", project=project, kinds=["rules"])["data"]["rules"]
    assert rules["netclasses"]["Power"] == {
        "clearance_mm": 0.2,
        "track_width_mm": 0.5,
        "via_size_mm": 0.8,
        "via_drill_mm": 0.4,
        "priority": 0,
        "nets": ["GND", "/V*"],
    }
    assert (
        rules["netclasses"]["Default"]["nets"] == []
        and rules["netclasses"]["Default"]["track_width_mm"] == 0.25
    )


# ---- kw_pcb_sync ----------------------------------------------------------------------------------------------


def test_pcb_sync_is_gated_on_erc_clean_and_reports_the_sync(tmp_path: Path):
    h, backend, project = harness(tmp_path)
    add_r(h, project, "R1", [50, 50], erc=False)
    sc = h.fail("kw_pcb_sync", "E_GATE", project=project)
    assert (
        sc["data"] == {"required_stage": "erc_clean", "stage": "sch_written"} and "kw_sch_check" in sc["fix"]
    )
    h.fail("kw_pcb_sync", "E_BAD_ARGS", project=project, force={})  # a force needs a reason
    to_erc_clean(h, project)
    sc = h.ok("kw_pcb_sync", project=project)
    d = sc["data"]
    assert (
        d["added"] == ["R1"]
        and d["updated"] == []
        and d["removed"] == []
        and d["unresolved"] == []
        and d["ok"] is True
    )
    assert d["netlist"] == {"components": 1, "nets": 1}
    assert calls(project, "sync") == [{"remove_orphans": True, "keep_positions": True, "components": 1}]
    assert sc["changed"]["added"] == ["R1"] and "demo.kicad_pcb" in sc["changed"]["updated"]
    assert sc["stage"] == "synced" and sc["next_suggested"] == "kw_pcb_place"
    names = [v["name"] for v in sc["validators"]]
    assert names == ["drc", "parity"]
    drc_call = [c for c in backend.calls if c[0] == "run_drc"][-1][1]
    assert drc_call["schematic_parity"] is True
    # the new part sits off-board (100, 100): placed is not reached, the checks say so
    assert (
        d["checks"]["off_board"] == 1
        and d["checks"]["off_board_copper"] == 1
        and d["checks"]["items"][0]["refs"] == ["R1"]
    )
    assert any("off-board" in w for w in sc["warnings"])
    # the netlist file went under .kiwrap/tmp (never into the snapshot)
    export = [c for c in backend.calls if c[0] == "export_netlist"][-1][1]
    assert export["schematic"].endswith("demo.kicad_sch")
    # a second sync updates instead of adding, and is not an external edit
    sc = h.ok("kw_pcb_sync", project=project, remove_orphans=False)
    assert (
        sc["data"]["updated"] == ["R1"]
        and sc["data"]["added"] == []
        and "external_edit" not in sc["degraded"]
    )
    assert calls(project, "sync")[-1]["remove_orphans"] is False


def test_pcb_sync_unresolved_footprints_and_forced_gate(tmp_path: Path):
    h, _, project = harness(tmp_path)
    add_r(h, project, "R1", [50, 50], erc=False)
    FakeBoardWriter.unresolved = [("R1", "Nope:Missing", "footprint library 'Nope' is not in fp-lib-table")]
    sc = h.ok("kw_pcb_sync", project=project, force={"reason": "ERC pending, dry run"})
    assert sc["data"]["ok"] is False and sc["data"]["unresolved"][0]["reason"].startswith("footprint library")
    assert sc["next_suggested"] == "kw_sch_assign_footprints" and any(
        "could not be resolved" in w for w in sc["warnings"]
    )
    assert sc["stage"] == "sch_written"  # forced from below the gate: the stage does not claim synced
    hist = Project.from_path(project).load_state().history[-1]
    assert hist["force"] == "ERC pending, dry run"


def test_pcb_sync_falls_back_to_the_file_netlist(tmp_path: Path):
    h, backend, project = harness(tmp_path)
    add_r(h, project, "R1", [50, 50])
    to_erc_clean(h, project)
    backend.net = KiwrapError("E_UNSUPPORTED", "no sch export netlist")
    h.ctx.services.netlist_from_file = lambda hier: type("N", (), {"components": [], "nets": []})()  # type: ignore[attr-defined]
    sc = h.ok("kw_pcb_sync", project=project)
    assert sc["degraded"] == ["netlist_estimated"] and sc["data"]["netlist"] == {"components": 0, "nets": 0}
    assert any("file netlist" in w for w in sc["warnings"])


def test_pcb_sync_over_ipc_import_netlist(tmp_path: Path):
    def twin(session: Any, netlist_path: Path, **kw: Any) -> FSyncReport:
        assert Path(netlist_path).name == "demo.sync.net" and ".kiwrap" in str(netlist_path)
        return FSyncReport(["R1", "R2"], [], ["C9"], [], [], [])

    h, backend, project = harness(tmp_path, **with_11(ipc_twins={"ipc_pcb_sync": twin}))
    add_r(h, project, "R1", [50, 50])
    to_erc_clean(h, project)
    sc = h.ok("kw_pcb_sync", project=project)
    assert sc["provider_used"] == "ipc11:11.0.0" and sc["changed"]["removed"] == ["C9"]
    assert backend.ipc_log[-2][1]["remove_orphans"] is True and backend.saves() == ["ipc_pcb_save"]
    # the presence probe says ImportNetlist is not handled headless -> file
    backend.ipc_twins["ipc_pcb_sync"] = KiwrapError(
        "E_UNSUPPORTED", "ImportNetlist not handled headless", data={"fallback": "file"}
    )
    sc = h.ok("kw_pcb_sync", project=project)
    assert sc["provider_used"] == "file" and sc["degraded"] == ["ipc11_unavailable"]


# ---- kw_pcb_place ------------------------------------------------------------------------------------------------


def test_pcb_place_reports_before_after_deltas_and_reaches_placed(tmp_path: Path):
    h, _, project = harness(tmp_path)
    to_synced(h, project)  # R1 at (100, 100), R2 at (110, 100): both off-board
    sc = h.ok("kw_pcb_place", project=project, placements=[{"ref": "R1", "at_mm": [10, 10], "rotation": 90}])
    d = sc["data"]
    assert calls(project, "place") == [
        {"ref": "R1", "at_mm": (10.0, 10.0), "rotation": 90.0, "side": None, "locked": None}
    ]
    assert d["before"] == {"courtyard_overlap": 0, "off_board": 2, "off_board_copper": 2, "unrouted": 1}
    assert d["after"] == {"courtyard_overlap": 0, "off_board": 1, "off_board_copper": 1, "unrouted": 1}
    by_name = {v["name"]: v for v in sc["validators"]}
    assert by_name["off_board_copper"] == {"name": "off_board_copper", "before": 2, "after": 1, "detail": {}}
    assert by_name["unrouted"]["before"] == 1 and "ratsnest_estimated" in sc["degraded"]
    assert (
        d["items"] == []
        and d["ok"] is False
        and sc["stage"] == "synced"
        and sc["next_suggested"] == "kw_pcb_check"
    )
    assert d["footprints_now"] == [
        {"ref": "R1", "x_mm": 10.0, "y_mm": 10.0, "rotation_deg": 90.0, "layer": "F.Cu", "bbox_mm": None}
    ]
    assert d["providers"] == {"overlaps": "file", "off_board": "file", "unrouted": "file"}
    # R2 next to R1 -> overlap item names both refs with a fix; still not placed
    sc = h.ok(
        "kw_pcb_place",
        project=project,
        placements=[{"ref": "R2", "at_mm": [1, 0]}],
        relative_to="R1",
        side="bottom",
    )
    assert calls(project, "place")[-1]["at_mm"] == (11.0, 10.0)
    assert sc["data"]["after"]["courtyard_overlap"] == 1 and sc["data"]["items"][0]["refs"] == ["R1", "R2"]
    assert "move one of" in sc["data"]["items"][0]["fix"] and sc["stage"] == "synced"
    # apart and inside: placed, drc optional
    sc = h.ok("kw_pcb_place", project=project, placements=[{"ref": "R2", "at_mm": [30, 20]}], drc=True)
    assert sc["data"]["ok"] is True and sc["stage"] == "placed" and sc["next_suggested"] == "kw_pcb_zones"
    assert sc["data"]["drc"]["ok"] is True and [v["name"] for v in sc["validators"]][-1] == "drc"
    sc = h.fail("kw_pcb_place", "E_BAD_ARGS", project=project, placements=[{"ref": "R9", "at_mm": [1, 1]}])
    assert sc["candidates"] == ["R1", "R2"]
    h.fail(
        "kw_pcb_place",
        "E_BAD_ARGS",
        project=project,
        placements=[{"ref": "R1", "at_mm": [1, 1]}],
        relative_to="U9",
    )


def test_pcb_place_without_analysis_degrades_instead_of_failing(tmp_path: Path):
    h, _, project = harness(tmp_path, analysis_missing=True)
    to_synced(h, project)
    sc = h.ok("kw_pcb_place", project=project, placements=[{"ref": "R1", "at_mm": [10, 10]}])
    assert sc["validators"] == [] and sc["data"]["after"]["off_board"] is None and sc["data"]["ok"] is True
    assert {"overlaps_unavailable", "off_board_unavailable", "connectivity_unavailable"} <= set(
        sc["degraded"]
    )
    assert sc["stage"] == "synced"  # unknown checks never promote to placed


def test_pcb_place_over_ipc(tmp_path: Path):
    h, backend, project = harness(tmp_path, **with_11(ipc_twins={"ipc_pcb_place": lambda s, **kw: None}))
    to_synced(h, project)
    sc = h.ok(
        "kw_pcb_place",
        project=project,
        placements=[{"ref": "R1", "at_mm": [10, 10], "side": "bottom"}, {"ref": "R2", "at_mm": [30, 20]}],
    )
    assert sc["provider_used"] == "ipc11:11.0.0"
    assert backend.ipc_log[-3:] == [
        (
            "ipc_pcb_place",
            {"ref": "R1", "at_mm": (10.0, 10.0), "rotation": None, "side": "back", "locked": None},
        ),
        (
            "ipc_pcb_place",
            {"ref": "R2", "at_mm": (30.0, 20.0), "rotation": None, "side": None, "locked": None},
        ),
        ("ipc_pcb_save", {}),
    ]


def test_pcb_place_rotation_is_guarded_off_ipc_on_affected_builds(tmp_path: Path):
    """G16: on a KiCad whose UpdateItems leaves 1 nm noise in a rotated footprint, a placement
    that changes an orientation goes to the file engine (`ipc11_guarded`); moves, flips and a
    rotation equal to the current one stay on ipc11; a build without the quirk rotates over IPC."""
    caps = {**CAPS_11, "ipc.footprint_update_rotation_noise": True}
    h, backend, project = harness(
        tmp_path,
        installs=[FakeInstall(version="11.0.0")],
        caps={"11.0.0": caps},
        ipc_twins={"ipc_pcb_place": lambda s, **kw: None},
    )
    to_synced(h, project)
    sc = h.ok("kw_pcb_place", project=project, placements=[{"ref": "R1", "at_mm": [10, 10], "rotation": 90}])
    assert sc["provider_used"] == "file" and "ipc11_guarded" in sc["degraded"]
    assert any("G16" in w for w in sc["warnings"])
    assert not [c for c in backend.ipc_log if c[0] == "ipc_pcb_place"]
    # the same rotation again is not a change: ipc11 serves it
    sc = h.ok(
        "kw_pcb_place",
        project=project,
        placements=[{"ref": "R1", "at_mm": [12, 10], "rotation": 90, "side": "bottom"}],
    )
    assert sc["provider_used"] == "ipc11:11.0.0" and "ipc11_guarded" not in sc["degraded"]
    sc = h.ok("kw_pcb_place", project=project, placements=[{"ref": "R2", "at_mm": [30, 20]}])
    assert sc["provider_used"] == "ipc11:11.0.0"
    # a build without the quirk
    (tmp_path / "clean").mkdir()
    h2, backend2, project2 = harness(
        tmp_path / "clean", **with_11(ipc_twins={"ipc_pcb_place": lambda s, **kw: None})
    )
    to_synced(h2, project2)
    sc = h2.ok(
        "kw_pcb_place", project=project2, placements=[{"ref": "R1", "at_mm": [10, 10], "rotation": 90}]
    )
    assert sc["provider_used"] == "ipc11:11.0.0" and "ipc11_guarded" not in sc["degraded"]


# ---- kw_pcb_zones / kw_pcb_route -------------------------------------------------------------------------------


def place_all(h: Harness, project: str) -> None:
    to_synced(h, project)
    sc = h.ok(
        "kw_pcb_place",
        project=project,
        placements=[{"ref": "R1", "at_mm": [10, 10]}, {"ref": "R2", "at_mm": [30, 20]}],
    )
    assert sc["stage"] == "placed"


def test_pcb_zones_add_fill_and_drc_delta(tmp_path: Path):
    h, backend, project = harness(tmp_path)
    place_all(h, project)
    backend.drc = drc_with(net_violation("clearance", "GND"), net_violation("clearance", "SIG"))
    sc = h.ok(
        "kw_pcb_zones",
        project=project,
        zones=[
            {
                "net": "GND",
                "layers": ["F.Cu", "B.Cu"],
                "rect_mm": [0, 0, 50, 30],
                "priority": 1,
                "clearance_mm": 0.3,
            },
            {"net": "", "layers": "F.Cu", "polygon_mm": [[1, 1], [5, 1], [5, 5]], "keepout": True},
        ],
    )
    zones = calls(project, "add_zone")
    assert zones[0] == {
        "net": "GND",
        "layers": ["F.Cu", "B.Cu"],
        "polygon_mm": [(0.0, 0.0), (50.0, 0.0), (50.0, 30.0), (0.0, 30.0)],
        "priority": 1,
        "clearance_mm": 0.3,
    }
    assert (
        zones[1]["keepout"] == {"tracks": True, "vias": True, "copperpour": True}
        and zones[1]["priority"] == 0
    )
    d = sc["data"]
    assert (
        len(d["zones"]) == 2
        and d["zones"][0]["net"] == "GND"
        and sc["changed"]["added"] == [z["uuid"] for z in d["zones"]]
    )
    drc_call = [c for c in backend.calls if c[0] == "run_drc"][-1][1]
    assert drc_call["refill_zones"] is True and drc_call["save_board"] is True
    assert d["filled"] is True and d["drc"]["refilled"] is True and d["drc"]["must_fix"] == 2
    assert d["drc"]["touched_nets"] == {"GND": 1}  # the unnamed keepout has no net
    by_name = {v["name"]: v for v in sc["validators"]}
    assert by_name["drc"]["after"] == 2 and by_name["drc_touched_nets"] == {
        "name": "drc_touched_nets",
        "before": None,
        "after": 1,
        "detail": {"by_net": {"GND": 1}},
    }
    assert sc["stage"] == "placed" and sc["next_suggested"] == "kw_pcb_drc"
    # clean DRC after the fix: routed + drc_clean
    backend.drc = drc_with()
    sc = h.ok(
        "kw_pcb_zones", project=project, zones=[{"net": "+3V3", "layers": "B.Cu", "rect_mm": [0, 0, 10, 10]}]
    )
    assert (
        sc["stage"] == "drc_clean"
        and sc["data"]["drc"]["ok"] is True
        and sc["next_suggested"] == "kw_pcb_check"
    )
    # errors
    h.fail(
        "kw_pcb_zones",
        "E_BAD_ARGS",
        project=project,
        zones=[{"net": "GND", "layers": "F.SilkS", "rect_mm": [0, 0, 1, 1]}],
    )
    h.fail("kw_pcb_zones", "E_BAD_ARGS", project=project, zones=[{"net": "GND", "layers": "F.Cu"}])
    h.fail(
        "kw_pcb_zones",
        "E_BAD_ARGS",
        project=project,
        zones=[{"net": "GND", "layers": "F.Cu", "polygon_mm": [[0, 0], [1, 1]]}],
    )


def test_pcb_zones_without_refill_capability_warns(tmp_path: Path):
    h, backend, project = harness(tmp_path, caps={"10.0.0": {**CAPS_10, "cli.drc_refill_flags": False}})
    place_all(h, project)
    sc = h.ok(
        "kw_pcb_zones", project=project, zones=[{"net": "GND", "layers": "F.Cu", "rect_mm": [0, 0, 50, 30]}]
    )
    assert sc["data"]["filled"] is False and "refill_unavailable" in sc["degraded"]
    assert [c for c in backend.calls if c[0] == "run_drc"][-1][1]["refill_zones"] is False


def test_place_and_route_refill_stale_zones_before_drc(tmp_path: Path):
    """Tracks added after a zone fill are `clearance` errors against the stale pour unless the
    DRC refills first: kw_pcb_route / kw_pcb_place refill (and save) when the board has copper
    zones, kw_pcb_drc refills in memory by default and saves only with save_board=true (D22),
    and the saved fill is not an external edit."""
    h, backend, project = harness(tmp_path)
    place_all(h, project)
    drc_calls = [c for c in backend.calls if c[0] == "run_drc"]
    assert drc_calls and drc_calls[-1][1]["refill_zones"] is False  # no zones yet: no refill
    h.ok("kw_pcb_zones", project=project, zones=[{"net": "GND", "layers": "F.Cu", "rect_mm": [0, 0, 50, 30]}])
    sc = h.ok(
        "kw_pcb_route",
        project=project,
        ops=[{"op": "track", "points_mm": [[10, 10], [20, 10]], "net": "SIG"}],
    )
    call = [c for c in backend.calls if c[0] == "run_drc"][-1][1]
    assert call["refill_zones"] is True and call["save_board"] is True
    assert sc["data"]["drc"]["refilled"] is True and sc["changed"]["updated"] == ["demo.kicad_pcb"]
    assert "zones_stale" not in sc["degraded"]
    sc = h.ok("kw_pcb_place", project=project, placements=[{"ref": "R1", "at_mm": [12, 12]}])
    assert [c for c in backend.calls if c[0] == "run_drc"][-1][1]["refill_zones"] is True
    sc = h.ok("kw_pcb_drc", project=project)
    call = [c for c in backend.calls if c[0] == "run_drc"][-1][1]
    # D22: kw_pcb_drc refills in memory by default and never writes the board unless asked
    assert call["refill_zones"] is True and call["save_board"] is False
    assert sc["data"]["options"] == {"schematic_parity": False, "refill_zones": True, "save_board": False}
    assert sc["stage"] == "drc_clean" and sc["changed"]["updated"] == []
    sc = h.ok("kw_pcb_drc", project=project, save_board=True)
    call = [c for c in backend.calls if c[0] == "run_drc"][-1][1]
    assert call["refill_zones"] is True and call["save_board"] is True
    assert sc["data"]["options"] == {"schematic_parity": False, "refill_zones": True, "save_board": True}
    status = h.ok("kw_project", action="status", project=project)
    assert status["data"]["stage"] == "drc_clean" and "external_edit" not in status["degraded"]
    sc = h.ok("kw_pcb_drc", project=project, refill_zones=False)
    assert [c for c in backend.calls if c[0] == "run_drc"][-1][1]["refill_zones"] is False


def test_stale_zones_without_refill_capability_are_review_items(tmp_path: Path):
    """No headless refill on this KiCad: the receipt says `zones_stale` and a track-vs-zone
    clearance item is `review` with a note, not a must_fix that blocks drc_clean."""
    h, backend, project = harness(tmp_path, caps={"10.0.0": {**CAPS_10, "cli.drc_refill_flags": False}})
    place_all(h, project)
    h.ok("kw_pcb_zones", project=project, zones=[{"net": "GND", "layers": "F.Cu", "rect_mm": [0, 0, 50, 30]}])
    zone_hit = Violation(
        type="clearance",
        severity="error",
        description="Clearance violation (clearance 0.15 mm; actual 0.0 mm)",
        items=(
            ViolationItem(uuid="u-1", description="Track [/VIN] on F.Cu", pos=Position(1.0, 2.0)),
            ViolationItem(uuid="u-2", description="Zone [GND] on F.Cu", pos=Position(1.0, 2.0)),
        ),
        sheet=None,
    )
    backend.drc = drc_with(zone_hit, net_violation("clearance", "SIG"))
    sc = h.ok(
        "kw_pcb_route",
        project=project,
        ops=[{"op": "track", "points_mm": [[10, 10], [20, 10]], "net": "SIG"}],
    )
    assert "zones_stale" in sc["degraded"] and sc["data"]["drc"]["refilled"] is False
    assert sc["data"]["drc"]["must_fix"] == 1 and sc["data"]["drc"]["review"] == 1
    reviewed = [v for v in sc["data"]["drc"]["violations"] if v["class"] == "review"]
    assert reviewed and "stale zone fill" in reviewed[0]["note"]
    sc = h.ok("kw_pcb_drc", project=project)
    assert "zones_stale" in sc["degraded"] and sc["data"]["must_fix"] == 1 and sc["data"]["review"] == 1
    assert [c for c in backend.calls if c[0] == "run_drc"][-1][1]["refill_zones"] is False


def test_pcb_route_primitives_defaults_and_drc_delta(tmp_path: Path):
    h, backend, project = harness(tmp_path)
    place_all(h, project)
    backend.drc = drc_with(net_violation("clearance", "SIG"))
    sc = h.ok(
        "kw_pcb_route",
        project=project,
        ops=[
            {"op": "track", "points_mm": [[10, 10], [20, 10], [20, 20]], "net": "SIG"},
            {"op": "track", "points_mm": [[0, 0], [5, 0]], "net": "GND", "layer": "B.Cu", "width_mm": 1.0},
            {"op": "via", "at_mm": [20, 20], "net": "SIG"},
            {
                "op": "via",
                "at_mm": [5, 0],
                "net": "GND",
                "size_mm": 1.2,
                "drill_mm": 0.6,
                "layers": ["F.Cu", "B.Cu"],
            },
        ],
    )
    tracks = calls(project, "add_track")
    assert tracks[0] == {
        "pts": [(10.0, 10.0), (20.0, 10.0), (20.0, 20.0)],
        "width_mm": 0.3,
        "layer": "F.Cu",
        "net": "SIG",
    }  # Default class
    assert tracks[1]["width_mm"] == 1.0 and tracks[1]["layer"] == "B.Cu"
    vias = calls(project, "add_via")
    assert vias[0] == {
        "at_mm": (20.0, 20.0),
        "size_mm": 0.7,
        "drill_mm": 0.35,
        "net": "SIG",
        "layers": ["F.Cu", "B.Cu"],
    }
    assert vias[1]["size_mm"] == 1.2 and vias[1]["drill_mm"] == 0.6
    d = sc["data"]
    assert [a["op"] for a in d["applied"]] == ["track", "track", "via", "via"] and d["applied"][0][
        "segments"
    ] == 2
    assert len(sc["changed"]["added"]) == 5 and d["touched_nets"] == ["GND", "SIG"]
    assert (
        d["drc"]["touched_nets"] == {"GND": 0, "SIG": 1}
        and d["drc"]["violations"][0]["items"][0]["description"] == "Track [SIG] on F.Cu"
    )
    assert sc["stage"] == "placed" and sc["next_suggested"] == "kw_pcb_drc" and d["ok"] is False
    # the netclass of the net drives the default width (Power: 0.6)
    backend.drc = drc_with()
    sc = h.ok(
        "kw_pcb_route",
        project=project,
        ops=[
            {"op": "track", "points_mm": [[0, 0], [1, 0]], "net": "GND"},
            {"op": "delete", "uuid": "track-0001"},
        ],
    )
    assert calls(project, "add_track")[-1]["width_mm"] == 0.6 and calls(project, "delete") == [
        {"uuid": "track-0001"}
    ]
    assert sc["changed"]["removed"] == ["track-0001"] and sc["stage"] == "drc_clean"
    assert {v["name"]: v["before"] for v in sc["validators"]} == {"drc": 1, "drc_touched_nets": 1}
    # a non-copper layer is refused before anything is written
    sc = h.fail(
        "kw_pcb_route",
        "E_BAD_ARGS",
        project=project,
        ops=[{"op": "track", "points_mm": [[0, 0], [1, 0]], "layer": "F.SilkS"}],
    )
    assert sc["candidates"] == ["F.Cu", "B.Cu"] and "silently" in sc["why"]
    h.fail("kw_pcb_route", "E_BAD_ARGS", project=project, ops=[{"op": "track", "points_mm": [[0, 0]]}])
    # a net the board does not have (a local label's net is /LOCAL on the board) is refused with candidates
    sc = h.fail(
        "kw_pcb_route",
        "E_BAD_ARGS",
        project=project,
        ops=[{"op": "track", "points_mm": [[0, 0], [1, 0]], "net": "LOCAL"}],
    )
    assert sc["candidates"] == ["/LOCAL"] and "ops[0]" in sc["message"]
    sc = h.fail(
        "kw_pcb_zones",
        "E_BAD_ARGS",
        project=project,
        zones=[{"net": "gnd", "layers": "F.Cu", "rect_mm": [0, 0, 1, 1]}],
    )
    assert sc["candidates"] == ["GND"]
    h.fail("kw_pcb_route", "E_BAD_ARGS", project=project, ops=[{"op": "delete"}])
    h.fail(
        "kw_pcb_route",
        "E_BAD_ARGS",
        project=project,
        ops=[{"op": "via", "at_mm": [1, 1], "layers": ["F.Cu"]}],
    )


def test_pcb_route_over_ipc_commits(tmp_path: Path):
    twins = {
        "ipc_pcb_add_track": lambda s, **kw: [f"ipc-t{i}" for i in range(len(kw["pts"]) - 1)],
        "ipc_pcb_add_via": lambda s, **kw: "ipc-v1",
        "ipc_pcb_delete": lambda s, **kw: None,
    }
    h, backend, project = harness(tmp_path, **with_11(ipc_twins=twins))
    place_all(h, project)
    sc = h.ok(
        "kw_pcb_route",
        project=project,
        ops=[
            {"op": "track", "points_mm": [[0, 0], [1, 0], [1, 1]], "net": "SIG"},
            {"op": "via", "at_mm": [1, 1], "net": "SIG"},
            {"op": "delete", "uuid": "z"},
        ],
    )
    assert (
        sc["provider_used"] == "ipc11:11.0.0"
        and sc["changed"]["added"] == ["ipc-t0", "ipc-t1", "ipc-v1"]
        and sc["changed"]["removed"] == ["z"]
    )
    assert [n for n, _ in backend.ipc_log][-4:] == [
        "ipc_pcb_add_track",
        "ipc_pcb_add_via",
        "ipc_pcb_delete",
        "ipc_pcb_save",
    ]


# ---- snapshots / undo / history ----------------------------------------------------------------------------------


def test_every_authoring_mutation_snapshots_and_restore_undoes(tmp_path: Path):
    h, _, project = harness(tmp_path)
    sc1 = add_r(h, project, "R1", [50, 50])
    sc2 = add_r(h, project, "R2", [70, 50])
    assert sc1["snapshot"] and sc2["snapshot"] and sc1["snapshot"] != sc2["snapshot"]
    listed = h.ok("kw_snapshot", project=project, action="list")["data"]
    messages = (
        [e["message"] for e in listed["entries"]]
        if "entries" in listed
        else [e["message"] for e in listed["snapshots"]]
    )
    assert any("[kiwrap] kw_sch_add_symbol R2" in m for m in messages)
    restored = h.ok("kw_snapshot", project=project, action="restore", ref=sc1["snapshot"])
    assert not restored["data"].get("error")
    text = Project.from_path(project).root_sch.read_text(encoding="utf-8")
    assert text.count("fake write") == 1
    state = Project.from_path(project).load_state()
    assert state.history[-1]["tool"] == "kw_snapshot" or state.history[-2]["tool"] == "kw_sch_add_symbol"


# ---- IPC sheet addressing ---------------------------------------------------------------------------------


def test_ipc_sheet_maps_file_names_to_instance_paths(tmp_path: Path):
    """The tools take a sheet FILE (stem) / name / '' while the IPC twins take the instance path
    `/name/` of a session on the ROOT document; `ipc_sheet` translates through the file reader's
    hierarchy and teaches with candidates when the sheet does not exist."""
    from types import SimpleNamespace

    from kiwrap.core.state.project import Project
    from kiwrap.core.tools._author import ipc_sheet

    h, backend, project = harness(tmp_path)
    proj = Project.from_path(project)

    def inst(names_path: str, file_name: str, name: str) -> Any:
        return SimpleNamespace(
            names_path=names_path, name=name, file=SimpleNamespace(path=proj.dir / file_name)
        )

    hier = SimpleNamespace(
        instances=[
            inst("/", proj.root_sch.name, ""),
            inst("/blinker/", "blinker.kicad_sch", "blinker"),
            inst("/blinker/sub/", "sub.kicad_sch", "sub"),
        ]
    )
    ctx = h.ctx
    ctx.services.file_read_schematic = lambda path: hier  # type: ignore[method-assign]
    assert ipc_sheet(ctx, proj, "") == "/"
    assert ipc_sheet(ctx, proj, proj.root_sch.name) == "/"
    assert ipc_sheet(ctx, proj, "blinker") == "/blinker/"
    assert ipc_sheet(ctx, proj, "blinker.kicad_sch") == "/blinker/"
    assert ipc_sheet(ctx, proj, "sub") == "/blinker/sub/"
    assert ipc_sheet(ctx, proj, "/blinker/sub/") == "/blinker/sub/"  # an instance path passes through
    with pytest.raises(KiwrapError) as err:
        ipc_sheet(ctx, proj, "power")
    assert err.value.code == "E_BAD_ARGS" and "/blinker/" in err.value.candidates
