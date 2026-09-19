"""Sandboxed KiCad configuration roots.

Every KiCad process kiwrap starts (kicad-cli, the SWIG driver, a GUI for live sessions) runs with
`KICAD_CONFIG_HOME` pointing at a root kiwrap owns under the storage home. The user's real config
(`Platform.user_kicad_config_home`) is read for library tables and environment variables and is
NEVER written; conformance test T6 asserts its hash is unchanged after every run.

Verified facts this module encodes (docs/VERIFICATION.md claim 10, docs/ARCHITECTURE.md §6):
- KiCad appends `<major.minor>/` under KICAD_CONFIG_HOME; seed files go INSIDE that subfolder.
- 10.0 shows the Setup wizard unless `kicad_common.json` has `api.enable_server` (any value),
  `do_not_show_again.update_check_prompt` and `.data_collection_prompt` both true, AND all three
  tables (sym-lib-table, fp-lib-table, design-block-lib-table) exist and parse. 9.0 gates its
  dialogs on the same flags; it auto-creates the design-block table.
- kicad-cli rewrites `kicad_common.json` on exit -> runs are serialised per root and the seed is
  restored afterwards; 8.0.1/9.0.5 segfaulted on the very first ERC against a brand-new root ->
  every root is pre-warmed once with a trivial command.
- Library seed: `<install>/share/kicad/template/*-lib-table` first, then rows merged read-only
  from the user's `<user_kicad_config_home>/<major.minor>/{sym,fp}-lib-table` (decision D7),
  env vars (`environment.vars`) copied from the user's `kicad_common.json` of that version.

Layout under `storage_home` (one root per INSTALL - full semver plus a digest of the install root,
`root_dirname()`, the same key `capability.cache_file` uses - so point releases never share state
and two installs that report the same version, e.g. Program Files 10.0.0 next to a portable or
patched 10.0.0, never share a root, a seed or a lock):

    kicad-config/<version>-<root digest>/   KICAD_CONFIG_HOME  (ConfigRoot.path)
        <major.minor>/                      the folder KiCad actually reads (ConfigRoot.versioned)
            kicad_common.json, sym-lib-table, fp-lib-table, design-block-lib-table
        .seed/                              pristine copies of the four files for restore_seed()
        .lock, .prewarmed                   cross-process lock; prewarm marker
    kicad-documents/<version>-<root digest>/  KICAD_DOCUMENTS_HOME (KiCad appends <major.minor>/)
    kicad-cache/<version>-<root digest>/    KICAD_CACHE_HOME

Spike evidence: spikes/verify-ipc-config-home/cfg10 (positive + negative wizard controls),
spikes/versioning-strategy/cfgapi10/10.0/kicad_common.json (the minimal seed that started
pcbnew wizard-free), spikes/erc-json-scale/cfg*/ (what kicad-cli writes back on exit),
docs/research/07-versioning-strategy.md (env var names, `kicad-cli` never creates lib tables).
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import subprocess
import tempfile
import time
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from kiwrap.installation import KiCadInstallation
from kiwrap.platform import Platform, current
from kiwrap.platform.system import RunResult, System

__all__ = [
    "ConfigRoot",
    "LibRow",
    "SandboxError",
    "SEED_FILES",
    "ensure",
    "merge_lib_table",
    "parse_lib_table",
    "probe_version",
    "root_dirname",
    "root_for",
    "tree_hash",
    "user_config_hash",
]

#: Files kiwrap seeds inside `<root>/<major.minor>/` and keeps pristine copies of in `<root>/.seed/`.
SEED_FILES: tuple[str, ...] = (
    "kicad_common.json",
    "sym-lib-table",
    "fp-lib-table",
    "design-block-lib-table",
)

_TABLE_HEADS = {
    "sym-lib-table": "sym_lib_table",
    "fp-lib-table": "fp_lib_table",
    "design-block-lib-table": "design_block_lib_table",
}

#: Library-path variables KiCad versions per MAJOR as `KICAD<major>_<suffix>` (docs/research/07,
#: env_vars.cpp `GetVersionedEnvVarName('KICAD%d_%s')`), mapped to the `share/kicad/<dir>` they
#: default to. `3RD_PARTY` defaults to `<documents>/<major.minor>/3rdparty` and is handled apart.
_SHARE_ENV_SUFFIXES: tuple[tuple[str, str], ...] = (
    ("SYMBOL_DIR", "symbols"),
    ("FOOTPRINT_DIR", "footprints"),
    ("3DMODEL_DIR", "3dmodels"),
    ("TEMPLATE_DIR", "template"),
)

#: Files KiCad leaves behind that must not survive a run (fp-info-cache is a footprint index
#: kicad-cli/pcbnew regenerate; `*.lck` are stale lock files from an aborted GUI session).
_STALE_NAMES = ("fp-info-cache",)
_STALE_SUFFIXES = (".lck",)

_PREWARM_TIMEOUT_S = 120.0


class SandboxError(RuntimeError):
    """A sandbox root could not be created, seeded, locked or pre-warmed."""


# --------------------------------------------------------------------------------------------
# Library tables: a deliberately tiny s-expression reader (kiwrap.formats may not exist yet and
# a lib table is flat: `(<head> (version N)? (lib (name ..)(type ..)(uri ..)(options ..)(descr ..))*)`).
# Row text is copied verbatim so `(hidden)` / `(disabled)` / unquoted atoms survive untouched.
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class LibRow:
    """One `(lib ...)` row of a KiCad library table, plus its verbatim source text."""

    name: str
    type: str
    uri: str
    text: str


@dataclass(frozen=True)
class _Node:
    head: str
    children: tuple[_Node | str, ...]
    start: int
    end: int


def _tokenize(text: str) -> Iterator[tuple[str, str, int, int]]:
    """Yield (kind, value, start, end): kind is "(" | ")" | "atom" (value unquoted)."""
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c.isspace():
            i += 1
        elif c in "()":
            yield c, c, i, i + 1
            i += 1
        elif c == '"':
            j = i + 1
            buf: list[str] = []
            while j < n and text[j] != '"':
                if text[j] == "\\" and j + 1 < n:
                    j += 1
                buf.append(text[j])
                j += 1
            if j >= n:
                raise SandboxError("unterminated string in library table")
            yield "atom", "".join(buf), i, j + 1
            i = j + 1
        else:
            j = i
            while j < n and not text[j].isspace() and text[j] not in "()":
                j += 1
            yield "atom", text[i:j], i, j
            i = j


def _parse(text: str) -> _Node:
    stack: list[list] = []
    root: _Node | None = None
    for kind, value, start, end in _tokenize(text):
        if kind == "(":
            stack.append([start, None, []])
        elif kind == ")":
            if not stack:
                raise SandboxError("unbalanced ')' in library table")
            start_, head, children = stack.pop()
            node = _Node(head or "", tuple(children), start_, end)
            if stack:
                stack[-1][2].append(node)
            elif root is None:
                root = node
            else:
                raise SandboxError("more than one top-level form in library table")
        else:
            if not stack:
                raise SandboxError("atom outside of any form in library table")
            if stack[-1][1] is None:
                stack[-1][1] = value
            else:
                stack[-1][2].append(value)
    if stack or root is None:
        raise SandboxError("unbalanced '(' in library table")
    return root


def parse_lib_table(text: str) -> tuple[str, list[LibRow]]:
    """Return (head, rows) for a `*-lib-table` document; raises SandboxError when it does not parse.

    KiCad 10.0 requires every global table to exist AND parse (`LIBRARY_MANAGER::GlobalTablesValid`,
    claim 10), so parsing here is what guarantees the seed will not trigger the Setup wizard.
    """
    root = _parse(text)
    rows: list[LibRow] = []
    for child in root.children:
        if isinstance(child, _Node) and child.head == "lib":
            fields = {
                c.head: str(c.children[0]) for c in child.children if isinstance(c, _Node) and c.children
            }
            rows.append(
                LibRow(
                    name=fields.get("name", ""),
                    type=fields.get("type", ""),
                    uri=fields.get("uri", ""),
                    text=text[child.start : child.end],
                )
            )
    return root.head, rows


def _empty_table(head: str) -> str:
    """A minimal table KiCad 9/10 parse; `(version 7)` is the lib-table format version KiCad 10
    writes (spikes/verify-ipc-config-home/cfg10/10.0/design-block-lib-table seeded the GUI runs)."""
    return f"({head}\n\t(version 7)\n)\n"


def merge_lib_table(base_text: str, user_text: str, *, skip_table_uris: tuple[Path, ...] = ()) -> str:
    """Append rows of `user_text` that `base_text` lacks (by nickname) and return the merged table.

    ARCHITECTURE §6 seed algorithm / decision D7: stock template first, user rows merged read-only.
    A user row of type `Table` whose `uri` is one of `skip_table_uris` (the install's own template
    table, which KiCad 10's default user config nests) is dropped: its rows are already inline.
    """
    head, base_rows = parse_lib_table(base_text)
    user_head, user_rows = parse_lib_table(user_text)
    if user_head != head:
        raise SandboxError(f"cannot merge a {user_head!r} table into a {head!r} table")
    present = {r.name for r in base_rows}
    skip = {_norm_path(p) for p in skip_table_uris}
    extra: list[str] = []
    for row in user_rows:
        if row.name in present:
            continue
        if row.type.lower() == "table" and _norm_path(Path(row.uri)) in skip:
            continue
        present.add(row.name)
        extra.append(row.text)
    if not extra:
        return base_text
    closing = base_text.rstrip().rfind(")")
    body = base_text[:closing].rstrip("\r\n\t ")
    return body + "\n" + "\n".join("\t" + t for t in extra) + "\n)\n"


def _norm_path(p: Path) -> str:
    return os.path.normcase(str(p.resolve() if p.exists() else p)).replace("\\", "/")


# --------------------------------------------------------------------------------------------
# Hashing (the T6 never-modified assertion)
# --------------------------------------------------------------------------------------------


def tree_hash(path: Path) -> str:
    """SHA-256 over sorted (relative posix path, size, bytes) of every file under `path`.

    A missing directory hashes like an empty one so before/after comparisons work on machines
    without a user config for that version.
    """
    h = hashlib.sha256()
    if path.is_dir():
        files = sorted(p for p in path.rglob("*") if p.is_file())
        for f in files:
            rel = f.relative_to(path).as_posix().encode("utf-8")
            data = f.read_bytes()
            h.update(rel + b"\0" + str(len(data)).encode() + b"\0")
            h.update(data)
    return h.hexdigest()


def root_dirname(install: KiCadInstallation) -> str:
    """`<version>-<sha1(install.key())[:10]>`: the per-install directory name under each storage
    subtree. Keyed like `capability.cache_file` (version + root), not on the version alone."""
    digest = hashlib.sha1(install.key().encode("utf-8")).hexdigest()[:10]
    return f"{install.version}-{digest}"


def root_for(install: KiCadInstallation, storage: Path) -> tuple[Path, Path, Path]:
    """(KICAD_CONFIG_HOME, KICAD_DOCUMENTS_HOME, KICAD_CACHE_HOME) of `install` under `storage`."""
    name = root_dirname(install)
    return storage / "kicad-config" / name, storage / "kicad-documents" / name, storage / "kicad-cache" / name


def user_config_hash(install: KiCadInstallation) -> str:
    """SHA-256 over the user's real config dir for this major.minor (for the never-modified assertion)."""
    return tree_hash(current().user_kicad_config_home / install.major_minor)


# --------------------------------------------------------------------------------------------
# ConfigRoot
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ConfigRoot:
    install: KiCadInstallation
    path: Path  # the KICAD_CONFIG_HOME value (root); KiCad uses path/<major.minor>/
    documents_home: Path  # KICAD_DOCUMENTS_HOME (plugins, templates, 3rd-party)
    cache_home: Path  # KICAD_CACHE_HOME
    #: `environment.vars` of the user's `kicad_common.json` for this major.minor (D7, read-only),
    #: as sorted pairs so the dataclass stays hashable. KiCad lets a process env var shadow the
    #: same name in kicad_common.json, so `env()` must not export a `KICAD<major>_*` default
    #: over a user override.
    user_env_vars: tuple[tuple[str, str], ...] = ()
    #: The user's real `<Documents>/KiCad` (`Platform.user_kicad_documents_home`); read-only.
    user_documents_home: Path | None = None

    @property
    def versioned(self) -> Path:
        return self.path / self.install.major_minor

    @property
    def seed_dir(self) -> Path:
        return self.path / ".seed"

    @property
    def lock_path(self) -> Path:
        return self.path / ".lock"

    @property
    def prewarm_marker(self) -> Path:
        return self.path / ".prewarmed"

    @property
    def sandbox_third_party_dir(self) -> Path:
        """The sandbox's own (empty) 3rd-party dir: `<documents_home>/<major.minor>/3rdparty`."""
        return self.documents_home / self.install.major_minor / "3rdparty"

    @property
    def user_third_party_dir(self) -> Path | None:
        """The user's real PCM library dir for this version (`<Documents>/KiCad/<major.minor>/3rdparty`),
        or None when the user has none. Never created."""
        if self.user_documents_home is None:
            return None
        candidate = self.user_documents_home / self.install.major_minor / "3rdparty"
        return candidate if candidate.is_dir() else None

    @property
    def third_party_dir(self) -> Path:
        """Where `KICAD<major>_3RD_PARTY` points, in KiCad's own precedence:

        1. the user's `environment.vars` override of `KICAD<major>_3RD_PARTY` (kicad_common.json);
        2. the user's real default `<Documents>/KiCad/<major.minor>/3rdparty` when it exists —
           every PCM-installed library row in the merged tables is `${KICAD<major>_3RD_PARTY}/...`
           (D7: the merge is pointless if the variable points at an empty sandbox dir);
        3. the sandbox's own empty dir (`sandbox_third_party_dir`).
        """
        override = dict(self.user_env_vars).get(f"KICAD{self.install.major}_3RD_PARTY")
        if override:
            return Path(override)
        return self.user_third_party_dir or self.sandbox_third_party_dir

    def temp_dir(self, platform: Platform | None = None) -> Path:
        """Short per-root TEMP for KiCad processes: `<Platform.temp_dir>/k<digits>-<6 hex>`.

        The IPC server listens on `<GetTempDir()>/kicad/api.sock` and nng rejects pipe paths
        longer than ~128 chars (claim 10: 126 OK, 146 fails), so the id is kept to ~12 chars.
        Keying it on the root also gives each version its own pipe.
        """
        platform = platform or current()
        digest = hashlib.sha1(str(self.path).encode("utf-8")).hexdigest()[:6]
        version_digits = "".join(ch for ch in self.install.version if ch.isdigit())
        return platform.temp_dir / f"k{version_digits}-{digest}"

    def env(self, base: dict[str, str] | None = None) -> dict[str, str]:
        """Environment for a KiCad process: base (or os.environ) + KICAD_CONFIG_HOME/DOCUMENTS/CACHE
        + KICAD<major>_* library vars + short TEMP/TMP (Windows) so IPC socket paths stay short.

        Variable names follow `KICAD<major>_<suffix>` (docs/research/07, verified on 7–10); the
        install's own major is enough because ExpandEnvVarSubstitutions maps any other
        `KICAD<digits>_*` name to the running version's directories (8.0+; 3DMODEL on 7.0).

        A `KICAD<major>_*` name the user overrides in `environment.vars` is exported with the
        user's value (a process env var beats kicad_common.json in KiCad, so exporting the
        install default would silently clobber the override); `KICAD<major>_3RD_PARTY` follows
        `third_party_dir` (user override, else the user's real PCM dir, else the sandbox dir).
        """
        platform = current()
        env = dict(os.environ if base is None else base)
        env["KICAD_CONFIG_HOME"] = str(self.path)
        env["KICAD_DOCUMENTS_HOME"] = str(self.documents_home)
        env["KICAD_CACHE_HOME"] = str(self.cache_home)
        prefix = f"KICAD{self.install.major}_"
        overrides = dict(self.user_env_vars)
        for suffix, share_dir in _SHARE_ENV_SUFFIXES:
            env[prefix + suffix] = overrides.get(prefix + suffix) or str(self.install.dir(share_dir))
        env[prefix + "3RD_PARTY"] = overrides.get(prefix + "3RD_PARTY") or str(self.third_party_dir)
        if platform.is_windows:
            tmp = self.temp_dir(platform)
            tmp.mkdir(parents=True, exist_ok=True)
            env["TEMP"] = str(tmp)
            env["TMP"] = str(tmp)
        return env

    @contextmanager
    def lock(self, timeout_s: float = 300) -> Iterator[None]:
        """Serialise KiCad processes against this root (cross-process file lock).

        kicad-cli rewrites `kicad_common.json` on exit (claim 10; spikes/erc-json-scale/cfg*/),
        so two processes must never run against the same root at once. Windows uses
        `msvcrt.locking` on byte 0 of `<root>/.lock`, Linux `fcntl.flock`; both are released by the
        OS if the holder dies. Raises `TimeoutError` after `timeout_s`.
        """
        self.path.mkdir(parents=True, exist_ok=True)
        fh = self.lock_path.open("a+b")
        try:
            _acquire(fh, timeout_s, current(), self.lock_path)
            try:
                yield
            finally:
                _release(fh, current())
        finally:
            fh.close()

    def restore_seed(self) -> None:
        """Put `kicad_common.json` and the tables back to the seeded content (kicad-cli rewrites them).

        Also removes `fp-info-cache` and `*.lck` files KiCad may have left anywhere under the root.
        """
        if not self.seed_dir.is_dir():
            raise SandboxError(f"{self.seed_dir} missing: call kiwrap.sandbox.ensure() first")
        self.versioned.mkdir(parents=True, exist_ok=True)
        for name in SEED_FILES:
            src = self.seed_dir / name
            if src.is_file():
                _write_if_changed(self.versioned / name, src.read_bytes())
        for p in self.path.rglob("*"):
            if (
                p.is_file()
                and (p.name in _STALE_NAMES or p.suffix in _STALE_SUFFIXES)
                and p != self.lock_path
            ):
                p.unlink(missing_ok=True)

    def seed_intact(self) -> bool:
        """True when every live seeded file is byte-identical to its `.seed/` copy."""
        return all(
            (self.versioned / n).is_file()
            and (self.versioned / n).read_bytes() == (self.seed_dir / n).read_bytes()
            for n in SEED_FILES
            if (self.seed_dir / n).is_file()
        )


def _acquire(fh: BinaryIO, timeout_s: float, platform: Platform, lock_path: Path) -> None:
    """Non-blocking lock attempts with exponential back-off until `timeout_s` elapses.

    The locking modules are loaded through `importlib` because only one of `msvcrt` / `fcntl`
    exists per platform (and the platform seam, not `sys.platform`, decides which).
    """
    deadline = time.monotonic() + timeout_s
    delay = 0.02
    while True:
        try:
            if platform.is_windows:
                msvcrt = importlib.import_module("msvcrt")
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl = importlib.import_module("fcntl")
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"could not lock {lock_path} within {timeout_s} s") from None
            time.sleep(delay)
            delay = min(delay * 2, 0.25)


