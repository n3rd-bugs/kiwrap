import time
from kipy import KiCad
from kipy.geometry import Vector2
k = KiCad(timeout_ms=20000)
b = k.get_board()
print("tracks after ESC:", len(b.get_tracks()))
fps = b.get_footprints()
before = {f.reference_field.text.value: (f.position.x/1e6, f.position.y/1e6) for f in fps}
# Move all passives + MCU off-board (x = -20mm) so autoplaceOffboard has something to do; keep J1/J2
movers = [f for f in fps if f.reference_field.text.value not in ("J1", "J2")]
for i, f in enumerate(movers):
    f.position = Vector2.from_xy(int(-20e6), int((2 + i*2) * 1e6))
b.update_items(movers)
print("moved", len(movers), "footprints off-board")
b.clear_selection()
t0 = time.time()
r = k.run_action("pcbnew.Autoplacer.autoplaceOffboard")
print("run_action autoplaceOffboard ->", str(r).strip())
time.sleep(6)
fps2 = b.get_footprints()
after = {f.reference_field.text.value: (round(f.position.x/1e6,2), round(f.position.y/1e6,2)) for f in fps2}
moved_back = sum(1 for r_, p in after.items() if p[0] > 0)
print("footprints now inside board (x>0):", moved_back, "of", len(after))
for r_ in sorted(after): print("  ", r_, before[r_], "->", after[r_])
