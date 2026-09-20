"""Specctra DSN export / SES import through the kiwrap patch `specctra-jobs` (fork branch
`mr/specctra-jobs`, bench build `C:/kiwrap-builds/6dd893f7d1-5`; `kiwrap.backends.cli.specctra`).

Offline: the two capability keys are derived from the `--help` tree by presence, the manifest lists
the patch, and a record without the keys makes both adapters raise `Unsupported(fallback="codec")`.

Live (`kicad_patch("specctra-jobs")`, auto-skip on stock installs; KiCad 10.0.6's bundled Python
is the oracle where the test says so, docs/VERIFICATION.md claim 7):

- `export_dsn` on the ecc83 demo, the Phase 1b blinker board and the 4-layer coldfire demo is
  semantically equal to SWIG `ExportSpecctraDSN` on 10.0.6 for the same board file: the same
  copper layers, boundary, rules, via padstacks, keepouts, placement (component, side, rotation,
  position), library images with their pins and padstacks, nets with their pins, and the same
  copper as two-point segments. KiCad master changed two things since 10.0 that the comparison
  normalises and this docstring records: existing tracks are written `(type protect)` one wire per
  segment (10.0: `(type route)` polylines), and copper zones are exported as `(wire (polygon ...)
  (type protect) (window ...))` in `wiring` (10.0: `(plane NET (polygon ...))` in `structure`);
- Freerouting 2.4.1 under the managed JRE 25 accepts the exported DSN of the ecc83 demo with half
  of its copper removed (seeded, segment level) and of the blinker board with the copper of 70% of
  its nets removed (`--router.fanout.enabled=false`, claim 8); the blinker route is complete (0
  unconnected, 0 violations after refill). Two router facts found here, for the routing ops:
  segment-level stripping leaves stubs that end inside SMD pads, which Freerouting refuses to
  connect to (pin-exit rule; "unrouted" from pass 1, passes 3+ take 20 ms) whatever the wire type
  (`protect` rewritten to `route` changes nothing), and Freerouting's own "unrouted" count keeps
  the pads a zone connects (2-5 on the blinker whose GND is a zone), so KiCad's DRC after refill,
  not the router's score, decides whether a route is complete;
- `import_ses` of that session equals SWIG `ImportSpecctraSES` on 10.0.6 for the same board and
  session: the same tracks and vias (positions within 1e-3 mm, layers, widths, nets), the same
  footprint positions, and `kicad-cli pcb drc --refill-zones` on the bench build reports the same
  violations and unconnected items for both results; locked tracks survive both importers;
- on the 4-layer demo the import oracle runs on a session made from the DSN's own wiring (a router
  that changed nothing; Freerouting would take minutes there), and the SES placement moves the
  footprints it names on both sides;
- a session without a `routes` section fails with `CliLoadError` and the board is untouched.

Process hygiene: every kicad-cli and SWIG python runs with a sandbox env (`config_root`), the
router runs from the managed JRE with a per-test `user_data_path` (telemetry-off profile) and is
killed on timeout; the user's KiCad config hash is asserted by the session fixture.
"""

from __future__ import annotations

import json
import os
import random
import shutil
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from kiwrap.backends.cli import specctra
from kiwrap.backends.cli.drc import run_drc
from kiwrap.backends.cli.runner import CliLoadError
from kiwrap.capability import (
    Capabilities,
    Unsupported,
    capabilities_for,
    flatten_help_tree,
    load_patch_manifest,
)
from kiwrap.formats.board import ArcTrack, BoardFile, Track, Via
from kiwrap.formats.sexpr import build, dump
from kiwrap.installation import KiCadInstallation
from kiwrap.platform import current
from tests._fakes import user_config_untouched  # noqa: F401 - session fixture

PATCH = "specctra-jobs"
ECC83 = ("ecc83", "ecc83-pp.kicad_pcb")
COLDFIRE = ("kit-dev-coldfire-xilinx_5213", "kit-dev-coldfire-xilinx_5213.kicad_pcb")
BLINKER = Path(__file__).parent / "fixtures" / "specctra" / "blinker_10" / "blinky.kicad_pcb"
TOL_MM = 1e-3
SWIG_TIMEOUT_S = 120
ROUTER_TIMEOUT_S = 240

# --------------------------------------------------------------------------------------------
# Offline
# --------------------------------------------------------------------------------------------


def _caps(values: dict[str, Any]) -> Capabilities:
    install = KiCadInstallation(
        root=Path(), kicad_cli=Path("kicad-cli"), share=Path(), version="0.0.0", source="explicit"
    )
    return Capabilities(install, values, probed=False)


