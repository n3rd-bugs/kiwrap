"""The D5 reference board as a scripted MCP stdio conversation (docs/DECISIONS.md D5; the eval
harness in evals/reference_board): an agent following AGENTS.md builds the ESP32-S3 dev board
from `evals/reference_board/design_plan.json` (29 parts, three child sheets, JLCPCB rules) from
nothing to fabrication files, with KiCad as the judge at every step, and the run is graded by
`evals/reference_board/grader.py` (ERC 0, intent diff empty, unrouted 0, must_fix 0, fab bundle).

    kw_project create -> [kw_intent set] -> kw_lib_search / kw_lib_info
    -> kw_sch_add_item sheet x3 (power, mcu, io with hierarchical pins)
    -> kw_sch_add_symbol x29 (pins from the receipts, checked against the plan's transform)
    -> kw_sch_connect (wires by (ref, pin) + explicit corners) / kw_sch_add_item (power, pwr_flag,
       no_connect x34, hierarchical_label x16, root labels)
    -> kw_sch_stats -> kw_sch_check (ERC 0) -> kw_sch_get netlist == intent
    -> kw_sch_annotate -> kw_sch_assign_footprints (mapping from the plan; nothing unresolved)
    -> kw_pcb_setup (preset jlcpcb-2layer when the server has presets, else the same rules)
    -> kw_pcb_sync (parity 0) -> kw_pcb_place from the plan -> kw_pcb_check (0 overlaps, 0 copper
       off-board; the module's antenna keep-out and the USB-C overhang are review items)
    -> kw_pcb_zones (the B.Cu GND plane the router works against)
    -> kw_pcb_route_auto when the server has it (+ kw_pcb_fix when the router leaves an open
       connection or a violation), else kw_pcb_route primitives for the power nets (the rest is
       expected-unrouted and the test SKIPS after grading the other stages)
    -> kw_pcb_zones (the F.Cu pour) -> kw_pcb_drc (parity) -> kw_fab_export (jlcpcb, else
       generic) -> grader

One scenario per primary install (10.99 nightly -> ipc11 writes, 10.0.6 -> file writes); the
capability probes are `tools/list` and each tool's input schema, never the version number. Every
call's provider / degraded / stage is logged to `<project>/.kiwrap/e2e-log.json` and printed
(`-s`). The fast tests at the bottom (plan sync, grader rubric) need no KiCad.
"""

from __future__ import annotations

import json
import math
import os
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import anyio
import pytest
from evals.reference_board import grader
from evals.reference_board.plan import PLAN_PATH, build_plan, expected_pins, load_plan
from mcp import Client, StdioServerParameters
from mcp.client.stdio import get_default_environment

Call = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]

WRITE_TOOLS = (
    "kw_sch_add_symbol",
    "kw_sch_connect",
    "kw_sch_add_item",
    "kw_sch_annotate",
    "kw_sch_assign_footprints",
    "kw_pcb_setup",
    "kw_pcb_sync",
    "kw_pcb_place",
    "kw_pcb_zones",
    "kw_pcb_route",
)


class FlowError(AssertionError):
    pass


