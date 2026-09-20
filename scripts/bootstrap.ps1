<#
.SYNOPSIS
    kiwrap developer/user bootstrap for Windows (Windows PowerShell 5.1 and PowerShell 7).

.DESCRIPTION
    Idempotent. Ensures, in this order:
      1. uv (uses the one on PATH; otherwise installs a private copy under <storage>/uv)
      2. `uv sync --all-extras` in the repo root
      3. a private Eclipse Temurin JRE 25 under <storage>/engines/jre/     (pinned in scripts/engines.json)
      4. freerouting-2.4.1.jar under <storage>/engines/freerouting/          (pinned in scripts/engines.json)
      5. a Freerouting profile under <storage>/freerouting/freerouting.json with analytics, telemetry,
         contact and the GitHub update check all OFF
      6. reports Node.js and Rust (report only, nothing installed): Node >= 20 LTS (even majors) builds the
         VS Code extension, Rust stable-msvc builds its Live KiCad helper (scripts/build_extension.ps1;
         docs/EXTENSION.md). Both are looked up on PATH and at their default install locations.
      7. reports the KiCad installs `kiwrap.installation` (or a Program Files glob) finds

    <storage> is %KIWRAP_HOME% if set, else %LOCALAPPDATA%\kiwrap (the same rule as kiwrap.platform).
    Every download is verified against the sha256 pinned in engines.json before it is used.
    Nothing is written to the user's %APPDATA%\kicad: every kicad-cli call runs with KICAD_CONFIG_HOME pointed
    at a throw-away directory, and the script hashes %APPDATA%\kicad before and after to prove it.

.PARAMETER Check
    Report only: verifies what is present (hashes included) and installs/downloads nothing.
    Exit code 0 when everything is present, 2 when something is missing.

.PARAMETER StorageHome
    Override the storage home for this run (same effect as setting KIWRAP_HOME).

.PARAMETER SkipSync
    Do not run `uv sync` (useful in CI where the environment is already synced).

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\bootstrap.ps1
    powershell -ExecutionPolicy Bypass -File scripts\bootstrap.ps1 -Check
#>
[CmdletBinding()]
param(
    [switch]$Check,
    [string]$StorageHome = "",
    [switch]$SkipSync
)

Set-StrictMode -Version 2
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'   # Invoke-WebRequest is ~10x slower with the progress bar on PS 5.1
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
} catch { }

$script:RepoRoot = Split-Path -Parent $PSScriptRoot
$script:PinsPath = Join-Path $PSScriptRoot 'engines.json'
$script:Failures = New-Object System.Collections.ArrayList
$script:Missing = New-Object System.Collections.ArrayList
$script:Rows = New-Object System.Collections.ArrayList

# ----------------------------------------------------------------------------------------------- helpers

function Write-Status {
    param([string]$Item, [string]$State, [string]$Detail = "")
    # State: ok | missing | fixed | warn | fail | info
    [void]$script:Rows.Add([pscustomobject]@{ Item = $Item; State = $State; Detail = $Detail })
    $tag = $State.ToUpperInvariant().PadRight(7)
    Write-Host ("[{0}] {1}: {2}" -f $tag, $Item, $Detail)
    if ($State -eq 'fail') { [void]$script:Failures.Add($Item) }
    if ($State -eq 'missing') { [void]$script:Missing.Add($Item) }
}

