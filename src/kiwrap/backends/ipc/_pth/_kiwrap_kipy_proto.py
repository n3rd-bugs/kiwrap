"""Site hook: make kicad-python's generated protobuf modules importable.

Installed at the root of site-packages by kiwrap's wheel (see `[tool.hatch.build.targets.wheel.force-include]`
in pyproject.toml) next to `kiwrap_kipy_proto.pth`, whose single line is `import _kiwrap_kipy_proto`.

Why this exists (verified 2026-09-19 on Windows, kicad-python git f79cebd1, protoletariat 3.3.10,
protoc-wheel-0 29.0 — see src/kiwrap/backends/ipc/__init__.py for the full write-up): kicad-python's
build script runs protoc and then `protol` (protoletariat) to rewrite the generated absolute imports
(`from common.types import base_types_pb2`) into package-relative ones. On Windows that rewrite is a
silent no-op, so the installed `kipy/proto/**/*_pb2.py` modules import bare top-level packages
`common`, `board`, `schematic` and `from kipy import KiCad` fails with `ModuleNotFoundError: common`.
Putting `<site-packages>/kipy/proto` on sys.path is exactly what makes protoc's default output
importable, and it is what upstream's own generated imports expect.

This module must stay dependency-free and must never raise: `site` runs it at every interpreter
start-up in the environment.
"""

from __future__ import annotations

import sys
from pathlib import Path


def _install() -> None:
    try:
        import importlib.util

        spec = importlib.util.find_spec("kipy")
    except Exception:  # pragma: no cover - never break interpreter start-up
        return
    if spec is None or not spec.submodule_search_locations:
        return
    for location in spec.submodule_search_locations:
        proto = Path(location) / "proto"
        if proto.is_dir() and str(proto) not in sys.path:
            # Append, not insert: the bare names `common`/`board`/`schematic` must never shadow a
            # real package of the same name that the environment already has.
            sys.path.append(str(proto))


_install()
