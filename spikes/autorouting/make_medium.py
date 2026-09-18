"""Medium test board: TQFP-32 MCU + 2x 1x10 headers + 8x 0603 R + 4x 0603 C, ~30 nets. KiCad SWIG."""
import sys, os, random
import pcbnew
from pcbnew import VECTOR2I, FromMM
outdir = sys.argv[1]; share = sys.argv[2]
os.makedirs(outdir, exist_ok=True)
pcb_path = os.path.join(outdir, "medium.kicad_pcb"); dsn_path = os.path.join(outdir, "medium.dsn")
board = pcbnew.BOARD(); board.SetFileName(pcb_path)
board.GetDesignSettings().SetCopperLayerCount(2)
W, H = 40, 30
def edge(x1,y1,x2,y2):
    s = pcbnew.PCB_SHAPE(board); s.SetShape(pcbnew.SHAPE_T_SEGMENT)
    s.SetStart(VECTOR2I(FromMM(x1),FromMM(y1))); s.SetEnd(VECTOR2I(FromMM(x2),FromMM(y2)))
    s.SetLayer(pcbnew.Edge_Cuts); s.SetWidth(FromMM(0.1)); board.Add(s)
edge(0,0,W,0); edge(W,0,W,H); edge(W,H,0,H); edge(0,H,0,0)
def load(lib, name, ref, x, y, rot=0, val=""):
    fp = pcbnew.FootprintLoad(os.path.join(share,"footprints",lib+".pretty"), name)
    assert fp, (lib,name)
    fp.SetReference(ref); fp.SetValue(val or name)
    fp.SetPosition(VECTOR2I(FromMM(x),FromMM(y)))
    fp.SetOrientationDegrees(rot)
    board.Add(fp); return fp
U1 = load("Package_QFP", "TQFP-32_7x7mm_P0.8mm", "U1", 20, 15, val="MCU")
J1 = load("Connector_PinHeader_2.54mm", "PinHeader_1x10_P2.54mm_Vertical", "J1", 3, 3.5, rot=0)
J2 = load("Connector_PinHeader_2.54mm", "PinHeader_1x10_P2.54mm_Vertical", "J2", 37, 3.5, rot=0)
Rs = [load("Resistor_SMD", "R_0603_1608Metric", f"R{i+1}", 8 + i*3.5, 27, rot=90, val="10k") for i in range(8)]
Cs = [load("Capacitor_SMD", "C_0603_1608Metric", f"C{i+1}", 13 + i*4.5, 7.5, rot=0, val="100n") for i in range(4)]
nets = {}
def net(name):
    if name not in nets:
        n = pcbnew.NETINFO_ITEM(board, name); board.Add(n); nets[name] = n
    return nets[name]
def pad(fp, num): 
    for p in fp.Pads():
        if p.GetNumber() == str(num): return p
    raise KeyError((fp.GetReference(), num))
GND, VCC = net("GND"), net("VCC")
# MCU pins 1..32
for i in range(1, 11):   pad(U1, i).SetNet(net(f"SIG{i}"));  pad(J1, i).SetNet(net(f"SIG{i}"))
for i in range(11, 21):  pad(U1, i).SetNet(net(f"SIG{i}"));  pad(J2, i-10).SetNet(net(f"SIG{i}"))
for i in range(21, 29):  r = Rs[i-21]; pad(U1, i).SetNet(net(f"RSIG{i}")); pad(r, 1).SetNet(net(f"RSIG{i}")); pad(r, 2).SetNet(GND)
pad(U1, 29).SetNet(VCC); pad(U1, 30).SetNet(GND); pad(U1, 31).SetNet(VCC); pad(U1, 32).SetNet(GND)
for c in Cs: pad(c, 1).SetNet(VCC); pad(c, 2).SetNet(GND)
board.BuildListOfNets()
pcbnew.SaveBoard(pcb_path, board)
print("saved", pcb_path, "nets:", len(nets), "footprints:", len(board.GetFootprints()))
print("ExportSpecctraDSN ->", pcbnew.ExportSpecctraDSN(board, dsn_path))
