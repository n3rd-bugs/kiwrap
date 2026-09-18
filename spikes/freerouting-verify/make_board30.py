"""Independent 30-net 2-layer test board: SOIC-16 + TQFP-32 + 2x8 header + 6 R + 4 C.
Built with KiCad 10 bundled python (SWIG pcbnew). Optional GND zone on B.Cu."""
import sys, os
import pcbnew
from pcbnew import VECTOR2I, FromMM
outdir, share = sys.argv[1], sys.argv[2]
with_zone = len(sys.argv) > 3 and sys.argv[3] == "zone"
os.makedirs(outdir, exist_ok=True)
pcb_path = os.path.join(outdir, "b30.kicad_pcb"); dsn_path = os.path.join(outdir, "b30.dsn")
board = pcbnew.BOARD(); board.SetFileName(pcb_path)
ds = board.GetDesignSettings(); ds.SetCopperLayerCount(2)
W, H = 45, 35
def edge(x1,y1,x2,y2):
    s = pcbnew.PCB_SHAPE(board); s.SetShape(pcbnew.SHAPE_T_SEGMENT)
    s.SetStart(VECTOR2I(FromMM(x1),FromMM(y1))); s.SetEnd(VECTOR2I(FromMM(x2),FromMM(y2)))
    s.SetLayer(pcbnew.Edge_Cuts); s.SetWidth(FromMM(0.1)); board.Add(s)
edge(0,0,W,0); edge(W,0,W,H); edge(W,H,0,H); edge(0,H,0,0)
def load(lib, name, ref, x, y, rot=0, val=""):
    fp = pcbnew.FootprintLoad(os.path.join(share,"footprints",lib+".pretty"), name)
    assert fp, (lib,name)
    fp.SetReference(ref); fp.SetValue(val or name)
    fp.SetPosition(VECTOR2I(FromMM(x),FromMM(y))); fp.SetOrientationDegrees(rot)
    board.Add(fp); return fp
U1 = load("Package_QFP", "TQFP-32_7x7mm_P0.8mm", "U1", 16, 17, val="MCU")
U2 = load("Package_SO", "SOIC-16_3.9x9.9mm_P1.27mm", "U2", 33, 17, rot=90, val="DRV")
J1 = load("Connector_PinHeader_2.54mm", "PinHeader_2x08_P2.54mm_Vertical", "J1", 4, 9, rot=0)
Rs = [load("Resistor_SMD", "R_0805_2012Metric", f"R{i+1}", 8 + i*4.5, 31, rot=90, val="1k") for i in range(6)]
Cs = [load("Capacitor_SMD", "C_0603_1608Metric", f"C{i+1}", 10 + i*5, 4, rot=0, val="100n") for i in range(4)]
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
# U1 pins 1..16 <-> J1 pins 1..16  (16 nets IO1..IO16)
for i in range(1, 17):
    pad(U1, i).SetNet(net(f"IO{i}")); pad(J1, i).SetNet(net(f"IO{i}"))
# U1 pins 17..22 <-> U2 pins 1..6 (6 nets BUS1..6)
for i in range(6):
    pad(U1, 17+i).SetNet(net(f"BUS{i+1}")); pad(U2, 1+i).SetNet(net(f"BUS{i+1}"))
# U2 pins 9..14 -> R1..R6 pin1 (6 nets OUT1..6), R pin2 -> GND
for i in range(6):
    pad(U2, 9+i).SetNet(net(f"OUT{i+1}")); pad(Rs[i], 1).SetNet(net(f"OUT{i+1}")); pad(Rs[i], 2).SetNet(GND)
# power
pad(U1, 23).SetNet(VCC); pad(U1, 24).SetNet(GND); pad(U1, 31).SetNet(VCC); pad(U1, 32).SetNet(GND)
pad(U2, 8).SetNet(GND); pad(U2, 16).SetNet(VCC); pad(U2, 7).SetNet(GND); pad(U2, 15).SetNet(VCC)
for c in Cs: pad(c, 1).SetNet(VCC); pad(c, 2).SetNet(GND)
if with_zone:
    z = pcbnew.ZONE(board); z.SetLayer(pcbnew.B_Cu); z.SetNet(GND); z.SetNetCode(GND.GetNetCode())
    z.SetIsFilled(False); z.SetLocalClearance(FromMM(0.25)); z.SetMinThickness(FromMM(0.25))
    ol = z.Outline(); ol.NewOutline()
    for (x,y) in [(1,1),(W-1,1),(W-1,H-1),(1,H-1)]: ol.Append(FromMM(x),FromMM(y))
    z.SetZoneName("GNDPOUR"); board.Add(z)
board.BuildListOfNets()
pcbnew.SaveBoard(pcb_path, board)
print("saved", pcb_path, "nets:", len(nets), "footprints:", len(board.GetFootprints()), "zone:", with_zone)
print("ExportSpecctraDSN ->", pcbnew.ExportSpecctraDSN(board, dsn_path))
