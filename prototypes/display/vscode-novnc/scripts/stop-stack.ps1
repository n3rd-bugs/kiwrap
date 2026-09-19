# stop-stack.ps1 : stop exactly the processes start-stack.ps1 started (by PID) and remove the TightVNC registry key.
param([string]$Scratch = "$env:LOCALAPPDATA\Temp\kiwrap-vnc-spike")
foreach ($f in "pcbnew", "websockify", "tvnserver") {
  $p = "$Scratch\$f.pid"
  if (Test-Path $p) { $id = [int](Get-Content $p); Get-CimInstance Win32_Process -Filter "ParentProcessId=$id" | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; Stop-Process -Id $id -Force -ErrorAction SilentlyContinue; Remove-Item $p; "stopped $f ($id)" }
}
Get-Process tvnserver -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "$Scratch*" } | Stop-Process -Force -ErrorAction SilentlyContinue  # the -controlapp -slave helper
Remove-Item -Path "HKCU:\Software\TightVNC" -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item "$Scratch\proj\~*.lck" -ErrorAction SilentlyContinue
"done"
