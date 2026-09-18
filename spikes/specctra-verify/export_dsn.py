import sys, os, pcbnew
src, out = sys.argv[1], sys.argv[2]
b = pcbnew.LoadBoard(src)
print("version", pcbnew.GetBuildVersion(), "footprints", len(b.GetFootprints()), "tracks", len(b.GetTracks()))
r = pcbnew.ExportSpecctraDSN(b, out)
print("ExportSpecctraDSN returned:", r, "exists:", os.path.exists(out), "size:", os.path.getsize(out) if os.path.exists(out) else None)
