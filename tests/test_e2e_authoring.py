"""End-to-end authoring over MCP stdio: an agent following AGENTS.md / the kiwrap-flow skill builds
a two-sheet LDO + 555 LED blinker (11 parts) from nothing to fabrication files, on every primary
KiCad, with KiCad as the judge at every step.

    kw_project create -> kw_lib_search -> kw_sch_add_symbol xN (pins from the receipts)
    -> kw_sch_add_item sheet (blinker.kicad_sch with a BLINK output pin) -> symbols + wires on it
    -> kw_sch_connect (wires by (ref, pin) + explicit corners; power symbols pin-on-pin)
    -> kw_sch_add_item pwr_flag / hierarchical_label -> kw_sch_check (0 errors)
    -> kw_sch_annotate -> kw_sch_assign_footprints (nothing unresolved)
    -> kw_pcb_setup (2 layers, 40 x 30 mm, JLCPCB-ish rules) -> kw_pcb_sync (parity 0)
    -> kw_pcb_place + kw_pcb_check until 0 overlaps / 0 off-board
    -> routing, two variants of the same conversation:
       hand: kw_pcb_route (every net as track primitives) -> kw_pcb_zones (GND pour, both sides)
       auto: kw_pcb_zones (the B.Cu GND plane the router works against) -> kw_pcb_route_auto
             (Freerouting 2.4.1 under the managed JRE, a background job polled with kw_job)
             -> kw_pcb_fix when the router left must_fix items -> kw_pcb_zones (the F.Cu pour)
    -> kw_pcb_drc -> kw_fab_export (gerbers, drill, pos)

The whole conversation goes through `kiwrap serve` over stdio with the mcp client (the agent's
surface, tests/test_mcp_protocol.py), one scenario per install: the 10.99 nightly answers the
writes from `ipc11` (headless api-server), 10.0.6 from the `file` engine; kicad-cli of the same
install validates every step. Every call's provider / degraded / stage is recorded in
`<project>/.kiwrap/e2e-log.json` and printed at the end (`-s`). The autorouted variant needs the
engines scripts/bootstrap.ps1 installs (`%LOCALAPPDATA%/kiwrap/engines`; the server finds them
from its throw-away storage home through `discover_engines(fallback=True)`) and skips otherwise;
the DSN/SES codec on the stock installs is kiwrap's own (`provider_used: codec`).
"""

from __future__ import annotations

import json
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import Client, StdioServerParameters

from kiwrap.backends.router import discover_engines

pytestmark = [pytest.mark.kicad, pytest.mark.slow]  # ~60 s per scenario: KiCad validates every write

Call = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]

WRITE_TOOLS = (
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
    "kw_pcb_route_auto",
    "kw_pcb_fix",
)

#: the autorouter's budget on the blinker (11 parts, 8 nets; a clean run takes a few seconds)
AUTO_PASSES = 60
AUTO_TIMEOUT_S = 300.0


class FlowError(AssertionError):
    pass


