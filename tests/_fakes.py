"""Test doubles and live fixtures for the kicad-cli backend tests.

What is still a double (pure unit tests, no KiCad needed):
- `FakeConfigRoot` seeds a throwaway `KICAD_CONFIG_HOME` with the recipe verified in
  docs/VERIFICATION.md claim 10: files INSIDE `<root>/<major.minor>/`: `kicad_common.json` with
  both `do_not_show_again` prompt flags true and `api.enable_server` false, plus `sym-lib-table`,
  `fp-lib-table` and `design-block-lib-table` copied from `<install>/share/kicad/template` (the
  design-block table is synthesised when the template lacks it). It records every `lock()` /
  `env()` / `restore_seed()` call so tests can assert the runner's contract. `tests/test_cli_runner.py`
  uses it with a Python script standing in for kicad-cli; `conformance/tests/t6_cli_quirks.py` keeps
  it as an independent oracle next to the real sandbox.
- `fake_capabilities()` builds a `Capabilities` by hand from docs/ARCHITECTURE.md §2 plus the
  per-version subcommand/flag lists preserved in spikes/versioning-strategy (cli_flag_drift.txt,
  clihelp/). T6 compares it against the shipped matrix + live probe.

What is real (integration fixtures for `tests/test_cli_*.py`):
- `cli_installs` comes from `kiwrap.installation.discover`.
- `cli_envs` builds one REAL `kiwrap.sandbox.ensure` root (wrapped in `RecordingConfigRoot`, which
  only adds call recording) and one REAL `kiwrap.capability.capabilities_for` record per installed
  major.minor, all under the session scratch dir so nothing lands in the real storage home.
- `user_config_untouched` hashes the user's real KiCad config before and after each test.

Version numbers appear here on purpose: this is the hand-written matrix the tests are keyed on.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from kiwrap.capability import Capabilities
from kiwrap.installation import KiCadInstallation
from kiwrap.sandbox import ConfigRoot

REPO = Path(__file__).resolve().parents[1]
SPIKES = REPO / "spikes"
CLIHELP_DIR = SPIKES / "versioning-strategy" / "clihelp"
FLAG_DRIFT = SPIKES / "versioning-strategy" / "cli_flag_drift.txt"
HEADLESS_BOARDS = SPIKES / "verify-headless-board"
ERC_UNITS_SPIKE = SPIKES / "verify-erc-json-units"

SEED_TABLES = ("sym-lib-table", "fp-lib-table", "design-block-lib-table")
_TABLE_HEAD = {
    "sym-lib-table": "sym_lib_table",
    "fp-lib-table": "fp_lib_table",
    "design-block-lib-table": "design_block_lib_table",
}
_LONG_FLAG_RE = re.compile(r"(?<![\w-])(--[A-Za-z][\w-]*)")


# --------------------------------------------------------------------------------------------
# Sandbox double
# --------------------------------------------------------------------------------------------


def seed_common_json() -> dict[str, Any]:
    """The minimal `kicad_common.json` that keeps kicad-cli/GUI prompt-free (claim 10)."""
    return {
        "api": {"enable_server": False},
        "do_not_show_again": {"update_check_prompt": True, "data_collection_prompt": True},
        "system": {"first_run_shown": True},
        "meta": {"filename": "kicad_common.json"},
    }


def seed_table_text(install: KiCadInstallation, name: str) -> str:
    template = install.dir("template") / name
    if template.is_file():
        return template.read_text(encoding="utf-8")
    return f"({_TABLE_HEAD[name]}\n  (version 7)\n)\n"


@dataclass(frozen=True)
class FakeConfigRoot(ConfigRoot):
    """`ConfigRoot` whose env/lock/restore_seed are self-contained and spied on."""

    calls: list[str] = field(default_factory=list)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False, compare=False)

    @classmethod
    def create(cls, install: KiCadInstallation, base: Path) -> FakeConfigRoot:
        base = Path(base)
        root = cls(
            install=install,
            path=base / "config",
            documents_home=base / "documents",
            cache_home=base / "cache",
        )
        root.versioned.mkdir(parents=True, exist_ok=True)
        root.documents_home.mkdir(parents=True, exist_ok=True)
        root.cache_home.mkdir(parents=True, exist_ok=True)
        root.seed_dir.mkdir(parents=True, exist_ok=True)
        files = {"kicad_common.json": json.dumps(seed_common_json(), indent=2)}
        for name in SEED_TABLES:
            files[name] = seed_table_text(install, name)
        for name, text in files.items():
            (root.seed_dir / name).write_text(text, encoding="utf-8")
        root.restore_seed()
        root.calls.clear()
        return root

    def env(self, base: dict[str, str] | None = None) -> dict[str, str]:
        self.calls.append("env")
        env = dict(os.environ if base is None else base)
        env["KICAD_CONFIG_HOME"] = str(self.path)
        env["KICAD_DOCUMENTS_HOME"] = str(self.documents_home)
        env["KICAD_CACHE_HOME"] = str(self.cache_home)
        n = self.install.major
        env[f"KICAD{n}_SYMBOL_DIR"] = str(self.install.dir("symbols"))
        env[f"KICAD{n}_FOOTPRINT_DIR"] = str(self.install.dir("footprints"))
        env[f"KICAD{n}_3DMODEL_DIR"] = str(self.install.dir("3dmodels"))
        env[f"KICAD{n}_TEMPLATE_DIR"] = str(self.install.dir("template"))
        env[f"KICAD{n}_3RD_PARTY"] = str(self.documents_home / self.install.major_minor / "3rdparty")
        tmp = self.path.parent / "tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        env["TEMP"] = env["TMP"] = str(tmp)
        return env

    @contextmanager
    def lock(self, timeout_s: float = 300) -> Iterator[None]:
        self.calls.append("lock")
        with self._lock:
            yield

    def restore_seed(self) -> None:
        self.calls.append("restore_seed")
        for src in self.seed_dir.iterdir():
            shutil.copyfile(src, self.versioned / src.name)

    def count(self, name: str) -> int:
        return self.calls.count(name)


@dataclass(frozen=True)
class RecordingConfigRoot(ConfigRoot):
    """The REAL sandbox root (seeded and pre-warmed by `kiwrap.sandbox.ensure`) with `env()`,
    `lock()` and `restore_seed()` delegated to `ConfigRoot` and recorded, so the live adapter tests
    can still assert the runner's lock -> env -> restore_seed contract."""

    calls: list[str] = field(default_factory=list)

    @classmethod
    def wrap(cls, root: ConfigRoot) -> RecordingConfigRoot:
        return cls(
            install=root.install,
            path=root.path,
            documents_home=root.documents_home,
            cache_home=root.cache_home,
            user_env_vars=root.user_env_vars,
            user_documents_home=root.user_documents_home,
        )

    def env(self, base: dict[str, str] | None = None) -> dict[str, str]:
        self.calls.append("env")
        return super().env(base)

    @contextmanager
    def lock(self, timeout_s: float = 300) -> Iterator[None]:
        self.calls.append("lock")
        with super().lock(timeout_s):
            yield

    def restore_seed(self) -> None:
        self.calls.append("restore_seed")
        super().restore_seed()

    def count(self, name: str) -> int:
        return self.calls.count(name)