def _tree(*commands: str) -> dict[str, Any]:
    """A minimal help tree with the given `pcb ...` leaf commands."""

    def node(command: str) -> dict[str, Any]:
        return {"command": command, "flags": [], "positionals": [], "exit_code": 0, "subcommands": {}}

    root = node("")
    root["subcommands"]["pcb"] = node("pcb")
    export = node("pcb export")
    root["subcommands"]["pcb"]["subcommands"]["export"] = export
    for cmd in commands:
        parent = export if cmd.startswith("pcb export ") else root["subcommands"]["pcb"]
        parent["subcommands"][cmd.split()[-1]] = node(cmd)
    return root


def test_keys_are_derived_from_the_help_tree_by_presence():
    both = flatten_help_tree(_tree("pcb export specctra", "pcb import-ses", "pcb drc"))
    assert both["cli.has_specctra_export"] is True and both["cli.has_specctra_import"] is True
    stock = flatten_help_tree(_tree("pcb drc", "pcb export gerbers"))
    assert stock["cli.has_specctra_export"] is False and stock["cli.has_specctra_import"] is False


def test_manifest_lists_the_patch_with_both_keys():
    patch = next(p for p in load_patch_manifest()["patches"] if p["id"] == PATCH)
    assert set(patch["keys"]) == {"cli.has_specctra_export", "cli.has_specctra_import"}
    assert patch["branch"] == "mr/specctra-jobs"


def test_capability_helpers_fall_back_to_the_subcommand_list():
    assert specctra.has_specctra_export(_caps({"cli.has_specctra_export": True}))
    assert not specctra.has_specctra_export(_caps({"cli.has_specctra_export": False, "cli.subcommands": []}))
    old = _caps({"cli.subcommands": ["pcb export specctra", "pcb import-ses"]})
    assert specctra.has_specctra_export(old) and specctra.has_specctra_import(old)
    assert not specctra.has_specctra_import(_caps({}))


def test_stock_records_raise_unsupported_with_the_codec_fallback(tmp_path):
    caps = _caps({"cli.has_specctra_export": False, "cli.has_specctra_import": False})
    config: Any = object()  # never reached: the capability check comes first
    with pytest.raises(Unsupported) as exc:
        specctra.export_dsn(caps.install, config, tmp_path / "b.kicad_pcb", tmp_path / "b.dsn", caps)
    assert exc.value.capability == "cli.has_specctra_export" and exc.value.fallback == "codec"
    with pytest.raises(Unsupported) as exc:
        specctra.import_ses(caps.install, config, tmp_path / "b.kicad_pcb", tmp_path / "b.ses", None, caps)
    assert exc.value.capability == "cli.has_specctra_import" and exc.value.fallback == "codec"


# --------------------------------------------------------------------------------------------
# Live: fixtures
# --------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def patched(patched_installs) -> Any:
    """The newest install carrying the patch (the registered fork build)."""
    return patched_installs(PATCH)[0]


@pytest.fixture(scope="module")
def swig10(installs) -> Any:
    """KiCad 10.0.6 with its bundled Python: the SWIG oracle (claim 7)."""
    for inst in installs:
        if inst.major_minor == "10.0" and inst.python and Path(inst.python).is_file():
            return inst
    pytest.skip("KiCad 10.0 with a bundled python (the SWIG oracle) is not installed")


def _real_storage_home() -> Path:
    """The user's real storage home (`scripts/bootstrap.ps1` installed the engines there); the
    session's `KIWRAP_HOME` is a throw-away directory without them (conftest)."""
    local = os.environ.get("LOCALAPPDATA")
    return Path(local) / "kiwrap" if local else current().storage_home


@pytest.fixture(scope="module")
def engines() -> tuple[Path, Path, Path | None]:
    """(java, freerouting jar, profile) from the managed engines (`kw_env` reports the same)."""
    storage = _real_storage_home()
    try:
        from kiwrap.backends.router.jre import discover_engines

        found = discover_engines(storage)
        if found.java and found.jar:
            return Path(found.java), Path(found.jar), Path(found.profile) if found.profile else None
    except Exception:  # noqa: BLE001 - the router backend is being built concurrently; fall back
        pass
    javas = sorted((storage / "engines" / "jre").glob("*/bin/java.exe"))
    jars = sorted((storage / "engines" / "freerouting").glob("freerouting-*.jar"))
    if not javas or not jars:
        pytest.skip("managed JRE / Freerouting not installed (scripts/bootstrap.ps1)")
    profile = storage / "freerouting" / "freerouting.json"
    return javas[-1], jars[-1], profile if profile.is_file() else None


def _demo_copy(install: Any, demo: tuple[str, str], dest: Path) -> Path:
    src = install.dir("demos") / demo[0]
    if not (src / demo[1]).is_file():
        pytest.skip(f"{demo[1]} not shipped under {src}")
    dest.mkdir(parents=True, exist_ok=True)
    for f in src.iterdir():
        if f.is_file() and f.suffix in (".kicad_pcb", ".kicad_pro", ".kicad_dru"):
            shutil.copyfile(f, dest / f.name)
    return dest / demo[1]


