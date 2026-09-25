"""Promote live probe results into kiwrap/data/kicad_version_matrix.json.

For every discovered install whose full semver is missing from the matrix, or present with
documentary provenance, this writes the probed `cli.*`, `swig.available`, `ipc.available` and
`ipc.headless` values as an entry inheriting from the seed the capability layer would have picked,
storing only the keys that differ from that seed, and marks them `verified`. Nightlies
(`<major>.99.x`) record their build id in the entry note.

D16 re-probe list (`NIGHTLY_REPROBE_KEYS`): keys the `--help` tree cannot show, which this script
measures by RUNNING kicad-cli on a fixture on every nightly bump. Today that is
`cli.erc_json_pos_scale` (docs/VERIFICATION.md claim 1; upstream fixed the x100 in master 407f6293 /
10.0 6d8e1fea, so the value flips from 100 to 1 on the first nightly built after the fix): the
sandboxed `sch erc` JSON and text report of `ERC_SCALE_FIXTURE` are compared (text / JSON, the
text report being in real units). A re-probe key that could not be measured is left as the matrix
has it - never promoted from the seed and never marked `verified` just because the seed said so.
The measurement goes into the entry note (`<key>=<value> probed <date> ...`), and the note's other
` | `-separated source parts are kept across refreshes.

One install per semver (review finding 12): every 10.99 nightly and bench build reports the same
plain version, and folding them one after another into one entry wrote the fork-only `--help`
flags of the patched bench builds into the stock `10.99.0` record, marked verified, where no later
refresh removed them. So a semver entry is refreshed from exactly one install: bench builds
(`kiwrap-install.json` with a `commit`) and installs whose record lists `kiwrap.patches` are skipped
unless `--root` names them, a second stock install of a semver already refreshed is skipped with a
note, and a promoted key the refreshed install no longer reports (and the seed does not have) is
removed from the entry.

Usage:  uv run python scripts/ci/refresh_matrix.py [--dry-run] [--only 10.0.6 ...] [--root DIR ...]
        [--skip-reprobe]
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from kiwrap.capability import MATRIX_RESOURCE, Capabilities, capabilities_for, load_matrix, seed_for
from kiwrap.installation import KiCadInstallation, discover
from kiwrap.platform import current

REPO = Path(__file__).resolve().parents[2]
MATRIX_PATH = REPO / "src" / "kiwrap" / "data" / MATRIX_RESOURCE
PROMOTED_PREFIXES = ("cli.",)
PROMOTED_KEYS = ("swig.available", "ipc.available", "ipc.headless")
#: The hand-written ERC fixture (a dangling 20.32 mm wire at (100.33, 50.8) mm) T6 uses too.
ERC_SCALE_FIXTURE = REPO / "spikes" / "verify-erc-json-units" / "t.kicad_sch"
#: D16: keys re-measured by running kicad-cli on every nightly bump (not derivable from `--help`).
NIGHTLY_REPROBE_KEYS: tuple[str, ...] = ("cli.erc_json_pos_scale",)
NOTE_SEP = " | "

#: A re-probe returns (value, note part) or None when it could not measure the key.
Reprobe = Callable[[KiCadInstallation, Capabilities], tuple[Any, str] | None]


def reprobe_erc_pos_scale(inst: KiCadInstallation, caps: Capabilities) -> tuple[Any, str] | None:
    """`cli.erc_json_pos_scale` measured with `kiwrap.backends.cli.erc.probe_erc_pos_scale` in a
    sandbox root (kiwrap.sandbox) and a throw-away folder under `Platform.temp_dir`."""
    from kiwrap import sandbox
    from kiwrap.backends.cli.erc import probe_erc_pos_scale

    if not caps.has("cli.has_erc"):
        return None
    config = sandbox.ensure(inst)
    temp_root = current().temp_dir
    temp_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ercscale-", dir=temp_root) as tmp:
        probe = probe_erc_pos_scale(inst, config, ERC_SCALE_FIXTURE, Path(tmp), caps=caps)
    if probe is None:
        return None
    value: Any = int(probe.scale) if float(probe.scale).is_integer() else probe.scale
    part = (
        f"cli.erc_json_pos_scale={value} probed {_dt.date.today().isoformat()} "
        f"(sandboxed sch erc on {ERC_SCALE_FIXTURE.relative_to(REPO).as_posix()}: "
        f"text report / JSON = {probe.ratio:g} over {probe.pairs} coordinates)"
    )
    return value, part


REPROBES: Mapping[str, Reprobe] = {"cli.erc_json_pos_scale": reprobe_erc_pos_scale}


def merge_note(old: str, head: str, parts: Mapping[str, str]) -> str:
    """The new entry note: `head`, then the re-probe `parts`, then every ` | ` part of `old`
    after its head that does not restate a re-probed key (sources recorded by hand survive)."""
    kept = [p for p in old.split(NOTE_SEP)[1:] if p and not any(p.startswith(f"{k}=") for k in parts)]
    return NOTE_SEP.join([head, *parts.values(), *kept])


def promote(
    caps_values: Mapping[str, Any],
    probed: Mapping[str, Any],
    base_values: Mapping[str, Any],
    values: dict[str, Any],
    prov: dict[str, str],
) -> int:
    """Fold one install's record into its matrix entry (`values`/`prov`, updated in place) and
    return how many keys changed. Re-probe keys count only when `probed` measured them."""
    merged = {**caps_values, **probed}
    touched = 0
    for key, value in merged.items():
        if not (key.startswith(PROMOTED_PREFIXES) or key in PROMOTED_KEYS):
            continue
        if key in NIGHTLY_REPROBE_KEYS and key not in probed:
            continue  # the seed's value is not a measurement
        if base_values.get(key) == value:
            if key in values:
                del values[key]
                touched += 1
            if key in probed:
                prov[key] = "verified"
            continue
        if values.get(key) != value:
            values[key] = value
            touched += 1
        prov[key] = "verified"
    for key in list(values):
        # a promoted key this install does not report (a `--help` flag of another build folded in
        # earlier) and the seed does not carry either: stale, never a measurement (finding 12)
        if (
            _promoted(key)
            and key not in merged
            and key not in base_values
            and key not in NIGHTLY_REPROBE_KEYS
        ):
            del values[key]
            prov.pop(key, None)
            touched += 1
    for key in list(prov):
        if _promoted(key) and key in merged and (key not in NIGHTLY_REPROBE_KEYS or key in probed):
            prov[key] = "verified"
    return touched


def _promoted(key: str) -> bool:
    return key.startswith(PROMOTED_PREFIXES) or key in PROMOTED_KEYS


def _root_key(path: object) -> str:
    return str(Path(str(path)).resolve()).casefold()


def select_installs(
    installs: list[KiCadInstallation], *, only: list[str] | None = None, roots: list[str] | None = None
) -> tuple[list[KiCadInstallation], list[str]]:
    """(at most one install per semver, notes on the ones skipped). With `roots`, exactly the
    installs at those roots (bench builds included: an explicit choice); else the first stock
    install of each semver in discovery order - bench builds are skipped (finding 12)."""
    wanted = {_root_key(r) for r in roots or []}
    chosen: dict[str, KiCadInstallation] = {}
    notes: list[str] = []
    for inst in installs:
        if only and inst.version not in only:
            continue
        explicit = _root_key(inst.root) in wanted
        if wanted and not explicit:
            continue
        if not explicit and inst.is_bench_build:
            notes.append(
                f"{inst.version}: skipped bench build {inst.root} ({inst.extra.get('commit')}); "
                "pass --root to refresh the entry from it"
            )
            continue
        first = chosen.get(inst.version)
        if first is not None:
            notes.append(
                f"{inst.version}: skipped {inst.root}; the entry is refreshed from {first.root} only "
                "(one install per semver)"
            )
            continue
        chosen[inst.version] = inst
    return list(chosen.values()), notes


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument(
        "--only", nargs="*", default=None, help="full semvers to refresh (default: all installed)"
    )
    ap.add_argument(
        "--root",
        nargs="*",
        default=None,
        help="install roots to refresh from (default: the first stock install of each semver; bench builds "
        "only when named here)",
    )
    ap.add_argument(
        "--skip-reprobe", action="store_true", help="do not run the D16 fixture re-probes (--help tree only)"
    )
    args = ap.parse_args(argv)

    raw = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))
    matrix = load_matrix()
    storage = current().storage_home
    changed: list[str] = []
    selected, skipped = select_installs(list(discover()), only=args.only, roots=args.root)
    for note in skipped:
        print(note, file=sys.stderr)
    explicit = {_root_key(r) for r in args.root or []}
    for inst in selected:
        caps = capabilities_for(inst, refresh=True, storage_home=storage)
        if not caps.get("probe.ok"):
            print(f"{inst.version}: probe failed, skipped", file=sys.stderr)
            continue
        if caps.get("kiwrap.patches") and _root_key(inst.root) not in explicit:
            print(
                f"{inst.version}: skipped {inst.root}: it carries kiwrap patches {caps.get('kiwrap.patches')} "
                "(pass --root to refresh the entry from it)",
                file=sys.stderr,
            )
            continue
        probed: dict[str, Any] = {}
        parts: dict[str, str] = {}
        if not args.skip_reprobe:
            for key, reprobe in REPROBES.items():
                try:
                    outcome = reprobe(inst, caps)
                except Exception as exc:  # noqa: BLE001 - one failed re-probe must not stop the refresh
                    print(f"{inst.version}: re-probe of {key} failed: {exc}", file=sys.stderr)
                    continue
                if outcome is not None:
                    probed[key], parts[key] = outcome
        seed_version, seed_values, exact, note = seed_for(inst.version, matrix)
        entry = raw["versions"].get(inst.version)
        if entry is None or entry.get("inherits") != seed_version and not exact:
            base = seed_version if not exact or entry is None else entry.get("inherits", seed_version)
            entry = {"inherits": base, "note": "", "values": {}}
        base = entry.get("inherits")
        base_values = matrix.versions.get(base, {}) if base else {}
        values = dict(entry.get("values", {}))
        prov = raw.setdefault("provenance", {}).setdefault(inst.version, {})
        touched = promote(dict(caps.items()), probed, base_values, values, prov)
        build = inst.extra.get("version_raw", "")
        head = f"probed live on {current().name} {inst.version}"
        if build and build != inst.version:
            head += f" ({build})"
        if inst.major_minor.endswith(".99"):
            head += " — nightly; re-run refresh_matrix.py after every nightly bump"
        entry["note"] = merge_note(str(entry.get("note", "")), head, parts)
        entry["values"] = values
        raw["versions"][inst.version] = entry
        changed.append(f"{inst.version}: {touched} keys differ from seed {base}; re-probed {sorted(probed)}")
    if not args.dry_run and changed:
        raw["versions"] = dict(
            sorted(raw["versions"].items(), key=lambda kv: tuple(int(p) for p in kv[0].split(".")))
        )
        MATRIX_PATH.write_text(json.dumps(raw, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print("\n".join(changed) or "nothing to refresh")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
