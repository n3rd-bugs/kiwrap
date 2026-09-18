import sys, os, pcbnew
out = sys.argv[1]; flags = set(sys.argv[2].split(","))
os.makedirs(out, exist_ok=True)
MM = pcbnew.FromMM
V = lambda x,y: pcbnew.VECTOR2I(MM(x),MM(y))
path = os.path.join(out, f"b2_{sys.argv[2].replace(',','-')}.kicad_pcb")
b = pcbnew.NewBoard(path) if "newboard" in flags else pcbnew.BOARD()
if "emptyboard2" in flags:
    b2 = pcbnew.CreateEmptyBoard(); del b2
if "emptyboard2keep" in flags:
    KEEP = pcbnew.CreateEmptyBoard()
if "boardctor2" in flags:
    b3 = pcbnew.BOARD(); del b3
if "edge" in flags:
    for a,bb in [((0,0),(30,0)),((30,0),(30,20)),((30,20),(0,20)),((0,20),(0,0))]:
        seg = pcbnew.PCB_SHAPE(b); seg.SetShape(pcbnew.SHAPE_T_SEGMENT); seg.SetStart(V(*a)); seg.SetEnd(V(*bb)); seg.SetLayer(pcbnew.Edge_Cuts); seg.SetWidth(MM(0.1)); b.Add(seg)
    print("edge added", flush=True)
if "edgerect" in flags:
    r = pcbnew.PCB_SHAPE(b); r.SetShape(pcbnew.SHAPE_T_RECT); r.SetStart(V(0,0)); r.SetEnd(V(30,20)); r.SetLayer(pcbnew.Edge_Cuts); r.SetWidth(MM(0.1)); b.Add(r)
    print("edgerect added", flush=True)
FP=os.environ.get("FPLIB","C:/Program Files/KiCad/10.0/share/kicad/footprints/Resistor_SMD.pretty")
fps=[]
if "fp" in flags:
    for i,(x,y) in enumerate([(8,10),(22,10)]):
        fp = pcbnew.FootprintLoad(FP, "R_0603_1608Metric")
        fp.SetReference(f"R{i+1}"); fp.SetPosition(V(x,y))
        if "fpid" in flags: fp.SetFPIDAsString("Resistor_SMD:R_0603_1608Metric")
        if "rot" in flags and i==1: fp.SetOrientationDegrees(90)
        b.Add(fp); fps.append(fp)
nets={}
if "nets" in flags:
    for nm in ["VCC","GND","SIG"]:
        ni = pcbnew.NETINFO_ITEM(b, nm); b.Add(ni); nets[nm]=ni
if "padnets" in flags:
    for fp in b.GetFootprints():
        for pad in fp.Pads():
            pad.SetNet(nets["SIG"] if pad.GetNumber()=="1" else nets["GND"])
if "track" in flags:
    t = pcbnew.PCB_TRACK(b); t.SetStart(V(1,1)); t.SetEnd(V(9,1)); t.SetWidth(MM(0.25)); t.SetLayer(pcbnew.F_Cu)
    if "nets" in flags: t.SetNet(nets["SIG"])
    b.Add(t)
if "via" in flags:
    v = pcbnew.PCB_VIA(b); v.SetPosition(V(15,5)); v.SetDrill(MM(0.3)); v.SetWidth(MM(0.6)); v.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
    if "nets" in flags: v.SetNet(nets["GND"])
    b.Add(v)
if "zone" in flags:
    z = pcbnew.ZONE(b); z.SetLayer(pcbnew.B_Cu)
    if "nets" in flags: z.SetNet(nets["GND"])
    if "zoneextra" in flags:
        z.SetAssignedPriority(0); z.SetZoneName("GND_POUR"); z.SetMinThickness(MM(0.2)); z.SetPadConnection(pcbnew.ZONE_CONNECTION_THERMAL)
    o = pcbnew.SHAPE_POLY_SET(); o.NewOutline()
    for p in [V(1,1),V(29,1),V(29,19),V(1,19)]: o.Append(p)
    z.SetOutline(o); b.Add(z)
    if "fill" in flags: pcbnew.ZONE_FILLER(b).Fill(b.Zones())
if "conn" in flags:
    b.BuildConnectivity()
print("pre-save", flush=True)
ok = pcbnew.SaveBoard(path, b, "skip" in flags)
print("saved", ok, os.path.getsize(path), flush=True)
