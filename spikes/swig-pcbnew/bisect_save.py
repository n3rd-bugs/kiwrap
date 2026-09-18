import sys, os, pcbnew
out = sys.argv[1]; mode = sys.argv[2]
os.makedirs(out, exist_ok=True)
MM = pcbnew.FromMM
V = lambda x,y: pcbnew.VECTOR2I(MM(x),MM(y))
path = os.path.join(out, f"b_{mode}.kicad_pcb")
if mode.startswith("newboard"):
    b = pcbnew.NewBoard(path)
elif mode.startswith("empty"):
    b = pcbnew.CreateEmptyBoard()
else:
    b = pcbnew.BOARD()
print("board created", mode, flush=True)
if "fp" in mode:
    fp = pcbnew.FootprintLoad(os.environ.get("FPLIB","C:/Program Files/KiCad/10.0/share/kicad/footprints/Resistor_SMD.pretty"), "R_0603_1608Metric")
    fp.SetReference("R1"); fp.SetPosition(V(5,5)); b.Add(fp)
    print("fp added", flush=True)
if "track" in mode:
    t = pcbnew.PCB_TRACK(b); t.SetStart(V(1,1)); t.SetEnd(V(9,1)); t.SetWidth(MM(0.25)); t.SetLayer(pcbnew.F_Cu); b.Add(t)
    print("track added", flush=True)
if "zone" in mode:
    z = pcbnew.ZONE(b); z.SetLayer(pcbnew.B_Cu)
    o = pcbnew.SHAPE_POLY_SET(); o.NewOutline()
    for p in [V(1,1),V(9,1),V(9,9),V(1,9)]: o.Append(p)
    z.SetOutline(o); b.Add(z)
    print("zone added", flush=True)
    if "fill" in mode:
        pcbnew.ZONE_FILLER(b).Fill(b.Zones()); print("filled", flush=True)
if "skip" in mode:
    ok = pcbnew.SaveBoard(path, b, True)
elif "bsave" in mode:
    ok = b.Save(path)
else:
    ok = pcbnew.SaveBoard(path, b)
print("saved", ok, os.path.getsize(path), flush=True)
