<#
.SYNOPSIS
Run KiCad's IPC API test binary (qa_api.exe, Boost.Test) from a build tree with the environment it
actually needs (docs/dev/kicad-source-build.md "Bench acceptance", verified 2026-09-19).

.DESCRIPTION
Facts that decide the invocation (read from qa/CMakeLists.txt `setup_qa_env`, common/paths.cpp and
qa/tests/api/api_e2e_utils.h):
- The working directory does not matter. Test data paths are compiled in (QA_PCBNEW_DATA_LOCATION,
  QA_KICAD_CLI_PATH) and, with KICAD_RUN_FROM_BUILD_DIR=1, stock data resolves to the EXE's parent
  directory (PATHS::getWindowsKiCadRoot = <exe dir>/..). For kicad-cli.exe that is the build root
  (which has schemas/); for qa/tests/api/qa_api.exe it is qa/tests/, so the ApiWizards plugin
  manager looks for qa/tests/schemas/api.v1.schema.json. This script creates that directory as a
  junction to <build>/schemas (KiCad's own test_remote_provider_client.cpp works around the same
  thing on macOS with KICAD_STOCK_DATA_HOME).
- PATH needs every build-tree directory holding a KiCad DLL (ctest prepends kicad/, common/, api/,
  common/gal/, pcbnew/, eeschema/, cvpcb/) plus vcpkg's bin (debug/bin for a Debug build).
- `ctest -R qa_api` applies a 180 s TIMEOUT property; a Debug build exceeds it because two wx
  asserts wedge the shared api-server and the 10 s nng timeouts cascade, so run the exe directly.
- The e2e fixture launches kicad-cli api-server with the inherited environment: sandboxed
  KICAD_*_HOME and a short TEMP are set here so nothing touches %APPDATA%\kicad.

.EXAMPLE
.\scripts\dev\run_qa_api.ps1 -BuildDir C:\src\kicad\build\msvc-win64-release
.\scripts\dev\run_qa_api.ps1 -BuildDir C:\src\kicad\build\msvc-win64-debug -RunTest ApiJobs
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string] $BuildDir,
    [string] $RunTest = "",
    [string] $LogLevel = "test_suite",
    [string] $Sandbox = "C:\src\kicad-sandbox",
    [string] $LogFile = ""   # default %TEMP%\qa_api.log (+ .stderr)
)

$ErrorActionPreference = "Stop"
$cacheText = Get-Content (Join-Path $BuildDir "CMakeCache.txt")
$buildType = ($cacheText | Select-String '^CMAKE_BUILD_TYPE:STRING=(.*)$').Matches[0].Groups[1].Value
$triplet = ($cacheText | Select-String '^VCPKG_TARGET_TRIPLET:STRING=(.*)$').Matches[0].Groups[1].Value
$installedDir = ($cacheText | Select-String '^VCPKG_INSTALLED_DIR:PATH=(.*)$').Matches[0].Groups[1].Value
if (-not $installedDir) { $installedDir = Join-Path $BuildDir "vcpkg_installed" }
$vcpkgBin = if ($buildType -eq "Debug") { Join-Path $installedDir "$triplet\debug\bin" } else { Join-Path $installedDir "$triplet\bin" }

$dllDirs = Get-ChildItem $BuildDir -Recurse -Filter "*.dll" |
    Where-Object { $_.FullName -notlike "*vcpkg_installed*" -and $_.FullName -notlike "*\qa\*" } |
    Select-Object -ExpandProperty DirectoryName -Unique
$env:Path = (($dllDirs + @((Join-Path $BuildDir "kicad"), $vcpkgBin)) -join ";") + ";" + $env:Path

$schemaLink = Join-Path $BuildDir "qa\tests\schemas"
if (-not (Test-Path $schemaLink)) { New-Item -ItemType Junction -Path $schemaLink -Target (Join-Path $BuildDir "schemas") | Out-Null }

foreach ($d in "config", "documents", "cache", "tmp") { New-Item -ItemType Directory -Force (Join-Path $Sandbox $d) | Out-Null }
$env:KICAD_RUN_FROM_BUILD_DIR = "1"
$env:KICAD_CONFIG_HOME = Join-Path $Sandbox "config"
$env:KICAD_DOCUMENTS_HOME = Join-Path $Sandbox "documents"
$env:KICAD_CACHE_HOME = Join-Path $Sandbox "cache"
$env:TEMP = Join-Path $Sandbox "tmp"; $env:TMP = $env:TEMP

$exe = Join-Path $BuildDir "qa\tests\api\qa_api.exe"
$args = @("--log_level=$LogLevel", "--report_level=short")
if ($RunTest) { $args += "--run_test=$RunTest" }
$t = Get-Date
# Start-Process, not a pipeline: the shared e2e api-server (a kicad-cli child) can outlive qa_api and
# would hold the pipe open, so `& $exe | Out-File` never returns; -Wait waits for qa_api only.
if (-not $LogFile) { $LogFile = Join-Path $env:TEMP "qa_api.log" }
$errFile = "$LogFile.stderr"
$proc = Start-Process -FilePath $exe -ArgumentList $args -NoNewWindow -Wait -PassThru `
    -RedirectStandardOutput $LogFile -RedirectStandardError $errFile
$code = $proc.ExitCode
Get-Process kicad-cli -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Get-Content $errFile | Where-Object { $_ -match "error|fatal|out of" }
Get-Content $LogFile | Where-Object { $_ -match "error|out of|Leaving test module" }
Write-Host "qa_api ($buildType) exit=$code elapsed=$((Get-Date) - $t)"
exit $code
