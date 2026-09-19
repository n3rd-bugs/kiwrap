"""Fingerprint the user's real KiCad config directory so CI can assert it is never written.

Usage:
    uv run python scripts/ci/user_config_hash.py --write before.txt      # snapshot
    uv run python scripts/ci/user_config_hash.py --verify before.txt     # exit 1 if it changed

The directory is `kiwrap.platform.current().user_kicad_config_home` (%APPDATA%\\kicad on Windows,
$XDG_CONFIG_HOME/kicad or ~/.config/kicad on Linux) - the platform seam is the only place that
knows this rule. A directory that does not exist hashes to "absent" - on a fresh CI runner the
assertion therefore also catches KiCad *creating* the directory. The fingerprint is
`kiwrap.sandbox.tree_hash` (relative path + size + content), the same hasher T6 and the pytest
`user_config_untouched` fixture use, so "user config unchanged" is one claim everywhere.

Why this exists: kicad-cli rewrites `kicad_common.json` on exit and the GUIs rewrite several files,
so any KiCad process that ever sees the user's config root modifies it (docs/ARCHITECTURE.md
section 6, docs/VERIFICATION.md claim 10). The hard rule in CLAUDE.md is that kiwrap never lets
that happen; this script is the CI-side evidence (ARCHITECTURE.md section 11: "CI assertion").
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from kiwrap.platform import current
from kiwrap.sandbox import tree_hash


def user_kicad_config_home() -> Path:
    return current().user_kicad_config_home


def fingerprint(root: Path) -> str:
    """`kiwrap.sandbox.tree_hash` (relative path, size, content) of every file, or "absent"."""
    if not root.is_dir():
        return "absent"
    return tree_hash(root)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, help="override the directory (tests)")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--write", type=Path, help="write the fingerprint to this file")
    group.add_argument("--verify", type=Path, help="compare against the fingerprint in this file")
    args = parser.parse_args(argv)

    root = args.root or user_kicad_config_home()
    fp = fingerprint(root)
    if args.write:
        args.write.write_text(fp + "\n", encoding="utf-8")
        print(f"{root}: {fp} (written to {args.write})")
        return 0
    if args.verify:
        expected = args.verify.read_text(encoding="utf-8").strip()
        if expected != fp:
            print(f"ERROR: {root} changed: before {expected}, after {fp}", file=sys.stderr)
            return 1
        print(f"{root}: unchanged ({fp})")
        return 0
    print(f"{root}: {fp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