class Log:
    """Every tool call with what the agent reads first: provider, degraded, stage, validators."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def add(self, tool: str, sc: dict[str, Any], *, error: bool) -> None:
        row = {
            "tool": tool,
            "error": error,
            "code": sc.get("code") if error else None,
            "provider_used": sc.get("provider_used"),
            "degraded": sc.get("degraded"),
            "stage": sc.get("stage"),
            "next": sc.get("next_suggested"),
            "validators": [
                {k: v.get(k) for k in ("name", "before", "after")} for v in (sc.get("validators") or [])
            ],
            "item_type": (sc.get("data") or {}).get("type") if isinstance(sc.get("data"), dict) else None,
        }
        self.rows.append(row)

    def dump(self) -> str:
        lines = []
        for r in self.rows:
            v = ", ".join(f"{x['name']} {x['before']}->{x['after']}" for x in r["validators"])
            lines.append(
                f"{'!!' if r['error'] else 'ok'} {r['tool']:<26} provider={r['provider_used']!s:<14} "
                f"degraded={r['degraded']} stage={r['stage']} [{v}]"
            )
        return "\n".join(lines)


async def flow(call: Call, work: Path, target: str, log: Log, *, router: str = "hand") -> dict[str, Any]:
    """The agent's conversation. `call(tool, args)` returns the structured content (a teaching
    error raises FlowError). `router` is `hand` (kw_pcb_route primitives, the pour after) or
    `auto` (the B.Cu plane, kw_pcb_route_auto + kw_pcb_fix, the F.Cu pour after). Returns the
    final numbers."""
    if router not in ("hand", "auto"):
        raise ValueError(f"router must be hand|auto, not {router!r}")
    out: dict[str, Any] = {"routing_mode": router}
    proj = work / "blinky"
    p = str(proj)

    async def ok(tool: str, **args: Any) -> dict[str, Any]:
        sc = await call(tool, args)
        return sc

    # ---- project + parts --------------------------------------------------------------------
    sc = await ok(
        "kw_project", action="create", dir=p, name="blinky", target_kicad=target, board_size_mm=[40, 30]
    )
    out["create"] = sc["data"]
    sc = await ok("kw_lib_search", kind="symbol", query="NE555P", project=p)
    hits = sc["data"]["hits"]
    if not hits or hits[0]["id"] != "Timer:NE555P":
        raise FlowError(f"kw_lib_search NE555P: {[h['id'] for h in hits[:5]]}")
    sc = await ok("kw_lib_search", kind="symbol", query="AMS1117-3.3", project=p)
    if not sc["data"]["hits"] or sc["data"]["hits"][0]["id"] != "Regulator_Linear:AMS1117-3.3":
        raise FlowError(f"kw_lib_search AMS1117: {[h['id'] for h in sc['data']['hits'][:5]]}")
    sc = await ok("kw_lib_info", id="Timer:NE555P", project=p)
    pins555 = {pn["number"]: pn["name"] for pn in sc["data"]["item"]["pins"]}
    if pins555.get("3") != "OUT" or pins555.get("7") != "DISCH":
        raise FlowError(f"kw_lib_info NE555P pins: {pins555}")
    sc = await ok("kw_lib_search", kind="footprint", query="DIP-8 W7.62mm", project=p)
    dip8 = next((h["id"] for h in sc["data"]["hits"] if h["id"] == "Package_DIP:DIP-8_W7.62mm"), None)
    if dip8 is None:
        raise FlowError(f"kw_lib_search DIP-8: {[h['id'] for h in sc['data']['hits'][:5]]}")

    # ---- root sheet: LDO --------------------------------------------------------------------
    pins: dict[str, dict[str, tuple[float, float]]] = {}

    async def add(lib_id: str, ref: str, value: str | None, at: list[float], **kw: Any) -> None:
        sc = await ok(
            "kw_sch_add_symbol", project=p, lib_id=lib_id, ref=ref, value=value, at_mm=at, erc=False, **kw
        )
        d = sc["data"]
        if d["ref"] != ref or not d["pins"]:
            raise FlowError(f"kw_sch_add_symbol {ref}: {d}")
        pins[ref] = {pn["number"]: (pn["x_mm"], pn["y_mm"]) for pn in d["pins"]}

    await add(
        "Connector_Generic:Conn_01x02",
        "J1",
        "VIN",
        [60.96, 78.74],
        footprint="Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical",
    )
    await add("Device:C", "C1", "10u", [71.12, 86.36])
    await add("Regulator_Linear:AMS1117-3.3", "U1", None, [88.9, 78.74])
    await add("Device:C", "C2", "10u", [106.68, 86.36])
    await add("Device:R", "R4", "330", [165.1, 78.74], rotation=90)
    await add("Device:LED", "D1", "RED", [177.8, 78.74], rotation=180)
    # the returned endpoints are the verified transform (claim 3): check a few against the library
    if pins["U1"]["3"] != (81.28, 78.74) or pins["U1"]["1"] != (88.9, 86.36):
        raise FlowError(f"U1 pins: {pins['U1']}")
    if pins["R4"]["1"] != (161.29, 78.74) or pins["D1"]["2"] != (173.99, 78.74):
        raise FlowError(f"R4/D1 pins: {pins['R4']} {pins['D1']}")

    def pin(ref: str, n: str) -> dict[str, Any]:
        return {"ref": ref, "pin": n}

    def at(x: float, y: float) -> dict[str, Any]:
        return {"at_mm": [x, y]}

    async def wire(*eps: dict[str, Any], net: str | None = None) -> None:
        sc = await ok("kw_sch_connect", project=p, endpoints=list(eps), route="direct", net=net, erc=False)
        if not sc["data"]["wires"]:
            raise FlowError(f"kw_sch_connect no wires: {sc['data']}")

    async def power(net: str, *eps: dict[str, Any]) -> None:
        sc = await ok("kw_sch_connect", project=p, endpoints=list(eps), style="power", net=net, erc=False)
        if len(sc["data"]["power"]) != len(eps):
            raise FlowError(f"kw_sch_connect power {net}: {sc['data']}")

    # VIN: J1.1 -> around the connector -> U1.VI, C1.1 stub onto it
    await wire(pin("J1", "1"), at(53.34, 78.74), at(53.34, 71.12), at(81.28, 71.12), pin("U1", "3"))
    await wire(pin("C1", "1"), at(71.12, 71.12))
    # a local label names the rail (/VIN on the board); labels connect anywhere along a wire
    sc = await ok(
        "kw_sch_add_item",
        project=p,
        item={"type": "label", "text": "VIN", "at_mm": [66.04, 71.12]},
        erc=False,
    )
    if not sc["data"].get("uuid"):
        raise FlowError(f"label VIN: {sc['data']}")
    # +3V3: U1.VO -> C2.1, power symbol on U1.VO
    await wire(pin("U1", "2"), at(106.68, 78.74), pin("C2", "1"))
    await power("+3V3", pin("U1", "2"))
    # GND rail y = 93.98 from J1.2 to D1.K, stubs from C1.2 / U1.GND / C2.2
    await wire(
        pin("J1", "2"),
        at(53.34, 81.28),
        at(53.34, 93.98),
        at(184.15, 93.98),
        at(184.15, 78.74),
        pin("D1", "1"),
    )
    await wire(pin("C1", "2"), at(71.12, 93.98))
    await wire(pin("U1", "1"), at(88.9, 93.98))
    await wire(pin("C2", "2"), at(106.68, 93.98))
    await power("GND", pin("U1", "1"))
    # LED: R4.2 -> D1.A
    await wire(pin("R4", "2"), pin("D1", "2"))
    # PWR_FLAG on the two power-input nets nothing drives (VIN, GND)
    for ref, n, net in (("J1", "1", "VIN"), ("J1", "2", "GND")):
        sc = await ok(
            "kw_sch_add_item",
            project=p,
            item={"type": "pwr_flag", "net": net, "ref": ref, "pin": n},
            erc=False,
        )
        if not str(sc["data"].get("ref") or "").startswith("#FLG"):
            raise FlowError(f"pwr_flag {net}: {sc['data']}")
    # the blinker sheet with its BLINK output pin, wired to the LED resistor
    sc = await ok(
        "kw_sch_add_item",
        project=p,
        item={
            "type": "sheet",
            "text": "blinker",
            "file": "blinker.kicad_sch",
            "at_mm": [127.0, 71.12],
            "size_mm": [25.4, 12.7],
            "pins": [{"name": "BLINK", "side": "right", "kind": "output"}],
        },
        erc=False,
    )
    sheet_pins = {sp["name"]: (sp["x_mm"], sp["y_mm"]) for sp in sc["data"]["pins"]}
    if "BLINK" not in sheet_pins:
        raise FlowError(f"sheet pins: {sc['data']}")
    out["sheet_provider"] = sc["provider_used"]
    bx, by = sheet_pins["BLINK"]
    await wire(at(bx, by), at(161.29, by), pin("R4", "1"))

    # ---- blinker sheet: NE555 astable -----------------------------------------------------
    sheet = "blinker"

    async def add_b(lib_id: str, ref: str, value: str | None, at_: list[float], **kw: Any) -> None:
        sc = await ok(
            "kw_sch_add_symbol",
            project=p,
            lib_id=lib_id,
            ref=ref,
            value=value,
            at_mm=at_,
            sheet=sheet,
            erc=False,
            **kw,
        )
        d = sc["data"]
        if d["ref"] != ref or not d["pins"]:
            raise FlowError(f"kw_sch_add_symbol {ref}: {d}")
        pins[ref] = {pn["number"]: (pn["x_mm"], pn["y_mm"]) for pn in d["pins"]}

    await add_b("Timer:NE555P", "U2", None, [101.6, 76.2])
    await add_b("Device:R", "R1", "1k", [81.28, 63.5])
    await add_b("Device:R", "R2", "10k", [81.28, 81.28])
    await add_b("Device:C", "C3", "10u", [81.28, 96.52])
    await add_b("Device:C", "C4", "10n", [96.52, 58.42])
    if (
        pins["U2"]["7"] != (91.44, 73.66)
        or pins["U2"]["3"] != (111.76, 76.2)
        or pins["U2"]["8"] != (104.14, 66.04)
    ):
        raise FlowError(f"U2 pins: {pins['U2']}")

    async def wire_b(*eps: dict[str, Any]) -> None:
        sc = await ok(
            "kw_sch_connect", project=p, endpoints=list(eps), route="direct", sheet=sheet, erc=False
        )
        if not sc["data"]["wires"]:
            raise FlowError(f"kw_sch_connect (blinker) no wires: {sc['data']}")

    async def power_b(net: str, ep: dict[str, Any], rotation: int = 0) -> None:
        item = {"type": "power", "net": net, "rotation": rotation, **ep}
        sc = await ok("kw_sch_add_item", project=p, item=item, sheet=sheet, erc=False)
        if not str(sc["data"].get("ref") or "").startswith("#PWR"):
            raise FlowError(f"power {net}: {sc['data']}")

    # DISCH node: U2.7 <- R1.2 (from above) and R2.1 (from below)
    await wire_b(pin("U2", "7"), at(81.28, 73.66))
    await wire_b(pin("R1", "2"), at(81.28, 73.66))
    await wire_b(pin("R2", "1"), at(81.28, 73.66))
    # THRES + TRIG joined, R2.2 and C3.1 onto that node
    await wire_b(pin("U2", "6"), at(88.9, 78.74), at(88.9, 81.28), pin("U2", "2"))
    await wire_b(pin("R2", "2"), at(81.28, 88.9), at(88.9, 88.9), at(88.9, 81.28))
    await wire_b(pin("C3", "1"), at(81.28, 88.9))
    # RST tied high through R1.1's +3V3 node; CONT decoupled by C4
    await wire_b(pin("U2", "4"), at(86.36, 71.12), at(86.36, 59.69), pin("R1", "1"))
    await wire_b(pin("U2", "5"), at(101.6, 63.5), at(96.52, 63.5), pin("C4", "2"))
    # OUT -> hierarchical label BLINK (output, matching the sheet pin)
    await wire_b(pin("U2", "3"), at(116.84, 76.2))
    sc = await ok(
        "kw_sch_add_item",
        project=p,
        item={"type": "hierarchical_label", "text": "BLINK", "kind": "output", "at_mm": [116.84, 76.2]},
        sheet=sheet,
        erc=False,
    )
    # power on the blinker sheet: +3V3 on R1.1 and U2.VCC; GND on U2.GND, C3.2, C4.1 (pin on top)
    await power_b("+3V3", pin("R1", "1"))
    await power_b("+3V3", pin("U2", "8"))
    await power_b("GND", pin("U2", "1"))
    await power_b("GND", pin("C3", "2"))
    await power_b("GND", pin("C4", "1"), rotation=180)

    # ---- schematic validation -------------------------------------------------------------------
    sc = await ok("kw_sch_stats", project=p)
    out["stats"] = {
        k: sc["data"].get(k)
        for k in (
            "ok",
            "components",
            "nets",
            "dangling_ends",
            "unconnected_pins",
            "label_only_nets",
            "single_node_nets",
        )
    }
    sc = await ok("kw_sch_check", project=p)
    out["erc"] = {k: sc["data"].get(k) for k in ("ok", "errors", "warnings")}
    out["erc_violations"] = [(v["type"], v.get("severity")) for v in sc["data"].get("violations", [])]
    out["netlist"] = sc["data"].get("netlist")
    if not sc["data"]["ok"] or sc["data"]["errors"] != 0:
        raise FlowError(f"kw_sch_check: {out['erc']} {out['erc_violations']}")
    out["stage_after_check"] = sc["stage"]
    # the intent: every net has the pins the agent meant
    sc = await ok("kw_sch_get", project=p, kinds=["netlist"], detail="normal")
    nets = {n["name"]: {(x["ref"], x["pin"]) for x in n["nodes"]} for n in sc["data"]["netlist"]["nets"]}
    out["nets"] = {k: sorted(v) for k, v in nets.items()}
    expected = {
        "+3V3": {("U1", "2"), ("C2", "1"), ("U2", "8"), ("U2", "4"), ("R1", "1")},
        "GND": {
            ("J1", "2"),
            ("C1", "2"),
            ("U1", "1"),
            ("C2", "2"),
            ("D1", "1"),
            ("U2", "1"),
            ("C3", "2"),
            ("C4", "1"),
        },
        "/blinker/BLINK": {("R4", "1"), ("U2", "3")},  # the hierarchical label names it (KiCad's rule)
        "/VIN": {("J1", "1"), ("C1", "1"), ("U1", "3")},
        "Net-(U2-THRES)": {("U2", "6"), ("U2", "2"), ("R2", "2"), ("C3", "1")},
        "Net-(U2-DISCH)": {("U2", "7"), ("R1", "2"), ("R2", "1")},
        "Net-(U2-CONT)": {("U2", "5"), ("C4", "2")},
        "Net-(D1-A)": {("D1", "2"), ("R4", "2")},
    }
    for name, nodes in expected.items():
        got = {x for x in nets.get(name, set()) if not x[0].startswith("#")}
        if got != nodes:
            raise FlowError(f"net {name}: {sorted(got)} != {sorted(nodes)}; nets: {sorted(nets)}")
    if len(nets) != len(expected):
        raise FlowError(f"unexpected nets: {sorted(nets)}")

    # ---- annotate + footprints ------------------------------------------------------------------
    sc = await ok("kw_sch_annotate", project=p)
    out["annotate"] = sc["data"]["mapping"]
    sc = await ok(
        "kw_sch_assign_footprints",
        project=p,
        mapping={"U2": dip8},
        rules=[
            {"ref_prefix": "R", "fpid": "Resistor_SMD:R_0603_1608Metric"},
            {"ref_prefix": "C", "fpid": "Capacitor_SMD:C_0603_1608Metric"},
            {"ref_prefix": "D", "fpid": "LED_SMD:LED_0603_1608Metric"},
            {
                "ref_prefix": "U",
                "lib_id": "Regulator_Linear:AMS1117-3.3",
                "fpid": "Package_TO_SOT_SMD:SOT-223-3_TabPin2",
            },
        ],
    )
    out["assigned"] = sc["data"]["assigned"]
    out["unresolved"] = sc["data"]["unresolved"]
    if sc["data"]["unresolved"] or not sc["data"]["ok"]:
        raise FlowError(f"kw_sch_assign_footprints unresolved: {sc['data']['unresolved']}")
    if sc["data"]["erc"]["errors"] != 0:
        raise FlowError(f"ERC after assign: {sc['data']['erc']}")

    # ---- board ---------------------------------------------------------------------------------
    sc = await ok(
        "kw_pcb_setup",
        project=p,
        layers=2,
        rules={
            "clearance_mm": 0.15,
            "track_width_mm": 0.25,
            "via_size_mm": 0.6,
            "via_drill_mm": 0.3,
            "min_track_mm": 0.127,
            "min_via_mm": 0.45,
            "min_hole_mm": 0.2,
            "copper_edge_clearance_mm": 0.3,
            "hole_clearance_mm": 0.25,
        },
        netclasses={"Power": {"nets": ["GND", "+3V3", "/VIN"], "track_width_mm": 0.4}},
        outline={"w": 40, "h": 30},
    )
    out["setup"] = {"ok": sc["data"]["ok"], "drc": sc["data"].get("drc")}
    sc = await ok("kw_pcb_sync", project=p)
    d = sc["data"]
    out["sync"] = {k: d.get(k) for k in ("added", "updated", "removed", "unresolved", "netlist")}
    out["sync_drc"] = d.get("drc")
    if d["unresolved"] or len(d["added"]) != 11:
        raise FlowError(f"kw_pcb_sync: {out['sync']}")
    if d["drc"] is None or d["drc"]["parity"] != 0:
        raise FlowError(f"kw_pcb_sync parity: {d.get('drc')}")
    out["stage_after_sync"] = sc["stage"]

    # placement planned from the footprints' pad geometry (kw_lib_info / the sync receipt): the
    # DIP's right column escapes to a column of 0603s at x = 37.3, RESET goes under the DIP on B.Cu
    placements: list[dict[str, Any]] = [
        {"ref": "J1", "at_mm": [4.0, 13.5]},
        {"ref": "C1", "at_mm": [5.0, 8.5], "rotation": 90},
        {"ref": "U1", "at_mm": [12.0, 8.0]},
        {"ref": "C2", "at_mm": [20.0, 8.0]},
        {"ref": "U2", "at_mm": [27.0, 14.0]},
        {"ref": "R1", "at_mm": [37.3, 14.6], "rotation": 270},
        {"ref": "R2", "at_mm": [37.3, 18.2], "rotation": 270},
        {"ref": "C3", "at_mm": [37.3, 21.9], "rotation": 270},
        {"ref": "C4", "at_mm": [37.3, 25.6], "rotation": 270},
        {"ref": "R4", "at_mm": [18.0, 19.08], "rotation": 180},
        {"ref": "D1", "at_mm": [13.0, 19.08]},
    ]
    # two calls: the unrotated parts first, then the rotated ones - on the 10.99 nightly the
    # second batch is guarded off ipc11 (G16: UpdateItems with a changed orientation leaves 1 nm
    # noise -> lib_footprint_mismatch), so the two providers are both exercised and asserted
    unrotated = [pl for pl in placements if "rotation" not in pl]
    rotated = [pl for pl in placements if "rotation" in pl]
    first = await ok("kw_pcb_place", project=p, placements=unrotated)
    sc = await ok("kw_pcb_place", project=p, placements=rotated)
    d = sc["data"]
    out["place_first"] = {"before": first["data"]["before"], "after": first["data"]["after"]}
    out["place"] = {
        "before": d["before"],
        "after": d["after"],
        "items": [i.get("description") for i in d["items"]],
    }
    sc = await ok("kw_pcb_check", project=p)
    out["check"] = sc["data"]["summary"]
    check_items = [i.get("description") for i in sc["data"].get("items", [])]
    if (
        not sc["data"]["ok"]
        or out["place"]["after"]["off_board"] != 0
        or out["place"]["after"]["courtyard_overlap"] != 0
    ):
        raise FlowError(f"placement not clean: {out['place']} {out['check']} {check_items}")
    out["stage_after_place"] = sc["stage"]

    # pads with nets and positions: what the routing is planned from (never guessed)
    sc = await ok("kw_pcb_get", project=p, kinds=["pads"], detail="summary", limit=200)
    pads: dict[tuple[str, str], dict[str, Any]] = {}
    u1_pad2: list[list[float]] = []
    for x in sc["data"]["items"]:
        key = (x["ref"], x["number"])
        if key == ("U1", "2"):
            u1_pad2.append([x["x_mm"], x["y_mm"]])
        # SOT-223's tab is a second pad "2": keep the one furthest right (the tab), where the track goes
        if key not in pads or x["x_mm"] > pads[key]["x_mm"]:
            pads[key] = x
    if len(u1_pad2) != 2:
        raise FlowError(f"U1 pad 2 + tab expected: {u1_pad2}")
    out["pads"] = {f"{r}.{n}": (x["x_mm"], x["y_mm"], x["net"]) for (r, n), x in sorted(pads.items())}

    def pad(ref: str, n: str) -> list[float]:
        x = pads[(ref, n)]
        return [x["x_mm"], x["y_mm"]]

    def net_of(ref: str, n: str) -> str:
        return str(pads[(ref, n)]["net"])

    async def pour(layers: list[str], key: str) -> None:
        sc = await ok(
            "kw_pcb_zones",
            project=p,
            zones=[{"net": "GND", "layers": layers, "rect_mm": [0, 0, 40, 30], "clearance_mm": 0.3}],
        )
        out[key] = {
            "filled": sc["data"]["filled"],
            "drc": {k: v for k, v in (sc["data"]["drc"] or {}).items() if k != "violations"},
            "provider": sc["provider_used"],
            "degraded": sc["degraded"],
        }
        if not sc["data"]["filled"] and "refill_unavailable" not in sc["degraded"]:
            raise FlowError(f"kw_pcb_zones not filled: {sc}")

    # ---- routing: by the autorouter (plane first, pour after) or by hand (pour after) --------
    if router == "auto":
        await pour(["B.Cu"], "zones_before")
        await route_auto(ok, p, out, log)
        await pour(["F.Cu"], "zones")
    else:
        await route_by_hand(ok, p, out, pads, u1_pad2)
        await pour(["F.Cu", "B.Cu"], "zones")

    sc = await ok("kw_pcb_drc", project=p, schematic_parity=True)
    d = sc["data"]
    out["drc"] = {
        k: d.get(k) for k in ("ok", "must_fix", "review", "cosmetic", "unconnected", "parity_violations")
    }
    out["drc_violations"] = [
        (
            v["type"],
            v.get("class"),
            " ".join(str(i.get("description")) for i in v.get("items", [])) or v.get("description"),
        )
        for v in d.get("violations", [])
    ][:40]
    out["stage_after_drc"] = sc["stage"]

    fab_args: dict[str, Any] = {"project": p, "targets": ["gerbers", "drill", "pos"]}
    if not d["ok"]:
        # a KiCad without zone refill leaves the pour unfilled: the GND net stays unrouted and
        # the drc_clean gate cannot be met - the agent overrides it with a reason (logged)
        fab_args["force"] = {
            "reason": f"e2e: {d['must_fix']} must_fix DRC items left (zone refill unavailable)"
        }
    out["fab_forced"] = "force" in fab_args
    sc = await ok("kw_fab_export", **fab_args)
    out["fab"] = {
        "files": sorted(Path(f).name if isinstance(f, str) else str(f) for f in _fab_files(sc["data"])),
        "unsupported": sc["data"].get("unsupported"),
    }
    sc = await ok("kw_project", action="status", project=p)
    out["status"] = {k: sc["data"].get(k) for k in ("stage", "epochs")}
    out["files"] = sorted(x.name for x in proj.iterdir() if x.is_file())
    return out


Ok = Callable[..., Awaitable[dict[str, Any]]]


async def route_by_hand(
    ok: Ok,
    p: str,
    out: dict[str, Any],
    pads: dict[tuple[str, str], dict[str, Any]],
    u1_pad2: list[list[float]],
) -> None:
    """Every non-GND connection as track primitives planned from the pad positions (never
    guessed); GND goes through the pour placed afterwards."""

    def pad(ref: str, n: str) -> list[float]:
        x = pads[(ref, n)]
        return [x["x_mm"], x["y_mm"]]

    def net_of(ref: str, n: str) -> str:
        return str(pads[(ref, n)]["net"])

    ops: list[dict[str, Any]] = []

    def track(points: list[list[float]], net: str, layer: str = "F.Cu") -> None:
        ops.append({"op": "track", "points_mm": points, "net": net, "layer": layer})

    vin, v33 = net_of("U1", "3"), net_of("U1", "2")
    disch, thres, cont = net_of("U2", "7"), net_of("U2", "6"), net_of("U2", "5")
    blink, led = net_of("U2", "3"), net_of("R4", "2")
    track([pad("J1", "1"), [pad("J1", "1")[0], pad("U1", "3")[1]], pad("U1", "3")], vin)
    track([pad("C1", "1"), [pad("C1", "1")[0], pad("U1", "3")[1]]], vin)
    # KiCad does not treat the SOT-223's two "2" pads as connected: tie them, then on to C2
    track(sorted(u1_pad2), v33)
    track([pad("U1", "2"), pad("C2", "1")], v33)
    track([pad("C2", "1"), [pad("C2", "1")[0], 10.5], [pad("U2", "8")[0], 10.5], pad("U2", "8")], v33)
    track([pad("U2", "8"), [pad("R1", "1")[0], pad("U2", "8")[1]], pad("R1", "1")], v33)
    track([pad("U2", "7"), [pad("R1", "2")[0], pad("U2", "7")[1]]], disch)
    track([pad("R1", "2"), pad("R2", "1")], disch)
    track([pad("U2", "6"), [pad("R2", "2")[0], pad("U2", "6")[1]]], thres)
    track([pad("R2", "2"), pad("C3", "1")], thres)
    track([pad("U2", "6"), [30.8, pad("U2", "6")[1]], [30.8, pad("U2", "2")[1]], pad("U2", "2")], thres)
    track([pad("U2", "5"), [pad("U2", "5")[0], pad("C4", "2")[1]], pad("C4", "2")], cont)
    track([pad("U2", "4"), [29.5, pad("U2", "4")[1]], [29.5, pad("U2", "8")[1]], pad("U2", "8")], v33, "B.Cu")
    track([pad("U2", "3"), pad("R4", "1")], blink)
    track([pad("R4", "2"), pad("D1", "2")], led)
    sc = await ok("kw_pcb_route", project=p, ops=ops)
    out["route"] = {
        "applied": len(sc["data"]["applied"]),
        "touched_nets": sc["data"]["touched_nets"],
        "drc": sc["data"]["drc"],
    }


async def wait_job(ok: Ok, tool: str, sc: dict[str, Any], budget_s: float, log: Log) -> dict[str, Any]:
    """The routing tools default to a background job: poll it the way an agent does (kw_job
    wait) and return the finished job's receipt (logged as `<tool>(result)`)."""
    job_id = (sc.get("data") or {}).get("job_id")
    if not job_id:
        return sc
    for _ in range(int(budget_s // 30) + 20):
        sc = await ok("kw_job", job_id=job_id, action="wait", wait_s=30)
        if sc["data"].get("done"):
            break
    job = sc["data"]["job"]
    if not sc["data"].get("done") or job.get("state") != "done":
        raise FlowError(f"{tool} job did not finish: {json.dumps(job, default=str)[:1500]}")
    result = job["result"] or {}
    log.add(f"{tool}(result)", result, error=False)
    return result


async def route_auto(ok: Ok, p: str, out: dict[str, Any], log: Log) -> None:
    """kw_pcb_route_auto (a background job) -> kw_pcb_fix when the router left must_fix items or
    an open connection (the designed closure for a plateau leftover, docs/ARCHITECTURE.md
    section 8). The caller pours the B.Cu GND plane before and the F.Cu pour after (pour order:
    the router sees the plane as GND's connectivity, the top pour ties the F.Cu GND pads in
    once the copper is there)."""
    sc = await ok("kw_pcb_route_auto", project=p, passes=AUTO_PASSES, timeout_s=AUTO_TIMEOUT_S)
    sc = await wait_job(ok, "kw_pcb_route_auto", sc, AUTO_TIMEOUT_S, log)
    d = sc.get("data") or {}
    out["route_auto"] = {
        "provider": sc.get("provider_used"),
        "degraded": sc.get("degraded"),
        "stage": sc.get("stage"),
        "ok": d.get("ok"),
        "engine": d.get("engine"),
        "dsn": d.get("dsn"),
        "router": {k: v for k, v in (d.get("router") or {}).items() if k not in ("log", "stdout", "stderr")},
        "routed": d.get("routed"),
        "drc": {k: v for k, v in (d.get("drc") or {}).items() if k != "violations"},
        "duration_s": d.get("duration_s"),
        "next": sc.get("next_suggested"),
    }
    rdrc = d.get("drc") or {}
    out["fix"] = None
    if rdrc.get("must_fix") or (d.get("routed") or {}).get("unrouted"):
        sc = await ok("kw_pcb_fix", project=p, max_iterations=3, passes=AUTO_PASSES, timeout_s=AUTO_TIMEOUT_S)
        sc = await wait_job(ok, "kw_pcb_fix", sc, 3 * AUTO_TIMEOUT_S, log)
        fd = sc.get("data") or {}
        out["fix"] = {
            "provider": sc.get("provider_used"),
            "ok": fd.get("ok"),
            "must_fix": fd.get("must_fix"),
            "iterations": [
                {
                    k: v
                    for k, v in it.items()
                    if k
                    in (
                        "iteration",
                        "nets",
                        "scope",
                        "ripped_up",
                        "margin_mm",
                        "must_fix_before",
                        "must_fix_after",
                    )
                }
                for it in (fd.get("iterations") or [])
            ],
            "unfixable": fd.get("unfixable"),
            "drc": {k: v for k, v in (fd.get("drc") or {}).items() if k != "violations"},
        }


def _fab_files(data: dict[str, Any]) -> list[Any]:
    files = data.get("files")
    if isinstance(files, dict):
        out: list[Any] = []
        for v in files.values():
            out.extend(v if isinstance(v, list) else [v])
        return out
    return list(files or [])


# ---- the MCP stdio conversation -------------------------------------------------------------------


def server_params(home: Path) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "kiwrap.core.cli_main", "--storage-home", str(home), "serve"],
    )


