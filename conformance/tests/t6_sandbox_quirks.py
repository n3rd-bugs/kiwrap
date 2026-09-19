"""T6 quirk probes for the sandboxed config root (docs/ARCHITECTURE.md §6, docs/VERIFICATION.md claim 10).

Each probe pins one behaviour of kicad-cli that `kiwrap.sandbox` compensates for, so a new KiCad
release that changes it fails here first:

- `kicad-cli version` creates `<KICAD_CONFIG_HOME>/<major.minor>/kicad_common.json` on exit but
  never a library table (why the sandbox seeds all three tables itself).
- files placed at the root of KICAD_CONFIG_HOME are ignored; KiCad always appends `<major.minor>/`
  (claim 10 correction (1), negative control `cfg10root`).
- kicad-cli rewrites `kicad_common.json` on exit yet preserves the seeded flags (why runs are
  serialised and `restore_seed()` exists).
- ERC/exports inside the sandbox resolve stock libraries without `lib_symbol_issues` /
  `footprint_link_issues`; against a brand-new empty root the first ERC may crash (8.0.1/9.0.5)
  or report footprint link issues (10.0.0) - recorded, never relied upon.
- the user's real KiCad config tree is byte-identical before and after everything above.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from kiwrap import sandbox
from kiwrap.installation import KiCadInstallation
from kiwrap.platform import Platform, current
from kiwrap.sandbox import ConfigRoot, tree_hash

pytestmark = [pytest.mark.kicad, pytest.mark.slow]


@pytest.fixture(scope="module")
def storage(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("kiwrap-home")


@pytest.fixture(scope="module", autouse=True)
def user_config_guard() -> Iterator[None]:
    """T6 never-modified assertion over the whole user config tree, for the entire module."""
    home = current().user_kicad_config_home
    before = tree_hash(home)
    yield
    assert tree_hash(home) == before, f"{home} was modified by a T6 probe"


def _run(install: KiCadInstallation, args: list[str], env: dict[str, str], timeout: float = 300):
    return subprocess.run(
        [str(install.kicad_cli), *args], env=env, capture_output=True, text=True, timeout=timeout, check=False
    )


def _bare_env(config_home: Path) -> dict[str, str]:
    """A non-sandbox environment for the negative probes (still never the user's config)."""
    env = dict(os.environ)
    env["KICAD_CONFIG_HOME"] = str(config_home)
    return env


def _has_erc(root: ConfigRoot) -> bool:
    with root.lock():
        out = _run(root.install, ["sch", "--help"], root.env(), timeout=60)
        root.restore_seed()
    return "erc" in (out.stdout + out.stderr)


def _violation_types(report: Path) -> list[str]:
    data = json.loads(report.read_text(encoding="utf-8"))
    return [v["type"] for sheet in data.get("sheets", []) for v in sheet.get("violations", [])]


def test_t6_version_creates_common_json_but_never_lib_tables(install: KiCadInstallation, tmp_path: Path):
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    proc = _run(install, ["version", "--format", "plain"], _bare_env(fresh), timeout=120)
    assert proc.returncode == 0, proc.stderr
    versioned = fresh / install.major_minor
    assert (versioned / "kicad_common.json").is_file(), "kicad-cli no longer writes kicad_common.json on exit"
    assert not list(versioned.glob("*-lib-table")), "kicad-cli started creating lib tables; revisit the seed"


def test_t6_files_at_config_root_are_ignored(install: KiCadInstallation, storage: Path, tmp_path: Path):
    """Claim 10 (1): seeds at the root of KICAD_CONFIG_HOME are not read; KiCad appends <major.minor>/."""
    root = sandbox.ensure(install, storage_home=storage)
    misplaced = tmp_path / "misplaced"
    misplaced.mkdir()
    for name in sandbox.SEED_FILES:
        shutil.copyfile(root.seed_dir / name, misplaced / name)
    proc = _run(install, ["version", "--format", "plain"], _bare_env(misplaced), timeout=120)
    assert proc.returncode == 0, proc.stderr
    written = misplaced / install.major_minor / "kicad_common.json"
    assert written.is_file()
    flags = json.loads(written.read_text(encoding="utf-8")).get("do_not_show_again", {})
    assert flags.get("update_check_prompt") is not True, (
        "root-level seed was honoured; layout assumption changed"
    )


def test_t6_kicad_common_rewritten_on_exit_flags_preserved(install: KiCadInstallation, storage: Path):
    root = sandbox.ensure(install, storage_home=storage)
    seed = (root.seed_dir / "kicad_common.json").read_bytes()
    with root.lock():
        proc = _run(install, ["version", "--format", "plain"], root.env(), timeout=120)
        live = root.versioned / "kicad_common.json"
        rewritten = live.read_bytes()
        doc = json.loads(rewritten.decode("utf-8"))
        root.restore_seed()
    assert proc.returncode == 0 and proc.stdout.strip() == install.version, proc.stderr
    assert rewritten != seed, "kicad-cli stopped rewriting kicad_common.json; restore_seed() may be redundant"
    assert doc["do_not_show_again"]["update_check_prompt"] is True
    assert doc["do_not_show_again"]["data_collection_prompt"] is True
    assert live.read_bytes() == seed
    assert root.seed_intact()


def test_t6_prewarm_marker_and_seed_layout(install: KiCadInstallation, storage: Path):
    root = sandbox.ensure(install, storage_home=storage)
    assert root.path == storage / "kicad-config" / sandbox.root_dirname(install)
    assert root.prewarm_marker.read_text(encoding="utf-8").strip() == install.version
    assert root.lock_path.exists()
    for name in sandbox.SEED_FILES:
        assert (root.versioned / name).is_file(), name
        assert (root.seed_dir / name).is_file(), name
    for name in sandbox.SEED_FILES[1:]:
        sandbox.parse_lib_table((root.versioned / name).read_text(encoding="utf-8"))
    common = json.loads((root.versioned / "kicad_common.json").read_text(encoding="utf-8"))
    assert common["api"]["enable_server"] is False
    assert common["system"]["first_run_shown"] is True


def test_t6_library_resolution_in_sandbox(install: KiCadInstallation, storage: Path, tmp_path: Path):
    """ARCHITECTURE §17: 'T6 probe for library resolution in the sandbox'."""
    root = sandbox.ensure(install, storage_home=storage)
    demo_src = install.dir("demos") / "ecc83"
    if not demo_src.is_dir():
        pytest.skip("ecc83 demo not shipped")
    demo = tmp_path / "ecc83"
    shutil.copytree(demo_src, demo)
    sch = demo / "ecc83-pp.kicad_sch"
    out = tmp_path / "out"
    out.mkdir()
    user_before = sandbox.user_config_hash(install)

    if _has_erc(root):
        report = out / "erc.json"
        args = ["sch", "erc", "--format", "json", "-o", str(report), str(sch)]
    else:
        report = out / "netlist.xml"
        args = ["sch", "export", "netlist", "-o", str(report), str(sch)]

    with root.lock():
        proc = _run(install, args, root.env())
        root.restore_seed()
    assert proc.returncode == 0, proc.stderr
    assert report.is_file()
    if report.suffix == ".json":
        types = _violation_types(report)
        assert "lib_symbol_issues" not in types, types
        assert "footprint_link_issues" not in types, types

        # Negative control, recorded only: a brand-new empty root. 8.0.1/9.0.5 crashed on the very
        # first ERC (the reason every sandbox root is pre-warmed); 10.0.0 reports footprint link
        # issues because kicad-cli never creates the tables.
        fresh = tmp_path / "fresh"
        fresh.mkdir()
        fresh_report = out / "erc_fresh.json"
        fresh_proc = _run(
            install, ["sch", "erc", "--format", "json", "-o", str(fresh_report), str(sch)], _bare_env(fresh)
        )
        fresh_types = _violation_types(fresh_report) if fresh_report.is_file() else []
        print(
            f"[T6 {install.version}] fresh-root ERC exit={fresh_proc.returncode} "
            f"lib issues={sum(t in ('lib_symbol_issues', 'footprint_link_issues') for t in fresh_types)}"
        )
    assert root.seed_intact()
    assert sandbox.user_config_hash(install) == user_before


def test_t6_merged_pcm_row_resolves_through_3rd_party_var(
    install: KiCadInstallation, tmp_path: Path, monkeypatch
):
    """D7 end to end: a user fp-lib-table row `${KICAD<major>_3RD_PARTY}/...` (what every
    PCM-installed library looks like) merged into the sandbox must resolve inside kicad-cli, i.e.
    `ConfigRoot.env()` points `KICAD<major>_3RD_PARTY` at the user's real
    `<Documents>/KiCad/<major.minor>/3rdparty`, not at the empty sandbox copy.

    Simulated with a fake user config + fake Documents tree (this machine has no PCM libraries);
    the negative control re-runs the same ERC with the sandbox dir and must report
    `footprint_link_issues`, proving the probe would catch a regression."""
    corpus = Path(__file__).resolve().parents[1] / "corpus" / install.major_minor
    sources = sorted(corpus.glob("led_d*/led_d*.kicad_sch"))
    if not sources:
        pytest.skip(f"no led_d* corpus schematic for {install.major_minor}")
    fp_src = install.dir("footprints") / "Resistor_SMD.pretty" / "R_0603_1608Metric.kicad_mod"
    if not fp_src.is_file():
        pytest.skip("stock Resistor_SMD.pretty not shipped")

    real = current()
    user_cfg = tmp_path / "user-kicad"
    docs = tmp_path / "Documents" / "KiCad"
    fake = Platform(
        name=real.name,
        exe_suffix=real.exe_suffix,
        storage_home=tmp_path / "storage",
        user_kicad_config_home=user_cfg,
        temp_dir=real.temp_dir,
        user_kicad_documents_home=docs,
    )
    monkeypatch.setattr(sandbox, "current", lambda: fake)
    var = f"KICAD{install.major}_3RD_PARTY"
    (user_cfg / install.major_minor).mkdir(parents=True)
    (user_cfg / install.major_minor / "fp-lib-table").write_text(
        "(fp_lib_table\n  (version 7)\n"
        f'  (lib (name "PCMLib")(type "KiCad")(uri "${{{var}}}/footprints/pcm.pretty")(options "")(descr ""))\n'
        ")\n",
        encoding="utf-8",
    )
    pretty = docs / install.major_minor / "3rdparty" / "footprints" / "pcm.pretty"
    pretty.mkdir(parents=True)
    shutil.copy(fp_src, pretty)
    proj = tmp_path / "proj"
    proj.mkdir()
    sch = proj / sources[0].name
    sch.write_text(
        sources[0].read_text(encoding="utf-8").replace("Resistor_SMD:R_0603_1608Metric", "PCMLib:R_0603_1608Metric"),
        encoding="utf-8",
    )
    shutil.copy(sources[0].with_suffix(".kicad_pro"), proj)

    root = sandbox.ensure(install, storage_home=tmp_path / "storage")
    if not _has_erc(root):
        pytest.skip("kicad-cli has no ERC on this version")
    env = root.env()
    assert env[var] == str(pretty.parents[1])
    assert "PCMLib" in (root.versioned / "fp-lib-table").read_text(encoding="utf-8")

    def erc(environment: dict[str, str]) -> list[str]:
        report = proj / "erc.json"
        args = ["sch", "erc", "--format", "json", "--severity-all", "-o", str(report), str(sch)]
        with root.lock():
            proc = _run(install, args, environment)
            root.restore_seed()
        assert report.is_file(), proc.stderr
        return _violation_types(report)

    # Negative control first: with the pre-fix value (the empty sandbox dir) ERC must flag the
    # missing library, otherwise this version's ERC does not check footprint links at all
    # (8.0.1 reports nothing for an unresolvable footprint library) and the probe is moot.
    negative = dict(env, **{var: str(root.sandbox_third_party_dir)})
    if "footprint_link_issues" not in erc(negative):
        pytest.skip("ERC on this version does not report footprint_link_issues; probe not applicable")
    assert "footprint_link_issues" not in erc(env)
    assert root.seed_intact()
