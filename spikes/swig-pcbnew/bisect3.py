import sys, os, pcbnew
out = sys.argv[1]; mode = sys.argv[2]
MM = pcbnew.FromMM
V = lambda x,y: pcbnew.VECTOR2I(MM(x),MM(y))
path = os.path.join(out, f"b3_{mode}.kicad_pcb")
b = pcbnew.NewBoard(path)
fp = pcbnew.FootprintLoad("C:/Program Files/KiCad/10.0/share/kicad/footprints/Resistor_SMD.pretty", "R_0603_1608Metric")
fp.SetReference("R1"); fp.SetPosition(V(5,5)); b.Add(fp)
z = pcbnew.ZONE(b); z.SetLayer(pcbnew.B_Cu)
o = pcbnew.SHAPE_POLY_SET(); o.NewOutline()
for p in [V(1,1),V(9,1),V(9,9),V(1,9)]: o.Append(p)
z.SetOutline(o); b.Add(z)
if "fill" in mode:
    pcbnew.ZONE_FILLER(b).Fill(b.Zones()); print("filled", flush=True)
if "zidx" in mode:
    zz = b.Zones()[0]; print("zone idx ok", zz.IsFilled(), zz.GetFilledArea(), flush=True)
if "getconn" in mode:
    b.BuildConnectivity()
    c = b.GetConnectivity()
    print("conn obj", c, "thisown=", getattr(c, "thisown", "n/a"), flush=True)
    print("unconnected", c.GetUnconnectedCount(True), flush=True)
    if "delconn" in mode:
        del c
        print("deleted conn proxy", flush=True)
if "enum" in mode:
    print("enum", len(pcbnew.FootprintEnumerate("C:/Program Files/KiCad/10.0/share/kicad/footprints/Resistor_SMD.pretty")), flush=True)
print("pre-save", flush=True)
ok = pcbnew.SaveBoard(path, b, True)
print("saved", ok, os.path.getsize(path), flush=True)
