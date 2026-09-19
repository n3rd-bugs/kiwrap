"""`kiwrap.backends.cli.runner` — contract tests that need no KiCad (a Python script stands in for
kicad-cli) plus a few live checks of the exit-code mapping on the installed versions."""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

from kiwrap.backends.cli import flagmap, runner
from kiwrap.backends.cli.runner import (
    EXIT_VIOLATIONS,
    LOAD_FAILURE_CODES,
    CliError,
    CliLoadError,
    CliTimeout,
    run_kicad_cli,
)
from kiwrap.capability import Capabilities
from kiwrap.installation import KiCadInstallation
from tests._fakes import (
    CLIHELP_DIR,
    FakeConfigRoot,
    env_for,
    spike_board,
)

# --------------------------------------------------------------------------------------------
# Offline: a fake "kicad-cli" (python -c ...) exercises the runner contract
# --------------------------------------------------------------------------------------------

_FAKE_CLI = textwrap.dedent(
    """
    import sys, time
    from pathlib import Path
    code = int(sys.argv[1])
    for a in sys.argv[2:]:
        if a.startswith("prl="):
            Path(a[4:]).with_suffix(".kicad_prl").write_text("prl")
        elif a.startswith("touch="):
            Path(a[6:]).write_text("x")
        elif a.startswith("sleep="):
            time.sleep(float(a[6:]))
    print("out")
    print("err line", file=sys.stderr)
    sys.exit(code)
    """
)


@pytest.fixture
def fake_install(tmp_path: Path) -> KiCadInstallation:
    return KiCadInstallation(
        root=tmp_path,
        kicad_cli=Path(sys.executable),
        share=tmp_path / "share",
        version="0.0.0",
        source="explicit",
    )


@pytest.fixture
def fake_config(fake_install: KiCadInstallation, tmp_path: Path) -> FakeConfigRoot:
    (tmp_path / "share" / "template").mkdir(parents=True)
    return FakeConfigRoot.create(fake_install, tmp_path / "sandbox")


def _args(code: int, *extra: str) -> list[str]:
    return ["-c", _FAKE_CLI, str(code), *extra]


def test_ok_run_uses_lock_env_and_restores_seed(fake_install, fake_config):
    res = run_kicad_cli(fake_install, _args(0), config=fake_config)
    assert res.ok and not res.violations and res.returncode == 0
    assert "out" in res.stdout and "err line" in res.stderr
    assert res.duration_s >= 0
    assert fake_config.calls == ["lock", "env", "restore_seed"]


def test_exit_5_is_violations_not_failure(fake_install, fake_config):
    res = run_kicad_cli(fake_install, _args(EXIT_VIOLATIONS), config=fake_config)
    assert res.violations and not res.ok


@pytest.mark.parametrize("code", sorted(c for c in LOAD_FAILURE_CODES if 0 <= c < 256))
def test_load_failure_codes_raise_cli_load_error_with_stderr(fake_install, fake_config, code, tmp_path):
    board = tmp_path / "in.kicad_pcb"
    board.write_text("(kicad_pcb)")
    with pytest.raises(CliLoadError) as ei:
        run_kicad_cli(fake_install, _args(code, str(board)), config=fake_config)
    assert ei.value.returncode == code
    assert "err line" in ei.value.stderr
    assert fake_config.count("restore_seed") == 1


def test_return_minus_one_family_needs_an_input_file_or_a_marker(fake_install, fake_config, tmp_path):
    """Finding: 255 is `return -1` truncated to 8 bits on POSIX (Linux 7/8 load failures), but 127 /
    255 are also ordinary exit statuses; without an input file or a load-failure message they are
    plain CliError."""
    assert 255 in LOAD_FAILURE_CODES
    for code in (127, 255):
        with pytest.raises(CliError) as ei:
            run_kicad_cli(fake_install, _args(code), config=fake_config)
        assert not isinstance(ei.value, CliLoadError)
    assert runner.is_load_failure(255, ["kicad-cli", "pcb", "export", "svg"], "Unable to open this file", None)
    assert runner.is_load_failure(3, ["kicad-cli", "x"], "", None)
    assert not runner.is_load_failure(4, ["kicad-cli", "x"], "Unable to open this file", None)
    board = tmp_path / "proj" / "b.kicad_pcb"
    board.parent.mkdir()
    board.write_text("(kicad_pcb)")
    # a relative input file resolves against the process cwd handed to kicad-cli, not Python's
    assert runner.is_load_failure(255, ["kicad-cli", "pcb", "export", "svg", "b.kicad_pcb"], "", board.parent)
    assert not runner.is_load_failure(255, ["kicad-cli", "pcb", "export", "svg", "b.kicad_pcb"], "", None)


def test_other_codes_raise_cli_error(fake_install, fake_config):
    with pytest.raises(CliError) as ei:
        run_kicad_cli(fake_install, _args(4), config=fake_config)
    assert not isinstance(ei.value, CliLoadError)
    assert ei.value.returncode == 4


def test_accept_overrides_mapping(fake_install, fake_config):
    res = run_kicad_cli(fake_install, _args(3), config=fake_config, accept=(3,))
    assert res.returncode == 3


def test_timeout_kills_and_restores_seed(fake_install, fake_config):
    with pytest.raises(CliTimeout):
        run_kicad_cli(fake_install, _args(0, "sleep=30"), config=fake_config, timeout_s=1)
    assert fake_config.count("restore_seed") == 1