async def run_over_stdio(
    home: Path, work: Path, target: str, log: Log, router: str = "hand"
) -> dict[str, Any]:
    async with Client(server_params(home), mode="auto") as c:

        async def call(tool: str, args: dict[str, Any]) -> dict[str, Any]:
            r = await c.call_tool(tool, args)
            sc = r.structured_content or {}
            log.add(tool, sc, error=bool(r.is_error))
            if r.is_error:
                raise FlowError(
                    f"{tool} -> {sc.get('code')}: {sc.get('message')}\n  fix: {sc.get('fix')}\n  data: "
                    + json.dumps(sc.get("data"), default=str)[:800]
                )
            return sc

        return await flow(call, work, target, log, router=router)


def real_engines_or_skip() -> Any:
    """The managed JRE + Freerouting scripts/bootstrap.ps1 installed into the REAL storage home
    (the session's storage home is a throw-away directory; the server falls back to the platform
    default the same way, `discover_engines(fallback=True)`)."""
    engines = discover_engines(fallback=True)
    if not engines.available:
        pytest.skip("managed JRE / Freerouting not installed (scripts/bootstrap.ps1)")
    return engines


def _run(
    target: str, tmp_path: Path, storage_home: Path, *, router: str = "hand"
) -> tuple[dict[str, Any], Log]:
    """One scenario over MCP stdio; the user's real KiCad config of the target install is hashed
    before and after (the flow exercises more KiCad processes than any other test: kicad-cli
    ERC / netlist / DRC / exports and, on 11, headless api-servers) - it must never change."""
    from kiwrap import sandbox
    from kiwrap.installation import discover

    install = next((i for i in discover() if i.major_minor == target and not i.is_bench_build), None)
    hash_before = sandbox.user_config_hash(install) if install is not None else None
    log = Log()
    work = tmp_path / f"e2e-{target.replace('.', '_')}-{router}"
    work.mkdir()
    out: dict[str, Any] = {}
    try:
        out = anyio.run(run_over_stdio, storage_home, work, target, log, router)
    finally:
        print("\n" + log.dump())
        state = work / "blinky" / ".kiwrap"
        if state.is_dir():
            (state / "e2e-log.json").write_text(json.dumps(log.rows, indent=1, default=str), encoding="utf-8")
            (state / "e2e-out.json").write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
    if install is not None:
        assert sandbox.user_config_hash(install) == hash_before, (
            f"the e2e flow modified the user's real KiCad {target} config (%APPDATA%/kicad)"
        )
    brief = {
        k: v
        for k, v in out.items()
        if k
        in (
            "stats",
            "erc",
            "erc_violations",
            "annotate",
            "sync",
            "place",
            "check",
            "zones_before",
            "route_auto",
            "fix",
            "zones",
            "drc",
            "drc_violations",
            "fab",
            "status",
        )
    }
    print(json.dumps(brief, indent=1, default=str)[:6000])
    return out, log


