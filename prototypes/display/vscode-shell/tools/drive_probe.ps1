<#
  drive_probe.ps1 - launches a VS Code Extension Development Host with this prototype loaded into an isolated
  profile (--user-data-dir / --extensions-dir under the scratch dir), waits for the probe panel, then pushes real
  Win32 input (SendInput) into the window: KiCad's important shortcuts, mouse buttons/drag/wheel, and runs the
  DPI / clipboard / pointer-lock / fullscreen / zen-mode / split-focus / retainContext / custom-editor checks
  through the extension's control channel (cmd.jsonl). Every observation is appended to probe.jsonl; window
  screenshots go next to it. Finally the dev host is closed by PID.

  Usage (from a normal PowerShell, the owner present - windows will pop up):
    powershell -ExecutionPolicy Bypass -File tools\drive_probe.ps1 -Run A -Rescue $true  -Retain $true
    powershell -ExecutionPolicy Bypass -File tools\drive_probe.ps1 -Run B -Rescue $false -Retain $false
    ... -Mode iframe   (runs the key matrix against the <iframe> view instead of the probe page)
#>
param(
  [string]$Run = 'A',
  [bool]$Rescue = $true,
  [bool]$Retain = $true,
  [ValidateSet('probe', 'iframe', 'canvas')] [string]$Mode = 'probe',
  [string]$Scratch = 'C:/Users/usama/AppData/Local/Temp/claude/c--Upwork-KiCad/48a8df66-7213-479f-ab2e-a63b89135016/scratchpad/display/vscode-shell',
  [switch]$KeepOpen,
  [string]$Only = ''
)
$ErrorActionPreference = 'Stop'
$proto = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$runDir = Join-Path $Scratch "run$Run"
$ws = Join-Path $runDir 'workspace'
$udd = Join-Path $runDir ('udd-' + (Get-Date -Format 'HHmmss'))
$ext = Join-Path $runDir 'ext'
$shots = Join-Path $runDir 'shots'
Get-ChildItem -Path $runDir -Directory -Filter 'udd*' -ErrorAction SilentlyContinue | ForEach-Object { Remove-Item -Recurse -Force $_.FullName -ErrorAction SilentlyContinue }  # fresh profile every run: no restored editors
New-Item -ItemType Directory -Force $ws, (Join-Path $ws '.vscode'), (Join-Path $udd 'User'), $ext, $shots | Out-Null
$log = Join-Path $runDir 'probe.jsonl'
$cmd = Join-Path $runDir 'cmd.jsonl'
Remove-Item -Force -ErrorAction SilentlyContinue $log, $cmd
$autoOpen = @{ probe = 'probe'; iframe = 'view'; canvas = 'canvas' }[$Mode]
@{
  'kiwrapShell.logFile' = $log.Replace('\', '/'); 'kiwrapShell.autoOpen' = $autoOpen; 'kiwrapShell.rescueKeys' = $Rescue
  'kiwrapShell.retainContextWhenHidden' = $Retain
} | ConvertTo-Json | Out-File -Encoding utf8 (Join-Path $ws '.vscode/settings.json')
@{
  'security.workspace.trust.enabled' = $false; 'update.mode' = 'none'; 'telemetry.telemetryLevel' = 'off'; 'workbench.startupEditor' = 'none'
  'extensions.autoUpdate' = $false; 'window.restoreWindows' = 'none'; 'extensions.ignoreRecommendations' = $true; 'workbench.tips.enabled' = $false
  'window.newWindowDimensions' = 'default'; 'workbench.colorTheme' = 'Default Dark Modern'; 'chat.commandCenter.enabled' = $false
} | ConvertTo-Json | Out-File -Encoding utf8 (Join-Path $udd 'User/settings.json')
# scratch copies of demo files for the custom editor + a text file for the split-focus test
$demo = 'C:/Program Files/KiCad/10.0/share/kicad/demos/ecc83'
Copy-Item (Join-Path $demo 'ecc83-pp.kicad_pcb') (Join-Path $ws 'ecc83-pp.kicad_pcb') -Force
Copy-Item (Join-Path $demo 'ecc83-pp.kicad_sch') (Join-Path $ws 'ecc83-pp.kicad_sch') -Force
'hello from a text editor' | Out-File -Encoding ascii (Join-Path $ws 'notes.txt')

# ------------------------------------------------------------------ Win32 helpers
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
  [DllImport("user32.dll")] public static extern short GetAsyncKeyState(int vk);
  [DllImport("user32.dll")] public static extern short GetKeyState(int vk);
  public static string Mods() { string r = ""; if ((GetAsyncKeyState(0x10) & 0x8000) != 0) r += "shift "; if ((GetAsyncKeyState(0x11) & 0x8000) != 0) r += "ctrl "; if ((GetAsyncKeyState(0x12) & 0x8000) != 0) r += "alt "; if ((GetAsyncKeyState(0x5B) & 0x8000) != 0) r += "win "; return r.Trim(); }
  public static void ReleaseMods() { foreach (ushort vk in new ushort[] { 0x10, 0xA0, 0xA1, 0x11, 0xA2, 0xA3, 0x12, 0xA4, 0xA5, 0x5B, 0x5C }) KeyUp(vk, false); }
  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
  [DllImport("user32.dll")] public static extern bool MoveWindow(IntPtr h, int x, int y, int w, int hh, bool repaint);
  [DllImport("user32.dll")] public static extern bool PostMessage(IntPtr h, uint msg, IntPtr wp, IntPtr lp);
  public delegate bool EnumProc(IntPtr h, IntPtr l);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr l);
  public static List<IntPtr> WindowsOf(uint pid) { var r = new List<IntPtr>(); EnumWindows((h, l) => { uint p; GetWindowThreadProcessId(h, out p); if (p == pid && IsWindowVisible(h) && GetWindowTextLength(h) > 0) r.Add(h); return true; }, IntPtr.Zero); return r; }
  public static string Title(IntPtr h) { var sb = new StringBuilder(512); GetWindowText(h, sb, 512); return sb.ToString(); }
  const uint KEYEVENTF_EXTENDEDKEY = 1, KEYEVENTF_KEYUP = 2, KEYEVENTF_SCANCODE = 8;
  static INPUT Key(ushort vk, bool up, bool ext) { var i = new INPUT(); i.type = 1; i.u.ki.wVk = vk; i.u.ki.wScan = (ushort)MapVirtualKey(vk, 0); i.u.ki.dwFlags = (up ? KEYEVENTF_KEYUP : 0) | (ext ? KEYEVENTF_EXTENDEDKEY : 0); return i; }
  public static void KeyDown(ushort vk, bool ext) { SendInput(1, new[] { Key(vk, false, ext) }, Marshal.SizeOf(typeof(INPUT))); }
  public static void KeyUp(ushort vk, bool ext) { SendInput(1, new[] { Key(vk, true, ext) }, Marshal.SizeOf(typeof(INPUT))); }
  const uint MOUSEEVENTF_MOVE = 1, LD = 2, LU = 4, RD = 8, RU = 0x10, MD = 0x20, MU = 0x40, WHEEL = 0x800, ABS = 0x8000;
  static INPUT M(uint flags, int dx, int dy, uint data) { var i = new INPUT(); i.type = 0; i.u.mi.dwFlags = flags; i.u.mi.dx = dx; i.u.mi.dy = dy; i.u.mi.mouseData = data; return i; }
  public static void MoveTo(int x, int y) { int sw = GetSystemMetrics(0), sh = GetSystemMetrics(1); SendInput(1, new[] { M(MOUSEEVENTF_MOVE | ABS, x * 65535 / sw, y * 65535 / sh, 0) }, Marshal.SizeOf(typeof(INPUT))); }
  public static void Button(int b, bool up) { uint f = b == 0 ? (up ? LU : LD) : b == 1 ? (up ? MU : MD) : (up ? RU : RD); SendInput(1, new[] { M(f, 0, 0, 0) }, Marshal.SizeOf(typeof(INPUT))); }
  public static void Wheel(int delta) { SendInput(1, new[] { M(WHEEL, 0, 0, unchecked((uint)delta)) }, Marshal.SizeOf(typeof(INPUT))); }
  public static void Shot(IntPtr h, string path) { RECT r; GetWindowRect(h, out r); int w = r.R - r.L, hh = r.B - r.T; using (var bmp = new Bitmap(w, hh)) { using (var g = Graphics.FromImage(bmp)) { g.CopyFromScreen(r.L, r.T, 0, 0, new Size(w, hh)); } bmp.Save(path, ImageFormat.Png); } }
}
'@
[void][W]::SetProcessDPIAware()

