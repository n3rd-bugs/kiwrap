<#
.SYNOPSIS
Install a KiCad build tree as a self-contained prefix kiwrap can discover (docs/UPSTREAM.md 4.3,
docs/dev/kicad-source-build.md "Bench acceptance").

.DESCRIPTION
Verified 2026-09-19 on the fork n3rd-bugs/kicad-source-mirror at 6dd893f7d1 (= nightly 10.99.0-4083):

1. KiCad bakes KICAD_DATA / KICAD_DOCS / KICAD_PLUGINS / KICAD_DEMOS / KICAD_TEMPLATE as ABSOLUTE
   cache strings under CMAKE_INSTALL_PREFIX at configure time (also into config.h, which 21 sources
   include), so `cmake --install --prefix <other>` puts bin/ under <other> but the share tree under
   the baked prefix.  Installing a second prefix from one tree therefore silently overwrote the
   first prefix's share/ (and an earlier version of this script moved it away).  Now the tree is
   RECONFIGURED for the requested prefix (cmake -D of every baked path, 15 s) and rebuilt (the
   config.h dependents, ~1 min) before `cmake --install`, and no other prefix is ever touched.  On
   Windows KiCad resolves share/ relative to the exe at run time (PATHS::getWindowsKiCadRoot), so
   the baked path never matters for the installed binaries.
