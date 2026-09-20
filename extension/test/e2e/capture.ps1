# Evidence capture for the end-to-end suite (docs/research/display/img/phase2-*.png must stay < 300 KB):
#   -Mode window  : PrintWindow(PW_RENDERFULLCONTENT) of the Extension Development Host whose command
#                   line carries -Udd (falls back to the primary screen), downscaled to -MaxPx
#   -Mode rect    : CopyFromScreen of the desktop rect -Rect l,t,r,b (what a user SEES there: a cloaked
#                   KiCad shows nothing), downscaled to -MaxPx
#   -Mode probe   : JSON on stdout: GetCursorPos, GetForegroundWindow (+ pid, title) and, with -Rect,
#                   WindowFromPoint at the rect centre and four inner points (+ owning pids)
#   -Mode keys    : bring the Extension Development Host (matched by -Udd) to the foreground and send
#                   -Keys through SendKeys (used by the keybinding shield check). Prints JSON
#                   {sent, foreground_pid, host_pid}; sends nothing when the host could not be made foreground.
#   -Mode activate: bring the Extension Development Host back to the foreground (after "Raise KiCad"
#                   handed it to KiCad), no keys. Prints JSON {activated, foreground_pid, host_pid}.
# Apart from -Mode keys / activate nothing here activates, moves or clicks any window.
param(
  [string]$Mode = 'window',
  [string]$Out = '',
  [string]$Udd = '',
  [string]$Rect = '',
  [string]$Keys = '',
  [int]$MaxPx = 1280,
  [string]$TitleMatch = 'Extension Development Host'
)
Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.Windows.Forms
Add-Type @"
using System; using System.Runtime.InteropServices; using System.Text;
public static class KwE2E {
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int L, T, R, B; }
  [StructLayout(LayoutKind.Sequential)] public struct POINT { public int X, Y; }
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr h, IntPtr hdc, uint flags);
  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
  [DllImport("user32.dll")] public static extern bool GetCursorPos(out POINT p);
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr h);
  [DllImport("user32.dll")] public static extern void keybd_event(byte vk, byte scan, uint flags, UIntPtr extra);
  [DllImport("user32.dll")] public static extern IntPtr WindowFromPoint(POINT p);
  [DllImport("user32.dll")] public static extern IntPtr GetAncestor(IntPtr h, uint flags);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
  [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
  public static string Title(IntPtr h) { var sb = new StringBuilder(512); GetWindowText(h, sb, 512); return sb.ToString(); }
  public static uint Pid(IntPtr h) { uint p; GetWindowThreadProcessId(h, out p); return p; }
}
"@
[void][KwE2E]::SetProcessDPIAware()

function Save-Scaled([System.Drawing.Bitmap]$bmp, [string]$path, [int]$maxPx) {
  $scale = [Math]::Min(1.0, $maxPx / [Math]::Max($bmp.Width, $bmp.Height))
  if ($scale -lt 1.0) {
    $w = [int]($bmp.Width * $scale); $h = [int]($bmp.Height * $scale)
    $small = New-Object System.Drawing.Bitmap($w, $h)
    $g = [System.Drawing.Graphics]::FromImage($small)
    $g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
    $g.DrawImage($bmp, 0, 0, $w, $h); $g.Dispose()
    $small.Save($path, [System.Drawing.Imaging.ImageFormat]::Png); $small.Dispose()
  } else {
    $bmp.Save($path, [System.Drawing.Imaging.ImageFormat]::Png)
  }
}

function Parse-Rect([string]$s) {
  $p = $s.Split(',') | ForEach-Object { [int]$_ }
  return @{ L = $p[0]; T = $p[1]; R = $p[2]; B = $p[3] }
}

if ($Mode -eq 'probe') {
  $c = New-Object KwE2E+POINT
  [void][KwE2E]::GetCursorPos([ref]$c)
  $fg = [KwE2E]::GetForegroundWindow()
  $res = @{ cursor = @($c.X, $c.Y); foreground = @{ hwnd = [int64]$fg; pid = [KwE2E]::Pid($fg); title = [KwE2E]::Title($fg) } }
  if ($Rect) {
    $r = Parse-Rect $Rect
    $pts = @(
      @([int](($r.L + $r.R) / 2), [int](($r.T + $r.B) / 2)),
      @([int]($r.L + ($r.R - $r.L) * 0.25), [int]($r.T + ($r.B - $r.T) * 0.25)),
      @([int]($r.L + ($r.R - $r.L) * 0.75), [int]($r.T + ($r.B - $r.T) * 0.25)),
      @([int]($r.L + ($r.R - $r.L) * 0.25), [int]($r.T + ($r.B - $r.T) * 0.75)),
      @([int]($r.L + ($r.R - $r.L) * 0.75), [int]($r.T + ($r.B - $r.T) * 0.75))
    )
    $hits = @()
    foreach ($pt in $pts) {
      $p = New-Object KwE2E+POINT; $p.X = $pt[0]; $p.Y = $pt[1]
      $h = [KwE2E]::WindowFromPoint($p)
      $root = [KwE2E]::GetAncestor($h, 2)
      if ($root -eq [IntPtr]::Zero) { $root = $h }
      $hits += @{ at = @($pt[0], $pt[1]); hwnd = [int64]$h; pid = [KwE2E]::Pid($h); title = [KwE2E]::Title($root) }
    }
    $res.window_from_point = $hits
  }
  $res | ConvertTo-Json -Compress -Depth 5
  exit 0
}

