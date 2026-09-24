"""B2 (requests/kiwrap-feature-requests.review.md): kicad-cli rewrites an EXISTING `.kicad_prl`
even on runs that should not write (10.0.6 `pcb drc` without `--save-board`; re-verified
2026-09-24 on 10.0.6 and 10.99.0). `run_kicad_cli` snapshots it and puts it back byte-identical
(mtime too) - except when a KiCad editor lock sat next to it before the run. A Python script
stands in for kicad-cli (the pattern of tests/test_cli_runner.py)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kiwrap.backends.cli.runner import CliTimeout, run_kicad_cli
from tests.test_cli_runner import _args, fake_config, fake_install  # noqa: F401 - fixtures


def _board(tmp_path: Path) -> tuple[Path, Path]:
    board = tmp_path / "b.kicad_pcb"
    board.write_text("(kicad_pcb)")
    prl = board.with_suffix(".kicad_prl")
    prl.write_bytes(b'{"board": {"active_layer": 0}}\n')
    os.utime(prl, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))
    return board, prl


def test_rewritten_prl_is_restored_byte_identical(fake_install, fake_config, tmp_path):  # noqa: F811
    board, prl = _board(tmp_path)
    before, mtime = prl.read_bytes(), prl.stat().st_mtime_ns
    res = run_kicad_cli(fake_install, _args(0, f"prl={board}", str(board)), config=fake_config)
    assert prl.read_bytes() == before and prl.stat().st_mtime_ns == mtime
    assert res.restored_prl == [prl] and res.prl_kept == [] and res.removed_prl == []


def test_prl_is_restored_after_a_violations_exit_and_a_timeout(fake_install, fake_config, tmp_path):  # noqa: F811
    board, prl = _board(tmp_path)
    before = prl.read_bytes()
    res = run_kicad_cli(fake_install, _args(5, f"prl={board}", str(board)), config=fake_config)
    assert res.violations and prl.read_bytes() == before
    with pytest.raises(CliTimeout):
        run_kicad_cli(
            fake_install, _args(0, f"prl={board}", "sleep=30", str(board)), config=fake_config, timeout_s=2
        )
    assert prl.read_bytes() == before


def test_untouched_prl_is_not_rewritten(fake_install, fake_config, tmp_path):  # noqa: F811
    board, prl = _board(tmp_path)
    mtime = prl.stat().st_mtime_ns
    res = run_kicad_cli(fake_install, _args(0, str(board)), config=fake_config)
    assert res.restored_prl == [] and prl.stat().st_mtime_ns == mtime


def test_an_open_editor_keeps_its_prl(fake_install, fake_config, tmp_path):  # noqa: F811
    """A `~*.lck` next to the file before the run: an editor owns the project and its `.kicad_prl`
    (it rewrites the file on close anyway); the runner leaves the file alone and says so."""
    board, prl = _board(tmp_path)
    (tmp_path / "~b.kicad_pcb.lck").write_text("{}")
    res = run_kicad_cli(fake_install, _args(0, f"prl={board}", str(board)), config=fake_config)
    assert prl.read_text() == "prl" and res.prl_kept == [prl] and res.restored_prl == []
