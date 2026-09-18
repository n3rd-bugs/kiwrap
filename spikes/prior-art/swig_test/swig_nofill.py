import pcbnew, os, time, sys
SP = os.path.dirname(os.path.abspath(__file__))
b = pcbnew.LoadBoard(SP + "/ecc83-pp.kicad_pcb")
print("loaded footprints:", len(b.GetFootprints()), "tracks:", len(b.GetTracks()), "nets:", b.GetNetCount(), flush=True)
t=time.time(); ok = pcbnew.ExportSpecctraDSN(b, SP + "/ecc83.dsn"); print("ExportSpecctraDSN ->", ok, os.path.getsize(SP+"/ecc83.dsn"), "bytes", round(time.time()-t,2), "s", flush=True)
t=time.time(); ok = pcbnew.WriteDRCReport(b, SP + "/drc.rpt", pcbnew.EDA_UNITS_MM, True); print("WriteDRCReport ->", ok, round(time.time()-t,2), "s", flush=True); print(open(SP+"/drc.rpt").read()[:300], flush=True)
pcbnew.SaveBoard(SP + "/ecc83-pp-saved.kicad_pcb", b); print("saved header:", open(SP+"/ecc83-pp-saved.kicad_pcb").read(90).replace("\n"," "), flush=True)
