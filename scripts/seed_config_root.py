"""Seed, inspect or verify a sandboxed KiCad config root (`kiwrap.sandbox`).

    uv run python scripts/seed_config_root.py seed    --kicad-root "C:/Program Files/KiCad/10.0" [--api] [--no-merge]
    uv run python scripts/seed_config_root.py inspect --kicad-root "C:/Program Files/KiCad/10.0"
    uv run python scripts/seed_config_root.py verify  --kicad-root "C:/Program Files/KiCad/10.0"

`--kicad <spec>` (e.g. `10`, `10.0.0`) goes through `kiwrap.installation.find()` instead of an
explicit root; `--storage <dir>` overrides the storage home (default `Platform.storage_home`,
or `KIWRAP_HOME`). `verify` runs `kicad-cli version` inside the sandbox and checks that the seed
is restorable and that the user's real KiCad config is byte-identical afterwards (the T6
never-modified assertion). Exit status 0 = ok, 1 = a check failed, 2 = usage / install not found.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from kiwrap import sandbox
from kiwrap.installation import KiCadInstallation, NoKiCadError, find
from kiwrap.platform import current
from kiwrap.sandbox import ConfigRoot, SandboxError


def _resolve_install(args: argparse.Namespace) -> KiCadInstallation:
    """`--kicad-root` and `--kicad` both go through `installation.find()`: a root path is probed
    with the platform seam's candidate rules (python / DLL dir), so both flags yield the same record."""
    return find(str(Path(args.kicad_root).resolve()) if args.kicad_root else args.kicad)


def _root_for(install: KiCadInstallation, args: argparse.Namespace) -> ConfigRoot:
    storage = Path(args.storage) if args.storage else current().storage_home
    config_home, documents_home, cache_home = sandbox.root_for(install, storage)
    return ConfigRoot(install=install, path=config_home, documents_home=documents_home, cache_home=cache_home)


def cmd_seed(args: argparse.Namespace) -> int:
    install = _resolve_install(args)
    root = sandbox.ensure(
        install,
        storage_home=Path(args.storage) if args.storage else None,
        merge_user_tables=not args.no_merge,
        enable_api_server=args.api,
        prewarm=not args.no_prewarm,
    )
    print(f"seeded {root.versioned} for KiCad {install.version} ({install.root})")
    print(f"  prewarmed: {root.prewarm_marker.is_file()}  api.enable_server: {args.api}")
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    install = _resolve_install(args)
    root = _root_for(install, args)
    info: dict[str, object] = {
        "install": {
            "root": str(install.root),
            "version": install.version,
            "kicad_cli": str(install.kicad_cli),
        },
        "config_home": str(root.path),
        "versioned": str(root.versioned),
        "documents_home": str(root.documents_home),
        "cache_home": str(root.cache_home),
        "temp_dir": str(root.temp_dir()),
        "exists": root.versioned.is_dir(),
        "seeded": root.seed_dir.is_dir(),
        "seed_intact": root.seed_dir.is_dir() and root.seed_intact(),
        "prewarmed": root.prewarm_marker.is_file(),
        "user_config_hash": sandbox.user_config_hash(install),
    }
    tables: dict[str, object] = {}
    for name in sandbox.SEED_FILES[1:]:
        path = root.versioned / name
        if path.is_file():
            try:
                head, rows = sandbox.parse_lib_table(path.read_text(encoding="utf-8"))
                tables[name] = {"head": head, "rows": len(rows)}
            except SandboxError as e:
                tables[name] = {"error": str(e)}
        else:
            tables[name] = None
    info["tables"] = tables
    common = root.versioned / "kicad_common.json"
    if common.is_file():
        try:
            doc = json.loads(common.read_text(encoding="utf-8"))
            info["kicad_common"] = {
                k: doc.get(k) for k in ("api", "do_not_show_again", "system", "environment")
            }
        except ValueError as e:
            info["kicad_common"] = {"error": str(e)}
    else:
        info["kicad_common"] = None
    env = root.env({})
    info["env"] = {k: v for k, v in env.items() if k.startswith("KICAD") or k in ("TEMP", "TMP")}
    print(json.dumps(info, indent=2))
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    install = _resolve_install(args)
    user_home = current().user_kicad_config_home
    before_all = sandbox.tree_hash(user_home)
    before = sandbox.user_config_hash(install)
    root = sandbox.ensure(install, storage_home=Path(args.storage) if args.storage else None)
    failures: list[str] = []
    seeds = {n: (root.seed_dir / n).read_bytes() for n in sandbox.SEED_FILES}
    with root.lock():
        proc = subprocess.run(
            [str(install.kicad_cli), "version", "--format", "plain"],
            env=root.env(),
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if proc.returncode != 0:
            failures.append(f"kicad-cli version exited {proc.returncode}: {proc.stderr.strip()}")
        elif proc.stdout.strip() != install.version:
            failures.append(f"kicad-cli printed {proc.stdout.strip()!r}, expected {install.version!r}")
        root.restore_seed()
    if {n: (root.versioned / n).read_bytes() for n in sandbox.SEED_FILES} != seeds:
        failures.append("restore_seed() did not restore byte-identical seeds")
    if sandbox.user_config_hash(install) != before:
        failures.append(f"user config {user_home / install.major_minor} changed during the run")
    if sandbox.tree_hash(user_home) != before_all:
        failures.append(f"user config tree {user_home} changed during the run")
    if not root.prewarm_marker.is_file():
        failures.append("root is not marked pre-warmed")
    for f in failures:
        print(f"FAIL: {f}")
    print(f"{'FAIL' if failures else 'OK'}: {root.versioned}")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for name, fn in (("seed", cmd_seed), ("inspect", cmd_inspect), ("verify", cmd_verify)):
        sp = sub.add_parser(name)
        g = sp.add_mutually_exclusive_group()
        g.add_argument("--kicad-root", help="install root, e.g. C:/Program Files/KiCad/10.0")
        g.add_argument("--kicad", help="version spec for kiwrap.installation.find(), e.g. 10 or 10.0.0")
        sp.add_argument(
            "--storage", help="kiwrap storage home (default: Platform.storage_home / KIWRAP_HOME)"
        )
        if name == "seed":
            sp.add_argument("--api", action="store_true", help="seed api.enable_server=true (GUI/IPC roots)")
            sp.add_argument("--no-merge", action="store_true", help="do not merge the user's library tables")
            sp.add_argument("--no-prewarm", action="store_true", help="skip the kicad-cli pre-warm run")
        sp.set_defaults(fn=fn)
    args = parser.parse_args(argv)
    try:
        return args.fn(args)
    except (SandboxError, NoKiCadError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
