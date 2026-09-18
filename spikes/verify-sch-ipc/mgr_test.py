"""Against an already-running kicad.exe (project manager) hosting the schematic editor: send Ping, GetVersion,
GetOpenDocuments, then BeginCommit, and check whether the kicad.exe PID survives."""
import sys, subprocess, time
from kipy import KiCad
from kipy.proto.common.types import DocumentType
from kipy.proto.common.commands import editor_commands_pb2 as ec

pid = int(sys.argv[1])

def pid_alive():
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True).stdout
    return str(pid) in out

k = KiCad(timeout_ms=6000)
for name, fn in [("Ping", k.ping), ("GetVersion", k.get_version),
                 ("GetOpenDocuments(SCH)", lambda: list(k.get_open_documents(DocumentType.DOCTYPE_SCHEMATIC))),
                 ("GetOpenDocuments(PCB)", lambda: list(k.get_open_documents(DocumentType.DOCTYPE_PCB)))]:
    try:
        print(f"[{name}] ->", str(fn()).replace("\n", " ")[:300])
    except Exception as e:
        print(f"[{name}] EXC {type(e).__name__}: {str(e)[:200]}")
    print(f"   kicad.exe pid {pid} alive: {pid_alive()}")

t0 = time.time()
try:
    r = k._client.send(ec.BeginCommit(), ec.BeginCommitResponse)
    print(f"[BeginCommit] -> {str(r)!r} ({time.time()-t0:.1f}s)")
except Exception as e:
    print(f"[BeginCommit] EXC {type(e).__name__}: {str(e)[:200]} ({time.time()-t0:.1f}s)")
time.sleep(3)
print(f"   kicad.exe pid {pid} alive: {pid_alive()}")
ps = ("Get-WinEvent -FilterHashtable @{LogName='Application';Id=1000;StartTime=(Get-Date).AddSeconds(-60)} -ErrorAction SilentlyContinue | "
      "ForEach-Object { ($_.Message -split \"`n\" | Select-String 'Faulting application name|Faulting module name|Fault offset|Faulting process id') -join ' | ' }")
time.sleep(4)
print("WER:", subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True).stdout.strip())
