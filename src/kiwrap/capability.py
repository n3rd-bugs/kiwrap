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
    seed.version / seed.exact / seed.note   which matrix entry seeded the record and how well it fits
    swig.module / swig.python              paths found by the probe ("" when absent)
    probe.ok                bool   whether the `--help` tree probe succeeded (else the seed's cli.* stand)

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

    def __init__(self, install: KiCadInstallation, values: dict[str, Any], *, probed: bool):
        self._install, self._values, self.probed = install, dict(values), probed

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
    no SWIG), not from the last 10.0.x point release.
    """
    matrix = matrix or load_matrix()
    want = version_tuple(version)
    known = matrix.known()
    if not known:
        raise ValueError(f"{MATRIX_RESOURCE} lists no versions")
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


def _read_cache(path: Path, install: KiCadInstallation, schema_key: str) -> dict[str, Any] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if raw.get("schema") != CACHE_SCHEMA or raw.get("install") != install.key():
        return None
    if raw.get("kicad_cli_stat") != _cli_stat(install):
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
            return Capabilities(install, cached["values"], probed=bool(cached.get("probed")))

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

    _write_cache(
        path,
        {
            "schema": CACHE_SCHEMA,
            "install": install.key(),
            "version": install.version,
            "kicad_cli_stat": _cli_stat(install),
            "probed": probed,
            "values": values,
        },
    )
    return Capabilities(install, values, probed=probed)