def _assert_common(out: dict[str, Any], log: Log, *, refill: bool = True) -> None:
    """The numbers every scenario must reach; `refill=False` for a KiCad without kicad-cli zone
    refill (the pour stays unfilled, GND stays unrouted, fab export needs force)."""
    assert not any(r["error"] for r in log.rows), log.dump()
    assert out["erc"] == {"ok": True, "errors": 0, "warnings": out["erc"]["warnings"]}
    assert out["erc"]["warnings"] == 0, out["erc_violations"]
    assert out["stats"]["dangling_ends"] == 0 and out["stats"]["unconnected_pins"] == 0
    assert out["stats"]["label_only_nets"] == 0 and out["stats"]["components"] == 11
    assert out["stage_after_check"] == "erc_clean"
    assert out["unresolved"] == []
    assert (
        len(out["sync"]["added"]) == 11 and out["sync"]["removed"] == [] and out["sync"]["unresolved"] == []
    )
    assert out["sync_drc"]["parity"] == 0
    assert out["stage_after_sync"] == "synced"
    assert out["place"]["after"] == {
        "courtyard_overlap": 0,
        "off_board": 0,
        "off_board_copper": 0,
        "unrouted": out["place"]["after"]["unrouted"],
    }
    assert out["stage_after_place"] == "placed"
    assert out["drc"]["parity_violations"] == 0
    if refill:
        assert out["zones"]["filled"] is True
        assert out["drc"]["must_fix"] == 0 and out["drc"]["ok"] is True, out["drc_violations"]
        assert out["stage_after_drc"] == "drc_clean"
        assert out["fab_forced"] is False
    else:
        assert out["zones"]["filled"] is False and "refill_unavailable" in out["zones"]["degraded"]
        # everything but the pour is routed: the only must_fix items are GND's unconnected pads
        assert {v[0] for v in out["drc_violations"] if v[1] == "must_fix"} == {"unconnected_items"}
        assert all("[GND]" in v[2] for v in out["drc_violations"] if v[0] == "unconnected_items"), out[
            "drc_violations"
        ]
        assert out["stage_after_drc"] == "placed" and out["fab_forced"] is True
    names = " ".join(out["fab"]["files"])
    assert ".gbr" in names or "gerber" in names.lower(), out["fab"]
    assert ".drl" in names, out["fab"]
    assert "pos" in names, out["fab"]
    assert out["status"]["stage"] == "exported"
    assert "blinker.kicad_sch" in out["files"]


