$SP = "C:\Users\usama\AppData\Local\Temp\claude\c--Upwork-KiCad\48a8df66-7213-479f-ab2e-a63b89135016\scratchpad\kicad-plugin-and-build"
$env:KICAD_CONFIG_HOME = "$SP\sandbox\config"; $env:KICAD_DOCUMENTS_HOME = "$SP\sandbox\documents"; $env:KICAD_CACHE_HOME = "$SP\sandbox\cache"
Add-Type -AssemblyName System.Windows.Forms
Add-Type @"
using System; using System.Runtime.InteropServices;
public class M2 {
  [StructLayout(LayoutKind.Sequential)] public struct R { public int L,T,Rt,B; }
  [StructLayout(LayoutKind.Sequential)] public struct PT { public int X, Y; }
  [DllImport("user32.dll")] public static extern void mouse_event(uint f, uint x, uint y, uint d, UIntPtr e);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out R r);
  [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
  [DllImport("user32.dll")] public static extern IntPtr WindowFromPoint(PT p);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
}
"@
$t0 = Get-Date
while ((Get-Process pcbnew,eeschema,kicad -ErrorAction SilentlyContinue) -and ((Get-Date) - $t0).TotalSeconds -lt 400) { Start-Sleep -Milliseconds 700 }
if (Get-Process pcbnew,eeschema,kicad -ErrorAction SilentlyContinue) { "ABORT"; exit 2 }
$p = Start-Process -FilePath "C:\Program Files\KiCad\10.0\bin\pcbnew.exe" -ArgumentList "`"$SP\sandbox\proj\ecc83-pp.kicad_pcb`"" -PassThru
try {
  for ($i=0; $i -lt 60; $i++) { Start-Sleep -Milliseconds 500; $p.Refresh(); if ($p.MainWindowTitle -and $p.MainWindowTitle -ne "KiCad Setup") { break } }
  Start-Sleep -Seconds 3
  & powershell -NoProfile -ExecutionPolicy Bypass -File "$SP\screenshot.ps1" -ProcId $p.Id -Out "$SP\shot_ct0.png" | Out-Null
  $r = New-Object M2+R; [M2]::GetWindowRect($p.MainWindowHandle, [ref]$r) | Out-Null
  $pos = (& "$SP\.venv\Scripts\python.exe" "$SP\find_blue.py" "$SP\shot_ct0.png").Trim(); $xy = $pos.Split(" ")
  $sx = [int]$xy[0] + $r.L; $sy = [int]$xy[1] + $r.T
  "button screen pos $sx,$sy"
  [M2]::SetCursorPos($sx, $sy) | Out-Null; Start-Sleep -Milliseconds 400
  $pt = New-Object M2+PT; $pt.X = $sx; $pt.Y = $sy; $hw = [M2]::WindowFromPoint($pt); $pid2 = 0; [M2]::GetWindowThreadProcessId($hw, [ref]$pid2) | Out-Null
  "window under cursor: hwnd=$hw pid=$pid2 (pcbnew pid=$($p.Id))"
  [M2]::mouse_event(0x0002, 0, 0, 0, [UIntPtr]::Zero); Start-Sleep -Milliseconds 150; [M2]::mouse_event(0x0004, 0, 0, 0, [UIntPtr]::Zero)
  Start-Sleep -Seconds 3
  $dump = "$SP\sandbox\documents\KiCad\10.0\plugins\com.example.envdump\envdump.txt"
  if (Test-Path $dump) { "envdump.txt:"; Get-Content $dump } else { "envdump.txt NOT written" }
  Get-CimInstance Win32_Process -Filter "ParentProcessId=$($p.Id)" | Select-Object ProcessId, Name, CommandLine | Format-List
  & powershell -NoProfile -ExecutionPolicy Bypass -File "$SP\screenshot.ps1" -ProcId $p.Id -Out "$SP\shot_ct1.png" | Out-Null
} finally { if (-not $p.HasExited) { Stop-Process -Id $p.Id -Force -Confirm:$false; "killed $($p.Id)" } }
