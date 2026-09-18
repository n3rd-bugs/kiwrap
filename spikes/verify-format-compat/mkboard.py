import pcbnew, sys, os
out = sys.argv[1]
b = pcbnew.BOARD()
seg = pcbnew.PCB_SHAPE(b)
seg.SetShape(pcbnew.SHAPE_T_SEGMENT)
seg.SetStart(pcbnew.VECTOR2I(0,0)); seg.SetEnd(pcbnew.VECTOR2I(10000000,0))
seg.SetLayer(pcbnew.Edge_Cuts)
b.Add(seg)
tr = pcbnew.PCB_TRACK(b)
tr.SetStart(pcbnew.VECTOR2I(1000000,1000000)); tr.SetEnd(pcbnew.VECTOR2I(5000000,1000000))
tr.SetWidth(250000); tr.SetLayer(pcbnew.F_Cu)
b.Add(tr)
pcbnew.SaveBoard(out, b)
print("saved", out, pcbnew.GetBuildVersion())
