"""`netlist_from_file`: a `kiwrap.formats.reports.Netlist` derived purely from schematic files, in
the shape `kicad-cli sch export netlist --format kicadsexpr` produces, so the two can be diffed.

Component and node selection follow `NETLIST_EXPORTER_XML` (eeschema/netlist_exporters/
netlist_exporter_xml.cpp, 10.0 branch and master, read 2026-09-19): symbols excluded from the
board (`on_board no`) and `#` references (power symbols, flags) are left out; multi-unit symbols
appear once; a net gets one node per (reference, pin number); `pinfunction` is
`<shown name>_<pin number>` when the shown name is non-empty (`pin_function_style="name_number"`,
what 10.0.6 and the nightly write - verified: 9.0.9 writes `(pinfunction "P1")`, 10.0.6 / 10.99
`"P1_1"`) or the bare name (`"name"`, KiCad 9 and older; the caller picks the style from the
capability key `cli.netlist_pinfunction_suffix`); `pintype` is the canonical electrical type with
`+no_connect` appended only when the net's subgraph carries a no-connect MARKER and the net has
one node (or every node is the same stacked pin) - a `no_connect`-type pin without a marker
stays `no_connect`, never `no_connect+no_connect`; nets without a single listed node are
dropped; net codes are assigned in name order (KiCad's net map is ordered by name).

`compare_netlists` gives the agreement report the conformance suite prints: nets matched by
name, nets matched by node set only (naming differences), missing/extra nets and the
discrepancy classes they fall into (bus-fed, power flags, multi-unit, unconnected naming, …).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from kiwrap.formats.analysis.connectivity import Connectivity, pin_connectivity
from kiwrap.formats.reports import Component, Net, Netlist, NetNode
from kiwrap.formats.schematic import Hierarchy, SchematicFile

__all__ = ["NetlistComparison", "compare_netlists", "netlist_from_connectivity", "netlist_from_file"]


def netlist_from_file(
    source: SchematicFile | Hierarchy | str,
    *,
    kicad_version: str = "",
    pin_function_style: str = "name_number",
) -> Netlist:
    """Build the netlist of a root schematic (path, `SchematicFile` or `Hierarchy`).
    `pin_function_style`: "name_number" (KiCad 10+) or "name" (module docstring)."""
    if isinstance(source, str):
        source = Hierarchy.load(source)
    elif isinstance(source, SchematicFile):
        source = (
            Hierarchy.load(source.path)
            if source.sheets and source.path is not None
            else Hierarchy.single(source)
        )
    conn = pin_connectivity(source)
    src = str(source.root.path) if source.root.path else ""
    return netlist_from_connectivity(
        conn, source, source=src, kicad_version=kicad_version, pin_function_style=pin_function_style
    )


PIN_FUNCTION_STYLES = ("name_number", "name")


def netlist_from_connectivity(
    conn: Connectivity,
    hierarchy: Hierarchy | None,
    *,
    source: str = "",
    kicad_version: str = "",
    pin_function_style: str = "name_number",
) -> Netlist:
    if pin_function_style not in PIN_FUNCTION_STYLES:
        raise ValueError(
            f"pin_function_style must be one of {PIN_FUNCTION_STYLES}, got {pin_function_style!r}"
        )
    components: dict[str, Component] = {}
    if hierarchy is not None:
        for inst, sym in hierarchy.symbols():
            ref = sym.reference_for(inst.path)
            if not sym.on_board or ref.startswith("#") or ref in components:
                continue
            props = {k: v for k, v in sym.properties.items() if k not in ("Reference", "Value", "Footprint")}
            components[ref] = Component(
                ref=ref,
                value=sym.value,
                footprint=sym.footprint or None,
                lib_id=sym.lib_id or None,
                sheet_path=inst.names_path,
                properties=props,
                dnp=sym.dnp,
                exclude_from_board=False,
            )
    nets: list[Net] = []
    code = 0
    for dn in sorted(conn.nets, key=lambda n: n.name):
        pins = dn.netlist_pins
        if not pins or dn.is_bus:
            continue
        code += 1
        nc_suffix = "+no_connect" if dn.no_connect and _all_nodes_stacked(pins) else ""
        nodes = tuple(
            NetNode(
                ref=p.reference,
                pin=p.number,
                pin_function=_pin_function(p.name, p.number, pin_function_style),
                pin_type=p.type + nc_suffix,
            )
            for p in pins
        )
        nets.append(Net(code=code, name=dn.name, nodes=nodes))
    comps = tuple(components[r] for r in sorted(components))
    return Netlist(source=source, kicad_version=kicad_version, components=comps, nets=tuple(nets))


def _pin_function(name: str, number: str, style: str) -> str | None:
    """`(pinfunction ...)`: `<name>_<number>` (10.0+) or the bare name; nothing without a name."""
    if not name:
        return None
    return f"{name}_{number}" if style == "name_number" else name


def _all_nodes_stacked(pins) -> bool:  # noqa: ANN001 - PinRefs, kept light
    """`allNetPinsStacked` of NETLIST_EXPORTER_XML: one node, or every node the same symbol
    instance's pin at the same position with the same name (stacked pins)."""
    if len(pins) <= 1:
        return True
    first = pins[0]
    return all(
        p.symbol is first.symbol
        and p.path == first.path
        and p.position == first.position
        and p.pin.name == first.pin.name
        for p in pins[1:]
    )


# ----------------------------------------------------------------------------------------------
# comparison
# ----------------------------------------------------------------------------------------------