@pytest.mark.kicad_version("10.99")
def test_e2e_blinker_on_the_kicad_11_preview_over_stdio(tmp_path: Path, storage_home: Path):
    """10.99 nightly: every schematic / board write that has an IPC twin is answered by ipc11
    (the headless api-server); sheet creation and footprint assignment / board setup by the file
    engine; kicad-cli 10.99 validates; no api-server survives."""
    out, log = _run("10.99", tmp_path, storage_home)
    _assert_common(out, log)
    by_tool: dict[str, list[dict[str, Any]]] = {}
    for r in log.rows:
        by_tool.setdefault(r["tool"], []).append(r)
    for tool in (
        "kw_sch_add_symbol",
        "kw_sch_connect",
        "kw_sch_annotate",
        "kw_pcb_sync",
        "kw_pcb_route",
        "kw_pcb_zones",
    ):
        rows = by_tool[tool]
        assert all(str(r["provider_used"]).startswith("ipc11:") for r in rows), (
            tool,
            [r["provider_used"] for r in rows],
        )
        assert all("ipc11_unavailable" not in (r["degraded"] or []) for r in rows), tool
    # placement: the unrotated batch is served by ipc11; the rotated batch is guarded off it on
    # this build (docs/UPSTREAM.md G16, capability ipc.footprint_update_rotation_noise) and the
    # receipt says so - never a silent fallback
    place_rows = by_tool["kw_pcb_place"]
    assert len(place_rows) == 2, place_rows
    assert str(place_rows[0]["provider_used"]).startswith("ipc11:") and "ipc11_guarded" not in (
        place_rows[0]["degraded"] or []
    ), place_rows[0]
    assert place_rows[1]["provider_used"] == "file" and "ipc11_guarded" in (
        place_rows[1]["degraded"] or []
    ), place_rows[1]
    # the placements added no lib_footprint_mismatch (the file engine rotated the parts exactly,
    # the ipc11 moves are exact): whatever the final DRC lists was already there right after
    # ImportNetlist (the nightly's own instantiation of SOT-223-3_TabPin2 drops the `${REFERENCE}`
    # text rotation and the polygon's closing point - a KiCad import quirk, docs/UPSTREAM.md G16 note)
    after_sync = {
        i.get("description")
        for v in (out["sync_drc"] or {}).get("violations", [])
        if v.get("type") == "lib_footprint_mismatch"
        for i in v.get("items", [])
    }
    final = {v[2] for v in out["drc_violations"] if v[0] == "lib_footprint_mismatch"}
    assert final <= after_sync, (final, after_sync)
    # the file engine answers what the twins cannot (sheet creation) and says so; every other
    # item (power, pwr_flag, labels) came from ipc11
    items = by_tool["kw_sch_add_item"]
    sheet_rows = [r for r in items if r["item_type"] == "sheet"]
    assert len(sheet_rows) == 1 and sheet_rows[0]["provider_used"] == "file", sheet_rows
    assert "ipc11_unavailable" in (sheet_rows[0]["degraded"] or [])
    assert all(str(r["provider_used"]).startswith("ipc11:") for r in items if r["item_type"] != "sheet"), (
        items
    )
    assert all(r["provider_used"] == "file" for r in by_tool["kw_pcb_setup"])
    assert all(r["provider_used"] == "file" for r in by_tool["kw_sch_assign_footprints"])
    # KiCad's own validators: kicad-cli of the same install
    assert all(
        str(r["provider_used"]).startswith("cli:10.99")
        for r in by_tool["kw_sch_check"] + by_tool["kw_pcb_drc"]
    )
    _no_api_server_left()