class Log:
    """Every tool call with what the agent reads first: provider, degraded, stage, validators."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def add(self, tool: str, sc: dict[str, Any], *, error: bool) -> None:
        self.rows.append(
            {
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
        )

    def count(self, tool: str) -> int:
        return sum(1 for r in self.rows if r["tool"] == tool)

    def dump(self) -> str:
        lines = []
        for r in self.rows:
            v = ", ".join(f"{x['name']} {x['before']}->{x['after']}" for x in r["validators"])
            lines.append(
                f"{'!!' if r['error'] else 'ok'} {r['tool']:<26} provider={r['provider_used']!s:<14} "
                f"degraded={r['degraded']} stage={r['stage']} [{v}]"
            )
        return "\n".join(lines)


# ---- the conversation ----------------------------------------------------------------------------------


def _close(a: list[float], b: list[float], tol: float = 1e-3) -> bool:
    return math.isclose(a[0], b[0], abs_tol=tol) and math.isclose(a[1], b[1], abs_tol=tol)


async def flow(
    call: Call, tools: dict[str, dict[str, Any]], work: Path, target: str, log: Log, *, router: bool = True
) -> dict[str, Any]:
    """The agent's conversation over the plan. `call(tool, args)` returns the structured content
    (a teaching error raises FlowError); `tools` is tools/list as {name: input schema}; `router`
    says whether the server can reach the managed Freerouting engines."""
    plan = load_plan()
    out: dict[str, Any] = {"capabilities": {}}
    proj = work / plan["name"]
    p = str(proj)
    want_pins = expected_pins()

    def has_tool(name: str) -> bool:
        return name in tools

    def has_arg(tool: str, arg: str) -> bool:
        return arg in ((tools.get(tool) or {}).get("properties") or {})

    out["capabilities"] = {
        "kw_intent": has_tool("kw_intent"),
        "kw_pcb_route_auto": has_tool("kw_pcb_route_auto"),
        "engines": router,
        "pcb_setup.preset": has_arg("kw_pcb_setup", "preset"),
    }

    # ---- project + intent ---------------------------------------------------------------------------
    sc = await call(
        "kw_project",
        {
            "action": "create",
            "dir": p,
            "name": plan["name"],
            "target_kicad": target,
            "board_size_mm": [plan["board"]["w_mm"], plan["board"]["h_mm"]],
        },
    )
    out["create"] = sc["data"]
    if has_tool("kw_intent"):
        sc = await call("kw_intent", {"project": p, "action": "set", "intent": plan["intent"]})
        out["intent_set"] = {"provider": sc.get("provider_used"), "data": sc.get("data")}
    else:
        out["intent_set"] = None  # contract deviation: the intent is checked by the grader instead

    # ---- library lookups: never guess a lib_id ------------------------------------------------------
    sc = await call("kw_lib_search", {"kind": "symbol", "query": "ESP32-S3-WROOM-1", "project": p})
    hits = [h["id"] for h in sc["data"]["hits"]]
    if "RF_Module:ESP32-S3-WROOM-1" not in hits[:5]:
        raise FlowError(f"kw_lib_search ESP32-S3-WROOM-1: {hits[:5]}")
    sc = await call("kw_lib_info", {"id": "RF_Module:ESP32-S3-WROOM-1", "project": p})
    esp_pins = {x["number"]: x["name"] for x in sc["data"]["item"]["pins"]}
    if len(esp_pins) != 41 or esp_pins.get("3") != "EN" or esp_pins.get("14") != "USB_D+":
        raise FlowError(f"kw_lib_info ESP32 pins: {len(esp_pins)} {esp_pins.get('3')} {esp_pins.get('14')}")
    for query, want in (
        ("USB_C_Receptacle_USB2.0_16P", "Connector:USB_C_Receptacle_USB2.0_16P"),
        ("MCP73831", "Battery_Management:MCP73831-2-OT"),
        ("AMS1117-3.3", "Regulator_Linear:AMS1117-3.3"),
        ("USBLC6-2SC6", "Power_Protection:USBLC6-2SC6"),
    ):
        sc = await call("kw_lib_search", {"kind": "symbol", "query": query, "project": p})
        hits = [h["id"] for h in sc["data"]["hits"]]
        if want not in hits[:8]:
            raise FlowError(f"kw_lib_search {query}: {hits[:8]}")
    sc = await call("kw_lib_search", {"kind": "footprint", "query": "HRO TYPE-C-31-M-12", "project": p})
    hits = [h["id"] for h in sc["data"]["hits"]]
    if "Connector_USB:USB_C_Receptacle_HRO_TYPE-C-31-M-12" not in hits[:8]:
        raise FlowError(f"kw_lib_search USB-C footprint: {hits[:8]}")

    # ---- the hierarchy: three child sheets on the root -----------------------------------------------
    sheet_pins: dict[str, dict[str, list[float]]] = {}
    for s in plan["sheets"]:
        sc = await call(
            "kw_sch_add_item",
            {
                "project": p,
                "item": {
                    "type": "sheet",
                    "text": s["name"],
                    "file": s["file"],
                    "at_mm": s["at_mm"],
                    "size_mm": s["size_mm"],
                    "pins": s["pins"],
                },
                "erc": False,
            },
        )
        sheet_pins[s["name"]] = {sp["name"]: [sp["x_mm"], sp["y_mm"]] for sp in sc["data"]["pins"]}
        if set(sheet_pins[s["name"]]) != {sp["name"] for sp in s["pins"]}:
            raise FlowError(f"sheet {s['name']} pins: {sc['data']}")
    out["sheet_pins"] = sheet_pins

    # ---- symbols (the receipts' absolute pins are the plan's transform) ---------------------------------
    pins: dict[str, dict[str, list[float]]] = {}
    for part in plan["parts"]:
        sc = await call(
            "kw_sch_add_symbol",
            {
                "project": p,
                "lib_id": part["lib_id"],
                "ref": part["ref"],
                "value": part["value"],
                "at_mm": part["at_mm"],
                "rotation": part["rotation"],
                "sheet": part["sheet"],
                "erc": False,
            },
        )
        d = sc["data"]
        if d["ref"] != part["ref"] or not d["pins"]:
            raise FlowError(f"kw_sch_add_symbol {part['ref']}: {d}")
        pins[part["ref"]] = {x["number"]: [x["x_mm"], x["y_mm"]] for x in d["pins"]}
        for number, want in want_pins[part["ref"]].items():
            got = pins[part["ref"]].get(number)
            if got is None or not _close(got, want):
                raise FlowError(f"{part['ref']} pin {number}: receipt {got} != plan {want}")
    out["symbols"] = len(pins)

    # ---- wires, power, flags, no-connects, hierarchical labels ---------------------------------------
    for w in plan["wires"]:
        sc = await call(
            "kw_sch_connect",
            {"project": p, "endpoints": w["points"], "route": "direct", "sheet": w["sheet"], "erc": False},
        )
        if not sc["data"]["wires"]:
            raise FlowError(f"kw_sch_connect ({w['sheet']}) no wires: {w['points']} -> {sc['data']}")
    for pw in plan["power"]:
        item = {"type": "power", "net": pw["net"], "rotation": pw["rotation"]}
        item.update({k: pw[k] for k in ("ref", "pin", "at_mm") if k in pw})
        sc = await call("kw_sch_add_item", {"project": p, "item": item, "sheet": pw["sheet"], "erc": False})
        if not str(sc["data"].get("ref") or "").startswith("#PWR"):
            raise FlowError(f"power {pw}: {sc['data']}")
    for fl in plan["pwr_flags"]:
        item = {"type": "pwr_flag", "net": fl["net"], "rotation": fl["rotation"]}
        item.update({k: fl[k] for k in ("ref", "pin", "at_mm") if k in fl})
        sc = await call("kw_sch_add_item", {"project": p, "item": item, "sheet": fl["sheet"], "erc": False})
        if not str(sc["data"].get("ref") or "").startswith("#FLG"):
            raise FlowError(f"pwr_flag {fl}: {sc['data']}")
    nc_count = 0
    for group in plan["no_connects"]:
        for number in group["pins"]:
            item = {"type": "no_connect", "ref": group["ref"], "pin": number}
            sc = await call(
                "kw_sch_add_item", {"project": p, "item": item, "sheet": group["sheet"], "erc": False}
            )
            if not sc["data"].get("uuid"):
                raise FlowError(f"no_connect {group['ref']}.{number}: {sc['data']}")
            nc_count += 1
    out["no_connects"] = nc_count
    for hl in plan["hier_labels"]:
        item = {
            "type": "hierarchical_label",
            "text": hl["text"],
            "kind": hl["kind"],
            "at_mm": hl["at_mm"],
            "rotation": hl["rotation"],
        }
        sc = await call("kw_sch_add_item", {"project": p, "item": item, "sheet": hl["sheet"], "erc": False})
        if not sc["data"].get("uuid"):
            raise FlowError(f"hierarchical_label {hl}: {sc['data']}")
    # root: sheet pin to sheet pin, a local label on each wire (names the net /USB_DP, /EN, ...)
    for rw in plan["root_wires"]:
        a = sheet_pins[rw["from"][0]][rw["from"][1]]
        b = sheet_pins[rw["to"][0]][rw["to"][1]]
        sc = await call(
            "kw_sch_connect",
            {"project": p, "endpoints": [{"at_mm": a}, {"at_mm": b}], "route": "direct", "erc": False},
        )
        if not sc["data"]["wires"]:
            raise FlowError(f"root wire {rw}: {sc['data']}")
        mid = [round((a[0] + b[0]) / 2 / 1.27) * 1.27, a[1]]
        sc = await call(
            "kw_sch_add_item",
            {"project": p, "item": {"type": "label", "text": rw["label"], "at_mm": mid}, "erc": False},
        )
        if not sc["data"].get("uuid"):
            raise FlowError(f"root label {rw['label']}: {sc['data']}")

    # ---- schematic validation: stats, ERC, the intent ------------------------------------------------
    sc = await call("kw_sch_stats", {"project": p})
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
    sc = await call("kw_sch_check", {"project": p})
    out["erc"] = {k: sc["data"].get(k) for k in ("ok", "errors", "warnings")}
    out["erc_violations"] = [
        (v["type"], v.get("severity"), v.get("description")) for v in sc["data"].get("violations", [])
    ][:40]
    out["stage_after_check"] = sc["stage"]
    # with an intent declared, kw_sch_check carries KiCad's own intent diff (validator intent_diff)
    out["check_intent_diff"] = sc["data"].get("intent_diff")
    if not sc["data"]["ok"] or sc["data"]["errors"] != 0:
        raise FlowError(f"kw_sch_check: {out['erc']} {out['erc_violations']}")
    if out["intent_set"] is not None and (out["check_intent_diff"] or {}).get("count", 0) != 0:
        raise FlowError(f"kw_sch_check intent diff: {json.dumps(out['check_intent_diff'], indent=1)[:3000]}")
    sc = await call("kw_sch_get", {"project": p, "kinds": ["netlist"], "detail": "normal"})
    nets = grader.nets_from_netlist(sc["data"]["netlist"])
    out["nets"] = {k: sorted(f"{x['ref']}.{x['pin']}" for x in v) for k, v in nets.items()}
    diff = grader.intent_diff(nets, plan["intent"])
    out["intent_diff"] = diff.as_dict()
    if not diff.ok:
        raise FlowError(
            f"intent diff: {json.dumps(diff.as_dict(), indent=1)}\nnets: {json.dumps(out['nets'], indent=1)}"
        )

    # ---- annotate + footprints ------------------------------------------------------------------------
    sc = await call("kw_sch_annotate", {"project": p})
    out["annotate"] = sc["data"]["mapping"]
    sc = await call(
        "kw_sch_assign_footprints",
        {"project": p, "mapping": {part["ref"]: part["fpid"] for part in plan["parts"]}},
    )
    out["assigned"] = sc["data"]["assigned"]
    out["unresolved"] = sc["data"]["unresolved"]
    if sc["data"]["unresolved"] or not sc["data"]["ok"]:
        raise FlowError(f"kw_sch_assign_footprints unresolved: {sc['data']['unresolved']}")
    if sc["data"]["erc"]["errors"] != 0:
        raise FlowError(f"ERC after assign: {sc['data']['erc']}")

    # ---- board setup (preset when the server has it, the same numbers otherwise) ---------------------
    board = plan["board"]
    setup_args: dict[str, Any] = {
        "project": p,
        "netclasses": board["netclasses"],
        "outline": {"w": board["w_mm"], "h": board["h_mm"]},
    }
    if has_arg("kw_pcb_setup", "preset"):
        setup_args["preset"] = board["preset"]  # the vendor's layers + rules (explicit keys would win)
    else:
        setup_args["layers"] = board["layers"]
        setup_args["rules"] = board["rules"]  # the same numbers, spelled out
    sc = await call("kw_pcb_setup", setup_args)
    out["setup"] = {
        "ok": sc["data"]["ok"],
        "preset": setup_args.get("preset"),
        "rules_now": sc["data"].get("rules_now"),
        "warnings": sc.get("warnings"),
    }
    rules_now = sc["data"].get("rules_now") or {}
    for key in ("min_hole_mm", "copper_edge_clearance_mm", "min_track_mm"):
        if key in rules_now and not math.isclose(float(rules_now[key]), board["rules"][key], abs_tol=1e-6):
            raise FlowError(
                f"board rule {key}: {rules_now[key]} != plan {board['rules'][key]} ({out['setup']})"
            )
    sc = await call("kw_pcb_sync", {"project": p})
    d = sc["data"]
    out["sync"] = {k: d.get(k) for k in ("added", "updated", "removed", "unresolved")}
    out["sync_drc"] = d.get("drc")
    if d["unresolved"] or len(d["added"]) != len(plan["parts"]):
        raise FlowError(f"kw_pcb_sync: {out['sync']}")
    if d["drc"] is None or d["drc"]["parity"] != 0:
        raise FlowError(f"kw_pcb_sync parity: {d.get('drc')}")
    out["stage_after_sync"] = sc["stage"]

    # ---- placement from the plan, checked by kw_pcb_check ------------------------------------------------
    placements = [
        {"ref": part["ref"], "at_mm": part["place"]["at_mm"], "rotation": part["place"]["rotation"]}
        for part in plan["parts"]
    ]
    sc = await call("kw_pcb_place", {"project": p, "placements": placements})
    d = sc["data"]
    out["place"] = {
        "before": d["before"],
        "after": d["after"],
        "provider": sc["provider_used"],
        "degraded": sc["degraded"],
    }
    sc = await call("kw_pcb_check", {"project": p})
    out["check"] = sc["data"]["summary"]
    check_items = sc["data"].get("items", [])
    out["check_items"] = [(i.get("check"), i.get("severity"), i.get("description")) for i in check_items][:40]
    off_board_review = sorted(
        {r for i in check_items if i.get("check") == "off_board" for r in i.get("refs", [])}
    )
    out["off_board_review"] = off_board_review
    if (
        not sc["data"]["ok"]
        or out["check"]["courtyard_overlap"] != 0
        or out["check"]["off_board_copper"] != 0
        or off_board_review != sorted(board["expected_off_board_review"])
    ):
        raise FlowError(f"placement not clean: {out['check']} {out['check_items']}")
    out["stage_after_place"] = sc["stage"]

    # pads with nets and positions: what the routing is planned from (never guessed)
    pads: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        args: dict[str, Any] = {"project": p, "kinds": ["pads"], "detail": "summary", "limit": 200}
        if cursor:
            args["cursor"] = cursor
        sc = await call("kw_pcb_get", args)
        pads.extend(sc["data"]["items"])
        cursor = sc["data"].get("next_cursor")
        if not cursor:
            break
    out["pad_count"] = len(pads)

    def resolve(point: dict[str, Any]) -> list[float]:
        if "at_mm" in point:
            return list(point["at_mm"])
        ref, number = point["pad"]
        near = point["near"]
        cands = [x for x in pads if x["ref"] == ref and str(x["number"]) == str(number)]
        if not cands:
            raise FlowError(f"pad {ref}.{number} not on the board")
        best = min(cands, key=lambda x: (x["x_mm"] - near[0]) ** 2 + (x["y_mm"] - near[1]) ** 2)
        got = [best["x_mm"], best["y_mm"]]
        if not _close(got, near, tol=0.05):
            raise FlowError(f"pad {ref}.{number}: board {got} != plan {near}")
        return got

    # ---- the bottom GND plane, then routing: the autorouter (+ repair loop), else by hand -------------
    async def pour(key: str) -> None:
        sc = await call("kw_pcb_zones", {"project": p, "zones": board[key]})
        out.setdefault("zones", {})[key] = {
            "filled": sc["data"]["filled"],
            "drc": {k: v for k, v in (sc["data"].get("drc") or {}).items() if k != "violations"},
            "provider": sc["provider_used"],
            "degraded": sc["degraded"],
        }
        if not sc["data"]["filled"] and "refill_unavailable" not in sc["degraded"]:
            raise FlowError(f"kw_pcb_zones {key} not filled: {sc}")

    await pour("zones_before_routing")

    async def wait_job(tool: str, sc: dict[str, Any], budget_s: float) -> dict[str, Any]:
        """The default of the routing tools is a background job: poll it the way an agent would
        (kw_job wait) and return the finished job's receipt."""
        job_id = (sc.get("data") or {}).get("job_id")
        if not job_id:
            return sc
        for _ in range(int(budget_s // 30) + 20):
            sc = await call("kw_job", {"job_id": job_id, "action": "wait", "wait_s": 30})
            if sc["data"].get("done"):
                break
        job = sc["data"]["job"]
        if not sc["data"].get("done") or job.get("state") != "done":
            raise FlowError(f"{tool} job did not finish: {json.dumps(job, default=str)[:1500]}")
        result = job["result"] or {}
        log.add(f"{tool}(result)", result, error=False)
        return result

    routing = plan["routing"]
    if has_tool("kw_pcb_route_auto") and router:
        auto = routing["auto"]
        sc = await call(
            "kw_pcb_route_auto",
            {"project": p, "passes": auto["passes"], "timeout_s": auto["timeout_s"]},
        )
        sc = await wait_job("kw_pcb_route_auto", sc, auto["timeout_s"])
        d = sc.get("data") or {}
        out["route_auto"] = {
            "provider": sc.get("provider_used"),
            "degraded": sc.get("degraded"),
            "stage": sc.get("stage"),
            "ok": d.get("ok"),
            "engine": d.get("engine"),
            "dsn": {k: v for k, v in (d.get("dsn") or {}).items() if k != "path"},
            "rules": d.get("rules"),
            "router": {
                k: v for k, v in (d.get("router") or {}).items() if k not in ("log", "stdout", "stderr")
            },
            "routed": d.get("routed"),
            "drc": {k: v for k, v in (d.get("drc") or {}).items() if k != "violations"},
            "duration_s": d.get("duration_s"),
            "next": sc.get("next_suggested"),
        }
        out["routing_mode"] = "auto"
        # what the router left (an open connection, a router-introduced clearance): the repair
        # loop rips up the violating nets and re-routes them until must_fix == 0 (section 8)
        rdrc = d.get("drc") or {}
        out["fix"] = None
        if has_tool("kw_pcb_fix") and (rdrc.get("must_fix") or (d.get("routed") or {}).get("unrouted")):
            sc = await call(
                "kw_pcb_fix",
                {"project": p, "max_iterations": 3, "passes": auto["passes"], "timeout_s": auto["timeout_s"]},
            )
            sc = await wait_job("kw_pcb_fix", sc, 3 * auto["timeout_s"])
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
    else:
        ops: list[dict[str, Any]] = []
        for op in routing["fallback_ops"]:
            if op["op"] == "track":
                ops.append(
                    {
                        "op": "track",
                        "net": op["net"],
                        "layer": op["layer"],
                        "points_mm": [resolve(pt) for pt in op["points"]],
                    }
                )
            else:
                ops.append({"op": "via", "net": op["net"], "at_mm": op["at_mm"]})
        sc = await call("kw_pcb_route", {"project": p, "ops": ops})
        out["route"] = {
            "applied": len(sc["data"]["applied"]),
            "touched_nets": sc["data"]["touched_nets"],
            "drc": sc["data"]["drc"],
            "provider": sc["provider_used"],
        }
        out["routing_mode"] = "fallback_power_nets"

    # the top pour once the copper is in (see the plan's note on pour order)
    await pour("zones_after_routing")

    sc = await call("kw_pcb_drc", {"project": p, "schematic_parity": True})
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
    ][:60]
    out["stage_after_drc"] = sc["stage"]
    sc = await call("kw_pcb_check", {"project": p, "checks": ["unrouted"]})
    out["unrouted"] = sc["data"]["summary"].get("unrouted")
    out["unrouted_nets"] = sorted(
        {str(i.get("net")) for i in sc["data"].get("items", []) if i.get("check") == "unrouted"}
    )

    # ---- fabrication --------------------------------------------------------------------------------------
    fab = plan["fab"]
    fab_args: dict[str, Any] = {"project": p, "targets": fab["targets"], "profile": fab["profile"]}
    if not out["drc"]["ok"]:
        fab_args["force"] = {
            "reason": f"e2e: {out['drc']['must_fix']} must_fix DRC items left ({out['routing_mode']})"
        }
    out["fab_forced"] = "force" in fab_args
    try:
        sc = await call("kw_fab_export", fab_args)
        out["fab_profile"] = fab["profile"]
    except FlowError as err:
        if "E_UNSUPPORTED" not in str(err) or "profile" not in str(err):
            raise
        # contract deviation: the jlcpcb profile is not on this server yet -> generic outputs
        fab_args["profile"] = "generic"
        sc = await call("kw_fab_export", fab_args)
        out["fab_profile"] = "generic"
    out["fab"] = {
        "files": sorted(Path(f).name if isinstance(f, str) else str(f) for f in _fab_files(sc["data"])),
        "unsupported": sc["data"].get("unsupported"),
    }
    sc = await call("kw_project", {"action": "status", "project": p})
    out["status"] = {k: sc["data"].get(k) for k in ("stage", "epochs")}
    out["files"] = sorted(x.name for x in proj.iterdir() if x.is_file())
    out["fab_files_on_disk"] = grader.fab_files_under(proj)
    return out


def _fab_files(data: dict[str, Any]) -> list[Any]:
    files = data.get("files")
    if isinstance(files, dict):
        out: list[Any] = []
        for v in files.values():
            out.extend(v if isinstance(v, list) else [v])
        return out
    return list(files or [])


# ---- the MCP stdio conversation -----------------------------------------------------------------------------


def real_engines() -> Any | None:
    """The managed JRE + Freerouting scripts/bootstrap.ps1 installed into the REAL storage home
    (%LOCALAPPDATA%/kiwrap); the session's storage home is a throw-away directory, so the server
    is pointed at them through the `KIWRAP_JAVA` / `KIWRAP_FREEROUTING_JAR` overrides. None when
    bootstrap never ran: the conversation then routes the power nets by hand."""
    if os.environ.get("KIWRAP_E2E_NO_ROUTER"):
        return None  # exercise the hand-routed fallback path on a machine that has the engines
    try:
        from kiwrap.backends.router import discover_engines
    except ImportError:
        return None
    engines = discover_engines(Path(os.environ.get("LOCALAPPDATA", "")) / "kiwrap")
    return engines if engines.available else None


def server_params(home: Path, engines: Any | None = None) -> StdioServerParameters:
    env = get_default_environment()
    if engines is not None:
        env["KIWRAP_JAVA"] = str(engines.java)
        env["KIWRAP_FREEROUTING_JAR"] = str(engines.jar)
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "kiwrap.core.cli_main", "--storage-home", str(home), "serve"],
        env=env,
    )


