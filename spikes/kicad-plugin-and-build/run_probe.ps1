param([ValidateSet("pcbnew","eeschema")][string]$App, [string]$File, [string]$Probe, [int]$WaitSlotSec = 420)
$SP = "C:\Users\usama\AppData\Local\Temp\claude\c--Upwork-KiCad\48a8df66-7213-479f-ab2e-a63b89135016\scratchpad\kicad-plugin-and-build"
$env:KICAD_CONFIG_HOME = "$SP\sandbox\config"; $env:KICAD_DOCUMENTS_HOME = "$SP\sandbox\documents"; $env:KICAD_CACHE_HOME = "$SP\sandbox\cache"
# 1. wait for a free slot (no KiCad GUI from anyone)
$t0 = Get-Date
while ((Get-Process pcbnew,eeschema,kicad -ErrorAction SilentlyContinue) -and ((Get-Date) - $t0).TotalSeconds -lt $WaitSlotSec) { Start-Sleep -Milliseconds 700 }
if (Get-Process pcbnew,eeschema,kicad -ErrorAction SilentlyContinue) { "ABORT: slot never free"; exit 2 }
# 2. launch
$p = Start-Process -FilePath "C:\Program Files\KiCad\10.0\bin\$App.exe" -ArgumentList "`"$File`"" -PassThru
"launched $App PID $($p.Id) at $(Get-Date -Format HH:mm:ss.fff)"
try {
  # 3. wait for main window (not the setup wizard) and pipe
  for ($i=0; $i -lt 60; $i++) { Start-Sleep -Milliseconds 500; $p.Refresh(); if ($p.MainWindowTitle -and $p.MainWindowTitle -ne "KiCad Setup") { break } }
  "window title: '$($p.MainWindowTitle)' after $($i*0.5)s"
  $pipes = [System.IO.Directory]::GetFiles('\\.\pipe\') | Where-Object { $_ -match 'kicad' }; "pipes: $pipes"
  $others = Get-Process pcbnew,eeschema,kicad -ErrorAction SilentlyContinue | Where-Object { $_.Id -ne $p.Id }; if ($others) { "WARNING: other KiCad GUI appeared: $($others.Id -join ',')" }
  Start-Sleep -Seconds 3
  # 4. probe
  "----- probe output -----"
  & "$SP\.venv\Scripts\python.exe" "$SP\$Probe" 2>&1 | ForEach-Object { "$_" }
  "----- end probe -----"
  # 5. screenshot
  & powershell -NoProfile -ExecutionPolicy Bypass -File "$SP\screenshot.ps1" -ProcId $p.Id -Out "$SP\shot_$App.png"
} finally {
  # 6. kill only my process
  if (-not $p.HasExited) { Stop-Process -Id $p.Id -Force -Confirm:$false; "killed PID $($p.Id)" }
}