@pytest.mark.kicad_version("9.0")
def test_e2e_blinker_on_kicad_9_best_effort(tmp_path: Path, storage_home: Path):
    """9.0.9 (best-effort tier): the same conversation through the file engine and kicad-cli 9,
    which has no zone refill (`cli.drc_refill_flags`): the receipt says `refill_unavailable`, the
    GND pour stays unfilled and the agent has to force the fab export."""
    out, log = _run("9.0", tmp_path, storage_home)
    _assert_common(out, log, refill=False)
    writes = [r for r in log.rows if r["tool"] in WRITE_TOOLS]
    assert writes and all(r["provider_used"] == "file" for r in writes), [
        (r["tool"], r["provider_used"]) for r in writes
    ]


@pytest.mark.kicad_version("10.0")
def test_e2e_blinker_on_kicad_10_over_stdio(tmp_path: Path, storage_home: Path):
    """10.0.6: every write is answered by the file engine (no headless IPC on 10.0); kicad-cli
    10.0.6 validates (ERC, netlist, DRC with parity, refill)."""
    out, log = _run("10.0", tmp_path, storage_home)
    _assert_common(out, log)
    writes = [r for r in log.rows if r["tool"] in WRITE_TOOLS]
    assert writes and all(r["provider_used"] == "file" for r in writes), [
        (r["tool"], r["provider_used"]) for r in writes
    ]
    assert all("ipc11_unavailable" not in (r["degraded"] or []) for r in writes)


