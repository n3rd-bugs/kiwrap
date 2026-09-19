# start-stack.ps1 : the exact stack the spike used (see docs/research/display/vnc-novnc.md), parametrised.
#   1. sandboxed pcbnew.exe on a scratch copy of the ecc83 demo (unique title "vncspike")
#   2. TightVNC 2.8.88 in application mode (no service), loopback only, no password, sharing ONLY that window
#   3. websockify serving the noVNC checkout (+ page/kicad.html copied into it) on 127.0.0.1:6080 -> 5900
# Prereqs (all portable, nothing installed):
#   - TightVNC msi unpacked:  msiexec /a tightvnc-2.8.88-gpl-setup-64bit.msi /qn TARGETDIR=<TightVncDir>
#   - noVNC checkout:         git clone --depth 1 https://github.com/novnc/noVNC.git <NoVncDir>
#   - websockify:             uv venv <venv>; uv pip install --python <venv>/Scripts/python.exe websockify
#   - kiwrap repo (for scripts/seed_config_root.py) at <Repo>
param(
  [string]$Repo = (Resolve-Path "$PSScriptRoot\..\..\..\..").Path,
  [string]$KicadRoot = "C:\Program Files\KiCad\10.0",
  [string]$Scratch = "$env:LOCALAPPDATA\Temp\kiwrap-vnc-spike",
  [string]$TightVncDir = "$env:LOCALAPPDATA\Temp\kiwrap-vnc-spike\tvn\PFiles\TightVNC",
  [string]$NoVncDir = "$env:LOCALAPPDATA\Temp\kiwrap-vnc-spike\novnc",
  [string]$Websockify = "$env:LOCALAPPDATA\Temp\kiwrap-vnc-spike\pyvenv\Scripts\websockify.exe",
  [string]$Canvas = "",        # "" = OpenGL (canvas_type 1), "cairo" = Cairo (canvas_type 2)
  [int]$WinX = 100, [int]$WinY = 100
)
$ErrorActionPreference = "Stop"
New-Item -ItemType Directory -Force "$Scratch\proj", "$Scratch\ktemp" | Out-Null
# --- 1. sandbox + project copy
& uv run --project $Repo python "$Repo\scripts\seed_config_root.py" seed --kicad-root $KicadRoot --storage "$Scratch\storage" | Out-Host
$cfg = Get-ChildItem "$Scratch\storage\kicad-config" | Select-Object -First 1
$ver = Get-ChildItem $cfg.FullName -Directory | Where-Object { $_.Name -match '^\d+\.\d+$' } | Select-Object -First 1
Copy-Item "$KicadRoot\share\kicad\demos\ecc83\*" "$Scratch\proj\" -Recurse -Force
foreach ($ext in "kicad_pcb", "kicad_pro", "kicad_sch") { Copy-Item "$Scratch\proj\ecc83-pp.$ext" "$Scratch\proj\vncspike.$ext" -Force }
Remove-Item "$Scratch\proj\~*.lck" -ErrorAction SilentlyContinue
$ct = if ($Canvas -eq "cairo") { 2 } else { 1 }   # EDA_DRAW_PANEL_GAL::GAL_TYPE: OPENGL=1, CAIRO=2
"{`"graphics`":{`"canvas_type`":$ct},`"window`":{`"maximized`":false,`"size_x`":1600,`"size_y`":1000,`"pos_x`":$WinX,`"pos_y`":$WinY}}" | Set-Content -Encoding ascii "$($ver.FullName)\pcbnew.json"
$env:KICAD_CONFIG_HOME = $cfg.FullName
$env:KICAD_DOCUMENTS_HOME = "$Scratch\storage\kicad-documents\$($cfg.Name)"
$env:KICAD_CACHE_HOME = "$Scratch\storage\kicad-cache\$($cfg.Name)"
$env:TEMP = "$Scratch\ktemp"; $env:TMP = "$Scratch\ktemp"
$pcb = Start-Process -FilePath "$KicadRoot\bin\pcbnew.exe" -ArgumentList "`"$Scratch\proj\vncspike.kicad_pcb`"" -PassThru
Set-Content "$Scratch\pcbnew.pid" $pcb.Id
for ($i = 0; $i -lt 60; $i++) { Start-Sleep -Milliseconds 500; $pcb.Refresh(); if ($pcb.MainWindowTitle -like "*vncspike*PCB Editor*") { break } }
"pcbnew pid=$($pcb.Id) title='$($pcb.MainWindowTitle)'"
# --- 2. TightVNC app mode, loopback only, share only that window
$k = "HKCU:\Software\TightVNC\Server"; New-Item -Path $k -Force | Out-Null
foreach ($kv in @{UseVncAuthentication=0; LoopbackOnly=1; AllowLoopback=1; RfbPort=5900; AcceptHttpConnections=0; AcceptRfbConnections=1; RemoveWallpaper=0; EnableFileTransfers=0; GrabTransparentWindows=1; UseMirrorDriver=0; PollingInterval=30; RunControlInterface=1}.GetEnumerator()) { Set-ItemProperty -Path $k -Name $kv.Key -Value $kv.Value -Type DWord }
$tvn = Start-Process -FilePath "$TightVncDir\tvnserver.exe" -ArgumentList "-run" -PassThru
Set-Content "$Scratch\tvnserver.pid" $tvn.Id
Start-Sleep 2
& "$TightVncDir\tvnserver.exe" -controlapp -sharewindow "vncspike $([char]0x2014) PCB Editor"
# --- 3. noVNC page + websockify
Copy-Item "$PSScriptRoot\..\page\kicad.html" "$NoVncDir\kicad.html" -Force
$ws = Start-Process -FilePath $Websockify -ArgumentList "--web `"$NoVncDir`" 127.0.0.1:6080 127.0.0.1:5900" -PassThru -WindowStyle Hidden
Set-Content "$Scratch\websockify.pid" $ws.Id
Start-Sleep 2
"open http://127.0.0.1:6080/kicad.html?fit=dpr   (or run the VS Code extension: F5 / code --extensionDevelopmentPath)"
