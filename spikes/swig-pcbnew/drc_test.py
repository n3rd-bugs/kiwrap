import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pcbnew
from wx_noassert import disable_wx_asserts
if os.environ.get("NOASSERT","1") == "1":
    print("disable asserts ->", disable_wx_asserts(), flush=True)
pcb = sys.argv[1]; rpt = sys.argv[2]
b = pcbnew.LoadBoard(pcb)
print("loaded", b.GetFileName(), "fp=", len(b.GetFootprints()), flush=True)
units = getattr(pcbnew, "EDA_UNITS_MM", None) or getattr(pcbnew, "EDA_UNITS_MILLIMETRES")
t0=time.time()
ok = pcbnew.WriteDRCReport(b, rpt, units, True)
print("WriteDRCReport ->", ok, f"{time.time()-t0:.2f}s", flush=True)
print(open(rpt, encoding="utf-8", errors="replace").read() if os.path.exists(rpt) else "<no report>")