def test_prl_created_by_run_is_deleted(fake_install, fake_config, tmp_path):
    board = tmp_path / "b.kicad_pcb"
    board.write_text("(kicad_pcb)")
    res = run_kicad_cli(fake_install, _args(0, f"prl={board}", str(board)), config=fake_config)
    assert res.removed_prl == [board.with_suffix(".kicad_prl")]
    assert not board.with_suffix(".kicad_prl").exists()


def test_preexisting_prl_is_kept(fake_install, fake_config, tmp_path):
    board = tmp_path / "b.kicad_pcb"
    board.write_text("(kicad_pcb)")
    prl = board.with_suffix(".kicad_prl")
    prl.write_text("mine")
    res = run_kicad_cli(fake_install, _args(0, f"prl={board}", str(board)), config=fake_config)
    assert res.removed_prl == []
    assert prl.exists()


def test_created_files_are_tracked_under_watch_dirs(fake_install, fake_config, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "old.txt").write_text("old")
    res = run_kicad_cli(
        fake_install,
        _args(0, f"touch={out / 'new.gbr'}", f"touch={out / 'old.txt'}"),
        config=fake_config,
        watch_dirs=[out],
    )
    assert res.created_files == [out / "new.gbr"]
    assert res.updated_files == [out / "old.txt"]


def test_restore_seed_restores_rewritten_common_json(fake_install, fake_config):
    common = fake_config.versioned / "kicad_common.json"
    seed = common.read_text()
    common.write_text("{}")
    run_kicad_cli(fake_install, _args(0), config=fake_config)
    assert common.read_text() == seed


# --------------------------------------------------------------------------------------------
# --help parser (flagmap-backed) against the preserved help texts
# --------------------------------------------------------------------------------------------


def test_help_parser_flags_and_subcommands():
    """`flagmap.parse_help` is the one parser (the runner's private wrapper is gone)."""
    assert not hasattr(runner, "parse_help")
    text = (CLIHELP_DIR / "10.0__pcb_export.txt").read_text(encoding="utf-8")
    parsed = flagmap.parse_help(text)
    assert "gerbers" in parsed["subcommands"] and "stats" in parsed["subcommands"]
    text7 = (CLIHELP_DIR / "7.0__pcb_export_gerbers.txt").read_text(encoding="utf-8")
    parsed7 = flagmap.parse_help(text7)
    assert parsed7["subcommands"] == []
    assert "--output" in parsed7["flags"] and "--no-protel-ext" in parsed7["flags"]


def test_has_flag_and_has_subcommand_are_optimistic_without_lists(fake_install):
    empty = Capabilities(fake_install, {}, probed=False)
    assert runner.has_flag(empty, "pcb drc", "--refill-zones")
    assert runner.has_subcommand(empty, "pcb export odb")
    listed = Capabilities(
        fake_install, {"cli.subcommands": ["pcb drc"], "cli.flags.pcb drc": ["--format"]}, probed=False
    )
    assert not runner.has_flag(listed, "pcb drc", "--refill-zones")
    assert not runner.has_subcommand(listed, "pcb export odb")


# --------------------------------------------------------------------------------------------
# Live: exit-code mapping and --help exit status on the installed versions (claim 9, matrix)
# --------------------------------------------------------------------------------------------


@pytest.mark.kicad
@pytest.mark.usefixtures("user_config_untouched")
@pytest.mark.parametrize(
    "mm",
    [
        pytest.param("7.0", marks=pytest.mark.kicad_version("7.0")),
        pytest.param("8.0", marks=pytest.mark.kicad_version("8.0")),
        pytest.param("9.0", marks=pytest.mark.kicad_version("9.0")),
        pytest.param("10.0", marks=pytest.mark.kicad_version("10.0")),
    ],
)
def test_help_text_regardless_of_exit_status(cli_envs, mm):
    env = env_for(cli_envs, mm)
    text = runner.help_text(env.install, ["pcb", "export", "svg"], config=env.config, caps=env.caps)
    parsed = flagmap.parse_help(text)
    assert "--layers" in parsed["flags"]
    assert env.config.count("restore_seed") >= 1


@pytest.mark.kicad
@pytest.mark.usefixtures("user_config_untouched")
@pytest.mark.parametrize(
    "mm",
    [
        pytest.param("7.0", marks=pytest.mark.kicad_version("7.0")),
        pytest.param("8.0", marks=pytest.mark.kicad_version("8.0")),
        pytest.param("9.0", marks=pytest.mark.kicad_version("9.0")),
    ],
)
def test_newer_board_raises_cli_load_error(cli_envs, mm, tmp_path):
    """A 10.0-format board fails to load on 7/8/9: exit 3 on 9, -1 (0xFFFFFFFF) on 7/8 (claim 9)."""
    env = env_for(cli_envs, mm)
    board = tmp_path / "newer.kicad_pcb"
    board.write_bytes(spike_board("10.0", "test.kicad_pcb").read_bytes())
    with pytest.raises(CliLoadError) as ei:
        run_kicad_cli(
            env.install,
            ["pcb", "export", "svg", "--layers", "F.Cu", "-o", str(tmp_path / "x.svg"), str(board)],
            config=env.config,
            caps=env.caps,
        )
    assert ei.value.returncode in LOAD_FAILURE_CODES
    assert ei.value.stderr.strip()
