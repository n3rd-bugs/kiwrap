from kipy import KiCad
from kipy.proto.common.types import base_types_pb2 as bt, enums_pb2 as en
from kipy.proto.common.commands import editor_commands_pb2 as ec
from kipy.proto.schematic import schematic_types_pb2 as st
from google.protobuf.any_pb2 import Any
from google.protobuf.empty_pb2 import Empty
def step(label, fn):
    try: r = fn(); print(f"{label}: OK {str(r)[:220].replace(chr(10),' ')}"); return r
    except Exception as e: print(f"{label}: ERR {type(e).__name__}: {str(e)[:220]}"); return None
k = KiCad(timeout_ms=15000, client_name="research-probe-sch")
step("version/ping", lambda: (k.get_version(), k.ping()))
docs = step("open SCH docs", lambda: [(d.type, d.board_filename, d.project.name, d.project.path) for d in k.get_open_documents(bt.DocumentType.DOCTYPE_SCHEMATIC)])
step("open PCB docs (from eeschema)", lambda: list(k.get_open_documents(bt.DocumentType.DOCTYPE_PCB)))
doc = k.get_open_documents(bt.DocumentType.DOCTYPE_SCHEMATIC)[0]
send = lambda c, r: k._client.send(c, r)
gi = ec.GetItems(); gi.header.document.CopyFrom(doc); gi.types.append(en.KiCadObjectType.KOT_SCH_LINE)
step("GetItems(SCH_LINE)", lambda: send(gi, ec.GetItemsResponse))
gs = ec.GetSelection(); gs.header.document.CopyFrom(doc); step("GetSelection", lambda: send(gs, ec.SelectionResponse))
sd = ec.SaveDocument(); sd.document.CopyFrom(doc); step("SaveDocument", lambda: send(sd, Empty))
sds = ec.SaveDocumentToString(); sds.document.CopyFrom(doc); step("SaveDocumentToString", lambda: send(sds, ec.SavedDocumentResponse))
bc_ = ec.BeginCommit(); bcr = step("BeginCommit", lambda: send(bc_, ec.BeginCommitResponse))
ci = ec.CreateItems(); ci.header.document.CopyFrom(doc)
line = st.Line(); line.id.value = "11111111-2222-3333-4444-555555555555"; line.start.x_nm = 50_000_000; line.start.y_nm = 50_000_000; line.end.x_nm = 60_000_000; line.end.y_nm = 50_000_000
a = Any(); a.Pack(line); ci.items.append(a)
step("CreateItems(schematic Line)", lambda: send(ci, ec.CreateItemsResponse))
ci2 = ec.CreateItems(); ci2.header.document.CopyFrom(doc); t = st.Text(); t.text.text = "api test"; t.text.position.x_nm = 70_000_000; t.text.position.y_nm = 70_000_000; a2 = Any(); a2.Pack(t); ci2.items.append(a2)
step("CreateItems(schematic Text)", lambda: send(ci2, ec.CreateItemsResponse))
ci3 = ec.CreateItems(); ci3.header.document.CopyFrom(doc); lbl = st.LocalLabel(); a3 = Any(); a3.Pack(lbl); ci3.items.append(a3)
step("CreateItems(LocalLabel default)", lambda: send(ci3, ec.CreateItemsResponse))
em = ec.EndCommit(); em.action = ec.CommitAction.CMA_COMMIT; em.message = "api test"
if bcr is not None: em.id.CopyFrom(bcr.id)
step("EndCommit", lambda: send(em, ec.EndCommitResponse))
ht = ec.HitTest(); ht.header.document.CopyFrom(doc); ht.id.value = "11111111-2222-3333-4444-555555555555"; ht.position.x_nm = 55_000_000; ht.position.y_nm = 50_000_000; ht.tolerance = 100000
step("HitTest(created line)", lambda: send(ht, ec.HitTestResponse))
di = ec.DeleteItems(); di.header.document.CopyFrom(doc); kid = di.item_ids.add(); kid.value = "11111111-2222-3333-4444-555555555555"
step("DeleteItems(created line)", lambda: send(di, ec.DeleteItemsResponse))
step("run_action common.Control.zoomFitScreen", lambda: k.run_action("common.Control.zoomFitScreen"))
step("run_action eeschema.EditorControl.showERC? (name guess: eeschema.InspectionTool.runERC)", lambda: k.run_action("eeschema.InspectionTool.runERC"))
step("get_project(doc).name", lambda: k.get_project(doc).name)
step("kipy Schematic wrapper get_items", lambda: __import__("kipy.schematic", fromlist=["Schematic"]).Schematic(k._client, doc).get_lines())