def _release(fh: BinaryIO, platform: Platform) -> None:
    try:
        if platform.is_windows:
            msvcrt = importlib.import_module("msvcrt")
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl = importlib.import_module("fcntl")
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass  # the OS drops the lock when the handle closes anyway


def _write_if_changed(path: Path, data: bytes) -> None:
    if path.is_file() and path.read_bytes() == data:
        return
    path.write_bytes(data)


def probe_version(kicad_cli: Path, *, timeout_s: float = 60.0) -> str:
    """`kicad-cli version --format plain` under a throw-away probe environment.

    Thin wrapper over the ONE prober, `kiwrap.installation.read_version` with
    `installation.probe_env(<temp dir>)`: all three of KICAD_CONFIG_HOME / KICAD_DOCUMENTS_HOME /
    KICAD_CACHE_HOME point under the temp dir, because even `version` makes kicad-cli create
    `<config>/<major.minor>/kicad_common.json` on exit (verified for 7.0.6-10.0.0) and 8.0.1 /
    9.0.5 / 10.0.0 also create `<cache>/KiCad/<ver>` and touch `<documents>/KiCad/<ver>`
    (finding: the old override of KICAD_CONFIG_HOME alone let those land in the user's real
    %LOCALAPPDATA% / Documents). Raises `SandboxError` when the binary cannot be run or prints no
    version. Prefer `installation.discover()` / `find()`; this exists for callers that already
    hold a kicad-cli path. Note: kicad-cli 7.0.6 writes crash-reporter state to
    `%LOCALAPPDATA%/kicad/7.0/sentry` on EVERY invocation regardless of any KICAD_*_HOME override
    (observed; not fixable from the environment) - %APPDATA%/kicad is still never touched.
    """
    from kiwrap.installation import probe_env, read_version  # sandbox sits above installation

    with tempfile.TemporaryDirectory(prefix="kiwrap-probe-") as tmp:
        env = probe_env(Path(tmp))
        found = read_version(Path(kicad_cli), _ProbeSystem(timeout_s), env)  # never raises
    if found is None:
        raise SandboxError(f"{kicad_cli} version failed or printed no version")
    return found[0]


