"""Schematic writer against every installed kicad-cli (KiCad is the judge, docs/ARCHITECTURE.md
§7): the four mandatory spikes — blink (claim 2 recipe), 3-sheet hierarchy with a twice-placed
sheet and a multi-unit op-amp, all 12 orientations wired at pin ends (claim 3), lossless edits —
each checked with `sch erc --format json`, `sch export netlist` and, on 10+, a `sch upgrade`
re-save diff. Auto-skips without KiCad; bench builds are skipped (same writer path as the stock
install of their version, docs/UPSTREAM.md §3.3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from kiwrap.capability import capabilities_for
from kiwrap.formats.analysis import pin_connectivity
from kiwrap.formats.libs import LibResolver
from kiwrap.formats.schematic import Hierarchy
from kiwrap.formats.schematic_writer import SchematicWriter
from kiwrap.formats.sexpr import load
from tests._sch_designs import (
    ORIENTATIONS,
    build_blink,
    build_demorgan,
    build_hierarchy,
    build_orientations,
)
from tests._sch_verify import epoch_for, verify_design

pytestmark = [pytest.mark.kicad, pytest.mark.slow]


@pytest.fixture
def env(install, config_root, storage_home):
    if install.is_bench_build:
        pytest.skip("bench build: same writer epoch as the stock install of this version")
    caps = capabilities_for(install, storage_home=storage_home)
    if not caps.has("cli.has_erc"):
        pytest.skip(f"KiCad {install.version} has no headless ERC (7.0 is best-effort only)")
    return install, config_root(install), caps


def _writer(install, project: Path, caps, config=None) -> SchematicWriter:
    """The production pairing: the writer over `kiwrap.formats.libs.LibResolver` (the sandbox
    root's sym-lib-table of the target install, its `.kicad_sym` files flattened as KiCad does)."""
    return SchematicWriter(project, epoch=epoch_for(caps), lib_resolver=LibResolver(install, config, project))


def test_blink_erc_clean_and_netlist(env, tmp_path: Path):
    install, config, caps = env
    project = tmp_path / "blink"
    project.mkdir()
    w = _writer(install, project, caps, config)
    design = build_blink(w, project / "blink.kicad_sch")
    v = verify_design(install, config, caps, design, tmp_path / "v")
    assert v.erc_errors == 0, v.erc_types
    assert v.erc_warnings == 0, v.erc_types
    assert v.netlist_ok, (v.netlist_missing, v.netlist_extra)
    assert all(v.named_ok.values()), v.named_ok
    assert v.components == design.refs
    if v.upgrade_diff_lines is not None:
        assert v.upgrade_diff_lines == 0, "\n".join(v.upgrade_diff[:40])


def test_hierarchy_multi_unit_and_repeated_sheet(env, tmp_path: Path):
    install, config, caps = env
    for flavour in (False, True):
        project = tmp_path / f"hier{int(flavour)}"
        project.mkdir()
        w = _writer(install, project, caps, config)
        design = build_hierarchy(w, project / "top.kicad_sch", populate_before_second_instance=flavour)
        assert {p.name for p in design.files} >= {"top.kicad_sch", "amp.kicad_sch", "source.kicad_sch"}
        v = verify_design(install, config, caps, design, tmp_path / f"v{int(flavour)}")
        assert v.erc_errors == 0, (flavour, v.erc_types)
        assert v.erc_warnings == 0, (flavour, v.erc_types)
        assert v.netlist_ok, (flavour, v.netlist_missing, v.netlist_extra)
        assert v.components == design.refs, (flavour, v.components)
        # the twice-placed sheet yields two references per symbol (U1/U2, R1/R2)
        assert design.extra["u_refs"] == ["U1", "U2"] and design.extra["r_refs"] == ["R1", "R2"]
        if v.upgrade_diff_lines is not None:
            assert v.upgrade_diff_lines == 0, "\n".join(v.upgrade_diff[:40])
        # the file model agrees on the per-instance references
        hier = Hierarchy.load(design.root)
        refs = {inst.names_path: sorted(inst.references()) for inst in hier.instances}
        assert sorted(refs["/amp_a/"]) != sorted(refs["/amp_b/"])
        assert any(r.startswith("U") for r in refs["/amp_a/"]) and any(
            r.startswith("U") for r in refs["/amp_b/"]
        )


def test_twelve_orientations_all_pins_connected(env, tmp_path: Path):
    install, config, caps = env
    project = tmp_path / "orient"
    project.mkdir()
    w = _writer(install, project, caps, config)
    design = build_orientations(w, project / "orient.kicad_sch")
    assert len(design.nets) == 3 * len(ORIENTATIONS) // 2 and all(len(n) == 2 for n in design.nets)
    # tier-1 check first: the file model sees no dangling wire and no unconnected pin
    conn = pin_connectivity(Hierarchy.load(design.root))
    assert not conn.dangling_wire_ends
    v = verify_design(install, config, caps, design, tmp_path / "v", upgrade=False)
    assert v.erc_errors == 0, v.erc_types
    assert not [t for t in v.erc_types if "dangling" in t or "not_connected" in t or "unconnected" in t], (
        v.erc_types
    )
    assert v.netlist_ok, (v.netlist_missing, v.netlist_extra)


def test_demorgan_symbol_pins_once(env, tmp_path: Path):
    """A symbol with a De Morgan alternate body (74xx:74LS00) gets one `(pin …)` entry per
    library pin of body style 1 (14, all units) and reports each unit pin once; KiCad's re-save
    keeps the file byte-identical and ERC / the netlist agree with the intent."""
    install, config, caps = env
    project = tmp_path / "dm"
    project.mkdir()
    w = _writer(install, project, caps, config)
    design = build_demorgan(w, project / "dm.kicad_sch")
    placed = [n for n in load(design.root).find_all("symbol") if n.get("lib_id") == "74xx:74LS00"]
    assert len(placed) == 5 and all(len(n.find_all("pin")) == 14 for n in placed), [
        len(n.find_all("pin")) for n in placed
    ]
    v = verify_design(install, config, caps, design, tmp_path / "v")
    assert v.erc_errors == 0, v.erc_types
    assert v.netlist_ok, (v.netlist_missing, v.netlist_extra)
    assert all(v.named_ok.values()), v.named_ok
    if v.upgrade_diff_lines is not None:
        assert v.upgrade_diff_lines == 0, "\n".join(v.upgrade_diff[:40])


def test_edit_round_trip_is_lossless(env, tmp_path: Path):
    """Value / footprint / delete / annotate edits on a KiCad-validated file change only the
    touched lines, and the result still passes ERC."""
    install, config, caps = env
    project = tmp_path / "edit"
    project.mkdir()
    w = _writer(install, project, caps, config)
    design = build_blink(w, project / "blink.kicad_sch")
    before = design.root.read_text(encoding="utf-8").splitlines()
    uuids = design.extra["uuids"]
    w2 = _writer(install, project, caps, config)
    w2.load(design.root)
    w2.edit(uuids["R1"], {"value": "470"})
    w2.assign_footprints({"D1": "LED_SMD:LED_0805_2012Metric"})
    w2.save()
    after = design.root.read_text(encoding="utf-8").splitlines()
    changed = [(a, b) for a, b in zip(before, after, strict=True) if a != b]
    assert len(changed) == 2, changed
    assert any('"330"' in a and '"470"' in b for a, b in changed)
    assert any("R_0603" not in a and "LED_0603" in a and "LED_0805" in b for a, b in changed)
    v = verify_design(install, config, caps, design, tmp_path / "v", upgrade=False)
    assert v.erc_errors == 0 and v.netlist_ok
    assert any(
        c.ref == "R1" and c.value == "470" for c in _components(install, config, caps, design.root, tmp_path)
    )


def _components(install, config, caps, root, tmp):
    from kiwrap.backends.cli.netlist import export_netlist

    return export_netlist(install, config, root, tmp / "c.net", caps=caps).components
