r"""Session-wide pytest hooks and fixtures for every test tree (`tests/`, `conformance/tests`,
`scripts/ci/tests_ci`).

- Every pytest session runs with `KIWRAP_HOME` pointed at a fresh directory under pytest's own base
  temp, so capability caches, sandbox roots and discovery probes never land in the real storage home
  (`%LOCALAPPDATA%\kiwrap` / `$XDG_DATA_HOME/kiwrap`). The user's real KiCad config
  (`%APPDATA%\kicad`) is never written by kiwrap at all; the live tests assert its hash.
- KiCad discovery runs ONCE per session (`discovered_installs()`), at collection time, and feeds
  the `kicad` / `kicad_version(spec)` marker skips, the session fixtures below and the
  parametrised `install` argument. No test module runs its own discovery or spawns kicad-cli
  while collecting.
- A test that takes an `install` argument is parametrised over every discovered install (ids are
  the versions) and is skipped when none is installed.

Session fixtures (shared by every tree): `installs`, `install10`, `newest`, `storage_home`,
`config_root`, `cli_installs`, `corpus`. The unit-test-only fixtures (`cli_envs`, `cli_scratch`,
`user_config_untouched`) stay in `tests/conftest.py`.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

_DISCOVERED: list[Any] | None = None


def discovered_installs() -> list[Any]:
    """Every KiCad `kiwrap.installation.discover()` finds, newest first, computed once per session.

    Discovery failures are NOT swallowed: a broken `kiwrap.installation` must fail the session
    loudly instead of silently skipping every live test.
    """
    global _DISCOVERED
    if _DISCOVERED is None:
        from kiwrap.installation import discover

        _DISCOVERED = list(discover())
    return _DISCOVERED


@pytest.hookimpl(trylast=True)
def pytest_configure(config):
    config.addinivalue_line("markers", "kicad_version(spec): needs an installed KiCad matching spec")
    # trylast: pytest's tmpdir plugin has registered `config._tmp_path_factory` by now. The base
    # temp is pytest's own (it keeps the last three sessions), so nothing is left in the repo or in
    # the real storage home. Set before collection because `pytest_generate_tests` /
    # `pytest_collection_modifyitems` already run `discover()`, which probes `kicad-cli version`
    # under <storage>/probe-config.
    factory = getattr(config, "_tmp_path_factory", None)
    if factory is not None:
        os.environ["KIWRAP_HOME"] = str(factory.mktemp("kiwrap-home"))


def pytest_generate_tests(metafunc):
    """Parametrise every test taking `install` over the discovered installs (one id per version)."""
    if "install" in metafunc.fixturenames and "install" not in metafunc._arg2fixturedefs:
        found = discovered_installs()
        metafunc.parametrize("install", found, ids=[i.version for i in found], scope="module")


def pytest_collection_modifyitems(config, items):
    """Apply `kicad` / `kicad_version` skips centrally."""
    found = discovered_installs()
    versions = {i.major_minor for i in found}
    for item in items:
        if item.get_closest_marker("kicad") and not found:
            item.add_marker(pytest.mark.skip(reason="no KiCad installed"))
        m = item.get_closest_marker("kicad_version")
        if m and m.args and m.args[0] not in versions:
            item.add_marker(pytest.mark.skip(reason=f"KiCad {m.args[0]} not installed"))


# ---- session fixtures ------------------------------------------------------------------------------


@pytest.fixture(scope="session")
def installs():
    found = discovered_installs()
    if not found:
        pytest.skip("no KiCad installation discovered")
    return found


@pytest.fixture(scope="session")
def install10(installs):
    match = [i for i in installs if i.major_minor == "10.0"]
    if not match:
        pytest.skip("KiCad 10.0 not installed")
    return match[0]


@pytest.fixture(scope="session")
def newest(installs):
    return installs[0]


@pytest.fixture(scope="session")
def storage_home(tmp_path_factory) -> Path:
    """The session's throw-away kiwrap storage home: the directory `pytest_configure` exported as
    `KIWRAP_HOME`, so explicit `storage_home=` callers and default-home code paths share one
    capability cache and one set of sandbox roots (each is probed/seeded once per session)."""
    home = os.environ.get("KIWRAP_HOME")
    return Path(home) if home else tmp_path_factory.mktemp("kiwrap-home")


@pytest.fixture
def config_root(storage_home):
    from kiwrap import sandbox

    def _make(install, **kw):
        return sandbox.ensure(install, storage_home=storage_home, **kw)

    return _make


@pytest.fixture(scope="session")
def cli_installs(installs):
    """The installs the kicad-cli adapter tests run against (same discovery as `installs`)."""
    return installs


@pytest.fixture(scope="session")
def corpus() -> Path:
    """The committed conformance corpus (`conformance/corpus/<major.minor>/...`, byte-exact files)."""
    return Path(__file__).resolve().parent / "conformance" / "corpus"
