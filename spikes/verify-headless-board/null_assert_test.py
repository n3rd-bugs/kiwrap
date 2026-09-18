import sys, gc, ctypes, pcbnew, glob, os
from pcbnew import FromMM
bindir = os.path.dirname(sys.executable)
wxbase = glob.glob(os.path.join(bindir, "wxbase*_vc_x64_custom.dll"))
wxbase = [p for p in wxbase if "_net_" not in p and "_xml_" not in p][0]
dll = ctypes.WinDLL(wxbase)
sym = "?wxTheAssertHandler@@3P6AXAEBVwxString@@H000@ZEA"
h = ctypes.c_void_p.in_dll(dll, sym)
print("wxTheAssertHandler before:", hex(h.value or 0))
h.value = None
print("wxTheAssertHandler after :", h.value)
out = sys.argv[1]
board = pcbnew.NewBoard(out)
def mkzone():
    z = pcbnew.ZONE(board); z.SetLayer(pcbnew.B_Cu)
    poly = pcbnew.SHAPE_POLY_SET(); poly.NewOutline()
    for x,y in [(1,1),(29,1),(29,19),(1,19)]: poly.Append(FromMM(x), FromMM(y))
    z.SetOutline(poly); board.Add(z); return z
z = mkzone(); gc.collect()
board.BuildConnectivity()
f = pcbnew.ZONE_FILLER(board); print("fill:", f.Fill(board.Zones()))
print("save:", pcbnew.SaveBoard(out, board)); print("done")
