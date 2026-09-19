"""Layer 1 — `KiCadInstallation`: one installed KiCad and how to reach its binaries.

Discovery never runs anything except `kicad-cli version --format plain` (to read the semver).
It must work with zero installs (returns []) and never writes anywhere except kiwrap's own
storage home: the throwaway `KICAD_CONFIG_HOME` every probe runs with (`<storage>/probe-config`)
and, on Linux, the generated Flatpak wrappers / AppImage extraction (`kiwrap.platform.linux`).

Verified live on this machine (Windows 11; KiCad 7.0.6, 8.0.1, 9.0.5, 10.0.0 under
`C:/Program Files/KiCad/<major.minor>`; tests/test_installation.py):
- `kicad-cli version --format plain` prints exactly `7.0.6` / `8.0.1` / `9.0.5` / `10.0.0` on
  stdout with exit 0 on every version (the registry's DisplayVersion says '10.0.0-1', which is why
  it is not used). `version --format plain` runs with `KICAD_CONFIG_HOME` set creates
  `<KICAD_CONFIG_HOME>/<major.minor>/kicad_common.json` — i.e. even this command rewrites config
  on exit, so it must never see the user's real config root (docs/VERIFICATION.md claim 10).
- `KICAD_DOCUMENTS_HOME` and `KICAD_CACHE_HOME` are honoured by kicad-cli 7–10 as well (a
  `KiCad/` folder appears under each), so the probe environment sets all three.
- Registry: `HKLM\\...\\Uninstall\\KiCad 8.0|9.0|10.0` and `HKLM\\...\\WOW6432Node\\...\\KiCad 7.0`
  (see `kiwrap.platform.windows`).
Linux sources are designed only (see `kiwrap.platform.linux`) and covered by fake-System tests.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from kiwrap.platform import Platform, current
from kiwrap.platform.discovery import Candidate, explicit_candidate, find_candidates
from kiwrap.platform.system import System

# Set by `discover()`; see kiwrap/data/kicad_version_matrix.json for what is known per version.
SOURCES = ("explicit", "registry", "program_files", "path", "apt", "flatpak", "snap", "appimage")

PROBE_CONFIG_DIRNAME = "probe-config"
_SEMVER_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")
_VERSION_TIMEOUT_S = 60


@dataclass(frozen=True)
class KiCadInstallation:
    root: Path  # e.g. C:/Program Files/KiCad/10.0 or /usr (distro) or the flatpak sandbox root
    kicad_cli: Path  # absolute path to kicad-cli(.exe); for flatpak a wrapper script kiwrap generates
    share: Path  # <root>/share/kicad on Windows; /usr/share/kicad on distro Linux
    version: str  # full semver as printed by `kicad-cli version --format plain`, e.g. "10.0.0"
    source: str  # one of SOURCES
    python: Path | None = None  # interpreter that can `import pcbnew` for THIS install; None if unknown
    python_dll_dir: Path | None = None  # Windows: <root>/bin (must be on PATH / add_dll_directory)
    extra: dict[str, str] = field(default_factory=dict)  # e.g. {"flatpak_id": "org.kicad.KiCad"}

    @property
    def major_minor(self) -> str:
        parts = self.version.split(".")
        return f"{parts[0]}.{parts[1]}" if len(parts) >= 2 else self.version

    @property
    def major(self) -> int:
        return int(self.version.split(".")[0])

    @property
    def version_tuple(self) -> tuple[int, ...]:
        return version_tuple(self.version)

    def dir(self, kind: str) -> Path:
        """`symbols` | `footprints` | `3dmodels` | `template` | `schemas` | `demos` under `share`."""
        return self.share / kind

    def key(self) -> str:
        """Stable cache key: full semver + root."""
        return f"{self.version}@{self.root}"


def version_tuple(version: str) -> tuple[int, ...]:
    """'10.0.6' -> (10, 0, 6); tolerant of trailing text ('10.0.0-1' -> (10, 0, 0))."""
    m = _SEMVER_RE.search(version)
    if m:
        return tuple(int(g) for g in m.groups())
    return tuple(int(p) for p in version.split(".") if p.isdigit())


def normalise_version(text: str) -> str | None:
    """First `X.Y.Z` in the output of `kicad-cli version --format plain`, or None.

    Release builds print the bare semver; nightlies print e.g. `10.99.0-unknown-abcdef` and some
    packagers append a revision (`10.0.0-1`), so everything after the semver is dropped."""
    m = _SEMVER_RE.search(text)
    return ".".join(m.groups()) if m else None


def probe_env(storage_home: Path, base: Mapping[str, str] | None = None) -> dict[str, str]:
    """Environment for throwaway kicad-cli runs (version read, `--help` probes).

    Points `KICAD_CONFIG_HOME`, `KICAD_DOCUMENTS_HOME` and `KICAD_CACHE_HOME` under
    `<storage_home>/probe-config` so nothing kicad-cli writes on exit lands in the user's real
    config (hard rule; conformance T6 and tests/test_installation.py assert the %APPDATA% hash).
    """
    env = dict(os.environ if base is None else base)
    probe = storage_home / PROBE_CONFIG_DIRNAME
    env["KICAD_CONFIG_HOME"] = str(probe / "config")
    env["KICAD_DOCUMENTS_HOME"] = str(probe / "documents")
    env["KICAD_CACHE_HOME"] = str(probe / "cache")
    return env


def read_version(kicad_cli: Path, system: System, env: Mapping[str, str]) -> tuple[str, str] | None:
    """Run `kicad-cli version --format plain`; return (semver, raw stdout) or None if it fails."""
    result = system.run(
        [str(kicad_cli), "version", "--format", "plain"], env=env, timeout_s=_VERSION_TIMEOUT_S
    )
    if not result.ok:
        return None
    version = normalise_version(result.stdout)
    return (version, result.stdout.strip()) if version else None


def discover(
    explicit: list[Path] | tuple[Path, ...] = (),
    *,
    platform: Platform | None = None,
    system: System | None = None,
    storage_home: Path | None = None,
) -> list[KiCadInstallation]:
    """Find every KiCad install on this machine, newest first, de-duplicated by root.

    Sources, in order: `explicit` roots or kicad-cli paths (settings), the platform's native
    package registry (Windows Uninstall keys incl. WOW6432Node for 7.x; Linux distro packages),
    well-known directories (C:/Program Files/KiCad/*; /usr, /usr/local, /opt/kicad*), Flatpak
    (`org.kicad.KiCad`), Snap, AppImage on PATH, and finally `kicad-cli` on PATH.

    The first source that names a root wins (so an explicit root keeps source "explicit" even
    when the registry also lists it). `platform`, `system` and `storage_home` are injectable for
    tests; defaults are the real machine and `Platform.storage_home`.
    """
    platform = platform or current()
    system = system or System()
    storage_home = storage_home or platform.storage_home
    env = probe_env(storage_home, system.environ())

    candidates: list[Candidate] = []
    for spec in explicit:
        cand = explicit_candidate(Path(spec), platform, system)
        if cand is not None:
            candidates.append(cand)
    candidates.extend(find_candidates(platform, system, storage_home))

    seen: set[Path] = set()
    installs: list[KiCadInstallation] = []
    for cand in candidates:
        root_key = _norm(cand.root)
        if root_key in seen:
            continue
        found = read_version(cand.kicad_cli, system, env)
        if found is None:
            continue
        seen.add(root_key)
        version, raw = found
        extra = dict(cand.extra)
        if raw != version:
            extra["version_raw"] = raw
        installs.append(
            KiCadInstallation(
                root=cand.root,
                kicad_cli=cand.kicad_cli,
                share=cand.share,
                version=version,
                source=cand.source,
                python=cand.python,
                python_dll_dir=cand.python_dll_dir,
                extra=extra,
            )
        )
    installs.sort(key=lambda i: (i.version_tuple, str(i.root)), reverse=True)
    return installs


def _norm(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path


def find(
    spec: str | None = None,
    installs: list[KiCadInstallation] | None = None,
    *,
    probe_paths: bool = True,
) -> KiCadInstallation:
    """Pick one install. `spec` is None (newest), "10", "10.0", "10.0.6", or a root/kicad-cli path.

    A numeric spec matches by prefix of the version components ("10" -> any 10.x.y, "10.0" ->
    any 10.0.y, "10.0.6" -> exactly 10.0.6); the newest match wins. A path spec matches an
    install whose root or kicad-cli equals it; when nothing discovered matches and `probe_paths`
    is true, the path is probed directly (so `find("D:/kicad-nightly")` works without settings).
    Callers whose `spec` comes from an untrusted source (the MCP tools: an LLM argument that file
    content can steer) pass `probe_paths=False`, so only a KiCad that discovery already found is
    ever executed.

    Raises `NoKiCadError` with the list of installs that *were* found.
    """
    if installs is None:
        installs = discover()
    if spec is None or spec.strip() == "":
        if installs:
            return installs[0]
        raise NoKiCadError(spec, installs)
    spec = spec.strip()
    if re.fullmatch(r"\d+(\.\d+){0,2}", spec):
        want = tuple(int(p) for p in spec.split("."))
        for inst in installs:  # already newest first
            if inst.version_tuple[: len(want)] == want:
                return inst
        raise NoKiCadError(spec, installs)
    path = _norm(Path(spec))
    for inst in installs:
        if path in (_norm(inst.root), _norm(inst.kicad_cli), _norm(inst.kicad_cli.parent)):
            return inst
    if probe_paths:
        probed = discover([Path(spec)])
        for inst in probed:
            if inst.source == "explicit":
                return inst
    raise NoKiCadError(spec, installs)


class NoKiCadError(RuntimeError):
    def __init__(self, spec: str | None, found: list[KiCadInstallation]):
        self.spec, self.found = spec, found
        super().__init__(f"no KiCad matching {spec!r}; found: {[i.version for i in found]}")


# --- human-readable summary (used by `python -m kiwrap.installation` and `kiwrap env`) -------------

DESCRIBE_KEYS = (
    ("erc", "cli.has_erc"),
    ("drc", "cli.has_drc"),
    ("render", "cli.has_render"),
    ("jobset", "cli.has_jobset"),
    ("upgrade", "cli.has_sch_pcb_upgrade"),
    ("swig", "swig.available"),
    ("ipc", "ipc.available"),
)


def describe(
    installs: list[KiCadInstallation] | None = None,
    caps_for: Callable[[KiCadInstallation], Mapping[str, object]] | None = None,
) -> str:
    """Plain-text table of installs; with `caps_for` (e.g. `kiwrap.capability.capabilities_for`)
    a second column block shows the key capabilities as y/n."""
    if installs is None:
        installs = discover()
    if not installs:
        return "no KiCad installation found"
    rows: list[list[str]] = []
    header = ["version", "source", "root", "python", "share"]
    if caps_for is not None:
        header += [label for label, _ in DESCRIBE_KEYS]
    for inst in installs:
        row = [inst.version, inst.source, str(inst.root), str(inst.python or "-"), str(inst.share)]
        if caps_for is not None:
            caps = caps_for(inst)
            row += ["y" if caps.get(key) else "n" for _, key in DESCRIBE_KEYS]
        rows.append(row)
    widths = [max(len(r[i]) for r in [header, *rows]) for i in range(len(header))]
    lines = [
        "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(header)),
        "  ".join("-" * w for w in widths),
    ]
    lines += ["  ".join(cell.ljust(widths[i]) for i, cell in enumerate(r)) for r in rows]
    return "\n".join(lines)


def _main(argv: list[str]) -> int:
    """`python -m kiwrap.installation [--no-probe] [--refresh] [root-or-kicad-cli ...]`."""
    import importlib

    probe = "--no-probe" not in argv
    refresh = "--refresh" in argv
    explicit = [Path(a) for a in argv if not a.startswith("--")]
    installs = discover(explicit)
    caps_for: Callable[[KiCadInstallation], Mapping[str, object]] | None = None
    if probe:
        # kiwrap.capability sits above this layer (import-linter: capability -> installation), so
        # the script entry point resolves it at run time only; library code never does this.
        capability = importlib.import_module("kiwrap.capability")
        caps_for = lambda inst: capability.capabilities_for(inst, refresh=refresh)  # noqa: E731

    print(describe(installs, caps_for))
    return 0 if installs else 1


if __name__ == "__main__":  # pragma: no cover
    import sys

    sys.exit(_main(sys.argv[1:]))