async def run_over_stdio(
    home: Path, work: Path, target: str, log: Log, engines: Any | None
) -> dict[str, Any]:
    async with Client(server_params(home, engines), mode="auto") as c:
        listed = await c.list_tools()
        tools = {t.name: (t.input_schema or {}) for t in listed.tools}

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

        out = await flow(call, tools, work, target, log, router=engines is not None)
        out["tool_names"] = sorted(tools)
        return out


def _run(target: str, tmp_path: Path, storage_home: Path) -> tuple[dict[str, Any], Log]:
    """One scenario over MCP stdio; the user's real KiCad config of the target install is hashed
    before and after - it must never change."""
    from kiwrap import sandbox
    from kiwrap.installation import discover

    install = next((i for i in discover() if i.major_minor == target and not i.is_bench_build), None)
    hash_before = sandbox.user_config_hash(install) if install is not None else None
    log = Log()
    work = tmp_path / f"ref-{target.replace('.', '_')}"
    work.mkdir()
    plan = load_plan()
    try:
        out = anyio.run(run_over_stdio, storage_home, work, target, log, real_engines())
    finally:
        print("\n" + log.dump())
        state = work / plan["name"] / ".kiwrap"
        if state.is_dir():
            (state / "e2e-log.json").write_text(json.dumps(log.rows, indent=1, default=str), encoding="utf-8")
            if "out" in locals():
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
            "capabilities",
            "stats",
            "erc",
            "erc_violations",
            "intent_diff",
            "sync",
            "place",
            "check",
            "off_board_review",
            "routing_mode",
            "route",
            "route_auto",
            "fix",
            "zones",
            "drc",
            "drc_violations",
            "unrouted",
            "unrouted_nets",
            "fab_profile",
            "fab",
            "status",
        )
    }
    print(json.dumps(brief, indent=1, default=str)[:8000])
    return out, log