2. Nothing installs the vcpkg DLLs (X_VCPKG_APPLOCAL_DEPS_INSTALL is OFF): copy every
   vcpkg_installed/<triplet>/[debug/]bin/*.dll into bin/, exactly what kicad-win-builder's build.ps1
   does for its package.
3. The stock libraries are separate packages: junction share/kicad/{symbols,footprints,3dmodels} to
   an installed nightly's share/kicad and copy its template/{sym,fp}-lib-table (the tables reference
   ${KICAD10_SYMBOL_DIR} etc.; `kiwrap.sandbox` exports those from the install's share dirs).
4. Write <prefix>/kiwrap-install.json (build tree, BUILT commit, base commit, branch, preset, build
   type, patches) so `kiwrap.installation.discover()` reports the install with source "explicit"
   plus that metadata, and register the prefix in <storage>/installs.json
   (`python -m kiwrap.installation --register`).  `commit` is what the source tree's HEAD was when
   the tree was built (`git rev-parse --short=10 HEAD`), never the base: a pristine build and a
   patched build of the same base must get different T4/T5 snapshot names
   (`<version>+<commit>[.debug].json`).

Run it from the developer shell (`. C:\src\kicad-env.ps1` with $env:KICAD_BUILD_PRESET set to the
tree's preset): the reconfigure and the incremental build need the MSVC environment and the official
CMake.  A Debug build (msvc-win64-debug) is installable but wx asserts wedge the headless api-server
on some commands; use msvc-win64-release (RelWithDebInfo) for the T5-equality bench.

.EXAMPLE
.\scripts\dev\install_kicad_build.ps1 -BuildDir C:\src\kicad\build\msvc-win64-release `
    -Prefix C:\kiwrap-builds\6dd893f7d1-2 -Base 6dd893f7d1 -Register
.\scripts\dev\install_kicad_build.ps1 -BuildDir C:\src\kicad\build\msvc-win64-release `
    -Prefix C:\kiwrap-builds\6dd893f7d1-4 -Base 6dd893f7d1 -Patches ratsnest-export `
    -Tag kiwrap-build/6dd893f7d1-4 -Register
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string] $BuildDir,
    [Parameter(Mandatory)] [string] $Prefix,
    [Parameter(Mandatory)] [string] $Base,      # the upstream commit the build is based on (base/<sha>)
    [string] $Commit = "",                      # built commit; default: HEAD of the tree's source dir
    [string] $LibrariesFrom = "C:\Program Files\KiCad\10.99\share\kicad",
    [string[]] $Patches = @(),
    [string] $Branch = "",                      # default: the source dir's current branch
    [string] $Tag = "",
    [switch] $Register,
    [switch] $PersistEnv,
    [switch] $Force                             # replace an existing install at $Prefix
)

$ErrorActionPreference = "Stop"
$cache = Join-Path $BuildDir "CMakeCache.txt"
if (-not (Test-Path $cache)) { throw "$BuildDir has no CMakeCache.txt" }
$cacheText = Get-Content $cache
function CacheValue([string] $pattern) {
    $m = ($cacheText | Select-String $pattern).Matches
    if ($m.Count -eq 0) { return "" }
    return $m[0].Groups[1].Value
}
$buildType = CacheValue '^CMAKE_BUILD_TYPE:STRING=(.*)$'
$cachePrefix = CacheValue '^CMAKE_INSTALL_PREFIX:PATH=(.*)$'
$triplet = CacheValue '^VCPKG_TARGET_TRIPLET:STRING=(.*)$'
$installedDir = CacheValue '^VCPKG_INSTALLED_DIR:PATH=(.*)$'
$sourceDir = CacheValue '^CMAKE_HOME_DIRECTORY:INTERNAL=(.*)$'
if (-not $installedDir) { $installedDir = Join-Path $BuildDir "vcpkg_installed" }
$preset = Split-Path $BuildDir -Leaf
if (-not (Get-Command cl.exe -ErrorAction SilentlyContinue)) {
    throw "cl.exe is not on PATH: run this from the developer shell (. C:\src\kicad-env.ps1)"
}
if (-not $Commit) { $Commit = (& git -C $sourceDir rev-parse --short=10 HEAD).Trim() }
if (-not $Branch) { $Branch = (& git -C $sourceDir rev-parse --abbrev-ref HEAD).Trim() }
$dirty = (& git -C $sourceDir status --porcelain --untracked-files=no) | Measure-Object | Select-Object -ExpandProperty Count
if ($dirty -gt 0) { throw "$sourceDir has uncommitted changes; the metadata would misreport the built commit" }

$prefixFwd = ($Prefix -replace '\\', '/').TrimEnd('/')
Write-Host "build type $buildType, triplet $triplet, source $sourceDir @ $Commit ($Branch), baked prefix $cachePrefix"

if (Test-Path (Join-Path $Prefix "kiwrap-install.json")) {
    if (-not $Force) { throw "$Prefix is already an install; pass -Force to replace it (or pick the next -<n>)" }
    Write-Host "removing the existing install at $Prefix"
    # The library junctions point INTO the nightly's share tree: unlink them first (DirectoryInfo.Delete
    # removes a reparse point only), because Remove-Item -Recurse follows junctions on PowerShell 5.1.
    Get-ChildItem $Prefix -Recurse -Force -Attributes ReparsePoint -ErrorAction SilentlyContinue |
        ForEach-Object { $_.Delete() }
    Remove-Item -Recurse -Force $Prefix
}

# 1. every baked install path must point at THIS prefix before cmake --install runs
if ($cachePrefix -ne $prefixFwd) {
    Write-Host "reconfiguring $BuildDir for prefix $prefixFwd (baked: $cachePrefix)"
    & cmake -B $BuildDir "-DCMAKE_INSTALL_PREFIX=$prefixFwd" "-DKICAD_DATA=$prefixFwd/share/kicad" `
        "-DKICAD_LIBRARY_DATA=$prefixFwd/share/kicad" "-DKICAD_DOCS=$prefixFwd/share/doc/kicad" `
        "-DKICAD_PLUGINS=$prefixFwd/share/kicad/plugins" "-DKICAD_DEMOS=$prefixFwd/share/kicad/demos" `
        "-DKICAD_TEMPLATE=$prefixFwd/share/kicad/template" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "cmake reconfigure failed ($LASTEXITCODE)" }
}
# incremental build: a no-op when the tree is current, the config.h dependents after a reconfigure
& cmake --build $BuildDir --parallel | Select-Object -Last 1
if ($LASTEXITCODE -ne 0) { throw "cmake --build failed ($LASTEXITCODE)" }

# 2. binaries + share tree (both under $Prefix now)
& cmake --install $BuildDir --prefix $Prefix | Out-Null
if ($LASTEXITCODE -ne 0) { throw "cmake --install failed ($LASTEXITCODE)" }
if (-not (Test-Path (Join-Path $Prefix "share\kicad\schemas\api.v1.schema.json"))) {
    throw "share/kicad/schemas/api.v1.schema.json missing under $Prefix (kiwrap's ipc.available probe needs it)"
}
if (-not (Test-Path (Join-Path $Prefix "share\kicad\demos\ecc83"))) {
    throw "share/kicad/demos/ecc83 missing under $Prefix (the T5 bench probe and the suite copy it)"
}

# 3. vcpkg DLLs (debug variants for a Debug build)
$vcpkgBin = if ($buildType -eq "Debug") { Join-Path $installedDir "$triplet\debug\bin" } else { Join-Path $installedDir "$triplet\bin" }
robocopy $vcpkgBin (Join-Path $Prefix "bin") *.dll /NFL /NDL /NJH /NJS /R:1 /W:1 | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy of $vcpkgBin failed ($LASTEXITCODE)" }

# 4. libraries from the installed nightly
$share = Join-Path $Prefix "share\kicad"
foreach ($d in "symbols", "footprints", "3dmodels") {
    $target = Join-Path $LibrariesFrom $d
    $link = Join-Path $share $d
    if (-not (Test-Path $target)) { Write-Warning "$target missing; $d not linked"; continue }
    if (-not (Test-Path $link)) { New-Item -ItemType Junction -Path $link -Target $target | Out-Null }
}
foreach ($t in "sym-lib-table", "fp-lib-table", "design-block-lib-table") {
    $src = Join-Path $LibrariesFrom "template\$t"
    if (Test-Path $src) { Copy-Item $src (Join-Path $share "template\") -Force }
}

# 5. metadata + registration
$meta = [ordered]@{
    schema = 1
    build_tree = ($BuildDir -replace '\\', '/')
    source_tree = ($sourceDir -replace '\\', '/')
    commit = $Commit
    base = $Base
    branch = $Branch
    tag = $Tag
    kiwrap_patches = @($Patches)
    preset = $preset
    build_type = $buildType
    installed_at = (Get-Date -Format "yyyy-MM-dd")
    libraries_from = ($LibrariesFrom -replace '\\', '/')
}
# no BOM (Windows PowerShell 5.1's -Encoding utf8 writes one; kiwrap reads utf-8-sig anyway)
[IO.File]::WriteAllText((Join-Path $Prefix "kiwrap-install.json"), ($meta | ConvertTo-Json), (New-Object Text.UTF8Encoding $false))

# The installed binary must find share/ next to its bin/ like an installer's would: the developer
# shell's KICAD_RUN_FROM_BUILD_DIR would make it look for <prefix>/schemas instead.
$env:KICAD_RUN_FROM_BUILD_DIR = ""
$env:KICAD_CONFIG_HOME = Join-Path $env:TEMP "kiwrap-install-check\config"
$env:KICAD_DOCUMENTS_HOME = Join-Path $env:TEMP "kiwrap-install-check\documents"
$env:KICAD_CACHE_HOME = Join-Path $env:TEMP "kiwrap-install-check\cache"
$cli = Join-Path $Prefix "bin\kicad-cli.exe"
$version = cmd /c "`"$cli`" version --format plain 2>&1"
if ($LASTEXITCODE -ne 0) { throw "installed kicad-cli does not run (exit $LASTEXITCODE): $version" }
Write-Host "installed $Prefix : kicad-cli $version ($buildType, commit $Commit on base $Base, patches: $($Patches -join ','))"

if ($Register) {
    Push-Location (Join-Path $PSScriptRoot "..\..")
    try { uv run python -m kiwrap.installation --no-probe --register $Prefix } finally { Pop-Location }
}
if ($PersistEnv) {
    $roots = [Environment]::GetEnvironmentVariable("KIWRAP_KICAD_ROOTS", "User")
    $list = @($roots -split ";" | Where-Object { $_ -and $_ -ne $Prefix }) + $Prefix
    [Environment]::SetEnvironmentVariable("KIWRAP_KICAD_ROOTS", ($list -join ";"), "User")
    Write-Host "KIWRAP_KICAD_ROOTS (user) = $($list -join ';')"
}
