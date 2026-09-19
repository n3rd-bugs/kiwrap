"""T8 — schematic authoring engine certified by KiCad (docs/ARCHITECTURE.md §6/§7, D17 tier 1).

For every installed stock KiCad with a headless ERC (8.0+; 7.0 has none and is best-effort):

1. The three spike designs (`tests/_sch_designs.py`: blink, 3-sheet hierarchy with a twice-
   placed sheet + 3-unit LM358, 12 orientations) are written at the install's own epoch with its
   own libraries and must give **0 ERC errors, 0 warnings** and exactly the intended netlist
   partition; per-instance references (U1/U2, R1/R2) are checked in the netlist components.
2. On installs with `sch upgrade` (10+), KiCad's re-save of the writer's file differs by zero
   lines once the two known re-save effects are removed (project name from a missing
   `.kicad_pro`; the root `(embedded_fonts no)` that kicad-cli drops) — the dialect tables and
   the KiCad-order insertion (items sorted by uuid within type) are what make that hold.
3. Cross-version load matrix: every committed `corpus/writer/*_<era>` fixture whose era is the
   same or older than the install's loads, passes ERC with 0 errors and exports the same netlist
   partition as the kicad-cli that produced it (newer KiCads read older dialects; the reverse is
   refused with "Failed to load schematic", which is asserted for one newer fixture).
4. Edit round trip: value / footprint / reference edits on the validated blink project change
   only those lines and the file still passes ERC.

Measured numbers are recorded as test properties (`-rP` shows them).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from tests._sch_designs import ORIENTATIONS, build_blink, build_hierarchy, build_orientations
from tests._sch_verify import epoch_for, netlist_partition, verify_design

from kiwrap.backends.cli.netlist import export_netlist
from kiwrap.backends.cli.runner import CliLoadError
from kiwrap.capability import capabilities_for
from kiwrap.formats.epoch_dialects import dialect_for
from kiwrap.formats.libs import LibResolver
from kiwrap.formats.reports.netlist import parse_netlist
from kiwrap.formats.schematic import Hierarchy
from kiwrap.formats.schematic_writer import SchematicWriter

pytestmark = [pytest.mark.kicad, pytest.mark.slow]

HERE = Path(__file__).resolve().parent
WRITER_CORPUS = HERE.parent / "corpus" / "writer"


@pytest.fixture
def env(install, config_root, storage_home):
    if install.is_bench_build:
        pytest.skip("bench build: same writer epoch as the stock install of this version")
    caps = capabilities_for(install, storage_home=storage_home)
    if not caps.has("cli.has_erc"):
        pytest.skip(f"KiCad {install.version} has no headless ERC")
    return install, config_root(install), caps


def _writer(install, project: Path, caps, config=None) -> SchematicWriter:
    """The production pairing: the writer over `kiwrap.formats.libs.LibResolver` (the sandbox
    root's sym-lib-table of the target install, its `.kicad_sym` files flattened as KiCad does)."""
    return SchematicWriter(project, epoch=epoch_for(caps), lib_resolver=LibResolver(install, config, project))


def _copy_fixture(src: Path, dst: Path) -> Path:
    dst.mkdir(parents=True, exist_ok=True)
    for f in src.iterdir():
        if f.suffix in (".kicad_sch", ".kicad_pro"):
            (dst / f.name).write_bytes(f.read_bytes())
    return next(dst.glob("blink.kicad_sch"), None) or dst / "top.kicad_sch"


# ----------------------------------------------------------------------------------------------
# 1 + 2: the spike designs at the install's epoch
# ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("design_name", ["blink", "hierarchy", "hierarchy_late", "orientations"])
def test_spike_design_erc_clean_netlist_exact(env, design_name, tmp_path: Path, record_property):
    install, config, caps = env
    project = tmp_path / design_name
    project.mkdir()
    w = _writer(install, project, caps, config)
    if design_name == "blink":
        design = build_blink(w, project / "blink.kicad_sch")
    elif design_name == "hierarchy":
        design = build_hierarchy(w, project / "top.kicad_sch")
    elif design_name == "hierarchy_late":
        design = build_hierarchy(w, project / "top.kicad_sch", populate_before_second_instance=True)
    else:
        design = build_orientations(w, project / "orient.kicad_sch")
    v = verify_design(install, config, caps, design, tmp_path / "v", upgrade=design_name != "orientations")
    record_property("kicad", install.version)
    record_property("epoch", epoch_for(caps))
    record_property("erc_errors", v.erc_errors)
    record_property("erc_warnings", v.erc_warnings)
    record_property("nets", len(design.nets))
    record_property("upgrade_diff_lines", v.upgrade_diff_lines)
    assert v.erc_errors == 0, v.erc_types
    assert v.erc_warnings == 0, v.erc_types
    assert v.netlist_ok, (v.netlist_missing, v.netlist_extra)
    assert all(v.named_ok.values()), v.named_ok
    assert v.components == design.refs, v.components
    if design_name.startswith("hierarchy"):
        assert design.extra["u_refs"] == ["U1", "U2"] and design.extra["r_refs"] == ["R1", "R2"]
        hier = Hierarchy.load(design.root)
        assert {i.names_path for i in hier.instances} == {"/", "/source/", "/amp_a/", "/amp_b/"}
    if design_name == "orientations":
        assert len(design.nets) == 3 * len(ORIENTATIONS) // 2
    if v.upgrade_diff_lines is not None:
        assert v.upgrade_diff_lines == 0, "\n".join(v.upgrade_diff[:60])


# ----------------------------------------------------------------------------------------------
# 3: cross-version load matrix over the committed fixtures
# ----------------------------------------------------------------------------------------------


def _fixture_dirs() -> list[Path]:
    return sorted(p for p in WRITER_CORPUS.iterdir() if p.is_dir() and "_" in p.name)


def _era_of(fixture: Path) -> int:
    return int(fixture.name.rsplit("_", 1)[1])


def test_committed_fixtures_load_on_same_or_newer_kicad(env, tmp_path: Path, record_property):
    install, config, caps = env
    my_era = int(dialect_for(epoch_for(caps)).name)
    loaded: list[str] = []
    refused: list[str] = []
    for fixture in _fixture_dirs():
        era = _era_of(fixture)
        root = _copy_fixture(fixture, tmp_path / fixture.name)
        reference_net = next(fixture.glob("netlist_*.net"))
        want = netlist_partition(parse_netlist(reference_net.read_text(encoding="utf-8")))
        if era > my_era:
            # a newer dialect must be refused, never silently misread
            with pytest.raises(CliLoadError):
                export_netlist(install, config, root, tmp_path / f"{fixture.name}.net", caps=caps)
            refused.append(fixture.name)
            continue
        netlist = export_netlist(install, config, root, tmp_path / f"{fixture.name}.net", caps=caps)
        got = netlist_partition(netlist)
        assert sorted(map(sorted, got)) == sorted(map(sorted, want)), fixture.name
        from kiwrap.backends.cli.erc import run_erc

        report = run_erc(install, config, root, caps=caps, out_json=tmp_path / f"{fixture.name}.erc.json")
        assert report.errors == 0, (fixture.name, [f"{x.type}: {x.description}" for x in report.violations])
        loaded.append(f"{fixture.name}:warnings={report.warnings}")
    record_property("loaded", loaded)
    record_property("refused", refused)
    assert loaded, "no fixture of this era or older is committed"


# ----------------------------------------------------------------------------------------------
# 4: lossless edits on a validated file
# ----------------------------------------------------------------------------------------------


def test_edits_touch_only_their_lines_and_stay_valid(env, tmp_path: Path):
    install, config, caps = env
    project = tmp_path / "edit"
    project.mkdir()
    w = _writer(install, project, caps, config)
    design = build_blink(w, project / "blink.kicad_sch")
    before = design.root.read_text(encoding="utf-8").splitlines()
    uuids = design.extra["uuids"]
    w2 = _writer(install, project, caps, config)
    w2.load(design.root)
    w2.edit(uuids["R1"], {"value": "470", "reference": "R7"})
    w2.assign_footprints({"D1": "LED_SMD:LED_0805_2012Metric"})
    w2.save()
    after = design.root.read_text(encoding="utf-8").splitlines()
    assert len(before) == len(after)
    changed = [(a, b) for a, b in zip(before, after, strict=True) if a != b]
    assert len(changed) == 4, changed  # Value, Reference property, instances reference, Footprint
    design.nets = [frozenset(("R7", p) if r == "R1" else (r, p) for r, p in net) for net in design.nets]
    design.named = {
        k: frozenset(("R7", p) if r == "R1" else (r, p) for r, p in v) for k, v in design.named.items()
    }
    v = verify_design(install, config, caps, design, tmp_path / "v", upgrade=False)
    assert v.erc_errors == 0 and v.netlist_ok, (v.erc_types, v.netlist_missing, v.netlist_extra)
    assert "R7" in v.components and "R1" not in v.components