function Find-Host {
  $p = $null
  if ($Udd) {
    $ids = Get-CimInstance Win32_Process -Filter "Name='Code.exe'" | Where-Object { $_.CommandLine -like "*$Udd*" } | Select-Object -ExpandProperty ProcessId
    if ($ids) { $p = Get-Process -Id $ids -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -ne 0 -and $_.MainWindowTitle -like "*$TitleMatch*" } | Select-Object -First 1 }
  }
  if ($null -eq $p) { $p = Get-Process | Where-Object { $_.MainWindowTitle -like "*$TitleMatch*" } | Select-Object -First 1 }
  return $p
}

if ($Mode -eq 'keys') {
  $p = Find-Host
  if ($null -eq $p) { @{ sent = $false; reason = 'no dev host window' } | ConvertTo-Json -Compress; exit 0 }
  $h = $p.MainWindowHandle
  # SetForegroundWindow is refused for a background caller: the documented ALT tap grants it once
  [void][KwE2E]::SetForegroundWindow($h)
  if ([KwE2E]::GetForegroundWindow() -ne $h) {
    [KwE2E]::keybd_event(0x12, 0, 0, [UIntPtr]::Zero); [KwE2E]::keybd_event(0x12, 0, 2, [UIntPtr]::Zero)
    [void][KwE2E]::SetForegroundWindow($h); [void][KwE2E]::BringWindowToTop($h)
  }
  Start-Sleep -Milliseconds 250
  $fg = [KwE2E]::GetForegroundWindow()
  $sent = $false
  if ($fg -eq $h) { [System.Windows.Forms.SendKeys]::SendWait($Keys); $sent = $true; Start-Sleep -Milliseconds 300 }
  @{ sent = $sent; foreground_pid = [KwE2E]::Pid($fg); host_pid = $p.Id; keys = $Keys } | ConvertTo-Json -Compress
  exit 0
}

if ($Mode -eq 'activate') {
  $p = Find-Host
  if ($null -eq $p) { @{ activated = $false; reason = 'no dev host window' } | ConvertTo-Json -Compress; exit 0 }
  $h = $p.MainWindowHandle
  [void][KwE2E]::SetForegroundWindow($h)
  if ([KwE2E]::GetForegroundWindow() -ne $h) {
    [KwE2E]::keybd_event(0x12, 0, 0, [UIntPtr]::Zero); [KwE2E]::keybd_event(0x12, 0, 2, [UIntPtr]::Zero)
    [void][KwE2E]::SetForegroundWindow($h); [void][KwE2E]::BringWindowToTop($h)
  }
  Start-Sleep -Milliseconds 250
  $fg = [KwE2E]::GetForegroundWindow()
  @{ activated = ($fg -eq $h); foreground_pid = [KwE2E]::Pid($fg); host_pid = $p.Id } | ConvertTo-Json -Compress
  exit 0
}

if ($Mode -eq 'rect') {
  $r = Parse-Rect $Rect
  $w = $r.R - $r.L; $h = $r.B - $r.T
  $bmp = New-Object System.Drawing.Bitmap($w, $h)
  $g = [System.Drawing.Graphics]::FromImage($bmp)
  $g.CopyFromScreen($r.L, $r.T, 0, 0, (New-Object System.Drawing.Size($w, $h))); $g.Dispose()
  Save-Scaled $bmp $Out $MaxPx; $bmp.Dispose()
  Write-Output "captured desktop rect ${w}x${h} -> $Out"
  exit 0
}

$p = $null
if ($Udd) {
  $ids = Get-CimInstance Win32_Process -Filter "Name='Code.exe'" | Where-Object { $_.CommandLine -like "*$Udd*" } | Select-Object -ExpandProperty ProcessId
  if ($ids) { $p = Get-Process -Id $ids -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -ne 0 -and $_.MainWindowTitle -like "*$TitleMatch*" } | Select-Object -First 1 }
}
if ($null -eq $p) { $p = Get-Process | Where-Object { $_.MainWindowTitle -like "*$TitleMatch*" } | Select-Object -First 1 }
if ($null -eq $p) {
  $b = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
  $bmp = New-Object System.Drawing.Bitmap($b.Width, $b.Height)
  $g = [System.Drawing.Graphics]::FromImage($bmp); $g.CopyFromScreen($b.Location, [System.Drawing.Point]::Empty, $b.Size); $g.Dispose()
  Save-Scaled $bmp $Out $MaxPx; $bmp.Dispose()
  Write-Output "captured primary screen (no window matched '$TitleMatch') -> $Out"
  exit 0
}
$r = New-Object KwE2E+RECT
[void][KwE2E]::GetWindowRect($p.MainWindowHandle, [ref]$r)
$w = $r.R - $r.L; $h = $r.B - $r.T
$bmp = New-Object System.Drawing.Bitmap($w, $h)
$g = [System.Drawing.Graphics]::FromImage($bmp)
$hdc = $g.GetHdc()
$ok = [KwE2E]::PrintWindow($p.MainWindowHandle, $hdc, 2)   # PW_RENDERFULLCONTENT
$g.ReleaseHdc($hdc)
if (-not $ok) { $g.CopyFromScreen($r.L, $r.T, 0, 0, (New-Object System.Drawing.Size($w, $h))) }
$g.Dispose()
Save-Scaled $bmp $Out $MaxPx; $bmp.Dispose()
Write-Output "captured '$($p.MainWindowTitle)' ${w}x${h} pid=$($p.Id) -> $Out"
