"""Build the pinned kicad-python wheel ONCE, from a shallow + sparse clone, and repair it.

Why (reviewer findings 5 / 6, 2026-09-19): `kicad-python` is pinned to a git commit (no 0.9 wheel
exists on any index) and building it from git makes uv clone the KiCad source tree as a full
submodule - 3.5 GB (2.2 GB .git, 58 179 commits) per cold `uv sync`, in every CI job - only to
read `api/proto/**/*.proto`. The build also needs protoc-wheel-0 / protoletariat / mypy-protobuf
at build time, its `git describe --long` fails in that clone (KICAD_API_VERSION = "unknown", so
kipy's `check_version()` is skipped), and on Windows protoletariat's import rewrite is a silent
no-op (kiwrap.backends.ipc explains the two bugs), leaving `*_pb2.py` importing bare `common.*` /
`board.*` packages.

What this script does:
1. `git fetch --depth 1` of the pinned kicad-python commit (no history) and a blobless, sparse
   checkout of the KiCad submodule limited to `api/proto` and `cmake/KiCadVersion.cmake`
   (`--filter=blob:none` + `sparse-checkout`: tens of MB instead of 3.5 GB).
2. `uv build --wheel` in that clone (the upstream build backend, unchanged). The wheel is located
   from uv's own "Successfully built <path>" line (resolved against the clone, uv's cwd), falling
   back to a search of the output directory and the clone: CI run 36013216332 (2026-09-24) passed
   the relative `--out wheelhouse`, uv resolved it against the clone and wrote
   `<clone>/wheelhouse/kicad_python-0.9.0.dev0-cp311-cp311-linux_x86_64.whl`, and the old
   `glob(out)` in the repository found nothing ("uv build produced no kicad_python wheel").
   `--out` is now resolved to an absolute path before anything runs.
3. Repairs the wheel: rewrites the generated modules' bare `from common.types import ...` /
   `import board.board_types_pb2` into `kipy.proto.*` imports (what protoletariat does on Linux),
   and the `.pyi` stubs' `import common.types.x_pb2` into protoletariat's `from .. import common`
   (so a Windows-built wheel type-checks like the Linux-built CI wheel: CI's mypy saw the
   jobs.py `ValueType` error a local Windows mypy could not, 2026-09-25), stamps
   `kipy/kicad_api_version.py` with `<KICAD_SEMANTIC_VERSION>-<submodule sha>` when the build
   left "unknown", retags the pure-Python wheel `py3-none-any` (poetry-core's `build.py`
   path emits a platform tag such as `cp314-cp314-win_amd64` / `cp311-cp311-linux_x86_64`
   although the wheel holds no extension: verified 2026-09-19 and 2026-09-24) and rewrites RECORD.
   The "fatal: No names found, cannot describe anything." line in the build log is upstream
   build.py's `git describe --long` in the tagless shallow submodule; the stamp replaces its
   "unknown" result.
4. Leaves exactly ONE `kicad_python-*.whl` in the output directory (stale wheels from earlier runs
   are removed: `scripts/ci/sync_with_wheelhouse.sh` installs `wheelhouse/kicad_python-*.whl`),
   writes `<wheel>.sha256` and `kicad_python.pin` (`<url>@<sha>`, the commit it was built from).
5. Verifies the result in a throw-away venv: `from kipy import KiCad` and the proto imports work
   WITHOUT kiwrap's .pth site hook.

`--check DIR` (no network) validates a wheelhouse - built here or restored from the CI cache -
against pyproject.toml and uv.lock: one pure wheel, its sha256 file, the pin file equal to
pyproject's direct reference (and to `--expect-pin` / `$KICAD_PYTHON_PIN` when given), the wheel's
version equal to uv.lock's kicad-python version, and uv.lock's git source at the same commit.

Usage: `python scripts/ci/build_kicad_python_wheel.py --out wheelhouse` (network needed; any
Python 3.11+, uv on PATH); `--skip-verify` for environments without `uv venv`;
`python scripts/ci/build_kicad_python_wheel.py --check wheelhouse`. The pin and URL default to
pyproject.toml's.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DEFAULT_URL = "https://gitlab.com/kicad/code/kicad-python.git"
PIN_FILE = "kicad_python.pin"
WHEEL_GLOB = "kicad_python-*.whl"
_PIN_RE = re.compile(r"kicad-python @ git\+(?P<url>[^@\s\"]+)@(?P<sha>[0-9a-f]{40})")
_BARE_PKGS = ("common", "board", "schematic")
_FROM_RE = re.compile(r"^(\s*)from (common|board|schematic)((?:\.\w+)*) import ", re.MULTILINE)
_IMPORT_RE = re.compile(r"^(\s*)import (common|board|schematic)\.", re.MULTILINE)
# uv's final build line: "Successfully built wheelhouse/kicad_python-0.9.0.dev0-cp311-cp311-linux_x86_64.whl"
# (one line per artifact; the path is relative to uv's cwd when --out-dir was relative).
_BUILT_RE = re.compile(r"^Successfully built (?P<path>.+?\.whl)\s*$", re.MULTILINE)
_LOCK_ENTRY_RE = re.compile(
    r'^\[\[package\]\]\s*\nname = "kicad-python"\s*\nversion = "(?P<version>[^"]+)"\s*\n'
    r"source = \{ git = \"(?P<source>[^\"]+)\" \}",
    re.MULTILINE,
)


def pinned(pyproject: Path | None = None) -> tuple[str, str]:
    """(url, sha) from pyproject.toml's direct reference."""
    m = _PIN_RE.search((pyproject or REPO / "pyproject.toml").read_text(encoding="utf-8"))
    if not m:
        raise SystemExit("pyproject.toml has no `kicad-python @ git+...@<sha>` dependency")
    return m.group("url"), m.group("sha")


