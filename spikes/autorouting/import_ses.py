import sys, os, pcbnew
pcb, ses, out = sys.argv[1], sys.argv[2], sys.argv[3]
board = pcbnew.LoadBoard(pcb)
print("tracks before:", len(board.GetTracks()))
ok = pcbnew.ImportSpecctraSES(board, ses)
print("ImportSpecctraSES ->", ok)
tracks = [t for t in board.GetTracks()]
n_trk = sum(1 for t in tracks if t.GetClass() == "PCB_TRACK")
n_via = sum(1 for t in tracks if t.GetClass() == "PCB_VIA")
print("tracks after:", len(tracks), "segments:", n_trk, "vias:", n_via)
for t in tracks:
    if t.GetClass() == "PCB_TRACK":
        print("  TRACK net=%s layer=%s w=%.3fmm %s->%s" % (t.GetNetname(), board.GetLayerName(t.GetLayer()), pcbnew.ToMM(t.GetWidth()), t.GetStart(), t.GetEnd()))
    else:
        print("  VIA   net=%s %s" % (t.GetNetname(), t.GetPosition()))
pcbnew.SaveBoard(out, board)
# connectivity: unrouted count
conn = board.GetConnectivity()
print("unconnected count:", conn.GetUnconnectedCount(True))
