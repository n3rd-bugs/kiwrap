import sys, os, traceback
import pcbnew
from pcbnew import VECTOR2I, FromMM

out = sys.argv[1]
fpdir = sys.argv[2]   # .../share/kicad/footprints
mode = sys.argv[3] if len(sys.argv) > 3 else "inplace"  # "inplace" or "setoutline"

print("pcbnew version:", pcbnew.GetBuildVersion(), "python", sys.version.split()[0])

board = pcbnew.NewBoard(out)
def mm(x, y): return VECTOR2I(FromMM(x), FromMM(y))

# ---- outline (Edge.Cuts) 30x20 mm
def edge(x1,y1,x2,y2):
    s = pcbnew.PCB_SHAPE(board)
    s.SetShape(pcbnew.SHAPE_T_SEGMENT)
    s.SetStart(mm(x1,y1)); s.SetEnd(mm(x2,y2))
    s.SetLayer(pcbnew.Edge_Cuts)
    s.SetWidth(FromMM(0.1))
    board.Add(s)
edge(0,0,30,0); edge(30,0,30,20); edge(30,20,0,20); edge(0,20,0,0)

# ---- nets
nets = {}
for name in ("GND", "VCC", "SIG"):
    ni = pcbnew.NETINFO_ITEM(board, name)
    board.Add(ni)
    nets[name] = ni
print("nets:", {n: nets[n].GetNetCode() for n in nets})

# ---- footprints
def place(ref, lib, name, x, y, netmap, rot=0):
    fp = pcbnew.FootprintLoad(os.path.join(fpdir, lib + ".pretty"), name)
    assert fp is not None, "FootprintLoad returned None"
    fp.SetFPID(pcbnew.LIB_ID(lib, name))
    fp.SetReference(ref)
    fp.SetValue(name)
    board.Add(fp)
    fp.SetPosition(mm(x, y))
    if rot:
        fp.SetOrientationDegrees(rot)
    for pad in fp.Pads():
        pad.SetNet(nets[netmap[pad.GetNumber()]])
    return fp
r1 = place("R1", "Resistor_SMD", "R_0805_2012Metric", 8, 10, {"1": "GND", "2": "SIG"})
r2 = place("R2", "Resistor_SMD", "R_0805_2012Metric", 20, 10, {"1": "SIG", "2": "VCC"})
j1 = place("J1", "Connector_PinHeader_2.54mm", "PinHeader_1x02_P2.54mm_Vertical", 26, 15, {"1": "VCC", "2": "GND"})
p = {}
for fp in (r1, r2, j1):
    for pad in fp.Pads():
        p[(fp.GetReference(), pad.GetNumber())] = pad.GetPosition()
        print(fp.GetReference(), pad.GetNumber(), pad.GetNetname(), pad.GetPosition(), "THT" if pad.GetAttribute()==pcbnew.PAD_ATTRIB_PTH else "SMD")

# ---- tracks
def track(a, b, net, layer=pcbnew.F_Cu, w=0.25):
    t = pcbnew.PCB_TRACK(board)
    t.SetStart(a); t.SetEnd(b); t.SetWidth(FromMM(w)); t.SetLayer(layer); t.SetNet(net)
    board.Add(t); return t
def via(pos, net):
    v = pcbnew.PCB_VIA(board)
    v.SetPosition(pos); v.SetDrill(FromMM(0.3)); v.SetWidth(FromMM(0.6))
    v.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu); v.SetNet(net)
    board.Add(v); return v

# SIG: R1.2 -> R2.1 on F.Cu (straight, same y)
track(p[("R1","2")], p[("R2","1")], nets["SIG"])
# GND: R1.1 -> track up -> via (into B.Cu GND zone); J1.2 (THT) sits in the zone
a = p[("R1","1")]; b = VECTOR2I(a.x, FromMM(5)); gv = mm(5, 5)
track(a, b, nets["GND"]); track(b, gv, nets["GND"]); via(gv, nets["GND"])
# VCC: R2.2 -> F.Cu track down -> via -> B.Cu track -> J1.1 (THT pad at (26,8))
a = p[("R2","2")]; b = VECTOR2I(a.x, FromMM(13)); vv = mm(24, 13)
track(a, b, nets["VCC"]); track(b, vv, nets["VCC"]); via(vv, nets["VCC"])
j = p[("J1","1")]
track(vv, VECTOR2I(j.x, FromMM(13)), nets["VCC"], layer=pcbnew.B_Cu)
track(VECTOR2I(j.x, FromMM(13)), j, nets["VCC"], layer=pcbnew.B_Cu)

# ---- GND zone on B.Cu covering the board
z = pcbnew.ZONE(board)
z.SetLayer(pcbnew.B_Cu)
z.SetNet(nets["GND"])
z.SetIsFilled(False)
pts = [(1,1),(29,1),(29,19),(1,19)]
if mode == "inplace":
    o = z.Outline()
    o.NewOutline()
    for x,y in pts: o.Append(FromMM(x), FromMM(y))
else:
    poly = pcbnew.SHAPE_POLY_SET()
    poly.NewOutline()
    for x,y in pts: poly.Append(FromMM(x), FromMM(y))
    z.SetOutline(poly)
z.SetLocalClearance(FromMM(0.3))
z.SetMinThickness(FromMM(0.25))
board.Add(z)

# ---- connectivity + zone fill
print("NO FILL / NO BuildConnectivity")

print("SaveBoard:", pcbnew.SaveBoard(out, board))
print("exists:", os.path.exists(out), os.path.getsize(out))
