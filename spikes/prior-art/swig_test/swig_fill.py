import pcbnew, os, time, sys
SP = os.path.dirname(os.path.abspath(__file__))
src = sys.argv[1]
b = pcbnew.LoadBoard(src)
print(pcbnew.GetBuildVersion(), "loaded zones:", len(b.Zones()), flush=True)
t=time.time(); f = pcbnew.ZONE_FILLER(b); f.Fill(b.Zones()); print("ZONE_FILLER ok", round(time.time()-t,2), "s", flush=True)
pcbnew.SaveBoard(SP + "/filled_" + pcbnew.GetBuildVersion().split('.')[0] + ".kicad_pcb", b); print("saved", flush=True)