def _facts(out: dict[str, Any], log: Log) -> grader.Facts:
    plan = load_plan()
    nets = {
        k: [{"ref": s.split(".", 1)[0], "pin": s.split(".", 1)[1]} for s in v] for k, v in out["nets"].items()
    }
    return grader.Facts(
        erc_errors=out["erc"]["errors"],
        erc_warnings=out["erc"]["warnings"],
        nets=nets,
        unrouted=out["unrouted"],
        must_fix=out["drc"]["must_fix"],
        review=out["drc"]["review"],
        parity_violations=out["drc"]["parity_violations"],
        fab_files=out["fab_files_on_disk"],
        counts={
            "components": out["stats"]["components"],
            "nets": out["stats"]["nets"],
            "pads": out["pad_count"],
            "tool_calls": len(log.rows),
            "parts_planned": len(plan["parts"]),
        },
        degraded=sorted({d for r in log.rows for d in (r["degraded"] or [])}),
        notes=[f"routing_mode={out['routing_mode']}", f"fab_profile={out['fab_profile']}"],
    )


def _assert_common(out: dict[str, Any], log: Log) -> grader.Grade:
    """The numbers every scenario must reach through the routing stage; returns the grade so the
    caller can decide what the routing mode allows."""
    plan = load_plan()
    n_parts = len(plan["parts"])
    assert not any(r["error"] for r in log.rows), log.dump()
    assert out["symbols"] == n_parts
    assert out["erc"] == {"ok": True, "errors": 0, "warnings": out["erc"]["warnings"]}
    assert out["erc"]["warnings"] == 0, out["erc_violations"]
    assert out["stats"]["dangling_ends"] == 0 and out["stats"]["unconnected_pins"] == 0, out["stats"]
    assert out["stats"]["label_only_nets"] == 0 and out["stats"]["components"] == n_parts, out["stats"]
    assert out["stage_after_check"] == "erc_clean"
    assert out["intent_diff"]["ok"], out["intent_diff"]
    if out["intent_set"] is not None:
        assert (out["check_intent_diff"] or {}).get("count") == 0, out["check_intent_diff"]
        assert out["check_intent_diff"]["extra"] == [], out["check_intent_diff"]  # strict_nets
    assert out["unresolved"] == []
    assert (
        len(out["sync"]["added"]) == n_parts
        and out["sync"]["removed"] == []
        and out["sync"]["unresolved"] == []
    )
    assert out["sync_drc"]["parity"] == 0
    assert out["stage_after_sync"] == "synced"
    assert out["check"]["courtyard_overlap"] == 0 and out["check"]["off_board_copper"] == 0, out[
        "check_items"
    ]
    assert out["off_board_review"] == sorted(plan["board"]["expected_off_board_review"])
    assert out["stage_after_place"] == "placed"
    assert all(z["filled"] is True for z in out["zones"].values()), out["zones"]
    assert out["drc"]["parity_violations"] == 0, out["drc_violations"]
    assert out["status"]["stage"] == "exported"
    assert {"power.kicad_sch", "mcu.kicad_sch", "io.kicad_sch"} <= set(out["files"]), out["files"]
    assert len(log.rows) <= plan["budget"]["max_tool_calls"], len(log.rows)
    grade = grader.grade(_facts(out, log), plan)
    print(json.dumps(grade.as_dict(), indent=1, default=str)[:4000])
    return grade


