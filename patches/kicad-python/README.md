# kicad-python patches (gap G14, docs/UPSTREAM.md)

Git format-patches against `gitlab.com/kicad/code/kicad-python` at the pinned commit
`f79cebd1c09f6ef1d16e74d538da521e68c2d9f1` (kicad-python 0.9.0.dev0, `main` as of 2026-09-19; the
files they touch are unchanged on `main` since). Each is one logical change with a commit message
written for KiCad's reviewers; the MR descriptions are drafted in
`docs/upstream/drafts/g14-mr-generate-protos.md` and `g14-mr-server-env.md`. Nothing here has
been submitted; the drafts wait for the issue-first step (docs/dev/upstream-workflow.md 1.1).

| Patch | What | Why kiwrap needs it |
|---|---|---|
| `0001-Fix-generated-proto-imports-when-building-on-Windows.patch` | `tools/generate_protos.py`: run the real `protoc` binary that `protoc-wheel-0` exports (`protoc.PROTOC_EXE`) instead of its `os.execv` console script, and feed protoletariat the `--descriptor_set_out` file (`protol ... raw`) instead of letting it run `protoc` a second time | a git install of kicad-python on Windows produces `kipy/proto/**/*_pb2.py` that import bare top-level `common`/`board`/`schematic` packages; `from kipy import KiCad` fails with `ModuleNotFoundError: common`. Until merged, kiwrap ships the `.pth` site hook (`src/kiwrap/backends/ipc/_pth`) and the repaired wheel (`scripts/ci/build_kicad_python_wheel.py`). |
| `0002-Allow-passing-an-environment-to-the-headless-server.patch` | `KiCadServer(..., env=None)` and `KiCad(..., env=None)`: the mapping is handed to `Popen` unchanged | lets `KiCad(headless=True, env=sandbox_env)` carry kiwrap's `KICAD_CONFIG_HOME`/`TEMP` without touching `os.environ` or subclassing; kiwrap keeps its own launcher (`_ApiServerProcess`: tree kill, transcript, `Ping` readiness) either way. |

## Evidence (2026-09-19, Windows 11, Python 3.14.4, uv 0.11)

Reproduction of the two build bugs in isolation (`protoc-wheel-0` 29.0, protoletariat 3.3.10),
scratchpad `g14-g13/repro_build_bugs.py`:

```
[A:launcher]     rc=0 run() returned after 89 ms; descriptor set size right after return: -1; 3 s later: 124594
[A:real binary]  rc=0 run() returned after 32 ms; descriptor set size right after return: 124594
[B:launcher, forward slashes]     protoletariat FileDescriptorSet bytes: 0
[B:real binary, forward slashes]  protoletariat FileDescriptorSet bytes: 124594
[B:any, backslash path]           protoletariat raised FileNotFoundError (shlex.split strips the backslashes)
```

So: (A) the console-script launcher returns before protoc has written anything (the ipc component's
root cause, confirmed); (B) protoletariat's still-open `NamedTemporaryFile(delete=False)` is *not*
a problem for the real binary (the file is written and read back in full), so that half of the
earlier explanation does not hold; what does bite in addition is that protoletariat passes
`--protoc-path` through `shlex.split`, which eats Windows backslashes, so the "just point protol at
PROTOC_EXE" fix needs a forward-slash path. The patch avoids the second protoc run altogether.

Wheel-level proof (`uv build --wheel` in the clone, isolated build env, launcher on PATH):

| Wheel | `_pb2.py` modules with bare imports | fresh venv, no kiwrap, no `.pth` |
|---|---|---|
| stock f79cebd1 | 18 of 24 | `from kipy import KiCad` -> `ModuleNotFoundError: No module named 'common'` |
| with 0001 + 0002 | 0 of 24 | import ok; `KiCad(headless=True, kicad_cli_path=<10.99 nightly>, file_path=ecc83-pp.kicad_pcb, env=<sandbox>)`: version `10.99.0-4083-g6dd893f7d1`, 15 footprints / 14 nets, `GetPaths` -> `PATH_USER_SETTINGS = <sandbox>/cfg/10.99` (the env reached kicad-cli) |

kicad-python's own gates on the patched tree: `ruff check` + `ruff format --check` clean,
`mypy -p kipy` clean, `pytest` 32 passed.

## Applying

```
git clone https://gitlab.com/kicad/code/kicad-python.git && cd kicad-python
git checkout f79cebd1c09f6ef1d16e74d538da521e68c2d9f1
git am ../kiwrap/patches/kicad-python/*.patch
```

`tests/test_kipy_packaging.py` checks the series stays well-formed and (network permitting)
still applies to the pin. Drop a patch here the moment it lands on `main` and bump the pin.
