"""T4 — kicad-cli `--help` tree snapshot per installed version (drift canary; ARCHITECTURE §6).

First run for a version writes `conformance/snapshots/cli-help/<version>.json`; later runs
re-probe live (never from the cache) and fail on any change in the command tree, its long flags,
positionals or `--help` exit codes. Descriptions are stored for humans but not compared (they are
translatable strings). Run with `uv run pytest conformance/tests/t4_cli_help_snapshot.py -q`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from kiwrap.backends.cli.flagmap import help_tree

SNAPSHOT_DIR = Path(__file__).resolve().parent.parent / "snapshots" / "cli-help"
SNAPSHOT_SCHEMA = 1


def _structure(node: dict[str, Any]) -> dict[str, Any]:
    """The comparable part of a tree node (no descriptions)."""
    return {
        "flags": list(node["flags"]),
        "positionals": list(node["positionals"]),
        "exit_code": node["exit_code"],
        "subcommands": {name: _structure(child) for name, child in sorted(node["subcommands"].items())},
    }


def _flatten(node: dict[str, Any], prefix: str = "") -> dict[str, dict[str, Any]]:
    out = {prefix: {k: v for k, v in node.items() if k != "subcommands"}}
    for name, child in node["subcommands"].items():
        out.update(_flatten(child, f"{prefix} {name}".strip()))
    return out


def diff_trees(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    a, b = _flatten(_structure(old)), _flatten(_structure(new))
    lines: list[str] = []
    for cmd in sorted(set(a) - set(b)):
        lines.append(f"- command removed: {cmd!r}")
    for cmd in sorted(set(b) - set(a)):
        lines.append(f"+ command added: {cmd!r} flags={b[cmd]['flags']}")
    for cmd in sorted(set(a) & set(b)):
        fa, fb = set(a[cmd]["flags"]), set(b[cmd]["flags"])
        if fa != fb:
            lines.append(f"~ {cmd!r}: flags +{sorted(fb - fa)} -{sorted(fa - fb)}")
        if a[cmd]["positionals"] != b[cmd]["positionals"]:
            lines.append(f"~ {cmd!r}: positionals {a[cmd]['positionals']} -> {b[cmd]['positionals']}")
        if a[cmd]["exit_code"] != b[cmd]["exit_code"]:
            lines.append(f"~ {cmd!r}: --help exit {a[cmd]['exit_code']} -> {b[cmd]['exit_code']}")
    return lines


@pytest.mark.kicad
def test_t4_cli_help_snapshot(installs, storage_home):
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    drift: dict[str, list[str]] = {}
    for install in installs:
        tree = help_tree(install, storage_home=storage_home, refresh=True)
        path = SNAPSHOT_DIR / f"{install.version}.json"
        payload = {"schema": SNAPSHOT_SCHEMA, "version": install.version, "tree": tree}
        if not path.exists():
            path.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n", encoding="utf-8")
            written.append(install.version)
            continue
        old = json.loads(path.read_text(encoding="utf-8"))
        assert old.get("schema") == SNAPSHOT_SCHEMA, (
            f"{path.name}: unknown snapshot schema; delete to re-snapshot"
        )
        lines = diff_trees(old["tree"], tree)
        if lines:
            drift[install.version] = lines
    if drift:
        report = "\n".join(f"KiCad {v}:\n  " + "\n  ".join(lines) for v, lines in drift.items())
        pytest.fail(f"kicad-cli help tree drifted from the snapshot (delete the file to accept):\n{report}")
    if written:
        print(f"wrote first snapshots for {written}")
