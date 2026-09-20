"""Where the managed engines live and whether they are usable.

`scripts/bootstrap.ps1` (Windows, D18) downloads the pinned engines of `scripts/engines.json`
into kiwrap's storage home (docs/DECISIONS.md D11; GPL-3.0 engines are never redistributed, D8):

    <storage>/engines/jre/<release top dir>/bin/java.exe     Eclipse Temurin JRE 25 (Freerouting
                                                             2.4.1 is class-file 69; Java 8/11/17 fail
                                                             with UnsupportedClassVersionError -
                                                             docs/VERIFICATION.md claim 8)
    <storage>/engines/jre/installed.json                     {release_name, asset, sha256, installed}
    <storage>/engines/freerouting/freerouting-<version>.jar
    <storage>/freerouting/freerouting.json                   the profile bootstrap wrote: analytics,
                                                             telemetry, contact and the GitHub update
                                                             check (blackholed through network.proxy_url)
                                                             off; `gui.enabled` false

`discover_engines()` only looks at the file system (no process is started): `kw_env` reports the
same directories as `engines`, and the router starts the JVM once per route. `KIWRAP_JAVA` /
`KIWRAP_FREEROUTING_JAR` override the locations for a developer who keeps them elsewhere;
`KIWRAP_ENGINES_HOME` names another storage home to look in. With `fallback=True` (what the
tools pass) a storage home without engines - a `--storage-home` override, a pytest session's
throw-away `KIWRAP_HOME` - falls back to the platform's default storage home, where bootstrap
installs them.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from kiwrap.platform import current

ENGINES_DIRNAME = "engines"
JRE_DIRNAME = "jre"
FREEROUTING_DIRNAME = "freerouting"
PROFILE_DIRNAME = "freerouting"
PROFILE_FILENAME = "freerouting.json"
BOOTSTRAP_HINT = (
    "run scripts/bootstrap.ps1 (it downloads the pinned JRE 25 + Freerouting 2.4.1 into the storage home)"
)

_JAR_RE = re.compile(r"^freerouting-(\d+(?:\.\d+)*)\.jar$", re.IGNORECASE)


class EngineMissing(RuntimeError):
    """The managed JRE or the Freerouting jar is not installed (or not where bootstrap puts it).
    `which` names the missing piece; `fix` is what the tools show the agent."""

    def __init__(self, which: str, looked_in: Path, *, fix: str = BOOTSTRAP_HINT):
        self.which, self.looked_in, self.fix = which, Path(looked_in), fix
        super().__init__(f"{which} not found under {looked_in}; {fix}")


@dataclass(frozen=True)
class Engines:
    """The resolved engine locations (any of the paths may be None when absent)."""

    storage_home: Path
    java: Path | None
    jar: Path | None
    profile: Path | None
    jre_release: str = ""  # e.g. "jdk-25.0.4.1+1" (from installed.json or the directory name)
    freerouting_version: str = ""  # from the jar file name

    @property
    def available(self) -> bool:
        return self.java is not None and self.jar is not None

    def require(self) -> Engines:
        """Raise `EngineMissing` unless both the JRE and the jar are present."""
        if self.java is None:
            raise EngineMissing(
                "the managed JRE (bin/java)", self.storage_home / ENGINES_DIRNAME / JRE_DIRNAME
            )
        if self.jar is None:
            raise EngineMissing(
                "the Freerouting jar", self.storage_home / ENGINES_DIRNAME / FREEROUTING_DIRNAME
            )
        return self

    def to_dict(self) -> dict[str, object]:
        return {
            "available": self.available,
            "java": str(self.java) if self.java else None,
            "jre_release": self.jre_release or None,
            "freerouting_jar": str(self.jar) if self.jar else None,
            "freerouting_version": self.freerouting_version or None,
            "profile": str(self.profile) if self.profile else None,
        }


def _version_key(text: str) -> tuple[int, ...]:
    return tuple(int(p) for p in re.findall(r"\d+", text))


def find_java(jre_dir: Path) -> tuple[Path | None, str]:
    """`<jre_dir>/<release>/bin/java[.exe]` (the newest release directory when several exist)
    and the release name (`installed.json`, else the directory name)."""
    plat = current()
    exe = "java" + plat.exe_suffix
    if not jre_dir.is_dir():
        return None, ""
    candidates = sorted(
        (p for p in jre_dir.iterdir() if p.is_dir() and (p / "bin" / exe).is_file()),
        key=lambda p: _version_key(p.name),
    )
    if not candidates:
        return None, ""
    top = candidates[-1]
    release = top.name
    marker = jre_dir / "installed.json"
    if marker.is_file():
        try:
            doc = json.loads(marker.read_text(encoding="utf-8"))
            release = str(doc.get("release_name") or release)
        except (OSError, ValueError):
            pass
    return top / "bin" / exe, release


def find_jar(fr_dir: Path) -> tuple[Path | None, str]:
    """The newest `freerouting-<version>.jar` in `fr_dir` and its version."""
    if not fr_dir.is_dir():
        return None, ""
    jars: list[tuple[tuple[int, ...], Path, str]] = []
    for p in fr_dir.iterdir():
        m = _JAR_RE.match(p.name)
        if m and p.is_file():
            jars.append((_version_key(m.group(1)), p, m.group(1)))
    if not jars:
        return None, ""
    jars.sort()
    _key, path, version = jars[-1]
    return path, version


def discover_engines(storage_home: Path | None = None, *, fallback: bool = False) -> Engines:
    """Locate the managed engines under `storage_home` (default: the platform's storage home).
    Environment overrides: `KIWRAP_JAVA` (a java executable), `KIWRAP_FREEROUTING_JAR` and
    `KIWRAP_ENGINES_HOME` (another storage home holding `engines/`). With `fallback`, a home
    without both engines is retried at `KIWRAP_ENGINES_HOME` and the platform's default storage
    home (`Platform.default_storage_home`); the first home that has them wins."""
    home = Path(storage_home) if storage_home is not None else current().storage_home
    found = _discover_in(home)
    if found.available or not fallback:
        return found
    alternatives: list[Path] = []
    env_home = os.environ.get("KIWRAP_ENGINES_HOME")
    if env_home:
        alternatives.append(Path(env_home))
    default_home = current().default_storage_home
    if default_home is not None:
        alternatives.append(Path(default_home))
    for alt in alternatives:
        if alt.resolve() == home.resolve():
            continue
        other = _discover_in(alt)
        if other.available:
            return other
    return found


def _discover_in(home: Path) -> Engines:
    engines_dir = home / ENGINES_DIRNAME
    java, release = find_java(engines_dir / JRE_DIRNAME)
    jar, version = find_jar(engines_dir / FREEROUTING_DIRNAME)
    env_java = os.environ.get("KIWRAP_JAVA")
    if env_java and Path(env_java).is_file():
        java, release = Path(env_java), release or "KIWRAP_JAVA"
    env_jar = os.environ.get("KIWRAP_FREEROUTING_JAR")
    if env_jar and Path(env_jar).is_file():
        jar = Path(env_jar)
        m = _JAR_RE.match(jar.name)
        version = m.group(1) if m else version
    profile = home / PROFILE_DIRNAME / PROFILE_FILENAME
    return Engines(
        storage_home=home,
        java=java,
        jar=jar,
        profile=profile if profile.is_file() else None,
        jre_release=release,
        freerouting_version=version,
    )


__all__ = ["BOOTSTRAP_HINT", "EngineMissing", "Engines", "discover_engines", "find_jar", "find_java"]
