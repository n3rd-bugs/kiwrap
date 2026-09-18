import sys, os, pcbnew
case = sys.argv[1]
print("version", pcbnew.GetBuildVersion(), "case", case, flush=True)
b = pcbnew.LoadBoard("test_locked.kicad_pcb")
try:
    if case == "missing": r = pcbnew.ImportSpecctraSES(b, "nonexistent.ses")
    elif case == "malformed": r = pcbnew.ImportSpecctraSES(b, "bad.ses")
    elif case == "badref": r = pcbnew.ImportSpecctraSES(b, "badref.ses")
    elif case == "nooutline":
        b2 = pcbnew.LoadBoard("test_nooutline.kicad_pcb"); r = pcbnew.ExportSpecctraDSN(b2, "nooutline.dsn")
    elif case == "unwritable": r = pcbnew.ExportSpecctraDSN(b, "Z:/nope/x.dsn")
    print("returned", r, flush=True)
except Exception as ex:
    print("EXCEPTION", type(ex).__name__, str(ex)[:300], flush=True)
print("tracks now:", len(b.GetTracks()), flush=True)
