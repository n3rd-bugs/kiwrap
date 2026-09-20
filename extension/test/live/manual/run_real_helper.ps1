# Manual end-to-end run of the Live KiCad tab against the REAL helper (helper/target/release/kiwrap-live.exe)
# and a sandboxed KiCad GUI, without ext-core / kw_live: the test harness extension attaches to the pid.
#
#   cd C:\Upwork\KiCad\extension
#   .\test\live\manual\run_real_helper.ps1 [-KiCadRoot "C:\Program Files\KiCad\10.0"] [-Inject none|hybrid|events]
#        [-Editor pcbnew|eeschema] [-HelperExe <path>] [-AttachPid <pid>] [-Scratch <dir>] [-KeepOpen] [-ShotAfter 12]
#
# What it does (process hygiene per CLAUDE.md): copies the ecc83 demo to the scratch dir, builds a sandboxed env with
# kiwrap.sandbox (prototypes/display/winstream/kicad_env.py; %APPDATA%\kicad untouched), launches ONE pcbnew.exe (or
# eeschema.exe) on the copy with a short TEMP, launches the Extension Development Host in an isolated profile with the
# harness auto-attaching the helper to that pid and opening the file as a Live KiCad tab, captures a screenshot after
# -ShotAfter seconds and, unless -KeepOpen, kills the dev host (by profile path), the helper and the KiCad by PID.
# With -AttachPid it attaches to a KiCad you already run (nothing is launched or killed on the KiCad side).
# -Inject events needs a KiCad built with mr/display-input started with KICAD_INPUT_FROM_EVENTS=cloak (-Cloak).
param(
  [string]$KiCadRoot = 'C:\Program Files\KiCad\10.0',
  [ValidateSet('none', 'hybrid', 'mirror', 'events')] [string]$Inject = 'none',
  [ValidateSet('pcbnew', 'eeschema')] [string]$Editor = 'pcbnew',
  [string]$HelperExe = '',
  [int]$AttachPid = 0,
  [string]$Scratch = '',
  [switch]$Cloak,
  [switch]$KeepOpen,
  [int]$ShotAfter = 12
)
$ErrorActionPreference = 'Stop'
$ext = Resolve-Path (Join-Path $PSScriptRoot '..\..\..')
$repo = Resolve-Path (Join-Path $ext '..')
if (-not $Scratch) { $Scratch = Join-Path $env:TEMP 'kiwrap-live-manual' }
New-Item -ItemType Directory -Force $Scratch | Out-Null
$env:Path = "C:\Program Files\nodejs;$env:Path"
if (-not $HelperExe) {
  foreach ($c in @((Join-Path $ext 'bin\kiwrap-live.exe'), (Join-Path $repo 'helper\target\release\kiwrap-live.exe'), (Join-Path $repo 'helper\target\debug\kiwrap-live.exe'))) { if (Test-Path $c) { $HelperExe = $c; break } }
}
if (-not $HelperExe -or -not (Test-Path $HelperExe)) { throw "kiwrap-live.exe not found: build helper/ (cargo build --release) or pass -HelperExe" }
Write-Host "[manual] helper: $HelperExe"

# 1. harness extension
$harness = Join-Path $Scratch 'harness'
node (Join-Path $ext 'test\live\harness\build.mjs') $harness
if ($LASTEXITCODE -ne 0) { throw 'harness build failed' }

# 2. sandboxed KiCad on a scratch copy of the demo
$kicadPid = $AttachPid
$file = ''
$ws = Join-Path $Scratch 'ws'
if ($AttachPid -eq 0) {
  $envJson = Join-Path $Scratch 'env.json'
  Push-Location $repo
  try {
    & uv run python (Join-Path $repo 'prototypes\display\winstream\kicad_env.py') --kicad-root $KiCadRoot --storage (Join-Path $Scratch 'kiwrap-home') | Set-Content -Encoding utf8 $envJson
    if ($LASTEXITCODE -ne 0) { throw 'kicad_env.py failed' }
  } finally { Pop-Location }
  $envObj = Get-Content $envJson -Raw | ConvertFrom-Json
  $demo = Join-Path $envObj.demos 'ecc83'
  New-Item -ItemType Directory -Force $ws | Out-Null
  Copy-Item -Recurse -Force $demo (Join-Path $ws 'ecc83')
  $file = if ($Editor -eq 'pcbnew') { Join-Path $ws 'ecc83\ecc83-pp.kicad_pcb' } else { Join-Path $ws 'ecc83\ecc83-pp.kicad_sch' }
  $exe = if ($Editor -eq 'pcbnew') { $envObj.pcbnew } else { $envObj.eeschema }
  $saved = @{}
  foreach ($p in $envObj.env.PSObject.Properties) { $saved[$p.Name] = [Environment]::GetEnvironmentVariable($p.Name); [Environment]::SetEnvironmentVariable($p.Name, $p.Value) }
  if ($Cloak) { $saved['KICAD_INPUT_FROM_EVENTS'] = $env:KICAD_INPUT_FROM_EVENTS; $env:KICAD_INPUT_FROM_EVENTS = 'cloak' }
  try {
    $proc = Start-Process -FilePath $exe -ArgumentList ('"' + $file + '"') -PassThru
    $kicadPid = $proc.Id
  } finally { foreach ($k in $saved.Keys) { [Environment]::SetEnvironmentVariable($k, $saved[$k]) } }
  Write-Host "[manual] launched $Editor pid=$kicadPid on $file (sandbox root: $($envObj.env.KICAD_CONFIG_HOME))"
  Start-Sleep -Seconds 6
} else {
  New-Item -ItemType Directory -Force $ws | Out-Null
  Write-Host "[manual] attaching to existing pid $AttachPid"
}

