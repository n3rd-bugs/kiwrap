# Building KiCad from source (gated developer diagnostic)

Status: **recipe only, not executed.** Decision D1 ([DECISIONS.md](../DECISIONS.md)) and
[ARCHITECTURE.md §5.2](../ARCHITECTURE.md#52-source-build--defer-as-a-gated-developer-diagnostic)
defer the build until a *named trigger* fires. Everything kiwrap ships runs against stock
installers; a patched KiCad is never a dependency, and conformance (T1–T9) runs against installed
binaries only. The Windows recipe below was assembled from the official dev-docs, the
`kicad-win-builder` scripts and the toolchain inventory of this machine
([research/09](../research/09-kicad-plugin-and-build.md), spike
`spikes/kicad-plugin-and-build/kwb_build.ps1`); the Linux outline is from dev-docs and the
10.0.6 PPA source package. Neither has been run here — expect small drift and fix the doc when
you run it.

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

The fork exists already: <https://github.com/n3rd-bugs/kicad-source-mirror> (fork of
`KiCad/kicad-source-mirror`, default branch `master`, created with `gh repo fork`). Clone it
**outside this repo** and at a short path (vcpkg and MSVC both choke on long paths):

```powershell
git clone https://github.com/n3rd-bugs/kicad-source-mirror.git C:\src\kicad
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

git clone https://github.com/n3rd-bugs/kicad-source-mirror.git ~/src/kicad && cd ~/src/kicad
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
