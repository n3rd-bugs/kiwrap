# Building KiCad from source (the bench)

Status: **executed and accepted on 2026-09-19** — Debug and RelWithDebInfo builds of the fork at
the nightly's commit `6dd893f7d1` (= installed 10.99.0-4083) at `C:\src\kicad` (presets
`msvc-win64-debug` / `msvc-win64-release`, QA tests on), installed under `C:\kiwrap-builds\`,
registered as kiwrap installs, T5-equal to the stock nightly ("Bench acceptance" below). Decision D17 ([DECISIONS.md](../DECISIONS.md)) made the bench *planned*: kiwrap extends
KiCad's CLI/IPC upstream ([UPSTREAM.md](../UPSTREAM.md) has the ranked gap cards and the
fork/patch workflow). Stock installers remain what kiwrap ships against; a patched build only
unlocks more.

## What actually happened (2026-09-19, this machine)

| Step | Outcome |
|---|---|
| VS 2022 17.9 → **17.14.37710 (MSVC 14.44.35207)** | `vs_community.exe update --installPath … --quiet --norestart --wait` via the current bootstrapper (the old installer's `setup.exe --update` / `update` returned 87 = invalid parameter). ~25 min. |
| vcpkg at the win-builder pin `66c0373d` at `C:\vcpkg`, `bootstrap-vcpkg.bat -disableMetrics` | OK (tool 2025-12-16). Run the batch file from PowerShell, not Git Bash. |
| `kicad-gitlab` NuGet source, `VCPKG_BINARY_SOURCES=clear;nuget,kicad-gitlab,read` | Feed reachable anonymously (HTTP 200) but **every package was NotFound**: our `cl` is 19.44.35229 (a newer 14.44 servicing than CI's 35207) and the ABI hash includes it. Expect a full local dependency build. |
| Fork clone (`--filter=blob:none` not used; full clone ~2 GB), `git checkout 6dd893f7d1` | OK. `CMakePresets.json.sample` hardcodes `VCPKG_ROOT: E:/vcpkg2/` — patch the copy to `C:/vcpkg/`; its `test-base` test preset lacks `"hidden": true` and CMake 3.31 rejects the file — add it. |
| `cmake --preset msvc-win64-debug` (vcpkg installs 143 packages) | 141 built in **84 min**; `wxwidgets-33` failed: wxWidgets downloads the WebView2 SDK with `file(DOWNLOAD)`, and vcpkg had picked the **MinGW `cmake.exe` from `%USERPROFILE%\tools\mingw64\bin`** (no Windows certificate store → "SSL peer certificate" error). Put `C:\Program Files\CMake\bin` first on PATH and drop the mingw64 dir; the retry configured in 70 min (wxWidgets + KiCad configure). |
| `cmake --build build\msvc-win64-debug --parallel 22` | **10 min**, 3648 targets, 0 errors: `kicad-cli.exe`, `kicad.exe`, `pcbnew.exe`, `eeschema.exe`, `qa_api.exe`, `qa_pcbnew.exe`, `qa_eeschema.exe`, … |
| Running from the build tree | Needs every KiCad DLL directory of the build tree on PATH (`kicommon.dll` in `common\`, `kigal.dll` in `common\gal`, `kicad_3dsg.dll`, …) plus `vcpkg_installed\x64-windows\debug\bin`, and `KICAD_RUN_FROM_BUILD_DIR=1`; otherwise exit 0xC0000135. `C:\src\kicad-env.ps1` (outside the repo; copy in [`../../scripts/dev/kicad-env.ps1`](../../scripts/dev/kicad-env.ps1)) sets all of it with a sandboxed `KICAD_*_HOME` and a short `TEMP`. `kicad-cli version --format plain` → `10.99.0`; `api-server` present. |
| `qa_api.exe` (KiCad's IPC API suite) | Debug: 99/108; RelWithDebInfo: **106/108**. Every failure is classified below ("Bench acceptance"): none is a server behaviour difference from the stock nightly. The cwd does NOT matter (paths are exe-relative / compiled in); run it with [`scripts/dev/run_qa_api.ps1`](../../scripts/dev/run_qa_api.ps1). `test_api_e2e.cpp` + `api_e2e_utils.h` are the template for new handler tests. |
| `msvc-win64-release` (RelWithDebInfo) second build tree | `cmake --preset msvc-win64-release -DVCPKG_INSTALLED_DIR=<debug tree>/vcpkg_installed -DVCPKG_MANIFEST_INSTALL=OFF -DCMAKE_INSTALL_PREFIX=C:/kiwrap-builds/6dd893f7d1-2` reuses the already-built vcpkg packages (vcpkg builds release and debug variants of every port; the NuGet cache misses anyway): configure 15 s, build **13 min 40 s** (3566 targets). This is the acceptance build (UPSTREAM §4.2 asked for RelWithDebInfo; the Debug tree stays for stepping). |
| Disk | vcpkg 31 GB (`buildtrees` 18 GB can be deleted), Debug tree 14 GB, RelWithDebInfo tree ~6 GB, installed prefixes ~0.7 GB (Debug) / ~0.4 GB (RelWithDebInfo). |

Done on the same day: both trees installed under `C:\kiwrap-builds\6dd893f7d1-{1,2}`, registered
as explicit kiwrap installs, T5 of the RelWithDebInfo build **equal** to
`conformance/snapshots/ipc-handlers/10.99.0.json`, fork tagged/branched (`base/6dd893f7d1`,
`kiwrap/6dd893f7d1`, `kiwrap-build/6dd893f7d1-{1,2}`, pushed to the GitHub mirror) — see "Bench
acceptance" below. G6 turned out to be a T5 probe artifact (§3); `mr/g1-ratsnest` is implemented
and built as `kiwrap-build/6dd893f7d1-4` = `C:\kiwrap-builds\6dd893f7d1-4` (§5).

**Three things learned the hard way on the same day (read before touching the bench):**

1. **Never create annotated tags in the fork.** KiCad's CMake takes `git describe` for the
   version string; an annotated tag such as `kiwrap-build/6dd893f7d1-1` becomes the describe
   output, `KICAD_MAJOR_MINOR_PATCH_VERSION` ends up empty and every reconfigure fails with a
   `set_target_properties` error. All fork tags are now **lightweight** (same commits, force-pushed)
   and upstream's tags are fetched (`git fetch upstream --tags`), so the build reports
   `10.99.0-4083-g6dd893f7d1` at base and `10.99.0-40xx-g<sha>` on the integration branch.
2. **One build tree installs into one prefix at a time, and only after a reconfigure.** KiCad
   bakes `KICAD_DATA` / `KICAD_DOCS` / `KICAD_PLUGINS` / `KICAD_DEMOS` / `KICAD_TEMPLATE` as
   absolute cache strings under `CMAKE_INSTALL_PREFIX` (and into `config.h`), so
   `cmake --install --prefix <other>` puts `bin/` under `<other>` and the `share/` tree under the
   baked prefix. Installing `-3` from the tree configured for `-2` overwrote `-2`'s `share/` and the
   first version of the install script then moved it away, leaving `-2` with `bin/` only
   (`kicad-cli` could not even print its version). `scripts/dev/install_kicad_build.ps1` now
   reconfigures the tree for the requested prefix (`-D` of every baked path), rebuilds the
   `config.h` dependents (~1 min) and refuses to touch any other prefix; `-2` was reinstalled from a
   pristine `base/6dd893f7d1` checkout and re-accepted (T5 EQUAL, T4 tree identical to the stock
   nightly's). The Debug tree (`msvc-win64-debug`, prefix `-1`) was never affected.
3. **The install metadata records the BUILT commit.** `commit` in `kiwrap-install.json` is the
   source tree's HEAD when the tree was built (`638d03a55e` for `-4`, the integration branch's
   sha), `base` is the upstream commit (`6dd893f7d1`); T4/T5 snapshots are named
   `<version>+<built commit>[.debug].json` (`KiCadInstallation.snapshot_name`), so a pristine and
   a patched build of one base never share a file. The first `-3` install had recorded the base as
   `commit` and overwrote the pristine build's snapshots; it was uninstalled.

## Bench acceptance (2026-09-19)

### 1. Running `qa_api.exe` correctly, and the true baseline

Facts (from `qa/CMakeLists.txt` `setup_qa_env`, `common/paths.cpp`, `qa/tests/api/api_e2e_utils.h`):

- **The working directory is irrelevant.** Test data is compiled in (`QA_PCBNEW_DATA_LOCATION`,
  `QA_KICAD_CLI_PATH`, overridable with `KICAD_TEST_PCBNEW_DATA_DIR` / `KICAD_TEST_EESCHEMA_DATA_DIR`).
  With `KICAD_RUN_FROM_BUILD_DIR=1` (which ctest sets and the e2e server needs), stock data
  resolves to the *executable's parent directory* (`PATHS::getWindowsKiCadRoot()` = `<exe dir>/..`):
  for `kicad/kicad-cli.exe` that is the build root, which has `schemas/`; for
  `qa/tests/api/qa_api.exe` it is `qa/tests/`, hence the "`qa\tests\schemas\api.v1.schema.json` not
  found" line. A directory junction `qa/tests/schemas -> <build>/schemas` fixes it (the runner
  script creates it); KiCad's own `test_remote_provider_client.cpp` works around the same thing on
  macOS with `KICAD_STOCK_DATA_HOME`.
- ctest's registered environment prepends `kicad/`, `common/`, `api/`, `common/gal/`, `pcbnew/`,
  `eeschema/`, `cvpcb/` and vcpkg's `[debug/]bin` to PATH; `ctest -R qa_api` also applies a
  **180 s TIMEOUT** property, which a Debug run exceeds (below), so run the exe directly:
  `.\scripts\dev\run_qa_api.ps1 -BuildDir C:\src\kicad\build\msvc-win64-release [-RunTest ApiJobs]`.
  The runner uses `Start-Process`, not a pipeline: the shared e2e api-server (a `kicad-cli` child)
  outlives `qa_api.exe` and holds the stdout pipe open otherwise.
- The e2e client's nng timeout is 10 s (`common/api/api_client.cpp`), so anything that wedges the
  shared server cascades into every later e2e case.

**True baseline at 6dd893f7d1 on Windows** (`--log_level=test_suite`, junction in place, sandboxed
`KICAD_*_HOME`, TEMP `C:\src\kicad-sandbox\tmp`):

| Build | Result | Wall time |
|---|---|---|
| RelWithDebInfo (`msvc-win64-release`) | **106 / 108** passed, 4039 / 4042 assertions | 3.8 s module, 5 s total |
| Debug (`msvc-win64-debug`) | 99 / 108 passed, 3946 / 3962 assertions | 189 s (timeouts) |

Every failure, with the evidence that classifies it (draft issue texts in
[`../upstream/drafts/issue-qa-api-windows-findings.md`](../upstream/drafts/issue-qa-api-windows-findings.md)):

| Case | Builds | Class | Evidence |
|---|---|---|---|
| `ApiE2E/CreateDocument` (2 assertions, line 614) | both | **real, test-side, platform-independent** | `projectWithExt.GetPath( true )` is `GetPath( wxPATH_GET_VOLUME )` (int parameter, `0x0001`) → no trailing separator; the server returns `PROJECT::GetProjectPath()` = `GetPathWithSep()`. Reproduced through kicad-python against the stock nightly and both bench builds: `project.path == 'C:\...\kicC129.tmp\'` for all three (identical server behaviour). Fix is `wxPATH_GET_VOLUME \| wxPATH_GET_SEPARATOR`. |
| `ApiE2E/CreateDocumentRejectsUnsavedModifications` | Debug only | **real handler defect, Debug-only manifestation** | The case sends `SetPageSettings` with only `orientation`; `FromProtoEnum<PAGE_SIZE_TYPE, PageSize>` hits `wxASSERT( false )` ("Unhandled case", `common/api/api_enums.cpp:822`) on `PS_UNKNOWN`; in a Debug build the assert wedges `kicad-cli api-server` (alive, never answers). Captured from the server's stderr through `HeadlessSession.transcript()`. The stock (release) nightly answers `AS_OK` in 0.0 s. |
| `ApiJobs/*` (6 cases, `OpenDocument ... nng_recv failed: Timed out`) | Debug only | **cascade** from the wedged server | `qa_api.exe --run_test=ApiJobs` alone: 6 / 6 pass in 5.8 s (Debug). `OpenDocument` of `ZoneFill-4.0.7.kicad_pcb` on the Debug server takes 0.4 s when measured directly. |
| `ApiWizards/QueryWizards` | both | **Windows QA-harness gap** | `API_PLUGIN_MANAGER::ReloadPlugins` → `#ifdef _WIN32` `FindKicadFile( "pythonw.exe" )` → `Pgm().IsKicadEnvVariableDefined()`, which `MOCK_PGM_BASE` has no expectation for (the Linux CI path never calls it). Not reachable on Linux, not a server defect. |

