"""kw_intent: the design intent an agent declares before it draws - blocks, rails, interfaces,
constraints and the nets it means to make as `{net: [[ref, pin], ...]}` - stored in
`<project>/.kiwrap/intent.json`, and the **intent diff** against KiCad's own netlist
(docs/ARCHITECTURE.md §3.3 step 1-2, §7 "the check that catches 'label on the wrong pin', which
ERC cannot see", §11 `validators.intent_diff`).

The diff (`intent_diff`) is pure data: for every declared net it finds the KiCad net it landed on
(by name - `GND`, `/VIN`, `/sheet/NET`, `Net-(U1-Pad3)` - else by the pins it shares) and reports

    missing   declared (ref, pin) that is not on that net: `found_in` names the KiCad net the
              pin is actually on (None = unconnected / no such pin)
    connected_no_connect
              a pin declared in `no_connect` that KiCad's netlist puts on a net
    wrong_pin declared (ref, pin) absent, while the same ref sits on the net with ANOTHER pin
              (`actual_pin`) - the classic off-by-one-pin wiring mistake
    extra     (ref, pin) on the KiCad net that the declaration does not list (power symbols and
              flags `#...` are never extra); counted as a defect only with
              `constraints.strict_nets = true` (a declaration is usually partial while authoring)
    unmatched declared nets no KiCad net corresponds to (every declared pin missing)

`kw_sch_check` calls `attach_intent_diff` after its netlist export so the receipt carries
`validators[name=intent_diff]` (`after` = missing + wrong_pin [+ extra when strict], `detail` = the lists)
and `data.intent_diff`; nothing is attached when the project has no intent. `kw_intent(
action="diff")` runs the same diff on demand through kicad-cli's netlist export.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from kiwrap.core.state import KiwrapError, Receipt
from kiwrap.core.state.project import Project
from kiwrap.core.tools._base import (
    MUTATING,
    ToolContext,
    ToolSpec,
    effective_target,
    read_state,
    scratch_folder,
    to_kiwrap_error,
)

INTENT_FILENAME = "intent.json"
INTENT_FORMAT_VERSION = 1
INTENT_KEYS = ("blocks", "rails", "interfaces", "nets", "constraints", "no_connect")
#: keys a caller plausibly means (a typo, a misplaced constraint, a plan section): what the
#: warning suggests instead of silently dropping them (Phase 1c finding)
_KEY_HINTS: dict[str, str] = {
    "constraint": "constraints",
    "strict_nets": "constraints.strict_nets",
    "strict": "constraints.strict_nets",
    "fab": "constraints.fab",
    "block": "blocks",
    "rail": "rails",
    "interface": "interfaces",
    "net": "nets",
    "no_connects": "no_connect",
    "nc": "no_connect",
    "unnamed": "nets (name the net yourself, e.g. `Net-(R1-Pad2)`, or leave the group out)",
}


# ---- the intent model ------------------------------------------------------------------------------------


@dataclass
class Intent:
    blocks: list[dict[str, Any]] = field(default_factory=list)  # [{name, function?, parts?: [refs]}]
    rails: list[dict[str, Any]] = field(default_factory=list)  # [{name, voltage_v?, current_a?, net?}]
    interfaces: list[dict[str, Any]] = field(default_factory=list)  # [{type, connector?, signals?: [...]}]
    nets: dict[str, list[tuple[str, str]]] = field(default_factory=dict)  # {net: [(ref, pin), ...]}
    constraints: dict[str, Any] = field(default_factory=dict)  # {size_mm, layers, fab, ...}
    no_connect: list[tuple[str, str]] = field(default_factory=list)  # pins declared unconnected
    format_version: int = INTENT_FORMAT_VERSION
    #: keys of the input that are not part of the model (reported by kw_intent, never stored)
    unknown_keys: list[str] = field(default_factory=list, compare=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "format_version": self.format_version,
            "blocks": list(self.blocks),
            "rails": list(self.rails),
            "interfaces": list(self.interfaces),
            "nets": {n: [[r, p] for r, p in pins] for n, pins in self.nets.items()},
            "constraints": dict(self.constraints),
            "no_connect": [[r, p] for r, p in self.no_connect],
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> Intent:
        errors: list[str] = []
        nets: dict[str, list[tuple[str, str]]] = {}
        unknown = sorted(str(k) for k in raw if k not in INTENT_KEYS and k != "format_version")
        no_connect: list[tuple[str, str]] = []
        for item in raw.get("no_connect") or []:
            pair = _pin_pair(item)
            if pair is None:
                errors.append(f"no_connect: {item!r} is not a [ref, pin] pair or 'REF.PIN' string")
            elif pair not in no_connect:
                no_connect.append(pair)
        for net, pins in dict(raw.get("nets") or {}).items():
            name = str(net).strip()
            if not name:
                errors.append("nets: an empty net name")
                continue
            out: list[tuple[str, str]] = []
            if not isinstance(pins, (list, tuple)):
                errors.append(f"nets[{name}]: expected a list of [ref, pin] pairs")
                continue
            for item in pins:
                pair = _pin_pair(item)
                if pair is None:
                    errors.append(f"nets[{name}]: {item!r} is not a [ref, pin] pair or 'REF.PIN' string")
                    continue
                if pair not in out:
                    out.append(pair)
            nets[name] = out
        for key in ("blocks", "rails", "interfaces"):
            val = raw.get(key) or []
            if not isinstance(val, list) or any(not isinstance(x, dict) for x in val):
                errors.append(f"{key}: expected a list of objects")
        constraints = raw.get("constraints") or {}
        if not isinstance(constraints, dict):
            errors.append("constraints: expected an object")
        if errors:
            raise KiwrapError(
                "E_BAD_ARGS",
                "intent is malformed: " + "; ".join(errors[:8]),
                why="the intent must follow {blocks[], rails[], interfaces[], nets{net: [[ref, pin], ...]}, constraints{}}",
                fix="fix the listed entries; kw_help(topic='kw_intent') shows the shape",
                example='kw_intent(project=..., action="set", intent={"nets": {"GND": [["U1", "1"], ["C1", "2"]]}})',
            )
        return cls(
            blocks=[dict(b) for b in raw.get("blocks") or []],
            rails=[dict(r) for r in raw.get("rails") or []],
            interfaces=[dict(i) for i in raw.get("interfaces") or []],
            nets=nets,
            constraints=dict(constraints),
            no_connect=no_connect,
            format_version=int(raw.get("format_version", INTENT_FORMAT_VERSION)),
            unknown_keys=unknown,
        )

    def unknown_key_warnings(self) -> list[str]:
        """What `from_dict` dropped, with the nearest known key (`constraint` -> `constraints`,
        a top-level `strict_nets` -> `constraints.strict_nets`), so a caller never believes it
        declared something the diff will never check."""
        if not self.unknown_keys:
            return []
        hints = [f"{k} -> {_KEY_HINTS[k]}" for k in self.unknown_keys if k in _KEY_HINTS]
        text = "intent: unknown keys ignored: " + ", ".join(self.unknown_keys)
        if hints:
            text += " (did you mean " + "; ".join(hints) + "?)"
        return [text + f"; known keys: {', '.join(INTENT_KEYS)}"]

    def merged(self, other: Intent) -> Intent:
        """`other` on top of self: nets/constraints keys replaced per key, lists replaced by name."""

        def by_name(a: list[dict[str, Any]], b: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
            out = {str(x.get(key, i)): x for i, x in enumerate(a)}
            for i, x in enumerate(b):
                out[str(x.get(key, f"new{i}"))] = x
            return list(out.values())

        merged_nc = list(self.no_connect)
        for pair in other.no_connect:
            if pair not in merged_nc:
                merged_nc.append(pair)
        return Intent(
            blocks=by_name(self.blocks, other.blocks, "name"),
            rails=by_name(self.rails, other.rails, "name"),
            interfaces=by_name(self.interfaces, other.interfaces, "type"),
            nets={**self.nets, **other.nets},
            constraints={**self.constraints, **other.constraints},
            no_connect=merged_nc,
            unknown_keys=list(other.unknown_keys),
        )


def _pin_pair(item: Any) -> tuple[str, str] | None:
    if isinstance(item, str):
        if "." in item:
            ref, pin = item.rsplit(".", 1)
            return (ref.strip(), pin.strip()) if ref.strip() and pin.strip() else None
        return None
    if isinstance(item, Mapping):
        mref, mpin = item.get("ref"), item.get("pin")
        return (str(mref), str(mpin)) if mref and mpin is not None else None
    if isinstance(item, (list, tuple)) and len(item) == 2 and item[0] and item[1] is not None:
        return (str(item[0]).strip(), str(item[1]).strip())
    return None


def intent_path(project: Project) -> Path:
    return project.state_dir / INTENT_FILENAME


def load_intent(project: Project) -> Intent | None:
    path = intent_path(project)
    if not path.is_file():
        return None
    try:
        return Intent.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        raise KiwrapError(
            "E_INTERNAL",
            f"cannot read {path}: {exc}",
            why="the intent file is unreadable or not valid JSON",
            fix="kw_intent(action='clear') removes it; then set it again",
        ) from exc


def save_intent(project: Project, intent: Intent) -> Path:
    path = intent_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(intent.to_dict(), indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return path


# ---- the diff ---------------------------------------------------------------------------------------------------


def _net_aliases(name: str) -> set[str]:
    """Spellings under which a declared net may appear in KiCad's netlist: `VIN` -> `/VIN`
    (local label), `+3V3` stays, `/sheet/NET` stays, and case is compared as written."""
    n = name.strip()
    out = {n}
    if not n.startswith("/"):
        out.add("/" + n)
    else:
        out.add(n[1:])
    return out


def _kicad_nets(nl: Any) -> dict[str, set[tuple[str, str]]]:
    return {str(n.name): {(str(x.ref), str(x.pin)) for x in n.nodes} for n in nl.nets}


def _is_virtual(ref: str) -> bool:
    return ref.startswith("#")


def intent_diff(intent: Intent, nl: Any) -> dict[str, Any]:
    """Compare `intent.nets` with a `kiwrap.formats.reports.Netlist` (see the module docstring)."""
    kicad = _kicad_nets(nl)
    pin_to_net: dict[tuple[str, str], str] = {}
    for net, pins in kicad.items():
        for p in pins:
            pin_to_net[p] = net
    known_refs = {str(c.ref) for c in nl.components} | {r for r, _ in pin_to_net}
    missing: list[dict[str, Any]] = []
    wrong_pin: list[dict[str, Any]] = []
    extra: list[dict[str, Any]] = []
    unmatched: list[str] = []
    matched: dict[str, str | None] = {}
    for name, declared in intent.nets.items():
        want = set(declared)
        # 1. by name; 2. by the KiCad net holding most of the declared pins
        knet = next((a for a in _net_aliases(name) if a in kicad), None)
        if knet is None:
            # most shared pins, then most shared refs (a tie between two nets a two-pin
            # declaration straddles goes to the net holding both parts)
            want_refs = {r for r, _ in want}
            best, best_score = None, (0, 0)
            for cand, pins in kicad.items():
                score = (len(want & pins), len(want_refs & {r for r, _ in pins}))
                if score > best_score:
                    best, best_score = cand, score
            knet = best
        matched[name] = knet
        if knet is None:
            unmatched.append(name)
            for ref, pin in declared:
                found = pin_to_net.get((ref, pin))
                missing.append(_missing(name, ref, pin, found, known_refs))
            continue
        have = kicad[knet]
        refs_on_net = {r for r, _ in have}
        for ref, pin in declared:
            if (ref, pin) in have:
                continue
            found = pin_to_net.get((ref, pin))
            if ref in refs_on_net:
                actual = sorted(p for r, p in have if r == ref)
                wrong_pin.append(
                    {
                        "net": name,
                        "kicad_net": knet,
                        "ref": ref,
                        "expected_pin": pin,
                        "actual_pin": actual,
                        "found_in": found,
                    }
                )
            else:
                missing.append(_missing(name, ref, pin, found, known_refs, kicad_net=knet))
        for ref, pin in sorted(have - want):
            if _is_virtual(ref):
                continue
            if any(r == ref for r, _ in want) and any(r == ref and (r, p) not in have for r, p in want):
                continue  # already reported as wrong_pin for that ref
            extra.append({"net": name, "kicad_net": knet, "ref": ref, "pin": pin})
    # declared no-connects that carry a net (a pin the agent meant to leave open but wired)
    connected_nc: list[dict[str, Any]] = []
    for ref, pin in intent.no_connect:
        found = pin_to_net.get((ref, pin))
        if found is not None and not found.startswith("unconnected-("):
            connected_nc.append({"ref": ref, "pin": pin, "kicad_net": found})
    # `extra` pins are only a defect when the intent says its nets are complete
    # (constraints.strict_nets): while authoring, a declaration usually names the pins that
    # matter, not every decoupling cap - the list is still reported
    strict = bool(intent.constraints.get("strict_nets", False))
    total = len(missing) + len(wrong_pin) + len(connected_nc) + (len(extra) if strict else 0)
    return {
        "ok": total == 0,
        "count": total,
        "strict_nets": strict,
        "nets_declared": len(intent.nets),
        "nets_matched": {k: v for k, v in matched.items() if v is not None},
        "unmatched": unmatched,
        "missing": missing,
        "wrong_pin": wrong_pin,
        "extra": extra,
        "connected_no_connect": connected_nc,
    }


def _missing(
    net: str, ref: str, pin: str, found: str | None, known_refs: set[str], *, kicad_net: str | None = None
) -> dict[str, Any]:
    if ref not in known_refs:
        reason = "no such component in the netlist"
    elif found is None:
        reason = "pin is unconnected (or no such pin)"
    else:
        reason = f"pin is on net {found!r}"
    return {"net": net, "kicad_net": kicad_net, "ref": ref, "pin": pin, "found_in": found, "reason": reason}


def attach_intent_diff(project: Project, m: Any, nl: Any) -> dict[str, Any] | None:
    """For kw_sch_check: run the diff against `nl` when the project has an intent, record the
    `intent_diff` validator on the mutation and return the diff (None without an intent)."""
    intent = load_intent(project)
    if intent is None or not (intent.nets or intent.no_connect):
        return None
    diff = intent_diff(intent, nl)
    m.validator_delta(
        "intent_diff",
        diff["count"],
        {
            "missing": diff["missing"],
            "wrong_pin": diff["wrong_pin"],
            "extra": diff["extra"],
            "unmatched": diff["unmatched"],
            "connected_no_connect": diff["connected_no_connect"],
        },
    )
    m.receipt.data["intent_diff"] = diff
    if diff["count"]:
        m.receipt.warnings.append(
            f"intent_diff: {len(diff['missing'])} missing, {len(diff['wrong_pin'])} wrong_pin, "
            f"{len(diff['extra'])} extra, {len(diff['connected_no_connect'])} connected no_connect "
            "against the declaration (data.intent_diff)"
        )
    return diff


# ---- the tool -----------------------------------------------------------------------------------------------------

Action = Literal["set", "get", "diff", "clear"]


class IntentArgs(BaseModel):
    project: str = Field(description="project directory or .kicad_pro path")
    action: Action = Field(
        default="get", description="set (store), get (read), diff (against KiCad's netlist), clear"
    )
    intent: dict[str, Any] | None = Field(
        default=None,
        description=(
            "for set: {blocks: [{name, function?, parts?}], rails: [{name, voltage_v?, current_a?, net?}], "
            "interfaces: [{type, connector?, signals?}], nets: {net: [[ref, pin], ...] | ['REF.PIN', ...]}, "
            "constraints: {size_mm?, layers?, fab?, strict_nets?, ...}, no_connect: [[ref, pin], ...] (pins "
            "that must stay unconnected; the diff reports them under connected_no_connect). Unknown keys are "
            "ignored with a warning naming the nearest known key"
        ),
    )
    merge: bool = Field(default=True, description="for set: merge into the stored intent (false = replace)")


def run(ctx: ToolContext, args: IntentArgs) -> Receipt:
    project = Project.from_path(args.project)
    state, external = read_state(project)
    receipt = Receipt(provider_used="file", stage=state.stage)
    if external.external_edit:
        receipt.degraded.append("external_edit")
    path = intent_path(project)
    if args.action == "set":
        if args.intent is None:
            raise KiwrapError(
                "E_BAD_ARGS",
                "action='set' needs `intent`",
                example='kw_intent(project=..., action="set", intent={"nets": {"GND": [["U1", "1"]]}})',
            )
        new = Intent.from_dict(args.intent)
        receipt.warnings.extend(new.unknown_key_warnings())
        current = load_intent(project) if args.merge else None
        intent = current.merged(new) if current is not None else new
        save_intent(project, intent)
        receipt.changed["updated" if current is not None else "added"].append(project.relpath(path))
        receipt.data = {
            "ok": True,
            "action": "set",
            "path": path.as_posix(),
            "intent": intent.to_dict(),
            **_counts(intent),
        }
        receipt.next_suggested = "kw_sch_check" if project.root_sch.exists() else "kw_sch_add_symbol"
        return receipt
    if args.action == "clear":
        existed = path.is_file()
        if existed:
            path.unlink()
            receipt.changed["removed"].append(project.relpath(path))
        receipt.data = {"ok": True, "action": "clear", "existed": existed}
        return receipt
    stored = load_intent(project)
    if args.action == "get":
        receipt.data = {
            "ok": True,
            "action": "get",
            "path": path.as_posix() if stored is not None else None,
            "intent": stored.to_dict() if stored is not None else None,
            **(_counts(stored) if stored is not None else {}),
        }
        return receipt
    # diff
    if stored is None or not (stored.nets or stored.no_connect):
        raise KiwrapError(
            "E_BAD_ARGS",
            "no declared nets to diff against",
            why="the project has no intent (or none with `nets` / `no_connect`)",
            fix="declare the nets first with kw_intent(action='set', intent={'nets': {...}})",
        )
    if not project.root_sch.exists():
        raise KiwrapError("E_PROJECT_NOT_FOUND", f"root schematic {project.root_sch.name} is missing")
    install = effective_target(ctx, project, state, for_write=False).install
    config = ctx.services.config_root(install)
    try:
        # into a throw-away folder: the backend's default `<root>.net` would be left in the project
        # (review §3 small defects, item 2)
        with scratch_folder(ctx, "netlist") as folder:
            nl = ctx.services.export_netlist(
                install, config, project.root_sch, folder / f"{project.root_sch.stem}.net"
            )
    except Exception as exc:  # noqa: BLE001 - map to the teaching error
        raise to_kiwrap_error(exc) from exc
    diff = intent_diff(stored, nl)
    receipt.provider_used = ctx.services.provider(install)
    receipt.validators.append(_delta(diff))
    receipt.data = {"ok": diff["ok"], "action": "diff", **diff}
    receipt.next_suggested = None if diff["ok"] else "kw_sch_connect"
    return receipt


def _delta(diff: dict[str, Any]) -> Any:
    from kiwrap.core.state import ValidatorDelta

    return ValidatorDelta(
        name="intent_diff",
        before=None,
        after=diff["count"],
        detail={k: diff[k] for k in ("missing", "wrong_pin", "extra", "unmatched", "connected_no_connect")},
    )


def _counts(intent: Intent) -> dict[str, Any]:
    return {
        "blocks": len(intent.blocks),
        "rails": len(intent.rails),
        "interfaces": len(intent.interfaces),
        "nets": len(intent.nets),
        "pins": sum(len(p) for p in intent.nets.values()),
        "constraints": sorted(intent.constraints),
        "no_connect": len(intent.no_connect),
    }


def declared_nets(pairs: Iterable[tuple[str, Iterable[tuple[str, str]]]]) -> dict[str, list[tuple[str, str]]]:
    """Helper for callers building an intent in code: `{net: [(ref, pin), ...]}` from pairs."""
    return {n: [(str(r), str(p)) for r, p in pins] for n, pins in pairs}


SPEC = ToolSpec(
    name="kw_intent",
    title="Design intent",
    description=(
        "Declare (set) or read (get) the design intent - blocks, rails, interfaces, constraints and the nets you "
        "mean to make as {net: [[ref, pin], ...]} - stored in <project>/.kiwrap/intent.json; diff runs the intent "
        "diff against KiCad's netlist (missing / wrong_pin / extra pins per declared net). kw_sch_check reports the "
        "same diff as validators.intent_diff once an intent exists."
    ),
    input_model=IntentArgs,
    handler=run,
    annotations=MUTATING,
)
