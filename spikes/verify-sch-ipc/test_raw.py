import sys, subprocess, time
from kipy import KiCad
from kipy.proto.common.types import DocumentType, KIID
from kipy.proto.common.types import base_types_pb2 as bt
from kipy.proto.common.types import enums_pb2 as en
from kipy.proto.common.commands import editor_commands_pb2 as ec
from kipy.proto.schematic import schematic_types_pb2 as st
from kipy.util import pack_any

PROC = sys.argv[1] if len(sys.argv) > 1 else "eeschema.exe"
DOCTYPE = DocumentType.DOCTYPE_SCHEMATIC

def alive():
    out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {PROC}"], capture_output=True, text=True).stdout
    return PROC.lower() in out.lower()

def step(name, fn):
    print(f"\n=== {name} ===", flush=True)
    try:
        r = fn()
        print("RESULT:", str(r).replace("\n", " ")[:700])
    except Exception as e:
        print("EXCEPTION:", type(e).__name__, str(e)[:700])
    time.sleep(1.0)
    print(f"[{PROC} alive after {name}: {alive()}]", flush=True)

k = KiCad(timeout_ms=8000)
c = k._client
step("ping", k.ping)
step("GetVersion", k.get_version)
docs = []
def gd():
    global docs
    docs = list(k.get_open_documents(DOCTYPE))
    return docs
step("GetOpenDocuments(SCHEMATIC)", gd)
if not docs:
    print("no schematic docs; abort"); sys.exit(1)
doc = docs[0]

def hdr(cmd):
    cmd.header.document.CopyFrom(doc)
    return cmd

def title():
    return c.send(hdr(ec.GetTitleBlockInfo()), bt.TitleBlockInfo)
step("GetTitleBlockInfo", title)

def getitems():
    cmd = hdr(ec.GetItems()); cmd.types.append(en.KOT_SCH_LINE)
    r = c.send(cmd, ec.GetItemsResponse); return f"status={r.status} n={len(r.items)}"
step("GetItems(KOT_SCH_LINE)", getitems)

def getsyms():
    cmd = hdr(ec.GetItems()); cmd.types.append(en.KOT_SCH_SYMBOL)
    r = c.send(cmd, ec.GetItemsResponse); return f"status={r.status} n={len(r.items)}"
step("GetItems(KOT_SCH_SYMBOL)", getsyms)

def hit():
    cmd = hdr(ec.HitTest())
    cmd.id.CopyFrom(KIID(value="00000000-0000-0000-0000-000000000000"))
    cmd.position.x_nm = 0; cmd.position.y_nm = 0
    cmd.tolerance = 100000
    return c.send(cmd, ec.HitTestResponse)
step("HitTest(raw)", hit)

commit_id = None
def begin():
    global commit_id
    r = c.send(hdr(ec.BeginCommit()), ec.BeginCommitResponse)
    commit_id = r.id
    return r
step("BeginCommit", begin)

def create():
    cmd = hdr(ec.CreateItems())
    ln = st.Line()
    ln.start.x_nm = 10_000_000; ln.start.y_nm = 10_000_000
    ln.end.x_nm = 20_000_000; ln.end.y_nm = 10_000_000
    cmd.items.append(pack_any(ln))
    r = c.send(cmd, ec.CreateItemsResponse)
    return r
step("CreateItems(Line)", create)

def end():
    if commit_id is None: return "no commit"
    cmd = ec.EndCommit(); cmd.id.CopyFrom(commit_id); cmd.action = ec.CMA_COMMIT; cmd.message = "api test"
    return c.send(cmd, ec.EndCommitResponse)
step("EndCommit(CMA_COMMIT)", end)

step("GetItems(KOT_SCH_LINE) after", getitems)

def sel():
    return c.send(hdr(ec.GetSelection()), ec.SelectionResponse)
step("GetSelection", sel)

def s2s():
    r = c.send(hdr(ec.SaveDocumentToString()), ec.SavedDocumentResponse); return f"len={len(r.contents)}"
step("SaveDocumentToString", s2s)
print("\nDONE; alive:", alive())
