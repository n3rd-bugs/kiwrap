import sys, subprocess, time, traceback
from kipy import KiCad
from kipy.client import KiCadClient
from kipy.errors import ApiError
from kipy.proto.common.types import DocumentType, KIID
from kipy.proto.common.types import base_types_pb2
from kipy.proto.common.commands import editor_commands_pb2 as ec
from kipy.proto.common.commands import base_commands_pb2 as bc
from kipy.geometry import Vector2
import kipy.common_types as _ct
if not hasattr(_ct, "PageSettings"): _ct.PageSettings = _ct.Wrapper  # kipy 0.8.0 wheel bug workaround
from kipy.schematic import Schematic
from kipy.schematic_types import SchematicLine

PROC = sys.argv[1] if len(sys.argv) > 1 else "eeschema.exe"

def alive():
    out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {PROC}"], capture_output=True, text=True).stdout
    return PROC.lower() in out.lower()

def step(name, fn):
    print(f"\n=== {name} ===", flush=True)
    try:
        r = fn()
        print("RESULT:", str(r)[:600])
    except Exception as e:
        print("EXCEPTION:", type(e).__name__, str(e)[:600])
    time.sleep(1.0)
    print(f"[{PROC} alive after {name}: {alive()}]", flush=True)

k = KiCad(timeout_ms=8000)
step("ping", k.ping)
step("get_version", k.get_version)
docs = []
def gd():
    global docs
    docs = list(k.get_open_documents(DocumentType.DOCTYPE_SCHEMATIC))
    return docs
step("GetOpenDocuments(SCHEMATIC)", gd)
if not docs:
    print("no schematic docs; abort"); sys.exit(1)
sch = Schematic(k._client, docs[0])

step("GetTitleBlockInfo", sch.get_title_block)
step("GetItems(lines)", lambda: len(sch.get_lines()))
step("GetItems(symbols)", lambda: len(sch.get_symbols()))

def hit():
    cmd = ec.HitTest()
    cmd.header.document.CopyFrom(docs[0])
    cmd.id.CopyFrom(KIID(value="00000000-0000-0000-0000-000000000000"))
    cmd.position.x_nm = 0; cmd.position.y_nm = 0
    cmd.tolerance = 100000
    return k._client.send(cmd, ec.HitTestResponse)
step("HitTest(raw)", hit)

commit = None
def begin():
    global commit
    commit = sch.begin_commit()
    return commit
step("BeginCommit", begin)

def create():
    ln = SchematicLine()
    ln.start = Vector2.from_xy(10_000_000, 10_000_000)
    ln.end = Vector2.from_xy(20_000_000, 10_000_000)
    return sch.create_items(ln)
step("CreateItems(line)", create)

def end():
    if commit is not None:
        return sch.push_commit(commit, "api test")
    return "no commit"
step("EndCommit(push)", end)

step("GetItems(lines) after", lambda: len(sch.get_lines()))
step("GetSelection", lambda: sch.get_selection())
step("SaveDocumentToString", lambda: len(sch.get_as_string()))
print("\nDONE; alive:", alive())
