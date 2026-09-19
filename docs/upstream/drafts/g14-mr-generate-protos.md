# MR draft: kicad-python `tools/generate_protos.py` Windows build fix (G14a; NOT opened, 2026-09-19)

Patch: [`patches/kicad-python/0001-Fix-generated-proto-imports-when-building-on-Windows.patch`](../../../patches/kicad-python/0001-Fix-generated-proto-imports-when-building-on-Windows.patch)
against kicad-python `f79cebd1` (`main`, unchanged in the touched file since). One commit, one
file, +32 / -17 lines. Target: `gitlab.com/kicad/code/kicad-python`, branch off `main`.

Process (docs/dev/upstream-workflow.md 1.1 and 1.7): open the issue first, say "may I work on
this", wait for assignment, then push the branch to a kicad-python fork with CI visible and
"allow commits from members who can merge" ticked, and open the MR with `Closes #<n>`. Related
existing issues to link, not duplicate: #82 (bare `common` imports on Windows) and #140
(`protoc-wheel-0` as a build requirement). Do not touch README.md; do not add a test file.

## Issue text (open first; `Closes` it from the MR)

Title: `Git install on Windows generates proto modules with absolute imports`

Installing from git on Windows (`pip install git+https://gitlab.com/kicad/code/kicad-python.git`
or `uv pip install ...`) succeeds but `from kipy import KiCad` fails:

    ModuleNotFoundError: No module named 'common'

The generated `kipy/proto/**/*_pb2.py` still contain protoc's absolute imports
(`from common.types import base_types_pb2 ...`); on Linux/macOS the same command produces
`from ...common.types import ...`. 18 of the 24 generated modules are affected (the six without
cross-package imports are fine). Same result with Python 3.11 and 3.14, pip and uv.

Cause, found by running `build.py` by hand in the isolated build environment:

1. `protoc-wheel-0==29` (build requirement since #140) installs a `protoc` console script that
   `os.execv()`s the real compiler. On Windows `execv` starts a new process and the script exits 0
   immediately, so the `subprocess.run([...], check=True)` in `generate_protos()` returns before
   protoc has written anything. (The output does appear a moment later, which is why the wheel
   is not simply empty.)
2. protoletariat gets its `FileDescriptorSet` by running `protoc` again, looked up on PATH; that
   finds the same launcher, so `protol` reads an empty descriptor set and rewrites no import.
   It does not report this.

A system `protoc` on PATH (as COMPILING.md describes) is not affected, which is why the released
wheels are fine. Related: #82, #140.

I have a small change to `tools/generate_protos.py` that uses the compiler path
`protoc-wheel-0` exports and feeds protoletariat the descriptor set written by the first protoc
run. May I open an MR for this?

## MR description

Title: `Fix generated proto imports when building on Windows`

Closes #<issue above>. Related: #82, #140.

`tools/generate_protos.py` now prefers `protoc.PROTOC_EXE` (the compiler binary shipped by the
`protoc-wheel-0` build requirement) over the `protoc` console script, which on Windows returns
before the compiler has run, and asks protoc for `--descriptor_set_out` in the same invocation
so `protol` can be run with `raw <file>` instead of running protoc a second time through PATH.
The PATH fallback for a system `protoc` is unchanged, so Linux/macOS builds behave as before
(one fewer protoc run). `--protoc` on the command line still overrides the default.

Tested on Windows 11 with Python 3.14, `protoc-wheel-0` 29.0 and protoletariat 3.3.10:
`uv build --wheel` from a clone of this branch, installed into an empty venv: `from kipy import
KiCad` works, all 24 generated modules use relative imports, and a headless session against a
10.99 nightly (`KiCad(headless=True, ...)`, `get_board()`) runs. The wheel built from `main` in
the same environment fails to import as described in the issue. `ruff check`, `ruff format
--check`, `mypy -p kipy` and `pytest` are clean.

Tool disclosure: I used Claude Code to help find the two causes (comparing the launcher and the
binary side by side) and to draft this text; the change and the commit message were written and
tested by me.

## Commit message (as in the patch)

    Fix generated proto imports when building on Windows

    On Windows a git install of kicad-python produces modules under
    kipy/proto that import top-level "common", "board" and "schematic"
    packages, and "from kipy import KiCad" fails with ModuleNotFoundError.
    Linux and macOS builds are fine, which is why the released wheels never
    showed it.

    Two things in generate_protos() go wrong on Windows:

    The "protoc" build requirement (protoc-wheel-0) installs a console
    script that os.execv()s the real compiler. On Windows execv spawns a
    new process and the script exits 0 immediately, so subprocess.run()
    returns before protoc has produced any output. Use the PROTOC_EXE path
    the package exports instead of the script when it is importable; the
    old PATH lookup stays as the fallback for a system protoc.

    protoletariat runs "protoc" a second time to get the FileDescriptorSet
    it rewrites imports from. That lookup goes through PATH and finds the
    same launcher script, so protol reads an empty descriptor set and
    rewrites nothing, without any error. Have the first protoc run write
    the descriptor set (--descriptor_set_out) and pass it to "protol raw",
    which also drops the redundant second protoc run.

    Verified on Windows 11 with Python 3.14, protoc-wheel-0 29.0 and
    protoletariat 3.3.10: before the change all 24 generated modules import
    "common.*"/"board.*"; after it they use relative imports and the wheel
    imports in a fresh venv. Linux (a real protoc on PATH) is unchanged.

Before pushing, add the `Assisted-by: Claude Code` trailer the tool-generated-content policy
asks for (the patch in `patches/` carries no trailer so the series stays usable either way).

## Notes for the reviewer thread (only if asked)

- Why `protol raw` rather than `--protoc-path PROTOC_EXE`: protoletariat splits `--protoc-path`
  with `shlex.split`, which strips the backslashes of a Windows path
  (`C:\...\protoc.exe` -> `C:Users...protoc.exe`, `FileNotFoundError`); a forward-slash path works
  but is a workaround for protoletariat, and the descriptor set is produced by the run we already
  make. Measured: `generate_protos()` 1.4 s on the patched script vs 0.9 s "finished" on the
  launcher (which had not finished).
- The earlier theory that protoletariat's still-open `NamedTemporaryFile` blocks protoc's write
  on Windows does not hold: with the real binary the temp file is written and read back in full
  (124594 bytes) while the handle is open (`delete=False`, shared read/write).
- `build.py` is untouched: `git describe --long` returning nothing in a shallow submodule
  checkout (`.gitmodules: shallow = true`) is handled by `96c004cb` and is a separate matter.
- kiwrap side once merged: bump the pin, delete `src/kiwrap/backends/ipc/_pth/`, drop the
  `rewrite_imports` step of `scripts/ci/build_kicad_python_wheel.py` (keep the version stamp and
  the pure-wheel retag).
