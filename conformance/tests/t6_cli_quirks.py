"""T6 quirk probes for kicad-cli (docs/ARCHITECTURE.md §6 certification, §2 matrix).

Each probe runs kicad-cli RAW (through the sandbox env, but without the adapters' normalisation)
and asserts that what the binary does matches the capability record the adapters branch on, so a
new KiCad release that changes a quirk fails here before it can silently break a tool:

- `cli.help_exit_code`         leaf `--help` exits 1 on 7/8 and 0 on 9/10
- `cli.gerbers_creates_outdir` `pcb export gerbers -o <missing dir>/` creates it (9/10) or fails (7/8)
- `cli.writes_prl_on_read`     a read-only export next to a `.kicad_pro` leaves a `.kicad_prl` (10.0)
- `cli.erc_json_pos_scale`     ERC JSON positions / text-report positions == 1/scale (claim 1)
- `cli.has_erc` / `cli.has_drc` / `cli.has_render` / `cli.has_sch_pcb_upgrade` /
  `cli.drc_refill_flags`       the subcommand or flag is (not) listed by `--help`
- load failure of a newer-format board exits 3 or -1/0xFFFFFFFF (claim 9)
- the adapters undo every one of the above (out dir created, `.kicad_prl` removed, positions in mm)
- the user's real KiCad config is byte-identical before and after.

When `kiwrap.capability.capabilities_for` is implemented the probes also compare against the real
(seeded + probed) record, so the shipped matrix is certified, not just the hand-built one.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from tests._fakes import (
    ERC_UNITS_SPIKE,
    CliEnv,
    FakeConfigRoot,
    demo_project,
    discover_for_tests,
    fake_capabilities,
    spike_board,
    user_config_digest,
)

from kiwrap.backends.cli import drc, erc, export
from kiwrap.backends.cli.runner import LOAD_FAILURE_CODES, run_kicad_cli
from kiwrap.capability import Capabilities

pytestmark = [pytest.mark.kicad, pytest.mark.slow]


@pytest.fixture(scope="module")
def envs(tmp_path_factory: pytest.TempPathFactory) -> dict[str, CliEnv]:
    scratch = tmp_path_factory.mktemp("t6cli")
    out: dict[str, CliEnv] = {}
    for install in discover_for_tests(scratch):
        if install.major_minor in out:
            continue
        try:
            caps = fake_capabilities(install)
        except pytest.skip.Exception:
            continue
        config = FakeConfigRoot.create(install, scratch / install.major_minor)
        subprocess.run(
            [str(install.kicad_cli), "version"], env=config.env(), capture_output=True, timeout=120
        )
        config.restore_seed()
        config.calls.clear()
        out[install.major_minor] = CliEnv(install=install, config=config, caps=caps)
    if not out:
        pytest.skip("no KiCad installation found")
    return out


@pytest.fixture(autouse=True)
def _user_config_untouched() -> Iterator[None]:
    before = user_config_digest()
    yield
    assert user_config_digest() == before, "a probe modified the user's real KiCad config"


def _records(env: CliEnv) -> list[Capabilities]:
    """The hand-built record plus the real one when the capability component is implemented."""
    records = [env.caps]
    try:
        from kiwrap.capability import capabilities_for

        records.append(capabilities_for(env.install))
    except (NotImplementedError, ImportError, OSError, ValueError):
        pass
    return records


def _raw(env: CliEnv, args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    with env.config.lock():
        try:
            return subprocess.run(
                [str(env.install.kicad_cli), *args],
                env=env.config.env(),
                cwd=str(cwd) if cwd else None,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=300,
            )
        finally:
            env.config.restore_seed()


def _versions(envs: dict[str, CliEnv]) -> list[str]:
    return sorted(envs, key=lambda v: tuple(int(p) for p in v.split(".")))


# --------------------------------------------------------------------------------------------


def test_t6_help_exit_code(envs):
    for mm, env in envs.items():
        rc = _raw(env, ["pcb", "export", "svg", "--help"]).returncode
        for caps in _records(env):
            assert rc == caps["cli.help_exit_code"], (
                f"{mm}: --help exited {rc}, record says {caps['cli.help_exit_code']}"
            )


def test_t6_subcommand_presence_matches_record(envs):
    checks = {
        "cli.has_erc": ["sch", "erc"],
        "cli.has_drc": ["pcb", "drc"],
        "cli.has_render": ["pcb", "render"],
        "cli.has_sch_pcb_upgrade": ["pcb", "upgrade"],
    }
    for mm, env in envs.items():
        for key, sub in checks.items():
            proc = _raw(env, [*sub, "--help"])
            usage = proc.stdout + proc.stderr
            present = bool(re.search(rf"Usage: (?:kicad-cli )?(?:{sub[0]} )?{sub[1]}\b", usage))
            for caps in _records(env):
                assert present == bool(caps.get(key)), (
                    f"{mm}: {' '.join(sub)} present={present}, record {key}={caps.get(key)}"
                )
        # drc --refill-zones / --save-board
        proc = _raw(env, ["pcb", "drc", "--help"])
        has_refill = "--refill-zones" in proc.stdout and "--save-board" in proc.stdout
        for caps in _records(env):
            assert has_refill == bool(caps.get("cli.drc_refill_flags")), f"{mm}: refill flags {has_refill}"


def test_t6_gerbers_outdir_creation(envs, tmp_path: Path):
    for mm, env in envs.items():
        board = tmp_path / mm / "hb.kicad_pcb"
        board.parent.mkdir(parents=True)
        shutil.copy(spike_board(mm, "test_inplace.kicad_pcb"), board)
        missing = tmp_path / mm / "does" / "not" / "exist"
        proc = _raw(
            env, ["pcb", "export", "gerbers", "--layers", "F.Cu", "-o", str(missing) + "/", str(board)]
        )
        created = missing.is_dir() and any(missing.iterdir())
        for caps in _records(env):
            assert created == bool(caps["cli.gerbers_creates_outdir"]), (
                f"{mm}: raw gerbers created dir={created} rc={proc.returncode}"
            )
        # the adapter always delivers, whatever the version does
        files = export.export_pcb(
            env.install,
            env.config,
            board,
            "gerbers",
            tmp_path / mm / "adapter" / "g",
            caps=env.caps,
            layers=["F.Cu"],
        )
        assert files and files[0].parent.is_dir()


def test_t6_prl_side_file(envs, tmp_path: Path):
    for mm, env in envs.items():
        proj = demo_project(env.install, tmp_path / mm)
        board = proj / "ecc83-pp.kicad_pcb"
        prl = board.with_suffix(".kicad_prl")
        assert not prl.exists()
        proc = _raw(env, ["pcb", "export", "pos", "-o", str(tmp_path / mm / "raw.pos"), str(board)])
        assert proc.returncode == 0, proc.stderr
        wrote = prl.exists()
        prl.unlink(missing_ok=True)
        for caps in _records(env):
            assert wrote == bool(caps["cli.writes_prl_on_read"]), (
                f"{mm}: raw pos export wrote .kicad_prl={wrote}"
            )
        export.export_pcb(env.install, env.config, board, "pos", tmp_path / mm / "adapter", caps=env.caps)
        assert not prl.exists(), f"{mm}: adapter left a .kicad_prl behind"


def test_t6_erc_json_position_scale(envs, tmp_path: Path):
    ran = 0
    for mm, env in envs.items():
        if not env.caps.has("cli.has_erc"):
            continue
        d = tmp_path / mm
        d.mkdir()
        sch = d / "t.kicad_sch"
        shutil.copy(ERC_UNITS_SPIKE / "t.kicad_sch", sch)
        _raw(env, ["sch", "erc", "--format", "json", "--severity-all", "-o", str(d / "erc.json"), str(sch)])
        _raw(env, ["sch", "erc", "--format", "report", "--severity-all", "-o", str(d / "erc.rpt"), str(sch)])
        doc = json.loads((d / "erc.json").read_text(encoding="utf-8"))
        json_x = [it["pos"]["x"] for s in doc["sheets"] for v in s["violations"] for it in v["items"]]
        rpt_x = [
            float(x)
            for x in re.findall(
                r"@\(([\d.]+) mm,", (d / "erc.rpt").read_text(encoding="utf-8", errors="replace")
            )
        ]
        assert json_x and rpt_x, f"{mm}: no positions to compare"
        ratio = rpt_x[0] / json_x[0]
        for caps in _records(env):
            assert ratio == pytest.approx(caps["cli.erc_json_pos_scale"]), f"{mm}: report/json = {ratio}"
        report = erc.run_erc(env.install, env.config, sch, caps=env.caps)
        assert report.violations[0].pos.x_mm == pytest.approx(rpt_x[0]), f"{mm}: adapter did not rescale"
        ran += 1
    if not ran:
        pytest.skip("no KiCad with sch erc installed")


def test_t6_drc_json_positions_are_mm(envs, tmp_path: Path):
    ran = 0
    for mm, env in envs.items():
        if not env.caps.has("cli.has_drc"):
            continue
        d = tmp_path / mm
        d.mkdir()
        board = d / "t.kicad_pcb"
        shutil.copy(ERC_UNITS_SPIKE / "t.kicad_pcb", board)
        report = drc.run_drc(env.install, env.config, board, caps=env.caps)
        dangling = [v for v in report.violations if v.type == "track_dangling"]
        assert dangling and dangling[0].pos.x_mm == pytest.approx(100.33), f"{mm}: DRC JSON no longer in mm"
        ran += 1
    if not ran:
        pytest.skip("no KiCad with pcb drc installed")


def test_t6_newer_board_load_failure_exit_codes(envs, tmp_path: Path):
    """A board written by a newer KiCad fails on every older kicad-cli with one of the codes the
    runner maps to CliLoadError (claim 9: 3, or -1/0xFFFFFFFF where LoadBoard's IO_ERROR is uncaught)."""
    versions = _versions(envs)
    if len(versions) < 2:
        pytest.skip("need two KiCad versions")
    newest = versions[-1]
    board = tmp_path / "newer.kicad_pcb"
    shutil.copy(spike_board(newest, "test_inplace.kicad_pcb"), board)
    for mm in versions[:-1]:
        env = envs[mm]
        proc = _raw(
            env, ["pcb", "export", "svg", "--layers", "F.Cu", "-o", str(tmp_path / f"{mm}.svg"), str(board)]
        )
        assert proc.returncode != 0, f"{mm}: loaded a {newest} board"
        assert proc.returncode in LOAD_FAILURE_CODES, f"{mm}: unexpected exit code {proc.returncode}"


def test_t6_runner_restores_seed_after_rewrite(envs):
    """kicad-cli rewrites kicad_common.json on exit; after run_kicad_cli the seed is back."""
    for mm, env in envs.items():
        common = env.config.versioned / "kicad_common.json"
        seed = (env.config.seed_dir / "kicad_common.json").read_bytes()
        n = env.config.count("restore_seed")
        run_kicad_cli(env.install, ["version"], config=env.config, caps=env.caps)
        assert env.config.count("restore_seed") == n + 1
        assert common.read_bytes() == seed, f"{mm}: seed not restored"
