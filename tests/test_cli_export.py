"""`pcb export *` / `sch export *` on 7.0-10.0 against the ecc83 demo and the spike boards.

Quirks under test: the output directory is created even where kicad-cli does not (7/8), single
file svg/pdf on every version (`--mode-single` where listed), `--layers` comma list, sch svg
output is a directory while sch pdf is a file, subcommands/flags a version lacks raise
`Unsupported`, and 10.0's `.kicad_prl` never survives."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from kiwrap.backends.cli import export
from kiwrap.capability import Capabilities, Unsupported
from kiwrap.installation import KiCadInstallation
from tests._fakes import (
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


# --------------------------------------------------------------------------------------------
# Offline: option -> flag translation against a hand-built record
# --------------------------------------------------------------------------------------------


def _caps(values: dict) -> Capabilities:
    install = KiCadInstallation(
        root=Path(), kicad_cli=Path("kicad-cli"), share=Path(), version="0.0.0", source="explicit"
    )
    return Capabilities(install, values, probed=False)


def test_opts_to_args_rejects_unknown_and_unlisted_flags():
    caps = _caps({"cli.flags.pcb export pos": ["--format", "--units"]})
    assert export._opts_to_args(
        caps, "pcb export pos", export._PCB_OPTS, {"format": "csv", "smd_only": False}
    ) == ["--format", "csv"]
    with pytest.raises(Unsupported) as ei:
        export._opts_to_args(caps, "pcb export pos", export._PCB_OPTS, {"exclude_dnp": True})
    assert ei.value.capability == "cli.flags.pcb export pos:--exclude-dnp"
    with pytest.raises(ValueError):
        export._opts_to_args(caps, "pcb export pos", export._PCB_OPTS, {"bogus": 1})


def test_opts_to_args_csv_and_repeat():
    caps = _caps({})
    args = export._opts_to_args(
        caps, "pcb export svg", export._PCB_OPTS, {"layers": ["F.Cu", "B.Cu"], "define_vars": {"A": "1"}}
    )
    assert args == ["--layers", "F.Cu,B.Cu", "--define-var", "A=1"]


def test_output_kind_override_from_record():
    caps = _caps({"cli.output_kind.sch export svg": "file"})
    assert export.output_kind(caps, "sch export svg") == "file"
    assert export.output_kind(_caps({}), "sch export svg") == "dir"
    assert export.output_kind(_caps({}), "pcb export gerbers") == "dir"
    assert export.output_kind(_caps({}), "pcb export pos") == "file"


def test_unknown_targets_are_rejected(tmp_path):
    caps = _caps({})
    with pytest.raises(ValueError):
        export.export_pcb(caps.install, None, tmp_path / "b.kicad_pcb", "png", tmp_path, caps=caps)
    with pytest.raises(ValueError):
        export.export_sch(caps.install, None, tmp_path / "s.kicad_sch", "png", tmp_path, caps=caps)


# --------------------------------------------------------------------------------------------
# Live
# --------------------------------------------------------------------------------------------


@pytest.mark.kicad
@pytest.mark.usefixtures("user_config_untouched")
@pytest.mark.parametrize("mm", _ALL)
def test_gerbers_and_drill_create_outdir(cli_envs, mm, tmp_path: Path):
    env = env_for(cli_envs, mm)
    proj = demo_project(env.install, tmp_path)
    board = proj / "ecc83-pp.kicad_pcb"
    out = tmp_path / "fab" / "nested" / "gerbers"  # does not exist; 7/8 would fail without mkdir
    files = export.export_pcb(env.install, env.config, board, "gerbers", out, caps=env.caps)
    names = {f.name for f in files}
    assert out.is_dir() and all(f.parent == out for f in files)
    assert {"ecc83-pp-top_cu.gtl", "ecc83-pp-bottom_cu.gbl", "ecc83-pp-Edge_Cuts.gm1"} <= names
    drills = export.export_pcb(
        env.install, env.config, board, "drill", tmp_path / "fab" / "drill", caps=env.caps, generate_map=True
    )
    assert any(f.suffix == ".drl" for f in drills)
    assert any(f.suffix == ".pdf" for f in drills), "--generate-map map file"
    assert not list(proj.glob("*.kicad_prl"))


@pytest.mark.kicad
@pytest.mark.usefixtures("user_config_untouched")
@pytest.mark.parametrize("mm", _ALL)
def test_layer_subset_gerbers(cli_envs, mm, tmp_path: Path):
    env = env_for(cli_envs, mm)
    proj = demo_project(env.install, tmp_path)
    files = export.export_pcb(
        env.install,
        env.config,
        proj / "ecc83-pp.kicad_pcb",
        "gerbers",
        tmp_path / "g",
        caps=env.caps,
        layers=["F.Cu", "Edge.Cuts"],
    )
    names = {f.name for f in files if f.suffix != ".gbrjob"}
    assert names == {"ecc83-pp-F_Cu.gtl", "ecc83-pp-Edge_Cuts.gm1"} or names == {
        "ecc83-pp-top_cu.gtl",
        "ecc83-pp-Edge_Cuts.gm1",
    }


@pytest.mark.kicad
@pytest.mark.usefixtures("user_config_untouched")
@pytest.mark.parametrize("mm", _ALL)
def test_pos_csv(cli_envs, mm, tmp_path: Path):
    env = env_for(cli_envs, mm)
    proj = demo_project(env.install, tmp_path)
    files = export.export_pcb(
        env.install,
        env.config,
        proj / "ecc83-pp.kicad_pcb",
        "pos",
        tmp_path / "pos",
        caps=env.caps,
        format="csv",
        units="mm",
    )
    assert [f.name for f in files] == ["ecc83-pp.csv"]
    head = files[0].read_text(encoding="utf-8", errors="replace").splitlines()[0]
    assert "Ref" in head and "PosX" in head


@pytest.mark.kicad
@pytest.mark.usefixtures("user_config_untouched")
@pytest.mark.parametrize("mm", _ALL)
def test_svg_pdf_dxf_single_file(cli_envs, mm, tmp_path: Path):
    env = env_for(cli_envs, mm)
    board = tmp_path / "hb.kicad_pcb"
    shutil.copy(spike_board(mm, "test_inplace.kicad_pcb"), board)
    # DXF group codes are line based; kicad-cli writes CRLF on Windows and LF on Linux (CI, 2026-09-19).
    for target, magic in (("svg", b"<?xml"), ("pdf", b"%PDF"), ("dxf", b"  0")):
        files = export.export_pcb(
            env.install,
            env.config,
            board,
            target,
            tmp_path / target,
            caps=env.caps,
            layers=["F.Cu", "Edge.Cuts"],
        )
        assert len(files) == 1 and files[0].name == f"hb.{target}", files
        head = files[0].read_bytes()[:16]
        assert head.startswith(magic) or magic.strip() in head, (target, head)
    assert not board.with_suffix(".kicad_prl").exists()


@pytest.mark.kicad
@pytest.mark.slow
@pytest.mark.usefixtures("user_config_untouched")
@pytest.mark.parametrize("mm", _ALL)
def test_step_export(cli_envs, mm, tmp_path: Path):
    env = env_for(cli_envs, mm)
    board = tmp_path / "hb.kicad_pcb"
    shutil.copy(spike_board(mm, "test_inplace.kicad_pcb"), board)
    files = export.export_pcb(
        env.install, env.config, board, "step", tmp_path / "step", caps=env.caps, board_only=True
    )
    assert files[0].name == "hb.step" and files[0].stat().st_size > 1000
    assert files[0].read_bytes()[:20].startswith(b"ISO-10303-21")


@pytest.mark.kicad
@pytest.mark.usefixtures("user_config_untouched")
@pytest.mark.parametrize("mm", _ALL)
def test_sch_pdf_svg(cli_envs, mm, tmp_path: Path):
    env = env_for(cli_envs, mm)
    proj = demo_project(env.install, tmp_path)
    sch = proj / "ecc83-pp.kicad_sch"
    pdf = export.export_sch(
        env.install, env.config, sch, "pdf", tmp_path / "sch" / "ecc83.pdf", caps=env.caps
    )
    assert pdf == [tmp_path / "sch" / "ecc83.pdf"] and pdf[0].read_bytes().startswith(b"%PDF")
    svgs = export.export_sch(
        env.install, env.config, sch, "svg", tmp_path / "svgdir", caps=env.caps, black_and_white=True
    )
    assert svgs and all(f.suffix == ".svg" and f.parent == tmp_path / "svgdir" for f in svgs)
    assert not list(proj.glob("*.kicad_prl"))


@pytest.mark.kicad
@pytest.mark.usefixtures("user_config_untouched")
@pytest.mark.parametrize("mm", _ALL)
def test_sch_bom_or_unsupported(cli_envs, mm, tmp_path: Path):
    env = env_for(cli_envs, mm)
    proj = demo_project(env.install, tmp_path)
    sch = proj / "ecc83-pp.kicad_sch"
    out = tmp_path / "bom.csv"
    if "sch export bom" in env.caps["cli.subcommands"]:
        files = export.export_sch(
            env.install,
            env.config,
            sch,
            "bom",
            out,
            caps=env.caps,
            fields=["Reference", "Value", "Footprint"],
            group_by=["Value"],
        )
        assert files == [out]
        lines = out.read_text(encoding="utf-8", errors="replace").splitlines()
        assert lines[0].startswith('"Reference"') or lines[0].startswith("Reference")
        assert len(lines) > 2
    else:
        with pytest.raises(Unsupported) as ei:
            export.export_sch(env.install, env.config, sch, "bom", out, caps=env.caps)
        assert ei.value.oldest_kicad_with_it == "8.0.0"


@pytest.mark.kicad
@pytest.mark.usefixtures("user_config_untouched")
@pytest.mark.parametrize("mm", _ALL)
def test_sch_netlist_via_export_sch(cli_envs, mm, tmp_path: Path):
    env = env_for(cli_envs, mm)
    proj = demo_project(env.install, tmp_path)
    files = export.export_sch(
        env.install, env.config, proj / "ecc83-pp.kicad_sch", "netlist", tmp_path / "n.net", caps=env.caps
    )
    assert files == [tmp_path / "n.net"] and files[0].read_text(encoding="utf-8").lstrip().startswith(
        "(export"
    )


@pytest.mark.kicad
@pytest.mark.usefixtures("user_config_untouched")
def test_version_specific_subcommands(cli_envs, tmp_path: Path):
    """`pcb export stats` (10.0+) and `odb` (9.0+): produced where listed, Unsupported elsewhere."""
    for mm, env in cli_envs.items():
        board = tmp_path / mm / "hb.kicad_pcb"
        board.parent.mkdir(parents=True)
        shutil.copy(spike_board(mm, "test_inplace.kicad_pcb"), board)
        for target, oldest in (("stats", "10.0.0"), ("odb", "9.0.0")):
            if f"pcb export {target}" in env.caps["cli.subcommands"]:
                files = export.export_pcb(
                    env.install, env.config, board, target, tmp_path / mm / target, caps=env.caps
                )
                assert files and files[0].stat().st_size > 0
            else:
                with pytest.raises(Unsupported) as ei:
                    export.export_pcb(
                        env.install, env.config, board, target, tmp_path / mm / target, caps=env.caps
                    )
                assert ei.value.oldest_kicad_with_it == oldest


@pytest.mark.kicad
@pytest.mark.kicad_version("7.0")
@pytest.mark.usefixtures("user_config_untouched")
def test_flag_missing_on_7_is_unsupported_not_passed(cli_envs, tmp_path: Path):
    env = env_for(cli_envs, "7.0")
    board = tmp_path / "hb.kicad_pcb"
    shutil.copy(spike_board("7.0", "test_inplace.kicad_pcb"), board)
    locks = env.config.count("lock")
    with pytest.raises(Unsupported) as ei:
        export.export_pcb(
            env.install, env.config, board, "pos", tmp_path / "pos", caps=env.caps, exclude_dnp=True
        )
    assert ei.value.capability.endswith(":--exclude-dnp")
    assert env.config.count("lock") == locks, "kicad-cli must not have been started"
