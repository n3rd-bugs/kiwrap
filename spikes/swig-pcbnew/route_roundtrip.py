"""
Headless autoroute round trip with SWIG pcbnew + Freerouting:
  build unrouted board -> ExportSpecctraDSN -> freerouting (java) -> ImportSpecctraSES -> SaveBoard -> kicad-cli drc
Usage: "C:/Program Files/KiCad/<v>/bin/python.exe" route_roundtrip.py <outdir> <kicad_ver> <java.exe> <freerouting.jar>
"""
import sys, os, subprocess, time
import pcbnew

OUT, KVER, JAVA, JAR = sys.argv[1:5]
os.makedirs(OUT, exist_ok=True)
FP_LIB = f"C:/Program Files/KiCad/{KVER}/share/kicad/footprints/Resistor_SMD.pretty"
MM = pcbnew.FromMM
V = lambda x, y: pcbnew.VECTOR2I(MM(x), MM(y))

pcb = os.path.join(OUT, "rr.kicad_pcb")
board = pcbnew.NewBoard(pcb); board.SetFileName(pcb)

# outline 40 x 30
for a, b in [((0,0),(40,0)), ((40,0),(40,30)), ((40,30),(0,30)), ((0,30),(0,0))]:
    s = pcbnew.PCB_SHAPE(board); s.SetShape(pcbnew.SHAPE_T_SEGMENT); s.SetStart(V(*a)); s.SetEnd(V(*b))
    s.SetLayer(pcbnew.Edge_Cuts); s.SetWidth(MM(0.1)); board.Add(s)

# 6 resistors in a 3x2 grid, daisy chained: N1..N7
fps = []
for i in range(6):
    fp = pcbnew.FootprintLoad(FP_LIB, "R_0603_1608Metric")
    fp.SetReference(f"R{i+1}"); fp.SetValue("1k"); fp.SetFPIDAsString("Resistor_SMD:R_0603_1608Metric")
    fp.SetPosition(V(8 + (i % 3) * 12, 8 + (i // 3) * 14))
    if i % 2: fp.SetOrientationDegrees(90)
    board.Add(fp); fps.append(fp)
nets = {}
for i in range(1, 8):
    ni = pcbnew.NETINFO_ITEM(board, f"N{i}"); board.Add(ni); nets[f"N{i}"] = ni
# chain: R1 pad1=N1, pad2=N2 ; R2 pad1=N2, pad2=N3 ; ... ; also cross links to force vias
for i, fp in enumerate(fps):
    fp.FindPadByNumber("1").SetNet(nets[f"N{i+1}"])
    fp.FindPadByNumber("2").SetNet(nets[f"N{i+2}"])
# extra: N1 also on R6 pad 2 would conflict; instead join N7 -> R1 pad 1? skip. Add a GND-less design.
board.BuildConnectivity()
print("unrouted: tracks =", len(board.GetTracks()), "unconnected =", board.GetConnectivity().GetUnconnectedCount(False), flush=True)
pcbnew.SaveBoard(os.path.join(OUT, "rr_unrouted.kicad_pcb"), board, True)

dsn = os.path.join(OUT, "rr.dsn"); ses = os.path.join(OUT, "rr.ses")
print("ExportSpecctraDSN ->", pcbnew.ExportSpecctraDSN(board, dsn), os.path.getsize(dsn), "bytes", flush=True)

t0 = time.time()
r = subprocess.run([JAVA, "-jar", JAR, "-de", dsn, "-do", ses, "-mp", "50", "-oit", "0.5"], capture_output=True, text=True, timeout=300)
print(f"freerouting exit={r.returncode} in {time.time()-t0:.1f}s; ses exists={os.path.exists(ses)}", flush=True)
for line in (r.stdout + r.stderr).splitlines()[-6:]:
    print("   ", line[:160])

ok = pcbnew.ImportSpecctraSES(board, ses)
board.BuildConnectivity()
print("ImportSpecctraSES ->", ok, "; tracks now =", len(board.GetTracks()),
      "(vias:", sum(1 for t in board.GetTracks() if t.GetClass() == "PCB_VIA"), ") unconnected =",
      board.GetConnectivity().GetUnconnectedCount(False), flush=True)
print("SaveBoard ->", pcbnew.SaveBoard(pcb, board, True), os.path.getsize(pcb), "bytes")
