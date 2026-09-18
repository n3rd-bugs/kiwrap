import os, time
from kipy import KiCad
from kipy.errors import ApiError
from kipy.board_types import Track, BoardLayer
from kipy.geometry import Vector2
from kipy.util.units import from_mm
from kipy.proto.common.types import KiCadObjectType
k = KiCad(timeout_ms=8000)
print("KiCad:", k.get_version(), "| kipy built against:", k.get_api_version(), "| check_version:", k.check_version())
b = k.get_board(); print("board:", b.name, "nets:", len(b.get_nets()), "fps:", len(b.get_footprints()), "tracks:", len(b.get_tracks()))
c = b.begin_commit()
t = Track(); t.start = Vector2.from_xy_mm(10,10); t.end = Vector2.from_xy_mm(20,10); t.width = from_mm(0.5); t.layer = BoardLayer.BL_F_Cu
r = b.create_items([t]); b.push_commit(c, "kipy0.8 on kicad9")
print("track created on 9.0:", type(r[0]).__name__, "tracks now:", len(b.get_tracks()))
for name, fn in [("get_items_by_id (10.0+)", lambda: b.get_items_by_id([r[0].id])), ("get_groups (10.0+)", lambda: b.get_groups()), ("get_layer_name (9.0.8+)", lambda: b.get_layer_name(BoardLayer.BL_F_Cu)), ("get_enabled_layers (9.0.5+)", lambda: len(b.get_enabled_layers())), ("get_connected_items (10.0.1+)", lambda: b.get_connected_items([r[0]]))]:
    try: print(name, "->", fn())
    except ApiError as e: print(name, "-> ApiError:", e)
print("run_action undo ->", k.run_action("common.Interactive.undo").status, "tracks:", len(b.get_tracks()))
out = os.path.abspath("proj9/api_saved9.kicad_pcb")
try: b.save_as(out, overwrite=True, include_project=False); print("save_as ok", os.path.getsize(out))
except ApiError as e: print("save_as err", e)
b.revert(); print("DONE9")
