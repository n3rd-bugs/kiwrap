import os, sys, time, traceback
from kipy import KiCad
from kipy.errors import ApiError
from kipy.board_types import Track, Via, BoardLayer, FootprintInstance, Footprint, Net
from kipy.geometry import Vector2
from kipy.util.units import from_mm
from kipy.proto.common.types import DocumentType, KiCadObjectType
from kipy.proto.common.commands import editor_commands_pb2

def step(n): print(f"\n### {n}")
k = KiCad(timeout_ms=8000)
b = k.get_board()
print("connected to", k.get_version(), "board", b.name)

step("undo with correct action name (common.Interactive.undo)")
n0 = len(b.get_tracks())
c = b.begin_commit()
t = Track(); t.start = Vector2.from_xy_mm(30, 30); t.end = Vector2.from_xy_mm(40, 30); t.width = from_mm(0.3); t.layer = BoardLayer.BL_B_Cu
b.create_items([t]); b.push_commit(c, "api track for undo test")
n1 = len(b.get_tracks())
r = k.run_action("common.Interactive.undo")
n2 = len(b.get_tracks())
print(f"tracks before={n0} after create={n1} after undo={n2}; run_action -> {r.status}")

step("place a footprint from the library by definition id only")
try:
    c = b.begin_commit()
    fp = FootprintInstance()
    fp.definition.id.library_nickname = "Resistor_SMD"
    fp.definition.id.entry_name = "R_0805_2012Metric"
    fp.position = Vector2.from_xy_mm(50, 50)
    fp.layer = BoardLayer.BL_F_Cu
    fp.reference_field.text.value = "R999"
    res = b.create_items([fp])
    b.push_commit(c, "place fp from lib")
    newfp = res[0]
    print("created:", type(newfp).__name__, "ref:", newfp.reference_field.text.value, "pads in definition:", len(newfp.definition.pads), "def id:", str(newfp.definition.id))
    print("footprint count:", len(b.get_footprints()))
except Exception as e:
    print("lib footprint placement failed:", type(e).__name__, e)

step("clone an existing footprint (with full definition) and place it")
try:
    fps = b.get_footprints()
    src = fps[0]
    c = b.begin_commit()
    cl = src.clone()
    cl.position = Vector2.from_xy_mm(80, 80)
    cl.reference_field.text.value = "C998"
    res = b.create_items([cl])
    b.push_commit(c, "clone fp")
    print("cloned:", res[0].reference_field.text.value, "pads:", len(res[0].definition.pads), "count now:", len(b.get_footprints()))
except Exception as e:
    print("clone failed:", type(e).__name__, e)

step("move a footprint (update_items) and read back")
try:
    fps = b.get_footprints()
    f = next(x for x in fps if x.reference_field.text.value == "C998")
    f.position = Vector2.from_xy_mm(90, 90)
    f.orientation_degrees = 90
    out = b.update_items([f])
    print("moved to", out[0].position, "rot", out[0].orientation_degrees)
except Exception as e:
    print("move failed:", type(e).__name__, e)

step("create a new net")
try:
    from kipy.board_types import Net
    n = Net(); n.name = "API_NEW_NET"
    res = b.create_items([n])
    print("net create:", res, "nets now:", len(b.get_nets()), "API_NEW_NET" in [x.name for x in b.get_nets()])
except Exception as e:
    print("net create failed:", type(e).__name__, e)

step("DRC markers before")
try:
    markers = b.get_items(KiCadObjectType.KOT_PCB_MARKER)
    print("markers:", len(markers))
except Exception as e:
    print("markers failed:", type(e).__name__, e)

step("run DRC dialog action (pcbnew.DRCTool.runDRC)")
try:
    r = k.run_action("pcbnew.DRCTool.runDRC")
    print("runDRC ->", r.status)
    time.sleep(2)
    print("ping after runDRC:", k.ping())
    try:
        print("markers after:", len(b.get_items(KiCadObjectType.KOT_PCB_MARKER)))
    except Exception as e:
        print("markers query:", type(e).__name__, e)
except Exception as e:
    print("runDRC failed:", type(e).__name__, e)

step("hit test / bounding box")
try:
    tr = b.get_tracks()[0]
    print("bbox:", b.get_item_bounding_box(tr))
    print("hit:", b.hit_test(tr, tr.start))
except Exception as e:
    print("hittest failed:", type(e).__name__, e)

step("save_as (retry)")
out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "proj", "pic_programmer_api_saved.kicad_pcb")
for i in range(3):
    try:
        b.save_as(out, overwrite=True, include_project=False)
        print("save_as ok:", os.path.exists(out), os.path.getsize(out)); break
    except Exception as e:
        print(f"save_as attempt {i}:", type(e).__name__, e); time.sleep(2)

step("revert")
try:
    b.revert(); print("revert ok, footprints:", len(b.get_footprints()))
except Exception as e:
    print("revert failed:", type(e).__name__, e)
print("\nDONE2")
