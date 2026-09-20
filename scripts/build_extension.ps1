<#
.SYNOPSIS
    Build the kiwrap VS Code extension and its Live KiCad helper into dist/kiwrap-<version>.vsix.

.DESCRIPTION
    Windows only (docs/DECISIONS.md D18). Private: this script packages, it never publishes (D19).
    Steps, in order (each one is a named step in the summary; the first failure stops the run):

      1. toolchains   Node.js >= 20 + npm (PATH, else C:\Program Files\nodejs), Rust stable-msvc + cargo
                      (PATH, else %USERPROFILE%\.cargo\bin). Nothing is installed; missing tools are an
                      error with the download URL (scripts/bootstrap.ps1 reports the same facts).
      2. npm ci       in extension/ (npm install --no-audit --no-fund when there is no package-lock.json)
      3. esbuild      `npm run build` (falls back to `vscode:prepublish` / `compile`); the file named by
                      package.json "main" must exist afterwards
      4. helper       `cargo build --release` in the helper crate (default: the first of <repo>/helper,
                      <repo>/extension/helper, <repo>/kiwrap-live that has a Cargo.toml; -HelperDir overrides),
                      then the produced kiwrap-live.exe is copied to extension/bin/ (the extension launches
                      it from there: docs/EXTENSION.md section 3). -SkipHelper keeps whatever is in
                      extension/bin (a VSIX without the helper still works for previews and diagnostics;
                      the Live KiCad view then reports "helper missing").
      5. tests        only with -RunTests: `cargo test` and `npm test` (@vscode/test-electron downloads a VS
                      Code build into extension/.vscode-test on the first run)
      6. vsce         `vsce package` (the extension's @vscode/vsce devDependency, else npx @vscode/vsce)
                      -> <repo>/dist/kiwrap-<version>.vsix, then the archive is opened and checked for
                      extension/bin/kiwrap-live.exe (unless the helper was skipped and absent).
                      `--no-dependencies` is passed (esbuild bundles the runtime dependencies such as
                      @modelcontextprotocol/sdk into dist/extension.js, so node_modules must not be
                      shipped); -IncludeDependencies lets vsce add the production node_modules instead.

    Install the result with `code --install-extension dist\kiwrap-<version>.vsix` (extension/README.md).

.PARAMETER SkipHelper
    Do not run cargo; package the extension with whatever extension/bin/ already holds.

.PARAMETER SkipInstall
    Do not run `npm ci` (node_modules/ is already in place).

.PARAMETER RunTests
    Run `cargo test` and `npm test` before packaging.

.PARAMETER NoPackage
    Stop after the build steps (no VSIX). Useful for the Extension Development Host loop:
    `code --extensionDevelopmentPath=<repo>\extension --new-window`.

.PARAMETER IncludeDependencies
    Let vsce include the production node_modules (only for an extension that is NOT bundled).

.PARAMETER ExtensionDir
    The extension's directory (default: <repo>/extension).

.PARAMETER HelperDir
    The helper crate's directory (default: auto-detected, see step 4).

.PARAMETER OutDir
    Where the VSIX goes (default: <repo>/dist; git-ignored).

.PARAMETER HelperExe
    File name of the helper executable (default: kiwrap-live.exe; the contract name in docs/EXTENSION.md).

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\build_extension.ps1
    powershell -ExecutionPolicy Bypass -File scripts\build_extension.ps1 -SkipHelper -NoPackage
    powershell -ExecutionPolicy Bypass -File scripts\build_extension.ps1 -RunTests
#>
[CmdletBinding()]
param(
    [switch]$SkipHelper,
    [switch]$SkipInstall,
    [switch]$RunTests,
    [switch]$NoPackage,
    [switch]$IncludeDependencies,
    [string]$ExtensionDir = "",
    [string]$HelperDir = "",
    [string]$OutDir = "",
    [string]$HelperExe = "kiwrap-live.exe"
)

Set-StrictMode -Version 2
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$script:RepoRoot = Split-Path -Parent $PSScriptRoot
$script:Steps = New-Object System.Collections.ArrayList
$script:Started = Get-Date

# ----------------------------------------------------------------------------------------------- helpers

function Write-Step {
    param([string]$Name, [string]$State, [string]$Detail = "")
    # State: ok | skip | fail
    [void]$script:Steps.Add([pscustomobject]@{ Step = $Name; State = $State; Detail = $Detail })
    Write-Host ("[{0}] {1}: {2}" -f $State.ToUpperInvariant().PadRight(5), $Name, $Detail)
}

function Fail {
    param([string]$Step, [string]$Message)
    Write-Step $Step 'fail' $Message
    Write-Host ""
    Write-Host "Summary:"
    $script:Steps | ForEach-Object { Write-Host ("  {0,-5} {1}" -f $_.State, $_.Step) }
    Write-Host "FAILED at '$Step': $Message"
    exit 1
}

function Invoke-Checked {
    <#
      Runs a native command with inherited stdout/stderr (the build tools stream progress) and throws on a
      non-zero exit code. Nothing is redirected, so PS 5.1's NativeCommandError trap never triggers.
    #>
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$Arguments = @(),
        [string]$WorkingDirectory = "",
        [hashtable]$Environment = @{}
    )
    $saved = @{}
    foreach ($k in $Environment.Keys) {
        $saved[$k] = [Environment]::GetEnvironmentVariable($k, 'Process')
        [Environment]::SetEnvironmentVariable($k, [string]$Environment[$k], 'Process')
    }
    $prev = Get-Location
    try {
        if ($WorkingDirectory -ne "") { Set-Location -LiteralPath $WorkingDirectory }
        Write-Host ("  > {0} {1}" -f $FilePath, ($Arguments -join ' '))
        $global:LASTEXITCODE = 0
        & $FilePath @Arguments
        if ($LASTEXITCODE -ne 0) { throw ("exit code {0}: {1} {2}" -f $LASTEXITCODE, (Split-Path -Leaf $FilePath), ($Arguments -join ' ')) }
    } finally {
        Set-Location $prev
        foreach ($k in $saved.Keys) { [Environment]::SetEnvironmentVariable($k, $saved[$k], 'Process') }
    }
}

