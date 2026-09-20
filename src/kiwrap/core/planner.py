"""Layer 4 - the planner (docs/ARCHITECTURE.md §6): every operation declares an ORDERED list of
providers with predicates over capability keys; the first provider whose predicate holds and
whose attempt succeeds wins, and the receipt carries `provider_used` plus `degraded[]` so the
agent learns what it got from the result, never from a version number (CLAUDE.md rules: nothing
here mentions a KiCad version).

Provider order (docs/DECISIONS.md D16 KiCad-11-first, D17 data-first):

    ipc11  - `kicad-cli api-server` headless IPC session (capability `ipc.headless`; schematic
             reads also need `ipc.schematic`). KiCad computes the answer.
    file   - the lossless CST readers / analysis (`kiwrap.formats`); always available. kiwrap
             computes the answer, so anything KiCad would normally compute (ratsnest, netlist,
             stats) is tagged `*_estimated` in `degraded`.
    cli    - kicad-cli structured exports (`cli.*` capabilities). KiCad computes the answer but
             as a one-shot process per call.

The general order is ipc11 -> file -> cli. Two operations rank `cli` above `file` on purpose:
`netlist` and `stats`, because there kicad-cli's answer is KiCad's own (authoritative) while the
file provider is an estimate. `diff` is cli-only (`sch diff --format json`, verified on the
installed nightly: exit 0 identical / 5 differences, JSON `{docType, path, changes[]}`, see
`kw_sch_diff`); its Unsupported fallback is "kw_sch_get on both files and compare".

The authoring operations (`schematic.write`, `board.sync`, `board.place`, `board.zones`,
`board.route_primitives`) are ipc11 -> file: the KiCad 11 twins in `kiwrap.backends.ipc.writes`
commit through the headless api-server, the file writers (`kiwrap.formats.schematic_writer` /
`board_writer`) patch the CST losslessly; `board.setup` is file only (rules and netclasses live
in the .kicad_pro / .kicad_dru, which no IPC command writes). The tools in
`kiwrap.core.tools._author` add the G13 hang guard on top of the predicates.

Predicates read capability keys through `Capabilities.has()` and, for `cli.*` keys that older
cached records may lack, fall back to the probed `cli.subcommands` list (both are `--help`
derived facts, so they agree whenever both exist).

`run()` is the execution half: it walks the candidates, calls the attempt registered for each,
and falls through to the next one when the attempt reports the provider UNAVAILABLE
(`E_UNSUPPORTED` - component missing or capability refused at call time; `E_TIMEOUT` /
`E_KICAD_FAILED` for the ipc11 provider, whose server may fail to start). Any other error is
the caller's problem and propagates. Each fall-through adds a warning and, when a later provider
is used, its `degraded` tags.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from kiwrap.core.state import KiwrapError
from kiwrap.core.tools._base import to_kiwrap_error

T = TypeVar("T")

IPC11 = "ipc11"
FILE = "file"
CLI = "cli"
# the Specctra DSN/SES codec providers of the routing operations (docs/ARCHITECTURE.md §8):
# `codec` is kiwrap's own DSN writer / SES reader (kiwrap.formats.specctra, always present once
# built), `swig` the bundled-python driver on a KiCad that still ships SWIG (swigdriver/)
CODEC = "codec"
SWIG = "swig"
PROVIDERS: tuple[str, ...] = (IPC11, FILE, CLI, CODEC, SWIG)
#: providers whose `provider_used` label carries no KiCad version (kiwrap computes the answer)
UNVERSIONED: frozenset[str] = frozenset({FILE, CODEC})

#: errors that mean "this provider cannot serve the request here" rather than "the request is wrong"
UNAVAILABLE_CODES: dict[str, frozenset[str]] = {
    IPC11: frozenset({"E_UNSUPPORTED", "E_TIMEOUT", "E_KICAD_FAILED"}),
    FILE: frozenset({"E_UNSUPPORTED"}),
    CLI: frozenset({"E_UNSUPPORTED"}),
    CODEC: frozenset({"E_UNSUPPORTED"}),
    SWIG: frozenset({"E_UNSUPPORTED", "E_TIMEOUT", "E_KICAD_FAILED"}),
}


@dataclass(frozen=True)
class Predicate:
    """All `keys` must be truthy in the capability record. A key listed in `subcommands` (as
    `key -> "sch diff"`) is also satisfied by that subcommand being in `cli.subcommands`."""

    keys: tuple[str, ...] = ()
    subcommands: Mapping[str, str] = field(default_factory=dict)

    def holds(self, caps: Any) -> bool:
        return all(self._has(caps, k) for k in self.keys)

    def missing(self, caps: Any) -> list[str]:
        return [k for k in self.keys if not self._has(caps, k)]

    def _has(self, caps: Any, key: str) -> bool:
        try:
            if caps.has(key):
                return True
        except Exception:  # noqa: BLE001 - a broken record counts as "does not have it"
            return False
        sub = self.subcommands.get(key)
        if sub is None:
            return False
        try:
            if key in caps:  # the record knows the key and says no; the subcommand list cannot override
                return False
            return sub in list(caps["cli.subcommands"])
        except Exception:  # noqa: BLE001
            return False


ALWAYS = Predicate()


@dataclass(frozen=True)
class Provider:
    name: str
    needs: Predicate = ALWAYS
    degraded: tuple[str, ...] = ()
    note: str = ""


@dataclass(frozen=True)
class Operation:
    name: str
    providers: tuple[Provider, ...]
    fallback: str = ""  # what the agent can do when no provider is available (names a kw_* tool)

    def names(self) -> list[str]:
        return [p.name for p in self.providers]


_HEADLESS = Predicate(keys=("ipc.headless",))
_HEADLESS_SCH = Predicate(keys=("ipc.headless", "ipc.schematic"))
_SCH_DIFF = Predicate(keys=("cli.has_sch_diff",), subcommands={"cli.has_sch_diff": "sch diff"})
_STATS = Predicate(keys=("cli.has_stats_json",), subcommands={"cli.has_stats_json": "pcb export stats"})
_STACKUP = Predicate(
    keys=("cli.has_stackup_json",), subcommands={"cli.has_stackup_json": "pcb export stackup"}
)
_NETLIST = Predicate(
    keys=("cli.has_netlist_export",), subcommands={"cli.has_netlist_export": "sch export netlist"}
)
_RATSNEST = Predicate(
    keys=("cli.has_ratsnest_export",), subcommands={"cli.has_ratsnest_export": "pcb export ratsnest"}
)
# the fork's commands are `pcb export specctra` and `pcb import-ses` (a sibling of `pcb import`,
# whose positional file argument leaves argparse no room for a subcommand - docs/UPSTREAM.md,
# kiwrap_patches.json `specctra-jobs`); both keys are presence probes of the --help tree
_SPECCTRA_CLI = Predicate(
    keys=("cli.has_specctra_export", "cli.has_specctra_import"),
    subcommands={
        "cli.has_specctra_export": "pcb export specctra",
        "cli.has_specctra_import": "pcb import-ses",
    },
)
_SPECCTRA_SWIG = Predicate(keys=("swig.available", "swig.ses_import_headless"))

#: the DSN/SES round trip of the routing tools (docs/ARCHITECTURE.md §8): the patched kicad-cli
#: (docs/UPSTREAM.md fork workflow; capability keys derived from the `--help` tree), kiwrap's own
#: codec certified against KiCad's exporter / importer, and the SWIG driver on a KiCad that still
#: has it (ImportSpecctraSES(BOARD, path) exists from 8.0 - docs/VERIFICATION.md claim 7)
_ROUTING_PROVIDERS: tuple[Provider, ...] = (
    Provider(
        CLI, _SPECCTRA_CLI, note="kicad-cli pcb export specctra / pcb import-ses (kiwrap patch specctra-jobs)"
    ),
    Provider(CODEC, note="kiwrap.formats.specctra DsnWriter / SesReader on the lossless CST"),
    Provider(SWIG, _SPECCTRA_SWIG, note="pcbnew.ExportSpecctraDSN / ImportSpecctraSES in the bundled python"),
)

OPERATIONS: dict[str, Operation] = {
    "board.read": Operation(
        "board.read",
        (
            Provider(IPC11, _HEADLESS, note="headless api-server GetItems / nets / bounding boxes"),
            Provider(FILE, note="lossless CST reader of the .kicad_pcb"),
        ),
    ),
    "schematic.read": Operation(
        "schematic.read",
        (
            Provider(IPC11, _HEADLESS_SCH, note="headless api-server symbols / lines / labels / hierarchy"),
            Provider(FILE, note="lossless CST reader of the .kicad_sch tree"),
        ),
    ),
    "connectivity": Operation(
        "connectivity",
        (
            Provider(
                IPC11,
                _HEADLESS,
                note=(
                    "KiCad's own airwires over GetRatsnest when the server handles it (ipc.has_get_ratsnest, "
                    "kiwrap patch ratsnest-export), else KiCad's connectivity clusters per net (GetItemsByNet + "
                    "GetConnectedItems): the unrouted count is KiCad's; airwire end points are nearest-anchor "
                    "guesses, which the tool tags airwire_endpoints_estimated"
                ),
            ),
            Provider(
                CLI,
                _RATSNEST,
                note=(
                    "kicad-cli pcb export ratsnest (kiwrap patch ratsnest-export, docs/UPSTREAM.md "
                    "G1): KiCad's airwires with both end items, anchor positions and layers; exact, no session"
                ),
            ),
            Provider(FILE, degraded=("ratsnest_estimated",), note="nearest-pad ratsnest estimate"),
        ),
        fallback="kw_pcb_drc reports unconnected_items with positions",
    ),
    "overlaps": Operation(
        "overlaps",
        (Provider(FILE, note="courtyard polygons / bounding boxes from the file"),),
        fallback="kw_pcb_drc reports courtyards_overlap (review class)",
    ),
    "netlist": Operation(
        "netlist",
        (
            Provider(IPC11, _HEADLESS_SCH, note="GetNetlist over the headless api-server"),
            Provider(CLI, _NETLIST, note="kicad-cli sch export netlist (kicadsexpr)"),
            Provider(FILE, degraded=("netlist_estimated",), note="pin/wire/label connectivity from the file"),
        ),
        fallback="kw_sch_check exports the netlist summary",
    ),
    "stats": Operation(
        "stats",
        (
            Provider(CLI, _STATS, note="kicad-cli pcb export stats --format json"),
            Provider(FILE, degraded=("stats_estimated",), note="counts from the file"),
        ),
    ),
    "stackup": Operation(
        "stackup",
        (Provider(CLI, _STACKUP, note="kicad-cli pcb export stackup --format json"),),
        fallback="kw_pcb_get(kinds=['rules']) lists the copper layers",
    ),
    "diff": Operation(
        "diff",
        (Provider(CLI, _SCH_DIFF, note="kicad-cli sch diff --format json"),),
        fallback="kw_sch_get on both files + compare",
    ),
    # ---- authoring (mutations; docs/ARCHITECTURE.md §7, §8, §10) ---------------------------------
    # ipc11 twins live in kiwrap.backends.ipc.writes and answer with the file writers' dataclasses;
    # the file provider is the lossless CST writer and is always available. The receipt's
    # `provider_used` tells the agent which one wrote the file.
    "schematic.write": Operation(
        "schematic.write",
        (
            Provider(
                IPC11,
                _HEADLESS_SCH,
                note=(
                    "PlaceSymbolFromLibrary / CreateItems / UpdateItems commits over the headless "
                    "api-server (one undo step each). Skipped by the tools on a build with "
                    "ipc.schematic_update_shared_lib_symbol_hangs whenever the write touches a second "
                    "unit of a multi-unit symbol (G13 hang guard) - the file engine answers instead"
                ),
            ),
            Provider(FILE, note="kiwrap.formats.schematic_writer: lossless CST patch of the .kicad_sch"),
        ),
    ),
    "board.setup": Operation(
        "board.setup",
        (
            Provider(
                FILE,
                note=(
                    "kiwrap.formats.board_writer: layers / rules / outline in the .kicad_pcb, netclasses "
                    "in the .kicad_pro, custom rules in the .kicad_dru"
                ),
            ),
        ),
    ),
    "board.sync": Operation(
        "board.sync",
        (
            Provider(
                IPC11,
                _HEADLESS,
                note=(
                    "ImportNetlist over the headless api-server when the presence probe says it is "
                    "handled headless; else Unsupported(fallback='file')"
                ),
            ),
            Provider(
                FILE,
                note=(
                    "kiwrap.formats.board_writer.sync: footprints from the fp-lib-table, pad nets from "
                    "the netlist nodes, positions kept, new parts off-board in a grid"
                ),
            ),
        ),
        fallback="kw_pcb_drc(schematic_parity=true) lists what differs",
    ),
    "board.place": Operation(
        "board.place",
        (
            Provider(IPC11, _HEADLESS, note="UpdateItems commit moving the footprints"),
            Provider(FILE, note="kiwrap.formats.board_writer.place"),
        ),
    ),
    "board.zones": Operation(
        "board.zones",
        (
            Provider(IPC11, _HEADLESS, note="CreateItems(Zone) commit + RefillZones"),
            Provider(
                FILE,
                note=(
                    "kiwrap.formats.board_writer.add_zone; the fill comes from kicad-cli pcb drc "
                    "--refill-zones --save-board (cli.drc_refill_flags)"
                ),
            ),
        ),
    ),
    "board.route_primitives": Operation(
        "board.route_primitives",
        (
            Provider(IPC11, _HEADLESS, note="CreateItems(Track / Via) / DeleteItems commits"),
            Provider(FILE, note="kiwrap.formats.board_writer.add_track / add_via / delete"),
        ),
    ),
    # ---- autorouting (kw_pcb_route_auto / kw_pcb_fix; docs/ARCHITECTURE.md §8) ------------------
    # the provider is the DSN/SES codec of the round trip; the router itself is always
    # Freerouting under the managed JRE (kiwrap.backends.router)
    "routing.autoroute": Operation(
        "routing.autoroute", _ROUTING_PROVIDERS, fallback="kw_pcb_route (track / via primitives)"
    ),
    "routing.fix": Operation(
        "routing.fix", _ROUTING_PROVIDERS, fallback="kw_pcb_route (delete + re-add the violating tracks)"
    ),
}


def operation(name: str) -> Operation:
    try:
        return OPERATIONS[name]
    except KeyError:
        raise KiwrapError(
            "E_INTERNAL", f"unknown planner operation {name!r}", candidates=sorted(OPERATIONS)
        ) from None


@dataclass
class Plan:
    operation: Operation
    candidates: list[Provider]
    skipped: list[tuple[str, str]]  # (provider, reason)
    preferred: str | None = None  # the provider the caller forced, when it is NOT eligible
    preferred_reason: str = ""
    elapsed_s: float | None = None  # wall time of the provider that answered (set by `run`)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "operation": self.operation.name,
            "candidates": [c.name for c in self.candidates],
            "skipped": [{"provider": p, "reason": r} for p, r in self.skipped],
        }
        if self.preferred:
            out["preferred_unavailable"] = {"provider": self.preferred, "reason": self.preferred_reason}
        if self.elapsed_s is not None:
            out["elapsed_s"] = self.elapsed_s
        return out


def plan(op: str | Operation, caps: Any, *, prefer: str | None = None) -> Plan:
    """The providers of `op` whose predicates hold on `caps`, in declared order; `prefer` (a
    provider name, or None/"auto") moves that provider to the front when it is eligible, and
    E_BAD_ARGS when the operation has no such provider at all."""
    operation_ = op if isinstance(op, Operation) else operation(op)
    if prefer and prefer != "auto" and prefer not in operation_.names():
        raise KiwrapError(
            "E_BAD_ARGS",
            f"provider {prefer!r} does not serve {operation_.name}",
            fix="pass one of the candidates, or 'auto'",
            candidates=operation_.names(),
        )
    candidates: list[Provider] = []
    skipped: list[tuple[str, str]] = []
    for prov in operation_.providers:
        missing = prov.needs.missing(caps)
        if missing:
            skipped.append((prov.name, "missing capability " + ", ".join(missing)))
        else:
            candidates.append(prov)
    plan_ = Plan(operation_, candidates, skipped)
    if prefer and prefer != "auto":
        candidates.sort(key=lambda p: 0 if p.name == prefer else 1)
        if not any(c.name == prefer for c in candidates):
            plan_.preferred = prefer
            plan_.preferred_reason = next((r for p, r in skipped if p == prefer), "not eligible")
    return plan_


def unsupported(plan_: Plan, install: Any) -> KiwrapError:
    """The teaching error for "no provider can serve this operation on this install"."""
    op = plan_.operation
    needs = sorted({k for p in op.providers for k in p.needs.keys})
    version = getattr(install, "version", None)
    fix = f"use the fallback: {op.fallback}" if op.fallback else "select a KiCad that has the capability"
    return KiwrapError(
        "E_UNSUPPORTED",
        f"{op.name} is not available on KiCad {version}: " + "; ".join(f"{p}: {r}" for p, r in plan_.skipped),
        why="none of the operation's providers has its capability on the selected KiCad",
        fix=fix,
        data={
            "capability": needs[0] if len(needs) == 1 else None,
            "capabilities": needs,
            "fallback": op.fallback or None,
            "kicad": version,
            "plan": plan_.to_dict(),
        },
    )


@dataclass
class Outcome(Generic[T]):
    value: T
    provider: Provider
    plan: Plan
    degraded: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[tuple[str, KiwrapError]] = field(default_factory=list)  # providers that fell through
    patch: str = ""  # kiwrap patch id the answering provider relied on (docs/UPSTREAM.md §3.3)

    @property
    def provider_name(self) -> str:
        return self.provider.name

    def provider_used(self, install: Any) -> str:
        return provider_label(self.provider.name, install, patch=self.patch)


def provider_label(provider: str, install: Any, *, patch: str = "") -> str:
    """`provider_used` string: `ipc11:<version>`, `cli:<version>`, or plain `file`; a kiwrap
    patch the provider relied on is appended as `+<id>` (`cli:10.99.0+ratsnest-export`)."""
    if provider in UNVERSIONED:
        return provider
    version = getattr(install, "version", None)
    label = f"{provider}:{version}" if version else provider
    return f"{label}+{patch}" if patch else label


def run(
    op: str | Operation,
    caps: Any,
    attempts: Mapping[str, Callable[[], T]],
    *,
    install: Any = None,
    prefer: str | None = None,
) -> Outcome[T]:
    """Try the eligible providers in order (see the module docstring for what "unavailable" means).

    `attempts` maps a provider name to a zero-argument callable; a candidate without an attempt
    is skipped (the caller does not implement it). E_UNSUPPORTED when nothing is left.
    """
    plan_ = plan(op, caps, prefer=prefer)
    warnings: list[str] = []
    errors: list[tuple[str, KiwrapError]] = []
    forced_unavailable: list[str] = []
    if plan_.preferred:
        # A forced provider that this KiCad cannot serve is never silently swapped for another
        # (finding 22: provider='ipc11' on 10.0 answered from the file engine with degraded=[]).
        version = getattr(install, "version", None)
        warnings.append(
            f"provider {plan_.preferred!r} was requested but is not available on KiCad {version} "
            f"({plan_.preferred_reason}); answered by the next eligible provider - use provider='auto' "
            "or one of " + ", ".join(c.name for c in plan_.candidates)
        )
        forced_unavailable.append(f"{plan_.preferred}_unavailable")
    started = time.perf_counter()
    for prov in plan_.candidates:
        attempt = attempts.get(prov.name)
        if attempt is None:
            plan_.skipped.append((prov.name, "not attempted by this tool"))
            continue
        try:
            value = attempt()
        except Exception as exc:  # noqa: BLE001 - classified right below
            err = to_kiwrap_error(exc)
            if err.code not in UNAVAILABLE_CODES.get(prov.name, frozenset()):
                raise err from exc
            errors.append((prov.name, err))
            warnings.append(f"{prov.name} unavailable for {plan_.operation.name}: {err.code}: {err.message}")
            plan_.skipped.append((prov.name, f"{err.code}: {err.message}"))
            continue
        degraded = list(prov.degraded)
        if errors and prov.name != plan_.candidates[0].name:
            first = plan_.candidates[0].name
            tag = f"{first}_unavailable"
            if tag not in degraded:
                degraded.insert(0, tag)
        for tag in forced_unavailable:
            if tag not in degraded:
                degraded.insert(0, tag)
        plan_.elapsed_s = round(time.perf_counter() - started, 3)
        return Outcome(
            value=value, provider=prov, plan=plan_, degraded=degraded, warnings=warnings, errors=errors
        )
    err = unsupported(plan_, install)
    if errors:
        err.data["attempts"] = [{"provider": p, "code": e.code, "message": e.message} for p, e in errors]
        err.message += "; attempted: " + "; ".join(f"{p} -> {e.code}" for p, e in errors)
    raise err


def describe() -> dict[str, Any]:
    """The whole table, for kw_help / kw_env: {operation: [{provider, needs[], degraded[], note}]}."""
    return {
        name: {
            "providers": [
                {
                    "provider": p.name,
                    "needs": list(p.needs.keys),
                    "degraded": list(p.degraded),
                    "note": p.note,
                }
                for p in op.providers
            ],
            "fallback": op.fallback or None,
        }
        for name, op in OPERATIONS.items()
    }
