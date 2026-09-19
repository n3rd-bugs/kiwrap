"""`pcb drc` adapter: JSON positions are already millimetres (claim 1), exit 5 = violations,
`--refill-zones --save-board` only where `cli.drc_refill_flags`, `Unsupported` on 7.0."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from kiwrap.backends.cli import drc
from kiwrap.capability import Unsupported
from tests._fakes import (
    ERC_UNITS_SPIKE,
    demo_project,
    env_for,
    spike_board,
)

# --------------------------------------------------------------------------------------------
# Offline
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("mm", ["8.0", "9.0", "10.0"])
def test_drc_json_from_spike_is_not_rescaled(mm):
    doc = json.loads((ERC_UNITS_SPIKE / f"drc_{mm}.json").read_text(encoding="utf-8"))
    report = drc.report_from_drc_json(doc, source="t.kicad_pcb")
    assert report.kind == "drc"
    v = report.violations[0]
    assert v.type == "track_dangling" and v.severity == "warning"
    assert v.pos.x_mm == pytest.approx(100.33) and v.pos.y_mm == pytest.approx(50.8)
    assert report.unconnected == () and report.schematic_parity == ()
    assert report.ok  # warnings never gate


def test_unconnected_items_count_towards_errors():
    doc = {
        "coordinate_units": "mm",
        "kicad_version": "x",
        "violations": [],
        "unconnected_items": [
            {"type": "unconnected_items", "severity": "error", "description": "d", "items": []}
        ],
        "schematic_parity": [{"type": "missing", "severity": "error", "description": "p", "items": []}],
    }
    report = drc.report_from_drc_json(doc, source="b")
    assert report.errors == 1 and not report.ok
    assert report.count("error", include_unconnected=False) == 0
    assert len(report.schematic_parity) == 1
    assert report.violations == () and report.unconnected[0].pos is None


# --------------------------------------------------------------------------------------------
# Live
# --------------------------------------------------------------------------------------------

_DRC_VERSIONS = [
    pytest.param("8.0", marks=pytest.mark.kicad_version("8.0")),
    pytest.param("9.0", marks=pytest.mark.kicad_version("9.0")),
    pytest.param("10.0", marks=pytest.mark.kicad_version("10.0")),
]


@pytest.mark.kicad
@pytest.mark.usefixtures("user_config_untouched")
@pytest.mark.parametrize("mm", _DRC_VERSIONS)
def test_run_drc_spike_board(cli_envs, mm, tmp_path: Path):
    env = env_for(cli_envs, mm)
    board = tmp_path / "t.kicad_pcb"
    shutil.copy(ERC_UNITS_SPIKE / "t.kicad_pcb", board)
    report = drc.run_drc(env.install, env.config, board, caps=env.caps)
    assert report.kicad_version == env.install.version
    dangling = [v for v in report.violations if v.type == "track_dangling"]
    assert (
        dangling
        and dangling[0].pos.x_mm == pytest.approx(100.33)
        and dangling[0].pos.y_mm == pytest.approx(50.8)
    )
    assert report.raw["_cli"]["exit_code_violations"] is True  # --severity-all counts the warning
    assert report.raw["_cli"]["returncode"] == 5
    assert not board.with_suffix(".kicad_prl").exists()


@pytest.mark.kicad
@pytest.mark.usefixtures("user_config_untouched")
@pytest.mark.parametrize("mm", _DRC_VERSIONS)
def test_run_drc_demo_project_with_parity(cli_envs, mm, tmp_path: Path):
    env = env_for(cli_envs, mm)
    proj = demo_project(env.install, tmp_path)
    board = proj / "ecc83-pp.kicad_pcb"
    before = hashlib.sha256(board.read_bytes()).hexdigest()
    report = drc.run_drc(
        env.install, env.config, board, caps=env.caps, schematic_parity=True, severity="error"
    )
    assert report.kind == "drc" and report.source.endswith("ecc83-pp.kicad_pcb")
    assert "schematic_parity" in report.raw
    assert hashlib.sha256(board.read_bytes()).hexdigest() == before, "read-only DRC must not touch the board"
    assert not list(proj.glob("*.kicad_prl")), ".kicad_prl left behind (10.0 writes one on read)"


@pytest.mark.kicad
@pytest.mark.usefixtures("user_config_untouched")
@pytest.mark.parametrize("mm", _DRC_VERSIONS)
def test_run_drc_headless_spike_board_is_clean(cli_envs, mm, tmp_path: Path):
    """The boards built headlessly by each version's python (spikes/verify-headless-board) DRC clean."""
    env = env_for(cli_envs, mm)
    board = tmp_path / "hb.kicad_pcb"
    shutil.copy(spike_board(mm, "test_inplace.kicad_pcb"), board)
    report = drc.run_drc(env.install, env.config, board, caps=env.caps, severity="error")
    assert report.errors == 0 and report.ok


@pytest.mark.kicad
@pytest.mark.usefixtures("user_config_untouched")
def test_refill_zones_save_board_where_supported(cli_envs, tmp_path: Path):
    """`--refill-zones --save-board` only where `cli.drc_refill_flags` (10.0+); elsewhere Unsupported
    with the SWIG ZONE_FILLER fallback."""
    ran = 0
    for mm, env in cli_envs.items():
        if not env.caps.has("cli.has_drc"):
            continue
        proj = demo_project(env.install, tmp_path / mm)
        board = proj / "ecc83-pp.kicad_pcb"
        if env.caps.has("cli.drc_refill_flags"):
            before = board.stat().st_mtime_ns
            report = drc.run_drc(
                env.install, env.config, board, caps=env.caps, refill_zones=True, save_board=True
            )
            assert report.raw["_cli"]["board_saved"] is True
            assert board.stat().st_mtime_ns != before, "--save-board should rewrite the board"
        else:
            with pytest.raises(Unsupported) as ei:
                drc.run_drc(env.install, env.config, board, caps=env.caps, refill_zones=True)
            assert ei.value.fallback == "swig ZONE_FILLER" and ei.value.oldest_kicad_with_it == "10.0.0"
        with pytest.raises(ValueError):  # --save-board without --refill-zones is a caller bug
            drc.run_drc(env.install, env.config, board, caps=env.caps, save_board=True)
        ran += 1
    if not ran:
        pytest.skip("no KiCad with pcb drc installed")


@pytest.mark.kicad
@pytest.mark.kicad_version("7.0")
@pytest.mark.usefixtures("user_config_untouched")
def test_drc_unsupported_on_7(cli_envs, tmp_path: Path):
    env = env_for(cli_envs, "7.0")
    with pytest.raises(Unsupported) as ei:
        drc.run_drc(env.install, env.config, spike_board("7.0"), caps=env.caps)
    assert ei.value.capability == "cli.has_drc" and ei.value.oldest_kicad_with_it == "8.0.0"
    assert "swig" in (ei.value.fallback or "")


def test_report_from_drc_json_is_the_formats_parser_without_scaling():
    from kiwrap.formats.reports.drc import parse_drc_json

    doc = json.loads((ERC_UNITS_SPIKE / "drc_10.0.json").read_text(encoding="utf-8"))
    report = drc.report_from_drc_json(doc, source="b")
    direct = parse_drc_json(json.dumps(doc), source="b")
    assert report.kind == "drc" and report.violations == direct.violations
    assert report.unconnected == direct.unconnected and report.raw == doc