def _blinker_copy(dest: Path) -> Path:
    if not BLINKER.is_file():
        pytest.skip(f"{BLINKER} missing")
    dest.mkdir(parents=True, exist_ok=True)
    for f in BLINKER.parent.iterdir():
        if f.is_file():
            shutil.copyfile(f, dest / f.name)
    return dest / BLINKER.name


def _strip_copper(board: Path, fraction: float, seed: int = 2) -> int:
    """Delete a seeded fraction of the segments / vias / arcs through the CST; returns how many."""
    model = BoardFile.load(board)
    rng = random.Random(seed)
    victims = [n for n in model.root.nodes if n.head in ("segment", "via", "arc") and rng.random() < fraction]
    for n in victims:
        model.root.remove(n)
    dump(model.root, board)
    return len(victims)


def _strip_nets(board: Path, fraction: float, seed: int = 5) -> tuple[set[str], int]:
    """Delete ALL copper (segments / vias / arcs) of a seeded fraction of the routed nets; returns
    (the nets, how many items). Whole nets, not random segments: a stub that ends inside an SMD
    pad is one Freerouting 2.4.1 refuses to connect to (its pin-exit rule; passes 3+ take 20 ms
    and leave it "unrouted"), so segment-level stripping is only used where the SWIG oracle, not
    a complete route, is the assertion."""
    model = BoardFile.load(board)
    rng = random.Random(seed)
    items: list[Track | Via | ArcTrack] = [*model.tracks, *model.vias, *model.arcs]
    names = sorted({item.net.name for item in items if item.net is not None})
    victims = {name for name in names if rng.random() < fraction}
    removed = 0
    for item in items:
        if item.net is not None and item.net.name in victims and item.node is not None:
            model.root.remove(item.node)
            removed += 1
    dump(model.root, board)
    return victims, removed


def _lock_first_track(board: Path) -> str:
    """Lock the first segment of the board file through the CST (`(locked yes)`, the 9+ form both
    oracles read); returns its uuid."""
    model = BoardFile.load(board)
    track = model.tracks[0]
    assert track.node is not None and not track.locked
    track.node.insert(1, build("locked", True))
    dump(model.root, board)
    assert BoardFile.load(board).tracks[0].locked
    return track.uuid


# --------------------------------------------------------------------------------------------
# Live: the SWIG oracle and the router
# --------------------------------------------------------------------------------------------

_SWIG_EXPORT = """
import sys, pcbnew
b = pcbnew.LoadBoard(sys.argv[1])
ok = pcbnew.ExportSpecctraDSN(b, sys.argv[2])
print("EXPORT", ok)
"""

_SWIG_IMPORT = """
import sys, pcbnew
b = pcbnew.LoadBoard(sys.argv[1])
ok = pcbnew.ImportSpecctraSES(b, sys.argv[2])
print("IMPORT", ok)
if ok:
    pcbnew.SaveBoard(sys.argv[3], b)
"""


