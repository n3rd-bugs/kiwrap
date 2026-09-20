"""Layer 2 — capability record keyed on FULL semver.

A capability record answers "can this install do X, and with which quirk?" so that tools never
branch on a version number. It is seeded from `kiwrap/data/kicad_version_matrix.json` (the
verified matrix from docs/ARCHITECTURE.md §2), then refined by cheap runtime probes (`--help`
trees, presence of python.exe, api schema file), and cached under the storage home per
`KiCadInstallation.key()`.

Key naming: dotted, lower-case, stable. Values are JSON scalars or lists. Unknown key -> `default`.
Documented keys (all of these must exist for every supported version, see the JSON file):
    cli.subcommands                 list[str]   e.g. ["pcb export gerbers", "sch erc", ...]
    cli.flags.<subcommand>          list[str]   long flags parsed from `--help`
    cli.has_erc / cli.has_drc       bool        (>= 8.0)
    cli.has_render                  bool        (>= 9.0)
    cli.has_jobset                  bool        (>= 9.0)
    cli.has_sch_pcb_upgrade         bool        (>= 10.0)
    cli.drc_refill_flags            bool        `pcb drc --refill-zones --save-board` (>= 10.0)
    cli.erc_json_pos_scale          number      100 on 8.0.1-10.0.x (ERC JSON positions are x100 too small)
    cli.gerbers_creates_outdir      bool        (>= 9.0)
    cli.writes_prl_on_read          bool        (>= 10.0)
    cli.help_exit_code              int         exit status of `--help` (non-zero on 7/8)
    cli.sch_stamp_gate_needs_generator_version  bool  (8-10)
    swig.available                  bool        (7-10; removed in 11)
    swig.shim                       str         "v7" | "v8plus" | ""
    swig.ses_import_headless        bool        (>= 8.0)
    swig.write_drc_report_ok        bool        (7-9 with shim; false on 10)
    swig.needs_newboard_for_fill    bool        ZONE_FILLER on BOARD() crashes (7-9)
    ipc.available                   bool        (>= 9.0)
    ipc.headless                    bool        (>= 11)
    ipc.schematic                   bool        (>= 11)
    ipc.schematic_write_crashes     bool        9.0.0-10.0.6 (#24966)
    ipc.commit_needs_document       bool        (>= 10.0.7)
    ipc.headless_libraries_need_load bool       headless server loads no library until LoadAllLibraries (11)
    ipc.headless_jobs               bool        the 22 RunBoardJobExport*/RunSchematicJobExport* commands are
                                                handled by the headless api-server (true on 10.99.0-4083 and 11;
                                                presence: T5 rows `handled`/`bad_request` on the job's own
                                                document type, `backends.ipc.jobs.probe_headless_jobs`,
                                                tests/test_ipc_patch_g6.py). A request whose nested
                                                `job_settings.document` is empty is AS_UNHANDLED by design; the
                                                first T5 snapshot sent that and misread it as "absent" (UPSTREAM G6)
    ipc.schematic_update_shared_lib_symbol_hangs bool  UpdateItems on a 2nd instance of one lib symbol never replies (10.99.0-4083)
    ipc.footprint_update_rotation_noise bool     UpdateItems(FootprintInstance) with a CHANGED orientation re-derives the
                                                children's library coordinates through the inverse transform: 1 nm noise
                                                -> DRC lib_footprint_mismatch (10.99.0-4083, UPSTREAM G16); flips / moves clean
    ipc.single_fixed_pipe           bool        Windows 9/10
    formats.sch / formats.pcb / formats.sym / formats.pro  int  epoch stamps written by this version
    formats.pcb_layer_ids_v2        bool        board layer ids renumbered (B.Cu 31 -> 2) + 4-word layerselection (>= 9.0)
    formats.generator_version_token bool        reader accepts `(generator_version "X.Y")` (>= 8.0; "Unknown token" on 7)
    libs.ships_wrl                  bool        (<= 9)
    rules.default_min_track_width_mm number     0.2 on 10, 0 on 9
    rules.drc_must_fix              list[str]   DRC violation types that gate kw_fab_export (ARCHITECTURE §11)
    rules.drc_review                list[str]   DRC violation types reported as "review" (§11)
    rules.drc_cosmetic_prefix       str         DRC type prefix classed "cosmetic" ("silk_", §11)
    setup.wizard_needs_three_tables bool        (10.0)
    setup.prompt_flags              list[str]   do_not_show_again keys that must be true

Extra keys this implementation adds (same conventions):
    cli.has_sym_fp_upgrade  bool   `sym upgrade` + `fp upgrade` (all supported versions)
    cli.has_stats           bool   `pcb export stats`
    cli.has_stats_json      bool   `pcb export stats --format json` (10.0+; unit strings, see formats.reports.stats)
    cli.has_stackup_json    bool   `pcb export stackup --format json` (11 / 10.99 nightly; MR !2691 + json on master)
    cli.has_sch_diff        bool   `sch diff --format json` (11 / 10.99 nightly; MR !2620; exit 5 = differences)
    cli.has_pcb_png         bool   `pcb export png` (11 / 10.99 nightly; 2D layer PNGs into a directory)
    cli.has_ratsnest_export bool   `pcb export ratsnest` (kiwrap fork patch ratsnest-export,
                                   docs/UPSTREAM.md G1; no release has it): KiCad's own airwires per net
                                   with both end items, anchor positions and layers. Derived from the
                                   `--help` tree like the keys above (presence, never the version).
    cli.has_specctra_export bool   `pcb export specctra` (kiwrap fork patch specctra-jobs, drafts under
                                   docs/upstream/drafts/specctra-jobs-*.md; no release has it): the board
                                   as a Specctra DSN, KiCad's own
                                   exporter (DSN::ExportBoardToSpecctraFile) run headless as a job -
                                   what SWIG ExportSpecctraDSN gave on <= 10 (docs/VERIFICATION.md
                                   claim 7). Derived from the `--help` tree (presence, never the version).
    cli.has_specctra_import bool   `pcb import-ses --ses FILE [-o OUT] BOARD` (same patch): KiCad's own
                                   session importer (DSN::ImportSpecctraSession) run headless as a job
                                   with the editor's semantics - unlocked tracks/arcs/vias replaced by
                                   the session's routes, locked ones kept, SES placement applied to
                                   every named footprint (locked too), unresolved items skipped with a
                                   warning on stderr; a session missing its sections fails with exit 3
                                   and leaves the board untouched (claim 7 on 8-10 said "partial
                                   mutation on False" - the job's commit is reverted instead).
    ipc.has_get_ratsnest    bool   the headless api-server answers `GetRatsnest` (same patch). Seeded
                                   false; `capabilities_for` DERIVES it from `ipc.headless` AND
                                   `cli.has_ratsnest_export` (the api-server is the probed kicad-cli
                                   binary; a build carrying only one half of the patch is not
                                   supported) until the first headless session with a board runs the
                                   presence probe (`backends.ipc.presence`, docs/UPSTREAM.md 3.3:
                                   `backends.ipc.queries.probe_get_ratsnest`, one request) and
                                   records the outcome through `Capabilities.refine`, which lists the
                                   key in `kiwrap.ipc_probed` and rewrites the cache.
    cli.drc_error_limit     int    `pcb drc` stops reporting a violation TYPE after this many items (199:
                                   DRC_ENGINE ERROR_LIMIT, pcbnew/drc/drc_engine.cpp, enforced atomically in
                                   ReportViolation; verified 2026-09-19: 203 missing courtyards -> 199 listed)
    cli.drc_extended_error_limit int  the same for the types in cli.drc_extended_error_types (499,
                                   EXTENDED_ERROR_LIMIT; kit-dev-coldfire with 30 % of its tracks removed
                                   reports exactly 499 unconnected_items for 634 real airwires)
    cli.drc_extended_error_types list[str]  ["clearance", "unconnected_items"] (DRC_ENGINE::RunTests)
    cli.netlist_pinfunction_suffix bool  `sch export netlist` writes (pinfunction "<name>_<number>") and the
                                   `+no_connect` pintype suffix only for a no-connect MARKER (10.0+,
                                   eeschema/netlist_exporters/netlist_exporter_xml.cpp); 9 and older write
                                   the bare pin name
    seed.version / seed.exact / seed.note   which matrix entry seeded the record and how well it fits
    swig.module / swig.python              paths found by the probe ("" when absent)
    probe.ok                bool   whether the `--help` tree probe succeeded (else the seed's cli.* stand)
    kiwrap.patches          list[str]  ids from `kiwrap/data/kiwrap_patches.json` whose EVERY detection key
                                   is truthy in this record (docs/UPSTREAM.md §3.3). Informational only:
                                   `kw_env` shows it, receipts cite it, T9 refuses to certify a non-empty
                                   list as *supported*; tools never branch on it.
    kiwrap.patched_build    bool   the install's `commit` (from `<root>/kiwrap-install.json`, see
                                   `kiwrap.installation`: the BUILT commit, the integration branch's
                                   sha for a patched build) matches a `builds[].commit` of the
                                   manifest - i.e. a bench build kiwrap made, T5-snapshotted and installed
    kiwrap.build_commit     str    that commit ("" for stock installs); `kiwrap.build_base` is the
                                   upstream commit it was built on (`base/<sha>`, "" for stock)
    kiwrap.ipc_probed       list[str]  ipc.* keys a live presence probe has decided for this install
                                   (`Capabilities.refine`); a key absent here still holds its seeded
                                   or derived value

Implementation notes
- Nearest-known-version seeding (docs/ARCHITECTURE.md §6): same major, highest known version
  <= the install's; if the install is OLDER than every known release of its major, the lowest
  known one is used and `seed.exact` is false with a `seed.note`.
- The `--help` tree probe (`probe_help_tree`) is the parser the rest of the code base reaches
  through `kiwrap.backends.cli.flagmap`; it lives here because import-linter's layer contract
  forbids capability -> backends imports, while backends -> capability is allowed.
- Every kicad-cli run uses `kiwrap.installation.probe_env` (throwaway KICAD_CONFIG_HOME):
  kicad-cli rewrites `kicad_common.json` on exit even for `--help` (docs/VERIFICATION.md claim 10).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

from kiwrap.installation import KiCadInstallation, probe_env, version_tuple
from kiwrap.platform import current
from kiwrap.platform.system import System

MATRIX_RESOURCE = "kicad_version_matrix.json"
MATRIX_SCHEMA = 1
PATCHES_RESOURCE = "kiwrap_patches.json"  # the fork's patch manifest (docs/UPSTREAM.md §3.2)
PATCHES_SCHEMA = 1
CACHE_SCHEMA = 1
CACHE_DIRNAME = "capabilities"
HELP_TIMEOUT_S = 60
MAX_HELP_DEPTH = 5

# Relative to the install root; the Windows one is verified (7.0.6-10.0.0 ship
# bin/Lib/site-packages/_pcbnew.pyd), the others are the designed distro-Linux layouts.
SWIG_MODULE_GLOBS = (
    "bin/Lib/site-packages/_pcbnew*.pyd",
    "lib/python3*/site-packages/_pcbnew*.so",
    "lib/python3*/dist-packages/_pcbnew*.so",
    "lib64/python3*/site-packages/_pcbnew*.so",
)
IPC_SCHEMA_RELPATH = (
    Path("schemas") / "api.v1.schema.json"
)  # under share; present on 9/10, absent on 7/8 [verified]


class Capabilities(Mapping[str, Any]):
    """Read-only view over the merged (seed + probe) record for one install."""

    def __init__(
        self,
        install: KiCadInstallation,
        values: dict[str, Any],
        *,
        probed: bool,
        storage_home: Path | None = None,
    ):
        self._install, self._values, self.probed = install, dict(values), probed
        self._storage_home = storage_home  # where `refine` persists (None: an unpersisted record)

    @property
    def install(self) -> KiCadInstallation:
        return self._install

    def __getitem__(self, key: str) -> Any:
        return self._values[key]

    def __iter__(self):
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def has(self, key: str) -> bool:
        """True iff key exists and is truthy."""
        return bool(self._values.get(key))

    def flags(self, subcommand: str) -> list[str]:
        return list(self._values.get(f"cli.flags.{subcommand}", []))

    def patch_for(self, key: str) -> str:
        """The id of the manifest patch (`kiwrap.patches`, i.e. present on this install) that
        provides `key`, or "" when the key is stock or the patch is not present. Receipts append it
        to `provider_used` (`cli:10.99.0+ratsnest-export`, docs/UPSTREAM.md §3.3)."""
        return patch_providing(self, key)

    def refine(self, key: str, value: Any) -> None:
        """Record the outcome of a live presence probe (docs/UPSTREAM.md §3.3: IPC patches are
        detected on first use of the headless backend). Updates the key, lists it in
        `kiwrap.ipc_probed`, updates the `kiwrap.*` keys and rewrites this install's cache so later
        sessions start from the probed value (and skip the probe). A record that was never cached
        (hand-built, `storage_home=None`) is updated in memory only."""
        probed = list(self._values.get("kiwrap.ipc_probed", []) or [])
        if self._values.get(key) == value and key in probed:
            return
        self._values[key] = value
        if key not in probed:
            probed.append(key)
        self._values["kiwrap.ipc_probed"] = sorted(probed)
        self._values.update(patch_keys(self._values, self._install))
        if self._storage_home is None:
            return
        path = cache_file(self._install, self._storage_home)
        cached = _read_cache(path, self._install, "values")
        if cached is None:
            return
        cached["values"] = dict(self._values)
        _write_cache(path, cached)

    def to_json(self) -> dict[str, Any]:
        return {
            "install": self._install.key(),
            "version": self._install.version,
            "probed": self.probed,
            "values": self._values,
        }


class Unsupported(RuntimeError):
    """Raised by backends when a capability is missing. Tools turn it into E_UNSUPPORTED."""

    def __init__(
        self,
        capability: str,
        install: KiCadInstallation,
        *,
        oldest_kicad_with_it: str | None,
        fallback: str | None,
    ):
        self.capability, self.install = capability, install
        self.oldest_kicad_with_it, self.fallback = oldest_kicad_with_it, fallback
        msg = f"{capability} is not available on KiCad {install.version}"
        if oldest_kicad_with_it:
            msg += f" (first available in {oldest_kicad_with_it})"
        if fallback:
            msg += f"; fallback: {fallback}"
        super().__init__(msg)


# --- the seed matrix ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Matrix:
    """`kicad_version_matrix.json` with `inherits` chains resolved."""

    schema: int
    versions: dict[str, dict[str, Any]]  # full semver -> flat key/value record
    provenance: dict[str, dict[str, str]]  # full semver -> key -> "verified" | "docs"
    notes: dict[str, str]  # full semver -> free-text note (e.g. "master, not installed")

    def known(self) -> list[str]:
        return sorted(self.versions, key=version_tuple)


def matrix_path() -> Path:
    return Path(__file__).parent / "data" / MATRIX_RESOURCE


@cache
def load_matrix() -> Matrix:
    raw = json.loads(matrix_path().read_text(encoding="utf-8"))
    if raw.get("schema") != MATRIX_SCHEMA:
        raise ValueError(f"{MATRIX_RESOURCE}: schema {raw.get('schema')!r}, expected {MATRIX_SCHEMA}")
    versions: dict[str, dict[str, Any]] = {}
    provenance: dict[str, dict[str, str]] = {}
    notes: dict[str, str] = {}

    def resolve(ver: str, chain: tuple[str, ...] = ()) -> None:
        if ver in versions:
            return
        if ver in chain:
            raise ValueError(f"{MATRIX_RESOURCE}: inheritance cycle at {ver}")
        entry = raw["versions"][ver]
        base = entry.get("inherits")
        values: dict[str, Any] = {}
        prov: dict[str, str] = {}
        if base:
            resolve(base, (*chain, ver))
            values.update(versions[base])
            prov.update(provenance[base])
        values.update(entry.get("values", {}))
        prov.update(raw.get("provenance", {}).get(ver, {}))
        versions[ver], provenance[ver] = values, prov
        if entry.get("note"):
            notes[ver] = entry["note"]

    for ver in raw["versions"]:
        resolve(ver)
    return Matrix(raw["schema"], versions, provenance, notes)


def seed_for(version: str, matrix: Matrix | None = None) -> tuple[str, dict[str, Any], bool, str]:
    """Return (seed_version, values, exact, note) for an install's full semver.

    Rule: same major, highest known <= version; else lowest known of that major; else the highest
    known version below it from any major; else the lowest known overall. Only the first case with
    an equal version is `exact`.

    A nightly / master build reports itself as `<major>.99.x` (`kicad-cli version --format plain`
    prints e.g. `10.99.0-unknown-abcdef`; KiCad's convention since 5.99), and it IS the development
    branch of major+1: 10.99 seeds from the 11.0.0 entry (headless api-server, 11 format epochs,
    no SWIG), not from the last 10.0.x point release - UNLESS the matrix carries an entry for that
    exact nightly version (`10.99.0`, refreshed by scripts/ci/refresh_matrix.py from the installed
    nightly): a verified entry for the very version beats the next major's docs-seeded one, and it
    is the only way a nightly-only quirk (`ipc.schematic_update_shared_lib_symbol_hangs`, G13)
    reaches the live record. `seed.exact` is true then, with a note saying it is the nightly entry.
    """
    matrix = matrix or load_matrix()
    want = version_tuple(version)
    known = matrix.known()
    if not known:
        raise ValueError(f"{MATRIX_RESOURCE} lists no versions")
    exact_entry = next((v for v in known if version_tuple(v) == want), None)
    if exact_entry is not None and len(want) >= 2 and want[1] == 99:
        values = dict(matrix.versions[exact_entry])
        note = f"seeded from the matrix's own {exact_entry} entry ({version} is a nightly of major {want[0] + 1})"
        values.update({"seed.version": exact_entry, "seed.exact": True, "seed.note": note})
        return exact_entry, values, True, note
    nightly_of: int | None = None
    if len(want) >= 2 and want[1] == 99:
        nightly_of = want[0] + 1
        want = (nightly_of, 0, 0)
    same_major = [v for v in known if version_tuple(v)[0] == want[0]]
    at_or_below = [v for v in same_major if version_tuple(v) <= want]
    if at_or_below:
        seed = at_or_below[-1]
        exact = version_tuple(seed) == want
        note = (
            "" if exact else f"seeded from {seed} (highest known release of this major at or below {version})"
        )
    elif same_major:
        seed = same_major[0]
        exact = False
        note = f"seeded from {seed}: {version} is older than every known release of this major; probe results override"
    else:
        below = [v for v in known if version_tuple(v) < want]
        seed = below[-1] if below else known[0]
        exact = False
        note = f"seeded from {seed}: no known release of major {want[0]} in the matrix; treat every value as a guess"
    if nightly_of is not None:
        exact = False
        note = f"seeded from {seed}: {version} is a nightly of major {nightly_of} (the development branch)"
    values = dict(matrix.versions[seed])
    values.update({"seed.version": seed, "seed.exact": exact, "seed.note": note})
    return seed, values, exact, note


# --- the patch manifest -----------------------------------------------------------------------------


def patches_path() -> Path:
    return Path(__file__).parent / "data" / PATCHES_RESOURCE


@cache
def load_patch_manifest() -> dict[str, Any]:
    """`kiwrap_patches.json`: `base` sha, `patches` (id, gap, keys, status, ...) and `builds`
    (tag, commit, patches). Missing file -> an empty manifest (stock-only checkouts)."""
    try:
        raw = json.loads(patches_path().read_text(encoding="utf-8"))
    except OSError:
        return {"schema": PATCHES_SCHEMA, "base": "", "patches": [], "builds": []}
    if raw.get("schema") != PATCHES_SCHEMA:
        raise ValueError(f"{PATCHES_RESOURCE}: schema {raw.get('schema')!r}, expected {PATCHES_SCHEMA}")
    raw.setdefault("patches", [])
    raw.setdefault("builds", [])
    return raw


def patch_keys(values: Mapping[str, Any], install: KiCadInstallation) -> dict[str, Any]:
    """The `kiwrap.*` keys (docstring) from the manifest and an already-probed record.

    Presence probes decide, never the version string or the install's own claim: a patch counts
    only when every key it names is truthy; a build counts only by its commit.
    """
    manifest = load_patch_manifest()
    patches = [
        p["id"] for p in manifest["patches"] if p.get("keys") and all(values.get(k) for k in p["keys"])
    ]
    commit = install.extra.get("commit", "")
    patched = bool(commit) and any(b.get("commit") == commit for b in manifest["builds"])
    return {
        "kiwrap.patches": patches,
        "kiwrap.patched_build": patched,
        "kiwrap.build_commit": commit,
        "kiwrap.build_base": install.extra.get("base", ""),
    }


def patch_providing(values: Mapping[str, Any], key: str) -> str:
    """`Capabilities.patch_for` over any record mapping (tools receive hand-built records in
    tests): the present patch (listed in the record's `kiwrap.patches`) whose manifest `keys`
    name `key`, else ""."""
    present = set(values.get("kiwrap.patches", []) or [])
    if not present:
        return ""
    for patch in load_patch_manifest()["patches"]:
        if patch.get("id") in present and key in (patch.get("keys") or []):
            return str(patch["id"])
    return ""


# --- `--help` parsing and the help tree probe ----------------------------------------------------

_SECTION_RE = re.compile(r"^(?P<name>[A-Za-z][A-Za-z ]*):\s*$")
_USAGE_RE = re.compile(r"^Usage:\s*(?P<cmd>[^\[\{]*)")
_BRACES_RE = re.compile(r"\{([^}]*)\}")
_OPTION_LINE_RE = re.compile(r"^\s{1,8}(?P<names>-[^\t]*?)(?:\t|\s{2,}|$)")
_SUB_LINE_RE = re.compile(r"^\s{1,8}(?P<name>[A-Za-z0-9][\w.-]*)(?:\s|$)")


def parse_help(text: str) -> dict[str, Any]:
    """Parse one kicad-cli `--help` page (argparse-for-C++ layout, 7.0 through 10.0 [verified on
    the spike corpus spikes/versioning-strategy/clihelp and live]).

    Returns {"subcommands": [...], "flags": [...], "positionals": [...], "usage": str,
    "command": str, "description": str}. `flags` are the long (`--x`) option names from the
    "Optional arguments" block, `subcommands` the union of the `{a,b,c}` group in the usage line
    and the "Subcommands" block. 7.0 separates option names from help with a TAB, 8+ with two or
    more spaces; both are handled. Lines before the usage line (7/8 print
    `input: 1 argument(s) expected. 0 provided.` on stderr) are ignored.
    """
    subcommands: set[str] = set()
    flags: list[str] = []
    positionals: list[str] = []
    usage = ""
    command = ""
    description_lines: list[str] = []
    section = ""
    seen_usage = False
    for raw in text.splitlines():
        line = raw.rstrip()
        if not seen_usage:
            m = _USAGE_RE.match(line)
            if m:
                usage = line
                command = m.group("cmd").strip()
                seen_usage = True
                for group in _BRACES_RE.findall(line):
                    subcommands.update(s.strip() for s in group.split(",") if s.strip())
            continue
        m = _SECTION_RE.match(line)
        if m:
            section = m.group("name").lower()
            continue
        if not line.strip():
            continue
        if not section:
            description_lines.append(line.strip())
        elif section.startswith("optional"):
            m = _OPTION_LINE_RE.match(line)
            if m:
                for name in re.split(r",\s*", m.group("names").strip()):
                    if name.startswith("--") and name not in flags:
                        flags.append(name)
        elif section.startswith("positional"):
            m = _SUB_LINE_RE.match(line)
            if m:
                positionals.append(m.group("name"))
        elif section.startswith("subcommand"):
            m = _SUB_LINE_RE.match(line)
            if m:
                subcommands.add(m.group("name"))
    return {
        "subcommands": sorted(subcommands),
        "flags": flags,
        "positionals": positionals,
        "usage": usage,
        "command": command,
        "description": " ".join(description_lines),
    }


def probe_help_tree(
    kicad_cli: Path,
    system: System,
    env: Mapping[str, str],
    *,
    max_depth: int = MAX_HELP_DEPTH,
) -> dict[str, Any]:
    """Run `kicad-cli [<path>...] --help` recursively and return the nested tree.

    Node layout: {"command": "pcb export", "flags": [...], "positionals": [...], "description":
    str, "exit_code": int, "subcommands": {name: node}}. Only names listed by the parent are
    visited: asking 7.0 for `pcb drc --help` prints the parent's usage plus "Maximum number of
    positional arguments exceeded" [verified], so the usage line's last word must equal the node
    name for the node to count (else it is dropped). `--help` exits 1 on 7/8 for leaves with a
    required positional and 0 on 9/10 [verified]; the exit code is recorded, never treated as
    failure. Raises RuntimeError only when the root `--help` produces no usage line at all.
    """

    def visit(path: list[str], depth: int) -> dict[str, Any] | None:
        result = system.run([str(kicad_cli), *path, "--help"], env=env, timeout_s=HELP_TIMEOUT_S)
        text = result.stdout if "Usage:" in result.stdout else result.stdout + "\n" + result.stderr
        parsed = parse_help(text)
        if not parsed["usage"]:
            return None
        if path and parsed["command"].split()[-1:] != path[-1:]:
            return None
        node: dict[str, Any] = {
            "command": " ".join(path),
            "flags": parsed["flags"],
            "positionals": parsed["positionals"],
            "description": parsed["description"],
            "exit_code": result.returncode,
            "subcommands": {},
        }
        if depth < max_depth:
            for name in parsed["subcommands"]:
                child = visit([*path, name], depth + 1)
                if child is not None:
                    node["subcommands"][name] = child
        return node

    root = visit([], 0)
    if root is None:
        raise RuntimeError(f"{kicad_cli} --help printed no usage line")
    return root


def flatten_help_tree(tree: dict[str, Any]) -> dict[str, Any]:
    """Derive the `cli.*` keys from a help tree (pure; shared by the live probe and the seed
    generator so the matrix and the probe agree byte-for-byte)."""
    nodes: dict[str, dict[str, Any]] = {}

    def walk(node: dict[str, Any]) -> None:
        if node["command"]:
            nodes[node["command"]] = node
        for child in node["subcommands"].values():
            walk(child)

    walk(tree)
    commands = sorted(nodes)
    values: dict[str, Any] = {"cli.subcommands": commands}
    for cmd in commands:
        values[f"cli.flags.{cmd}"] = list(nodes[cmd]["flags"])
    drc_flags = set(nodes.get("pcb drc", {}).get("flags", []))
    values.update(
        {
            "cli.has_erc": "sch erc" in nodes,
            "cli.has_drc": "pcb drc" in nodes,
            "cli.has_render": "pcb render" in nodes,
            "cli.has_jobset": "jobset run" in nodes,
            "cli.has_sch_pcb_upgrade": "sch upgrade" in nodes and "pcb upgrade" in nodes,
            "cli.has_sym_fp_upgrade": "sym upgrade" in nodes and "fp upgrade" in nodes,
            "cli.has_stats": "pcb export stats" in nodes,
            "cli.has_stats_json": "--format" in set(nodes.get("pcb export stats", {}).get("flags", [])),
            "cli.has_stackup_json": "--format" in set(nodes.get("pcb export stackup", {}).get("flags", [])),
            "cli.has_sch_diff": "sch diff" in nodes,
            "cli.has_pcb_png": "pcb export png" in nodes,
            "cli.has_ratsnest_export": "pcb export ratsnest" in nodes,
            "cli.has_specctra_export": "pcb export specctra" in nodes,
            "cli.has_specctra_import": "pcb import-ses" in nodes,
            "cli.drc_refill_flags": {"--refill-zones", "--save-board"} <= drc_flags,
            "cli.help_exit_code": max(
                (n["exit_code"] for n in nodes.values()), default=tree.get("exit_code", 0)
            ),
        }
    )
    return values


# --- the other probes ------------------------------------------------------------------------------


def find_swig_module(install: KiCadInstallation, system: System) -> Path | None:
    for pattern in SWIG_MODULE_GLOBS:
        hits = system.glob(install.root, pattern)
        if hits:
            return hits[0]
    return None


def probe_static(install: KiCadInstallation, system: System) -> dict[str, Any]:
    """Filesystem-only probes: SWIG module + interpreter, IPC schema file."""
    module = find_swig_module(install, system)
    python_ok = install.python is not None and system.is_file(install.python)
    return {
        "swig.available": bool(module) and python_ok,
        "swig.module": str(module) if module else "",
        "swig.python": str(install.python) if python_ok and install.python else "",
        "ipc.available": system.is_file(install.share / IPC_SCHEMA_RELPATH),
    }


# --- cache + public entry point ----------------------------------------------------------------


def _cache_dir(storage_home: Path) -> Path:
    return storage_home / CACHE_DIRNAME


def cache_file(install: KiCadInstallation, storage_home: Path) -> Path:
    """`<storage>/capabilities/<version>@<root-sanitised>-<hash>.json` (key() is not filename-safe)."""
    key = install.key()
    safe = re.sub(r"[^A-Za-z0-9.@_-]+", "_", key).strip("_")[:80]
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]
    return _cache_dir(storage_home) / f"{safe}-{digest}.json"


def help_tree_file(install: KiCadInstallation, storage_home: Path) -> Path:
    return cache_file(install, storage_home).with_suffix(".help.json")


def _cli_stat(install: KiCadInstallation) -> list[int]:
    try:
        st = install.kicad_cli.stat()
    except OSError:
        return [0, 0]
    return [st.st_size, st.st_mtime_ns]


def _matrix_digest() -> str:
    """sha1 of the seed matrix AND the patch manifest: a cache written from an older matrix (new
    keys such as `cli.drc_error_limit`) or an older manifest (a patch added, the base sha bumped)
    is stale even though the KiCad did not change (docs/UPSTREAM.md §3.3 cache safety)."""
    h = hashlib.sha1()
    for path in (matrix_path(), patches_path()):
        with contextlib.suppress(OSError):
            h.update(path.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


def _read_cache(path: Path, install: KiCadInstallation, schema_key: str) -> dict[str, Any] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if raw.get("schema") != CACHE_SCHEMA or raw.get("install") != install.key():
        return None
    if raw.get("kicad_cli_stat") != _cli_stat(install):
        return None
    if schema_key == "values" and raw.get("matrix_digest") != _matrix_digest():
        return None
    return raw if schema_key in raw else None


def _write_cache(path: Path, payload: dict[str, Any]) -> None:
    """Atomic write through a per-process temp name. Two processes probing the same install at
    once (cold cache: MCP server start + `kiwrap env`) must not share one `.tmp` (on Windows the
    loser's write/replace raised PermissionError, surfacing as a bogus E_FILE_LOCKED); whoever
    replaces last wins, and both wrote the same record for the same install key."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(json.dumps(payload, indent=1, sort_keys=True), encoding="utf-8")
    try:
        tmp.replace(path)
    except OSError:
        # a concurrent writer holds the destination open on Windows: its content is equivalent
        with contextlib.suppress(OSError):
            tmp.unlink()
        if not path.is_file():
            raise


def help_tree_for(
    install: KiCadInstallation,
    *,
    refresh: bool = False,
    storage_home: Path | None = None,
    system: System | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """The cached `--help` tree for an install (probed on first use / `refresh`).

    `env` defaults to `probe_env(storage_home)`; callers that already hold a sandbox root may pass
    `ConfigRoot.env()` instead. The tree is cached next to the capability record.
    """
    storage_home = storage_home or current().storage_home
    system = system or System()
    path = help_tree_file(install, storage_home)
    if not refresh:
        cached = _read_cache(path, install, "tree")
        if cached is not None:
            return cached["tree"]
    tree = probe_help_tree(install.kicad_cli, system, env or probe_env(storage_home, system.environ()))
    _write_cache(
        path,
        {
            "schema": CACHE_SCHEMA,
            "install": install.key(),
            "version": install.version,
            "kicad_cli_stat": _cli_stat(install),
            "tree": tree,
        },
    )
    return tree


def capabilities_for(
    install: KiCadInstallation,
    *,
    refresh: bool = False,
    storage_home: Path | None = None,
    system: System | None = None,
) -> Capabilities:
    """Seed from the matrix (nearest known version <= this one, same major), probe, cache, return.

    `storage_home` / `system` are injectable for tests (default: `Platform.storage_home`, the real
    machine). `refresh=True` ignores and rewrites the cache. A failed `--help` probe (kicad-cli not
    runnable) keeps the seed's `cli.*` values and reports `probed=False` / `probe.ok=False`.
    """
    storage_home = storage_home or current().storage_home
    system = system or System()
    path = cache_file(install, storage_home)
    if not refresh:
        cached = _read_cache(path, install, "values")
        if cached is not None:
            return Capabilities(
                install, cached["values"], probed=bool(cached.get("probed")), storage_home=storage_home
            )

    _seed, values, _exact, _note = seed_for(install.version)
    values.update(probe_static(install, system))
    probed = False
    try:
        tree = help_tree_for(install, refresh=refresh, storage_home=storage_home, system=system)
    except RuntimeError:
        values["probe.ok"] = False
    else:
        for key in [k for k in values if k.startswith("cli.flags.")]:
            del values[key]
        values.update(flatten_help_tree(tree))
        values["probe.ok"] = True
        probed = True
    # The api-server is the probed kicad-cli binary, so a build whose --help tree lists the CLI half
    # of the ratsnest patch carries its IPC half too (the manifest's patches are all-or-nothing on
    # the integration branch); the first headless session with a board runs the presence probe
    # (backends.ipc.presence) and Capabilities.refine records the live answer.
    if values.get("ipc.headless") and values.get("cli.has_ratsnest_export"):
        values["ipc.has_get_ratsnest"] = True
    values["kiwrap.ipc_probed"] = []
    values.update(patch_keys(values, install))

    _write_cache(
        path,
        {
            "schema": CACHE_SCHEMA,
            "install": install.key(),
            "version": install.version,
            "kicad_cli_stat": _cli_stat(install),
            "matrix_digest": _matrix_digest(),
            "probed": probed,
            "values": values,
        },
    )
    return Capabilities(install, values, probed=probed, storage_home=storage_home)
