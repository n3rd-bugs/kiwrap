# MR draft: kicad-python `KiCadServer(env=)` (G14b; NOT opened, 2026-09-19)

Patch: [`patches/kicad-python/0002-Allow-passing-an-environment-to-the-headless-server.patch`](../../../patches/kicad-python/0002-Allow-passing-an-environment-to-the-headless-server.patch)
against kicad-python `f79cebd1`. One commit, `kipy/server.py` (+11) and `kipy/kicad.py` (+6 / -2).
Independent of patch 0001 (both apply to the pin in either order). Target: `main`; the
headless server class is part of the "unstable until 11 GA" surface, so this is an 11.0 change.

Process: issue first ("may I"), one MR, `Closes #<n>`. Small enough that Jon may just take it;
the point to make in the issue is the concrete use (private `KICAD_CONFIG_HOME` for a build
server or an editor integration), not "kiwrap wants it".

## Issue text

Title: `KiCadServer cannot be given an environment for kicad-cli`

`kipy.server.KiCadServer` starts `kicad-cli api-server` with `subprocess.Popen(cmd, ...)` and no
`env`, so the server always inherits the calling process's environment. A tool that drives the
headless server from a larger program (a CI job, an editor extension, a test harness) usually
wants the server to use its own settings directory (`KICAD_CONFIG_HOME`, so the user's
`kicad_common.json` and library tables are not read or rewritten), its own
`KICAD_DOCUMENTS_HOME`/`KICAD_CACHE_HOME`, and on Windows a short `TEMP` (the IPC pipe path has a
length limit). Today the only ways are to mutate `os.environ` around `KiCad(headless=True, ...)`,
which leaks into every other subprocess of the program, or to bypass `KiCadServer` and manage
the process by hand.

Proposal: an optional `env: Mapping[str, str] | None = None` on `KiCadServer` and on
`KiCad(...)` (headless mode only), passed to `Popen` unchanged; default behaviour unchanged.
May I open an MR?

## MR description

Title: `Allow passing an environment to the headless server`

Closes #<issue above>.

Adds an optional `env` mapping to `KiCadServer` and to `KiCad()` for headless mode. It is handed
to `subprocess.Popen` as is; leaving it out inherits the current environment as before. This lets
a caller give `kicad-cli api-server` a private `KICAD_CONFIG_HOME` (and `TEMP` on Windows) without
changing `os.environ` for the rest of its process.

Tested on Windows against a 10.99 nightly:
`KiCad(headless=True, kicad_cli_path=..., file_path=board, env={**os.environ,
"KICAD_CONFIG_HOME": private_dir, ...})` connects, `get_board()` works, and `get_paths()` reports
`PATH_USER_SETTINGS` under the private directory, so the environment reached kicad-cli. Without
`env` the behaviour is unchanged. `ruff`, `mypy -p kipy` and `pytest` clean.

Tool disclosure: drafted with Claude Code's help; written, reviewed and tested by me.

## Commit message (as in the patch)

    Allow passing an environment to the headless server

    KiCadServer starts kicad-cli with the environment of the current
    process. A caller that wants the server to use a private settings
    directory (KICAD_CONFIG_HOME) or a different TEMP for the socket has to
    change os.environ before constructing KiCad(headless=True), which leaks
    into everything else the process runs, or replace KiCadServer.

    Add an optional env mapping to KiCadServer and KiCad() that is handed
    to Popen unchanged. The default still inherits the environment.

Add the `Assisted-by:` trailer before pushing.

## Notes

- `socket_path` is still generated from the *client's* `tempfile.gettempdir()`; a server-side
  `TEMP` in `env` does not move it. That is fine (the client must be able to reach the path) and
  is why the docstring only mentions `KICAD_CONFIG_HOME`.
- kiwrap keeps `_ApiServerProcess` as its launcher regardless (process-tree kill, transcript,
  `Ping` readiness as in `qa/tests/api/api_e2e_utils.h`); `env=` mainly removes the need for
  users of plain kipy to poke `os.environ`, and lets kiwrap's tests use upstream's class directly.
