import pcbnew, os, sys
sp = os.path.dirname(os.path.abspath(__file__))
ver = pcbnew.GetBuildVersion().split('.')[0]
src = os.path.join(sp, "ecc83_%s" % ver, "ecc83-pp.kicad_pcb")
b = pcbnew.LoadBoard(src)
print("pcbnew", pcbnew.GetBuildVersion(), "footprints:", len(b.GetFootprints()), flush=True)
libdir = r"C:\Program Files\KiCad\%s.0\share\kicad\footprints\Resistor_SMD.pretty" % ver
fp = pcbnew.FootprintLoad(libdir, "R_0603_1608Metric")
fp.SetReference("R999")
try: fp.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(10), pcbnew.FromMM(10)))
except Exception: fp.SetPosition(pcbnew.wxPointMM(10,10))
b.Add(fp)
print("FootprintLoad+Add -> pads:", len(fp.Pads()), "footprints:", len(b.GetFootprints()), flush=True)
dsn = os.path.join(sp, "swig_%s.dsn" % ver)
ok = pcbnew.ExportSpecctraDSN(b, dsn)
print("ExportSpecctraDSN ->", ok, os.path.getsize(dsn), flush=True)
out = os.path.join(sp, "swig_out_%s.kicad_pcb" % ver); pcbnew.SaveBoard(out, b); print("saved", os.path.getsize(out), flush=True)