function Get-Sha256 {
    param([string]$Path)
    return (Get-FileHash -Path $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function ConvertTo-CommandLineArgument {
    # Windows CommandLineToArgvW quoting (ProcessStartInfo.ArgumentList does not exist on PS 5.1).
    param([string]$Arg)
    if ($Arg -eq "") { return '""' }
    if ($Arg -notmatch '[\s"]') { return $Arg }
    $sb = New-Object System.Text.StringBuilder
    [void]$sb.Append('"')
    $backslashes = 0
    foreach ($ch in $Arg.ToCharArray()) {
        if ($ch -eq '\') { $backslashes++; continue }
        if ($ch -eq '"') { [void]$sb.Append('\' * ($backslashes * 2 + 1)); [void]$sb.Append('"'); $backslashes = 0; continue }
        if ($backslashes -gt 0) { [void]$sb.Append('\' * $backslashes); $backslashes = 0 }
        [void]$sb.Append($ch)
    }
    if ($backslashes -gt 0) { [void]$sb.Append('\' * ($backslashes * 2)) }
    [void]$sb.Append('"')
    return $sb.ToString()
}

function Invoke-Native {
    <#
      Runs a native executable with captured stdout/stderr, an environment overlay and a timeout, without the
      PS 5.1 NativeCommandError trap that `2>&1` triggers under $ErrorActionPreference = 'Stop'.
    #>
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$Arguments = @(),
        [hashtable]$Environment = @{},
        [string]$WorkingDirectory = "",
        [int]$TimeoutSec = 300
    )
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $FilePath
    $psi.Arguments = (($Arguments | ForEach-Object { ConvertTo-CommandLineArgument $_ }) -join ' ')
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.CreateNoWindow = $true
    if ($WorkingDirectory -ne "") { $psi.WorkingDirectory = $WorkingDirectory }
    foreach ($key in $Environment.Keys) { $psi.EnvironmentVariables[$key] = [string]$Environment[$key] }
    $proc = New-Object System.Diagnostics.Process
    $proc.StartInfo = $psi
    [void]$proc.Start()
    $outTask = $proc.StandardOutput.ReadToEndAsync()
    $errTask = $proc.StandardError.ReadToEndAsync()
    if (-not $proc.WaitForExit($TimeoutSec * 1000)) {
        try { $proc.Kill() } catch { }
        throw "timed out after ${TimeoutSec}s: $FilePath $($psi.Arguments)"
    }
    $proc.WaitForExit()
    return [pscustomobject]@{ ExitCode = $proc.ExitCode; StdOut = $outTask.Result; StdErr = $errTask.Result }
}

function Get-DirectoryFingerprint {
    <#
      SHA-256 over (relative path, size, mtime ticks, content hash) of every file under $Path.
      Used to prove the user's %APPDATA%\kicad is untouched (CLAUDE.md hard rule; ARCHITECTURE.md section 6).
    #>
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return "absent" }
    $sb = New-Object System.Text.StringBuilder
    $files = Get-ChildItem -LiteralPath $Path -Recurse -File -Force | Sort-Object FullName
    foreach ($f in $files) {
        $rel = $f.FullName.Substring($Path.Length)
        [void]$sb.AppendLine("$rel|$($f.Length)|$($f.LastWriteTimeUtc.Ticks)|$(Get-Sha256 $f.FullName)")
    }
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($sb.ToString())
    $sha = [System.Security.Cryptography.SHA256]::Create()
    return (($sha.ComputeHash($bytes) | ForEach-Object { $_.ToString('x2') }) -join '')
}

function Get-VerifiedDownload {
    <# Returns the path of a file whose sha256 matches; downloads (resumable-by-rerun) only when needed. #>
    param([string]$Url, [string]$Destination, [string]$Sha256, [long]$Size)
    if (Test-Path -LiteralPath $Destination) {
        if ((Get-Sha256 $Destination) -eq $Sha256) { return $Destination }
        Write-Host "  cached file has a different hash; re-downloading $Destination"
        Remove-Item -LiteralPath $Destination -Force
    }
    $dir = Split-Path -Parent $Destination
    if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    $part = "$Destination.part"
    if (Test-Path -LiteralPath $part) { Remove-Item -LiteralPath $part -Force }
    Write-Host ("  downloading {0} ({1:N1} MB)" -f $Url, ($Size / 1MB))
    Invoke-WebRequest -Uri $Url -OutFile $part -UseBasicParsing
    $actual = Get-Sha256 $part
    if ($actual -ne $Sha256) {
        Remove-Item -LiteralPath $part -Force
        throw "sha256 mismatch for $Url`n  expected $Sha256`n  actual   $actual"
    }
    Move-Item -LiteralPath $part -Destination $Destination -Force
    return $Destination
}

function Set-JsonValue {
    <# Sets a dotted path on a ConvertFrom-Json object tree, creating intermediate objects (PS 5.1 compatible). #>
    param($Object, [string]$Path, $Value)
    $parts = $Path.Split('.')
    $node = $Object
    for ($i = 0; $i -lt $parts.Length - 1; $i++) {
        $prop = $node.PSObject.Properties[$parts[$i]]
        if ($null -eq $prop -or $null -eq $prop.Value -or -not ($prop.Value -is [System.Management.Automation.PSCustomObject])) {
            $child = New-Object PSObject
            if ($null -eq $prop) { $node | Add-Member -MemberType NoteProperty -Name $parts[$i] -Value $child }
            else { $prop.Value = $child }
            $node = $child
        } else {
            $node = $prop.Value
        }
    }
    $leaf = $node.PSObject.Properties[$parts[-1]]
    if ($null -eq $leaf) { $node | Add-Member -MemberType NoteProperty -Name $parts[-1] -Value $Value }
    else { $leaf.Value = $Value }
}

function Get-JsonValue {
    param($Object, [string]$Path)
    $node = $Object
    foreach ($part in $Path.Split('.')) {
        if ($null -eq $node) { return $null }
        $prop = $node.PSObject.Properties[$part]
        if ($null -eq $prop) { return $null }
        $node = $prop.Value
    }
    return $node
}

function Write-Utf8NoBom {
    param([string]$Path, [string]$Content)
    [System.IO.File]::WriteAllText($Path, $Content, (New-Object System.Text.UTF8Encoding($false)))
}

# ----------------------------------------------------------------------------------------------- paths

if ($StorageHome -ne "") { $Storage = $StorageHome }
elseif ($env:KIWRAP_HOME) { $Storage = $env:KIWRAP_HOME }
else { $Storage = Join-Path $env:LOCALAPPDATA 'kiwrap' }
$Storage = [System.IO.Path]::GetFullPath($Storage)

$EnginesDir = Join-Path $Storage 'engines'
$DownloadsDir = Join-Path $EnginesDir 'downloads'
$JreDir = Join-Path $EnginesDir 'jre'
$FreeroutingDir = Join-Path $EnginesDir 'freerouting'
$ProfileDir = Join-Path $Storage 'freerouting'
$UvDir = Join-Path $Storage 'uv'

$Pins = Get-Content -LiteralPath $script:PinsPath -Raw | ConvertFrom-Json
$JrePin = $Pins.jre.windows
$FrPin = $Pins.freerouting

$mode = 'install'
if ($Check) { $mode = 'check' }
Write-Host "kiwrap bootstrap ($mode) - storage home: $Storage"
Write-Host "  pins: JRE $($Pins.jre.release_name) / Freerouting $($FrPin.version) (scripts/engines.json, pinned $($Pins.pinned_on))"

if (-not $Check) {
    foreach ($d in @($Storage, $EnginesDir, $DownloadsDir, $JreDir, $FreeroutingDir, $ProfileDir)) {
        if (-not (Test-Path -LiteralPath $d)) { New-Item -ItemType Directory -Path $d -Force | Out-Null }
    }
}

# ----------------------------------------------------------------------------------------------- 1. uv

$Uv = $null
$uvCmd = Get-Command uv -ErrorAction SilentlyContinue
if ($null -ne $uvCmd) { $Uv = $uvCmd.Source }
elseif (Test-Path -LiteralPath (Join-Path $UvDir 'uv.exe')) { $Uv = Join-Path $UvDir 'uv.exe' }

if ($null -eq $Uv -and -not $Check) {
    Write-Host "  installing uv into $UvDir (official installer, PATH left untouched)"
    $env:UV_INSTALL_DIR = $UvDir
    $env:UV_NO_MODIFY_PATH = '1'
    $installer = Invoke-RestMethod -Uri 'https://astral.sh/uv/install.ps1' -UseBasicParsing
    Invoke-Expression $installer
    if (Test-Path -LiteralPath (Join-Path $UvDir 'uv.exe')) { $Uv = Join-Path $UvDir 'uv.exe' }
}
if ($null -eq $Uv) {
    Write-Status 'uv' 'missing' "not on PATH and not under $UvDir"
} else {
    $v = Invoke-Native -FilePath $Uv -Arguments @('--version') -TimeoutSec 30
    Write-Status 'uv' 'ok' "$($v.StdOut.Trim()) ($Uv)"
}

# ----------------------------------------------------------------------------------------------- 2. uv sync

$venvPython = Join-Path $script:RepoRoot '.venv\Scripts\python.exe'
if ($null -ne $Uv -and -not $Check -and -not $SkipSync) {
    Write-Host "  uv sync --all-extras (repo: $script:RepoRoot)"
    $r = Invoke-Native -FilePath $Uv -Arguments @('sync', '--all-extras') -WorkingDirectory $script:RepoRoot -TimeoutSec 900
    if ($r.ExitCode -ne 0) { Write-Status 'venv' 'fail' ("uv sync exit {0}: {1}" -f $r.ExitCode, $r.StdErr.Trim()) }
    else { Write-Status 'venv' 'ok' "uv sync --all-extras done ($venvPython)" }
} elseif (Test-Path -LiteralPath $venvPython) {
    Write-Status 'venv' 'ok' $venvPython
} else {
    Write-Status 'venv' 'missing' "$venvPython (run without -Check to create it)"
}

# ----------------------------------------------------------------------------------------------- 3. JRE 25

$JavaExe = Join-Path (Join-Path $JreDir $JrePin.top_dir) ($JrePin.java -replace '/', '\')
$JreMarker = Join-Path $JreDir 'installed.json'
$jreOk = $false
if ((Test-Path -LiteralPath $JavaExe) -and (Test-Path -LiteralPath $JreMarker)) {
    $marker = Get-Content -LiteralPath $JreMarker -Raw | ConvertFrom-Json
    if ($marker.sha256 -eq $JrePin.sha256) { $jreOk = $true }
}
if (-not $jreOk -and -not $Check) {
    try {
        $archive = Get-VerifiedDownload -Url $JrePin.url -Destination (Join-Path $DownloadsDir $JrePin.asset) -Sha256 $JrePin.sha256 -Size $JrePin.size
        $target = Join-Path $JreDir $JrePin.top_dir
        if (Test-Path -LiteralPath $target) { Remove-Item -LiteralPath $target -Recurse -Force }
        Write-Host "  extracting $($JrePin.asset) -> $JreDir"
        Expand-Archive -LiteralPath $archive -DestinationPath $JreDir -Force
        if (-not (Test-Path -LiteralPath $JavaExe)) { throw "archive did not contain $($JrePin.top_dir)/$($JrePin.java)" }
        $markerJson = [pscustomobject]@{ release_name = $Pins.jre.release_name; asset = $JrePin.asset; sha256 = $JrePin.sha256; installed = (Get-Date).ToString('o') } | ConvertTo-Json
        Write-Utf8NoBom -Path $JreMarker -Content $markerJson
        $jreOk = $true
    } catch {
        Write-Status 'jre' 'fail' $_.Exception.Message
    }
}
if ($jreOk) {
    $jv = Invoke-Native -FilePath $JavaExe -Arguments @('-version') -TimeoutSec 60
    $firstLine = ($jv.StdErr + $jv.StdOut).Trim() -split "`r?`n" | Select-Object -First 1
    Write-Status 'jre' 'ok' "$firstLine ($JavaExe; archive sha256 $($JrePin.sha256))"
} elseif ($Check) {
    Write-Status 'jre' 'missing' "$JavaExe (or marker hash != pinned)"
}

# ----------------------------------------------------------------------------------------------- 4. Freerouting jar

$Jar = Join-Path $FreeroutingDir $FrPin.asset
$jarOk = $false
if (Test-Path -LiteralPath $Jar) {
    $h = Get-Sha256 $Jar
    if ($h -eq $FrPin.sha256) { $jarOk = $true }
    elseif (-not $Check) { Write-Host "  $Jar has sha256 $h, expected $($FrPin.sha256); replacing"; Remove-Item -LiteralPath $Jar -Force }
    else { Write-Status 'freerouting' 'fail' "sha256 mismatch: $h != $($FrPin.sha256)" }
}
if (-not $jarOk -and -not $Check) {
    try {
        [void](Get-VerifiedDownload -Url $FrPin.url -Destination $Jar -Sha256 $FrPin.sha256 -Size $FrPin.size)
        $jarOk = $true
    } catch {
        Write-Status 'freerouting' 'fail' $_.Exception.Message
    }
}
if ($jarOk) { Write-Status 'freerouting' 'ok' "$Jar (sha256 $($FrPin.sha256) verified)" }
elseif ($Check -and -not (Test-Path -LiteralPath $Jar)) { Write-Status 'freerouting' 'missing' $Jar }

# ----------------------------------------------------------------------------------------------- 5. Freerouting profile

# Keys and values: profile written by Freerouting 2.4.1 itself (spikes/autorouting/fr_userdata/freerouting.json),
# docs/research/04-autorouting.md, ARCHITECTURE.md section 8.  The GitHub version check
# (app.freerouting.util.VersionChecker, started unconditionally from main in 2.4.1 - there is no settings key for
# it) is blackholed through `network.proxy_url` -> https.proxyHost, which the JVM's default HttpClient honours;
# localhost/127.0.0.1 are on the non-proxy list, so the local REST/MCP servers are unaffected.
$EnforcedProfile = [ordered]@{
    'version'                                        = $FrPin.version
    'profile.email'                                  = ''
    'profile.allow_telemetry'                        = $false
    'profile.allow_contact'                          = $false
    'usage_and_diagnostic_data.disable_analytics'    = $true
    'usage_and_diagnostic_data.track_window_changed' = $false
    'usage_and_diagnostic_data.track_button_clicked' = $false
    'gui.enabled'                                    = $false
    'gui.show_routing_summary'                       = $false
    'feature_flags.save_jobs'                        = $false
    'api_server.enabled'                             = $false
    'api_server.http_allowed'                        = $true
    'api_server.endpoints'                           = @('http://127.0.0.1:37864')
    'mcp_server.enabled'                             = $false
    'network.proxy_url'                              = 'http://127.0.0.1:9'
    'network.no_proxy'                               = 'localhost,127.0.0.1'
    'logging.console.enabled'                        = $true
    'logging.file.enabled'                           = $true
    'logging.file.level'                             = 'INFO'
    'logging.file.location'                          = (Join-Path $ProfileDir 'freerouting.log')
}
$ProfilePath = Join-Path $ProfileDir 'freerouting.json'

function Test-FreeroutingProfile {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return @('file missing') }
    $obj = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
    $bad = @()
    foreach ($key in $EnforcedProfile.Keys) {
        $want = $EnforcedProfile[$key]
        $have = Get-JsonValue -Object $obj -Path $key
        if ($want -is [array]) { $same = ($null -ne $have) -and ((@($have) -join ',') -eq ($want -join ',')) }
        else { $same = ($null -ne $have) -and ([string]$have -eq [string]$want) }
        if (-not $same) { $bad += "$key=$have (want $want)" }
    }
    return $bad
}

if (-not $Check) {
    if (Test-Path -LiteralPath $ProfilePath) { $profileObj = Get-Content -LiteralPath $ProfilePath -Raw | ConvertFrom-Json }
    else { $profileObj = New-Object PSObject }
    if ($null -eq (Get-JsonValue -Object $profileObj -Path 'profile.id')) {
        Set-JsonValue -Object $profileObj -Path 'profile.id' -Value ([guid]::NewGuid().ToString())
    }
    foreach ($key in $EnforcedProfile.Keys) { Set-JsonValue -Object $profileObj -Path $key -Value $EnforcedProfile[$key] }
    Write-Utf8NoBom -Path $ProfilePath -Content ($profileObj | ConvertTo-Json -Depth 20)
}
$problems = @(Test-FreeroutingProfile -Path $ProfilePath)
if ($problems.Count -eq 0) { Write-Status 'freerouting-profile' 'ok' "$ProfilePath (analytics/telemetry/contact/update-check off)" }
elseif ($Check -and $problems[0] -eq 'file missing') { Write-Status 'freerouting-profile' 'missing' $ProfilePath }
else { Write-Status 'freerouting-profile' 'fail' ($problems -join '; ') }

# Smoke test: the jar must start under the private JRE with this profile; Freerouting rewrites the profile on
# exit, so re-validating afterwards proves the keys survive a real run (not just our write).
if ($jreOk -and $jarOk -and -not $Check) {
    try {
        $fr = Invoke-Native -FilePath $JavaExe -Arguments @('-jar', $Jar, "--user_data_path=$ProfileDir", '--gui.enabled=false', '-help') -WorkingDirectory $ProfileDir -TimeoutSec 120
        $banner = (($fr.StdOut + $fr.StdErr) -split "`r?`n" | Where-Object { $_ -match 'Freerouting v' } | Select-Object -First 1)
        if ($null -eq $banner) { $banner = "exit $($fr.ExitCode), no banner" }
        $after = @(Test-FreeroutingProfile -Path $ProfilePath)
        if ($after.Count -eq 0) { Write-Status 'freerouting-run' 'ok' "$($banner.Trim()); profile keys intact after the run" }
        else { Write-Status 'freerouting-run' 'fail' "profile changed by Freerouting: $($after -join '; ')" }
    } catch {
        Write-Status 'freerouting-run' 'fail' $_.Exception.Message
    }
}

# ----------------------------------------------------------------------------------------------- 6. Node + Rust (report only)

# The VS Code extension (extension/, docs/EXTENSION.md) is built by scripts/build_extension.ps1: Node >= 20 for
# npm ci + esbuild + vsce, Rust stable-msvc for the Live KiCad helper (kiwrap-live.exe). Neither is installed
# here: an installed VSIX needs neither (the helper ships prebuilt inside it); only building does.
$nodeExe = $null
$nodeCmd = Get-Command node -ErrorAction SilentlyContinue
if ($null -ne $nodeCmd) { $nodeExe = $nodeCmd.Source }
elseif (Test-Path -LiteralPath (Join-Path $env:ProgramFiles 'nodejs\node.exe')) { $nodeExe = Join-Path $env:ProgramFiles 'nodejs\node.exe' }
if ($null -eq $nodeExe) {
    Write-Status 'node' 'warn' 'not on PATH and not under Program Files\nodejs - install Node.js LTS (https://nodejs.org) to build the VS Code extension (scripts\build_extension.ps1); report only'
} else {
    $nv = (Invoke-Native -FilePath $nodeExe -Arguments @('--version') -TimeoutSec 30).StdOut.Trim()
    $major = 0
    if ($nv -match '^v(\d+)') { $major = [int]$Matches[1] }
    $where = if ($null -ne $nodeCmd) { $nodeExe } else { "$nodeExe, not on PATH in this shell" }
    if ($major -ge 20 -and ($major % 2) -eq 0) { Write-Status 'node' 'ok' "$nv ($where) - even major = LTS line" }
    elseif ($major -ge 20) { Write-Status 'node' 'warn' "$nv ($where) is not an LTS line (even majors >= 20 are); report only" }
    else { Write-Status 'node' 'warn' "$nv ($where) is too old for the extension build (>= 20); report only" }
}

$cargoExe = $null
$cargoCmd = Get-Command cargo -ErrorAction SilentlyContinue
$cargoHome = if ($env:CARGO_HOME) { $env:CARGO_HOME } else { Join-Path $env:USERPROFILE '.cargo' }
if ($null -ne $cargoCmd) { $cargoExe = $cargoCmd.Source }
elseif (Test-Path -LiteralPath (Join-Path $cargoHome 'bin\cargo.exe')) { $cargoExe = Join-Path $cargoHome 'bin\cargo.exe' }
if ($null -eq $cargoExe) {
    Write-Status 'rust' 'warn' "cargo not on PATH and not under $cargoHome\bin - install Rust stable (https://rustup.rs, x86_64-pc-windows-msvc) to build the Live KiCad helper; report only"
} else {
    $rustcExe = Join-Path (Split-Path -Parent $cargoExe) 'rustc.exe'
    if (-not (Test-Path -LiteralPath $rustcExe)) { $rustcExe = 'rustc' }
    try {
        $rv = Invoke-Native -FilePath $rustcExe -Arguments @('-vV') -TimeoutSec 30
        $rustLine = ($rv.StdOut -split "`r?`n" | Where-Object { $_ -match '^rustc ' } | Select-Object -First 1)
        $hostLine = ($rv.StdOut -split "`r?`n" | Where-Object { $_ -match '^host: ' } | Select-Object -First 1)
        if ($null -eq $rustLine) { $rustLine = 'rustc (version unknown)' }
        if ($null -ne $hostLine -and $hostLine -notmatch 'x86_64-pc-windows-msvc') {
            Write-Status 'rust' 'warn' "$rustLine, $hostLine ($cargoExe) - the helper needs x86_64-pc-windows-msvc (rustup default stable-msvc); report only"
        } else {
            Write-Status 'rust' 'ok' "$rustLine ($cargoExe)"
        }
    } catch {
        Write-Status 'rust' 'warn' "cargo found at $cargoExe but rustc -vV failed: $($_.Exception.Message); report only"
    }
}

# ----------------------------------------------------------------------------------------------- 7. KiCad installs (report only)

$userKicad = Join-Path $env:APPDATA 'kicad'
$fpBefore = Get-DirectoryFingerprint -Path $userKicad
$throwaway = Join-Path ([System.IO.Path]::GetTempPath()) ("kiwrap-bootstrap-" + $PID)
New-Item -ItemType Directory -Path $throwaway -Force | Out-Null
# Hard rule: every kicad-cli invocation (even `version`) runs with KICAD_CONFIG_HOME pointed away from %APPDATA%,
# because kicad-cli rewrites kicad_common.json on exit (ARCHITECTURE.md section 6).
$kicadEnv = @{
    KICAD_CONFIG_HOME    = (Join-Path $throwaway 'config')
    KICAD_DOCUMENTS_HOME = (Join-Path $throwaway 'documents')
    KICAD_CACHE_HOME     = (Join-Path $throwaway 'cache')
    KIWRAP_HOME          = $Storage
}
$reported = $false
if ($null -ne $Uv -and (Test-Path -LiteralPath $venvPython)) {
    # With capability probing first; `--no-probe` when kiwrap.capability is not implemented yet.
    foreach ($extra in @(@(), @('--no-probe'))) {
        if ($reported) { break }
        try {
            $args = @('run', '--no-sync', 'python', '-m', 'kiwrap.installation') + $extra
            $r = Invoke-Native -FilePath $Uv -Arguments $args -WorkingDirectory $script:RepoRoot -Environment $kicadEnv -TimeoutSec 300
            if ($r.ExitCode -eq 0 -and $r.StdOut.Trim() -ne "") {
                Write-Status 'kicad' 'ok' ('via `uv run python -m kiwrap.installation ' + ($extra -join ' ') + '`:')
                $r.StdOut.TrimEnd() -split "`r?`n" | ForEach-Object { Write-Host "         $_" }
                $reported = $true
            }
        } catch { }
    }
}
if (-not $reported) {
    $clis = @(Get-ChildItem -Path (Join-Path $env:ProgramFiles 'KiCad') -Directory -ErrorAction SilentlyContinue |
        ForEach-Object { Join-Path $_.FullName 'bin\kicad-cli.exe' } | Where-Object { Test-Path -LiteralPath $_ })
    $pathCli = Get-Command kicad-cli -ErrorAction SilentlyContinue
    if ($null -ne $pathCli -and ($clis -notcontains $pathCli.Source)) { $clis += $pathCli.Source }
    if ($clis.Count -eq 0) {
        Write-Status 'kicad' 'warn' 'no kicad-cli.exe under Program Files\KiCad\*\bin or on PATH (kiwrap.installation not usable yet); report only'
    } else {
        Write-Status 'kicad' 'ok' "$($clis.Count) install(s) found by glob (kiwrap.installation not usable yet):"
        foreach ($cli in $clis) {
            try {
                $r = Invoke-Native -FilePath $cli -Arguments @('version', '--format', 'plain') -Environment $kicadEnv -TimeoutSec 120
                Write-Host ("         {0}  ->  {1}" -f $cli, $r.StdOut.Trim())
            } catch {
                Write-Host ("         {0}  ->  {1}" -f $cli, $_.Exception.Message)
            }
        }
    }
}
try { Remove-Item -LiteralPath $throwaway -Recurse -Force -ErrorAction SilentlyContinue } catch { }
$fpAfter = Get-DirectoryFingerprint -Path $userKicad
if ($fpBefore -eq $fpAfter) { Write-Status 'user-kicad-config' 'ok' "$userKicad untouched (fingerprint $($fpBefore.Substring(0, [Math]::Min(12, $fpBefore.Length))))" }
else { Write-Status 'user-kicad-config' 'fail' "$userKicad CHANGED during bootstrap (before $fpBefore, after $fpAfter)" }

# ----------------------------------------------------------------------------------------------- summary

Write-Host ""
Write-Host "Summary ($mode):"
$script:Rows | ForEach-Object { Write-Host ("  {0,-8} {1}" -f $_.State, $_.Item) }
if (-not $Check) {
    $reportPath = Join-Path $Storage 'bootstrap-report.json'
    $report = [pscustomobject]@{
        generated = (Get-Date).ToString('o'); mode = $mode; storage_home = $Storage
        java = $JavaExe; freerouting_jar = $Jar; freerouting_profile = $ProfilePath; uv = $Uv
        rows = @($script:Rows)
    }
    Write-Utf8NoBom -Path $reportPath -Content ($report | ConvertTo-Json -Depth 6)
    Write-Host "  report: $reportPath"
}
if ($script:Failures.Count -gt 0) { Write-Host "FAILED: $($script:Failures -join ', ')"; exit 1 }
if ($script:Missing.Count -gt 0) { Write-Host "MISSING: $($script:Missing -join ', ')"; exit 2 }
exit 0
