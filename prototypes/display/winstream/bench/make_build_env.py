"""Derive an env JSON for a KiCad *build tree* (C:/src/kicad/build/<preset>) from a kicad_env.py
JSON of an installed KiCad of the same major.minor (the sandboxed config roots are reused):

    python bench/make_build_env.py --base <scratch>/env1099.json --build C:/src/kicad/build/msvc-win64-release \
        --set KICAD_INPUT_FROM_EVENTS=1 > <scratch>/env-build.json

Adds the build tree's DLL directories and the vcpkg bin dirs to PATH (what C:/src/kicad-env.ps1 does),
sets KICAD_RUN_FROM_BUILD_DIR=1 and any --set VAR=VALUE. The config root stays the sandbox of --base.
"""

from __future__ import annotations

import argparse
import json
import os
import sys


def dll_dirs(build: str) -> list[str]:
    out: list[str] = []
    for root, dirs, files in os.walk(build):
        low = root.lower()
        if "vcpkg_installed" in low or f"{os.sep}qa{os.sep}" in low + os.sep:
            dirs[:] = []
            continue
        if any(f.lower().endswith(".dll") for f in files) and root not in out:
            out.append(root)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True, help="env JSON from kicad_env.py (same major.minor as the build)")
    ap.add_argument("--build", required=True, help="build tree, e.g. C:/src/kicad/build/msvc-win64-release")
    ap.add_argument("--vcpkg", default="C:/src/kicad/build/msvc-win64-debug/vcpkg_installed/x64-windows")
    ap.add_argument("--set", action="append", default=[], help="VAR=VALUE to add to the env")
    args = ap.parse_args()
    with open(args.base, encoding="utf-8-sig") as fh:
        spec = json.load(fh)
    build = os.path.abspath(args.build)
    env = dict(spec["env"])
    path = dll_dirs(build) + [os.path.join(build, "kicad"), os.path.join(args.vcpkg, "bin"),
                              os.path.join(args.vcpkg, "debug", "bin")]
    env["PATH"] = ";".join(path + [env.get("PATH", "")])
    env["KICAD_RUN_FROM_BUILD_DIR"] = "1"
    for kv in args.set:
        k, _, v = kv.partition("=")
        env[k] = v
    spec = dict(spec)
    spec["root"] = build
    spec["pcbnew"] = os.path.join(build, "pcbnew", "pcbnew.exe")
    spec["eeschema"] = os.path.join(build, "eeschema", "eeschema.exe")
    spec["kicad"] = os.path.join(build, "kicad", "kicad.exe")
    spec["env"] = env
    json.dump(spec, sys.stdout, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
