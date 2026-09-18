import sys, pcbnew
pcb, ses, out = sys.argv[1], sys.argv[2], sys.argv[3]
board = pcbnew.LoadBoard(pcb)
ok = pcbnew.ImportSpecctraSES(board, ses)
tracks = list(board.GetTracks())
n_trk = sum(1 for t in tracks if t.GetClass() == "PCB_TRACK"); n_via = sum(1 for t in tracks if t.GetClass() == "PCB_VIA")
widths = sorted(set(round(pcbnew.ToMM(t.GetWidth()),3) for t in tracks if t.GetClass()=="PCB_TRACK"))
pcbnew.SaveBoard(out, board)
print("ImportSpecctraSES", ok, "segments", n_trk, "vias", n_via, "widths_mm", widths)