function Get-CommandOutput {
    <# Captured stdout of a native command (short, quiet probes such as `--version`). #>
    param([string]$FilePath, [string[]]$Arguments = @(), [string]$WorkingDirectory = "")
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $FilePath
    $psi.Arguments = ($Arguments | ForEach-Object { if ($_ -match '[\s"]') { '"' + ($_ -replace '"', '\"') + '"' } else { $_ } }) -join ' '
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.CreateNoWindow = $true
    if ($WorkingDirectory -ne "") { $psi.WorkingDirectory = $WorkingDirectory }
    $proc = [System.Diagnostics.Process]::Start($psi)
    $out = $proc.StandardOutput.ReadToEnd()
    $err = $proc.StandardError.ReadToEnd()
    $proc.WaitForExit()
    return [pscustomobject]@{ ExitCode = $proc.ExitCode; StdOut = $out; StdErr = $err }
}

function Find-Tool {
    <# First hit on PATH, else the given fallback paths; $null when absent. #>
    param([string]$Name, [string[]]$Fallbacks = @())
    $cmd = Get-Command $Name -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -ne $cmd) { return $cmd.Source }
    foreach ($p in $Fallbacks) { if (Test-Path -LiteralPath $p) { return $p } }
    return $null
}

function Add-ToPath {
    param([string]$Dir)
    if ($env:Path -notlike "*$Dir*") { $env:Path = "$Dir;$env:Path" }
}

# ----------------------------------------------------------------------------------------------- paths

if ($ExtensionDir -eq "") { $ExtensionDir = Join-Path $script:RepoRoot 'extension' }
$ExtensionDir = [System.IO.Path]::GetFullPath($ExtensionDir)
if ($OutDir -eq "") { $OutDir = Join-Path $script:RepoRoot 'dist' }
$OutDir = [System.IO.Path]::GetFullPath($OutDir)
$PackageJsonPath = Join-Path $ExtensionDir 'package.json'
$BinDir = Join-Path $ExtensionDir 'bin'

Write-Host "kiwrap extension build - repo $script:RepoRoot"
if (-not (Test-Path -LiteralPath $PackageJsonPath)) { Fail 'layout' "$PackageJsonPath not found (the extension lives under <repo>/extension; -ExtensionDir overrides)" }
$Pkg = Get-Content -LiteralPath $PackageJsonPath -Raw | ConvertFrom-Json
$PkgName = [string]$Pkg.name
$PkgVersion = [string]$Pkg.version
if ($PkgName -eq "" -or $PkgVersion -eq "") { Fail 'layout' "package.json needs name and version" }
Write-Host "  extension: $PkgName $PkgVersion ($ExtensionDir)"

