<#
  drive_live.ps1 - "a project with pcbnew + eeschema open" inside VS Code: launches the Extension Development
  Host with kiwrapShell.liveWs pointing at a running winstream helper (prototypes/display/winstream, attached to a
  kicad.exe session that has the PCB and schematic editors open), opens the .kicad_pcb and .kicad_sch of the
  scratch project as two custom-editor tabs side by side (+ the 3D viewer as a panel), screenshots the window,
  runs latency probes through each tab, measures fps while another client pans, and sends REAL host input
  (SendInput) into each tab to see where it lands. Everything is appended to probe.jsonl; the dev host is closed
  by PID at the end.

    powershell -ExecutionPolicy Bypass -File tools\drive_live.ps1 -Project <scratch>\ecc83 -Scratch <dir> [-KeepOpen]
#>
param(
  [string]$Project = 'C:/Users/usama/AppData/Local/Temp/claude/c--Upwork-KiCad/48a8df66-7213-479f-ab2e-a63b89135016/scratchpad/display2/multi/ecc83',
  [string]$Scratch = 'C:/Users/usama/AppData/Local/Temp/claude/c--Upwork-KiCad/48a8df66-7213-479f-ab2e-a63b89135016/scratchpad/display2/multi/vscode',
  [string]$LiveWs = 'ws://127.0.0.1:8765/ws',
  [string]$Helper = 'http://127.0.0.1:8765',
  [string]$Node = 'C:/Program Files/nodejs/node.exe',
  [string]$PwDir = 'C:/Users/usama/AppData/Local/Temp/claude/c--Upwork-KiCad/48a8df66-7213-479f-ab2e-a63b89135016/scratchpad/display/winstream/pw',
  [int]$WinX = 40, [int]$WinY = 40, [int]$WinW = 2600, [int]$WinH = 1500,
  [ValidateSet('all', 'hostinput')] [string]$Phase = 'all',
  [switch]$KeepOpen
)
$ErrorActionPreference = 'Stop'
$proto = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$ws = Join-Path $Scratch 'workspace'
$udd = Join-Path $Scratch ('udd-' + (Get-Date -Format 'HHmmss'))
$ext = Join-Path $Scratch 'ext'
$shots = Join-Path $Scratch 'shots'
Get-ChildItem -Path $Scratch -Directory -Filter 'udd*' -ErrorAction SilentlyContinue | ForEach-Object { Remove-Item -Recurse -Force $_.FullName -ErrorAction SilentlyContinue }
New-Item -ItemType Directory -Force $ws, (Join-Path $ws '.vscode'), (Join-Path $udd 'User'), $ext, $shots | Out-Null
$log = Join-Path $Scratch 'probe.jsonl'
$cmd = Join-Path $Scratch 'cmd.jsonl'
Remove-Item -Force -ErrorAction SilentlyContinue $log, $cmd
@{ 'kiwrapShell.logFile' = $log.Replace('\', '/'); 'kiwrapShell.autoOpen' = ''; 'kiwrapShell.rescueKeys' = $true
   'kiwrapShell.retainContextWhenHidden' = $true; 'kiwrapShell.liveWs' = $LiveWs } | ConvertTo-Json | Out-File -Encoding utf8 (Join-Path $ws '.vscode/settings.json')
@{ 'security.workspace.trust.enabled' = $false; 'update.mode' = 'none'; 'telemetry.telemetryLevel' = 'off'; 'workbench.startupEditor' = 'none'
   'extensions.autoUpdate' = $false; 'window.restoreWindows' = 'none'; 'extensions.ignoreRecommendations' = $true; 'workbench.tips.enabled' = $false
   'window.newWindowDimensions' = 'default'; 'workbench.colorTheme' = 'Default Dark Modern'; 'chat.commandCenter.enabled' = $false
   'window.customMenuBarAltFocus' = $false; 'window.enableMenuBarMnemonics' = $false } | ConvertTo-Json | Out-File -Encoding utf8 (Join-Path $udd 'User/settings.json')
# the same scratch project files KiCad has open (the custom editor only needs the URI; it never reads the file)
Copy-Item (Join-Path $Project 'ecc83-pp.kicad_pcb') (Join-Path $ws 'ecc83-pp.kicad_pcb') -Force
Copy-Item (Join-Path $Project 'ecc83-pp.kicad_sch') (Join-Path $ws 'ecc83-pp.kicad_sch') -Force

Add-Type -ReferencedAssemblies System.Drawing @'
using System; using System.Runtime.InteropServices; using System.Collections.Generic; using System.Text; using System.Drawing; using System.Drawing.Imaging;
public static class W {
  [StructLayout(LayoutKind.Sequential)] public struct KEYBDINPUT { public ushort wVk; public ushort wScan; public uint dwFlags; public uint time; public IntPtr dwExtraInfo; }
  [StructLayout(LayoutKind.Sequential)] public struct MOUSEINPUT { public int dx; public int dy; public uint mouseData; public uint dwFlags; public uint time; public IntPtr dwExtraInfo; }
  [StructLayout(LayoutKind.Explicit)] public struct INPUTUNION { [FieldOffset(0)] public MOUSEINPUT mi; [FieldOffset(0)] public KEYBDINPUT ki; }
  [StructLayout(LayoutKind.Sequential)] public struct INPUT { public uint type; public INPUTUNION u; }
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int L, T, R, B; }
  [DllImport("user32.dll", SetLastError = true)] public static extern uint SendInput(uint n, INPUT[] inputs, int size);
  [DllImport("user32.dll")] public static extern uint MapVirtualKey(uint code, uint mapType);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int cmd);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern int GetWindowTextLength(IntPtr h);
  [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
  [DllImport("user32.dll")] public static extern int GetSystemMetrics(int i);
  [DllImport("user32.dll")] public static extern bool SetProcessDpiAwarenessContext(IntPtr ctx);
  [DllImport("user32.dll")] public static extern bool MoveWindow(IntPtr h, int x, int y, int w, int hh, bool repaint);
  [DllImport("user32.dll")] public static extern bool GetCursorPos(out System.Drawing.Point p);
  public delegate bool EnumProc(IntPtr h, IntPtr l);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr l);
  public static List<IntPtr> WindowsOf(uint pid) { var r = new List<IntPtr>(); EnumWindows((h, l) => { uint p; GetWindowThreadProcessId(h, out p); if (p == pid && IsWindowVisible(h) && GetWindowTextLength(h) > 0) r.Add(h); return true; }, IntPtr.Zero); return r; }
  public static string Title(IntPtr h) { var sb = new StringBuilder(512); GetWindowText(h, sb, 512); return sb.ToString(); }
  public static string FgTitle() { return Title(GetForegroundWindow()); }
  const uint KEYEVENTF_EXTENDEDKEY = 1, KEYEVENTF_KEYUP = 2;
  static INPUT Key(ushort vk, bool up, bool ext) { var i = new INPUT(); i.type = 1; i.u.ki.wVk = vk; i.u.ki.wScan = (ushort)MapVirtualKey(vk, 0); i.u.ki.dwFlags = (up ? KEYEVENTF_KEYUP : 0) | (ext ? KEYEVENTF_EXTENDEDKEY : 0); return i; }
  public static void KeyDown(ushort vk, bool ext) { SendInput(1, new[] { Key(vk, false, ext) }, Marshal.SizeOf(typeof(INPUT))); }
  public static void KeyUp(ushort vk, bool ext) { SendInput(1, new[] { Key(vk, true, ext) }, Marshal.SizeOf(typeof(INPUT))); }
  const uint MOUSEEVENTF_MOVE = 1, LD = 2, LU = 4, RD = 8, RU = 0x10, MD = 0x20, MU = 0x40, WHEEL = 0x800, ABS = 0x8000, VDESK = 0x4000;
  static INPUT M(uint flags, int dx, int dy, uint data) { var i = new INPUT(); i.type = 0; i.u.mi.dwFlags = flags; i.u.mi.dx = dx; i.u.mi.dy = dy; i.u.mi.mouseData = data; return i; }
  public static void MoveTo(int x, int y) { int vx = GetSystemMetrics(76), vy = GetSystemMetrics(77), vw = GetSystemMetrics(78), vh = GetSystemMetrics(79); SendInput(1, new[] { M(MOUSEEVENTF_MOVE | ABS | VDESK, (x - vx) * 65535 / vw, (y - vy) * 65535 / vh, 0) }, Marshal.SizeOf(typeof(INPUT))); }
  public static void Button(int b, bool up) { uint f = b == 0 ? (up ? LU : LD) : b == 1 ? (up ? MU : MD) : (up ? RU : RD); SendInput(1, new[] { M(f, 0, 0, 0) }, Marshal.SizeOf(typeof(INPUT))); }
  public static void Wheel(int delta) { SendInput(1, new[] { M(WHEEL, 0, 0, unchecked((uint)delta)) }, Marshal.SizeOf(typeof(INPUT))); }
  public static string Cursor() { System.Drawing.Point p; GetCursorPos(out p); return p.X + "," + p.Y; }
  public static void Shot(IntPtr h, string path) { RECT r; GetWindowRect(h, out r); int w = r.R - r.L, hh = r.B - r.T; using (var bmp = new Bitmap(w, hh)) { using (var g = Graphics.FromImage(bmp)) { g.CopyFromScreen(r.L, r.T, 0, 0, new Size(w, hh)); } bmp.Save(path, ImageFormat.Png); } }
}
'@
[void][W]::SetProcessDpiAwarenessContext([IntPtr](-4))   # per-monitor v2: physical pixels everywhere

