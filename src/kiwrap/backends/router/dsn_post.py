"""DSN post-processing: what kiwrap changes in the Specctra design between KiCad's export and
Freerouting's input (docs/ARCHITECTURE.md §8 "DSN post-processing").

KiCad's exporter (pcbnew/specctra_import_export/specctra_export.cpp) writes `(string_quote ")`,
a lone double quote as an atom, which the KiCad s-expression CST rejects as an unterminated
string; the Specctra grammar also has no escapes. So this module carries its own tiny parser:
a node is a Python list whose first element is the head token, atoms are the raw token text
(quotes kept), and `dump_dsn` re-emits the tree with one child list per line. Freerouting reads
any whitespace layout; the exact formatting of KiCad's file is not preserved and does not matter.

Hooks (each takes the parsed tree, edits it in place and returns what it did):

* `restrict_nets(tree, route_nets)` - the nets NOT in `route_nets` are REMOVED from the
  `(network …)`: their `(net … (pins …))` entries and their class memberships go, their wiring
  in `(wiring …)` is made `(type fix)`. Freerouting 2.4.1 then has no airwire to route for them
  while their pads (pins of the component images with no net), planes and fixed wires stay
  obstacles it keeps clearance from. Verified on 2.4.1 (Phase 1c, blinker with /VIN stripped and
  the other seven nets excluded: the router reported 3 unrouted items instead of the board's,
  the SES carried /VIN alone, kicad-cli DRC after the apply: 0 errors, 0 unconnected).

  Why not `-inc` / `router.ignore_net_classes`: the headless 2.4.1 jar never consumes it (the
  only callers of `setIgnoredByAutorouter` are the GUI classes); probes with `-inc
  kiwrap_ignore`, `-inc kicad_default,Power` and `--router.ignore_net_classes=…` routed every
  net of the DSN (reference board: 18 nets routed for a 2-net request). The earlier "verified"
  note about `-inc kicad_default` skipping every pass was a misread of a run whose nets were
  already complete. Nets already routed by the user are additionally LOCKED by the tool before
  the export (`(type fix)` wires), which is what makes KiCad's SES import keep them; the
  restriction hook fixes the wiring of the removed nets the same way for a DSN written by hand.
* `dsn_summary(tree)` - read-only facts for the receipt (layers, nets, pins, wires, vias,
  classes, the structure rule, whether a boundary and planes exist).

Copper-to-edge clearance is NOT written into the DSN (the `(rule …)` grammar has no such
clearance type Freerouting honours); it goes in as `--router.copper_to_edge_clearance_um`
(`kiwrap.backends.router.rules`).
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from typing import Any

DsnNode = list  # [head: str, *children: str | DsnNode]

IGNORE_CLASS = "kiwrap_ignore"


class DsnError(ValueError):
    """The text is not a Specctra s-expression this module can parse."""


# ---- parser ------------------------------------------------------------------------------------------


def _tokens(text: str) -> list[str]:
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch.isspace():
            i += 1
            continue
        if ch in "()":
            out.append(ch)
            i += 1
            continue
        if ch == '"':
            # `(string_quote ")`: the quote character itself is the atom
            if out and out[-1] == "string_quote":
                out.append('"')
                i += 1
                continue
            j = text.find('"', i + 1)
            if j < 0:
                raise DsnError(f"unterminated string at offset {i}")
            out.append(text[i : j + 1])
            i = j + 1
            continue
        j = i
        while j < n and not text[j].isspace() and text[j] not in "()":
            j += 1
        out.append(text[i:j])
        i = j
    return out


def parse_dsn(text: str) -> DsnNode:
    """Parse one Specctra design / session into nested lists (see the module docstring)."""
    tokens = _tokens(text)
    stack: list[DsnNode] = []
    root: DsnNode | None = None
    for tok in tokens:
        if tok == "(":
            node: DsnNode = []
            if stack:
                stack[-1].append(node)
            stack.append(node)
        elif tok == ")":
            if not stack:
                raise DsnError("unbalanced ')'")
            node = stack.pop()
            if not node:
                raise DsnError("empty list '()' is not valid Specctra")
            if not stack:
                root = node
        else:
            if not stack:
                raise DsnError(f"token {tok!r} outside any list")
            stack[-1].append(tok)
    if stack:
        raise DsnError("unbalanced '(': the design ends inside a list")
    if root is None:
        raise DsnError("no list found")
    return root


def dump_dsn(node: DsnNode, indent: int = 0) -> str:
    """Re-emit a tree: atoms of a list on the head's line, child lists on their own lines."""
    out: list[str] = []
    _emit(node, indent, out)
    return "\n".join(out) + "\n"


