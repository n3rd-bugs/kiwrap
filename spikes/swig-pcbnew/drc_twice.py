import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pcbnew
from wx_noassert import disable_wx_asserts; disable_wx_asserts()
print("GetBoard() outside GUI:", pcbnew.GetBoard())
print("UTILS_STEP_MODEL members:", [m for m in dir(pcbnew.UTILS_STEP_MODEL) if not m.startswith('_')])
units = getattr(pcbnew, "EDA_UNITS_MM", None) or pcbnew.EDA_UNITS_MILLIMETRES
b = pcbnew.LoadBoard(sys.argv[1])
for i in range(3):
    t0=time.time(); ok = pcbnew.WriteDRCReport(b, sys.argv[2]+f".{i}.rpt", units, True); print(f"call {i}: {ok} {time.time()-t0:.2f}s", flush=True)
# modify board and re-run: add a shorting track between VCC and GND pads
t = pcbnew.PCB_TRACK(b); t.SetStart(pcbnew.VECTOR2I_MM(7.175,10)); t.SetEnd(pcbnew.VECTOR2I_MM(22,9.175)); t.SetWidth(pcbnew.FromMM(0.25)); t.SetLayer(pcbnew.F_Cu); t.SetNet(b.FindNet("VCC")); b.Add(t)
ok = pcbnew.WriteDRCReport(b, sys.argv[2]+".short.rpt", units, True); print("after adding short:", ok)
print(open(sys.argv[2]+".short.rpt", encoding="utf-8", errors="replace").read()[:900])
