param([ValidateSet("pcbnew","eeschema")][string]$App, [string]$File, [int]$WaitSlotSec = 420)
$SP = "C:\Users\usama\AppData\Local\Temp\claude\c--Upwork-KiCad\48a8df66-7213-479f-ab2e-a63b89135016\scratchpad\kicad-plugin-and-build"
$env:KICAD_CONFIG_HOME = "$SP\sandbox\config"; $env:KICAD_DOCUMENTS_HOME = "$SP\sandbox\documents"; $env:KICAD_CACHE_HOME = "$SP\sandbox\cache"
$dump = "$SP\sandbox\documents\KiCad\10.0\plugins\com.example.envdump\envdump.txt"; Remove-Item $dump -ErrorAction SilentlyContinue
Add-Type -AssemblyName System.Windows.Forms
Add-Type @"
using System; using System.Runtime.InteropServices;
public class M { [DllImport("user32.dll")] public static extern void mouse_event(uint f, uint x, uint y, uint d, UIntPtr e); [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out R r); [StructLayout(LayoutKind.Sequential)] public struct R { public int L,T,Rt,B; } }
"@
$t0 = Get-Date
while ((Get-Process pcbnew,eeschema,kicad -ErrorAction SilentlyContinue) -and ((Get-Date) - $t0).TotalSeconds -lt $WaitSlotSec) { Start-Sleep -Milliseconds 700 }
if (Get-Process pcbnew,eeschema,kicad -ErrorAction SilentlyContinue) { "ABORT: slot never free"; exit 2 }
$p = Start-Process -FilePath "C:\Program Files\KiCad\10.0\bin\$App.exe" -ArgumentList "`"$File`"" -PassThru
"launched $App PID $($p.Id) at $(Get-Date -Format HH:mm:ss.fff)"
try {
  for ($i=0; $i -lt 60; $i++) { Start-Sleep -Milliseconds 500; $p.Refresh(); if ($p.MainWindowTitle -and $p.MainWindowTitle -ne "KiCad Setup") { break } }
  Start-Sleep -Seconds 3
  & powershell -NoProfile -ExecutionPolicy Bypass -File "$SP\screenshot.ps1" -ProcId $p.Id -Out "$SP\shot_click_$App.png" | Out-Null
  $r = New-Object M+R; [M]::GetWindowRect($p.MainWindowHandle, [ref]$r) | Out-Null
  $pos = (& "$SP\.venv\Scripts\python.exe" "$SP\find_blue.py" "$SP\shot_click_$App.png").Trim()
  "blue button in window at: $pos ; window origin $($r.L),$($r.T)"
  if ($pos -ne "none") {
    $xy = $pos.Split(" "); $sx = [int]$xy[0] + $r.L; $sy = [int]$xy[1] + $r.T
    $vw = [System.Windows.Forms.SystemInformation]::VirtualScreen.Width; $vh = [System.Windows.Forms.SystemInformation]::VirtualScreen.Height
    $nx = [uint32]([math]::Round($sx * 65535 / $vw)); $ny = [uint32]([math]::Round($sy * 65535 / $vh))
    foreach ($k in 1..2) {
      [M]::mouse_event(0x8001, $nx, $ny, 0, [UIntPtr]::Zero); Start-Sleep -Milliseconds 200   # MOVE|ABSOLUTE
      [M]::mouse_event(0x8001, $nx+1, $ny, 0, [UIntPtr]::Zero); Start-Sleep -Milliseconds 200
      [M]::mouse_event(0x8002, $nx, $ny, 0, [UIntPtr]::Zero); Start-Sleep -Milliseconds 120  # LEFTDOWN
      [M]::mouse_event(0x8004, $nx, $ny, 0, [UIntPtr]::Zero); Start-Sleep -Milliseconds 800  # LEFTUP
    }
    "clicked at screen $sx,$sy"; Start-Sleep -Milliseconds 1500; & powershell -NoProfile -ExecutionPolicy Bypass -File "$SP\screenshot.ps1" -ProcId $p.Id -Out "$SP\shot_afterclick_$App.png" | Out-Null
    for ($i=0; $i -lt 20; $i++) { Start-Sleep -Milliseconds 500; if (Test-Path $dump) { break } }
    if (Test-Path $dump) { "envdump.txt after $($i*0.5)s:"; Get-Content $dump } else { "envdump.txt NOT written" }
  }
} finally { if (-not $p.HasExited) { Stop-Process -Id $p.Id -Force -Confirm:$false; "killed PID $($p.Id)" } }
