"""`upgrade_copy`: sch/pcb only where `cli.has_sch_pcb_upgrade` (10.0), sym/fp everywhere, and
the source file is never touched (docs/ARCHITECTURE.md §6 emit policy)."""

from __future__ import annotations

import hashlib
import re
import shutil
from pathlib import Path

import pytest

from kiwrap.backends.cli import upgrade
from kiwrap.capability import Unsupported
from tests._fakes import (
    ERC_UNITS_SPIKE,
    demo_project,
    env_for,
    spike_board,
)

_ALL = [
    pytest.param("7.0", marks=pytest.mark.kicad_version("7.0")),
    pytest.param("8.0", marks=pytest.mark.kicad_version("8.0")),
    pytest.param("9.0", marks=pytest.mark.kicad_version("9.0")),
    pytest.param("10.0", marks=pytest.mark.kicad_version("10.0")),
]


def _stamp(path: Path) -> int:
    m = re.search(r"\(version (\d+)\)", path.read_text(encoding="utf-8", errors="replace")[:400])
    assert m, f"no version stamp in {path}"
    return int(m.group(1))


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_upgrade_kind():
    assert upgrade.upgrade_kind(Path("a.kicad_sch")) == "sch"
    assert upgrade.upgrade_kind(Path("a.kicad_pcb")) == "pcb"
    assert upgrade.upgrade_kind(Path("a.kicad_sym")) == "sym"
    assert upgrade.upgrade_kind(Path("lib.pretty")) == "fp"
    with pytest.raises(ValueError):
        upgrade.upgrade_kind(Path("a.kicad_pro"))


@pytest.mark.kicad
@pytest.mark.usefixtures("user_config_untouched")
@pytest.mark.parametrize("mm", _ALL)
def test_sch_pcb_upgrade_copy_or_unsupported(cli_envs, mm, tmp_path: Path):
    env = env_for(cli_envs, mm)
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    sch = src_dir / "t.kicad_sch"
    pcb = src_dir / "t.kicad_pcb"
    shutil.copy(ERC_UNITS_SPIKE / "t.kicad_sch", sch)
    shutil.copy(ERC_UNITS_SPIKE / "t.kicad_pcb", pcb)
    before = {p: _sha(p) for p in (sch, pcb)}
    for src in (sch, pcb):
        if env.caps.has("cli.has_sch_pcb_upgrade"):
            dst = upgrade.upgrade_copy(env.install, env.config, src, tmp_path / "up", caps=env.caps)
            assert dst == tmp_path / "up" / src.name and dst.is_file()
            assert _stamp(dst) > _stamp(src), "copy carries this version's (newer) stamp"
        else:
            with pytest.raises(Unsupported) as ei:
                upgrade.upgrade_copy(env.install, env.config, src, tmp_path / "up", caps=env.caps)
            assert (
                ei.value.capability == "cli.has_sch_pcb_upgrade" and ei.value.oldest_kicad_with_it == "10.0.0"
            )
            assert not (tmp_path / "up" / src.name).exists()
    assert {p: _sha(p) for p in (sch, pcb)} == before, "source files must be untouched"


@pytest.mark.kicad
@pytest.mark.usefixtures("user_config_untouched")
@pytest.mark.parametrize("mm", _ALL)
def test_sym_and_fp_upgrade_copy(cli_envs, mm, tmp_path: Path):
    env = env_for(cli_envs, mm)
    proj = demo_project(env.install, tmp_path)
    sym = next(proj.glob("*.kicad_sym"))
    pretty = proj / "footprints.pretty"
    sym_before, mods_before = _sha(sym), {p.name: _sha(p) for p in pretty.glob("*.kicad_mod")}
    up_sym = upgrade.upgrade_copy(env.install, env.config, sym, tmp_path / "up", caps=env.caps)
    assert up_sym.is_file() and _stamp(up_sym) >= _stamp(sym)
    up_fp = upgrade.upgrade_copy(env.install, env.config, pretty, tmp_path / "up", caps=env.caps)
    assert up_fp.is_dir() and len(list(up_fp.glob("*.kicad_mod"))) == len(mods_before)
    assert _sha(sym) == sym_before
    assert {p.name: _sha(p) for p in pretty.glob("*.kicad_mod")} == mods_before


@pytest.mark.kicad
@pytest.mark.kicad_version("10.0")
@pytest.mark.usefixtures("user_config_untouched")
def test_refuses_in_place(cli_envs, tmp_path: Path):
    env = env_for(cli_envs, "10.0")
    board = tmp_path / "hb.kicad_pcb"
    shutil.copy(spike_board("10.0", "test_inplace.kicad_pcb"), board)
    with pytest.raises(ValueError):
        upgrade.upgrade_copy(env.install, env.config, board, tmp_path, caps=env.caps)