def _assert_autorouted(out: dict[str, Any], log: Log) -> None:
    """The autorouted variant's numbers: Freerouting ran from the managed engines through
    kiwrap's codec (`provider_used: codec` on a stock install), the mandatory refill + DRC judged
    it, kw_pcb_fix closed whatever the router left, and the final DRC is 0 must_fix / 0
    unconnected without forcing the fab gate."""
    _assert_common(out, log)
    assert out["routing_mode"] == "auto"
    assert out["zones_before"]["filled"] is True
    ra = out["route_auto"]
    assert ra["provider"] == "codec", ra["provider"]
    assert ra["engine"]["name"] == "freerouting" and ra["engine"]["version"], ra["engine"]
    assert ra["router"]["ok"] is True and ra["router"]["exit_code"] == 0, ra["router"]
    assert ra["router"]["passes"] >= 1 and ra["router"]["history"], ra["router"]
    assert ra["dsn"]["provider"] == "codec" and ra["dsn"]["nets"] >= 7, ra["dsn"]
    assert ra["routed"]["tracks_added"] > 0, ra["routed"]
    assert ra["drc"]["refilled"] is True, ra["drc"]
    assert ra["stage"] in ("placed", "drc_clean")
    if ra["drc"]["must_fix"] or ra["routed"]["unrouted"]:
        assert ra["next"] == "kw_pcb_fix" and out["fix"] is not None, ra
        assert out["fix"]["ok"] is True and out["fix"]["must_fix"]["after"] == 0, out["fix"]
    else:
        assert out["fix"] is None
    assert out["drc"]["unconnected"] == 0 and out["drc"]["must_fix"] == 0, out["drc_violations"]
    route_rows = [r for r in log.rows if r["tool"] == "kw_pcb_route_auto"]
    assert route_rows and route_rows[0]["provider_used"] == "jobs", route_rows
    assert any(r["tool"] == "kw_job" for r in log.rows)
    assert not any(r["tool"] == "kw_pcb_route" for r in log.rows)