$VKTAB = @{ ctrl = 0x11; shift = 0x10; alt = 0x12; win = 0x5B; esc = 0x1B; escape = 0x1B; tab = 0x09; delete = 0x2E; backspace = 0x08; space = 0x20; enter = 0x0D
  left = 0x25; up = 0x26; right = 0x27; down = 0x28; home = 0x24; end = 0x23; pageup = 0x21; pagedown = 0x22; insert = 0x2D
  '`' = 0xC0; '-' = 0xBD; '=' = 0xBB; '[' = 0xDB; ']' = 0xDD; '\' = 0xDC; ';' = 0xBA; "'" = 0xDE; ',' = 0xBC; '.' = 0xBE; '/' = 0xBF
  numpad_add = 0x6B; numpad_subtract = 0x6D; numpad0 = 0x60; numpad1 = 0x61; numpad_multiply = 0x6A; numpad_divide = 0x6F; numpad_decimal = 0x6E
  capslock = 0x14; numlock = 0x90; scrolllock = 0x91; pause = 0x13; printscreen = 0x2C; apps = 0x5D }
1..24 | ForEach-Object { $VKTAB["f$_"] = 0x6F + $_ }
$extended = @('left', 'up', 'right', 'down', 'home', 'end', 'pageup', 'pagedown', 'insert', 'delete', 'numpad_divide', 'apps', 'printscreen')
function VkOf([string]$name) {
  $n = $name.ToLower()
  if ($VKTAB.ContainsKey($n)) { return [int]$VKTAB[$n] }
  if ($n.Length -eq 1) { $c = [int][char]$n.ToUpper(); return $c }
  throw "unknown key $name"
}
function Mark([string]$kind, [hashtable]$data) {
  $rec = [ordered]@{ t = [int64]([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()); kind = $kind }
  foreach ($k in $data.Keys) { $rec[$k] = $data[$k] }
  Add-Content -Path $log -Value ($rec | ConvertTo-Json -Compress) -Encoding ascii
}
$script:cmdId = 0
function Ctl([hashtable]$m) {
  $script:cmdId++; $m['id'] = $script:cmdId
  Add-Content -Path $cmd -Value ($m | ConvertTo-Json -Compress -Depth 5) -Encoding ascii
  Start-Sleep -Milliseconds 350
}
function ClickCenter() { [W]::MoveTo($cx, $cy); Start-Sleep -Milliseconds 40; [W]::Button(0, $false); Start-Sleep -Milliseconds 30; [W]::Button(0, $true); Start-Sleep -Milliseconds 200 }
function EnsureFront([int]$tries = 40) {
  for ($t = 0; $t -lt $tries; $t++) {
    if ([W]::GetForegroundWindow() -eq $hwnd) { return $true }
    [void][W]::SetForegroundWindow($hwnd); Start-Sleep -Milliseconds 150
    if ([W]::GetForegroundWindow() -eq $hwnd) { return $true }
    # foreground-lock workaround: a modifier tap lets a background process activate a window
    [W]::KeyDown(0x12, $false); [W]::KeyUp(0x12, $false); [void][W]::SetForegroundWindow($hwnd); Start-Sleep -Milliseconds 250
    if ([W]::GetForegroundWindow() -eq $hwnd) { Chord 'esc'; ClickCenter; return $true }
  }
  Mark 'driver' @{ msg = 'could not get foreground'; fg = [W]::Title([W]::GetForegroundWindow()) }
  return $false
}
function Chord([string]$chord) {
  if ([W]::GetForegroundWindow() -ne $hwnd) { if (-not (EnsureFront)) { Mark 'driver' @{ msg = 'chord skipped, not foreground'; chord = $chord }; return } }
  # "ctrl+shift+p", "ctrl+k ctrl+s" (two-stroke chord), "r"
  foreach ($stroke in $chord.Split(' ')) {
    $parts = $stroke.Split('+')
    $mods = @(); $key = $null
    foreach ($p in $parts) { if ($p -in @('ctrl', 'shift', 'alt', 'win')) { $mods += $p } else { $key = $p } }
    foreach ($m in $mods) { [W]::KeyDown((VkOf $m), $false); Start-Sleep -Milliseconds 15 }
    if ($key) { $code = VkOf $key; $ext = $extended -contains $key.ToLower(); [W]::KeyDown($code, $ext); Start-Sleep -Milliseconds 40; [W]::KeyUp($code, $ext) }
    [array]::Reverse($mods); foreach ($m in $mods) { Start-Sleep -Milliseconds 15; [W]::KeyUp((VkOf $m), $false) }
    Start-Sleep -Milliseconds 120
  }
}
function LogTail([int]$n = 5) { if (Test-Path $log) { Get-Content $log -Tail $n } }
function WaitLog([string]$pattern, [int]$sec = 30) {
  $sw = [Diagnostics.Stopwatch]::StartNew()
  while ($sw.Elapsed.TotalSeconds -lt $sec) { if ((Test-Path $log) -and (Select-String -Path $log -Pattern $pattern -Quiet)) { return $true }; Start-Sleep -Milliseconds 200 }
  return $false
}
function LogCount([string]$pattern) { if (Test-Path $log) { @(Select-String -Path $log -Pattern $pattern).Count } else { 0 } }

# ------------------------------------------------------------------ launch the dev host
$codeCmd = (Get-Command code).Source
$codeExe = Join-Path (Split-Path (Split-Path $codeCmd)) 'Code.exe'
if (-not (Test-Path $codeExe)) { throw "Code.exe not found next to $codeCmd" }
$args = @("--extensionDevelopmentPath=$proto", "--user-data-dir=$udd", "--extensions-dir=$ext", '--new-window', '--skip-release-notes', '--disable-workspace-trust', '--skip-welcome', $ws)
Mark 'driver' @{ msg = 'launch'; run = $Run; rescue = $Rescue; retain = $Retain; mode = $Mode; exe = $codeExe }
$proc = Start-Process -FilePath $codeExe -ArgumentList $args -PassThru
$pid0 = $proc.Id
Write-Host "dev host pid $pid0"
if (-not (WaitLog '"kind":"activated"' 60)) { throw 'extension did not activate' }
if (-not (WaitLog '"ev":"loaded"' 30)) { Write-Warning 'no loaded event from the webview yet' }
Start-Sleep -Seconds 2
$hwnd = [IntPtr]::Zero
foreach ($h in [W]::WindowsOf([uint32]$pid0)) { if ([W]::Title($h) -like '*Extension Development Host*') { $hwnd = $h } }
if ($hwnd -eq [IntPtr]::Zero) { throw 'dev host window not found' }
Write-Host ("window: " + [W]::Title($hwnd))
[void][W]::ShowWindow($hwnd, 9); [void][W]::SetForegroundWindow($hwnd); Start-Sleep -Milliseconds 500
[void][W]::MoveWindow($hwnd, 40, 40, 1600, 1000, $true); Start-Sleep -Milliseconds 800
$r = New-Object W+RECT; [void][W]::GetWindowRect($hwnd, [ref]$r)
$cx = [int](($r.L + $r.R) / 2); $cy = [int](($r.T + $r.B) / 2) + 60
Mark 'driver' @{ msg = 'window'; title = [W]::Title($hwnd); rect = "$($r.L),$($r.T),$($r.R),$($r.B)"; center = "$cx,$cy"; screen = "$([W]::GetSystemMetrics(0))x$([W]::GetSystemMetrics(1))" }
function Focus() { [void](EnsureFront); [W]::MoveTo($cx, $cy); Start-Sleep -Milliseconds 60; [W]::Button(0, $false); Start-Sleep -Milliseconds 40; [W]::Button(0, $true); Start-Sleep -Milliseconds 250 }
function Shot([string]$name) { Start-Sleep -Milliseconds 400; [W]::Shot($hwnd, (Join-Path $shots "$name.png")); Mark 'shot' @{ name = $name } }
function Recover() {
  [void](EnsureFront); Chord 'esc'; Start-Sleep -Milliseconds 150
  if ([W]::GetForegroundWindow() -ne $hwnd) {
    # a new window / dialog stole focus: close it and come back
    $fg = [W]::GetForegroundWindow(); Mark 'driver' @{ msg = 'foreign foreground'; title = [W]::Title($fg) }
    if ([W]::Title($fg) -like '*Extension Development Host*' -or [W]::Title($fg) -like '*Visual Studio Code*') { Chord 'alt+f4'; Start-Sleep -Milliseconds 800 }
    [void][W]::SetForegroundWindow($hwnd)
  }
  Ctl @{ cmd = 'executeCommand'; command = 'workbench.action.closePanel' }
  Ctl @{ cmd = 'executeCommand'; command = 'workbench.action.closeAuxiliaryBar' }
  if ((LogCount '"disposed":true') -gt $script:disposedSeen) { $script:disposedSeen = LogCount '"disposed":true'; Mark 'driver' @{ msg = 'panel was disposed - reopening' }; Ctl @{ cmd = 'openPanel'; mode = $autoOpen; column = 1 }; Start-Sleep -Milliseconds 1200 }
  Ctl @{ cmd = 'reveal' }
  [void](EnsureFront)
}
$script:disposedSeen = 0
$skipped = @()
[W]::ReleaseMods(); Start-Sleep -Milliseconds 100
if (([W]::GetKeyState(0x14) -band 1) -ne 0) { Chord 'capslock'; Mark 'driver' @{ msg = 'capslock was on, turned off' } }
Mark 'driver' @{ msg = 'mods at start'; mods = [W]::Mods() }
Focus
Shot "00-initial-$Mode"
Ctl @{ cmd = 'state' }
Ctl @{ cmd = 'post'; msg = @{ type = 'dpi' } }

# ------------------------------------------------------------------ key matrix
$keys = @(
  'r', 'm', 'x', 'v', 'e', 'd', 'f', 'b', 'g', 'a', 'w', 'l', 'p', 'u', 'i', 'n', 'q', 't', 'h', 'c', 'z', 's', 'o', 'y', 'k', 'j',
  'shift+r', 'shift+m', 'shift+f', 'shift+g', 'shift+n', 'shift+a', 'shift+space', 'shift+delete',
  'delete', 'backspace', 'esc', 'tab', 'shift+tab', 'space', 'enter', 'insert', 'home', 'end', 'pageup', 'pagedown',
  'left', 'right', 'up', 'down', 'shift+left', 'ctrl+left', 'ctrl+shift+right',
  '1', '2', '3', '4', '5', '6', '7', '8', '9', '0', '-', '=', '[', ']', '\', ';', ',', '.', '/', '`',
  'numpad_add', 'numpad_subtract', 'numpad0', 'numpad1', 'numpad_multiply', 'numpad_divide', 'numpad_decimal',
  'ctrl+z', 'ctrl+y', 'ctrl+shift+z', 'ctrl+x', 'ctrl+c', 'ctrl+v', 'ctrl+s', 'ctrl+a', 'ctrl+f', 'ctrl+shift+s', 'ctrl+shift+a',
  'ctrl+d', 'ctrl+e', 'ctrl+h', 'ctrl+i', 'ctrl+l', 'ctrl+m', 'ctrl+r', 'ctrl+t', 'ctrl+u', 'ctrl+g', 'ctrl+k', 'ctrl+b', 'ctrl+j',
  'ctrl+shift+f', 'ctrl+shift+m', 'ctrl+shift+v', 'ctrl+shift+c', 'ctrl+shift+x', 'ctrl+shift+e', 'ctrl+shift+g', 'ctrl+shift+d', 'ctrl+shift+h', 'ctrl+shift+i', 'ctrl+shift+u', 'ctrl+shift+b', 'ctrl+shift+r', 'ctrl+shift+o', 'ctrl+shift+t', 'ctrl+shift+l', 'ctrl+shift+k', 'ctrl+shift+y',
  'ctrl+1', 'ctrl+2', 'ctrl+3', 'ctrl+0', 'ctrl+=', 'ctrl+-', 'ctrl+shift+=', 'ctrl+numpad_add', 'ctrl+numpad_subtract', 'ctrl+`', 'ctrl+,', 'ctrl+[', 'ctrl+]', 'ctrl+\', 'ctrl+space', 'ctrl+enter', 'ctrl+delete', 'ctrl+backspace', 'ctrl+tab', 'ctrl+shift+tab', 'ctrl+pageup', 'ctrl+pagedown',
  'ctrl+p', 'ctrl+shift+p', 'ctrl+q', 'ctrl+o', 'ctrl+n', 'ctrl+w', 'ctrl+f4', 'ctrl+k ctrl+s', 'ctrl+k ctrl+o', 'ctrl+k z', 'ctrl+k m',
  'f1', 'f2', 'f3', 'f4', 'f5', 'f6', 'f7', 'f8', 'f9', 'f10', 'f11', 'f12', 'shift+f5', 'ctrl+f5', 'shift+f10', 'shift+f11', 'ctrl+f2', 'ctrl+f11', 'ctrl+f12',
  'alt+f', 'alt+e', 'alt+v', 'alt+p', 'alt+i', 'alt+t', 'alt+r', 'alt+s', 'alt+h', 'alt+3', 'alt+1', 'alt+2', 'alt+a', 'alt+z', 'alt+x', 'alt+d', 'alt+left', 'alt+right', 'alt+up', 'alt+down', 'alt+space', 'alt+enter', 'alt+shift+f', 'ctrl+alt+f', 'ctrl+alt+r', 'ctrl+alt+i', 'ctrl+alt+b', 'ctrl+alt+p',
  'alt', 'shift', 'ctrl', 'f10', 'capslock', 'numlock', 'scrolllock', 'apps', 'pause', 'win+shift+s'
)
# ctrl+shift+n (new window), ctrl+shift+w (close window), alt+f4, win+* are tested separately / skipped: they destroy the window.
if ($Only -eq 'mouse') { $keys = @() }
Mark 'driver' @{ msg = 'keys-begin'; count = $keys.Count }
$i = 0
foreach ($k in $keys) {
  $i++
  $fgok = EnsureFront
  Mark 'sent' @{ name = $k; i = $i; fg = $fgok; mods = [W]::Mods() }
  if (-not $fgok) { $skipped += $k; continue }
  if ($k -eq 'win+shift+s') { continue } # do not trigger the Snipping tool; kept in the list for the report
  $before = @(Get-Content $log).Count
  Chord $k
  Start-Sleep -Milliseconds 260
  # the desktop is shared with other automation that steals the foreground: re-send if nothing arrived
  for ($try = 0; $try -lt 3; $try++) {
    $after = @(Get-Content $log | Select-Object -Skip $before) -match '"ev":"key(down|up)"'
    if ($after.Count -gt 0) { break }
    Mark 'driver' @{ msg = 'retry'; name = $k; try = $try + 1; fg = ([W]::GetForegroundWindow() -eq $hwnd) }
    [void](EnsureFront); [W]::ReleaseMods(); Ctl @{ cmd = 'executeCommand'; command = 'workbench.action.closePanel' }; Ctl @{ cmd = 'reveal' }; Start-Sleep -Milliseconds 200; Chord $k; Start-Sleep -Milliseconds 300
  }
  if ($k -in @('ctrl+p', 'ctrl+shift+p', 'ctrl+q', 'ctrl+o', 'ctrl+n', 'ctrl+w', 'ctrl+f4', 'ctrl+e', 'ctrl+r', 'ctrl+g', 'ctrl+,', 'ctrl+shift+n', 'ctrl+k ctrl+s', 'ctrl+k ctrl+o', 'ctrl+k z', 'ctrl+k m', 'f1', 'f5', 'f11', 'shift+f5', 'ctrl+f5', 'ctrl+shift+p', 'ctrl+shift+f', 'ctrl+shift+e', 'ctrl+shift+g', 'ctrl+shift+x', 'ctrl+shift+d', 'ctrl+shift+m', 'ctrl+shift+h', 'ctrl+shift+u', 'ctrl+shift+b', 'ctrl+shift+o', 'ctrl+shift+t', 'ctrl+shift+i', 'ctrl+shift+r', 'ctrl+shift+a', 'ctrl+t', 'ctrl+b', 'ctrl+j', 'ctrl+`', 'ctrl+1', 'ctrl+2', 'ctrl+3', 'ctrl+tab', 'ctrl+shift+tab', 'ctrl+pageup', 'ctrl+pagedown', 'ctrl+=', 'ctrl+-', 'ctrl+numpad_add', 'ctrl+numpad_subtract', 'ctrl+0', 'ctrl+shift+=', 'ctrl+m', 'ctrl+d', 'ctrl+u', 'ctrl+l', 'ctrl+h', 'ctrl+i', 'ctrl+f', 'ctrl+shift+f', 'ctrl+k', 'shift+f10', 'f10', 'ctrl+f11', 'shift+f11', 'alt', 'ctrl+space', 'ctrl+enter') -or $k.StartsWith('alt+') -or $k.StartsWith('f')) {
    # anything that may open a palette / menu / dialog / view: press Esc and put focus back
    if ($k -eq 'f11' -or $k -eq 'ctrl+k z') { Start-Sleep -Milliseconds 600; Shot "key-$($i)-$($k -replace '[^a-z0-9]','_')"; Chord $k; Start-Sleep -Milliseconds 700 }
    elseif ($k -in @('ctrl+shift+p', 'ctrl+w', 'alt+f', 'ctrl+b', 'ctrl+f', 'ctrl+p', 'f1', 'alt', 'ctrl+shift+f', 'f5')) { Shot "key-$($i)-$($k -replace '[^a-z0-9]','_')" }
    Recover
  }
}
Mark 'driver' @{ msg = 'keys-end'; skipped = ($skipped -join ',') }
Focus

# ------------------------------------------------------------------ mouse
[void](EnsureFront); Mark 'sent' @{ name = 'mouse:leftclick' }; [W]::MoveTo($cx, $cy); [W]::Button(0, $false); Start-Sleep -Milliseconds 50; [W]::Button(0, $true); Start-Sleep -Milliseconds 250
[void](EnsureFront); Mark 'sent' @{ name = 'mouse:dblclick' }; [W]::Button(0, $false); [W]::Button(0, $true); Start-Sleep -Milliseconds 60; [W]::Button(0, $false); [W]::Button(0, $true); Start-Sleep -Milliseconds 250
[void](EnsureFront); Mark 'sent' @{ name = 'mouse:rightclick' }; [W]::Button(2, $false); Start-Sleep -Milliseconds 50; [W]::Button(2, $true); Start-Sleep -Milliseconds 350; Shot 'mouse-rightclick'; Chord 'esc'
[void](EnsureFront); Mark 'sent' @{ name = 'mouse:middleclick' }; [W]::Button(1, $false); Start-Sleep -Milliseconds 50; [W]::Button(1, $true); Start-Sleep -Milliseconds 350; Shot 'mouse-middleclick'
[void](EnsureFront); Mark 'sent' @{ name = 'mouse:middledrag' }; [W]::Button(1, $false); 1..10 | ForEach-Object { [W]::MoveTo($cx + $_ * 12, $cy + $_ * 6); Start-Sleep -Milliseconds 20 }; [W]::Button(1, $true); Start-Sleep -Milliseconds 250
[void](EnsureFront); Mark 'sent' @{ name = 'mouse:leftdrag' }; [W]::MoveTo($cx, $cy); [W]::Button(0, $false); 1..10 | ForEach-Object { [W]::MoveTo($cx - $_ * 12, $cy + $_ * 6); Start-Sleep -Milliseconds 20 }; [W]::Button(0, $true); Start-Sleep -Milliseconds 250
[void](EnsureFront); Mark 'sent' @{ name = 'mouse:rightdrag' }; [W]::MoveTo($cx, $cy); [W]::Button(2, $false); 1..6 | ForEach-Object { [W]::MoveTo($cx + $_ * 12, $cy - $_ * 6); Start-Sleep -Milliseconds 20 }; [W]::Button(2, $true); Start-Sleep -Milliseconds 250; Chord 'esc'
[void](EnsureFront); Mark 'sent' @{ name = 'mouse:wheelup' }; [W]::MoveTo($cx, $cy); [W]::Wheel(120); Start-Sleep -Milliseconds 250
[void](EnsureFront); Mark 'sent' @{ name = 'mouse:wheeldown' }; [W]::Wheel(-120); Start-Sleep -Milliseconds 250
[void](EnsureFront); Mark 'sent' @{ name = 'mouse:ctrl+wheel' }; [W]::KeyDown(0x11, $false); Start-Sleep -Milliseconds 30; [W]::Wheel(120); Start-Sleep -Milliseconds 60; [W]::Wheel(-120); Start-Sleep -Milliseconds 30; [W]::KeyUp(0x11, $false); Start-Sleep -Milliseconds 250
[void](EnsureFront); Mark 'sent' @{ name = 'mouse:shift+wheel' }; [W]::KeyDown(0x10, $false); Start-Sleep -Milliseconds 30; [W]::Wheel(120); Start-Sleep -Milliseconds 30; [W]::KeyUp(0x10, $false); Start-Sleep -Milliseconds 250
[void](EnsureFront); Mark 'sent' @{ name = 'mouse:alt+wheel' }; [W]::KeyDown(0x12, $false); Start-Sleep -Milliseconds 30; [W]::Wheel(120); Start-Sleep -Milliseconds 30; [W]::KeyUp(0x12, $false); Start-Sleep -Milliseconds 250; Chord 'esc'
[void](EnsureFront); Mark 'sent' @{ name = 'mouse:x1button' }; Start-Sleep -Milliseconds 100
[void](EnsureFront); Mark 'sent' @{ name = 'mouse:move-latency' }; 1..30 | ForEach-Object { [W]::MoveTo($cx + ($_ % 10) * 5, $cy); Start-Sleep -Milliseconds 16 }; Start-Sleep -Milliseconds 300
Ctl @{ cmd = 'post'; msg = @{ type = 'dpi' } }

if ($Mode -ne 'canvas' -and $Only -ne 'mouse') {
  # ---------------------------------------------------------------- clipboard
  Mark 'sent' @{ name = 'clipboard:webview-api' }; Ctl @{ cmd = 'post'; msg = @{ type = 'clipboard'; tag = "wv-$Run" } }; Start-Sleep -Milliseconds 600
  Mark 'sent' @{ name = 'clipboard:ext-write-then-webview-read' }; Ctl @{ cmd = 'clipboardWrite'; text = "from-extension-$Run" }; Ctl @{ cmd = 'post'; msg = @{ type = 'clipboard'; tag = "wv2-$Run" } }; Start-Sleep -Milliseconds 600
  Mark 'sent' @{ name = 'clipboard:ext-read' }; Ctl @{ cmd = 'clipboardRead' }
  Mark 'sent' @{ name = 'clipboard:api-inside-keydown-gesture' }; Ctl @{ cmd = 'post'; msg = @{ type = 'armClipboard'; tag = "gesture-$Run" } }; Focus; Chord 'c'; Start-Sleep -Milliseconds 700
  Mark 'sent' @{ name = 'clipboard:api-inside-click-gesture' }; Ctl @{ cmd = 'post'; msg = @{ type = 'armClipboardClick'; tag = "click-$Run" } }; Focus; Start-Sleep -Milliseconds 700
  Mark 'sent' @{ name = 'clipboard:ctrl+v-after-ext-write' }; Ctl @{ cmd = 'clipboardWrite'; text = "paste-me-$Run" }; Focus; Chord 'ctrl+v'; Start-Sleep -Milliseconds 400

  # ---------------------------------------------------------------- pointer lock / fullscreen inside the webview
  Mark 'sent' @{ name = 'pointerlock' }; Ctl @{ cmd = 'post'; msg = @{ type = 'armPointerLock' } }; Focus; 1..8 | ForEach-Object { [W]::MoveTo($cx + $_ * 7, $cy + $_ * 3); Start-Sleep -Milliseconds 25 }; Start-Sleep -Milliseconds 500; Shot 'pointerlock'; Ctl @{ cmd = 'post'; msg = @{ type = 'exitPointerLock' } }; Chord 'esc'
  Mark 'sent' @{ name = 'fullscreen-element' }; Ctl @{ cmd = 'post'; msg = @{ type = 'armFullscreen' } }; Focus; Start-Sleep -Milliseconds 900; Shot 'element-fullscreen'; Chord 'esc'; Start-Sleep -Milliseconds 500

  # ---------------------------------------------------------------- DPI / zoom / zen / fullscreen (workbench)
  Mark 'sent' @{ name = 'zoom:in' }; Ctl @{ cmd = 'executeCommand'; command = 'workbench.action.zoomIn' }; Start-Sleep -Milliseconds 600; Ctl @{ cmd = 'post'; msg = @{ type = 'dpi' } }; Shot 'zoom-in'
  Mark 'sent' @{ name = 'zoom:reset' }; Ctl @{ cmd = 'executeCommand'; command = 'workbench.action.zoomReset' }; Start-Sleep -Milliseconds 600; Ctl @{ cmd = 'post'; msg = @{ type = 'dpi' } }
  Mark 'sent' @{ name = 'zen:on' }; Ctl @{ cmd = 'executeCommand'; command = 'workbench.action.toggleZenMode' }; Start-Sleep -Milliseconds 1200; Ctl @{ cmd = 'post'; msg = @{ type = 'dpi' } }; Focus; Chord 'r'; Chord 'ctrl+z'; Shot 'zen-mode'
  Mark 'sent' @{ name = 'zen:off' }; Ctl @{ cmd = 'executeCommand'; command = 'workbench.action.toggleZenMode' }; Start-Sleep -Milliseconds 1200
  Mark 'sent' @{ name = 'fullscreen:on' }; Ctl @{ cmd = 'executeCommand'; command = 'workbench.action.toggleFullScreen' }; Start-Sleep -Milliseconds 1500; Ctl @{ cmd = 'post'; msg = @{ type = 'dpi' } }; Focus; Chord 'r'; Shot 'workbench-fullscreen'
  Mark 'sent' @{ name = 'fullscreen:off' }; Ctl @{ cmd = 'executeCommand'; command = 'workbench.action.toggleFullScreen' }; Start-Sleep -Milliseconds 1500
  [void][W]::MoveWindow($hwnd, 40, 40, 1600, 1000, $true); Start-Sleep -Milliseconds 600

  # ---------------------------------------------------------------- split + focus behaviour
  Mark 'sent' @{ name = 'split:open-text-beside' }; Ctl @{ cmd = 'openText'; path = (Join-Path $ws 'notes.txt').Replace('\', '/'); column = 2 }; Start-Sleep -Milliseconds 900; Shot 'split'
  Mark 'sent' @{ name = 'split:keys-while-text-editor-focused' }; Chord 'r'; Chord 'ctrl+z'; Start-Sleep -Milliseconds 300
  Mark 'sent' @{ name = 'split:focusFirstEditorGroup-then-keys' }; Ctl @{ cmd = 'executeCommand'; command = 'workbench.action.focusFirstEditorGroup' }; Start-Sleep -Milliseconds 400; Chord 'r'; Chord 'ctrl+z'; Start-Sleep -Milliseconds 300
  Mark 'sent' @{ name = 'split:focusSecondEditorGroup-then-focusActiveEditorGroup-then-keys' }; Ctl @{ cmd = 'executeCommand'; command = 'workbench.action.focusSecondEditorGroup' }; Start-Sleep -Milliseconds 300; Ctl @{ cmd = 'executeCommand'; command = 'workbench.action.focusFirstEditorGroup' }; Ctl @{ cmd = 'executeCommand'; command = 'workbench.action.focusActiveEditorGroup' }; Start-Sleep -Milliseconds 300; Chord 'r'; Start-Sleep -Milliseconds 300
  Mark 'sent' @{ name = 'split:click-webview-then-keys' }; $r2 = New-Object W+RECT; [void][W]::GetWindowRect($hwnd, [ref]$r2); [W]::MoveTo([int]($r2.L + ($r2.R - $r2.L) * 0.42), $cy); [W]::Button(0, $false); [W]::Button(0, $true); Start-Sleep -Milliseconds 300; Chord 'r'; Chord 'ctrl+z'; Start-Sleep -Milliseconds 300
  Mark 'sent' @{ name = 'split:reveal-preserveFocus-false-then-keys' }; Ctl @{ cmd = 'reveal'; preserveFocus = $false }; Start-Sleep -Milliseconds 300; Chord 'r'; Start-Sleep -Milliseconds 300
  Ctl @{ cmd = 'state' }

  # ---------------------------------------------------------------- retainContextWhenHidden
  Mark 'sent' @{ name = "retain:hide-panel(retain=$Retain)" }; Ctl @{ cmd = 'post'; msg = @{ type = 'state' } }; Ctl @{ cmd = 'openText'; path = (Join-Path $ws 'notes.txt').Replace('\', '/'); column = 1 }; Start-Sleep -Milliseconds 1200
  Mark 'sent' @{ name = 'retain:reveal-panel' }; Ctl @{ cmd = 'reveal' }; Start-Sleep -Milliseconds 1200; Ctl @{ cmd = 'post'; msg = @{ type = 'state' } }; Start-Sleep -Milliseconds 400
  Ctl @{ cmd = 'executeCommand'; command = 'workbench.action.closeEditorsInOtherGroups' }; Start-Sleep -Milliseconds 500

  # ---------------------------------------------------------------- custom editor for *.kicad_pcb / *.kicad_sch
  Mark 'sent' @{ name = 'custom-editor:open-kicad_pcb' }; Ctl @{ cmd = 'openFile'; path = (Join-Path $ws 'ecc83-pp.kicad_pcb').Replace('\', '/'); column = 1 }; Start-Sleep -Milliseconds 1500; Shot 'custom-editor-pcb'
  Focus
  Mark 'sent' @{ name = 'custom-editor:keys' }; Chord 'r'; Chord 'ctrl+z'; Chord 'ctrl+w'; Start-Sleep -Milliseconds 300; Chord 'ctrl+p'; Start-Sleep -Milliseconds 300; Shot 'custom-editor-after-ctrl+p'; Recover
  Mark 'sent' @{ name = 'custom-editor:open-kicad_sch' }; Ctl @{ cmd = 'openFile'; path = (Join-Path $ws 'ecc83-pp.kicad_sch').Replace('\', '/'); column = 2 }; Start-Sleep -Milliseconds 1500; Shot 'custom-editor-sch-split'
  Ctl @{ cmd = 'state' }

  # ---------------------------------------------------------------- window-destroying chords, last
  Mark 'sent' @{ name = 'ctrl+shift+n' }; Focus; Chord 'ctrl+shift+n'; Start-Sleep -Milliseconds 2500
  $fg = [W]::GetForegroundWindow(); Mark 'driver' @{ msg = 'after ctrl+shift+n'; fgTitle = [W]::Title($fg); sameWindow = ($fg -eq $hwnd); windows = ([W]::WindowsOf([uint32]$pid0) | ForEach-Object { [W]::Title($_) }) -join ' | ' }
  if ($fg -ne $hwnd -and [W]::Title($fg) -like '*Extension Development Host*') { Chord 'alt+f4'; Start-Sleep -Milliseconds 1000 }
  [void][W]::SetForegroundWindow($hwnd); Focus
  Mark 'sent' @{ name = 'ctrl+shift+w' }; Chord 'ctrl+shift+w'; Start-Sleep -Milliseconds 2000
  Mark 'driver' @{ msg = 'after ctrl+shift+w'; windows = ([W]::WindowsOf([uint32]$pid0) | ForEach-Object { [W]::Title($_) }) -join ' | '; alive = -not $proc.HasExited }
}
else {
  # canvas mode: let the stream run and collect stats + RTT while moving the mouse and typing
  Mark 'sent' @{ name = 'canvas:stream-10s' }; Shot 'canvas-stream'
  1..120 | ForEach-Object { [W]::MoveTo($cx + ($_ % 20) * 4, $cy + ($_ % 7) * 3); Start-Sleep -Milliseconds 16; if ($_ % 20 -eq 0) { Chord 'r' } }
  Start-Sleep -Seconds 6; Shot 'canvas-stream-2'
  Ctl @{ cmd = 'post'; msg = @{ type = 'dpi' } }
}

Shot "99-final-$Mode"
if (([W]::GetKeyState(0x14) -band 1) -ne 0) { Chord 'capslock'; Mark 'driver' @{ msg = 'capslock restored to off' } }
Mark 'driver' @{ msg = 'done' }
if (-not $KeepOpen) {
  Start-Sleep -Milliseconds 500
  if (-not $proc.HasExited) { Stop-Process -Id $pid0 -Force -ErrorAction SilentlyContinue }
  Start-Sleep -Milliseconds 800
  # children of the dev host (extension host, renderer, etc.) die with the main process; verify by user-data-dir
  $left = Get-CimInstance Win32_Process -Filter "Name='Code.exe'" | Where-Object { $_.CommandLine -like "*$($udd.Replace('\','\\'))*" }
  foreach ($p in $left) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
  Mark 'driver' @{ msg = 'closed'; leftover = @($left).Count }
}
Write-Host "log: $log"