Also found this way: `SetBoardDesignRules` with an empty `rules` message reaches
`JSON_SETTINGS::Migrate`'s `assert "m_migrators.count( filever ) > 0"` (`json_settings.cpp:778`)
through the `BOARD_DESIGN_SETTINGS` copy in the handler and wedges a Debug server the same way
(that is the single T5 difference of the Debug build, §3).

### 2. Installing the build as a kiwrap install

[`scripts/dev/install_kicad_build.ps1`](../../scripts/dev/install_kicad_build.ps1) does all of
this; what it encodes:

- `cmake --install <build> --prefix <prefix>` relocates `bin/` only: KiCad bakes `KICAD_DATA`
  (and `KICAD_DOCS`, `KICAD_PLUGINS`, `KICAD_DEMOS`, `KICAD_TEMPLATE`, `KICAD_LIBRARY_DATA`) as
  **absolute cache strings** at configure time, so `share/kicad/{demos,template,schemas,plugins,
  resources,libwmf}` land under the CMakeCache's `CMAKE_INSTALL_PREFIX`. The script therefore
  reconfigures the tree for the requested prefix first (`cmake -B <build> -DCMAKE_INSTALL_PREFIX=…
  -DKICAD_DATA=… …`, 15 s, then an incremental build of the 21 `config.h` dependents) and never
  touches another prefix; `-Force` replaces an existing install at the prefix (unlinking the
  library junctions first — `Remove-Item -Recurse` would follow them into the nightly's libraries).
  Run it from the developer shell (`$env:KICAD_BUILD_PRESET='msvc-win64-release'; . C:\src\kicad-env.ps1`).
  At run time KiCad on Windows resolves `share/` relative to the exe, so the baked path never matters.
- `X_VCPKG_APPLOCAL_DEPS_INSTALL` is OFF: the vcpkg DLLs are not installed. `kicad-win-builder`'s
  `build.ps1` copies `vcpkg_installed/<triplet>/bin/*.dll` into the package `bin/`; the script does
  the same (`debug/bin` for a Debug build: 214 DLLs in `bin/` afterwards). The debug CRT
  (`vcruntime140d`, `msvcp140d`, `ucrtbased`) is not redistributable and comes from `System32`
  on a machine with VS installed. `cmake --install` also drops the release CRT redistributables.
- Libraries are separate packages. The nightly's tables reference **`KICAD10_*`**
  (`KICAD_MAJOR_MINOR_PATCH_TUPLE {10, 99, 0}` → `ENV_VAR::GetVersionedEnvVarName` uses major 10;
  verified in `sym-lib-table`: 223 rows of `${KICAD10_SYMBOL_DIR}`), and `kiwrap.sandbox` already
  exports `KICAD<major>_SYMBOL_DIR/FOOTPRINT_DIR/3DMODEL_DIR/TEMPLATE_DIR` from the install's own
  `share/kicad/<dir>`. So the prefix gets directory junctions `share/kicad/{symbols,footprints,
  3dmodels}` → `C:\Program Files\KiCad\10.99\share\kicad\...` plus a copy of the nightly's
  `template/{sym,fp}-lib-table` (the source tree's `template/` only ships `kicad.kicad_pro`).
  `demos/` (incl. ecc83) and `schemas/api.v1.schema.json` (kiwrap's `ipc.available` probe) come
  from the source install.
- `<prefix>/kiwrap-install.json` records `build_tree`, `source_tree`, `commit` (the BUILT commit:
  `git rev-parse --short=10 HEAD` of the source tree, refused when the tree is dirty), `base`
  (`-Base`, the upstream commit), `branch`, `tag`, `kiwrap_patches`, `preset`, `build_type`,
  `installed_at` (written without BOM — Windows PowerShell's `-Encoding utf8` adds one, and kiwrap
  reads `utf-8-sig` anyway).
- The installed `kicad-cli.exe` runs from a plain shell with no dev-shell PATH:
  `version --format plain` → `10.99.0`; `version --format commit` → the built commit (the script
  clears `KICAD_RUN_FROM_BUILD_DIR` for this check, otherwise the binary looks for
  `<prefix>/schemas` instead of `<prefix>/share/kicad/schemas`).

**Discovery.** `kiwrap.installation.discover()` now reads explicit roots from `KIWRAP_KICAD_ROOTS`
(`;`-separated) and `<storage>/installs.json` (`python -m kiwrap.installation --register <root>`),
reports them with source `explicit` and the metadata file's fields in `extra`
(`KiCadInstallation.is_bench_build`, `.kiwrap_patches`), and sorts explicit roots after native
installs of the same semver (bench builds last), so `find("10.99")` / `newest` still return the
installer's nightly. pytest redirects `KIWRAP_HOME` per session, so for the test suite the env var
is the mechanism:

```powershell
$env:KIWRAP_KICAD_ROOTS = "C:\kiwrap-builds\6dd893f7d1-2;C:\kiwrap-builds\6dd893f7d1-4"; uv run pytest -q --timeout 900
```

(`-2` = the pristine acceptance build, `-4` = the patched integration build; three installs then
share the semver 10.99.0, which the suite handles: parametrisation ids are `<version>@<root name>`
and per-install scratch directories use the root name.) `python -m kiwrap.installation --no-probe`
on this machine (the Debug `-1` is on disk but not registered — wx asserts wedge some handlers):

```
version  source                 root                           python  share
10.99.0  registry               C:\Program Files\KiCad\10.99   -       C:\Program Files\KiCad\10.99\share\kicad
10.99.0  explicit (638d03a55e)  C:\kiwrap-builds\6dd893f7d1-4  -       C:\kiwrap-builds\6dd893f7d1-4\share\kicad
10.99.0  explicit (6dd893f7d1)  C:\kiwrap-builds\6dd893f7d1-2  -       C:\kiwrap-builds\6dd893f7d1-2\share\kicad
10.0.6   registry               C:\Program Files\KiCad\10.0    ...
```

The full capability records of the stock nightly and the pristine bench (`--help` tree of
69 subcommands, `ipc.available`, `ipc.headless`, `swig.available=false`) differ **only** in the
informational keys `kiwrap.patched_build` (true for the bench), `kiwrap.build_commit`
(`6dd893f7d1`) and `kiwrap.build_base`. `kiwrap.patches` is `[]` on both; on `-4` it is
`["ratsnest-export"]` (`cli.has_ratsnest_export` from the help tree, `ipc.has_get_ratsnest`
derived from it and confirmed by the presence probe the first headless session with a board runs,
`kiwrap.ipc_probed`). The manifest `src/kiwrap/data/kiwrap_patches.json` (base `6dd893f7d1`,
one patch, `builds: [-1, -2, -3 (superseded), -4]`) is part of the capability cache key.

### 3. T5 equality and the kiwrap suite

`uv run python scripts/dev/t5_bench_snapshot.py [<root>]` probes every registered bench build,
writes `conformance/snapshots/ipc-handlers/<version>+<built commit>.json` (`.debug.json` for a
Debug build; `KiCadInstallation.snapshot_name`, the same rule `conformance/tests/t5_ipc_handlers.py`
and T4 use) and diffs against the stock `<version>.json` with `handlers.compare` (only commands a
manifest patch lists may differ):

| Build | Snapshot | Result |
|---|---|---|
| RelWithDebInfo `6dd893f7d1-2` (pristine, reinstalled) | `10.99.0+6dd893f7d1.json` | **EQUAL** to `10.99.0.json` — pcb handled 67 / bad_request 37 / unhandled 25; schematic 34 / 26 / 69 (after the G6 probe fix; 52/37/40 and 28/25/76 before); 57 s |
| RelWithDebInfo `6dd893f7d1-4` (integration, `638d03a55e`) | `10.99.0+638d03a55e.json` | **EQUAL** to `10.99.0.json` (the inventory cannot see `GetRatsnest` until kicad-python is regenerated, so equality proves the patch changed nothing else) |
| Debug `6dd893f7d1-1` | `10.99.0+6dd893f7d1.debug.json` | one row: `SetBoardDesignRules [pcb]: handled -> timeout` (the Debug assert above); 118 s |

**The bench acceptance test (UPSTREAM §4.2) passes on the RelWithDebInfo build.** The Debug build
is the stepping tree; do not use it for T5 or for the `installs`-parametrised suite.

**Full kiwrap suite with the bench registered** (`KIWRAP_KICAD_ROOTS="C:\kiwrap-builds\6dd893f7d1-2;C:\kiwrap-builds\6dd893f7d1-4"
uv run pytest -q --timeout 900`, `%APPDATA%\kicad` hash unchanged before/after): **881 passed,
9 skipped, 0 failed in 20 min 35 s** (2026-09-19, stock nightly + `-2` + `-4`, three installs of
10.99.0; the skips are the documented pre-existing reasons plus the `kicad_patch` canary). The
failures the first runs had are fixed:

| Test | Cause | Status |
|---|---|---|
| `tests/test_installation.py::TestFind::test_no_match_raises_with_found_list` | `find("<missing path>")` probes `discover([path])`, which now also lists the registered roots, and accepted the bench as "the explicit install" | fixed in `installation.find()`: a probed install counts only when its root / kicad-cli IS the spec |
| `tests/test_cli_integration.py::test_every_install_end_to_end` | scratch dir `tmp_path / install.major_minor` collided for two 10.99 installs (`FileExistsError`) | fixed: `tmp_path / install.root.name` |
| `conformance/tests/t5_ipc_handlers.py::test_t5_ipc_handler_snapshot` | same collision, and a bench build was compared against the stock `10.99.0.json` | fixed: scratch dir by root name, snapshot `<version>+<built commit>[.debug].json` (`KiCadInstallation.snapshot_name`); the root `conftest.py` parametrises `install` with ids `<version>@<root name>` |
| `conformance/tests/t4_cli_help_snapshot.py` | `-2` and `-3` (both `commit: 6dd893f7d1`) shared one snapshot file and failed each other (`pcb export ratsnest` removed/added) | fixed by recording the built commit: `10.99.0+6dd893f7d1.json` (pristine) and `10.99.0+638d03a55e.json` (patched) |

Every live test that ran on the bench install (CLI adapters, sandbox, structured exports, IPC
sessions, T1–T9 where parametrised over `installs`) passed with the same outcomes as on the stock
nightly.

**G6 is a T5 probe artifact, not a KiCad gap.** While classifying the `ApiJobs` failures it turned
out the job handlers ARE registered on the headless server (`API_HANDLER_PCB` constructor,
`api_handler_pcb.cpp:170-190`, unconditional): sending `RunBoardJobExportSvg` with
`job_settings.document` set to the open board returns `JS_SUCCESS` + the SVG path in 0.1 s on the
**stock** nightly, and `RunSchematicJobExportNetlist` (`SNF_KICAD_XML`) returns `JS_SUCCESS` on the
schematic (same on the bench). The T5 "minimal instance" only fills top-level `DocumentSpecifier` /
`ItemHeader` fields; the job requests carry the document inside `RunJobSettings job_settings`, so
the probe sent an empty document, `validateDocument` answered `AS_UNHANDLED` and the server reported
"no handler available" — the 22 `RunBoardJobExport*` / `RunSchematicJobExport*` rows of the first
`10.99.0.json`. Consequences (all done 2026-09-19): `ipc.headless_jobs` is `true` for 10.99.0
(verified, `tests/test_ipc_patch_g6.py`) and 11.0.0 in the matrix; `handlers._minimal_instance`
fills any one-level-nested `DocumentSpecifier` (`RunJobSettings.document` included) and the stock
and bench snapshots were regenerated (22 rows `unhandled -> handled`/`bad_request`); live exports
run through `backends.ipc.jobs`; the `mr/g6-headless-jobs` branch only carries the
`ApiJobs/ExportBoardStats` e2e case (kept in the integration branch, not to be opened standalone).

### 4. Fork refs (all on the GitHub mirror github.com/n3rd-bugs/kicad-fork, nothing on GitLab)

**Remote names on this checkout:** in `C:\src\kicad` the mirror is the remote named **`public`**
(`https://github.com/n3rd-bugs/kicad-fork.git`); `origin` is an unreachable ssh remote of
a different fork (`git@github.com:n3rd-bugs/kicad-fork.git`) and `upstream` is GitLab with its
push URL set to `DISABLED` (`git remote set-url --push upstream DISABLED`), so a slip cannot reach
GitLab. Push with `git push public <ref>`; renaming `public` to `origin` (after removing the ssh
remote) makes the `git push origin` wording of UPSTREAM.md literal — it needs a permission the
fixer session did not have. All tags below are **lightweight** (annotated tags break `git describe`
and every CMake reconfigure, see the top of this file).

| Ref | Points at | Pushed |
|---|---|---|
| tag `base/6dd893f7d1` | `6dd893f7d1` | yes |
| branch `mr/g6-headless-jobs` | `4589ac04b6` = base + the `ApiJobs/ExportBoardStats` e2e case | yes |
| branch `mr/g1-ratsnest` | base + `8f5b175459` (job + CLI + schema + qa_pcbnew/qa_cli) + `f474f77212` (proto + handler + qa_api) | yes (force-pushed after the review rework) |
| branch `kiwrap/6dd893f7d1` | `638d03a55e` = base + merge `mr/g6-headless-jobs` + merge `mr/g1-ratsnest` | yes (force-pushed) |
| tag `kiwrap-build/6dd893f7d1-1` | `6dd893f7d1`, the Debug install | yes |
| tag `kiwrap-build/6dd893f7d1-2` | `6dd893f7d1`, the RelWithDebInfo pristine install (acceptance build) | yes |
| tag `kiwrap-build/6dd893f7d1-3` | `0595fba8db`, the first integration install (superseded, uninstalled) | yes |
| tag `kiwrap-build/6dd893f7d1-4` | `638d03a55e`, the RelWithDebInfo integration install | yes |

`master` was not touched; the checkout is on `kiwrap/6dd893f7d1` with the release tree built from
it (= the installed `-4`).

**State of the mirror on 2026-09-19 22:25 local (17:25Z):** the GitHub events API shows eight
`DeleteEvent`s by the `n3rd-bugs` account (17:24:52Z-17:25:10Z) removing every kiwrap ref listed
above — the three branches and the five tags — 30 minutes after the fixer's force-pushes of
`mr/g1-ratsnest` (`f474f77212`) and `kiwrap/6dd893f7d1` (`638d03a55e`) at 16:55Z; the mirror now
carries `master` only. The fixer did not re-push (an unexplained deliberate deletion is not
something to fight). Every ref is intact in `C:\src\kicad`; to restore the mirror once the owner
confirms it should carry them:

```powershell
cd C:\src\kicad
git push public base/6dd893f7d1 kiwrap-build/6dd893f7d1-1 kiwrap-build/6dd893f7d1-2 kiwrap-build/6dd893f7d1-3 kiwrap-build/6dd893f7d1-4
git push public mr/g6-headless-jobs mr/g1-ratsnest kiwrap/6dd893f7d1
```

2026-09-24: every fork commit was rewritten without its `Assisted-by:` trailer (DECISIONS.md D19 as
amended; message-only, trees identical) and force-pushed; the hashes above are the rewritten ones.

### 5. Rebuilding after a patch change (the loop that works)

```powershell
$env:KICAD_BUILD_PRESET = 'msvc-win64-release'; . C:\src\kicad-env.ps1
cd C:\src\kicad; git checkout mr/g1-ratsnest            # edit, amend (no AI trailer, D19), clang-format --dry-run
git checkout -B kiwrap/6dd893f7d1 base/6dd893f7d1; git merge --no-ff mr/g6-headless-jobs; git merge --no-ff mr/g1-ratsnest
cmake --build C:\src\kicad\build\msvc-win64-release --parallel 22      # 2-3 min incremental
.\scripts\dev\run_qa_api.ps1 -BuildDir C:\src\kicad\build\msvc-win64-release   # expect 108/110
.\build\msvc-win64-release\qa\tests\pcbnew\qa_pcbnew.exe --run_test=RatsnestReport
# qa/tests/cli needs numpy + pillow + cairosvg: a scratch venv (uv venv; uv pip install pytest numpy pillow cairosvg),
# then $env:KICAD_CLI=<build>\kicad\kicad-cli.exe; $env:KICAD_BUILD_PATHS=1; python -m pytest test_pcb.py -k ratsnest
cd C:\Upwork\KiCad
.\scripts\dev\install_kicad_build.ps1 -BuildDir C:\src\kicad\build\msvc-win64-release -Prefix C:\kiwrap-builds\6dd893f7d1-<n> `
    -Base 6dd893f7d1 -Patches ratsnest-export -Tag kiwrap-build/6dd893f7d1-<n> -Register
git -C C:\src\kicad tag kiwrap-build/6dd893f7d1-<n> kiwrap/6dd893f7d1     # LIGHTWEIGHT
uv run python scripts/dev/t5_bench_snapshot.py C:\kiwrap-builds\6dd893f7d1-<n>   # EQUAL expected
# add the build to src/kiwrap/data/kiwrap_patches.json builds[] (commit = built sha, snapshot = <version>+<sha>.json)
$env:KIWRAP_KICAD_ROOTS = "C:\kiwrap-builds\6dd893f7d1-2;C:\kiwrap-builds\6dd893f7d1-<n>"; uv run pytest -q --timeout 900
```

## When a build is the honest tool (the named triggers)

Do **not** build for: driving installed 7–10 binaries, writing IPC/PCM plugins, crash diagnosis
(use [debugging-kicad-with-symbols.md](debugging-kicad-with-symbols.md) — PDBs + `cdb` +
`api.log` are verified), regenerating kipy protos, or KiCad 11 prototyping (use the 10.99 nightly
installer). Build only when one of these fires, and record which one in the PR:

| Trigger | Why installed binaries are not enough |
|---|---|
| **T-UPSTREAM** — contributing a handler/proto upstream (RunDRC/GetDrcMarkers do not exist even on master; ERC JSON ×100; #24966; `document` field) | MR must build, pass `qa/` tests and follow proto compatibility rules |
| **T-STEP** — stepping through KiCad's netlist updater / DRC engine while kiwrap reimplements their semantics ([ARCHITECTURE.md §4, §17](../ARCHITECTURE.md)) | needs unoptimised locals; release PDBs give file@line but optimised-away variables |
| **T-BLOCKER** — a crash that `api.log` + minidump + symbols cannot explain | last resort after the symbols workflow |
| **T-PNS** — driving PNS as a batch engine (PCBWorld-Engine-style fork) | **out of scope by decision**; listed so nobody starts it by accident |

Budget once triggered (this machine: Core Ultra 9 285K, 24 threads, 127 GB RAM, C: 715 GB free):
environment 1–2 h; vcpkg dependencies 10 min–1 h with binary-cache hits, 2–5 h without; KiCad
itself 30–60 min for a Debug/RelWithDebInfo build, minutes incremental; **40–80 GB disk**
(vcpkg 30–50 GB + build tree 10–25 GB). Plan **one working day** to the first debug build.

## Where the source lives

The fork exists already: <https://github.com/n3rd-bugs/kicad-fork> (fork of
`KiCad/kicad-source-mirror`, default branch `master`, created with `gh repo fork`). Clone it
**outside this repo** and at a short path (vcpkg and MSVC both choke on long paths):

```powershell
git clone https://github.com/n3rd-bugs/kicad-fork.git C:\src\kicad
cd C:\src\kicad
git remote add upstream https://gitlab.com/kicad/code/kicad.git   # MRs go to GitLab, not GitHub
git fetch upstream --tags
git checkout 10.0            # or the exact tag you are debugging, e.g. 10.0.6, or master for 11 work
```

Upstream is GitLab; the GitHub mirror (and therefore the fork) is read-only tracking. Branch from
the tag that matches the installed binary you are investigating so PDB file@line numbers (which
point at `C:\jenkins\workspace\build-win-10.0-stable\.build\kicad\...`) line up with your checkout.

## Windows recipe (Visual Studio 2022 + vcpkg manifest mode)

### 0. Toolchain facts that decide cache hits

- Official 10.0.x binaries are compiled with **MSVC 14.44.35207 = VS 2022 17.14 Build Tools**
  (seen in the PDB paths during the symbols spike). This machine has VS 2022 **17.9.34728 (MSVC
  14.39)**. vcpkg binary-cache hashes include the compiler version, so with 17.9 every dependency
  rebuilds locally (2–5 h). **Update VS 2022 to 17.14.x first** (Visual Studio Installer → Update;
  workloads "Desktop development with C++" and "C++ CMake tools for Windows").
- CMake ≥ 3.28.1 is required by `CMakeLists.txt` (MSVC path); this machine has 3.31.5 on PATH and
  Ninja 1.13.2 under `%USERPROFILE%\tools\mingw64\bin`. `kicad-win-builder` pins CMake 3.31.10
  and Ninja 1.12.1 — the VS-bundled ones also work.
- vcpkg **must sit at a drive root** (`C:\vcpkg`) — path-length failures otherwise.
- Antivirus real-time scanning can break vcpkg builds; exclude `C:\vcpkg` and the build tree.

### 1. vcpkg at the pinned commit

`kicad-win-builder/build.ps1` pins `$vcpkgCommit = "66c0373dc7fca549e5803087b9487edfe3aca0a1"`
(the dev-docs page still shows an older `ef7dbf94…`; the win-builder pin is what CI uses — re-read
`build.ps1` on the day you build and use its current value).

```powershell
git clone https://github.com/microsoft/vcpkg.git C:\vcpkg
cd C:\vcpkg
git checkout 66c0373dc7fca549e5803087b9487edfe3aca0a1
.\bootstrap-vcpkg.bat -disableMetrics
[Environment]::SetEnvironmentVariable('VCPKG_ROOT', 'C:\vcpkg', 'User')
```

### 2. KiCad's pre-built dependency cache (NuGet source `kicad-gitlab`)

KiCad publishes vcpkg binary packages to a GitLab NuGet feed; with a matching compiler they turn
the dependency step from hours into minutes.

```powershell
$nuget = & C:\vcpkg\vcpkg.exe fetch nuget | Select-Object -Last 1
& $nuget sources add -Name kicad-gitlab -Source "https://gitlab.com/api/v4/projects/27426693/packages/nuget/index.json"
# Consume the cache (read-only) for every vcpkg invocation in this shell:
$env:VCPKG_BINARY_SOURCES = "clear;nuget,kicad-gitlab,read"
```

If the feed rejects anonymous reads at the time you build, drop the variable — vcpkg then builds
everything locally (the 2–5 h case).

### 3. swigwin 4.3.1 (SWIG pcbnew bindings, still built on 10.x)

`build.ps1` downloads `swigwin-4.3.1` (SHA-256
`7EA5197C557AF20B2F7780FFCFE803BBE0E2009F5846874112AEA37E5F693417`) from SourceForge; dev-docs
still say 4.1.1. Use 4.3.1 to match CI:

```powershell
$zip = "$env:TEMP\swigwin-4.3.1.zip"
Invoke-WebRequest -UseBasicParsing -OutFile $zip `
  "https://sourceforge.net/projects/swig/files/swigwin/swigwin-4.3.1/swigwin-4.3.1.zip/download"
if ((Get-FileHash $zip).Hash -ne '7EA5197C557AF20B2F7780FFCFE803BBE0E2009F5846874112AEA37E5F693417') { throw 'swig hash' }
Expand-Archive $zip -DestinationPath C:\tools\
$env:Path = "C:\tools\swigwin-4.3.1;$env:Path"      # persist it for the VS session too
```

### 4. Configure with the `msvc-win64-debug` preset

`CMakePresets.json.sample` on branch 10.0 defines `msvc` (Ninja, `binaryDir =
${sourceDir}/build/${presetName}`, `CMAKE_TOOLCHAIN_FILE = $env{VCPKG_ROOT}/scripts/buildsystems/vcpkg.cmake`,
`KICAD_BUILD_QA_TESTS=ON`, `KICAD_SCRIPTING_WXPYTHON=ON`, `KICAD_WIN32_DPI_AWARE=ON`) and the
children `msvc-win64-debug` (`CMAKE_BUILD_TYPE=Debug`, `KICAD_BUILD_PNS_DEBUG_TOOL=ON`) and
`msvc-win64-release` (`RelWithDebInfo`).

```powershell
cd C:\src\kicad
Copy-Item CMakePresets.json.sample CMakePresets.json        # gitignored; edit freely
# From an "x64 Native Tools Command Prompt for VS 2022" (or after vcvars64.bat):
cmake --preset msvc-win64-debug                            # runs vcpkg install (the slow step)
cmake --build build\msvc-win64-debug --parallel            # 30-60 min first time on 24 threads
```

Alternatively open the folder in Visual Studio (File → Open → Folder), pick the
`msvc-win64-debug` configuration and let VS drive the same presets.

Useful cache variables (add to the preset or `-D`): `KICAD_USE_SENTRY=OFF` (default OFF),
`KICAD_IPC_API=ON` (default ON — required for anything kiwrap cares about),
`KICAD_BUILD_QA_TESTS=ON` (needed for T-UPSTREAM; `qa/tests/api` is where API tests live).

### 5. Run from the build directory (`KICAD_RUN_FROM_BUILD_DIR`)

The binaries in `build\msvc-win64-debug` are not installed; they find resources through
`KICAD_RUN_FROM_BUILD_DIR` and need the vcpkg Python. Put this in `.vs\launch.vs.json` (VS) or
export it before running `pcbnew.exe` from a shell:

```json
{
  "version": "0.2.1",
  "configurations": [
    {
      "type": "default", "project": "CMakeLists.txt", "projectTarget": "pcbnew.exe (pcbnew\\pcbnew.exe)",
      "name": "pcbnew (build dir, sandboxed config)",
      "env": {
        "KICAD_RUN_FROM_BUILD_DIR": "1",
        "KICAD_USE_EXTERNAL_PYTHONHOME": "1",
        "PYTHONHOME": "${cmake.buildRoot}\\vcpkg_installed\\x64-windows\\tools\\python3",
        "PYTHONPATH": "${cmake.buildRoot}\\pcbnew;${workspaceRoot}\\scripting",
        "PATH": "${cmake.buildRoot}\\vcpkg_installed\\x64-windows\\debug\\bin;${env.PATH}",
        "KICAD_CONFIG_HOME": "C:\\src\\kicad-sandbox\\config",
        "KICAD_DOCUMENTS_HOME": "C:\\src\\kicad-sandbox\\documents",
        "KICAD_CACHE_HOME": "C:\\src\\kicad-sandbox\\cache",
        "TEMP": "C:\\src\\kicad-sandbox\\tmp", "TMP": "C:\\src\\kicad-sandbox\\tmp"
      }
    }
  ]
}
```

The three `KICAD_*_HOME` variables are **not optional** in this project: a debug KiCad rewrites
`kicad_common.json` on exit exactly like a release one, and the hard rule is that nothing ever
touches `%APPDATA%\kicad` (seed the sandbox root with `kiwrap.sandbox.ensure(...)` or copy the
three lib tables from `share/kicad/template` plus a `kicad_common.json` with the two
`do_not_show_again` flags, otherwise the 10.x Setup wizard blocks the GUI). A short `TEMP` keeps
the IPC pipe name under nng's ~128-character limit.

### 6. What to verify before trusting the build

1. `kicad-cli.exe version --format plain` from the build dir prints the tag's version.
2. `python.exe -c "import pcbnew; print(pcbnew.GetBuildVersion())"` with the vcpkg Python and
   `PYTHONPATH` above (SWIG bindings present).
3. `qa\tests\api` and `qa\pcbnew` targets pass (`ctest --preset` or `ninja test` in the build dir).
4. Symbols: the debug build produces its own PDBs next to the binaries; **do not** point `cdb` at
   `symbols.kicad.org` for a local build — that server only knows the release GUIDs.

## Linux recipe (outline — unverified, for the Phase 0 Linux spike machine or a CI bench)

Ubuntu 24.04 (noble), matching the CI runner and the KiCad 10.0 PPA:

```bash
# Build dependencies straight from the 10.0.6 source package (verified list in the PPA's Sources index):
sudo add-apt-repository -y -s ppa:kicad/kicad-10.0-releases     # -s also enables deb-src
sudo apt-get update
sudo apt-get build-dep -y kicad
sudo apt-get install -y ninja-build ccache lld
# Key Build-Depends for reference: cmake >= 3.21, libwxgtk3.2-dev + libwxgtk-webview3.2-dev, python3-dev,
# swig4.0|swig4.1, python3-wxgtk4.0, libocct-*-dev >= 7.6.3, libngspice-kicad-dev, libnng-dev,
# libprotobuf-dev + protobuf-compiler, libgit2-dev, libsecret-1-dev, libboost-all-dev, libcairo2-dev, libglew-dev.

git clone https://github.com/n3rd-bugs/kicad-fork.git ~/src/kicad && cd ~/src/kicad
git remote add upstream https://gitlab.com/kicad/code/kicad.git && git fetch upstream --tags && git checkout 10.0
cp CMakePresets.json.sample CMakePresets.json
cmake --preset linux-debug -DCMAKE_CXX_FLAGS=-fuse-ld=lld -DKICAD_BUILD_QA_TESTS=ON   # build/linux-debug, Ninja
cmake --build build/linux-debug --parallel
```

`CMakePresets.json.sample` defines `linux-base` (Ninja, `build/<preset>`), `linux-debug`
(`Debug`) and `linux-release` (`RelWithDebInfo`). Run from the build dir with
`KICAD_RUN_FROM_BUILD_DIR=1` and the same sandboxed `KICAD_CONFIG_HOME` / `KICAD_DOCUMENTS_HOME` /
`KICAD_CACHE_HOME`; GUI runs on a headless box go under `xvfb-run -a`. The SWIG module is
`build/linux-debug/pcbnew/pcbnew.py` + `_pcbnew.so` and must be imported by the **system**
`python3` whose `python3-dev` headers were used (no bundled interpreter on Linux — see
[LINUX.md](../LINUX.md)). Expect roughly the same CPU time as Windows minus the vcpkg step; disk
≈ 10–20 GB.

## After the trigger is resolved

Delete `build/` (and on Windows consider `vcpkg\buildtrees`) to reclaim 40–80 GB, keep the
checkout for `.srcpath` use with `cdb`, and note in the PR whether the build changed the
conclusion that the shipped tools never depend on a patched KiCad (policy in ARCHITECTURE.md §5.2).