if (-not $SkipHelper) {
    if ($HelperDir -eq "") {
        foreach ($cand in @((Join-Path $script:RepoRoot 'helper'), (Join-Path $ExtensionDir 'helper'), (Join-Path $script:RepoRoot 'kiwrap-live'))) {
            if (Test-Path -LiteralPath (Join-Path $cand 'Cargo.toml')) { $HelperDir = $cand; break }
        }
    }
    if ($HelperDir -eq "") { Fail 'layout' "no helper crate found (looked for Cargo.toml under <repo>/helper, <repo>/extension/helper, <repo>/kiwrap-live); pass -HelperDir or -SkipHelper" }
    $HelperDir = [System.IO.Path]::GetFullPath($HelperDir)
    if (-not (Test-Path -LiteralPath (Join-Path $HelperDir 'Cargo.toml'))) { Fail 'layout' "$HelperDir has no Cargo.toml" }
    Write-Host "  helper:    $HelperDir"
}

# ----------------------------------------------------------------------------------------------- 1. toolchains

$NodeExe = Find-Tool 'node' @((Join-Path $env:ProgramFiles 'nodejs\node.exe'))
if ($null -eq $NodeExe) { Fail 'toolchains' "node not found on PATH or under $env:ProgramFiles\nodejs - install Node.js LTS (https://nodejs.org); not installed by this script" }
$NodeDir = Split-Path -Parent $NodeExe
Add-ToPath $NodeDir
$nodeVersion = (Get-CommandOutput $NodeExe @('--version')).StdOut.Trim()
$nodeMajor = 0
if ($nodeVersion -match '^v(\d+)') { $nodeMajor = [int]$Matches[1] }
if ($nodeMajor -lt 20) { Fail 'toolchains' "node $nodeVersion is too old (>= 20 required; Node 24 LTS recommended)" }
$NpmCmd = Join-Path $NodeDir 'npm.cmd'
if (-not (Test-Path -LiteralPath $NpmCmd)) { $NpmCmd = Find-Tool 'npm.cmd' }
if ($null -eq $NpmCmd) { Fail 'toolchains' "npm.cmd not found next to $NodeExe" }
$npmVersion = (Get-CommandOutput $NpmCmd @('--version')).StdOut.Trim()
$NpxCmd = Join-Path (Split-Path -Parent $NpmCmd) 'npx.cmd'
$toolDetail = "node $nodeVersion, npm $npmVersion ($NodeDir)"

$CargoExe = $null
if (-not $SkipHelper) {
    $cargoHome = if ($env:CARGO_HOME) { $env:CARGO_HOME } else { Join-Path $env:USERPROFILE '.cargo' }
    $CargoExe = Find-Tool 'cargo' @((Join-Path $cargoHome 'bin\cargo.exe'))
    if ($null -eq $CargoExe) { Fail 'toolchains' "cargo not found on PATH or under $cargoHome\bin - install Rust stable (https://rustup.rs, x86_64-pc-windows-msvc) or pass -SkipHelper" }
    Add-ToPath (Split-Path -Parent $CargoExe)
    $rustc = Get-CommandOutput (Join-Path (Split-Path -Parent $CargoExe) 'rustc.exe') @('-vV')
    if ($rustc.ExitCode -ne 0) { $rustc = Get-CommandOutput 'rustc' @('-vV') }
    $rustLine = ($rustc.StdOut -split "`r?`n" | Where-Object { $_ -match '^rustc ' } | Select-Object -First 1)
    $hostLine = ($rustc.StdOut -split "`r?`n" | Where-Object { $_ -match '^host: ' } | Select-Object -First 1)
    if ($null -eq $rustLine) { Fail 'toolchains' "rustc -vV failed: $($rustc.StdErr.Trim())" }
    if ($null -ne $hostLine -and $hostLine -notmatch 'x86_64-pc-windows-msvc') {
        Fail 'toolchains' "$hostLine - the helper needs the x86_64-pc-windows-msvc toolchain (rustup default stable-msvc)"
    }
    $cargoVersion = (Get-CommandOutput $CargoExe @('--version')).StdOut.Trim()
    $toolDetail += "; $rustLine, $cargoVersion"
}
Write-Step 'toolchains' 'ok' $toolDetail

# ----------------------------------------------------------------------------------------------- 2. npm ci

