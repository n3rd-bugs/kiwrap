"""The schematic-writer spike designs (docs/ARCHITECTURE.md §7 "Phase 1 opens with a spike") as
reusable builders, shared by `tests/test_sch_writer*.py` and `conformance/tests/t8_sch_writer.py`.

Each builder drives a `SchematicWriter` and returns what KiCad must report for the design:
the expected netlist partition as sets of `(ref, pin)` nodes (net names are left to KiCad, the
partition is the intent), plus the references / files involved. Every coordinate is chosen on
the 1.27 mm grid; pin positions always come from the writer (never hand-computed).

1. `build_blink`: LED + resistor + 2-pin connector + +5V / GND power symbols + PWR_FLAGs — the
   claim-2 recipe (ERC 0 errors, intended netlist).
2. `build_hierarchy`: root + `source.kicad_sch` + `amp.kicad_sch` placed TWICE (amp_a / amp_b),
   a multi-unit op-amp (LM358 units A, B and the power unit) per amp instance, hierarchical
   labels + sheet pins, global labels for the outputs — the §7 "not yet verified" list.
3. `build_orientations`: an asymmetric 3-pin potentiometer (all passive) placed in all 12 orientations, every
   pin wired to its own label — the claim-3 transform under ERC.
4. `build_demorgan`: a quad NAND (74xx:74LS00, four gate units with a De Morgan alternate body
   plus a power unit) — the placed symbol must carry ONE `(pin …)` entry per library pin of its
   own body style (a `sch upgrade` re-save drops duplicates) and report each unit pin once.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from kiwrap.formats.schematic_writer import SchematicWriter

__all__ = ["Design", "build_blink", "build_demorgan", "build_hierarchy", "build_orientations"]

Node = tuple[str, str]


@dataclass
class Design:
    root: Path
    #: intended connectivity: every set is one net (only real components; power symbols and
    #: flags never appear in kicad-cli netlists)
    nets: list[frozenset[Node]]
    refs: set[str]
    files: list[Path] = field(default_factory=list)
    named: dict[str, frozenset[Node]] = field(default_factory=dict)  # net name -> nodes, when fixed
    extra: dict[str, object] = field(default_factory=dict)

    def all_nodes(self) -> frozenset[Node]:
        return frozenset(n for net in self.nets for n in net)


def _net(*nodes: Node) -> frozenset[Node]:
    return frozenset(nodes)


# ----------------------------------------------------------------------------------------------
# 1. blink
# ----------------------------------------------------------------------------------------------


def build_blink(w: SchematicWriter, root: Path) -> Design:
    w.load(root)
    j1 = w.add_symbol("Connector_Generic:Conn_01x02", "J1", None, (38.1, 63.5))
    r1 = w.add_symbol("Device:R", "R1", "330", (63.5, 63.5), rotation=90)
    d1 = w.add_symbol("Device:LED", "D1", None, (88.9, 76.2), rotation=90)
    v5 = w.add_power("+5V", (38.1, 50.8))
    gnd = w.add_power("GND", (38.1, 88.9))
    flag_v = w.add_power("+5V", (50.8, 50.8), flag=True)
    flag_g = w.add_power("GND", (63.5, 88.9), flag=True)
    assert v5.ref == "#PWR01" and gnd.ref == "#PWR02" and flag_v.ref == "#FLG01" and flag_g.ref == "#FLG02"
    # +5V rail: J1.1 up to the +5V pin, PWR_FLAG on the rail; J1.1 right to R1.1
    w.add_wire([j1.pin("1").at_mm, (j1.pin("1").x_mm, v5.pin("1").y_mm), v5.pin("1").at_mm])
    w.add_wire([v5.pin("1").at_mm, flag_v.pin("1").at_mm])
    w.add_wire([j1.pin("1").at_mm, r1.pin("1").at_mm])
    # R1.2 -> LED anode (LED rot 90: A on top, K at the bottom)
    w.add_wire([r1.pin("2").at_mm, (d1.pin("A").x_mm, r1.pin("2").y_mm), d1.pin("A").at_mm])
    # LED cathode -> GND rail; J1.2 -> GND rail; flag on the rail
    w.add_wire([d1.pin("K").at_mm, (d1.pin("K").x_mm, gnd.pin("1").y_mm), gnd.pin("1").at_mm])
    w.add_wire([j1.pin("2").at_mm, (j1.pin("2").x_mm, gnd.pin("1").y_mm), gnd.pin("1").at_mm])
    w.add_wire([flag_g.pin("1").at_mm, gnd.pin("1").at_mm])
    w.assign_footprints(
        {
            "R1": "Resistor_SMD:R_0603_1608Metric",
            "D1": "LED_SMD:LED_0603_1608Metric",
            "J1": "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical",
        }
    )
    files = w.save()
    return Design(
        root=root,
        nets=[
            _net(("J1", "1"), ("R1", "1")),
            _net(("D1", "1"), ("J1", "2")),
            _net(("D1", "2"), ("R1", "2")),
        ],
        refs={"J1", "R1", "D1"},
        files=files,
        named={
            "+5V": _net(("J1", "1"), ("R1", "1")),
            "GND": _net(("D1", "1"), ("J1", "2")),
        },
        extra={"uuids": {"J1": j1.uuid, "R1": r1.uuid, "D1": d1.uuid}},
    )


# ----------------------------------------------------------------------------------------------
# 2. hierarchy with a twice-instantiated sheet and a multi-unit op-amp
# ----------------------------------------------------------------------------------------------


def build_hierarchy(
    w: SchematicWriter, root: Path, *, populate_before_second_instance: bool = False
) -> Design:
    """Root: `source` sheet (connector + power) feeding two instances of `amp.kicad_sch`.

    `populate_before_second_instance=True` fills amp.kicad_sch after placing it once and then
    places it a second time (the writer must then give every symbol a second reference);
    False places both instances first (references are allocated for both paths at once)."""
    w.load(root)
    src = w.add_sheet("source", "source.kicad_sch", (38.1, 50.8), (25.4, 20.32), [("SIG", "right", "output")])
    amp_a = w.add_sheet(
        "amp_a",
        "amp.kicad_sch",
        (88.9, 38.1),
        (25.4, 20.32),
        [("IN", "left", "input"), ("OUT", "right", "output")],
    )
    if not populate_before_second_instance:
        amp_b = w.add_sheet(
            "amp_b",
            "amp.kicad_sch",
            (88.9, 71.12),
            (25.4, 20.32),
            [("IN", "left", "input"), ("OUT", "right", "output")],
        )

    # --- source sheet: J1 pin 1 = +5V, pin 2 = SIG, pin 3 = GND, flags on both rails
    j1 = w.add_symbol("Connector_Generic:Conn_01x03", "J1", None, (38.1, 63.5), sheet="source")
    v5 = w.add_power("+5V", (25.4, 50.8), sheet="source")
    fv = w.add_power("+5V", (17.78, 50.8), flag=True, sheet="source")
    gnd = w.add_power("GND", (25.4, 76.2), sheet="source")
    fg = w.add_power("GND", (17.78, 76.2), flag=True, sheet="source")
    w.add_wire([j1.pin("1").at_mm, (25.4, j1.pin("1").y_mm), v5.pin("1").at_mm], sheet="source")
    w.add_wire([v5.pin("1").at_mm, fv.pin("1").at_mm], sheet="source")
    w.add_wire([j1.pin("3").at_mm, (25.4, j1.pin("3").y_mm), gnd.pin("1").at_mm], sheet="source")
    w.add_wire([gnd.pin("1").at_mm, fg.pin("1").at_mm], sheet="source")
    w.add_wire([j1.pin("2").at_mm, (20.32, j1.pin("2").y_mm)], sheet="source")
    w.add_label(
        "SIG", (20.32, j1.pin("2").y_mm), "hierarchical", rotation=180, sheet="source", shape="output"
    )

    # --- amp sheet: R in series with a two-stage LM358 follower, power unit on +5V/GND
    r = w.add_symbol("Device:R", None, "1k", (78.74, 73.66), rotation=90, sheet="amp")
    ua = w.add_symbol("Amplifier_Operational:LM358", None, None, (101.6, 76.2), sheet="amp")
    ub = w.add_symbol("Amplifier_Operational:LM358", ua.ref, None, (101.6, 101.6), unit=2, sheet="amp")
    uc = w.add_symbol("Amplifier_Operational:LM358", ua.ref, None, (139.7, 88.9), unit=3, sheet="amp")
    assert ub.ref == ua.ref == uc.ref
    w.add_wire([(67.31, 73.66), r.pin("1").at_mm], sheet="amp")
    w.add_label("IN", (67.31, 73.66), "hierarchical", rotation=180, sheet="amp", shape="input")
    w.add_wire([r.pin("2").at_mm, ua.pin("3").at_mm], sheet="amp")
    out_a = ua.pin("1").at_mm
    w.add_wire(
        [
            out_a,
            (114.3, out_a[1]),
            (114.3, 83.82),
            (88.9, 83.82),
            (88.9, ua.pin("2").y_mm),
            ua.pin("2").at_mm,
        ],
        sheet="amp",
    )
    w.add_wire(
        [
            (114.3, out_a[1]),
            (116.84, out_a[1]),
            (116.84, 91.44),
            (88.9, 91.44),
            (88.9, ub.pin("5").y_mm),
            ub.pin("5").at_mm,
        ],
        sheet="amp",
    )
    out_b = ub.pin("7").at_mm
    w.add_wire(
        [
            out_b,
            (114.3, out_b[1]),
            (114.3, 109.22),
            (88.9, 109.22),
            (88.9, ub.pin("6").y_mm),
            ub.pin("6").at_mm,
        ],
        sheet="amp",
    )
    w.add_wire([(114.3, out_b[1]), (121.92, out_b[1])], sheet="amp")
    w.add_label("OUT", (121.92, out_b[1]), "hierarchical", rotation=0, sheet="amp", shape="output")
    vp = w.add_power("+5V", (uc.pin("8").x_mm, uc.pin("8").y_mm - 5.08), sheet="amp")
    vm = w.add_power("GND", (uc.pin("4").x_mm, uc.pin("4").y_mm + 5.08), sheet="amp")
    w.add_wire([uc.pin("8").at_mm, vp.pin("1").at_mm], sheet="amp")
    w.add_wire([uc.pin("4").at_mm, vm.pin("1").at_mm], sheet="amp")

    if populate_before_second_instance:
        amp_b = w.add_sheet(
            "amp_b",
            "amp.kicad_sch",
            (88.9, 71.12),
            (25.4, 20.32),
            [("IN", "left", "input"), ("OUT", "right", "output")],
        )

    # --- root wiring: SIG -> both IN pins (junction), OUT pins -> global labels
    sig = src.pin("SIG").at_mm
    in_a, in_b = amp_a.pin("IN").at_mm, amp_b.pin("IN").at_mm
    w.add_wire([sig, (76.2, sig[1]), (76.2, in_a[1]), in_a])
    w.add_wire([(76.2, sig[1]), (76.2, in_b[1]), in_b])
    for sheet_pin, name in ((amp_a.pin("OUT"), "OUT_A"), (amp_b.pin("OUT"), "OUT_B")):
        w.add_wire([sheet_pin.at_mm, (sheet_pin.x_mm + 7.62, sheet_pin.y_mm)])
        w.add_label(name, (sheet_pin.x_mm + 7.62, sheet_pin.y_mm), "global", rotation=0, shape="output")
    w.assign_footprints(
        {
            "J1": "Connector_PinHeader_2.54mm:PinHeader_1x03_P2.54mm_Vertical",
            ua.ref: "Package_DIP:DIP-8_W7.62mm",
        }
    )
    files = w.save()

    # references per instance: amp_a carries the first allocation, amp_b the second
    refs_a = {p: r for p, r in ua.references.items()} if not populate_before_second_instance else None
    syms = w.symbols("amp")
    u_refs = sorted({ref for s in syms if s.lib_id.endswith("LM358") for ref in s.references.values()})
    r_refs = sorted({ref for s in syms if s.lib_id == "Device:R" for ref in s.references.values()})
    assert len(u_refs) == 2 and len(r_refs) == 2, (u_refs, r_refs)
    u1, u2 = u_refs
    r1, r2 = r_refs
    nets = [
        _net(("J1", "1"), (u1, "8"), (u2, "8")),
        _net(("J1", "3"), (u1, "4"), (u2, "4")),
        _net(("J1", "2"), (r1, "1"), (r2, "1")),
    ]
    for u, rr in ((u1, r1), (u2, r2)):
        nets.append(_net((rr, "2"), (u, "3")))
        nets.append(_net((u, "1"), (u, "2"), (u, "5")))
        nets.append(_net((u, "6"), (u, "7")))
    return Design(
        root=root,
        nets=nets,
        refs={"J1", u1, u2, r1, r2},
        files=files,
        named={
            "+5V": _net(("J1", "1"), (u1, "8"), (u2, "8")),
            "GND": _net(("J1", "3"), (u1, "4"), (u2, "4")),
        },
        extra={
            "u_refs": u_refs,
            "r_refs": r_refs,
            "refs_a": refs_a,
            "amp_a": amp_a,
            "amp_b": amp_b,
            "source": src,
        },
    )


# ----------------------------------------------------------------------------------------------
# 3. twelve orientations
# ----------------------------------------------------------------------------------------------

ORIENTATIONS: list[tuple[int, str | None]] = [(r, m) for r in (0, 90, 180, 270) for m in (None, "x", "y")]


def build_orientations(w: SchematicWriter, root: Path, lib_id: str = "Device:R_Potentiometer") -> Design:
    """One asymmetric 3-pin potentiometer per orientation, each pin wired 5.08 mm outward to a
    label; pin N of symbols 2k-1 and 2k share the label `P<k>_<N>` (a label on a single pin is
    `label_dangling` on 8/9 and `isolated_pin_label` on 10 — KiCad's rule, verified), so every
    net has exactly two nodes. A wrong transform leaves pins unconnected / wires dangling."""
    w.load(root)
    nets: dict[str, set[Node]] = {}
    refs: set[str] = set()
    for i, (rot, mirror) in enumerate(ORIENTATIONS):
        x = 38.1 + (i % 4) * 38.1
        y = 50.8 + (i // 4) * 38.1
        q = w.add_symbol(lib_id, f"Q{i + 1}", None, (x, y), rotation=rot, mirror=mirror)
        refs.add(q.ref)
        for pin in q.pins:
            dx, dy = pin.x_mm - x, pin.y_mm - y
            # extend outward along the dominant axis so wires never cross the body
            if abs(dx) >= abs(dy):
                end = (pin.x_mm + (5.08 if dx > 0 else -5.08), pin.y_mm)
            else:
                end = (pin.x_mm, pin.y_mm + (5.08 if dy > 0 else -5.08))
            w.add_wire([pin.at_mm, end])
            name = f"P{i // 2 + 1}_{pin.number}"
            w.add_label(name, end, "local", rotation=0 if end[0] >= pin.x_mm else 180)
            nets.setdefault(name, set()).add((q.ref, pin.number))
    files = w.save()
    return Design(root=root, nets=[frozenset(v) for v in nets.values()], refs=refs, files=files)


# ----------------------------------------------------------------------------------------------
# 4. De Morgan alternate body style (74xx:74LS00)
# ----------------------------------------------------------------------------------------------


def build_demorgan(w: SchematicWriter, root: Path, lib_id: str = "74xx:74LS00") -> Design:
    """U1 gate A as an inverter driven by J1 pin 1 (inputs tied), its output through R1 back to J1
    pin 2, gates B-D placed with every pin marked no-connect, the power unit on VCC / GND with
    PWR_FLAGs.

    The library symbol has `_U_2` (De Morgan) blocks for the gates: every pin must appear once in
    `SymbolPlacement.pins`, `pins_of()` and the saved `(pin "N" (uuid …))` list."""
    w.load(root)
    ua = w.add_symbol(lib_id, "U1", None, (63.5, 63.5))
    assert [p.number for p in ua.pins] == ["1", "2", "3"], [p.number for p in ua.pins]
    others = [w.add_symbol(lib_id, "U1", None, (63.5, 63.5 + 25.4 * k), unit=k + 1) for k in (1, 2, 3)]
    up = w.add_symbol(lib_id, "U1", None, (139.7, 63.5), unit=5)
    assert {p.number for p in up.pins} == {"7", "14"}, [p.number for p in up.pins]
    assert len(w.pins_of("U1")) == 14, [p.number for p in w.pins_of("U1")]
    r1 = w.add_symbol("Device:R", "R1", "1k", (88.9, 63.5), rotation=90)
    j1 = w.add_symbol("Connector_Generic:Conn_01x02", "J1", None, (38.1, 63.5))
    a, b, y = ua.pin("1").at_mm, ua.pin("2").at_mm, ua.pin("3").at_mm
    tie = (a[0] - 5.08, a[1])
    w.add_wire([a, tie, (tie[0], b[1]), b])
    w.add_wire(
        [j1.pin("1").at_mm, (j1.pin("1").x_mm + 2.54, j1.pin("1").y_mm), (tie[0], j1.pin("1").y_mm), tie]
    )
    w.add_wire([y, r1.pin("1").at_mm])
    out = (r1.pin("2").x_mm, r1.pin("2").y_mm - 5.08)
    w.add_wire([r1.pin("2").at_mm, out, (j1.pin("2").x_mm + 2.54, out[1]), j1.pin("2").at_mm])
    for u in others:
        for pin in u.pins:
            w.add_no_connect(pin.at_mm)
    vcc = w.add_power("VCC", (up.pin("14").x_mm, up.pin("14").y_mm - 2.54))
    gnd = w.add_power("GND", (up.pin("7").x_mm, up.pin("7").y_mm + 2.54))
    fv = w.add_power("VCC", (vcc.pin("1").x_mm + 7.62, vcc.pin("1").y_mm), flag=True)
    fg = w.add_power("GND", (gnd.pin("1").x_mm + 7.62, gnd.pin("1").y_mm), flag=True)
    w.add_wire([up.pin("14").at_mm, vcc.pin("1").at_mm])
    w.add_wire([vcc.pin("1").at_mm, fv.pin("1").at_mm])
    w.add_wire([up.pin("7").at_mm, gnd.pin("1").at_mm])
    w.add_wire([gnd.pin("1").at_mm, fg.pin("1").at_mm])
    w.assign_footprints(
        {
            "U1": "Package_DIP:DIP-14_W7.62mm",
            "R1": "Resistor_SMD:R_0603_1608Metric",
            "J1": "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical",
        }
    )
    files = w.save()
    return Design(
        root=root,
        nets=[
            _net(("J1", "1"), ("U1", "1"), ("U1", "2")),
            _net(("U1", "3"), ("R1", "1")),
            _net(("R1", "2"), ("J1", "2")),
            _net(("U1", "14")),
            _net(("U1", "7")),
            *[_net(("U1", p.number)) for u in others for p in u.pins],  # no-connect pins: 1-node nets
        ],
        refs={"U1", "R1", "J1"},
        files=files,
        named={"VCC": _net(("U1", "14")), "GND": _net(("U1", "7"))},
        extra={"uuids": {"U1": ua.uuid}, "unit_pins": [p.number for p in ua.pins]},
    )