# --------------------------------------------------------------------------------------------
# Capability double: docs/ARCHITECTURE.md §2 by hand + spike-preserved subcommand/flag lists
# --------------------------------------------------------------------------------------------

_MATRIX: dict[str, dict[str, Any]] = {
    "7.0": {
        "cli.has_erc": False,
        "cli.has_drc": False,
        "cli.has_render": False,
        "cli.has_jobset": False,
        "cli.has_sch_pcb_upgrade": False,
        "cli.drc_refill_flags": False,
        "cli.erc_json_pos_scale": 1,
        "cli.gerbers_creates_outdir": False,
        "cli.writes_prl_on_read": False,
        "cli.help_exit_code": 1,
    },
    "8.0": {
        "cli.has_erc": True,
        "cli.has_drc": True,
        "cli.has_render": False,
        "cli.has_jobset": False,
        "cli.has_sch_pcb_upgrade": False,
        "cli.drc_refill_flags": False,
        "cli.erc_json_pos_scale": 100,
        "cli.gerbers_creates_outdir": False,
        "cli.writes_prl_on_read": False,
        "cli.help_exit_code": 1,
    },
    "9.0": {
        "cli.has_erc": True,
        "cli.has_drc": True,
        "cli.has_render": True,
        "cli.has_jobset": True,
        "cli.has_sch_pcb_upgrade": False,
        "cli.drc_refill_flags": False,
        "cli.erc_json_pos_scale": 100,
        "cli.gerbers_creates_outdir": True,
        "cli.writes_prl_on_read": False,
        "cli.help_exit_code": 0,
    },
    "10.0": {
        "cli.has_erc": True,
        "cli.has_drc": True,
        "cli.has_render": True,
        "cli.has_jobset": True,
        "cli.has_sch_pcb_upgrade": True,
        "cli.drc_refill_flags": True,
        "cli.erc_json_pos_scale": 100,
        "cli.gerbers_creates_outdir": True,
        "cli.writes_prl_on_read": True,
        "cli.help_exit_code": 0,
    },
}


def _subcommands_from_drift(major_minor: str) -> list[str]:
    """`== pcb export odb: available in ['9.0', '10.0']` lines -> subcommands present on major_minor."""
    out: list[str] = []
    if not FLAG_DRIFT.is_file():
        return out
    for line in FLAG_DRIFT.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^== (.+?): available in \[(.*)\]", line)
        if m and f"'{major_minor}'" in m.group(2):
            out.append(m.group(1))
    if major_minor == "10.0":
        # sch/pcb upgrade were not captured by the drift script; verified by hand (this component).
        out += ["sch upgrade", "pcb upgrade"]
    return out


def _flags_from_clihelp(major_minor: str, subcommand: str) -> list[str]:
    f = CLIHELP_DIR / f"{major_minor}__{subcommand.replace(' ', '_')}.txt"
    if not f.is_file():
        return []
    flags: list[str] = []
    for flag in _LONG_FLAG_RE.findall(f.read_text(encoding="utf-8", errors="replace")):
        if flag not in flags and flag != "--help":
            flags.append(flag)
    return flags