def _settle(out: dict[str, Any], grade: grader.Grade) -> None:
    """Routed by the autorouter: the D5 acceptance (grade passed) holds. Power nets by hand: the
    other stages are graded, the signal nets are expected-unrouted and the test skips - clearly."""
    if out["routing_mode"] == "auto":
        ra = out["route_auto"]
        # the DSN/SES codec: kiwrap's own on a stock install, the patched kicad-cli on a bench build
        assert ra["provider"] == "codec" or str(ra["provider"]).startswith("cli:"), ra["provider"]
        assert ra["engine"]["name"] == "freerouting" and ra["engine"]["version"], ra["engine"]
        assert ra["router"]["ok"] is True and ra["router"]["exit_code"] == 0, ra["router"]
        assert ra["router"]["history"] and ra["router"]["passes"] >= 1, ra["router"]
        assert ra["drc"]["refilled"] is True, ra["drc"]
        if ra["drc"]["must_fix"] or (ra["routed"] or {}).get("unrouted"):
            assert out["fix"] is not None and out["fix"]["must_fix"]["after"] == 0, out["fix"]
        assert out["drc"]["must_fix"] == 0 and out["drc"]["ok"] is True, out["drc_violations"]
        assert out["unrouted"] == 0, out["unrouted_nets"]
        assert out["stage_after_drc"] == "drc_clean" and out["fab_forced"] is False
        assert grade.passed, grade.failures
        return
    plan = load_plan()
    power = set(plan["routing"]["power_nets"])
    must_fix_types = {v[0] for v in out["drc_violations"] if v[1] == "must_fix"}
    assert must_fix_types <= {"unconnected_items"}, out["drc_violations"]
    assert not (set(out["unrouted_nets"]) & power), (
        "a power net routed by hand is still open",
        out["unrouted_nets"],
        out["drc_violations"],
    )
    assert out["fab_forced"] is True
    other = [f for f in grade.failures if not f.startswith(("unrouted", "must_fix"))]
    assert not other, other
    caps = out["capabilities"]
    why = (
        "kw_pcb_route_auto is not on this server"
        if not caps["kw_pcb_route_auto"]
        else "the managed JRE / Freerouting are not installed (scripts/bootstrap.ps1)"
    )
    pytest.skip(
        f"{why}: schematic, placement, power-net routing, zones, "
        f"parity and the fab bundle passed; {out['unrouted']} signal connections on "
        f"{len(out['unrouted_nets'])} nets are expected-unrouted until the router component lands"
    )