def locked(lock: Path | None = None) -> tuple[str, str]:
    """(version, git source) of kicad-python in uv.lock."""
    m = _LOCK_ENTRY_RE.search((lock or REPO / "uv.lock").read_text(encoding="utf-8"))
    if not m:
        raise SystemExit("uv.lock has no git-sourced kicad-python package entry")
    return m.group("version"), m.group("source")


def run(
    args: list[str], cwd: Path | None = None, capture_output: bool = False
) -> subprocess.CompletedProcess[str]:
    print("+", " ".join(args), flush=True)
    return subprocess.run(
        args, cwd=str(cwd) if cwd else None, check=True, text=True, capture_output=capture_output
    )


def shallow_clone(url: str, sha: str, dest: Path) -> None:
    """One commit of kicad-python, no history."""
    dest.mkdir(parents=True, exist_ok=True)
    run(["git", "init", "-q"], dest)
    run(["git", "remote", "add", "origin", url], dest)
    run(["git", "fetch", "-q", "--depth", "1", "origin", sha], dest)
    run(["git", "checkout", "-q", "FETCH_HEAD"], dest)


def sparse_submodule(clone: Path) -> str:
    """Check out only `api/proto` and `cmake/KiCadVersion.cmake` of the KiCad submodule at the
    commit kicad-python pins; returns that commit."""
    tree = run(["git", "ls-tree", "HEAD", "kicad"], clone, capture_output=True).stdout.split()
    sub_sha = tree[2]
    url = run(
        ["git", "config", "-f", ".gitmodules", "submodule.kicad.url"], clone, capture_output=True
    ).stdout.strip()
    sub = clone / "kicad"
    if sub.exists():
        shutil.rmtree(sub, ignore_errors=True)
    sub.mkdir()
    run(["git", "init", "-q"], sub)
    run(["git", "remote", "add", "origin", url], sub)
    run(["git", "config", "core.sparseCheckout", "true"], sub)
    run(["git", "sparse-checkout", "set", "--no-cone", "api/proto", "cmake/KiCadVersion.cmake"], sub)
    run(["git", "fetch", "-q", "--depth", "1", "--filter=blob:none", "origin", sub_sha], sub)
    run(["git", "checkout", "-q", "FETCH_HEAD"], sub)
    if not (sub / "api" / "proto").is_dir():
        raise SystemExit("sparse checkout of kicad/api/proto failed")
    return sub_sha


