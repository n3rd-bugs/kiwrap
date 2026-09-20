# Captures the Extension Development Host window (or the whole primary screen as a fallback) to a PNG.
# Usage: powershell -NoProfile -File capture_window.ps1 -Out <png> [-Udd <user-data-dir>] [-TitleMatch "Extension Development Host"]
# With -Udd the window is the main Code.exe whose command line carries that --user-data-dir (several dev hosts may run).
param([string]$Out, [string]$Udd = '', [string]$TitleMatch = 'Extension Development Host')
Add-Type -AssemblyName System.Drawing
Add-Type @"
using System; using System.Runtime.InteropServices;
public static class KwWin {
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int L, T, R, B; }
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr h, IntPtr hdc, uint flags);
  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
}
"@
[void][KwWin]::SetProcessDPIAware()
$p = $null
if ($Udd) {
  $ids = Get-CimInstance Win32_Process -Filter "Name='Code.exe'" | Where-Object { $_.CommandLine -like "*$Udd*" } | Select-Object -ExpandProperty ProcessId
  $p = Get-Process -Id $ids -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -ne 0 -and $_.MainWindowTitle -like "*$TitleMatch*" } | Select-Object -First 1
}
if ($null -eq $p) { $p = Get-Process | Where-Object { $_.MainWindowTitle -like "*$TitleMatch*" } | Select-Object -First 1 }
if ($null -eq $p) {
  $b = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
  $bmp = New-Object System.Drawing.Bitmap($b.Width, $b.Height)
  $g = [System.Drawing.Graphics]::FromImage($bmp); $g.CopyFromScreen($b.Location, [System.Drawing.Point]::Empty, $b.Size); $g.Dispose()
  $bmp.Save($Out, [System.Drawing.Imaging.ImageFormat]::Png); $bmp.Dispose()
  Write-Output "captured primary screen (no window matched '$TitleMatch') -> $Out"
  exit 0
}
$r = New-Object KwWin+RECT
[void][KwWin]::GetWindowRect($p.MainWindowHandle, [ref]$r)
$w = $r.R - $r.L; $h = $r.B - $r.T
$bmp = New-Object System.Drawing.Bitmap($w, $h)
$g = [System.Drawing.Graphics]::FromImage($bmp)
$hdc = $g.GetHdc()
$ok = [KwWin]::PrintWindow($p.MainWindowHandle, $hdc, 2)   # PW_RENDERFULLCONTENT
$g.ReleaseHdc($hdc)
if (-not $ok) { $g.CopyFromScreen($r.L, $r.T, 0, 0, (New-Object System.Drawing.Size($w, $h))) }
$g.Dispose()
$bmp.Save($Out, [System.Drawing.Imaging.ImageFormat]::Png); $bmp.Dispose()
Write-Output "captured '$($p.MainWindowTitle)' ${w}x${h} pid=$($p.Id) -> $Out"
