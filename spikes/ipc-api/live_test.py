import os, sys, time, traceback
from kipy import KiCad
from kipy.errors import ApiError, ConnectionError
from kipy.board_types import Track, Via, BoardLayer, Footprint, FootprintInstance
from kipy.geometry import Vector2
from kipy.util.units import from_mm
from kipy.proto.common.types import DocumentType

def step(name):
    print(f"\n### {name}")

print("KICAD_API_SOCKET env:", os.environ.get("KICAD_API_SOCKET"))
print("KICAD_API_TOKEN env:", os.environ.get("KICAD_API_TOKEN"))

step("connect")
try:
    k = KiCad(timeout_ms=5000)
    print("socket path used:", k._client._socket_path)
    v = k.get_version()
    print("KiCad version:", v)
    print("kicad-python API version:", k.get_api_version())
    print("check_version:", k.check_version())
    k.ping(); print("ping ok")
    print("kicad-cli path via API:", k.get_kicad_binary_path("kicad-cli"))
    print("token now:", repr(k._client._kicad_token))
except Exception as e:
    print("CONNECT FAILED:", type(e).__name__, e); traceback.print_exc(); sys.exit(1)

step("open documents")
for dt in [DocumentType.DOCTYPE_PCB, DocumentType.DOCTYPE_SCHEMATIC, DocumentType.DOCTYPE_PROJECT, DocumentType.DOCTYPE_FOOTPRINT, DocumentType.DOCTYPE_SYMBOL]:
    try:
        docs = k.get_open_documents(dt)
        print(DocumentType.Name(dt), "->", [(d.board_filename or d.project.path or d.project.name) for d in docs] if docs else [])
    except Exception as e:
        print(DocumentType.Name(dt), "ERROR:", type(e).__name__, e)

step("board info")
b = k.get_board()
print("board name:", b.name)
print("document:", b.document)
proj = b.get_project()
print("project name/path:", proj.name, proj.path)
nets = b.get_nets(); print("nets:", len(nets), [n.name for n in nets[:8]])
fps = b.get_footprints(); print("footprints:", len(fps), [(f.reference_field.text.value, str(f.definition.id)) for f in fps[:5]])
tracks = b.get_tracks(); print("tracks:", len(tracks))
vias = b.get_vias(); print("vias:", len(vias))
zones = b.get_zones(); print("zones:", len(zones))
pads = b.get_pads(); print("pads:", len(pads))
print("enabled layers:", len(b.get_enabled_layers()), "copper count:", b.get_copper_layer_count())
print("active layer:", BoardLayer.Name(b.get_active_layer()))
from kipy.proto.board.board_commands_pb2 import BoardOriginType; print("origin (grid):", b.get_origin(BoardOriginType.BOT_GRID))
st = b.get_stackup(); print("stackup layers:", len(st.layers))
print("netclasses:", [nc.name for nc in proj.get_net_classes()])
print("title block:", b.get_title_block_info())
sel = b.get_selection(); print("selection:", len(sel))
print("text variables:", proj.get_text_variables())

step("add a track + via inside a commit")
gnd = next((n for n in nets if n.name == "GND"), nets[1] if len(nets) > 1 else nets[0])
print("using net:", gnd.name)
commit = b.begin_commit()
t = Track()
t.start = Vector2.from_xy_mm(10, 10)
t.end = Vector2.from_xy_mm(20, 10)
t.width = from_mm(0.5)
t.layer = BoardLayer.BL_F_Cu
t.net = gnd
via = Via()
via.position = Vector2.from_xy_mm(20, 10)
via.diameter = from_mm(0.8)
via.drill_diameter = from_mm(0.4)
via.net = gnd
created = b.create_items([t, via])
print("created:", [(type(c).__name__, str(c.id)) for c in created])
b.push_commit(commit, "API test: add track+via")
print("tracks now:", len(b.get_tracks()), "vias now:", len(b.get_vias()))

step("update the track")
newt = created[0]
newt.width = from_mm(1.0)
upd = b.update_items([newt])
print("update result:", [(type(u).__name__, u.width) for u in upd])

step("undo via run_action (unstable API)")
try:
    r = k.run_action("common.Control.undo")
    print("run_action undo ->", r)
    print("tracks after undo:", len(b.get_tracks()))
except Exception as e:
    print("run_action failed:", type(e).__name__, e)

step("selection ops")
try:
    b.clear_selection()
    b.add_to_selection(created)
    print("selection after add:", len(b.get_selection()))
    b.clear_selection()
except Exception as e:
    print("selection failed:", type(e).__name__, e)

step("DRC via API?")
try:
    from kipy.proto.board import board_commands_pb2
    print("board_commands with 'Drc' in name:", [n for n in board_commands_pb2.DESCRIPTOR.message_types_by_name if 'Drc' in n or 'DRC' in n])
    r = k.run_action("pcbnew.DRCTool.showDRCDialog")
    print("run_action showDRCDialog ->", r)
except Exception as e:
    print("DRC probe failed:", type(e).__name__, e)

step("refill zones")
try:
    b.refill_zones(); print("refill ok")
except Exception as e:
    print("refill failed:", type(e).__name__, e)

step("save_as copy")
out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "proj", "pic_programmer_api_saved.kicad_pcb")
try:
    b.save_as(out, overwrite=True, include_project=False)
    print("save_as ok:", out, os.path.exists(out), os.path.getsize(out) if os.path.exists(out) else None)
except Exception as e:
    print("save_as failed:", type(e).__name__, e)

step("save (in place)")
try:
    b.save(); print("save ok")
except Exception as e:
    print("save failed:", type(e).__name__, e)

step("get_as_string")
try:
    s = b.get_as_string(); print("board as string length:", len(s), "starts:", s[:60].replace("\n"," "))
except Exception as e:
    print("get_as_string failed:", type(e).__name__, e)

step("schematic doc via raw proto (expect unhandled in 10.0)")
try:
    from kipy.proto.common.commands import editor_commands_pb2
    cmd = editor_commands_pb2.GetOpenDocuments(); cmd.type = DocumentType.DOCTYPE_SCHEMATIC
    r = k._client.send(cmd, editor_commands_pb2.GetOpenDocumentsResponse)
    print("schematic docs:", r)
except Exception as e:
    print("schematic query:", type(e).__name__, e)
print("\nDONE")