if ($SkipInstall) {
    if (-not (Test-Path -LiteralPath (Join-Path $ExtensionDir 'node_modules'))) { Fail 'npm-ci' "-SkipInstall given but $ExtensionDir\node_modules is missing" }
    Write-Step 'npm-ci' 'skip' '-SkipInstall'
} else {
    try {
        if (Test-Path -LiteralPath (Join-Path $ExtensionDir 'package-lock.json')) {
            Invoke-Checked $NpmCmd @('ci', '--no-audit', '--no-fund') -WorkingDirectory $ExtensionDir
            Write-Step 'npm-ci' 'ok' 'npm ci (package-lock.json)'
        } else {
            Write-Host "  no package-lock.json: falling back to npm install (commit the lockfile so CI can use npm ci)"
            Invoke-Checked $NpmCmd @('install', '--no-audit', '--no-fund') -WorkingDirectory $ExtensionDir
            Write-Step 'npm-ci' 'ok' 'npm install (no lockfile)'
        }
    } catch { Fail 'npm-ci' $_.Exception.Message }
}

# ----------------------------------------------------------------------------------------------- 3. esbuild

$scripts = @{}
if ($null -ne $Pkg.PSObject.Properties['scripts'] -and $null -ne $Pkg.scripts) {
    foreach ($p in $Pkg.scripts.PSObject.Properties) { $scripts[$p.Name] = [string]$p.Value }
}
$buildScript = $null
foreach ($cand in @('build', 'vscode:prepublish', 'compile')) { if ($scripts.ContainsKey($cand)) { $buildScript = $cand; break } }
if ($null -eq $buildScript) { Fail 'esbuild' "package.json has no build / vscode:prepublish / compile script" }
try {
    Invoke-Checked $NpmCmd @('run', $buildScript) -WorkingDirectory $ExtensionDir
} catch { Fail 'esbuild' $_.Exception.Message }
$mainRel = if ($null -ne $Pkg.PSObject.Properties['main']) { [string]$Pkg.main } else { 'dist/extension.js' }
$mainRel = ($mainRel -replace '\\', '/') -replace '^\./', ''
$mainPath = Join-Path $ExtensionDir ($mainRel -replace '/', '\')
if (-not (Test-Path -LiteralPath $mainPath)) { Fail 'esbuild' "npm run $buildScript finished but package.json main ($mainRel) does not exist" }
Write-Step 'esbuild' 'ok' ("npm run {0} -> {1} ({2:N0} KB)" -f $buildScript, $mainRel, ((Get-Item -LiteralPath $mainPath).Length / 1KB))

# ----------------------------------------------------------------------------------------------- 4. helper

$HelperInBin = Join-Path $BinDir $HelperExe
if ($SkipHelper) {
    if (Test-Path -LiteralPath $HelperInBin) { Write-Step 'helper' 'skip' "-SkipHelper; using existing $HelperInBin" }
    else { Write-Step 'helper' 'skip' "-SkipHelper; no $HelperInBin - the VSIX ships without the Live KiCad helper" }
} else {
    try {
        $cargoArgs = @('build', '--release')
        if (Test-Path -LiteralPath (Join-Path $HelperDir 'Cargo.lock')) { $cargoArgs += '--locked' }
        Invoke-Checked $CargoExe $cargoArgs -WorkingDirectory $HelperDir
        $meta = Get-CommandOutput $CargoExe @('metadata', '--format-version', '1', '--no-deps') -WorkingDirectory $HelperDir
        if ($meta.ExitCode -ne 0) { throw "cargo metadata failed: $($meta.StdErr.Trim())" }
        $targetDir = [string](($meta.StdOut | ConvertFrom-Json).target_directory)
        $built = Join-Path (Join-Path $targetDir 'release') $HelperExe
        if (-not (Test-Path -LiteralPath $built)) {
            $found = Get-ChildItem -LiteralPath (Join-Path $targetDir 'release') -Filter '*.exe' -File -ErrorAction SilentlyContinue | ForEach-Object { $_.Name }
            throw "cargo built no $built (release exes: $($found -join ', ')); the crate's [[bin]] must be named $([System.IO.Path]::GetFileNameWithoutExtension($HelperExe)) or pass -HelperExe"
        }
        if (-not (Test-Path -LiteralPath $BinDir)) { New-Item -ItemType Directory -Path $BinDir -Force | Out-Null }
        Copy-Item -LiteralPath $built -Destination $HelperInBin -Force
        $pdb = [System.IO.Path]::ChangeExtension($built, '.pdb')
        if (Test-Path -LiteralPath $pdb) { Copy-Item -LiteralPath $pdb -Destination ([System.IO.Path]::ChangeExtension($HelperInBin, '.pdb')) -Force }
        $sha = (Get-FileHash -LiteralPath $HelperInBin -Algorithm SHA256).Hash.ToLowerInvariant()
        Write-Step 'helper' 'ok' ("{0} -> {1} ({2:N0} KB, sha256 {3})" -f $built, $HelperInBin, ((Get-Item -LiteralPath $HelperInBin).Length / 1KB), $sha.Substring(0, 12))
    } catch { Fail 'helper' $_.Exception.Message }
}

# ----------------------------------------------------------------------------------------------- 5. tests

if ($RunTests) {
    if (-not $SkipHelper) {
        try { Invoke-Checked $CargoExe @('test', '--release') -WorkingDirectory $HelperDir; Write-Step 'cargo-test' 'ok' '' }
        catch { Fail 'cargo-test' $_.Exception.Message }
    }
    if ($scripts.ContainsKey('test')) {
        try { Invoke-Checked $NpmCmd @('test') -WorkingDirectory $ExtensionDir; Write-Step 'npm-test' 'ok' '' }
        catch { Fail 'npm-test' $_.Exception.Message }
    } else { Write-Step 'npm-test' 'skip' 'package.json has no test script' }
} else {
    Write-Step 'tests' 'skip' '(-RunTests runs cargo test + npm test)'
}

# ----------------------------------------------------------------------------------------------- 6. vsce package

if ($NoPackage) {
    Write-Step 'vsce' 'skip' '-NoPackage'
} else {
    if (-not (Test-Path -LiteralPath $OutDir)) { New-Item -ItemType Directory -Path $OutDir -Force | Out-Null }
    $vsix = Join-Path $OutDir ("{0}-{1}.vsix" -f $PkgName, $PkgVersion)
    if (Test-Path -LiteralPath $vsix) { Remove-Item -LiteralPath $vsix -Force }
    $vsceArgs = @('package', '--out', $vsix, '--allow-missing-repository')
    if (-not $IncludeDependencies) { $vsceArgs += '--no-dependencies' }
    if (-not (Get-ChildItem -LiteralPath $ExtensionDir -Filter 'LICENSE*' -File -ErrorAction SilentlyContinue)) { $vsceArgs += '--skip-license' }
    $localVsce = Join-Path $ExtensionDir 'node_modules\.bin\vsce.cmd'
    try {
        if (Test-Path -LiteralPath $localVsce) {
            Invoke-Checked $localVsce $vsceArgs -WorkingDirectory $ExtensionDir
        } else {
            Write-Host "  @vscode/vsce is not a devDependency; using npx @vscode/vsce"
            Invoke-Checked $NpxCmd (@('--yes', '@vscode/vsce') + $vsceArgs) -WorkingDirectory $ExtensionDir
        }
        if (-not (Test-Path -LiteralPath $vsix)) { throw "vsce finished but $vsix was not written" }
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $zip = [System.IO.Compression.ZipFile]::OpenRead($vsix)
        try { $entries = @($zip.Entries | ForEach-Object { $_.FullName }) } finally { $zip.Dispose() }
        $helperEntry = "extension/bin/$HelperExe"
        $hasHelper = ($entries -contains $helperEntry)
        if ((Test-Path -LiteralPath $HelperInBin) -and -not $hasHelper) {
            throw "$vsix does not contain $helperEntry although $HelperInBin exists - check .vscodeignore (bin/ must not be excluded)"
        }
        $mainEntry = "extension/$mainRel"
        if ($entries -notcontains $mainEntry) { throw "$vsix does not contain $mainEntry" }
        $helperNote = if ($hasHelper) { "with $HelperExe" } else { "WITHOUT the helper" }
        Write-Step 'vsce' 'ok' ("{0} ({1:N0} KB, {2} entries, {3})" -f $vsix, ((Get-Item -LiteralPath $vsix).Length / 1KB), $entries.Count, $helperNote)
    } catch { Fail 'vsce' $_.Exception.Message }
}

# ----------------------------------------------------------------------------------------------- summary

$elapsed = (Get-Date) - $script:Started
Write-Host ""
Write-Host ("Summary ({0:N0} s):" -f $elapsed.TotalSeconds)
$script:Steps | ForEach-Object { Write-Host ("  {0,-5} {1}" -f $_.State, $_.Step) }
if (-not $NoPackage) {
    Write-Host "  install: code --install-extension `"$vsix`""
}
exit 0
