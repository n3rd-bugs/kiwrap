import sys, os, pcbnew
out = sys.argv[1]; mode = sys.argv[2]
MM = pcbnew.FromMM
V = lambda x,y: pcbnew.VECTOR2I(MM(x),MM(y))
path = os.path.join(out, f"b4_{mode}.kicad_pcb")
b = pcbnew.NewBoard(path)
FP="C:/Program Files/KiCad/10.0/share/kicad/footprints/Resistor_SMD.pretty"
fps=[]
for i,(x,y) in enumerate([(8,10),(22,10)]):
    fp = pcbnew.FootprintLoad(FP, "R_0603_1608Metric"); fp.SetReference(f"R{i+1}"); fp.SetPosition(V(x,y)); b.Add(fp); fps.append(fp)
nets={}
for nm in ["VCC","GND","SIG"]:
    ni = pcbnew.NETINFO_ITEM(b, nm); b.Add(ni); nets[nm]=ni
assign = {("R1","1"):"VCC", ("R1","2"):"SIG", ("R2","1"):"SIG", ("R2","2"):"GND"}
for fp in b.GetFootprints():
    for pad in fp.Pads():
        pad.SetNet(nets[assign[(fp.GetReference(), pad.GetNumber())]])
v = pcbnew.PCB_VIA(b); v.SetPosition(V(15,5)); v.SetDrill(MM(0.3)); v.SetWidth(MM(0.6)); v.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu); v.SetNet(nets["GND"]); b.Add(v)
t = pcbnew.PCB_TRACK(b); t.SetStart(fps[1].Pads()[1].GetPosition() if False else V(22,10.825)); t.SetEnd(V(15,5)); t.SetWidth(MM(0.25)); t.SetLayer(pcbnew.F_Cu); t.SetNet(nets["GND"]); b.Add(t)
z = pcbnew.ZONE(b); z.SetLayer(pcbnew.B_Cu); z.SetNet(nets["GND"])
o = pcbnew.SHAPE_POLY_SET(); o.NewOutline()
for p in [V(1,1),V(29,1),V(29,19),V(1,19)]: o.Append(p)
z.SetOutline(o); b.Add(z)
if "island_never" in mode:
    z.SetIslandRemovalMode(pcbnew.ISLAND_REMOVAL_MODE_NEVER)
if "prefill_conn" in mode:
    b.BuildConnectivity(); print("connectivity built before fill", flush=True)
pcbnew.ZONE_FILLER(b).Fill(b.Zones())
print("filled area mm2 =", b.Zones()[0].GetFilledArea()/1e12, flush=True)
if "conn" in mode and "prefill_conn" not in mode:
    b.BuildConnectivity(); print("BuildConnectivity ok", flush=True)
if "getconn" in mode:
    c = b.GetConnectivity(); print("GetConnectivity ok", flush=True)
    if "uncT" in mode: print("unconnected(True)", c.GetUnconnectedCount(True), flush=True)
    if "uncF" in mode: print("unconnected(False)", c.GetUnconnectedCount(False), flush=True)
    if "rats" in mode: print("ratsnest edges", len(c.GetRatsnestForItems([])) if hasattr(c,'GetRatsnestForItems') else 'n/a', flush=True)
print("pre-save", flush=True)
ok = pcbnew.SaveBoard(path, b, True)
print("saved", ok, os.path.getsize(path), flush=True)
