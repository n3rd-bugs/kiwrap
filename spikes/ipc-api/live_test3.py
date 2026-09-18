import os, sys, time
from kipy import KiCad
from kipy.errors import ApiError
from kipy.board_types import Track, Via, BoardLayer, FootprintInstance, Footprint, Net, Pad, Zone
from kipy.geometry import Vector2, Angle
from kipy.util.units import from_mm
from kipy.proto.common.types import KiCadObjectType

def step(n): print(f"\n### {n}")
k = KiCad(timeout_ms=8000)
b = k.get_board()

step("footprint definition detail of existing footprint C1")
fps = b.get_footprints()
c1 = next(f for f in fps if f.reference_field.text.value == "C1")
print("C1 pads:", len(c1.definition.pads), "shapes:", len(c1.definition.shapes), "texts:", len(c1.definition.texts), "id:", str(c1.definition.id).replace("\n"," "))
print("C1 orientation:", c1.orientation, "pos:", c1.position)
print("pads via get_pads() belonging to C1:", len([p for p in b.get_pads() if p.parent == c1.id]))

step("clone C1 with definition and place; check pads survive")
c = b.begin_commit()
cl = c1.clone()
cl.position = Vector2.from_xy_mm(80, 80)
cl.reference_field.text.value = "C998"
res = b.create_items([cl])
b.push_commit(c, "clone fp")
newid = res[0].id
again = b.get_items_by_id([newid])[0]
print("created C998 pads (create response):", len(res[0].definition.pads), "| pads (re-fetched):", len(again.definition.pads), "| total board pads:", len(b.get_pads()))

step("rotate via update_items (orientation)")
again.orientation = Angle.from_degrees(90)
out = b.update_items([again])
print("orientation after update:", out[0].orientation.degrees if hasattr(out[0].orientation,'degrees') else out[0].orientation)

step("place library footprint by id (definition.id only)")
c = b.begin_commit()
fp = FootprintInstance()
fp.definition.id.library_nickname = "Resistor_SMD"
fp.definition.id.entry_name = "R_0805_2012Metric"
fp.position = Vector2.from_xy_mm(50, 50)
fp.layer = BoardLayer.BL_F_Cu
fp.reference_field.text.value = "R999"
res = b.create_items([fp])
b.push_commit(c, "place fp from lib")
r999 = b.get_items_by_id([res[0].id])[0]
print("R999 re-fetched: pads:", len(r999.definition.pads), "def id:", str(r999.definition.id).replace("\n"," "), "-> library NOT auto-loaded" if len(r999.definition.pads)==0 else "-> library loaded")

step("new net by assigning unknown net name to a track")
c = b.begin_commit()
t = Track(); t.start = Vector2.from_xy_mm(60, 60); t.end = Vector2.from_xy_mm(70, 60); t.width = from_mm(0.25); t.layer = BoardLayer.BL_F_Cu
n = Net(); n.name = "API_NEW_NET"; t.net = n
res = b.create_items([t]); b.push_commit(c, "track new net")
print("track net after create:", res[0].net.name if res[0].net else None, "| API_NEW_NET in nets:", "API_NEW_NET" in [x.name for x in b.get_nets()])

step("zone creation")
try:
    from kipy.board_types import Zone
    from kipy.geometry import PolygonWithHoles, PolyLine, PolyLineNode
    c = b.begin_commit()
    z = Zone()
    z.name = "API_ZONE"
    z.layers = [BoardLayer.BL_B_Cu]
    gnd = next(x for x in b.get_nets() if x.name == "GND"); z.net = gnd
    pl = PolyLine()
    for x,y in [(100,100),(120,100),(120,120),(100,120)]:
        pl.append(PolyLineNode.from_xy(from_mm(x), from_mm(y)))
    pl.closed = True
    poly = PolygonWithHoles(); poly.outline = pl
    z.outline = poly
    res = b.create_items([z]); b.push_commit(c, "zone")
    print("zone created:", res[0].name, "zones:", len(b.get_zones()))
    b.refill_zones(); time.sleep(1)
    zz = [x for x in b.get_zones() if x.name == "API_ZONE"][0]
    print("filled polygons after refill:", {BoardLayer.Name(l): len(p) for l,p in zz.filled_polygons.items()} if zz.filled_polygons else zz.filled_polygons)
except Exception as e:
    import traceback; traceback.print_exc()

step("valid get_items types on Board (probe each KOT)")
ok, bad = [], []
for v in KiCadObjectType.DESCRIPTOR.values:
    try:
        b.get_items(v.number); ok.append(v.name)
    except ApiError as e:
        bad.append(v.name)
print("OK:", ok); print("REJECTED:", bad)

step("GetItems on schematic types via raw proto with board doc -> expect rejection (done above)")

step("revert to clean")
b.revert(); print("footprints after revert:", len(b.get_footprints()), "zones:", len(b.get_zones()))
print("\nDONE3")