@pytest.mark.kicad
@pytest.mark.slow
@pytest.mark.timeout(2400)
@pytest.mark.kicad_version("10.99")
def test_e2e_reference_board_on_the_kicad_11_preview_over_stdio(tmp_path: Path, storage_home: Path):
    """10.99 nightly: every schematic / board write with an IPC twin is answered by ipc11; sheet
    creation, footprint assignment and board setup by the file engine; kicad-cli 10.99 judges."""
    out, log = _run("10.99", tmp_path, storage_home)
    grade = _assert_common(out, log)
    by_tool: dict[str, list[dict[str, Any]]] = {}
    for r in log.rows:
        by_tool.setdefault(r["tool"], []).append(r)
    for tool in ("kw_sch_add_symbol", "kw_sch_connect", "kw_sch_annotate", "kw_pcb_sync", "kw_pcb_zones"):
        rows = by_tool[tool]
        assert all(str(r["provider_used"]).startswith("ipc11:") for r in rows), (
            tool,
            sorted({str(r["provider_used"]) for r in rows}),
        )
        assert all("ipc11_unavailable" not in (r["degraded"] or []) for r in rows), tool
    # every placement is unrotated: one batch, served by ipc11 without the G16 guard
    place_rows = by_tool["kw_pcb_place"]
    assert len(place_rows) == 1 and str(place_rows[0]["provider_used"]).startswith("ipc11:"), place_rows
    assert "ipc11_guarded" not in (place_rows[0]["degraded"] or []), place_rows
    items = by_tool["kw_sch_add_item"]
    sheet_rows = [r for r in items if r["item_type"] == "sheet"]
    assert len(sheet_rows) == 3 and all(r["provider_used"] == "file" for r in sheet_rows), sheet_rows
    assert all(
        str(r["provider_used"]).startswith("cli:10.99")
        for r in by_tool["kw_sch_check"] + by_tool["kw_pcb_drc"]
    )
    _no_api_server_left(load_plan()["name"])
    _settle(out, grade)


