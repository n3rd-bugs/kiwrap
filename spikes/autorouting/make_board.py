"""Create a tiny 2-footprint / 2-net board with pcbnew SWIG and export Specctra DSN.
Usage: "<kicad>/bin/python.exe" make_board.py <outdir> [kicad_share_dir]
"""
import sys, os
import pcbnew
from pcbnew import VECTOR2I, FromMM

outdir = sys.argv[1]
share = sys.argv[2] if len(sys.argv) > 2 else r"C:\Program Files\KiCad\10.0\share\kicad"
os.makedirs(outdir, exist_ok=True)
pcb_path = os.path.join(outdir, "tiny.kicad_pcb")
dsn_path = os.path.join(outdir, "tiny.dsn")

board = pcbnew.BOARD()
board.SetFileName(pcb_path)
ds = board.GetDesignSettings()
ds.SetCopperLayerCount(2)

# Board outline 20x15 mm rectangle on Edge.Cuts (Specctra export needs an outline)
def add_edge(x1, y1, x2, y2):
    seg = pcbnew.PCB_SHAPE(board)
    seg.SetShape(pcbnew.SHAPE_T_SEGMENT)
    seg.SetStart(VECTOR2I(FromMM(x1), FromMM(y1)))
    seg.SetEnd(VECTOR2I(FromMM(x2), FromMM(y2)))
    seg.SetLayer(pcbnew.Edge_Cuts)
    seg.SetWidth(FromMM(0.1))
    board.Add(seg)
add_edge(0, 0, 20, 0); add_edge(20, 0, 20, 15); add_edge(20, 15, 0, 15); add_edge(0, 15, 0, 0)

# Nets
net1 = pcbnew.NETINFO_ITEM(board, "NET1"); board.Add(net1)
net2 = pcbnew.NETINFO_ITEM(board, "NET2"); board.Add(net2)

# Footprints from the standard library
lib = os.path.join(share, "footprints", "Resistor_SMD.pretty")
fps = []
for ref, x, y in (("R1", 5, 7.5), ("R2", 15, 7.5)):
    fp = pcbnew.FootprintLoad(lib, "R_0805_2012Metric")
    assert fp is not None, "footprint load failed"
    fp.SetReference(ref)
    fp.SetValue("10k")
    fp.SetPosition(VECTOR2I(FromMM(x), FromMM(y)))
    board.Add(fp)
    fps.append(fp)

# Assign nets: pad 1 of both -> NET1, pad 2 of both -> NET2
for fp in fps:
    for pad in fp.Pads():
        pad.SetNet(net1 if pad.GetNumber() == "1" else net2)

board.BuildListOfNets()
pcbnew.SaveBoard(pcb_path, board)
print("saved", pcb_path)
ok = pcbnew.ExportSpecctraDSN(board, dsn_path)
print("ExportSpecctraDSN ->", ok, dsn_path, os.path.getsize(dsn_path) if os.path.exists(dsn_path) else "MISSING")
print("tracks before routing:", len(board.GetTracks()))
