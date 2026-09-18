import sys, os, pcbnew
src, ses, out = sys.argv[1], sys.argv[2], sys.argv[3]
b = pcbnew.LoadBoard(src)
def dump(b):
    for fp in b.GetFootprints():
        p = fp.GetPosition()
        try: ang = fp.GetOrientationDegrees()
        except Exception: ang = fp.GetOrientation().AsDegrees()
        print("  FP", fp.GetReference(), "pos", pcbnew.ToMM(p.x), pcbnew.ToMM(p.y), "rot", ang, "layer", fp.GetLayerName(), "locked", fp.IsLocked())
    for t in b.GetTracks():
        s, e = t.GetStart(), t.GetEnd()
        if t.GetClass() == "PCB_VIA":
            try:
                w = pcbnew.ToMM(t.GetWidth(pcbnew.F_Cu))
            except TypeError:
                w = pcbnew.ToMM(t.GetWidth())
            print("   VIA", (pcbnew.ToMM(s.x), pcbnew.ToMM(s.y)), "dia", w, "net", t.GetNetname(), "locked", t.IsLocked())
        else:
            print("   TRACK", t.GetLayerName(), (pcbnew.ToMM(s.x), pcbnew.ToMM(s.y)), (pcbnew.ToMM(e.x), pcbnew.ToMM(e.y)), "w", pcbnew.ToMM(t.GetWidth()), "net", t.GetNetname(), "locked", t.IsLocked())
print("version", pcbnew.GetBuildVersion())
print("BEFORE:"); dump(b)
try:
    r = pcbnew.ImportSpecctraSES(b, ses)
    print("ImportSpecctraSES returned:", r)
except Exception as ex:
    print("ImportSpecctraSES EXCEPTION:", type(ex).__name__, ex)
print("AFTER:"); dump(b)
pcbnew.SaveBoard(out, b)
print("saved", os.path.exists(out))
