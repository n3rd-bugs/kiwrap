"""T5 — IPC handler snapshot for every install with a headless server (`ipc.headless`).

For each such install (the 10.99 nightly today; 11.0 at GA) every command message of the pinned
kicad-python protos (`kiwrap.backends.ipc.handlers.command_messages`, 129 on f79cebd) is sent as a
minimal instance to `kicad-cli api-server` with a scratch copy of the ecc83 board and, separately,
the ecc83 schematic open. The classified replies (handled / bad_request / unhandled / crash /
timeout / error) are written to `conformance/snapshots/ipc-handlers/<version>.json` on the first
run and compared on later runs: any outcome change fails the test (drift canary; the snapshot is
also D17's authoritative gap list for tier 3).

A kiwrap bench build (`KiCadInstallation.is_bench_build`, docs/UPSTREAM.md §3.3) shares its semver
with the nightly it was built from, so its snapshot is `<version>+<built commit>[.debug].json`
(`KiCadInstallation.snapshot_name`) and is written / compared on its own; the stock-vs-bench diff
(only manifest-listed commands may differ) is `scripts/dev/t5_bench_snapshot.py`.

KiCad 10.x is deliberately NOT covered: it has no headless server, and its handlers can only be
probed from a sandboxed GUI session (Phase 3, `gui` marker). Installs without `ipc.headless` are
skipped with that reason.

Run: `uv run pytest conformance/tests/t5_ipc_handlers.py -q` (about two minutes per install:
stateful commands each start a fresh server).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from kiwrap.backends.ipc import handlers
from kiwrap.backends.ipc.sessions import close_all
from kiwrap.capability import capabilities_for

SNAPSHOT_DIR = Path(__file__).resolve().parent.parent / "snapshots" / "ipc-handlers"
DEMO = "ecc83"


def _demo_copy(install, tmp: Path) -> Path:
    src = install.dir("demos") / DEMO
    if not src.is_dir():
        pytest.skip(f"demo {DEMO} not shipped with KiCad {install.version}")
    dst = tmp / DEMO
    shutil.copytree(src, dst)
    return dst


@pytest.mark.kicad
@pytest.mark.slow
def test_t5_ipc_handler_snapshot(installs, storage_home, config_root, tmp_path):
    headless = [i for i in installs if capabilities_for(i, storage_home=storage_home).has("ipc.headless")]
    if not headless:
        pytest.skip("no installed KiCad has a headless api-server (ipc.headless); 10.x needs a GUI session")
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    drift: list[str] = []
    try:
        for install in headless:
            project = _demo_copy(install, tmp_path / install.root.name)
            snap = handlers.probe(
                install,
                project / "ecc83-pp.kicad_pcb",
                project / "ecc83-pp.kicad_sch",
                config_root=config_root(install),
            )
            assert len(snap.commands) >= 100, "command inventory collapsed: check the kipy proto path"
            # The handlers this backend relies on must be live on every headless install.
            for name in ("Ping", "GetVersion", "GetItems", "GetNets", "GetConnectedItems", "HitTest"):
                assert snap.outcome(name, "pcb") in ("handled", "bad_request"), (name, snap.results.get(name))
            assert snap.outcome("GetSchematicNetlist", "schematic") == "handled"
            assert snap.outcome("GetSchematicHierarchy", "schematic") == "handled"
            path = SNAPSHOT_DIR / f"{install.snapshot_name}.json"
            if path.is_file():
                previous = json.loads(path.read_text(encoding="utf-8"))
                lines = handlers.compare(previous, snap)
                if lines:
                    drift.append(f"KiCad {install.snapshot_name}: " + "; ".join(lines))
                    snap.write(path.with_suffix(".drift.json"))
            else:
                snap.write(path)
    finally:
        close_all()
    assert not drift, "IPC handler drift (delete the snapshot to accept):\n" + "\n".join(drift)


def test_t5_command_inventory_is_stable():
    """The inventory rule itself (no KiCad needed): the commands this backend uses are commands."""
    commands = handlers.command_messages()
    for name in (
        "Ping",
        "GetVersion",
        "OpenDocument",
        "GetItems",
        "GetNets",
        "GetConnectedItems",
        "GetBoundingBox",
        "HitTest",
        "GetSchematicNetlist",
        "PlaceFootprintFromLibrary",
        "PlaceSymbolFromLibrary",
        "LoadAllLibraries",
        "BeginCommit",
        "EndCommit",
        "CreateItems",
        "SaveCopyOfDocument",
    ):
        assert name in commands, name
    for name in ("GetItemsResponse", "ItemHeader", "SaveOptions", "BoardPlotSettings", "DocumentSpecifier"):
        assert name not in commands, name
    assert commands["SaveDocument"].stateful and not commands["GetNets"].stateful