def _emit(node: DsnNode, depth: int, out: list[str]) -> None:
    pad = "  " * depth
    head = str(node[0])
    atoms = [str(a) for a in node[1:] if not isinstance(a, list)]
    lists = [c for c in node[1:] if isinstance(c, list)]
    line = pad + "(" + " ".join([head, *atoms])
    if not lists:
        out.append(line + ")")
        return
    out.append(line)
    for child in lists:
        _emit(child, depth + 1, out)
    out.append(pad + ")")


# ---- helpers -----------------------------------------------------------------------------------------


def head(node: Any) -> str:
    return str(node[0]) if isinstance(node, list) and node else ""


def atoms(node: DsnNode) -> list[str]:
    return [str(a) for a in node[1:] if not isinstance(a, list)]


def lists(node: DsnNode) -> list[DsnNode]:
    return [c for c in node[1:] if isinstance(c, list)]


def find(node: DsnNode, name: str) -> DsnNode | None:
    return next((c for c in lists(node) if head(c) == name), None)


def find_all(node: DsnNode, name: str) -> list[DsnNode]:
    return [c for c in lists(node) if head(c) == name]


def unquote(atom: str) -> str:
    a = str(atom)
    if len(a) >= 2 and a[0] == '"' and a[-1] == '"':
        return a[1:-1]
    return a


def quote_if_needed(name: str) -> str:
    """KiCad quotes a DSN token when it holds a space, parenthesis, quote or is empty."""
    if not name or any(ch in name for ch in ' ()"\t\n-'):
        return '"' + name + '"'
    return name


# ---- hooks -------------------------------------------------------------------------------------------


@dataclass
class RestrictReport:
    """`ignored_nets` were removed from the network (their wiring fixed); `ignore_class` is kept
    for callers that still read it - no `-inc` class is created any more (module docstring)."""

    ignore_class: str = IGNORE_CLASS
    routed_nets: list[str] = field(default_factory=list)
    ignored_nets: list[str] = field(default_factory=list)
    unknown_nets: list[str] = field(default_factory=list)  # requested nets the DSN does not have
    applied: bool = False
    wires_fixed: int = 0


def _class_split(cls: DsnNode) -> tuple[str, list[str], list[DsnNode]]:
    """`(class NAME net… (circuit…) (rule…))` -> (name, nets, child lists)."""
    at = atoms(cls)
    name = at[0] if at else ""
    return name, at[1:], lists(cls)


def restrict_nets(tree: DsnNode, route_nets: list[str] | set[str] | tuple[str, ...]) -> RestrictReport:
    """Keep only `route_nets` routable: every other net leaves the `(network …)` (its `(net …)`
    entry and its class memberships are removed) and its wiring becomes `(type fix)`, so the
    router has nothing to route for it while its copper and pads remain obstacles (module
    docstring). The bogus `@:no_net_N` nets of netless planes are left alone. A `kiwrap_ignore`
    class of an older DSN is dissolved. Nets not present in the DSN are reported in
    `unknown_nets`."""
    wanted = {str(n) for n in route_nets}
    report = RestrictReport()
    network = find(tree, "network")
    if network is None:
        raise DsnError("the design has no (network …) section")
    present = {unquote(atoms(n)[0]) for n in find_all(network, "net") if atoms(n)}
    report.unknown_nets = sorted(wanted - present)
    removed: set[str] = set()
    for net in list(find_all(network, "net")):
        at = atoms(net)
        name = unquote(at[0]) if at else ""
        if name in wanted or name.startswith("@:"):
            continue
        network.remove(net)
        removed.add(name)
    for cls in list(find_all(network, "class")):
        name, nets, children = _class_split(cls)
        if name == IGNORE_CLASS:
            network.remove(cls)  # an older restriction's class: its nets are re-evaluated above
            continue
        keep = [n for n in nets if unquote(n) in wanted]
        if len(keep) != len(nets):
            removed.update(unquote(n) for n in nets if unquote(n) not in wanted)
            cls[:] = [cls[0], name, *keep, *children]
    wiring = find(tree, "wiring")
    if wiring is not None:
        for item in lists(wiring):
            if head(item) not in ("wire", "via"):
                continue
            ref = find(item, "net")
            net_name = unquote(atoms(ref)[0]) if ref is not None and atoms(ref) else ""
            if net_name in wanted or not net_name:
                continue
            kind = find(item, "type")
            if kind is None:
                item.append(["type", "fix"])
                report.wires_fixed += 1
            elif atoms(kind) != ["fix"]:
                kind[:] = ["type", "fix"]
                report.wires_fixed += 1
    report.applied = bool(removed) or report.wires_fixed > 0
    report.ignored_nets = sorted(removed)
    report.routed_nets = sorted(n for n in present if n in wanted)
    return report


