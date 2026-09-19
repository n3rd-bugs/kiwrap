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
2. `uv build --wheel` in that clone (the upstream build backend, unchanged).
3. Repairs the wheel: rewrites the generated modules' bare `from common.types import ...` /
   `import board.board_types_pb2` into `kipy.proto.*` imports (what protoletariat does on Linux),
   stamps `kipy/kicad_api_version.py` with `<KICAD_SEMANTIC_VERSION>-<submodule sha>` when the
   build left "unknown", retags the pure-Python wheel `py3-none-any` (poetry-core's `build.py`
   path emits a platform tag such as `cp314-cp314-win_amd64` although the wheel holds no
   extension: verified 2026-09-19) and rewrites RECORD.
4. Verifies the result in a throw-away venv: `from kipy import KiCad` and the proto imports work
   WITHOUT kiwrap's .pth site hook, and writes `<wheel>.sha256`.

Usage: `uv run python scripts/ci/build_kicad_python_wheel.py --out wheelhouse` (network needed);
`--skip-verify` for environments without `uv venv`. The pin and URL default to pyproject.toml's.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DEFAULT_URL = "https://gitlab.com/kicad/code/kicad-python.git"
_PIN_RE = re.compile(r"kicad-python @ git\+(?P<url>[^@\s\"]+)@(?P<sha>[0-9a-f]{40})")
_BARE_PKGS = ("common", "board", "schematic")
_FROM_RE = re.compile(r"^(\s*)from (common|board|schematic)((?:\.\w+)*) import ", re.MULTILINE)
_IMPORT_RE = re.compile(r"^(\s*)import (common|board|schematic)\.", re.MULTILINE)


def pinned() -> tuple[str, str]:
    """(url, sha) from pyproject.toml's direct reference."""
    m = _PIN_RE.search((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    if not m:
        raise SystemExit("pyproject.toml has no `kicad-python @ git+...@<sha>` dependency")
    return m.group("url"), m.group("sha")


def run(args: list[str], cwd: Path | None = None, **kw: object) -> subprocess.CompletedProcess[str]:
    print("+", " ".join(args), flush=True)
    return subprocess.run(args, cwd=str(cwd) if cwd else None, check=True, text=True, **kw)  # type: ignore[call-overload]


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
    url = run(["git", "config", "-f", ".gitmodules", "submodule.kicad.url"], clone, capture_output=True).stdout.strip()
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


def build(clone: Path, out: Path) -> Path:
    run([sys.executable, "-m", "uv", "build", "--wheel", "--out-dir", str(out)], clone) if _has_uv_module() else run(
        ["uv", "build", "--wheel", "--out-dir", str(out)], clone
    )
    wheels = sorted(out.glob("kicad_python-*.whl"))
    if not wheels:
        raise SystemExit("uv build produced no kicad_python wheel")
    return wheels[-1]


def _has_uv_module() -> bool:
    try:
        import uv  # noqa: F401
    except ImportError:
        return False
    return True


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
            elif name == "kipy/kicad_api_version.py" and 'KICAD_API_VERSION = "unknown"' in data.decode("utf-8"):
                entries[name] = (
                    "# This file is automatically generated, do not modify it\n"
                    f'KICAD_API_VERSION = "{stamp}"\n'
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


def verify(wheel: Path, work: Path) -> str:
    """Install into a throw-away venv (no kiwrap, no .pth) and import the client + protos."""
    venv = work / "verify-venv"
    run(["uv", "venv", "-q", str(venv)])
    python = venv / ("Scripts/python.exe" if sys.platform.startswith("win") else "bin/python")
    run(["uv", "pip", "install", "-q", "--python", str(python), str(wheel)])
    probe = (
        "import kipy, kipy.kicad_api_version as v; from kipy import KiCad; "
        "from kipy.proto.common.types import base_types_pb2; from kipy.proto.board import board_types_pb2; "
        "from kipy.proto.schematic import schematic_commands_pb2; "
        "import sys; assert not any(m in sys.modules for m in ('common', 'board', 'schematic')), 'bare packages leaked'; "
        "print(v.KICAD_API_VERSION)"
    )
    out = run([str(python), "-c", probe], capture_output=True).stdout.strip()
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", type=Path, default=REPO / "wheelhouse", help="where the wheel + .sha256 land")
    ap.add_argument("--work", type=Path, default=None, help="scratch dir (default: a temp dir, removed)")
    ap.add_argument("--pin", default=None, help="kicad-python commit (default: pyproject.toml's)")
    ap.add_argument("--url", default=None)
    ap.add_argument("--skip-verify", action="store_true")
    args = ap.parse_args(argv)
    url, sha = pinned()
    url, sha = args.url or url, args.pin or sha
    work = args.work or Path(tempfile.mkdtemp(prefix="kicad-python-wheel-"))
    work.mkdir(parents=True, exist_ok=True)
    clone = work / "kicad-python"
    if clone.exists():
        shutil.rmtree(clone)
    shallow_clone(url, sha, clone)
    sub_sha = sparse_submodule(clone)
    stamp = f"{semantic_version(clone)}-{sub_sha[:10]}"
    args.out.mkdir(parents=True, exist_ok=True)
    wheel, rewritten, stamped = repair_wheel(build(clone, args.out), stamp)
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    wheel.with_suffix(wheel.suffix + ".sha256").write_text(f"{digest}  {wheel.name}\n", encoding="utf-8")
    print(f"{wheel}: {rewritten} proto imports rewritten, stamp {'set' if stamped else 'kept'} ({stamp})")
    print(f"sha256 {digest}")
    if not args.skip_verify:
        print("verified: KICAD_API_VERSION =", verify(wheel, work))
    if args.work is None:
        shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