def _swig(install: Any, config: Any, script: str, *args: Path) -> str:
    """Run a snippet under the install's bundled python with the sandbox env (no GUI)."""
    scratch = config.temp_dir() / "specctra-swig"
    scratch.mkdir(parents=True, exist_ok=True)
    py = scratch / f"snippet_{abs(hash(script))}.py"
    py.write_text(script, encoding="utf-8")
    with config.lock(timeout_s=SWIG_TIMEOUT_S):
        proc = subprocess.run(
            [str(install.python), str(py), *(str(a) for a in args)],
            env=config.env(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=SWIG_TIMEOUT_S,
            check=False,
            cwd=str(scratch),
        )
        config.restore_seed()
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc.stdout


def _route(
    engines: tuple[Path, Path, Path | None], dsn: Path, ses: Path, work: Path, passes: int = 10
) -> str:
    """Freerouting headless with the verified flags (claim 8); kills the JVM on timeout."""
    java, jar, profile = engines
    work.mkdir(parents=True, exist_ok=True)
    if profile is not None:
        shutil.copyfile(profile, work / profile.name)
    argv = [
        str(java),
        "-jar",
        str(jar),
        f"--user_data_path={work}",
        "-de",
        str(dsn),
        "-do",
        str(ses),
        "--gui.enabled=false",
        "-mp",
        str(passes),
        "-mt",
        "1",
        "--router.fanout.enabled=false",
        f"--logging.file.location={work}",
    ]
    proc = subprocess.Popen(argv, cwd=str(work), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        out, _ = proc.communicate(timeout=ROUTER_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        pytest.fail(f"Freerouting did not finish within {ROUTER_TIMEOUT_S}s")
    assert proc.returncode == 0, out
    assert ses.is_file(), out
    return out


# --------------------------------------------------------------------------------------------
# Live: DSN / SES normalisation
# --------------------------------------------------------------------------------------------


def _tokens(text: str) -> list[str]:
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c.isspace():
            i += 1
        elif c in "()":
            out.append(c)
            i += 1
        elif c == '"':
            j = text.index('"', i + 1)
            out.append(text[i + 1 : j])
            i = j + 1
        else:
            j = i
            while j < n and not text[j].isspace() and text[j] not in "()":
                j += 1
            out.append(text[i:j])
            i = j
    return out


def _parse(tokens: list[str], pos: int = 0) -> tuple[list[Any], int]:
    assert tokens[pos] == "("
    node: list[Any] = []
    pos += 1
    while tokens[pos] != ")":
        if tokens[pos] == "(":
            child, pos = _parse(tokens, pos)
            node.append(child)
        else:
            node.append(tokens[pos])
            pos += 1
    return node, pos + 1


def _sexpr(path: Path) -> list[Any]:
    return _parse(_tokens(path.read_text(encoding="utf-8")))[0]


def _kids(node: list[Any], head: str) -> list[list[Any]]:
    return [c for c in node if isinstance(c, list) and c and c[0] == head]


def _kid(node: list[Any], head: str) -> list[Any] | None:
    found = _kids(node, head)
    return found[0] if found else None


def _num(atom: Any) -> float:
    return round(float(atom), 3)


def _freeze(node: Any) -> Any:
    """Hashable form of a parsed node with numbers rounded to 1e-3 (DSN units: um)."""
    if isinstance(node, list):
        return tuple(_freeze(c) for c in node)
    try:
        return _num(node)
    except ValueError:
        return node


def _edges(outline: list[Any]) -> Any:
    """An image `(outline (path LAYER W x y ...))` as its layer, width and undirected edge set:
    master starts a rectangle's closed polyline at another corner (and may walk it the other way
    round) than 10.0 does, the shape is the same."""
    shape = outline[1]
    if not isinstance(shape, list) or shape[0] not in ("path", "polygon"):
        return _freeze(outline)
    pts = [(_num(shape[i]), _num(shape[i + 1])) for i in range(3, len(shape), 2)]
    edges = frozenset((min(a, b), max(a, b)) for a, b in zip(pts, pts[1:], strict=False) if a != b)
    return ("outline", shape[0], shape[1], _num(shape[2]), edges)


def dsn_summary(path: Path) -> dict[str, Any]:
    """The comparable content of a KiCad DSN (see the module docstring for what is normalised)."""
    pcb = _sexpr(path)
    structure = _kid(pcb, "structure") or []
    placement = _kid(pcb, "placement") or []
    library = _kid(pcb, "library") or []
    network = _kid(pcb, "network") or []
    wiring = _kid(pcb, "wiring") or []

    places = set()
    for comp in _kids(placement, "component"):
        for place in _kids(comp, "place"):
            places.add((comp[1], place[1], _num(place[2]), _num(place[3]), place[4], _num(place[5])))

    images: dict[str, Any] = {}
    for image in _kids(library, "image"):
        images[image[1]] = (
            frozenset(_freeze(pin) for pin in _kids(image, "pin")),
            frozenset(_edges(o) for o in _kids(image, "outline")),
            frozenset(_freeze(k) for k in _kids(image, "keepout")),
        )

    segments: Counter[Any] = Counter()
    zone_wires = 0
    for wire in _kids(wiring, "wire"):
        path_ = _kid(wire, "path")
        if path_ is None:  # a zone: (wire (polygon ...)) on master, (plane ...) on 10.0
            zone_wires += 1
            continue
        net = (_kid(wire, "net") or ["net", ""])[1]
        layer, width = path_[1], _num(path_[2])
        pts = [(_num(path_[i]), _num(path_[i + 1])) for i in range(3, len(path_), 2)]
        for a, b in zip(pts, pts[1:], strict=False):
            segments[(net, layer, width, min(a, b), max(a, b))] += 1

    vias: Counter[Any] = Counter()
    for via in _kids(wiring, "via"):
        net = (_kid(via, "net") or ["net", ""])[1]
        vias[(net, via[1], _num(via[2]), _num(via[3]))] += 1

    planes = {p[1] for p in _kids(structure, "plane")}
    zone_nets = {(_kid(w, "net") or ["net", ""])[1] for w in _kids(wiring, "wire") if _kid(w, "path") is None}
    return {
        "resolution": _freeze(_kid(pcb, "resolution")),
        "unit": _freeze(_kid(pcb, "unit")),
        "layers": [layer[1] for layer in _kids(structure, "layer")],
        "boundary": _freeze(_kid(structure, "boundary")),
        "vias": _freeze(_kid(structure, "via")),
        "rules": _freeze(_kid(structure, "rule")),
        "keepouts": frozenset(_freeze(k) for k in _kids(structure, "keepout")),
        "places": places,
        "images": images,
        "padstacks": {ps[1]: _freeze(ps[2:]) for ps in _kids(library, "padstack")},
        "nets": {net[1]: frozenset(_freeze(p) for p in _kids(net, "pins")) for net in _kids(network, "net")},
        "classes": frozenset(_freeze(c) for c in _kids(network, "class")),
        "segments": segments,
        "wire_vias": vias,
        "zone_nets": planes | zone_nets,
    }


def assert_same_dsn(a: dict[str, Any], b: dict[str, Any]) -> None:
    for key in a:
        assert a[key] == b[key], f"DSN differs in {key}"


def session_from_dsn(
    dsn_path: Path,
    ses_path: Path,
    placement: dict[str, tuple[str, float, float, str, float]] | None = None,
    nets: set[str] | None = None,
) -> None:
    """A session carrying the DSN's own wiring (a router that changed nothing), in 0.1 um as
    Freerouting writes it; `placement` adds `(place REF x_um y_um side rot)` entries, `nets`
    keeps only those nets' wires and vias. `(type fix)` wires and vias (locked copper) are left
    out, as Freerouting leaves them out of its session."""
    pcb = _sexpr(dsn_path)
    wiring = _kid(pcb, "wiring") or []
    library = _kid(pcb, "library") or []

    def wanted(item: list[Any]) -> bool:
        net = (_kid(item, "net") or ["net", ""])[1]
        kind = (_kid(item, "type") or ["type", ""])[1]
        return kind != "fix" and (nets is None or net in nets)

    def q(atom: str) -> str:
        return atom if atom and not any(c in atom for c in ' ()"') else f'"{atom}"'

    def s(value: str) -> str:
        return str(int(round(float(value) * 10)))

    routes: dict[str, list[str]] = {}
    used: set[str] = set()
    for wire in _kids(wiring, "wire"):
        path_ = _kid(wire, "path")
        if path_ is None or not wanted(wire):
            continue
        net = (_kid(wire, "net") or ["net", ""])[1]
        coords = " ".join(s(v) for v in path_[3:])
        routes.setdefault(net, []).append(f"(wire (path {q(path_[1])} {s(path_[2])} {coords}) (type route))")
    for via in _kids(wiring, "via"):
        if not wanted(via):
            continue
        net = (_kid(via, "net") or ["net", ""])[1]
        routes.setdefault(net, []).append(f"(via {q(via[1])} {s(via[2])} {s(via[3])})")
        used.add(via[1])
    lib_out = []
    for ps in _kids(library, "padstack"):
        if ps[1] in used:
            shapes = "".join(
                f" (shape (circle {q(sh[1][1])} {s(sh[1][2])} 0 0))"
                for sh in _kids(ps, "shape")
                if sh[1][0] == "circle"
            )
            lib_out.append(f"(padstack {q(ps[1])}{shapes} (attach off))")
    lines = [f"(session {q(dsn_path.stem)}", f"  (base_design {q(dsn_path.stem)})"]
    if placement:
        lines.append("  (placement (resolution um 10)")
        for ref, (image, x_um, y_um, side, rot) in placement.items():
            lines.append(
                f"    (component {q(image)} (place {q(ref)} {int(round(x_um * 10))} {int(round(-y_um * 10))} {side} {rot:g}))"
            )
        lines.append("  )")
    lines += [
        "  (routes (resolution um 10)",
        "    (library_out " + " ".join(lib_out) + ")",
        "    (network_out",
    ]
    for net, items in routes.items():
        lines.append(f"      (net {q(net)} " + " ".join(items) + ")")
    lines += ["    )", "  )", ")"]
    ses_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def copper(board: Path) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]]]:
    """Tracks and vias of a board file as comparable tuples (net name, layer(s), geometry, width)."""
    model = BoardFile.load(board)

    def net(n: Any) -> str:
        return n.name if n is not None else ""

    def pt(v: Any) -> tuple[float, float]:
        return (round(v.x, 3), round(v.y, 3))

    tracks = sorted(
        (
            net(t.net),
            t.layer,
            min(pt(t.start), pt(t.end)),
            max(pt(t.start), pt(t.end)),
            round(t.width, 3),
            t.locked,
        )
        for t in model.tracks
    )
    vias = sorted(
        (net(v.net), v.layers, pt(v.position), round(v.size, 3), round(v.drill, 3), v.locked)
        for v in model.vias
    )
    return tracks, vias


