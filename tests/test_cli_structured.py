"""`kiwrap.backends.cli.structured` — offline capability gating plus live runs on every
installed KiCad that has the subcommand (primary: the 10.99 nightly and 10.0; ipc2581 / ipcd356 /
pos / stats also on 9.0 / 8.0 where they exist).

The live fixtures build one REAL sandbox root + REAL capability record per installed
major.minor (including the nightly, which `tests._fakes.cli_envs` does not cover because its
hand-written matrix stops at 10.0). Every live test asserts the user's real KiCad config is
untouched and that no `.kicad_prl` is left next to the demo.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

import pytest

from kiwrap.backends.cli import structured
from kiwrap.backends.cli.runner import CliLoadError
from kiwrap.capability import Capabilities, Unsupported
from kiwrap.formats.reports.ipc2581 import Point
from kiwrap.installation import KiCadInstallation
from kiwrap.sandbox import ConfigRoot
from tests._fakes import user_config_untouched  # noqa: F401 - fixture

# --------------------------------------------------------------------------------------------
# Offline: capability gating against hand-built records
# --------------------------------------------------------------------------------------------


def _caps(values: dict) -> Capabilities:
    install = KiCadInstallation(
        root=Path(), kicad_cli=Path("kicad-cli"), share=Path(), version="0.0.0", source="explicit"
    )
    return Capabilities(install, values, probed=False)


def test_capability_reads_the_key_first():
    assert structured.capability(_caps({"cli.has_sch_diff": True}), "cli.has_sch_diff")
    assert not structured.capability(
        _caps({"cli.has_sch_diff": False, "cli.subcommands": ["sch diff"]}), "cli.has_sch_diff"
    )


def test_capability_is_derived_from_flag_lists_when_the_key_is_missing():
    """A capability record cached before these keys existed still answers correctly."""
    caps = _caps(
        {
            "cli.subcommands": ["pcb export stats", "pcb export stackup", "sch diff", "pcb export png"],
            "cli.flags.pcb export stats": ["--output", "--format"],
            "cli.flags.pcb export stackup": ["--output"],
        }
    )
    assert structured.capability(caps, "cli.has_stats_json")
    assert not structured.capability(caps, "cli.has_stackup_json")  # listed, but no --format
    assert structured.capability(caps, "cli.has_sch_diff")
    assert structured.capability(caps, "cli.has_pcb_png")
    assert not structured.capability(_caps({}), "cli.has_pcb_png")  # no lists at all -> unknown -> False
    assert not structured.capability(caps, "cli.has_render")  # unknown key, no derivation


def test_unsupported_names_oldest_version_and_fallback(tmp_path: Path):
    caps = _caps({"cli.subcommands": ["pcb export gerbers"], "cli.has_stats_json": False})
    board = tmp_path / "b.kicad_pcb"
    board.write_text("(kicad_pcb)", encoding="utf-8")
    with pytest.raises(Unsupported) as ei:
        structured.export_stats(caps.install, None, board, caps=caps)
    assert ei.value.capability == "cli.has_stats_json"
    assert ei.value.oldest_kicad_with_it == "10.0.0" and ei.value.fallback == "ipc2581"
    with pytest.raises(Unsupported) as ei:
        structured.sch_diff(caps.install, None, board, board, caps=caps)
    assert ei.value.oldest_kicad_with_it == "11.0.0" and ei.value.fallback == "netlist"
    with pytest.raises(Unsupported) as ei:
        structured.export_png(caps.install, None, board, tmp_path / "png", caps=caps)
    assert ei.value.capability == "cli.has_pcb_png" and ei.value.fallback == "render"
    with pytest.raises(Unsupported) as ei:
        structured.export_ipcd356(caps.install, None, board, caps=caps)
    assert (
        ei.value.capability == "cli.subcommands:pcb export ipcd356"
        and ei.value.oldest_kicad_with_it == "9.0.0"
    )
    with pytest.raises(Unsupported) as ei:
        structured.export_stackup(caps.install, None, board, caps=caps, fallback_to_board=False)
    assert ei.value.fallback == "board"


def test_stackup_board_fallback_without_kicad(tmp_path: Path):
    caps = _caps({"cli.subcommands": ["pcb export gerbers"]})
    board = tmp_path / "b.kicad_pcb"
    board.write_text(
        '(kicad_pcb (layers (0 "F.Cu" signal) (31 "B.Cu" signal)) (setup (stackup '
        '(layer "F.Cu" (type "copper") (thickness 0.035)) (layer "dielectric 1" (type "core") (thickness 1.51)) '
        '(layer "B.Cu" (type "copper") (thickness 0.035)) (copper_finish "ENIG"))))',
        encoding="utf-8",
    )
    s = structured.export_stackup(caps.install, None, board, caps=caps)
    assert s.source == "board" and s.copper_count == 2 and s.finish == "ENIG"


def test_read_aux_origin(tmp_path: Path):
    board = tmp_path / "b.kicad_pcb"
    board.write_text("(kicad_pcb (setup (aux_axis_origin 100 50.5)))", encoding="utf-8")
    assert structured.read_aux_origin(board) == (100.0, 50.5)
    board.write_text("(kicad_pcb (setup))", encoding="utf-8")
    assert structured.read_aux_origin(board) == (0.0, 0.0)
    assert structured.read_aux_origin(tmp_path / "missing.kicad_pcb") == (0.0, 0.0)


# --------------------------------------------------------------------------------------------
# Live fixtures: one real sandbox root + capability record per installed major.minor
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class LiveEnv:
    install: KiCadInstallation
    config: ConfigRoot
    caps: Capabilities


@pytest.fixture(scope="session")
def live_envs(installs, storage_home: Path) -> dict[str, LiveEnv]:
    from kiwrap import sandbox
    from kiwrap.capability import capabilities_for

    envs: dict[str, LiveEnv] = {}
    for install in installs:
        if install.major_minor in envs:
            continue
        config = sandbox.ensure(install, storage_home=storage_home)
        caps = capabilities_for(install, storage_home=storage_home)
        envs[install.major_minor] = LiveEnv(install=install, config=config, caps=caps)
    return envs


def _env(
    live_envs: dict[str, LiveEnv], caps_key: str | None = None, subcommand: str | None = None
) -> list[LiveEnv]:
    """Every installed version whose record has the capability / subcommand (newest first)."""
    out = []
    for env in live_envs.values():
        if caps_key and not structured.capability(env.caps, caps_key):
            continue
        if subcommand and subcommand not in env.caps.get("cli.subcommands", []):
            continue
        out.append(env)
    if not out:
        pytest.skip(f"no installed KiCad has {caps_key or subcommand}")
    return out


def _nightly(live_envs: dict[str, LiveEnv]) -> LiveEnv:
    nightly = [e for e in live_envs.values() if e.install.major_minor.endswith(".99")]
    if not nightly:
        pytest.skip("no nightly installed")
    return nightly[0]


def _demo(install: KiCadInstallation, tmp: Path, name: str = "ecc83") -> Path:
    src = install.dir("demos") / name
    if not src.is_dir():
        pytest.skip(f"demo {name} not shipped with KiCad {install.version}")
    dst = tmp / install.version / name
    shutil.copytree(src, dst)
    return dst


def _board(install: KiCadInstallation, tmp: Path) -> Path:
    return _demo(install, tmp) / "ecc83-pp.kicad_pcb"


def _no_prl(project: Path) -> None:
    assert not list(project.glob("*.kicad_prl")), "a .kicad_prl survived the run"


# --------------------------------------------------------------------------------------------
# Live: stats (10.0 + nightly)
# --------------------------------------------------------------------------------------------


@pytest.mark.kicad
@pytest.mark.usefixtures("user_config_untouched")
def test_live_stats(live_envs, tmp_path: Path):
    for env in _env(live_envs, caps_key="cli.has_stats_json"):
        board = _board(env.install, tmp_path)
        s = structured.export_stats(
            env.install, env.config, board, out_dir=tmp_path / env.install.version / "stats", caps=env.caps
        )
        assert s.kicad_version == env.install.version and s.board_name == "ecc83-pp"
        assert s.width_mm == pytest.approx(52.07) and s.area_mm2 == pytest.approx(2413.705, abs=0.01)
        assert (
            s.pads.through_hole == 33
            and s.component_count == 15
            and s.board_thickness_mm == pytest.approx(1.6)
        )
        assert sum(h.count for h in s.drill_holes) == 33
        _no_prl(board.parent)


@pytest.mark.kicad
def test_live_stats_unsupported_where_absent(live_envs, tmp_path: Path):
    old = [e for e in live_envs.values() if not structured.capability(e.caps, "cli.has_stats_json")]
    if not old:
        pytest.skip("every installed KiCad has stats json")
    env = old[0]
    board = _board(env.install, tmp_path)
    with pytest.raises(Unsupported) as ei:
        structured.export_stats(env.install, env.config, board, caps=env.caps)
    assert ei.value.fallback == "ipc2581"


# --------------------------------------------------------------------------------------------
# Live: stackup (json on the nightly; board-file fallback everywhere else)
# --------------------------------------------------------------------------------------------


@pytest.mark.kicad
@pytest.mark.usefixtures("user_config_untouched")
def test_live_stackup_json_nightly(live_envs, tmp_path: Path):
    env = _env(live_envs, caps_key="cli.has_stackup_json")[0]
    board = _board(env.install, tmp_path)
    s = structured.export_stackup(env.install, env.config, board, out_dir=tmp_path / "stackup", caps=env.caps)
    assert s.source == "json" and s.copper_count == 2 and s.finish == "None"
    assert s.total_thickness_mm == pytest.approx(1.6)
    assert s.layer("F.Cu").user_name == "top_cu" and s.layer("F.Cu").thickness_mm == pytest.approx(0.035)
    # the board file's own section describes the same stackup
    b = structured.export_stackup(env.install, env.config, board, caps=env.caps, fallback_to_board=True)
    assert b.source == "json"
    from kiwrap.formats.reports.stackup import parse_stackup_board

    fb = parse_stackup_board(board.read_text(encoding="utf-8"))
    assert [x.kind for x in fb.layers] == [x.kind for x in s.layers]
    assert fb.total_thickness_mm == pytest.approx(s.total_thickness_mm)
    _no_prl(board.parent)


@pytest.mark.kicad
@pytest.mark.usefixtures("user_config_untouched")
def test_live_stackup_falls_back_to_board_file(live_envs, tmp_path: Path):
    old = [e for e in live_envs.values() if not structured.capability(e.caps, "cli.has_stackup_json")]
    if not old:
        pytest.skip("every installed KiCad exports stackup json")
    env = old[0]
    board = _board(env.install, tmp_path)
    s = structured.export_stackup(env.install, env.config, board, caps=env.caps)
    assert s.source == "board" and s.has_stackup_section and s.copper_count == 2
    assert s.total_thickness_mm == pytest.approx(1.6)
    with pytest.raises(Unsupported):
        structured.export_stackup(env.install, env.config, board, caps=env.caps, fallback_to_board=False)


# --------------------------------------------------------------------------------------------
# Live: sch diff (nightly)
# --------------------------------------------------------------------------------------------


@pytest.mark.kicad
@pytest.mark.usefixtures("user_config_untouched")
def test_live_sch_diff(live_envs, tmp_path: Path):
    env = _env(live_envs, caps_key="cli.has_sch_diff")[0]
    proj = _demo(env.install, tmp_path)
    sch = proj / "ecc83-pp.kicad_sch"
    same = structured.sch_diff(env.install, env.config, sch, sch, caps=env.caps)
    assert same.identical and same.exit_code == 0 and same.doc_type == "kicad_sch"

    text = sch.read_text(encoding="utf-8")
    modified = text.replace('(property "Value" "1.5K"', '(property "Value" "2.2K"', 1)
    modified = modified.replace(
        '\t(symbol\n\t\t(lib_id "ecc83-pp:R")\n\t\t(at 154.94 127 0)',
        '\t(symbol\n\t\t(lib_id "ecc83-pp:R")\n\t\t(at 165.10 127 0)',
        1,
    )
    assert modified != text
    other = tmp_path / "ecc83-pp-mod.kicad_sch"
    other.write_text(modified, encoding="utf-8")
    out = tmp_path / "diff" / "changes.json"
    d = structured.sch_diff(env.install, env.config, sch, other, out=out, caps=env.caps)
    assert d.exit_code == 5 and not d.identical and out.is_file()
    assert d.reference == str(sch) and d.comparison == str(other)
    assert d.counts["modified"] == 2 and d.counts["added"] == 0 and d.counts["removed"] == 0
    r1 = next(c for c in d.modified if c.refdes == "R1")
    assert r1.property("Value").after.value == "2.2K"
    r2 = next(c for c in d.modified if c.refdes == "R2")
    pos = r2.property("Position X")
    assert pos.before_mm == pytest.approx(154.94) and pos.after_mm == pytest.approx(165.1)

    with pytest.raises(CliLoadError):
        structured.sch_diff(env.install, env.config, sch, tmp_path / "missing.kicad_sch", caps=env.caps)
    _no_prl(proj)


# --------------------------------------------------------------------------------------------
# Live: IPC-2581 (8.0+), IPC-D-356 (9.0+), position (all)
# --------------------------------------------------------------------------------------------


@pytest.mark.kicad
@pytest.mark.usefixtures("user_config_untouched")
def test_live_ipc2581(live_envs, tmp_path: Path):
    for env in _env(live_envs, subcommand="pcb export ipc2581"):
        board = _board(env.install, tmp_path)
        b = structured.export_ipc2581(
            env.install, env.config, board, out_dir=tmp_path / env.install.version / "ipc", caps=env.caps
        )
        assert Path(b.source).is_file() and b.name == "ecc83-pp"
        assert len(b.components) == 15 and len(b.pads) == 33 and b.copper_layers == ("top_cu", "bottom_cu")
        c1 = b.component("C1")
        assert (c1.x_mm, c1.y_mm, c1.rotation_deg, c1.side) == (141.605, 99.695, 90.0, "top")
        assert c1.value == "10uF"
        gnd = b.net("GND")
        assert gnd is not None and ("C1", "2") in gnd.pins and len(gnd.pins) == 7
        assert b.bbox == (Point(121.285, 90.17), Point(173.355, 136.525))
        assert len(b.holes) == 33 and all(h.plating == "PLATED" for h in b.holes)
        _no_prl(board.parent)


@pytest.mark.kicad
@pytest.mark.usefixtures("user_config_untouched")
def test_live_ipcd356(live_envs, tmp_path: Path):
    for env in _env(live_envs, subcommand="pcb export ipcd356"):
        board = _board(env.install, tmp_path)
        t = structured.export_ipcd356(
            env.install, env.config, board, out_dir=tmp_path / env.install.version / "d356", caps=env.caps
        )
        assert len(t.points) == 33 and t.aux_origin_mm == (0.0, 0.0)
        c1 = next(p for p in t.of("C1") if p.pin == "1")  # the nightly sorts records by net
        assert (c1.x_mm, c1.y_mm) == (pytest.approx(141.605), pytest.approx(99.695))
        assert c1.net.upper() == "NET-(P3-P1)" and c1.plated and c1.drill_mm == pytest.approx(1.0, abs=0.002)
        # the nightly aliases long names; 10.0 truncates to the last 14 upper-cased characters
        p5 = t.of("P5")[0]
        assert p5.net in ("unconnected-(P5-Pad1)", "CTED-(P5-PAD1)")
        _no_prl(board.parent)


@pytest.mark.kicad
@pytest.mark.usefixtures("user_config_untouched")
def test_live_position(live_envs, tmp_path: Path):
    for env in _env(live_envs, subcommand="pcb export pos"):
        board = _board(env.install, tmp_path)
        p = structured.export_position(
            env.install, env.config, board, out_dir=tmp_path / env.install.version / "pos", caps=env.caps
        )
        assert len(p.rows) == 11 and p.format == "csv"
        c1 = p.row("C1")
        assert (c1.x_mm, c1.y_mm, c1.rotation_deg, c1.side) == (141.605, 99.695, 90.0, "top")
        assert p.row("R2").rotation_deg == 180.0 and p.row("P2").rotation_deg == -90.0
        _no_prl(board.parent)


@pytest.mark.kicad
@pytest.mark.usefixtures("user_config_untouched")
def test_live_position_with_aux_origin_is_absolute_again(live_envs, tmp_path: Path):
    env = (
        _nightly(live_envs)
        if any(e.install.major_minor.endswith(".99") for e in live_envs.values())
        else next(iter(live_envs.values()))
    )
    board = _board(env.install, tmp_path)
    text = board.read_text(encoding="utf-8")
    assert "(aux_axis_origin" not in text
    board.write_text(text.replace("(setup\n", "(setup\n\t\t(aux_axis_origin 100 50)\n", 1), encoding="utf-8")
    assert structured.read_aux_origin(board) == (100.0, 50.0)
    p = structured.export_position(
        env.install,
        env.config,
        board,
        out_dir=tmp_path / "pos-origin",
        caps=env.caps,
        use_drill_file_origin=True,
    )
    c1 = p.row("C1")
    assert (c1.x_raw, c1.y_raw) == (pytest.approx(41.605), pytest.approx(-49.695))
    assert (c1.x_mm, c1.y_mm) == (pytest.approx(141.605), pytest.approx(99.695))
    if "pcb export ipcd356" in env.caps.get("cli.subcommands", []):
        t = structured.export_ipcd356(
            env.install, env.config, board, out_dir=tmp_path / "d356-origin", caps=env.caps
        )
        pad = next(p for p in t.of("C1") if p.pin == "1")
        # decimil resolution: 41.605 mm = 16379.9 -> 16380 decimils = 41.6052 mm
        assert (pad.x_mm, pad.y_mm) == (pytest.approx(141.605, abs=0.002), pytest.approx(99.695, abs=0.002))
        assert pad.x_raw_mm == pytest.approx(41.605, abs=0.002)
        assert pad.y_raw_mm == pytest.approx(-49.695, abs=0.002)


# --------------------------------------------------------------------------------------------
# Live: 2D PNG (nightly)
# --------------------------------------------------------------------------------------------


@pytest.mark.kicad
@pytest.mark.usefixtures("user_config_untouched")
def test_live_export_png(live_envs, tmp_path: Path):
    env = _env(live_envs, caps_key="cli.has_pcb_png")[0]
    board = _board(env.install, tmp_path)
    files = structured.export_png(
        env.install, env.config, board, tmp_path / "png", layers=["F.Cu", "Edge.Cuts"], caps=env.caps, dpi=150
    )
    assert len(files) == 2 and all(f.suffix == ".png" and f.stat().st_size > 100 for f in files)
    assert all(f.read_bytes().startswith(b"\x89PNG") for f in files)
    # the demo renamed F.Cu to "top_cu": the file carries the user name
    assert {f.name for f in files} == {"ecc83-pp-top_cu.png", "ecc83-pp-Edge_Cuts.png"}
    _no_prl(board.parent)


@pytest.mark.kicad
@pytest.mark.usefixtures("user_config_untouched")
def test_live_default_out_dir_stays_out_of_the_project(live_envs, tmp_path: Path):
    env = _env(live_envs, subcommand="pcb export pos")[0]
    board = _board(env.install, tmp_path)
    before = sorted(p.name for p in board.parent.iterdir())
    p = structured.export_position(env.install, env.config, board, caps=env.caps)
    assert len(p.rows) == 11
    assert sorted(x.name for x in board.parent.iterdir()) == before, "the raw export landed in the project"
    assert Path(p.source).is_relative_to(env.config.temp_dir())
