import pcbnew, os, time, sys
SP = os.path.dirname(os.path.abspath(__file__))
src = sys.argv[1]
b = pcbnew.LoadBoard(src)
print(pcbnew.GetBuildVersion(), "loaded", flush=True)
t=time.time(); ok = pcbnew.WriteDRCReport(b, SP + "/drc_" + pcbnew.GetBuildVersion().split('.')[0] + ".rpt", getattr(pcbnew,"EDA_UNITS_MM",getattr(pcbnew,"EDA_UNITS_MILLIMETRES",None)), True); print("WriteDRCReport ->", ok, round(time.time()-t,2), "s", flush=True)