def semantic_version(clone: Path) -> str:
    text = (clone / "kicad" / "cmake" / "KiCadVersion.cmake").read_text(encoding="utf-8", errors="replace")
    m = re.search(r'KICAD_SEMANTIC_VERSION\s+"([^"]+)"', text)
    return m.group(1).replace("-unknown", "") if m else "0.0.0"


def _uv() -> list[str]:
    """`uv` on PATH (setup-uv in CI, the developer's uv locally), else the `uv` module of this Python."""
    exe = shutil.which("uv")
    if exe:
        return [exe]
    try:
        import uv  # noqa: F401
    except ImportError as exc:
        raise SystemExit("uv is neither on PATH nor importable: install uv first") from exc
    return [sys.executable, "-m", "uv"]


def remove_stale(out: Path) -> list[Path]:
    """Delete earlier kicad_python wheels (+ their .sha256) and the pin file from `out`: the
    wheelhouse must hold exactly the wheel this run builds."""
    stale = [
        p for p in (*out.glob(WHEEL_GLOB), *out.glob(WHEEL_GLOB + ".sha256"), out / PIN_FILE) if p.is_file()
    ]
    for p in stale:
        print(f"removing stale {p}", flush=True)
        p.unlink()
    return stale


def find_built_wheel(log: str, cwd: Path, out: Path, since: float) -> Path:
    """The kicad_python wheel `uv build` just produced.

    1. uv's "Successfully built <path>" lines, relative paths resolved against `cwd` (uv's working
       directory, the clone) - the line CI run 36013216332 printed for the wheel the old glob missed.
    2. Otherwise any `kicad_python-*.whl` modified at or after `since` in `out`, then anywhere
       under `cwd` (newest first).
    Raises SystemExit naming every place searched when nothing is found."""
    for m in _BUILT_RE.finditer(log):
        raw = Path(m.group("path").strip().strip("'\""))
        path = raw if raw.is_absolute() else cwd / raw
        if path.name.startswith("kicad_python-") and path.is_file():
            return path.resolve()
    fresh: list[Path] = []
    for root, pattern in ((out, WHEEL_GLOB), (cwd, "**/" + WHEEL_GLOB)):
        if root.is_dir():
            fresh = [p for p in root.glob(pattern) if p.is_file() and p.stat().st_mtime >= since - 2]
        if fresh:
            return max(fresh, key=lambda p: p.stat().st_mtime).resolve()
    tail = "\n".join(log.strip().splitlines()[-15:])
    raise SystemExit(
        f"uv build produced no kicad_python wheel: no 'Successfully built ...whl' line naming an existing "
        f"file, and no fresh {WHEEL_GLOB} under {out} or {cwd}.\nlast build output:\n{tail}"
    )


