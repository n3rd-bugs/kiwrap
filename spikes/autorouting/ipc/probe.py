import time, sys
from kipy import KiCad
from kipy.board_types import Pad, Footprint
k = KiCad(timeout_ms=20000)
print("KiCad version:", k.get_version(), "| kipy api version:", k.get_api_version())
b = k.get_board()
print("board:", b.name)
fps = b.get_footprints(); print("footprints:", len(fps), [f.reference_field.text.value for f in fps][:6])
print("tracks before:", len(b.get_tracks()), "vias:", len(b.get_vias()))
nets = b.get_nets(); print("nets:", len(nets))
# 1) Try KiCad's own 'Route Selected' / 'Attempt Finish (Autoroute)' actions via RunAction on a selection
pads = b.get_pads()
sig1 = [p for p in pads if p.net and p.net.name == "SIG1"]
print("SIG1 pads:", len(sig1))
b.clear_selection()
b.add_to_selection(sig1)
print("selection size:", len(b.get_selection()))
for action in ("pcbnew.InteractiveRouter.Autoroute", "pcbnew.InteractiveRouter.RouteSelected"):
    r = k.run_action(action)
    print("run_action", action, "->", r)
    time.sleep(4)
    try:
        print("  tracks now:", len(b.get_tracks()), "vias:", len(b.get_vias()))
    except Exception as e:
        print("  board query failed (editor busy?):", type(e).__name__, str(e)[:200])
        time.sleep(5)
        print("  retry tracks:", len(b.get_tracks()))