@pytest.mark.kicad
@pytest.mark.slow
@pytest.mark.timeout(2400)
@pytest.mark.kicad_version("10.0")
def test_e2e_reference_board_on_kicad_10_over_stdio(tmp_path: Path, storage_home: Path):
    """10.0.6: every write is answered by the file engine; kicad-cli 10.0.6 judges (ERC, netlist,
    DRC with parity, zone refill)."""
    out, log = _run("10.0", tmp_path, storage_home)
    grade = _assert_common(out, log)
    writes = [r for r in log.rows if r["tool"] in WRITE_TOOLS]
    assert writes and all(r["provider_used"] == "file" for r in writes), sorted(
        {(r["tool"], str(r["provider_used"])) for r in writes}
    )
    assert all("ipc11_unavailable" not in (r["degraded"] or []) for r in writes)
    _settle(out, grade)


def _no_api_server_left(project_name: str) -> None:
    """PROCESS HYGIENE: no `kicad-cli api-server` on the e2e project survives the conversation."""
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
    servers = [ln for ln in ps.stdout.splitlines() if "api-server" in ln and project_name in ln]
    assert not servers, servers


# ---- fast checks (no KiCad): the plan file and the grader rubric --------------------------------------------


def test_design_plan_json_is_current():
    """design_plan.json is generated from plan.py; a stale file would grade against the wrong intent."""
    assert json.loads(PLAN_PATH.read_text(encoding="utf-8")) == build_plan(), (
        "run `uv run python -m evals.reference_board.plan` to regenerate design_plan.json"
    )


