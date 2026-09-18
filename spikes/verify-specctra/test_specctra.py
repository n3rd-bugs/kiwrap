import sys, os, pcbnew
ver = pcbnew.GetBuildVersion()
out = sys.argv[1]
os.makedirs(out, exist_ok=True)
V = lambda x,y: pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y))
b = pcbnew.BOARD()
gnd = pcbnew.NETINFO_ITEM(b, "GND"); b.Add(gnd)
fplib = os.path.join(os.path.dirname(pcbnew.__file__), "..","..","..","share","kicad","footprints","Resistor_SMD.pretty")
fp = pcbnew.FootprintLoad(os.path.abspath(fplib), "R_0603_1608Metric")
fp.SetReference("R1"); fp.SetPosition(V(50,50)); b.Add(fp)
for p in fp.Pads(): p.SetNet(gnd)
# board outline
for (x1,y1,x2,y2) in [(0,0,100,0),(100,0,100,100),(100,100,0,100),(0,100,0,0)]:
    s = pcbnew.PCB_SHAPE(b); s.SetShape(pcbnew.SHAPE_T_SEGMENT); s.SetStart(V(x1,y1)); s.SetEnd(V(x2,y2)); s.SetLayer(pcbnew.Edge_Cuts); b.Add(s)
def track(x1,y1,x2,y2,locked):
    t = pcbnew.PCB_TRACK(b); t.SetStart(V(x1,y1)); t.SetEnd(V(x2,y2)); t.SetWidth(pcbnew.FromMM(0.25)); t.SetLayer(pcbnew.F_Cu); t.SetNet(gnd); t.SetLocked(locked); b.Add(t); return t
track(10,10,20,10,False); track(30,30,40,30,True)
v = pcbnew.PCB_VIA(b); v.SetPosition(V(20,10)); v.SetDrill(pcbnew.FromMM(0.3))
try: v.SetWidth(pcbnew.FromMM(0.6))
except TypeError: v.SetWidth(pcbnew.PADSTACK.ALL_LAYERS, pcbnew.FromMM(0.6))
v.SetNet(gnd); v.SetLocked(False); b.Add(v)
brd = os.path.join(out, "t.kicad_pcb"); pcbnew.SaveBoard(brd, b)
b = pcbnew.LoadBoard(brd)
dsn = os.path.join(out, "t.dsn")
ok = pcbnew.ExportSpecctraDSN(b, dsn)
print(ver, "ExportSpecctraDSN(board,path) ->", ok, "file:", os.path.exists(dsn), os.path.getsize(dsn) if os.path.exists(dsn) else -1)
ses = os.path.join(out, "t.ses")
open(ses,"w").write('''(session "t.ses"
  (base_design "t.dsn")
  (placement (resolution um 10)
    (component "Resistor_SMD:R_0603_1608Metric" (place R1 700000 -800000 back 90)))
  (was_is)
  (routes (resolution um 10) (parser (host_cad "KiCad's Pcbnew"))
    (library_out
      (padstack "Via[0-1]_600:300_um" (shape (circle F.Cu 6000 0 0)) (shape (circle B.Cu 6000 0 0)) (attach off)))
    (network_out
      (net GND
        (wire (path F.Cu 2500 100000 -100000 300000 -100000))
        (via "Via[0-1]_600:300_um" 300000 -100000)))))
''')
def summary(b):
    tr = [(t.GetClass(), pcbnew.ToMM(t.GetStart().x), pcbnew.ToMM(t.GetStart().y), pcbnew.ToMM(t.GetEnd().x), pcbnew.ToMM(t.GetEnd().y), t.IsLocked()) for t in b.Tracks()]
    fp = b.FindFootprintByReference("R1")
    return dict(tracks=tr, R1_pos=(pcbnew.ToMM(fp.GetPosition().x), pcbnew.ToMM(fp.GetPosition().y)), R1_layer=fp.GetLayer(), R1_orient=fp.GetOrientationDegrees())
print("before:", summary(b))
try:
    ok = pcbnew.ImportSpecctraSES(b, ses)
    print(ver, "ImportSpecctraSES(board,path) ->", ok)
    print("after:", summary(b))
except Exception as e:
    print(ver, "ImportSpecctraSES(board,path) raised", type(e).__name__, e)