@pytest.mark.kicad_version("10.99")
def test_e2e_blinker_autorouted_on_the_kicad_11_preview_over_stdio(tmp_path: Path, storage_home: Path):
    """The same conversation with kw_pcb_route_auto instead of hand routing on the 10.99 nightly:
    the board writes stay ipc11, the DSN/SES round trip is kiwrap's codec (the stock nightly has
    no SWIG and no `pcb export specctra`), kicad-cli 10.99 refills and judges."""
    real_engines_or_skip()
    out, log = _run("10.99", tmp_path, storage_home, router="auto")
    _assert_autorouted(out, log)
    by_tool: dict[str, list[dict[str, Any]]] = {}
    for r in log.rows:
        by_tool.setdefault(r["tool"], []).append(r)
    for tool in ("kw_sch_add_symbol", "kw_sch_connect", "kw_sch_annotate", "kw_pcb_sync", "kw_pcb_zones"):
        assert all(str(r["provider_used"]).startswith("ipc11:") for r in by_tool[tool]), tool
    assert all(
        str(r["provider_used"]).startswith("cli:10.99")
        for r in by_tool["kw_sch_check"] + by_tool["kw_pcb_drc"]
    )
    _no_api_server_left()
    _no_jvm_left()


@pytest.mark.kicad_version("10.0")
def test_e2e_blinker_autorouted_on_kicad_10_over_stdio(tmp_path: Path, storage_home: Path):
    """10.0.6 with kw_pcb_route_auto: file-engine writes, kiwrap's codec (ranked ahead of the
    driverless SWIG provider), kicad-cli 10.0.6 refills and judges."""
    real_engines_or_skip()
    out, log = _run("10.0", tmp_path, storage_home, router="auto")
    _assert_autorouted(out, log)
    writes = [r for r in log.rows if r["tool"] in WRITE_TOOLS and r["tool"] not in ("kw_pcb_route_auto",)]
    assert writes and all(r["provider_used"] == "file" for r in writes), [
        (r["tool"], r["provider_used"]) for r in writes
    ]
    _no_jvm_left()


def _no_jvm_left() -> None:
    """PROCESS HYGIENE: no Freerouting JVM of the managed engines survives the conversation
    (the router kills its process tree on timeout / cancel and waits for it otherwise)."""
    import subprocess

    ps = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            "Get-CimInstance Win32_Process -Filter \"Name='java.exe'\" | Select-Object -ExpandProperty CommandLine",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    ours = [ln for ln in ps.stdout.splitlines() if "blinky" in ln.lower() and "freerouting" in ln.lower()]
    assert not ours, ours


def _no_api_server_left() -> None:
    """PROCESS HYGIENE: no `kicad-cli api-server` on the e2e project survives the conversation
    (every mutation closes its session before kicad-cli validates). Other kicad-cli processes may
    belong to concurrent tests, so only command lines naming this project count (Windows, D18)."""
    import subprocess

    ps = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            "Get-CimInstance Win32_Process -Filter \"Name='kicad-cli.exe'\" | "
            "Select-Object -ExpandProperty CommandLine",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    servers = [ln for ln in ps.stdout.splitlines() if "api-server" in ln and "blinky" in ln]
    assert not servers, servers
