"""Print a sandboxed environment for a KiCad GUI as JSON (run with the kiwrap workspace):

    uv run --project C:/Upwork/KiCad python prototypes/display/winstream/kicad_env.py \
        --kicad-root "C:/Program Files/KiCad/10.0" --storage <scratch>/kiwrap-home > env.json

The env comes from `kiwrap.sandbox.ensure(install)` (kicad_common.json + the three lib tables
seeded under <root>/<major.minor>/ so no Setup wizard appears; api server off). The user's real
%APPDATA%/kicad is never written.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from kiwrap import sandbox
from kiwrap.installation import find


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kicad-root", required=True)
    ap.add_argument("--storage", required=True)
    args = ap.parse_args()
    install = find(str(Path(args.kicad_root).resolve()))
    root = sandbox.ensure(install, storage_home=Path(args.storage), enable_api_server=False)
    out = {
        "version": install.version,
        "root": str(install.root),
        "pcbnew": str(install.root / "bin" / "pcbnew.exe"),
        "eeschema": str(install.root / "bin" / "eeschema.exe"),
        "demos": str(install.root / "share" / "kicad" / "demos"),
        "env": root.env(),
    }
    json.dump(out, sys.stdout, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