def _close(a: Any, b: Any, tol: float = TOL_MM) -> bool:
    if isinstance(a, tuple) and isinstance(b, tuple):
        return len(a) == len(b) and all(_close(x, y, tol) for x, y in zip(a, b, strict=False))
    if isinstance(a, float) and isinstance(b, float):
        return abs(a - b) <= tol * (1 + 1e-6)
    return bool(a == b)


def assert_same_copper(board_a: Path, board_b: Path) -> None:
    """Every track / via of A has a counterpart in B within `TOL_MM` and vice versa."""
    for kind, items_a, items_b in zip(("tracks", "vias"), copper(board_a), copper(board_b), strict=True):
        assert len(items_a) == len(items_b), f"{kind}: {len(items_a)} vs {len(items_b)}"
        remaining = list(items_b)
        for item in items_a:
            match = next((o for o in remaining if _close(item, o)), None)
            assert match is not None, f"{kind}: {item} has no counterpart"
            remaining.remove(match)
        assert not remaining


def places(board: Path) -> dict[str, tuple[float, float, float, str]]:
    return {
        fp.reference: (round(fp.position.x, 3), round(fp.position.y, 3), round(fp.rotation, 3), fp.layer)
        for fp in BoardFile.load(board).footprints
    }


def _drc_counts(install: Any, config: Any, board: Path) -> tuple[Counter[str], int]:
    report = run_drc(install, config, board, severity="all", refill_zones=True)
    return Counter(v.type for v in report.violations), len(report.unconnected)


