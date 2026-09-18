import sys, time, subprocess
from kipy import KiCad
from kipy.errors import ApiError, ConnectionError
from kipy.proto.common.types import DocumentType, KiCadObjectType
from kipy.proto.common.commands import editor_commands_pb2 as ec
from kipy.proto.schematic import schematic_types_pb2 as st
from kipy.util import pack_any
mode = sys.argv[1]
k = KiCad(timeout_ms=8000); c = k._client
cmd = ec.GetOpenDocuments(); cmd.type = DocumentType.DOCTYPE_SCHEMATIC
doc = c.send(cmd, ec.GetOpenDocumentsResponse).documents[0]
print("doc:", doc.board_filename)
def alive():
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq eeschema.exe"], capture_output=True, text=True).stdout
    return "eeschema.exe" in out
if mode == "begincommit":
    try:
        r = c.send(ec.BeginCommit(), ec.BeginCommitResponse); print("BeginCommit ->", r)
    except (ApiError, ConnectionError) as e:
        print("BeginCommit ERR:", type(e).__name__, e)
elif mode == "createitems":
    cmd = ec.CreateItems(); cmd.header.document.CopyFrom(doc)
    ln = st.Line(); ln.start.x_nm = 50_000_000; ln.start.y_nm = 50_000_000; ln.end.x_nm = 80_000_000; ln.end.y_nm = 50_000_000
    cmd.items.append(pack_any(ln))
    try:
        r = c.send(cmd, ec.CreateItemsResponse); print("CreateItems ->", r)
    except (ApiError, ConnectionError) as e:
        print("CreateItems ERR:", type(e).__name__, e)
elif mode == "hittest":
    cmd = ec.HitTest(); cmd.header.document.CopyFrom(doc); cmd.position.x_nm = 1; cmd.position.y_nm = 1
    try:
        r = c.send(cmd, ec.HitTestResponse); print("HitTest ->", r)
    except (ApiError, ConnectionError) as e:
        print("HitTest ERR:", type(e).__name__, e)
elif mode == "savedoc":
    from google.protobuf.empty_pb2 import Empty
    cmd = ec.SaveDocument(); cmd.document.CopyFrom(doc)
    try:
        r = c.send(cmd, Empty); print("SaveDocument ->", r)
    except (ApiError, ConnectionError) as e:
        print("SaveDocument ERR:", type(e).__name__, e)
time.sleep(2)
print("eeschema alive after", mode, ":", alive())