def _copy(node: DsnNode) -> DsnNode:
    return [_copy(c) if isinstance(c, list) else c for c in node]


def dsn_summary(tree: DsnNode) -> dict[str, Any]:
    """Read-only facts about a design for receipts and tests."""
    structure = find(tree, "structure")
    network = find(tree, "network")
    wiring = find(tree, "wiring")
    layers = [atoms(layer)[0] for layer in find_all(structure, "layer")] if structure is not None else []
    rule: dict[str, float] = {}
    if structure is not None:
        r = find(structure, "rule")
        if r is not None:
            for entry in lists(r):
                if head(entry) in ("width", "clearance") and atoms(entry) and not lists(entry):
                    with contextlib.suppress(ValueError):
                        rule.setdefault(head(entry), float(atoms(entry)[0]))
    nets: dict[str, int] = {}
    classes: dict[str, list[str]] = {}
    if network is not None:
        for net in find_all(network, "net"):
            at = atoms(net)
            pins = find(net, "pins")
            nets[unquote(at[0]) if at else ""] = len(atoms(pins)) if pins is not None else 0
        for cls in find_all(network, "class"):
            name, members, _ = _class_split(cls)
            classes[name] = [unquote(m) for m in members]
    wires = find_all(wiring, "wire") if wiring is not None else []
    vias = find_all(wiring, "via") if wiring is not None else []
    fixed = sum(1 for w in [*wires, *vias] if any(head(c) == "type" and "fix" in atoms(c) for c in lists(w)))
    return {
        "layers": layers,
        "boundary": structure is not None and find(structure, "boundary") is not None,
        "planes": [unquote(atoms(p)[0]) for p in find_all(structure, "plane") if atoms(p)]
        if structure is not None
        else [],
        "keepouts": len([c for c in lists(structure) if head(c).endswith("keepout")])
        if structure is not None
        else 0,
        "rule": rule,
        "nets": nets,
        "classes": classes,
        "wires": len(wires),
        "vias": len(vias),
        "fixed": fixed,
        "components": sum(
            len(find_all(comp, "place"))
            for comp in find_all(find(tree, "placement") or ["placement"], "component")
        ),
    }


@dataclass
class PostResult:
    text: str
    summary: dict[str, Any]
    restrict: RestrictReport | None = None
    changed: bool = False

    @property
    def ignore_classes(self) -> list[str]:
        """`-inc` classes the restriction asks for: none - the removal from the network does the
        job on its own (module docstring); the property stays for the router's argv builder."""
        return []


def postprocess_dsn(text: str, *, route_nets: list[str] | None = None) -> PostResult:
    """Parse, apply the hooks, re-emit. Without `route_nets` the text is returned unchanged
    (byte-identical: KiCad's own layout is kept when nothing has to change)."""
    tree = parse_dsn(text)
    summary = dsn_summary(tree)
    if route_nets is None:
        return PostResult(text=text, summary=summary)
    report = restrict_nets(tree, route_nets)
    if not report.applied:
        return PostResult(text=text, summary=summary, restrict=report)
    return PostResult(text=dump_dsn(tree), summary=dsn_summary(tree), restrict=report, changed=True)


__all__ = [
    "IGNORE_CLASS",
    "DsnError",
    "DsnNode",
    "PostResult",
    "RestrictReport",
    "atoms",
    "dsn_summary",
    "dump_dsn",
    "find",
    "find_all",
    "head",
    "lists",
    "parse_dsn",
    "postprocess_dsn",
    "quote_if_needed",
    "restrict_nets",
    "unquote",
]