# --------------------------------------------------------------------------------------------
# Live: tests
# --------------------------------------------------------------------------------------------


@pytest.mark.kicad
@pytest.mark.kicad_patch(PATCH)
def test_capability_and_provider_label(patched, storage_home):
    caps = capabilities_for(patched, storage_home=storage_home)
    assert caps.has("cli.has_specctra_export") and "pcb export specctra" in caps["cli.subcommands"]
    assert caps.has("cli.has_specctra_import") and "pcb import-ses" in caps["cli.subcommands"]
    assert PATCH in caps["kiwrap.patches"] and caps.has("kiwrap.patched_build")
    assert caps.patch_for("cli.has_specctra_export") == PATCH
    assert caps.patch_for("cli.has_specctra_import") == PATCH


@pytest.mark.kicad
@pytest.mark.kicad_patch(PATCH)
@pytest.mark.kicad_version("10.0")
@pytest.mark.parametrize("which", ["ecc83", "blinker", "coldfire"])
def test_dsn_export_equals_swig_export(patched, swig10, config_root, tmp_path, which):
    """The patched CLI's DSN carries what SWIG ExportSpecctraDSN on 10.0.6 writes for the same file."""
    if which == "blinker":
        board = _blinker_copy(tmp_path / which)
    else:
        board = _demo_copy(swig10, ECC83 if which == "ecc83" else COLDFIRE, tmp_path / which)
    cli = specctra.export_dsn_run(patched, config_root(patched), board, tmp_path / f"{which}_cli.dsn")
    assert cli.path.is_file() and cli.warnings == []
    swig_dsn = tmp_path / f"{which}_swig.dsn"
    assert "EXPORT True" in _swig(swig10, config_root(swig10), _SWIG_EXPORT, board, swig_dsn)
    a, b = dsn_summary(cli.path), dsn_summary(swig_dsn)
    assert_same_dsn(a, b)
    model = BoardFile.load(board)
    user_names = [layer.user_name or layer.name for layer in model.layers if layer.is_copper]
    assert a["layers"] == user_names, "the DSN names the copper layers by their user names"
    assert len(a["places"]) == len(model.footprints)
    assert sum(a["segments"].values()) == len(model.tracks) + sum(1 for t in model.arcs)
    assert sum(a["wire_vias"].values()) == len(model.vias)


@pytest.mark.kicad
@pytest.mark.kicad_patch(PATCH)
def test_export_without_outline_warns_and_writes_bounding_box(patched, config_root, tmp_path):
    src = Path(__file__).parents[1] / "spikes" / "specctra-verify" / "test_nooutline.kicad_pcb"
    if not src.is_file():
        pytest.skip("spike fixture missing")
    board = tmp_path / src.name
    shutil.copyfile(src, board)
    run = specctra.export_dsn_run(patched, config_root(patched), board, tmp_path / "no.dsn")
    assert any("outline is malformed" in w for w in run.warnings)
    assert _kid(_kid(_sexpr(run.path), "structure") or [], "boundary") is not None


@pytest.fixture(scope="module")
def routed_ecc83(patched, swig10, storage_home, engines, tmp_path_factory) -> dict[str, Path]:
    """ecc83 (10.0.6's copy) with half of its copper removed and one track locked, its DSN from
    the patched CLI, and Freerouting's session for it (module scope: one router run for the two
    import tests, so the sandbox root comes from the session's storage home directly)."""
    from kiwrap import sandbox

    root = tmp_path_factory.mktemp("specctra") / "ecc83"
    board = _demo_copy(swig10, ECC83, root)
    removed = _strip_copper(board, 0.5)
    assert removed > 0
    locked = _lock_first_track(board)
    config = sandbox.ensure(patched, storage_home=storage_home)
    dsn = specctra.export_dsn(patched, config, board, root / "ecc83.dsn")
    ses = root / "ecc83.ses"
    log = _route(engines, dsn, ses, root / "fr")
    assert "finished with state: COMPLETED" in log or ses.is_file()
    return {"board": board, "dsn": dsn, "ses": ses, "locked": Path(locked)}


