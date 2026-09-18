import sys, gc, pcbnew
from pcbnew import FromMM
out = sys.argv[1]
board = pcbnew.NewBoard(out)
def mkzone():
    z = pcbnew.ZONE(board)
    z.SetLayer(pcbnew.B_Cu)
    poly = pcbnew.SHAPE_POLY_SET()
    poly.NewOutline()
    for x,y in [(1,1),(29,1),(29,19),(1,19)]: poly.Append(FromMM(x), FromMM(y))
    z.SetOutline(poly)   # poly goes out of scope here
    board.Add(z)
    return z
z = mkzone()
gc.collect()
print("outline count after scope exit:", z.Outline().OutlineCount(), "vertices:", z.Outline().VertexCount() if hasattr(z.Outline(),'VertexCount') else '?')
board.BuildConnectivity()
f = pcbnew.ZONE_FILLER(board); print("fill:", f.Fill(board.Zones()))
print("save:", pcbnew.SaveBoard(out, board))
print("done")
