"""Print the capability record of every KiCad install found on this machine (CI artifact).

Usage:
    uv run python scripts/ci/collect_capabilities.py [--out FILE] [--require-one] [--no-probe]

Discovery and probing are `kiwrap.installation.discover` and `kiwrap.capability.capabilities_for`
(the only discovery rules in the tree: `kiwrap.platform.windows` / `.linux`); a probe failure on
one install is recorded in that install's `error` and never loses the others.

Hard rule encoded here: every kicad-cli invocation (even `version`) runs with `KICAD_CONFIG_HOME`,
`KICAD_DOCUMENTS_HOME` and `KICAD_CACHE_HOME` pointed at a throw-away directory, because kicad-cli
rewrites `kicad_common.json` on exit (docs/ARCHITECTURE.md section 6, docs/VERIFICATION.md claim 10).
kiwrap's own probes already use `<storage>/probe-config`; the environment set here is a
belt-and-braces net for anything else that might spawn kicad-cli. The CI jobs additionally
snapshot the user's config dir with `user_config_hash.py` before and after.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import platform as _platform
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kiwrap.capability import capabilities_for
from kiwrap.installation import discover


@dataclass
class InstallRecord:
    root: str
    kicad_cli: str
    version: str
    source: str
    share: str | None = None
    python: str | None = None
    capabilities: dict[str, Any] | None = None
    error: str | None = None
    extra: dict[str, str] = field(default_factory=dict)


@dataclass
class Report:
    generated: str
    platform: str
    discovery: str  # always "kiwrap.installation" (kept for artifact compatibility)
    installs: list[InstallRecord]
    notes: list[str] = field(default_factory=list)


def throwaway_env() -> tuple[dict[str, str], tempfile.TemporaryDirectory[str]]:
    """Environment for a kicad-cli probe whose config writes land in a temp dir, never in the user's."""
    tmp = tempfile.TemporaryDirectory(prefix="kiwrap-ci-probe-")
    root = Path(tmp.name)
    env = dict(os.environ)
    env["KICAD_CONFIG_HOME"] = str(root / "config")
    env["KICAD_DOCUMENTS_HOME"] = str(root / "documents")
    env["KICAD_CACHE_HOME"] = str(root / "cache")
    return env, tmp


def discover_with_kiwrap(probe: bool) -> tuple[list[InstallRecord], list[str]]:
    """Every install `kiwrap.installation.discover` finds, with its capability record when `probe`."""
    records: list[InstallRecord] = []
    for inst in discover():
        rec = InstallRecord(
            root=str(inst.root),
            kicad_cli=str(inst.kicad_cli),
            version=inst.version,
            source=inst.source,
            share=str(inst.share),
            python=str(inst.python) if inst.python else None,
            extra=dict(inst.extra),
        )
        if probe:
            try:
                rec.capabilities = capabilities_for(inst).to_json()
            except Exception as exc:  # noqa: BLE001 - a probe failure must not lose the other installs
                rec.error = f"{type(exc).__name__}: {exc}"
        records.append(rec)
    return records, []


def collect(probe: bool = True) -> Report:
    env, tmp = throwaway_env()
    saved = {k: os.environ.get(k) for k in ("KICAD_CONFIG_HOME", "KICAD_DOCUMENTS_HOME", "KICAD_CACHE_HOME")}
    try:
        # kiwrap.installation/capability use their own probe sandbox; this is a belt-and-braces net.
        os.environ.update({k: env[k] for k in saved})
        records, notes = discover_with_kiwrap(probe)
        return Report(
            generated=datetime.now(UTC).isoformat(timespec="seconds"),
            platform=f"{_platform.system()} {_platform.release()} ({_platform.machine()})",
            discovery="kiwrap.installation",
            installs=records,
            notes=notes,
        )
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        tmp.cleanup()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, help="write JSON here as well as to stdout")
    parser.add_argument("--require-one", action="store_true", help="exit 1 when no install was found")
    parser.add_argument("--no-probe", action="store_true", help="skip capability probing (discovery only)")
    args = parser.parse_args(argv)

    report = collect(probe=not args.no_probe)
    text = json.dumps(dataclasses.asdict(report), indent=2)
    print(text)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
    if args.require_one and not report.installs:
        print("error: no KiCad installation found", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