@pytest.mark.kicad
@pytest.mark.kicad_patch(PATCH)
@pytest.mark.kicad_version("10.0")
def test_freerouting_accepts_the_dsn_and_import_equals_swig_import(
    patched, swig10, config_root, routed_ecc83, tmp_path
):
    board, ses = routed_ecc83["board"], routed_ecc83["ses"]
    before_tracks, before_vias = copper(board)
    assert any(t[-1] for t in before_tracks), "a locked track is part of the fixture"

    # patched CLI
    out_cli = tmp_path / "cli" / board.name
    run = specctra.import_ses_run(patched, config_root(patched), board, ses, out_cli)
    assert run.path == out_cli and out_cli.is_file() and run.skipped == 0
    assert run.tracks_before == len(before_tracks) + len(before_vias) and run.tracks_after > 0
    assert out_cli.with_suffix(".kicad_pro").is_file(), "the project follows the routed copy"

    # SWIG oracle
    out_swig = tmp_path / "swig" / board.name
    out_swig.parent.mkdir()
    shutil.copyfile(board.with_suffix(".kicad_pro"), out_swig.with_suffix(".kicad_pro"))
    assert "IMPORT True" in _swig(swig10, config_root(swig10), _SWIG_IMPORT, board, ses, out_swig)

    assert_same_copper(out_cli, out_swig)
    assert places(out_cli) == places(out_swig)
    tracks, vias = copper(out_cli)
    assert len(tracks) + len(vias) == run.tracks_after
    assert any(t[-1] for t in tracks), "the locked track survived"
    assert sum(1 for t in tracks if not t[-1]) > 0, "the session's routes were added"

    # KiCad is the judge: the same DRC on both results
    cfg = config_root(patched)
    assert _drc_counts(patched, cfg, out_cli) == _drc_counts(patched, cfg, out_swig)


@pytest.mark.kicad
@pytest.mark.kicad_patch(PATCH)
def test_import_in_place_replaces_unlocked_copper(patched, config_root, routed_ecc83, tmp_path):
    """The editor's rule (docs/VERIFICATION.md claim 7): every unlocked track and via goes, locked
    ones stay, the session's routes come in - shown with a session that carries one net only
    (Freerouting's own session returns the `protect` wires it was given, so the untouched
    copper would come back identical and the rule would be invisible)."""
    board = tmp_path / routed_ecc83["board"].name
    shutil.copyfile(routed_ecc83["board"], board)
    shutil.copyfile(routed_ecc83["board"].with_suffix(".kicad_pro"), board.with_suffix(".kicad_pro"))
    before_tracks, before_vias = copper(board)
    net = next(t[0] for t in before_tracks if not t[-1] and t[0])
    ses = tmp_path / "one_net.ses"
    session_from_dsn(routed_ecc83["dsn"], ses, nets={net})
    run = specctra.import_ses_run(patched, config_root(patched), board, ses, None)
    assert run.path == board and run.skipped == 0
    assert run.tracks_before == len(before_tracks) + len(before_vias)
    tracks, vias = copper(board)
    assert len(tracks) + len(vias) == run.tracks_after
    assert [t for t in tracks if t[-1]] == [t for t in before_tracks if t[-1]], "locked tracks stay"
    assert [v for v in vias if v[-1]] == [v for v in before_vias if v[-1]], "locked vias stay"
    assert all(t[0] == net for t in tracks if not t[-1]), "every other unlocked track is gone"
    assert all(v[0] == net for v in vias if not v[-1]), "every other unlocked via is gone"
    wanted = [t for t in before_tracks if not t[-1] and t[0] == net]
    assert len([t for t in tracks if not t[-1]]) == len(wanted) and wanted, "the session's routes came in"
    assert not run.warnings


@pytest.mark.kicad
@pytest.mark.kicad_patch(PATCH)
def test_freerouting_accepts_the_blinker_dsn(patched, config_root, engines, tmp_path):
    """The Phase 1b blinker with the copper of 70% of its routed nets removed: Freerouting routes
    the DSN, the session imports, and KiCad's DRC after refill is clean with 0 unconnected items.
    (Freerouting's own "unrouted" count stays at the GND pads the zone connects - the zone is a
    `(wire (polygon ...))` on master - so KiCad's DRC, never the router's score, is the judge.)"""
    board = _blinker_copy(tmp_path / "blinker")
    nets, removed = _strip_nets(board, 0.7)
    assert removed > 0 and nets
    dsn = specctra.export_dsn(patched, config_root(patched), board, tmp_path / "blinky.dsn")
    ses = tmp_path / "blinky.ses"
    _route(engines, dsn, ses, tmp_path / "fr")
    out = tmp_path / "routed" / board.name
    run = specctra.import_ses_run(patched, config_root(patched), board, ses, out)
    tracks, vias = copper(out)
    assert len(tracks) + len(vias) == run.tracks_after > 0
    assert {t[0] for t in tracks} >= nets, "the stripped nets were routed"
    counts, unconnected = _drc_counts(patched, config_root(patched), out)
    assert unconnected == 0 and not counts, counts