def fake_capabilities(install: KiCadInstallation) -> Capabilities:
    mm = install.major_minor
    if mm not in _MATRIX:
        pytest.skip(f"no hand-built capability record for KiCad {mm}")
    values: dict[str, Any] = dict(_MATRIX[mm])
    subs = _subcommands_from_drift(mm)
    values["cli.subcommands"] = subs
    for sub in subs:
        flags = _flags_from_clihelp(mm, sub)
        if flags:
            values[f"cli.flags.{sub}"] = flags
    if mm == "10.0":
        values["cli.flags.sch upgrade"] = ["--force"]
        values["cli.flags.pcb upgrade"] = ["--force"]
    return Capabilities(install, values, probed=False)


# --------------------------------------------------------------------------------------------
# Installs: the real discovery, probing under `scratch` so nothing touches the real storage home
# --------------------------------------------------------------------------------------------


def discover_for_tests(scratch: Path) -> list[KiCadInstallation]:
    """`kiwrap.installation.discover` with its `kicad-cli version` probes sandboxed under `scratch`."""
    from kiwrap.installation import discover

    return discover(storage_home=scratch)


# --------------------------------------------------------------------------------------------
# pytest fixtures (import them into a test module: `from tests._fakes import *`)
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class CliEnv:
    install: KiCadInstallation
    config: RecordingConfigRoot
    caps: Capabilities


@pytest.fixture(scope="session")
def cli_scratch(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("cli")


@pytest.fixture(scope="session")
def cli_installs(cli_scratch: Path) -> list[KiCadInstallation]:
    found = discover_for_tests(cli_scratch)
    if not found:
        pytest.skip("no KiCad installation found")
    return found


@pytest.fixture(scope="session")
def cli_envs(cli_installs: list[KiCadInstallation], storage_home: Path) -> dict[str, CliEnv]:
    """One REAL pre-warmed sandbox root + REAL capability record per installed major.minor.

    `sandbox.ensure` seeds and pre-warms the root (8.0.1 / 9.0.5 segfaulted on the very first ERC
    against a brand-new root, claim 1 side observation) and `capabilities_for` seeds from the
    matrix and probes the `--help` tree once per session (cached under the session `storage_home`,
    which is also the session's `KIWRAP_HOME`).
    """
    from kiwrap import sandbox
    from kiwrap.capability import capabilities_for

    envs: dict[str, CliEnv] = {}
    for install in cli_installs:
        if install.major_minor in envs or install.major_minor not in _MATRIX:
            continue
        config = RecordingConfigRoot.wrap(sandbox.ensure(install, storage_home=storage_home))
        caps = capabilities_for(install, storage_home=storage_home)
        envs[install.major_minor] = CliEnv(install=install, config=config, caps=caps)
    return envs


def env_for(cli_envs: dict[str, CliEnv], major_minor: str) -> CliEnv:
    if major_minor not in cli_envs:
        pytest.skip(f"KiCad {major_minor} not installed")
    return cli_envs[major_minor]


def user_kicad_config_dir() -> Path:
    """The user's real KiCad config root, from the platform seam only (no %APPDATA% fallback)."""
    from kiwrap.platform import current

    return current().user_kicad_config_home


def user_config_digest() -> str:
    """`kiwrap.sandbox.tree_hash` (relative path + size + CONTENT) of the user's real KiCad config:
    the one hasher T6, this fixture and scripts/ci/user_config_hash.py share, so "user config
    unchanged" is a single claim (a same-size in-place rewrite is caught; mtime alone is not)."""
    from kiwrap.sandbox import tree_hash

    return tree_hash(user_kicad_config_dir())


@pytest.fixture
def user_config_untouched() -> Iterator[None]:
    before = user_config_digest()
    yield
    assert user_config_digest() == before, "a kicad-cli run modified the user's real KiCad config"


def demo_project(install: KiCadInstallation, tmp: Path, name: str = "ecc83") -> Path:
    """Copy `<install>/share/kicad/demos/<name>` into `tmp` and return the copy (never the original)."""
    src = install.dir("demos") / name
    if not src.is_dir():
        pytest.skip(f"demo {name} not shipped with KiCad {install.version}")
    dst = tmp / name
    shutil.copytree(src, dst)
    return dst


def spike_board(major_minor: str, name: str = "test_inplace.kicad_pcb") -> Path:
    """A small board built headlessly by that version's bundled python (spikes/verify-headless-board)."""
    p = HEADLESS_BOARDS / f"k{major_minor}" / name
    if not p.is_file():
        pytest.skip(f"spike board {p} missing")
    return p


__all__ = [
    "CliEnv",
    "FakeConfigRoot",
    "RecordingConfigRoot",
    "cli_envs",
    "cli_installs",
    "cli_scratch",
    "demo_project",
    "env_for",
    "fake_capabilities",
    "spike_board",
    "user_config_digest",
    "user_config_untouched",
]
