# Developer environment for the local KiCad builds (fork n3rd-bugs/kicad-source-mirror, remote
# `public` in C:\src\kicad; base 6dd893f7d1 = nightly 10.99.0-4083).  Dot-source it:
#     $env:KICAD_BUILD_PRESET = 'msvc-win64-release'   # or msvc-win64-debug (default)
#     . C:\src\kicad-env.ps1
# Then: kicad-cli version --format plain ; kicad-cli api-server --socket C:\src\kicad-sandbox\tmp\a.sock <file>
# Rebuild after a source change:  cmake --build C:\src\kicad\build\msvc-win64-debug --parallel 22
# Reconfigure (vcpkg deps already built, ~1 min):  cmake --preset msvc-win64-debug   (from C:\src\kicad)
#
# Verified 2026-09-19: VS 2022 17.14.37710 (MSVC 14.44.35207), vcpkg 66c0373d, official CMake 3.31.5
# first on PATH (the MinGW cmake under ~\tools\mingw64 breaks wxWidgets' WebView2 download: no
# Windows cert store), 141 vcpkg packages built locally in 84 min (KiCad's NuGet cache misses on
# this compiler servicing), KiCad itself in 10 min on 24 threads.

$vs = "C:\Program Files\Microsoft Visual Studio\2022\Community"
Import-Module "$vs\Common7\Tools\Microsoft.VisualStudio.DevShell.dll"
Enter-VsDevShell -VsInstallPath $vs -SkipAutomaticLocation -DevCmdArguments '-arch=x64 -host_arch=x64' | Out-Null

# Pick the tree with $env:KICAD_BUILD_PRESET (msvc-win64-debug = stepping tree, default;
# msvc-win64-release = the RelWithDebInfo acceptance build, docs/dev/kicad-source-build.md).
$preset = if ($env:KICAD_BUILD_PRESET) { $env:KICAD_BUILD_PRESET } else { "msvc-win64-debug" }
$script:KICAD_BUILD = "C:\src\kicad\build\$preset"
# The release tree was configured with -DVCPKG_INSTALLED_DIR=<debug tree>/vcpkg_installed (no second vcpkg run).
$vcpkgInstalled = "C:\src\kicad\build\msvc-win64-debug\vcpkg_installed"
$env:VCPKG_ROOT = "C:\vcpkg"
$env:VCPKG_BINARY_SOURCES = "clear;nuget,kicad-gitlab,read"

# Every directory in the build tree that holds a KiCad DLL (kicommon, kigal, kicad_3dsg, ...), then vcpkg's.
$kicadDllDirs = Get-ChildItem $KICAD_BUILD -Recurse -Filter "*.dll" |
    Where-Object { $_.FullName -notlike "*vcpkg_installed*" -and $_.FullName -notlike "*\qa\*" } |
    Select-Object -ExpandProperty DirectoryName -Unique
# vcpkg's debug DLLs first for the Debug tree only: some ports ship one DLL name for both variants.
$vcpkgBins = if ($preset -like "*debug*") { "$vcpkgInstalled\x64-windows\debug\bin;$vcpkgInstalled\x64-windows\bin" }
             else { "$vcpkgInstalled\x64-windows\bin;$vcpkgInstalled\x64-windows\debug\bin" }
$env:Path = "C:\Program Files\CMake\bin;" + ($kicadDllDirs -join ";") + ";$KICAD_BUILD\kicad;$vcpkgBins;" +
    (($env:Path -split ";" | Where-Object { $_ -notlike "*tools\mingw64*" }) -join ";")

# Run from the build dir, never against the user's real KiCad config (kiwrap hard rule).
$env:KICAD_RUN_FROM_BUILD_DIR = "1"
foreach ($d in "config", "documents", "cache", "tmp") { New-Item -ItemType Directory -Force "C:\src\kicad-sandbox\$d" | Out-Null }
$env:KICAD_CONFIG_HOME    = "C:\src\kicad-sandbox\config"
$env:KICAD_DOCUMENTS_HOME = "C:\src\kicad-sandbox\documents"
$env:KICAD_CACHE_HOME     = "C:\src\kicad-sandbox\cache"
$env:TEMP = "C:\src\kicad-sandbox\tmp"; $env:TMP = $env:TEMP     # short: IPC pipe path limit ~128 chars

Write-Host "kicad build env ready: $(kicad-cli version --format plain)  ($KICAD_BUILD)"