class _ProbeSystem(System):
    """`System` whose `run` honours the caller's timeout (read_version uses its own default)."""

    def __init__(self, timeout_s: float) -> None:
        self._timeout_s = timeout_s

    def run(
        self, argv: Iterable[str], *, env: Mapping[str, str], timeout_s: float = 60, cwd: Path | None = None
    ) -> RunResult:
        return super().run(argv, env=env, timeout_s=min(timeout_s, self._timeout_s), cwd=cwd)


# --------------------------------------------------------------------------------------------
# ensure(): build the seed, write it, pre-warm
# --------------------------------------------------------------------------------------------


def ensure(
    install: KiCadInstallation,
    *,
    storage_home: Path | None = None,
    merge_user_tables: bool = True,
    enable_api_server: bool = False,
    prewarm: bool = True,
) -> ConfigRoot:
    """Create (idempotently) and return the sandbox root for `install`.

    `enable_api_server=True` is only used for live GUI sessions; kicad-cli roots keep it false.

    Steps (ARCHITECTURE §6 "Seed algorithm", claim 10 conditions (1)–(3)):
    1. Build `kicad_common.json` (flags + `environment.vars` from the user's file of this
       major.minor) and the three tables (template, plus user rows when `merge_user_tables`).
    2. Store them in `<root>/.seed/` and copy them into `<root>/<major.minor>/` (under the lock).
    3. Pre-warm once with `kicad-cli version`, retrying once, then mark `<root>/.prewarmed`.
    Calling it again re-derives the seed (the user may have added libraries) and restores it; a
    root is never deleted.
    """
    platform = current()
    storage = storage_home or platform.storage_home
    user_dir = platform.user_kicad_config_home / install.major_minor
    user_vars = _read_user_env_vars(user_dir / "kicad_common.json")
    config_home, documents_home, cache_home = root_for(install, storage)
    root = ConfigRoot(
        install=install,
        path=config_home,
        documents_home=documents_home,
        cache_home=cache_home,
        user_env_vars=tuple(sorted(user_vars.items())),
        user_documents_home=platform.user_kicad_documents_home,
    )
    try:
        for d in (
            root.versioned,
            root.seed_dir,
            root.documents_home,
            root.cache_home,
            root.sandbox_third_party_dir,  # never the user's real 3rdparty dir (read-only)
        ):
            d.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise SandboxError(f"cannot create sandbox root under {storage}: {e}") from e

    seeds = _build_seed(
        install, user_dir, merge_user_tables=merge_user_tables, enable_api_server=enable_api_server
    )

    with root.lock():
        for name, data in seeds.items():
            _write_if_changed(root.seed_dir / name, data)
        root.restore_seed()
    if prewarm and not root.prewarm_marker.is_file():
        _prewarm(root)
    return root


