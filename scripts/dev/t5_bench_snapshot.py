"""T5 handler snapshot of a bench build, written under its own name and diffed against the stock file.

docs/UPSTREAM.md §3.3: a patched build has the SAME semver as the nightly it was built from, so its
T5 snapshot goes to `conformance/snapshots/ipc-handlers/<version>+<commit>.json` (never the stock
`<version>.json`), and stock-vs-bench is a drift check: for the pristine `base/<sha>` build the two
must be EQUAL (the bench acceptance test, §4.2); for a patched build only the manifest's commands may
differ.

Usage (from the kiwrap repo root):

    uv run python scripts/dev/t5_bench_snapshot.py [<install root> ...] [--no-write]

With no root every discovered install whose `extra` carries a `commit` (a registered bench build,
`kiwrap.installation`) is probed. Exit 0 = equal (or only manifest-listed commands differ), 1 = drift.
Each probe takes ~2 minutes (stateful commands each start a fresh api-server); every server is a
sandboxed `kicad-cli api-server` and is stopped by `close_all()`.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from kiwrap import sandbox
from kiwrap.backends.ipc import handlers
from kiwrap.backends.ipc.sessions import close_all
from kiwrap.capability import capabilities_for, load_patch_manifest
from kiwrap.installation import discover
from kiwrap.platform import current

REPO = Path(__file__).resolve().parents[2]
SNAPSHOT_DIR = REPO / "conformance" / "snapshots" / "ipc-handlers"
DEMO = "ecc83"


def main(argv: list[str]) -> int:
    write = "--no-write" not in argv
    roots = [Path(a) for a in argv if not a.startswith("--")]
    installs = discover(roots)
    if roots:
        wanted = {r.resolve() for r in roots}
        installs = [i for i in installs if i.root.resolve() in wanted]
    else:
        installs = [i for i in installs if i.is_bench_build]
    if not installs:
        print("no bench build found (register one: python -m kiwrap.installation --register <root>)")
        return 2

    manifest = load_patch_manifest()
    allowed: set[str] = set()
    for patch in manifest["patches"]:
        allowed.update(patch.get("commands", []))

    scratch = current().temp_dir / "t5-bench"
    drift = 0
    try:
        for install in installs:
            caps = capabilities_for(install)
            if not caps.has("ipc.headless"):
                print(f"{install.root}: no headless api-server (ipc.headless false); skipped")
                continue
            commit = install.extra.get("commit", "")
            # `<version>+<built commit>.json` (KiCadInstallation.snapshot_name) is the canonical
            # (RelWithDebInfo) snapshot of that commit; a Debug build gets `.debug` because wx asserts
            # wedge some handlers there (2026-09-19: SetBoardDesignRules handled -> timeout).
            name = f"{install.snapshot_name}.json" if commit else f"{install.version}.explicit.json"
            demo = install.dir("demos") / DEMO
            if not demo.is_dir():
                print(f"{install.root}: demo {DEMO} not installed under {install.dir('demos')}")
                return 2
            project = scratch / commit / DEMO
            shutil.rmtree(project.parent, ignore_errors=True)
            shutil.copytree(demo, project)
            print(f"probing {install.root} ({install.version} @ {commit or 'no commit'}) ...")
            snap = handlers.probe(
                install,
                project / "ecc83-pp.kicad_pcb",
                project / "ecc83-pp.kicad_sch",
                config_root=sandbox.ensure(install),
            )
            snap.notes.append(
                f"bench build {install.root}: commit {commit}, {install.extra.get('build_type', '?')} build"
            )
            print("  summary:", json.dumps(snap.summary(), sort_keys=True))
            if write:
                print("  written:", snap.write(SNAPSHOT_DIR / name))
            stock = SNAPSHOT_DIR / f"{install.version}.json"
            if not stock.is_file():
                print(f"  no stock snapshot {stock.name} to compare against")
                continue
            lines = handlers.compare(json.loads(stock.read_text(encoding="utf-8")), snap)
            unexpected = [ln for ln in lines if ln.split(" [")[0] not in allowed]
            if not lines:
                print(f"  EQUAL to {stock.name} (every command, both documents)")
            elif not unexpected:
                print(
                    f"  differs from {stock.name} only in manifest-listed commands:\n    "
                    + "\n    ".join(lines)
                )
            else:
                drift += 1
                print(f"  DRIFT vs {stock.name}:\n    " + "\n    ".join(unexpected))
    finally:
        close_all()
        shutil.rmtree(scratch, ignore_errors=True)
    return 1 if drift else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
