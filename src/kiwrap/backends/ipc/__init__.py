"""KiCad 11 headless IPC backend (`kicad-cli api-server`) — D16's primary backend.

Modules
- `sessions`  HeadlessSession: one sandboxed `kicad-cli api-server` per session, guaranteed teardown.
- `readers`   read_board / read_schematic -> millimetre, JSON-serialisable dataclasses (`models`).
- `queries`   connectivity, ratsnest approximation, bounding boxes, hit tests.
- `writes`    the minimal mutating path (commit -> create_items -> push -> save_as).
- `handlers`  the T5 command inventory / handler probe (conformance/tests/t5_ipc_handlers.py).
- `units`     exact nm <-> mm conversion (integer / Decimal, never float accumulation).

Client: kicad-python 0.9.0.dev0 from git (f79cebd1c09f6ef1d16e74d538da521e68c2d9f1), MIT; pinned
in pyproject.toml under `[tool.uv.sources]`. No 0.9 wheel exists on PyPI (latest 0.8.0, whose
10.0.6 protos cannot send `OpenDocument`) nor on test.pypi (verified 2026-09-19).

Packaging: why `kipy` needs a sys.path entry (verified 2026-09-19, Windows, uv 0.11)
---------------------------------------------------------------------------------
kicad-python's `build.py` runs protoc (`protoc-wheel-0==29`) and then `protol` (protoletariat
3.3.10, `--in-place --dont-create-package`) to rewrite protoc's absolute imports
(`from common.types import base_types_pb2 ...`) into package-relative ones. On Windows that second
step is a silent no-op, for two independent reasons found by re-running the build by hand
(scratchpad/ipc-backend/buildprobe):

1. `protoletariat.fdsetgen.Protoc.generate_file_descriptor_set_bytes` keeps its
   `NamedTemporaryFile` OPEN while protoc writes `--descriptor_set_out` into it; on Windows the
   open handle makes protoc's write fail (sharing violation), so the FileDescriptorSet read back is
   empty and there is nothing to rewrite.
2. `protoc-wheel-0`'s `protoc.exe` launcher `os.execv`s the real binary; on Windows execv spawns a
   child and the launcher returns 0 immediately, so protoc's failure (and even its completion) is
   invisible to protoletariat's `check=True`.

The result is a wheel whose `kipy/proto/**/*_pb2.py` import bare top-level packages `common`,
`board`, `schematic`, and `from kipy import KiCad` dies with `ModuleNotFoundError: common`.
Linux/macOS builds do not have the problem, which is why upstream never saw it. (Upstream fix:
close the temp file before running protoc in protoletariat, or have kicad-python pass the real
`protoc.PROTOC_EXE` path to `protol` — both reported in this component's findings.)

kiwrap's fix, in order of precedence:
0. A REPAIRED wheel: `scripts/ci/build_kicad_python_wheel.py` builds kicad-python once from a
   shallow, sparse clone (no 3.5 GB KiCad submodule), rewrites the generated imports to
   `kipy.proto.*` itself, stamps `KICAD_API_VERSION` (`10.99.0-<proto commit>` instead of
   "unknown", so kipy's `check_version()` runs again) and retags the wheel `py3-none-any`; CI
   installs that wheel (`scripts/ci/sync_with_wheelhouse.sh`) and it imports without any site
   hook (verified 2026-09-19 in a bare venv). The hooks below are belt and braces for a
   kicad-python built from git on Windows (`uv sync` on a developer machine).
1. `kiwrap_kipy_proto.pth` + `_kiwrap_kipy_proto.py`, installed at the root of site-packages by
   kiwrap's wheel (`[tool.hatch.build.targets.wheel.force-include]`; hatchling copies
   force-include files into editable wheels too, verified with hatchling 1.32.3 under `uv sync`).
   The .pth runs at interpreter start-up and appends `<site-packages>/kipy/proto` to `sys.path`, so
   `python -c "from kipy import KiCad"` works in the project environment without importing kiwrap.
2. `ensure_kipy_importable()` below, run when this package is imported: the same path insertion for
   environments where kiwrap is not installed as a distribution (plain `PYTHONPATH=src`).
Both append (never insert at 0) so the bare names cannot shadow a real `common`/`board` package.
`kiwrap.formats` stays untouched by all of this: it never imports kipy.

Never install kipy 0.8.0 into the same environment: its `kipy.proto` packages collide.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

__all__ = ["client_version", "ensure_kipy_importable", "kipy_proto_dir"]

#: The pinned kicad-python commit (pyproject.toml); the fallback when the installed distribution
#: carries no direct_url.json (a wheel from a wheelhouse).
KICAD_PYTHON_PIN = "f79cebd1c09f6ef1d16e74d538da521e68c2d9f1"


def client_version() -> str:
    """Provenance of the IPC client for snapshots and kw_env: `kicad-python <version>@<commit>`
    plus kipy's own `KICAD_API_VERSION` stamp. The stamp alone is useless: built from the uv git
    checkout it reads "unknown" (`build.py` runs `git describe --long` in a tag-less submodule
    clone, verified 2026-09-19), and kipy then skips its `check_version()` silently, so the
    commit from importlib.metadata's `direct_url.json` (or the pin) is what identifies the client."""
    import importlib.metadata as md
    import json

    version, commit = "?", ""
    try:
        dist = md.distribution("kicad-python")
        version = dist.version
        raw = dist.read_text("direct_url.json")
        if raw:
            commit = str(json.loads(raw).get("vcs_info", {}).get("commit_id", ""))
    except (md.PackageNotFoundError, ValueError, OSError):
        pass
    commit = commit or KICAD_PYTHON_PIN
    stamp = "unknown"
    try:
        from kipy.kicad_api_version import KICAD_API_VERSION

        stamp = str(KICAD_API_VERSION)
    except Exception:  # noqa: BLE001 - kipy not importable: the stamp stays unknown
        pass
    return f"kicad-python {version}@{commit[:8]} (KICAD_API_VERSION {stamp})"


def kipy_proto_dir() -> str | None:
    """`<site-packages>/kipy/proto` when kicad-python is installed, else None (no import of kipy)."""
    try:
        spec = importlib.util.find_spec("kipy")
    except (ImportError, ValueError):
        return None
    if spec is None or not spec.submodule_search_locations:
        return None
    for location in spec.submodule_search_locations:
        proto = Path(location) / "proto"
        if proto.is_dir():
            return str(proto)
    return None


def ensure_kipy_importable() -> bool:
    """Append `kipy/proto` to `sys.path` once (see the module docstring). Returns True when the
    directory exists (whether or not it was already on the path)."""
    proto = kipy_proto_dir()
    if proto is None:
        return False
    if proto not in sys.path:
        sys.path.append(proto)
    return True


ensure_kipy_importable()