@dataclass
class NetlistComparison:
    """Agreement between a file-derived netlist (`ours`) and kicad-cli's (`theirs`)."""

    total_theirs: int
    total_ours: int
    exact: int  # same name and same (ref, pin) node set
    renamed: int  # same node set, different name
    missing: list[str]  # their nets with no counterpart (by name or node set)
    extra: list[str]  # our nets with no counterpart
    node_mismatch: list[tuple[str, tuple[str, ...], tuple[str, ...]]]  # (name, only theirs, only ours)
    renames: list[tuple[str, str]] = field(default_factory=list)  # (their name, our name)
    classes: dict[str, int] = field(default_factory=dict)
    # per-node attribute disagreements on exactly matched nets: (net, ref.pin, theirs, ours)
    function_mismatch: list[tuple[str, str, str | None, str | None]] = field(default_factory=list)
    type_mismatch: list[tuple[str, str, str | None, str | None]] = field(default_factory=list)
    nodes_compared: int = 0

    @property
    def agreement(self) -> float:
        return (self.exact + self.renamed) / self.total_theirs if self.total_theirs else 1.0

    @property
    def exact_agreement(self) -> float:
        return self.exact / self.total_theirs if self.total_theirs else 1.0

    def summary(self) -> str:
        return (
            f"{self.exact}/{self.total_theirs} exact, {self.renamed} same nodes renamed, "
            f"{len(self.missing)} missing, {len(self.extra)} extra, {len(self.node_mismatch)} node mismatches; "
            f"classes={self.classes}; nodes {self.nodes_compared}: {len(self.function_mismatch)} pinfunction "
            f"and {len(self.type_mismatch)} pintype differ"
        )


def _nodes(net: Net) -> frozenset[tuple[str, str]]:
    return frozenset((n.ref, n.pin) for n in net.nodes)


def compare_netlists(ours: Netlist, theirs: Netlist, conn: Connectivity | None = None) -> NetlistComparison:
    theirs_by_name = {n.name: n for n in theirs.nets}
    ours_by_name = {n.name: n for n in ours.nets}
    ours_by_nodes: dict[frozenset[tuple[str, str]], list[Net]] = {}
    for n in ours.nets:
        ours_by_nodes.setdefault(_nodes(n), []).append(n)
    matched_ours: set[str] = set()
    exact = renamed = 0
    missing: list[str] = []
    mismatch: list[tuple[str, tuple[str, ...], tuple[str, ...]]] = []
    renames: list[tuple[str, str]] = []
    classes: dict[str, int] = {}

    def classify(name: str, net: Net | None) -> None:
        cls = _discrepancy_class(name, net, conn)
        classes[cls] = classes.get(cls, 0) + 1

    function_mismatch: list[tuple[str, str, str | None, str | None]] = []
    type_mismatch: list[tuple[str, str, str | None, str | None]] = []
    nodes_compared = 0
    for name, tn in theirs_by_name.items():
        on = ours_by_name.get(name)
        if on is not None and _nodes(on) == _nodes(tn):
            exact += 1
            matched_ours.add(name)
            ours_nodes = {(x.ref, x.pin): x for x in on.nodes}
            for theirs_node in tn.nodes:
                mine = ours_nodes.get((theirs_node.ref, theirs_node.pin))
                if mine is None:
                    continue
                nodes_compared += 1
                key = f"{theirs_node.ref}.{theirs_node.pin}"
                if (theirs_node.pin_function or None) != (mine.pin_function or None):
                    function_mismatch.append((name, key, theirs_node.pin_function, mine.pin_function))
                if (theirs_node.pin_type or None) != (mine.pin_type or None):
                    type_mismatch.append((name, key, theirs_node.pin_type, mine.pin_type))
            continue
        cands = [c for c in ours_by_nodes.get(_nodes(tn), []) if c.name not in matched_ours]
        if cands:
            renamed += 1
            matched_ours.add(cands[0].name)
            renames.append((name, cands[0].name))
            classify(name, tn)
            continue
        if on is not None:
            only_theirs = tuple(sorted(f"{r}.{p}" for r, p in _nodes(tn) - _nodes(on)))
            only_ours = tuple(sorted(f"{r}.{p}" for r, p in _nodes(on) - _nodes(tn)))
            mismatch.append((name, only_theirs, only_ours))
            matched_ours.add(name)
            classify(name, tn)
            continue
        missing.append(name)
        classify(name, tn)
    extra = [n.name for n in ours.nets if n.name not in matched_ours]
    return NetlistComparison(
        total_theirs=len(theirs.nets),
        total_ours=len(ours.nets),
        exact=exact,
        renamed=renamed,
        missing=missing,
        extra=extra,
        node_mismatch=mismatch,
        renames=renames,
        classes=classes,
        function_mismatch=function_mismatch,
        type_mismatch=type_mismatch,
        nodes_compared=nodes_compared,
    )


def _discrepancy_class(name: str, net: Net | None, conn: Connectivity | None) -> str:
    """A coarse reason for a disagreement, for the conformance report."""
    if "[" in name and ".." in name:
        return "bus_vector_name"
    if conn is not None:
        ours = conn.net_by_name().get(name)
        if ours is not None and ours.via_bus_entry:
            return "bus_fed"
        if net is not None:
            for node in net.nodes:
                dn = conn.net_of_pin(node.ref, node.pin)
                if dn is not None and dn.via_bus_entry:
                    return "bus_fed"
    if name.startswith("unconnected-("):
        return "unconnected_naming"
    if name.startswith("Net-("):
        return "auto_name"
    if name.endswith(("_1", "_2", "_3")):
        return "weak_name_suffix"
    if net is not None and any(n.pin_type and n.pin_type.startswith("power") for n in net.nodes):
        return "power"
    return "other"