# 3. dev host with the harness auto-attaching
$udd = Join-Path $Scratch 'udd'; $exts = Join-Path $Scratch 'exts'
New-Item -ItemType Directory -Force (Join-Path $udd 'User'), $exts | Out-Null
@{ 'kiwrap.live.helperPath' = $HelperExe; 'kiwrap.live.mode' = 'auto'; 'kiwrap.live.hud' = $true; 'workbench.startupEditor' = 'none'; 'update.mode' = 'none'; 'telemetry.telemetryLevel' = 'off'; 'security.workspace.trust.enabled' = $false } |
  ConvertTo-Json | Set-Content -Encoding utf8 (Join-Path $udd 'User\settings.json')
$env:KIWRAP_HARNESS_ATTACH_PID = "$kicadPid"
$env:KIWRAP_HARNESS_INJECT = $Inject
$env:KIWRAP_HARNESS_OPEN_FILE = $file
$codeExe = Join-Path $env:LOCALAPPDATA 'Programs\Microsoft VS Code\Code.exe'
if (-not (Test-Path $codeExe)) { $codeExe = 'C:\Program Files\Microsoft VS Code\Code.exe' }
$args = @("--extensionDevelopmentPath=$harness", '--user-data-dir', $udd, '--extensions-dir', $exts, '--disable-extensions', '--disable-workspace-trust', '--skip-welcome', '--skip-release-notes', '--new-window', $ws)
$code = Start-Process -FilePath $codeExe -ArgumentList $args -PassThru
Write-Host "[manual] dev host started (pid $($code.Id)); the harness attaches the helper to pid $kicadPid with --inject $Inject and opens the Live KiCad tab"
Write-Host "[manual] commands in the dev host: kiwrap: Live KiCad: Status / Show KiCad window in a tab / Screenshot surface / Close session"

# 4. screenshot for the record
Start-Sleep -Seconds $ShotAfter
$shot = Join-Path $Scratch ("live-real-helper-{0}-{1}.png" -f $Editor, $Inject)
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $ext 'test\live\integration\capture_window.ps1') -Out $shot -Udd $udd
Write-Host "[manual] screenshot: $shot"
$log = Get-ChildItem (Join-Path $udd 'logs') -Recurse -Filter '*.log' -ErrorAction SilentlyContinue | Where-Object { $_.FullName -like '*exthost*' } | Select-Object -First 1
if ($log) { Write-Host "[manual] exthost log: $($log.FullName)" }

if ($KeepOpen) { Write-Host "[manual] -KeepOpen: leaving the dev host (profile $udd) and KiCad pid $kicadPid running; kill them yourself"; exit 0 }

# 5. cleanup by pid / profile path (never by process name alone: other agents may run their own KiCad)
Start-Sleep -Seconds 1
Get-CimInstance Win32_Process -Filter "Name='Code.exe'" | Where-Object { $_.CommandLine -like "*$udd*" } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 2
Get-CimInstance Win32_Process -Filter "Name='kiwrap-live.exe'" | Where-Object { $_.CommandLine -like "*--pid $kicadPid *" -or $_.CommandLine -like "*--pid $kicadPid" } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
if ($AttachPid -eq 0 -and $kicadPid) { Stop-Process -Id $kicadPid -Force -ErrorAction SilentlyContinue; Write-Host "[manual] killed $Editor pid $kicadPid" }
Write-Host '[manual] done'