@pytest.mark.kicad
@pytest.mark.kicad_patch(PATCH)
@pytest.mark.kicad_version("10.0")
def test_four_layer_import_equals_swig_import(patched, swig10, config_root, tmp_path):
    """4-layer demo: a session made of the DSN's own wiring plus a placement that moves two
    footprints (one locked) imports identically through the patched CLI and SWIG 10.0.6."""
    board = _demo_copy(swig10, COLDFIRE, tmp_path / "k4")
    model = BoardFile.load(board)
    assert len(model.copper_layers) == 4
    dsn = specctra.export_dsn(patched, config_root(patched), board, tmp_path / "k4.dsn")
    summary = dsn_summary(dsn)
    by_ref = {place[1]: place for place in summary["places"]}
    refs = sorted(by_ref)[:2]
    placement = {}
    for ref in refs:
        comp, _, x, y, side, rot = by_ref[ref]
        placement[ref] = (
            comp,
            x + 5000.0,
            -y + 2500.0,
            "back" if side == "front" else "front",
            (rot + 90.0) % 360,
        )
    ses = tmp_path / "k4.ses"
    session_from_dsn(dsn, ses, placement)

    out_cli = tmp_path / "cli" / board.name
    run = specctra.import_ses_run(patched, config_root(patched), board, ses, out_cli)
    assert run.skipped == 0
    out_swig = tmp_path / "swig" / board.name
    out_swig.parent.mkdir()
    assert "IMPORT True" in _swig(swig10, config_root(swig10), _SWIG_IMPORT, board, ses, out_swig)

    assert_same_copper(out_cli, out_swig)
    assert_same_copper(out_cli, board)  # a router that changed nothing gives the copper back
    assert places(out_cli) == places(out_swig)
    before = places(board)
    for ref in refs:
        assert places(out_cli)[ref] != before[ref], f"{ref} was moved by the session placement"
    for ref in set(before) - set(refs):
        assert places(out_cli)[ref] == before[ref]


@pytest.mark.kicad
@pytest.mark.kicad_patch(PATCH)
def test_bad_session_fails_and_leaves_the_board_alone(patched, config_root, tmp_path):
    board = _demo_copy(patched, ECC83, tmp_path / "bad")
    original = board.read_bytes()
    bad = tmp_path / "bad.ses"
    bad.write_text("(session bad (base_design bad))\n", encoding="utf-8")
    with pytest.raises(CliLoadError):
        specctra.import_ses(patched, config_root(patched), board, bad, None)
    assert board.read_bytes() == original
    with pytest.raises(CliLoadError):
        specctra.import_ses(patched, config_root(patched), board, tmp_path / "missing.ses", None)


@pytest.mark.kicad
@pytest.mark.kicad_patch(PATCH)
def test_specctra_jobs_run_in_a_jobset_too(patched, config_root, tmp_path):
    """The jobs are registered for jobsets: `kicad-cli jobset run` executes the export."""
    caps = capabilities_for(patched)
    if not caps.has("cli.has_jobset"):
        pytest.skip("no jobset runner")
    board = _demo_copy(patched, ECC83, tmp_path / "js")
    jobset = board.parent / "specctra.kicad_jobset"
    jobset.write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "description": "dsn",
                        "id": "11111111-2222-3333-4444-555555555555",
                        "settings": {"description": "", "output_filename": "out/ecc83.dsn"},
                        "type": "pcb_export_specctra",
                    }
                ],
                "meta": {"version": 1},
                "outputs": [
                    {
                        "description": "",
                        "id": "aaaaaaaa-2222-3333-4444-555555555555",
                        "only": [],
                        "settings": {"output_path": str(board.parent / "jobset-out")},
                        "type": "folder",
                    }
                ],
            },
            indent=1,
        ),
        encoding="utf-8",
    )
    from kiwrap.backends.cli.runner import run_kicad_cli

    res = run_kicad_cli(
        patched,
        ["jobset", "run", "--file", str(jobset), str(board.with_suffix(".kicad_pro"))],
        config=config_root(patched),
        accept=(0,),
    )
    written = list((board.parent / "jobset-out").glob("**/ecc83.dsn"))
    assert written, res.stdout + res.stderr
    assert _kid(_sexpr(written[0]), "structure") is not None
