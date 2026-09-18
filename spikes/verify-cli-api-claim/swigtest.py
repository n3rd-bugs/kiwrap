import pcbnew, os, sys, re
sp = os.path.dirname(os.path.abspath(__file__))
print("pcbnew", pcbnew.GetBuildVersion())
ver = pcbnew.GetBuildVersion().split('.')[0]
src = os.path.join(sp, "ecc83_10" if ver=="10" else ("ecc83_9" if ver=="9" else "ecc83_8"), "ecc83-pp.kicad_pcb")
b = pcbnew.LoadBoard(src)
print("loaded footprints:", len(b.GetFootprints()))
# 1. DRC
rep = os.path.join(sp, f"swig_drc_{ver}.rpt")
ok = pcbnew.WriteDRCReport(b, rep, pcbnew.EDA_UNITS_MM, True)
print("WriteDRCReport ->", ok, os.path.exists(rep), os.path.getsize(rep) if os.path.exists(rep) else None)
if os.path.exists(rep):
    txt = open(rep, errors='replace').read()
    print("  report violations lines:", [l for l in txt.splitlines() if l.startswith('[')][:3])
# 2. FootprintLoad by library path + name (nickname resolution?)
libdir = r"C:\Program Files\KiCad\%s.0\share\kicad\footprints\Resistor_SMD.pretty" % ver
fp = pcbnew.FootprintLoad(libdir, "R_0603_1608Metric")
print("FootprintLoad ->", fp is not None, fp.GetFPID().Format().wx_str() if hasattr(fp.GetFPID(),'Format') else fp.GetFPID().GetUniStringLibId())
fp.SetReference("R999"); fp.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(10), pcbnew.FromMM(10)))
b.Add(fp)
print("board footprints now:", len(b.GetFootprints()))
# can we resolve nickname via fp-lib-table in SWIG?
cands = [n for n in dir(pcbnew) if re.search(r'(?i)lib_table|LibTable|FpLib|PCB_IO_MGR|IO_MGR|FootprintLoad', n)]
print("lib-table-ish symbols:", cands)
# 3. Specctra DSN
dsn = os.path.join(sp, f"swig_{ver}.dsn")
ok = pcbnew.ExportSpecctraDSN(b, dsn)
print("ExportSpecctraDSN ->", ok, os.path.exists(dsn), os.path.getsize(dsn) if os.path.exists(dsn) else None)
# 4. netlist
print("netlist-ish symbols:", [n for n in dir(pcbnew) if re.search(r'(?i)netlist|NETLIST_READER|NETLIST_UPDATER', n)])
out = os.path.join(sp, f"swig_out_{ver}.kicad_pcb")
pcbnew.SaveBoard(out, b)
print("saved", out, os.path.getsize(out))
