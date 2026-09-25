"""scripts/e2e_consent.py helpers (no KiCad, no Claude Code): the verify-approve pin check.

Review finding 11: when installs share a version but write different formats (a bench build next to
the stock nightly, D24), `kw_project_upgrade` pins the install ROOT (`project._install_spec`, the same
spec `upgrade_args.to_kicad` carries); verify-approve compared the pin with the version only and
reported a false FAIL after a correct approved upgrade.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType

REPO = Path(__file__).resolve().parents[1]


def _e2e() -> ModuleType:
    path = REPO / "scripts" / "e2e_consent.py"
    spec = importlib.util.spec_from_file_location("e2e_consent_under_test", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _manifest(e2e: ModuleType, *, root: str, to_kicad: str) -> object:
    return e2e.Manifest(
        created_utc="20260925T000000Z",
        run_dir="run",
        project_dir="p",
        storage_home="h",
        source={"version": "10.0.6", "root": "C:/Program Files/KiCad/10.0"},
        target={"version": "10.99.0", "root": root},
        files={},
        stamps={},
        snapshot_head=None,
        state_sha256=None,
        upgrade_args={"to_kicad": to_kicad},
        expected_files=[],
        to_format={"sch": 20260830, "pcb": 20260901},
    )


def test_the_pin_may_be_the_version_or_the_install_root():
    e2e = _e2e()
    root = "C:/kiwrap-builds/10.99-0cdfa41cb6"
    m = _manifest(e2e, root=root, to_kicad=root)
    accepted = e2e.pin_forms(m)
    assert accepted == ["10.99.0", root]
    assert e2e.same_spec("10.99.0", accepted)
    assert e2e.same_spec(root, accepted)
    if os.name == "nt":  # separators and case normalised on Windows
        assert e2e.same_spec(root.replace("/", "\\").upper(), accepted)
    assert not e2e.same_spec("10.0.6", accepted)
    assert not e2e.same_spec("C:/Program Files/KiCad/10.99", accepted)
    assert not e2e.same_spec(None, accepted) and not e2e.same_spec("", accepted)