def build(clone: Path, out: Path) -> Path:
    """`uv build --wheel` in the clone with an ABSOLUTE `--out-dir`; returns the wheel, moved into
    `out` if uv left it elsewhere."""
    out = out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    args = [*_uv(), "build", "--wheel", "--out-dir", str(out)]
    print("+", " ".join(args), f"(cwd {clone})", flush=True)
    print(
        "  (a 'fatal: No names found, cannot describe anything.' line is upstream build.py's git describe",
        flush=True,
    )
    print(
        "   in the tagless shallow submodule; the KICAD_API_VERSION stamp below replaces its result)",
        flush=True,
    )
    since = time.time()
    proc = subprocess.run(
        args, cwd=str(clone), capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    sys.stdout.write(proc.stdout)
    sys.stdout.write(proc.stderr)
    sys.stdout.flush()
    if proc.returncode != 0:
        raise SystemExit(f"uv build failed with exit code {proc.returncode}")
    wheel = find_built_wheel(proc.stdout + "\n" + proc.stderr, clone, out, since)
    if wheel.parent != out:
        print(f"uv wrote the wheel to {wheel}; moving it into {out}", flush=True)
        wheel = Path(shutil.move(str(wheel), str(out / wheel.name)))
    return wheel


def rewrite_imports(text: str) -> tuple[str, int]:
    """protoc's absolute imports -> `kipy.proto.*` (protoletariat's job, skipped on Windows)."""
    count = 0

    def sub_from(m: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return f"{m.group(1)}from kipy.proto.{m.group(2)}{m.group(3)} import "

    def sub_import(m: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return f"{m.group(1)}import kipy.proto.{m.group(2)}."

    text = _FROM_RE.sub(sub_from, text)
    text = _IMPORT_RE.sub(sub_import, text)
    return text, count


_STUB_IMPORT_RE = re.compile(r"^import (common|board|schematic)(?:\.\w+)+[ \t]*\r?$")


def rewrite_stub_imports(text: str, depth: int) -> tuple[str, int]:
    """mypy-protobuf's `.pyi` stubs: `import common.types.base_types_pb2` -> `from .. import common`
    (one line per package, dots = 1 + the stub's directory depth below `kipy/proto`), exactly what
    protoletariat writes on Linux (verified 2026-09-25 on the CI-built wheel: `from .. import board`
    in `kipy/proto/board/board_jobs_pb2.pyi`). Without it a Windows-built wheel's stubs leave the
    proto types unresolved (Any) and a local mypy passes code the Linux-built CI wheel rejects."""
    dots = "." * (depth + 1)
    seen: set[str] = set()
    count = 0
    lines: list[str] = []
    for line in text.splitlines(keepends=True):
        m = _STUB_IMPORT_RE.match(line)
        if m is None:
            lines.append(line)
            continue
        count += 1
        new = f"from {dots} import {m.group(1)}"
        if new not in seen:  # one binding per package is enough
            seen.add(new)
            lines.append(new + ("\n" if line.endswith("\n") else ""))
    return "".join(lines), count


PURE_TAG = "py3-none-any"


def repair_wheel(wheel: Path, stamp: str) -> tuple[Path, int, bool]:
    """Rewrite the proto imports in place, stamp KICAD_API_VERSION and retag the wheel pure;
    returns (wheel path, imports rewritten, stamped)."""
    tmp = wheel.with_suffix(".tmp")
    rewritten = 0
    stamped = False
    with zipfile.ZipFile(wheel) as src, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as dst:
        record_name = next(n for n in src.namelist() if n.endswith(".dist-info/RECORD"))
        wheel_name = next(n for n in src.namelist() if n.endswith(".dist-info/WHEEL"))
        entries: dict[str, bytes] = {n: src.read(n) for n in src.namelist()}
        if any(n.endswith((".pyd", ".so", ".dll", ".dylib")) for n in entries):
            raise SystemExit("the kicad-python wheel carries an extension module: not retagging it")
        wheel_meta = entries[wheel_name].decode("utf-8")
        wheel_meta = re.sub(r"^Root-Is-Purelib: .*$", "Root-Is-Purelib: true", wheel_meta, flags=re.MULTILINE)
        wheel_meta = re.sub(r"^Tag: .*$", f"Tag: {PURE_TAG}", wheel_meta, flags=re.MULTILINE)
        entries[wheel_name] = wheel_meta.encode("utf-8")
        for name, data in list(entries.items()):
            if name.startswith("kipy/proto/") and name.endswith("_pb2.py"):
                text, n = rewrite_imports(data.decode("utf-8"))
                if n:
                    rewritten += n
                    entries[name] = text.encode("utf-8")
            elif name.startswith("kipy/proto/") and name.endswith("_pb2.pyi"):
                depth = name.count("/") - 2  # kipy/proto/board/x.pyi -> 1
                text, n = rewrite_stub_imports(data.decode("utf-8"), depth)
                if n:
                    rewritten += n
                    entries[name] = text.encode("utf-8")
            elif name == "kipy/kicad_api_version.py" and 'KICAD_API_VERSION = "unknown"' in data.decode(
                "utf-8"
            ):
                entries[name] = (
                    f'# This file is automatically generated, do not modify it\nKICAD_API_VERSION = "{stamp}"\n'
                ).encode()
                stamped = True
        lines = []
        for name, data in entries.items():
            if name == record_name:
                continue
            digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode("ascii")
            lines.append(f"{name},sha256={digest},{len(data)}")
        lines.append(f"{record_name},,")
        entries[record_name] = ("\n".join(lines) + "\n").encode("utf-8")
        for name, data in entries.items():
            info = src.getinfo(name)
            dst.writestr(zipfile.ZipInfo(name, date_time=info.date_time), data, zipfile.ZIP_DEFLATED)
    dist, version = wheel.name.split("-")[:2]
    pure = wheel.with_name(f"{dist}-{version}-{PURE_TAG}.whl")
    tmp.replace(pure)
    if pure != wheel:
        wheel.unlink()
    return pure, rewritten, stamped


def write_sidecars(wheel: Path, url: str, sha: str) -> str:
    """`<wheel>.sha256` (sha256sum format) and `kicad_python.pin` next to the wheel; returns the digest."""
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    wheel.with_name(wheel.name + ".sha256").write_text(f"{digest}  {wheel.name}\n", encoding="utf-8")
    (wheel.parent / PIN_FILE).write_text(f"{url}@{sha}\n", encoding="utf-8")
    return digest


def verify(wheel: Path, work: Path) -> str:
    """Install into a throw-away venv (no kiwrap, no .pth) and import the client + protos."""
    venv = work / "verify-venv"
    uv = _uv()
    run([*uv, "venv", "-q", str(venv)])
    python = venv / ("Scripts/python.exe" if sys.platform.startswith("win") else "bin/python")
    run([*uv, "pip", "install", "-q", "--python", str(python), str(wheel)])
    probe = (
        "import kipy, kipy.kicad_api_version as v; from kipy import KiCad; "
        "from kipy.proto.common.types import base_types_pb2; from kipy.proto.board import board_types_pb2; "
        "from kipy.proto.schematic import schematic_commands_pb2; "
        "import sys; assert not any(m in sys.modules for m in ('common', 'board', 'schematic')), 'bare packages leaked'; "
        "print(v.KICAD_API_VERSION)"
    )
    out = run([str(python), "-c", probe], capture_output=True).stdout.strip()
    if not out or out == "unknown":
        raise SystemExit(f"verification: KICAD_API_VERSION is {out!r} after the repair")
    return out


@dataclass
class WheelhouseReport:
    """What `check_wheelhouse` found (all fields set only when `problems` is empty)."""

    wheel: Path | None
    version: str
    pin: str
    problems: list[str]


def check_wheelhouse(
    out: Path, pyproject: Path | None = None, lock: Path | None = None, expect_pin: str | None = None
) -> WheelhouseReport:
    """Offline validation of a wheelhouse against pyproject.toml / uv.lock (see the module docstring)."""
    problems: list[str] = []
    url, sha = pinned(pyproject)
    lock_version, lock_source = locked(lock)
    wheels = sorted(out.glob(WHEEL_GLOB)) if out.is_dir() else []
    wheel = wheels[0] if len(wheels) == 1 else None
    if len(wheels) != 1:
        problems.append(f"expected exactly one {WHEEL_GLOB} in {out}, found {[w.name for w in wheels]}")
    version = ""
    if wheel is not None:
        parts = wheel.name[: -len(".whl")].split("-")
        version = parts[1] if len(parts) >= 2 else ""
        if "-".join(parts[-3:]) != PURE_TAG:
            problems.append(f"{wheel.name} is not tagged {PURE_TAG} (repair step skipped?)")
        if version != lock_version:
            problems.append(f"{wheel.name} is version {version}, uv.lock locks kicad-python {lock_version}")
        side = wheel.with_name(wheel.name + ".sha256")
        if not side.is_file():
            problems.append(f"missing {side.name}")
        else:
            want = side.read_text(encoding="utf-8").split()[0]
            got = hashlib.sha256(wheel.read_bytes()).hexdigest()
            if want != got:
                problems.append(f"{side.name} says {want}, the wheel hashes to {got}")
        try:
            with zipfile.ZipFile(wheel) as z:
                z.testzip()
        except zipfile.BadZipFile as exc:
            problems.append(f"{wheel.name} is not a valid zip: {exc}")
    pin_path = out / PIN_FILE
    pin = pin_path.read_text(encoding="utf-8").strip() if pin_path.is_file() else ""
    if pin != f"{url}@{sha}":
        problems.append(f"{PIN_FILE} is {pin!r}, pyproject.toml pins {url}@{sha}")
    if expect_pin and expect_pin != sha:
        problems.append(
            f"the expected pin {expect_pin} (KICAD_PYTHON_PIN) differs from pyproject.toml's {sha}"
        )
    if not lock_source.endswith("#" + sha):
        problems.append(f"uv.lock's kicad-python source {lock_source} is not at commit {sha}")
    return WheelhouseReport(wheel=wheel, version=version, pin=pin, problems=problems)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("--out", type=Path, default=REPO / "wheelhouse", help="where the wheel + .sha256 land")
    ap.add_argument("--work", type=Path, default=None, help="scratch dir (default: a temp dir, removed)")
    ap.add_argument("--pin", default=None, help="kicad-python commit (default: pyproject.toml's)")
    ap.add_argument("--url", default=None)
    ap.add_argument("--skip-verify", action="store_true")
    ap.add_argument(
        "--check", type=Path, default=None, metavar="DIR", help="validate DIR offline; build nothing"
    )
    ap.add_argument(
        "--expect-pin",
        default=os.environ.get("KICAD_PYTHON_PIN"),
        help="also require this commit (default $KICAD_PYTHON_PIN)",
    )
    args = ap.parse_args(argv)
    if args.check is not None:
        report = check_wheelhouse(args.check.resolve(), expect_pin=args.expect_pin)
        for p in report.problems:
            print(f"wheelhouse check: {p}", file=sys.stderr)
        if report.problems:
            return 1
        assert report.wheel is not None
        print(f"wheelhouse ok: {report.wheel.name} (version {report.version}, built from {report.pin})")
        return 0
    url, sha = pinned()
    url, sha = args.url or url, args.pin or sha
    out = args.out.resolve()  # relative to OUR cwd, never uv's (CI run 36013216332)
    work = (args.work or Path(tempfile.mkdtemp(prefix="kicad-python-wheel-"))).resolve()
    work.mkdir(parents=True, exist_ok=True)
    clone = work / "kicad-python"
    if clone.exists():
        shutil.rmtree(clone)
    shallow_clone(url, sha, clone)
    sub_sha = sparse_submodule(clone)
    stamp = f"{semantic_version(clone)}-{sub_sha[:10]}"
    out.mkdir(parents=True, exist_ok=True)
    remove_stale(out)
    wheel, rewritten, stamped = repair_wheel(build(clone, out), stamp)
    digest = write_sidecars(wheel, url, sha)
    print(f"{wheel}: {rewritten} proto imports rewritten, stamp {'set' if stamped else 'kept'} ({stamp})")
    print(f"sha256 {digest}")
    if not args.skip_verify:
        print("verified: KICAD_API_VERSION =", verify(wheel, work))
    if args.work is None:
        shutil.rmtree(work, ignore_errors=True)
    if args.pin is None and args.url is None:
        report = check_wheelhouse(out, expect_pin=args.expect_pin)
        for p in report.problems:
            print(f"wheelhouse check: {p}", file=sys.stderr)
        if report.problems:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