function Mark([string]$kind, [hashtable]$data) {
  $rec = [ordered]@{ t = [int64]([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()); kind = $kind }
  foreach ($k in $data.Keys) { $rec[$k] = $data[$k] }
  Add-Content -Path $log -Value ($rec | ConvertTo-Json -Compress -Depth 6) -Encoding utf8
}
$script:cmdId = 0
function Ctl([hashtable]$m) { $script:cmdId++; $m['id'] = $script:cmdId; Add-Content -Path $cmd -Value ($m | ConvertTo-Json -Compress -Depth 6) -Encoding ascii; Start-Sleep -Milliseconds 350 }
function WaitLog([string]$pattern, [int]$sec = 30) { $sw = [Diagnostics.Stopwatch]::StartNew(); while ($sw.Elapsed.TotalSeconds -lt $sec) { if ((Test-Path $log) -and (Select-String -Path $log -Pattern $pattern -Quiet)) { return $true }; Start-Sleep -Milliseconds 200 }; return $false }
function LogLines([string]$pattern, [int]$skip = 0) { @(Get-Content $log | Select-Object -Skip $skip) -match $pattern }
function Helper([string]$path) { (Invoke-WebRequest -UseBasicParsing "$Helper$path").Content | ConvertFrom-Json }
function Shot([string]$name) { Start-Sleep -Milliseconds 500; [W]::Shot($hwnd, (Join-Path $shots "$name.png")); Mark 'shot' @{ name = $name } }
function Chord([string]$k, [bool]$ctrl = $false) { if ($ctrl) { [W]::KeyDown(0x11, $false); Start-Sleep -Milliseconds 20 }; $ext = $k -in 'home', 'delete'; $vk = @{ home = 0x24; delete = 0x2E; esc = 0x1B }[$k]; if (-not $vk) { $vk = [int][char]$k.ToUpper() }; [W]::KeyDown($vk, $ext); Start-Sleep -Milliseconds 40; [W]::KeyUp($vk, $ext); if ($ctrl) { Start-Sleep -Milliseconds 20; [W]::KeyUp(0x11, $false) }; Start-Sleep -Milliseconds 150 }

# ------------------------------------------------------------------ launch the dev host
$codeCmd = (Get-Command code).Source
$codeExe = Join-Path (Split-Path (Split-Path $codeCmd)) 'Code.exe'
$args = @("--extensionDevelopmentPath=$proto", "--user-data-dir=$udd", "--extensions-dir=$ext", '--new-window', '--skip-release-notes', '--disable-workspace-trust', '--skip-welcome', $ws)
$helperStats = Helper '/stats'
Mark 'driver' @{ msg = 'launch'; exe = $codeExe; helperPid = $helperStats.pid; inject = $helperStats.inject; surfaces = ($helperStats.surfaces | ForEach-Object { $_.kind + '@' + ($_.rect -join ',') }) -join ' ' }
$proc = Start-Process -FilePath $codeExe -ArgumentList $args -PassThru
$pid0 = $proc.Id
if (-not (WaitLog '"kind":"activated"' 60)) { throw 'extension did not activate' }
Start-Sleep -Seconds 2
$hwnd = [IntPtr]::Zero
foreach ($h in [W]::WindowsOf([uint32]$pid0)) { if ([W]::Title($h) -like '*Extension Development Host*') { $hwnd = $h } }
if ($hwnd -eq [IntPtr]::Zero) { throw 'dev host window not found' }
[void][W]::ShowWindow($hwnd, 9); [void][W]::SetForegroundWindow($hwnd); Start-Sleep -Milliseconds 500
[void][W]::MoveWindow($hwnd, $WinX, $WinY, $WinW, $WinH, $true); Start-Sleep -Milliseconds 800
$r = New-Object W+RECT; [void][W]::GetWindowRect($hwnd, [ref]$r)
Mark 'driver' @{ msg = 'window'; title = [W]::Title($hwnd); rect = "$($r.L),$($r.T),$($r.R),$($r.B)" }

# ------------------------------------------------------------------ open the two editors side by side (+ 3D panel)
$t0 = Get-Date
Ctl @{ cmd = 'openFile'; path = (Join-Path $ws 'ecc83-pp.kicad_pcb').Replace('\', '/'); column = 1 }
Ctl @{ cmd = 'openFile'; path = (Join-Path $ws 'ecc83-pp.kicad_sch').Replace('\', '/'); column = 2 }
$okPcb = WaitLog '"ev":"live-stats","surface":"pcb".*"connected":true' 30
$okSch = WaitLog '"ev":"live-stats","surface":"sch".*"connected":true' 30
Ctl @{ cmd = 'executeCommand'; command = 'workbench.action.closeAuxiliaryBar' }
Ctl @{ cmd = 'executeCommand'; command = 'workbench.action.closeSidebar' }
Ctl @{ cmd = 'executeCommand'; command = 'workbench.action.closePanel' }
# zoom-to-fit both editors through their tabs (Home), so the screenshots show the whole board / sheet
foreach ($s in 'pcb', 'sch') { Ctl @{ cmd = 'postSurface'; surface = $s; msg = @{ type = 'liveInput'; ev = @{ t = 'key'; code = 'Home'; down = $true } } }; Ctl @{ cmd = 'postSurface'; surface = $s; msg = @{ type = 'liveInput'; ev = @{ t = 'key'; code = 'Home'; down = $false } } } }
Mark 'driver' @{ msg = 'two tabs'; pcb = $okPcb; sch = $okSch; seconds = [math]::Round(((Get-Date) - $t0).TotalSeconds, 1) }
Start-Sleep -Seconds 3
Shot '01-vscode-pcb-sch-tabs'
Ctl @{ cmd = 'openPanel'; mode = 'live'; surface = '3d'; column = 3 }
[void](WaitLog '"ev":"live-stats","surface":"3d".*"connected":true' 20)
Start-Sleep -Seconds 2
Shot '02-vscode-pcb-sch-3d'
Ctl @{ cmd = 'state' }
Ctl @{ cmd = 'livePanels' }
# stats snapshot of each tab (frame sizes, scale, dpr)
Start-Sleep -Seconds 2
foreach ($s in 'pcb', 'sch', '3d') { $l = @(LogLines ('"ev":"live-stats","surface":"' + $s + '"')); if ($l.Count) { Mark 'tab' @{ surface = $s; last = $l[-1] } } }

if ($Phase -eq 'all') {
# ------------------------------------------------------------------ latency probes through each tab (wheel -> painted in the tab)
[void][W]::SetForegroundWindow($hwnd); Start-Sleep -Milliseconds 400
Mark 'driver' @{ msg = 'probes start'; fg = [W]::FgTitle() }
foreach ($s in @('pcb', 'sch')) {
  $before = @(Get-Content $log).Count
  for ($i = 0; $i -lt 8; $i++) {
    $dy = if ($i % 2 -eq 0) { 120 } else { -120 }
    Ctl @{ cmd = 'postSurface'; surface = $s; msg = @{ type = 'liveProbe'; ev = @{ t = 'wheel'; x = 1000; y = 600; dy = $dy; dx = 0 } } }
    Start-Sleep -Milliseconds 300
  }
  Start-Sleep -Milliseconds 800
  $res = @(@(LogLines '"ev":"live-probe"' $before) | ForEach-Object { ($_ | ConvertFrom-Json).e2e_ms } | Where-Object { $_ -ne $null })
  $med = if ($res.Count) { ($res | Sort-Object)[[int]($res.Count / 2)] } else { -1 }
  Mark 'probe' @{ surface = $s; e2e_ms = ($res -join ','); median = $med; n = $res.Count; fg = [W]::FgTitle() }
}

# ------------------------------------------------------------------ fps in the tabs while another client pans (headless Playwright drives the helper)
function PanFrom([string]$surface, [int]$secs) {
  $js = @"
import { chromium } from 'playwright';
const b = await chromium.launch({ headless: true }); const c = await b.newContext({ viewport: { width: 1920, height: 1226 }, deviceScaleFactor: 1 }); const p = await c.newPage();
await p.goto('$Helper/?surface=$surface&fit=0'); await p.waitForFunction(() => window.__state.connected && window.__state.frames > 0, null, { timeout: 15000 }); await p.waitForTimeout(300);
const x = 1000, y = 626; await p.mouse.click(x, y); await p.waitForTimeout(200); await p.mouse.move(x, y); await p.mouse.down({ button: 'middle' });
const tEnd = Date.now() + $secs * 1000; let i = 0; while (Date.now() < tEnd) { i++; await p.mouse.move(x + Math.sin(i / 8) * 150, y + Math.cos(i / 8) * 100, { steps: 2 }); await p.waitForTimeout(25); }
await p.mouse.up({ button: 'middle' }); await p.keyboard.press('Home'); await b.close();
"@
  $f = Join-Path $PwDir "pan_$surface.mjs"; Set-Content -Path $f -Value $js -Encoding ascii
  Push-Location $PwDir; & $Node $f; Pop-Location
}
foreach ($s in @('pcb', 'sch')) {
  $before = @(Get-Content $log).Count
  $cpuJob = Start-Job -ScriptBlock { param($py, $script, $secs) & $py $script $secs 'pan' } -ArgumentList 'C:/Users/usama/AppData/Local/Temp/claude/c--Upwork-KiCad/48a8df66-7213-479f-ab2e-a63b89135016/scratchpad/display/winstream/.venv/Scripts/python.exe', 'C:/Users/usama/AppData/Local/Temp/claude/c--Upwork-KiCad/48a8df66-7213-479f-ab2e-a63b89135016/scratchpad/display2/multi/cpu_sample.py', 6
  PanFrom $s 6
  $cpu = Receive-Job -Job $cpuJob -Wait; Remove-Job $cpuJob
  $fps = @{}
  foreach ($t in 'pcb', 'sch', '3d') { $l = @(LogLines ('"ev":"live-stats","surface":"' + $t + '"') $before) | ForEach-Object { ($_ | ConvertFrom-Json).fps }; $fps[$t] = ($l | Select-Object -Skip 1 -First 5) -join ',' }
  Mark 'pan' @{ driven = $s; fps_pcb_tab = $fps['pcb']; fps_sch_tab = $fps['sch']; fps_3d_tab = $fps['3d']; cpu = $cpu }
}
Shot '03-vscode-after-pan'
}

# ------------------------------------------------------------------ REAL host input into the tabs (SendInput at the dev host window)
# left editor group = pcb tab, right = sch tab: click a canvas point in each, press Home, then a key with a visible
# effect. The desktop is shared with other automation that steals the foreground and moves the cursor, so every
# attempt verifies the foreground before and the cursor right after the click, and retries up to 6 times.
$w = $r.R - $r.L; $hh = $r.B - $r.T
$pcbPt = @([int]($r.L + $w * 0.20), [int]($r.T + $hh * 0.22)); $schPt = @([int]($r.L + $w * 0.52), [int]($r.T + $hh * 0.25))
foreach ($tab in @(@('pcb', $pcbPt), @('sch', $schPt))) {
  $name = $tab[0]; $pt = $tab[1]
  for ($try = 1; $try -le 6; $try++) {
    [void][W]::ShowWindow($hwnd, 9); [void][W]::SetForegroundWindow($hwnd); Start-Sleep -Milliseconds 400
    if ([W]::GetForegroundWindow() -ne $hwnd) { Mark 'hostinput' @{ tab = $name; try = $try; skipped = 'not foreground'; fg = [W]::FgTitle() }; Start-Sleep -Seconds 2; continue }
    $s0 = Helper '/stats'; $n0 = $s0.input_events; $sess0 = @{}; foreach ($k in $s0.sessions.PSObject.Properties) { $sess0[$k.Value.surface] = $k.Value.frames }
    $before = @(Get-Content $log).Count
    [W]::MoveTo($pt[0], $pt[1]); Start-Sleep -Milliseconds 80
    $curBefore = [W]::Cursor()
    [W]::Button(0, $false); Start-Sleep -Milliseconds 40; [W]::Button(0, $true); Start-Sleep -Milliseconds 350
    $fgAfterClick = [W]::FgTitle(); $curAfterClick = [W]::Cursor()
    $pd = @(LogLines ('"ev":"pointerdown".*"src2":"live-' + $name + '"') $before).Count
    if ($pd -eq 0) { Mark 'hostinput' @{ tab = $name; try = $try; skipped = 'click did not reach the tab'; fg = $fgAfterClick; cursorBefore = $curBefore; cursorAfter = $curAfterClick }; Start-Sleep -Seconds 2; continue }
    # keys typed into VS Code now: Home (zoom fit) then wheel over the tab; who gets them?
    Chord 'home'; Start-Sleep -Milliseconds 500
    $fgAfterKey = [W]::FgTitle()
    [W]::MoveTo($pt[0], $pt[1]); Start-Sleep -Milliseconds 60; [W]::Wheel(120); Start-Sleep -Milliseconds 600
    $s1 = Helper '/stats'
    $delta = @{}; foreach ($k in $s1.sessions.PSObject.Properties) { $delta[$k.Value.surface] = $k.Value.frames - $sess0[$k.Value.surface] }
    $keys = @(LogLines ('"ev":"keydown".*"src2":"live-' + $name + '"') $before).Count
    Mark 'hostinput' @{ tab = $name; try = $try; clickAt = ($pt -join ','); cursorBefore = $curBefore; cursorAfterClick = $curAfterClick; fgAfterClick = $fgAfterClick; fgAfterKey = $fgAfterKey; fgNow = [W]::FgTitle(); cursorNow = [W]::Cursor(); pointerdownInTab = $pd; keydownInTab = $keys; inputEvents = ($s1.input_events - $n0); inputLog = ($s1.input_log | Select-Object -Last 8 | ForEach-Object { $_ -join ' ' }) -join ' | '; kicadFrames = ($delta.GetEnumerator() | ForEach-Object { "$($_.Key)=$($_.Value)" }) -join ' '; foreground_fixes = $s1.foreground_fixes; obscured_fixes = $s1.obscured_fixes; inject = $s1.inject }
    Shot "04-hostinput-$name"
    break
  }
}
# keyboard focus check: type R in the pcb tab -> KiCad rotate or not (Home first so R has a selection? no: just count events)
Ctl @{ cmd = 'state' }
Shot '05-vscode-final'
Mark 'driver' @{ msg = 'done' }
if (-not $KeepOpen) {
  Start-Sleep -Milliseconds 500
  if (-not $proc.HasExited) { Stop-Process -Id $pid0 -Force -ErrorAction SilentlyContinue }
  Start-Sleep -Milliseconds 800
  $left = Get-CimInstance Win32_Process -Filter "Name='Code.exe'" | Where-Object { $_.CommandLine -like "*$($udd.Replace('\','\\'))*" }
  foreach ($p in $left) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
  Mark 'driver' @{ msg = 'closed'; leftover = @($left).Count }
}
Write-Host "log: $log"
