"""Shared fixtures. KiCad-dependent tests auto-skip when no install is discovered."""

from __future__ import annotations

import pytest


def pytest_configure(config):
    config.addinivalue_line("markers", "kicad_version(spec): needs an installed KiCad matching spec")


@pytest.fixture(scope="session")
def installs():
    from kiwrap.installation import discover

    try:
        found = discover()
    except NotImplementedError:  # scaffold state
        found = []
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
def storage_home(tmp_path_factory):
    """A throw-away kiwrap storage home so tests never touch the real one."""
    return tmp_path_factory.mktemp("kiwrap-home")


@pytest.fixture
def config_root(storage_home):
    from kiwrap import sandbox

    def _make(install, **kw):
        return sandbox.ensure(install, storage_home=storage_home, **kw)

    return _make


def pytest_collection_modifyitems(config, items):
    """Apply `kicad` / `kicad_version` skips centrally."""
    try:
        from kiwrap.installation import discover

        found = discover()
    except Exception:
        found = []
    versions = {i.major_minor for i in found}
    for item in items:
        if item.get_closest_marker("kicad") and not found:
            item.add_marker(pytest.mark.skip(reason="no KiCad installed"))
        m = item.get_closest_marker("kicad_version")
        if m and m.args and m.args[0] not in versions:
            item.add_marker(pytest.mark.skip(reason=f"KiCad {m.args[0]} not installed"))