def _build_seed(
    install: KiCadInstallation,
    user_dir: Path,
    *,
    merge_user_tables: bool,
    enable_api_server: bool,
) -> dict[str, bytes]:
    seeds: dict[str, bytes] = {}
    seeds["kicad_common.json"] = _build_common_json(
        install, user_dir / "kicad_common.json", enable_api_server
    )
    template_dir = install.dir("template")
    for name, head in _TABLE_HEADS.items():
        template = template_dir / name
        text = template.read_text(encoding="utf-8") if template.is_file() else _empty_table(head)
        try:
            parse_lib_table(text)
        except SandboxError as e:
            raise SandboxError(f"{template} does not parse: {e}") from e
        user_table = user_dir / name
        if merge_user_tables and user_table.is_file():
            # A user table KiCad itself could not load must not break the sandbox (D7: read-only).
            with suppress(SandboxError, UnicodeDecodeError):
                text = merge_lib_table(
                    text, user_table.read_text(encoding="utf-8"), skip_table_uris=(template,)
                )
        seeds[name] = text.encode("utf-8")
    return seeds


def _build_common_json(install: KiCadInstallation, user_common: Path, enable_api_server: bool) -> bytes:
    """The minimal `kicad_common.json` that suppresses every first-run dialog (claim 10 (2)).

    Keys mirror spikes/versioning-strategy/cfgapi10/10.0/kicad_common.json, which started
    pcbnew 10.0.0 wizard-free with the API answering kipy. `environment.vars` and `meta` are taken
    from the user's own file for this major.minor when it exists (D7), otherwise left minimal
    (`vars: null` is exactly what KiCad writes for "no user variables").
    """
    user_vars = _read_user_env_vars(user_common) or None
    meta: dict | None = None
    if user_common.is_file():
        try:
            user = json.loads(user_common.read_text(encoding="utf-8"))
            if isinstance(user.get("meta"), dict):
                meta = {"filename": "kicad_common.json", **user["meta"]}
        except (OSError, ValueError, AttributeError):
            meta = None
    api: dict[str, object] = {"enable_server": bool(enable_api_server)}
    if install.python is not None:
        api["interpreter_path"] = str(install.python)
    doc: dict[str, object] = {
        "api": api,
        "do_not_show_again": {
            "data_collection_prompt": True,
            "update_check_prompt": True,
            "env_var_overwrite_warning": True,
            "scaled_3d_models_warning": True,
            "zone_fill_warning": True,
        },
        "environment": {"vars": user_vars},
        "system": {"first_run_shown": True},
    }
    if meta is not None:
        doc["meta"] = meta
    return (json.dumps(doc, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _read_user_env_vars(user_common: Path) -> dict[str, str]:
    """`environment.vars` of the user's `kicad_common.json` (empty when absent/unreadable; D7 read-only)."""
    if not user_common.is_file():
        return {}
    try:
        user = json.loads(user_common.read_text(encoding="utf-8"))
        vars_ = user.get("environment", {}).get("vars")
    except (OSError, ValueError, AttributeError):
        return {}
    if not isinstance(vars_, dict):
        return {}
    return {str(k): str(v) for k, v in vars_.items()}


def _prewarm(root: ConfigRoot) -> None:
    """Run `kicad-cli version` once against the root (retry once) and mark `.prewarmed`.

    8.0.1/9.0.5 segfaulted on the very first ERC against a brand-new root (ARCHITECTURE §6); a
    trivial command absorbs that, and the seed is restored afterwards because kicad-cli rewrites
    `kicad_common.json` on exit.
    """
    argv = [str(root.install.kicad_cli), "version", "--format", "plain"]
    last: subprocess.CompletedProcess[bytes] | None = None
    with root.lock():
        try:
            for _attempt in range(2):
                last = subprocess.run(
                    argv,
                    env=root.env(),
                    capture_output=True,
                    timeout=_PREWARM_TIMEOUT_S,
                    check=False,
                )
                if last.returncode == 0:
                    break
        except (OSError, subprocess.TimeoutExpired) as e:
            root.restore_seed()
            raise SandboxError(f"pre-warm of {root.path} failed: {e}") from e
        root.restore_seed()
    if last is None or last.returncode != 0:
        rc = last.returncode if last is not None else "?"
        err = last.stderr.decode("utf-8", "replace").strip() if last is not None else ""
        raise SandboxError(f"pre-warm of {root.path} failed twice (exit {rc}): {err}")
    root.prewarm_marker.write_text(f"{root.install.version}\n", encoding="utf-8")
