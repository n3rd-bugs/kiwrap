"""Specctra session (SES) reader and its application to a board — the SWIG-free twin of KiCad's
`ImportSpecctraSES` (pcbnew/specctra_import_export/specctra_import.cpp, tag 10.0.6).

What a Freerouting 2.4.1 session holds (spikes/autorouting/*/tiny.ses, med10/medium.ses; the
spec grammar as KiCad's `SPECCTRA_DB::doSESSION` reads it):

    (session <name> (base_design <name>)
      (placement (resolution um 10) (component <image> (place <ref> x y front|back rot))*)
      (was_is)
      (routes (resolution um 10) (parser …)
        (library_out (padstack <id> (shape (circle <layer> dia [x y]))+ (attach off))*)
        (network_out (net <name> (wire (path <layer> width x y x y …) [(type …)])* (via <padstack> x y)*)*)))

Coordinates are in `resolution` units (Freerouting echoes KiCad's `(resolution um 10)`: tenths
of a micrometre, y up) and are converted to millimetres here; each section's own resolution
applies (KiCad: `place.GetUnits()` for placements, `route->GetUnits()` for wires and vias).

`apply()` reproduces `SPECCTRA_DB::FromSESSION` (docs/VERIFICATION.md claim 7, verified on
8.0.1 / 9.0.5 / 10.0.x):

1. every unlocked track, arc and via of the board is deleted, locked ones survive (they were
   exported `(type fix)` and are not in the session). `keep_nets` additionally preserves the
   copper of nets the router was told to ignore (`DsnWriter(nets=…)` writes them `fix` too);
2. the placement section is *not* applied by default (the caller asked for routing, and KiCad's
   importer moves even locked footprints — claim 7 correction 2); it is counted in
   `placement_ignored`. `apply_placement=True` moves/flips/rotates like KiCad does (`rotation`
   on the front, `rotation + 180` flipped on the back);
3. `network_out` wires become one `(segment …)` per path leg on the layer named in the path
   (DSN layer ids are the board's user layer names when set, mapped back to canonical names),
   with the path's aperture as width and the net resolved *by name* (an unknown net gives an
   unconnected track, as KiCad's `FindNet` -> netcode 0 does); `(type fix)` wires come back locked;
4. vias take their diameter from the `library_out` padstack's circle, the drill from the padstack
   name (`Via[0-1]_600:300_um` -> 0.3 mm) with KiCad's `makeVIA` rule: a drill equal to the
   Default netclass drill - or a padstack without one - is UNDEFINED to the importer and KiCad's
   save materialises `PCB_VIA::GetDrillValue()`, the via's own NET's effective netclass drill
   (pcb_io_kicad_sexpr.cpp tag 10.0.6 always writes `(drill …)`), so a GND via of a Power class
   with `via_drill 0.5` comes back as `(drill 0.5)` even though the router used the Default
   padstack (`rules`, else the `.kicad_pro` next to the board through `rules_from_project`), and
   their layer pair from the padstack's shape layers: one shape or one per copper layer -> through
   (F.Cu/B.Cu); otherwise blind/buried (`micro` when the pair is the outermost two layers) between
   the shallowest and deepest layer named (a padstack naming only the two outer layers of a
   deeper stack is a through via here; KiCad's importer would call it buried, but Freerouting
   echoes the DSN padstacks, which carry one circle per spanned layer, so the case never comes
   from the router). Unknown layers / padstacks are skipped and reported, like KiCad 10.0.6 does
   (it warns "%d session item(s) were skipped").

After `apply()` the caller must refill zones and run DRC (docs/ARCHITECTURE.md §8): the session
carries no fills, and KiCad's own importer ends with `BuildConnectivity()` for the same reason.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from kiwrap.formats.board import BoardFile
from kiwrap.formats.board_writer import BoardWriter
from kiwrap.formats.sexpr import Atom, Node, build
from kiwrap.formats.specctra.dsn_reader import load_specctra
from kiwrap.formats.specctra.dsn_writer import SpecctraError, copper_stack
from kiwrap.formats.specctra.rules import DsnRules, rules_from_project

__all__ = [
    "SesPlace",
    "SesReader",
    "SesReport",
    "SesVia",
    "SesWire",
    "drill_from_padstack_id",
    "session_scale",
]

# nanometres per engineering unit (specctra_import.cpp `scale()`)
_NM_PER_UNIT = {"inch": 25.4e6, "mil": 25.4e3, "cm": 1e7, "mm": 1e6, "um": 1e3}


def session_scale(unit: str, resolution: int) -> float:
    """Millimetres per session coordinate unit for `(resolution <unit> <value>)`."""
    nm = _NM_PER_UNIT.get(unit, 25.4e6)
    return nm / max(1, resolution) / 1e6


def drill_from_padstack_id(padstack_id: str) -> float | None:
    """The drill encoded after the `:` and before the last `_` of a KiCad via padstack id, in mm."""
    colon = padstack_id.find(":")
    if colon < 0:
        return None
    end = padstack_id.rfind("_")
    if end < colon:
        return None
    text = padstack_id[colon + 1 : end].strip()
    try:
        return float(text) / 1000.0
    except ValueError:
        return None


@dataclass(frozen=True)
class SesWire:
    layer: str  # DSN layer id as written
    width: float  # mm
    points: tuple[tuple[float, float], ...]  # mm, KiCad y-down
    net: str
    type: str = "route"


@dataclass(frozen=True)
class SesVia:
    padstack: str
    x: float
    y: float
    net: str
    type: str = "route"


@dataclass(frozen=True)
class SesPlace:
    ref: str
    x: float
    y: float
    side: str
    rotation: float


@dataclass(frozen=True)
class SesPadstack:
    id: str
    diameter: float  # mm (first circle)
    layers: tuple[str, ...]  # DSN layer ids of its shapes
    drill: float | None


@dataclass
class SesReport:
    tracks_added: int = 0
    vias_added: int = 0
    tracks_removed: int = 0
    vias_removed: int = 0
    placement_ignored: int = 0
    nets_routed: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    skipped: int = 0

    def as_dict(self) -> dict:
        return {
            "tracks_added": self.tracks_added,
            "vias_added": self.vias_added,
            "tracks_removed": self.tracks_removed,
            "vias_removed": self.vias_removed,
            "placement_ignored": self.placement_ignored,
            "nets_routed": list(self.nets_routed),
            "warnings": list(self.warnings),
            "skipped": self.skipped,
        }


class SesReader:
    """Parse a session file; `wires`, `vias`, `placements` and `padstacks` are in millimetres."""

    def __init__(self, text: str) -> None:
        root = load_specctra(text)
        if root.head != "session":
            raise SpecctraError(f"not a Specctra session: top-level list is ({root.head or '?'} …)")
        self.name = root.atoms[0].value if root.atoms else ""
        self.wires: list[SesWire] = []
        self.vias: list[SesVia] = []
        self.placements: list[SesPlace] = []
        self.padstacks: dict[str, SesPadstack] = {}
        self.warnings: list[str] = []
        routes = root.find("routes")
        if routes is None:
            raise SpecctraError('session file is missing the "routes" section')
        library = routes.find("library_out")
        if library is None:
            raise SpecctraError('session file is missing the "library_out" section')
        scale = self._resolution(routes, session_scale("um", 10))
        for ps in library.find_all("padstack"):
            self._read_padstack(ps, scale)
        network = routes.find("network_out")
        if network is not None:
            for net in network.find_all("net"):
                net_name = net.atoms[0].value if net.atoms else ""
                for wire in net.find_all("wire"):
                    self._read_wire(wire, net_name, scale)
                for via in net.find_all("via"):
                    va = via.atoms
                    if len(va) < 3:
                        self.warnings.append(f"net {net_name!r}: via without coordinates skipped")
                        continue
                    try:
                        x, y = float(va[1].value), float(va[2].value)
                    except ValueError:
                        self.warnings.append(f"net {net_name!r}: via with malformed coordinates skipped")
                        continue
                    self.vias.append(
                        SesVia(va[0].value, x * scale, -y * scale, net_name, via.get("type") or "route")
                    )
        placement = root.find("placement")
        if placement is not None:
            pscale = self._resolution(placement, scale)
            for comp in placement.find_all("component"):
                for place in comp.find_all("place"):
                    pa = place.atoms
                    if len(pa) < 5:
                        continue
                    try:
                        x, y, rot = float(pa[1].value), float(pa[2].value), float(pa[4].value)
                    except ValueError:
                        continue
                    self.placements.append(SesPlace(pa[0].value, x * pscale, -y * pscale, pa[3].value, rot))

    @staticmethod
    def _resolution(node: Node, default: float) -> float:
        res = node.find("resolution")
        if res is None or len(res.atoms) < 2:
            return default
        try:
            return session_scale(res.atoms[0].value, int(float(res.atoms[1].value)))
        except ValueError:
            return default

    def _read_padstack(self, node: Node, scale: float) -> None:
        pid = node.atoms[0].value if node.atoms else ""
        layers: list[str] = []
        diameter: float | None = None
        for shape in node.find_all("shape"):
            for s in shape.nodes:
                if s.head == "circle" and len(s.atoms) >= 2:
                    layers.append(s.atoms[0].value)
                    if diameter is None:
                        with contextlib.suppress(ValueError):
                            diameter = float(s.atoms[1].value) * scale
                elif s.head in ("rect", "path", "polygon"):
                    self.warnings.append(f"padstack {pid!r}: unsupported via shape {s.head}")
        if diameter is None:
            self.warnings.append(f"padstack {pid!r}: no circle shape; via diameter unknown")
            return
        self.padstacks[pid] = SesPadstack(pid, diameter, tuple(layers), drill_from_padstack_id(pid))

    def _read_wire(self, node: Node, net_name: str, scale: float) -> None:
        path = node.find("path")
        if path is None:
            if node.find("qarc") is not None:
                self.warnings.append(f"net {net_name!r}: qarc wire skipped (not produced by Freerouting)")
            elif node.find("polygon") is not None:
                pass  # a plane echoed back as a polygon wire: KiCad ignores those too
            return
        atoms = path.atoms
        if len(atoms) < 2:
            return
        layer = atoms[0].value
        try:
            width = float(atoms[1].value) * scale
            nums = [float(a.value) for a in atoms[2:]]
        except ValueError:
            self.warnings.append(f"net {net_name!r}: malformed path skipped")
            return
        pts = tuple((nums[i] * scale, -nums[i + 1] * scale) for i in range(0, len(nums) - 1, 2))
        if len(pts) < 2:
            return
        self.wires.append(SesWire(layer, width, pts, net_name, node.get("type") or "route"))

    # ------------------------------------------------------------------------------------------

    def apply(
        self,
        board_writer: BoardWriter,
        *,
        keep_locked: bool = True,
        delete_unlocked: bool = True,
        keep_nets: Iterable[str] | None = None,
        apply_placement: bool = False,
        default_via_drill: float | None = None,
        rules: DsnRules | None = None,
    ) -> SesReport:
        """Replace the board's routing with the session's (module docstring); returns a report.

        `rules` (default: the `.kicad_pro` next to the writer's board, else KiCad's built-in
        defaults) resolves the drill of a via the session leaves undefined to its net's netclass;
        `default_via_drill` overrides the Default class drill the padstack names are compared to."""
        root = board_writer.root
        if root is None:
            raise SpecctraError("board_writer has no board loaded")
        report = SesReport()
        report.warnings.extend(self.warnings)
        view = BoardFile.from_node(root)
        if rules is None:
            pro = getattr(board_writer, "board_path", None)
            rules = rules_from_project(view, Path(pro).with_suffix(".kicad_pro") if pro else None)
        class_drill = default_via_drill if default_via_drill is not None else rules.default.via_drill
        stack = copper_stack(view)
        layer_map: dict[str, str] = {}
        for row in view.layers:
            if row.is_copper:
                layer_map[row.name] = row.name
                if row.user_name:
                    layer_map[row.user_name] = row.name
        known_nets = {n.name for n in view.nets if n.name}
        for _fp, pad in view.pads():
            if pad.net is not None and pad.net.name:
                known_nets.add(pad.net.name)
        keep = set(keep_nets or ())

        # 1. delete unlocked tracks / arcs / vias
        if delete_unlocked:
            for node in list(root.nodes):
                if node.head not in ("segment", "arc", "via"):
                    continue
                if keep_locked and _node_locked(node):
                    continue
                if keep and board_writer.nets.name_of(node.find("net")) in keep:
                    continue
                uid = node.get("uuid") or node.get("tstamp") or ""
                if not uid:
                    board_writer.delete_node(node)
                else:
                    board_writer.delete(uid)
                if node.head == "via":
                    report.vias_removed += 1
                else:
                    report.tracks_removed += 1

        # 2. placement
        if self.placements:
            if apply_placement:
                for place in self.placements:
                    rotation = place.rotation if place.side == "front" else place.rotation + 180.0
                    try:
                        board_writer.place(place.ref, (place.x, place.y), rotation, place.side)
                    except Exception as exc:  # BoardWriterError: unknown reference
                        report.skipped += 1
                        report.warnings.append(f"placement of {place.ref!r} skipped: {exc}")
            else:
                report.placement_ignored = len(self.placements)

        # 3. wires
        routed: set[str] = set()
        for wire in self.wires:
            layer = layer_map.get(wire.layer)
            if layer is None:
                report.skipped += 1
                report.warnings.append(f"wire on unknown layer {wire.layer!r} skipped (net {wire.net!r})")
                continue
            net = wire.net if wire.net in known_nets else ""
            if wire.net and not net:
                report.warnings.append(f"net {wire.net!r} is not on the board: its wires are unconnected")
            uuids = board_writer.add_track(list(wire.points), wire.width, layer, net)
            if wire.type == "fix":
                for uid in uuids:
                    _lock(board_writer, uid)
            report.tracks_added += len(uuids)
            if net:
                routed.add(net)

        # 4. vias
        n_layers = len(stack)
        for via in self.vias:
            ps = self.padstacks.get(via.padstack)
            if ps is None:
                report.skipped += 1
                report.warnings.append(f"via refers to missing padstack {via.padstack!r}; skipped")
                continue
            indices = sorted({stack.index(layer_map[la]) for la in ps.layers if la in layer_map})
            if ps.layers and not indices:
                report.skipped += 1
                report.warnings.append(
                    f"via padstack {via.padstack!r} names unknown layers {ps.layers}; skipped"
                )
                continue
            kind = "through"
            pair = (stack[0], stack[-1])
            if len(ps.layers) not in (0, 1, n_layers) and indices:
                lo, hi = indices[0], indices[-1]
                pair = (stack[lo], stack[hi])
                if lo == 0 and hi == n_layers - 1:
                    kind = "through"  # outer-to-outer with inner shapes omitted (KiCad would say buried)
                elif (lo == 0 and hi == 1) or (lo == n_layers - 2 and hi == n_layers - 1):
                    kind = "micro"
                elif lo > 0 and hi < n_layers - 1:
                    kind = "buried"
                else:
                    kind = "blind"
            net = via.net if via.net in known_nets else ""
            # makeVIA: the padstack drill equal to the Default class drill (or absent) is
            # UNDEFINED_DRILL_DIAMETER, which KiCad saves as the via net's netclass drill
            drill = ps.drill
            if drill is None or abs(drill - class_drill) < 1e-6:
                drill = rules.class_of(net).via_drill if net else class_drill
            uid = board_writer.add_via((via.x, via.y), ps.diameter, drill, net, pair)
            if kind != "through":
                via_node = _via_node(root, uid)
                if via_node is not None:
                    via_node.insert(1, Atom.symbol("blind" if kind == "buried" else kind))
            if via.type == "fix":
                _lock(board_writer, uid)
            report.vias_added += 1
            if net:
                routed.add(net)
        report.nets_routed = sorted(routed)
        return report


def _node_locked(node: Node) -> bool:
    for a in node.atoms:
        if a.kind == "symbol" and a.value == "locked":
            return True
    child = node.find("locked")
    if child is None:
        return False
    if not child.atoms:
        return True
    return child.atoms[0].value.lower() in ("yes", "true", "1")


def _via_node(root: Node, uid: str) -> Node | None:
    for node in root.find_all("via"):
        if (node.get("uuid") or node.get("tstamp")) == uid:
            return node
    return None


def _lock(writer: BoardWriter, uid: str) -> None:
    root = writer.root
    if root is None:
        return
    for node in root.nodes:
        if node.head in ("segment", "arc", "via") and (node.get("uuid") or node.get("tstamp")) == uid:
            if writer.dialect.locked_as_list:
                anchor = node.find("net") or node.find("layer") or node.find("layers")
                idx = node.index(anchor) + 1 if anchor is not None else len(node.children)
                node.insert(idx, _locked_list())
            else:
                node.insert(1, Atom.symbol("locked"))
            return


def _locked_list() -> Node:
    return build("locked", "yes")