def test_plan_is_self_consistent():
    plan = load_plan()
    refs = [p["ref"] for p in plan["parts"]]
    assert len(refs) == len(set(refs)) and 25 <= len(refs) <= 35
    sheets = {s["name"] for s in plan["sheets"]}
    assert sheets == {"power", "mcu", "io"}
    assert {p["sheet"] for p in plan["parts"]} == sheets
    # every pin the intent names belongs to a planned part; no pin is both connected and no-connect
    mentioned = {(r, n) for nodes in plan["intent"]["nets"].values() for r, n in nodes}
    mentioned |= {(r, n) for group in plan["intent"]["unnamed"] for r, n in group}
    nc = {(r, n) for r, n in plan["intent"]["no_connect"]}
    assert not (mentioned & nc)
    assert {r for r, _n in mentioned | nc} == set(refs)
    # hierarchical labels match the sheet pins (name + kind) of their sheet
    pins_by_sheet = {s["name"]: {sp["name"]: sp["kind"] for sp in s["pins"]} for s in plan["sheets"]}
    for hl in plan["hier_labels"]:
        assert pins_by_sheet[hl["sheet"]].get(hl["text"]) == hl["kind"], hl
    # the fallback routing only touches the power nets it claims to route
    ops = plan["routing"]["fallback_ops"]
    assert {op["net"] for op in ops} <= set(plan["routing"]["power_nets"])


def test_grader_intent_diff_rubric():
    plan = load_plan()
    nets = {name: [tuple(n) for n in nodes] for name, nodes in plan["intent"]["nets"].items()}
    for i, group in enumerate(plan["intent"]["unnamed"]):
        nets[f"Net-(X{i}-Pad1)"] = [tuple(n) for n in group]
    for ref, number in plan["intent"]["no_connect"]:
        nets[f"unconnected-({ref}-Pad{number})"] = [(ref, number)]
    nets["GND"] = [*nets["GND"], ("#PWR01", "1"), ("#FLG01", "1")]  # power symbols are ignored
    assert grader.intent_diff(nets, plan["intent"]).ok
    # a misnamed net, a missing pin, a stray connection and a connected no-connect are all reported
    bad = dict(nets)
    bad["/power/USB_DP"] = bad.pop("/USB_DP")
    bad["/EN"] = [n for n in bad["/EN"] if n != ("SW1", "1")]
    bad["/IO0"] = [*bad["/IO0"], ("U1", "4")]
    bad.pop("unconnected-(U1-Pad4)")
    diff = grader.intent_diff(bad, plan["intent"])
    assert not diff.ok
    assert diff.misnamed == ["/USB_DP is named '/power/USB_DP' on the board"]
    assert diff.missing == ["/EN: SW1.1"]
    assert diff.extra == ["/IO0: U1.4"]
    assert diff.count == 3
    facts = grader.Facts(
        erc_errors=0,
        erc_warnings=0,
        nets=nets,
        unrouted=0,
        must_fix=0,
        review=2,
        parity_violations=0,
        fab_files=[
            "gerbers/x-F_Cu.gbr",
            "gerbers/x-Edge_Cuts.gbr",
            "drill/x.drl",
            "pos/x-all-pos.csv",
            "bom/x.csv",
        ],
        counts={"components": len(plan["parts"])},
    )
    good = grader.grade(facts, plan)
    assert good.passed and good.fab["complete"] and good.fab["present"]["bom"]
    facts.must_fix, facts.unrouted, facts.fab_files = 3, 2, ["gerbers/x-F_Cu.gbr"]
    worse = grader.grade(facts, plan)
    assert not worse.passed
    assert worse.failures == [
        "unrouted connections: 2",
        "must_fix DRC violations: 3",
        "fab bundle incomplete: missing drill, pos",
    ]
