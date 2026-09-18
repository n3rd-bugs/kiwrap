#!/bin/bash
SP="C:/Users/usama/AppData/Local/Temp/claude/c--Upwork-KiCad/48a8df66-7213-479f-ab2e-a63b89135016/scratchpad/verify-sch-ipc"
cd "$SP"
export PYTHONIOENCODING=utf-8
waitfree() {
  for i in $(seq 1 60); do
    if tasklist | grep -q -i -E "eeschema.exe|pcbnew.exe|kicad.exe"; then sleep 5; else return 0; fi
  done
  echo "TIMEOUT waiting for foreign KiCad to exit"; return 1
}
for set in "Ping GetVersion BeginCommit"; do
  echo "######## SET: $set"
  waitfree || exit 1
  timeout 200 .venv/Scripts/python.exe ${ISO:-isolate.py} $set 2>&1
done
