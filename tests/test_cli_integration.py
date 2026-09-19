"""End-to-end through the REAL `kiwrap.sandbox` root and the REAL `capabilities_for` record
(`caps=None`), one pass per installed KiCad, all under the session's throw-away storage home."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from kiwrap.backends.cli import drc, erc, export, netlist, render, upgrade
from kiwrap.capability import Unsupported

pytestmark = [pytest.mark.kicad, pytest.mark.slow, pytest.mark.usefixtures("user_config_untouched")]


@pytest.fixture
def real_env(config_root, storage_home):
    from kiwrap.capability import capabilities_for

    def _make(install):
        return config_root(install), capabilities_for(install, storage_home=storage_home)

    return _make


def test_every_install_end_to_end(installs, real_env, tmp_path: Path):
    for install in installs:
        cfg, caps = real_env(install)
        proj = tmp_path / install.major_minor
        shutil.copytree(install.dir("demos") / "ecc83", proj)
        sch, pcb = proj / "ecc83-pp.kicad_sch", proj / "ecc83-pp.kicad_pcb"

        if caps.has("cli.has_erc"):
            assert erc.run_erc(install, cfg, sch).kind == "erc"
        else:
            with pytest.raises(Unsupported):
                erc.run_erc(install, cfg, sch)
        if caps.has("cli.has_drc"):
            assert drc.run_drc(install, cfg, pcb).kind == "drc"
        else:
            with pytest.raises(Unsupported):
                drc.run_drc(install, cfg, pcb)

        assert len(netlist.export_netlist(install, cfg, sch).nets) == 13
        assert export.export_pcb(install, cfg, pcb, "gerbers", proj / "g")
        assert export.export_sch(install, cfg, sch, "pdf", proj / "s.pdf")[0].read_bytes().startswith(b"%PDF")

        png = render.render_pcb_2d(install, cfg, pcb, proj / "2d.png", max_px=512)
        assert png.read_bytes().startswith(render.PNG_SIGNATURE)
        if caps.has("cli.has_render"):
            png3d = render.render_pcb(install, cfg, pcb, proj / "3d.png", width=640, height=360)
            assert png3d.read_bytes().startswith(render.PNG_SIGNATURE)
        else:
            with pytest.raises(Unsupported):
                render.render_pcb(install, cfg, pcb, proj / "3d.png")

        if caps.has("cli.has_sch_pcb_upgrade"):
            assert upgrade.upgrade_copy(install, cfg, pcb, proj / "up").is_file()
        else:
            with pytest.raises(Unsupported):
                upgrade.upgrade_copy(install, cfg, pcb, proj / "up")

        assert not list(proj.glob("*.kicad_prl")), f"{install.version} left a .kicad_prl"
