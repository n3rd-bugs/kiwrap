import pcbnew, os, sys
sp = os.path.dirname(os.path.abspath(__file__))
print("pcbnew", pcbnew.GetBuildVersion(), flush=True)
b = pcbnew.LoadBoard(os.path.join(sp, "ecc83_10", "ecc83-pp.kicad_pcb"))
print("loaded footprints:", len(b.GetFootprints()), flush=True)
dsn = os.path.join(sp, "swig_10.dsn")
ok = pcbnew.ExportSpecctraDSN(b, dsn)
print("ExportSpecctraDSN ->", ok, os.path.exists(dsn), os.path.getsize(dsn) if os.path.exists(dsn) else None, flush=True)
