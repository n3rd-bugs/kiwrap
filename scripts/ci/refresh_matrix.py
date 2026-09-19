"""Promote live probe results into kiwrap/data/kicad_version_matrix.json.

For every discovered install whose full semver is missing from the matrix, or present with
documentary provenance, this writes the probed `cli.*`, `swig.available`, `ipc.available` and
`ipc.headless` values as an entry inheriting from the seed the capability layer would have picked,
storing only the keys that differ from that seed, and marks them `verified`. Nightlies
(`<major>.99.x`) record their build id in the entry note.

Usage:  uv run python scripts/ci/refresh_matrix.py [--dry-run] [--only 10.0.6 ...]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from kiwrap.capability import MATRIX_RESOURCE, capabilities_for, load_matrix, seed_for
from kiwrap.installation import discover
from kiwrap.platform import current

MATRIX_PATH = Path(__file__).resolve().parents[2] / "src" / "kiwrap" / "data" / MATRIX_RESOURCE
PROMOTED_PREFIXES = ("cli.",)
PROMOTED_KEYS = ("swig.available", "ipc.available", "ipc.headless")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", nargs="*", default=None, help="full semvers to refresh (default: all installed)")
    args = ap.parse_args(argv)

    raw = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))
    matrix = load_matrix()
    storage = current().storage_home
    changed: list[str] = []
    for inst in discover():
        if args.only and inst.version not in args.only:
            continue
        caps = capabilities_for(inst, refresh=True, storage_home=storage)
        if not caps.get("probe.ok"):
            print(f"{inst.version}: probe failed, skipped", file=sys.stderr)
            continue
        seed_version, seed_values, exact, note = seed_for(inst.version, matrix)
        entry = raw["versions"].get(inst.version)
        if entry is None or entry.get("inherits") != seed_version and not exact:
            base = seed_version if not exact else entry.get("inherits", seed_version)
            entry = {"inherits": base, "note": "", "values": {}}
        base = entry.get("inherits")
        base_values = matrix.versions.get(base, {}) if base else {}
        values = dict(entry.get("values", {}))
        prov = raw.setdefault("provenance", {}).setdefault(inst.version, {})
        touched = 0
        for key, value in caps.items():
            if not (key.startswith(PROMOTED_PREFIXES) or key in PROMOTED_KEYS):
                continue
            if base_values.get(key) == value:
                if key in values:
                    del values[key]
                    touched += 1
                continue
            if values.get(key) != value:
                values[key] = value
                touched += 1
            prov[key] = "verified"
        for key in list(prov):
            if key.startswith(PROMOTED_PREFIXES) or key in PROMOTED_KEYS:
                prov[key] = "verified"
        build = inst.extra.get("version_raw", "")
        note = f"probed live on {current().name} {inst.version}"
        if build and build != inst.version:
            note += f" ({build})"
        if inst.major_minor.endswith(".99"):
            note += " — nightly; re-run refresh_matrix.py after every nightly bump"
        entry["note"] = note
        entry["values"] = values
        raw["versions"][inst.version] = entry
        changed.append(f"{inst.version}: {touched} keys differ from seed {base}")
    if not args.dry_run and changed:
        raw["versions"] = dict(sorted(raw["versions"].items(), key=lambda kv: tuple(int(p) for p in kv[0].split("."))))
        MATRIX_PATH.write_text(json.dumps(raw, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print("\n".join(changed) or "nothing to refresh")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
