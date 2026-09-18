"""Launch eeschema (isolated cfg + private API socket), send ONE rpc, report whether the PID survived."""
import os, sys, subprocess, time, json
SP = os.path.dirname(os.path.abspath(__file__))
KVER = os.environ.get("KVER", "10.0")
EXE = rf"C:\Program Files\KiCad\{KVER}\bin\kicad.exe"
SCH = os.path.join(SP, "proj", "ecc83-pp.kicad_sch")
CFG = os.path.join(SP, f"cfg{KVER.replace('.0','')}")
SOCK = None  # default socket; KiCad 10.0.0 does not honour KICAD_API_SOCKET as an override
os.environ["KICAD_CONFIG_HOME"] = CFG

from kipy import KiCad
from kipy.errors import ApiError, ConnectionError as KConnErr
from kipy.proto.common.types import DocumentType, KIID
from kipy.proto.common.types import base_types_pb2 as bt, enums_pb2 as en
from kipy.proto.common.commands import editor_commands_pb2 as ec, base_commands_pb2 as bc
from kipy.proto.schematic import schematic_types_pb2 as st
from kipy.util import pack_any

def pid_alive(pid):
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True).stdout
    return str(pid) in out

def wer_events(since):
    ps = ("Get-WinEvent -FilterHashtable @{LogName='Application';Id=1000;StartTime=(Get-Date).AddSeconds(-%d)} -ErrorAction SilentlyContinue | "
          "Where-Object { $_.Message -match 'eeschema|kicad' } | ForEach-Object { ($_.Message -split \"`n\" | Select-String 'Faulting module name|Fault offset|Faulting process id') -join ' | ' }") % since
    return subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True).stdout.strip()

def kill_mine():
    ps = ("Get-CimInstance Win32_Process | Where-Object { ($_.Name -eq 'eeschema.exe' -or $_.Name -eq 'kicad.exe') -and $_.CommandLine -like '*verify-sch-ipc*' } | "
          "ForEach-Object { Stop-Process -Id $_.ProcessId -Force; \"killed $($_.ProcessId)\" }")
    return subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True).stdout.strip()

def build(name, doc):
    def hdr(cmd): cmd.header.document.CopyFrom(doc); return cmd
    if name == "HitTest":
        cmd = hdr(ec.HitTest()); cmd.id.CopyFrom(KIID(value="00000000-0000-0000-0000-000000000000"))
        cmd.position.x_nm = 0; cmd.position.y_nm = 0; cmd.tolerance = 100000
        return cmd, ec.HitTestResponse
    if name == "BeginCommit":
        return ec.BeginCommit(), ec.BeginCommitResponse
    if name == "CreateItems":
        cmd = hdr(ec.CreateItems()); ln = st.Line()
        ln.start.x_nm = 10_000_000; ln.start.y_nm = 10_000_000; ln.end.x_nm = 20_000_000; ln.end.y_nm = 10_000_000
        cmd.items.append(pack_any(ln)); return cmd, ec.CreateItemsResponse
    if name == "UpdateItems":
        cmd = hdr(ec.UpdateItems()); ln = st.Line(); ln.id.CopyFrom(KIID(value="00000000-0000-0000-0000-000000000000"))
        cmd.items.append(pack_any(ln)); return cmd, ec.UpdateItemsResponse
    if name == "DeleteItems":
        cmd = hdr(ec.DeleteItems()); cmd.item_ids.append(KIID(value="00000000-0000-0000-0000-000000000000")); return cmd, ec.DeleteItemsResponse
    if name == "EndCommit":
        cmd = ec.EndCommit(); cmd.id.CopyFrom(KIID(value="00000000-0000-0000-0000-000000000000")); cmd.action = ec.CMA_DROP; return cmd, ec.EndCommitResponse
    if name == "GetItems":
        cmd = hdr(ec.GetItems()); cmd.types.append(en.KOT_SCH_LINE); return cmd, ec.GetItemsResponse
    if name == "GetSelection":
        return hdr(ec.GetSelection()), ec.SelectionResponse
    if name == "GetTitleBlockInfo":
        cmd = ec.GetTitleBlockInfo(); cmd.document.CopyFrom(doc); return cmd, bt.TitleBlockInfo
    if name == "SaveDocumentToString":
        cmd = ec.SaveDocumentToString(); cmd.document.CopyFrom(doc); return cmd, ec.SavedDocumentResponse
    if name == "RunAction":
        cmd = ec.RunAction(); cmd.action = "common.Control.zoomFitScreen"; return cmd, ec.RunActionResponse
    if name == "Ping":
        return bc.Ping(), bc.Ping
    if name == "GetVersion":
        return bc.GetVersion(), bc.GetVersionResponse
    raise SystemExit("unknown rpc " + name)

def foreign_kicad():
    ps = "Get-CimInstance Win32_Process | Where-Object { $_.Name -match 'eeschema|pcbnew|kicad' } | ForEach-Object { \"$($_.ProcessId) $($_.Name) $($_.CommandLine)\" }"
    return subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True).stdout.strip()

def main():
    rpcs = sys.argv[1:]
    f = foreign_kicad()
    if f:
        print("ABORT: other KiCad processes running: " + f); return
    print(f"KiCad {KVER}; socket {SOCK}")
    print("pre-clean:", kill_mine())
    p = subprocess.Popen([EXE, SCH])
    pid = p.pid
    print("launched pid", pid, flush=True)
    k = KiCad(socket_path=SOCK, timeout_ms=6000)
    docs = None
    for i in range(40):
        time.sleep(1)
        try:
            docs = list(k.get_open_documents(DocumentType.DOCTYPE_SCHEMATIC))
            if docs: break
        except Exception as e:
            docs = None
            if i % 5 == 0: print("  waiting:", type(e).__name__, str(e)[:120], flush=True)
    print("GetOpenDocuments ->", str(docs).replace("\n", " ") if docs else "FAILED", flush=True)
    if not docs:
        print("kill:", kill_mine()); return
    doc = docs[0]
    for name in rpcs:
        cmd, rt = build(name, doc)
        t0 = time.time()
        try:
            r = k._client.send(cmd, rt)
            print(f"[{name}] RESPONSE ({time.time()-t0:.2f}s): {str(r).replace(chr(10),' ')[:400]!r}")
        except Exception as e:
            print(f"[{name}] EXC ({time.time()-t0:.2f}s): {type(e).__name__}: {str(e)[:300]}")
        time.sleep(3)
        alive = pid_alive(pid)
        print(f"[{name}] pid {pid} alive: {alive}", flush=True)
        if not alive:
            time.sleep(4)
            print(f"[{name}] WER events: {wer_events(60)}")
            break
    print("cleanup:", kill_mine())
    print("other kicad procs at end:", foreign_kicad() or "none")

main()
