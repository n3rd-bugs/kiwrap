import os, sys, time
from google.protobuf.empty_pb2 import Empty
from kipy import KiCad
from kipy.errors import ApiError
from kipy.proto.common.types import DocumentType, DocumentSpecifier, KiCadObjectType
from kipy.proto.common.commands import editor_commands_pb2 as ec
from kipy.proto.schematic import schematic_types_pb2 as st
from kipy.proto.common.types import base_types_pb2 as bt
from kipy.util import pack_any

def step(n): print(f"\n### {n}")
k = KiCad(timeout_ms=8000)
print("socket", k._client._socket_path)
try:
    print("version:", k.get_version())
except ApiError as e:
    print("GetVersion ERR:", e)
try:
    k.ping(); print("ping ok")
except ApiError as e:
    print("Ping ERR:", e)
c = k._client

step("GetOpenDocuments(SCHEMATIC) with eeschema running")
cmd = ec.GetOpenDocuments(); cmd.type = DocumentType.DOCTYPE_SCHEMATIC
try:
    r = c.send(cmd, ec.GetOpenDocumentsResponse); print(r)
    docs = list(r.documents)
except ApiError as e:
    print("ERR", e); docs = []

step("GetOpenDocuments(PCB) with only eeschema running")
cmd = ec.GetOpenDocuments(); cmd.type = DocumentType.DOCTYPE_PCB
try: print(c.send(cmd, ec.GetOpenDocumentsResponse))
except ApiError as e: print("ERR", e)

if docs:
    doc = docs[0]
    step("GetItems(KOT_SCH_SYMBOL) on schematic doc")
    cmd = ec.GetItems(); cmd.header.document.CopyFrom(doc); cmd.types.append(KiCadObjectType.KOT_SCH_SYMBOL)
    try: r = c.send(cmd, ec.GetItemsResponse); print("items:", len(r.items))
    except ApiError as e: print("ERR", e)

    step("GetItems(KOT_SCH_LINE)")
    cmd = ec.GetItems(); cmd.header.document.CopyFrom(doc); cmd.types.append(KiCadObjectType.KOT_SCH_LINE)
    try: r = c.send(cmd, ec.GetItemsResponse); print("items:", len(r.items))
    except ApiError as e: print("ERR", e)

    step("GetSelection on schematic")
    cmd = ec.GetSelection(); cmd.header.document.CopyFrom(doc)
    try: r = c.send(cmd, ec.SelectionResponse); print("sel:", len(r.items))
    except ApiError as e: print("ERR", e)

    step("BeginCommit + CreateItems(schematic Line) + EndCommit")
    try:
        bc = ec.BeginCommit(); rb = c.send(bc, ec.BeginCommitResponse); print("commit id:", rb.id.value)
        cmd = ec.CreateItems(); cmd.header.document.CopyFrom(doc)
        ln = st.Line()
        ln.id.value = "11111111-2222-3333-4444-555555555555"
        ln.start.x_nm = 50_000_000; ln.start.y_nm = 50_000_000
        ln.end.x_nm = 80_000_000; ln.end.y_nm = 50_000_000
        ln.layer = st.SchematicLayer.SL_WIRE if hasattr(st.SchematicLayer, 'SL_WIRE') else 1
        cmd.items.append(pack_any(ln))
        r = c.send(cmd, ec.CreateItemsResponse)
        print("create status:", r.status, [ (x.status.code, x.status.error_message) for x in r.created_items])
        e = ec.EndCommit(); e.id.CopyFrom(rb.id); e.action = ec.CommitAction.CMA_COMMIT; e.message = "api sch line"
        print("end commit:", c.send(e, ec.EndCommitResponse))
    except ApiError as ex: print("ERR", ex)

    step("SaveDocumentToString on schematic")
    cmd = ec.SaveDocumentToString(); cmd.document.CopyFrom(doc)
    try: r = c.send(cmd, ec.SavedDocumentResponse); print("len:", len(r.contents), r.contents[:50])
    except ApiError as e: print("ERR", e)

    step("RunAction (ERC) on schematic frame")
    from kipy.proto.common.commands import editor_commands_pb2
    cmd = editor_commands_pb2.RunAction(); cmd.action = "eeschema.InspectionTool.runERC"
    try: print(c.send(cmd, editor_commands_pb2.RunActionResponse))
    except ApiError as e: print("ERR", e)

    step("SaveCopyOfDocument schematic")
    cmd = ec.SaveCopyOfDocument(); cmd.document.CopyFrom(doc); cmd.path = os.path.abspath("proj/sch_api_copy.kicad_sch"); cmd.options.overwrite = True
    try: c.send(cmd, Empty); print("saved copy exists:", os.path.exists("proj/sch_api_copy.kicad_sch"))
    except ApiError as e: print("ERR", e)
print("\nDONE SCH")
