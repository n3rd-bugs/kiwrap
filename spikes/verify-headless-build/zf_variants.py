import sys, os, pcbnew
mode = sys.argv[1]; out = sys.argv[2]
ver = pcbnew.GetBuildVersion()
if mode == 'newboard':
    b = pcbnew.NewBoard(out)         # creates project + board
elif mode == 'loadsaved':
    b = pcbnew.LoadBoard(out)        # load a file previously saved (zone already there)
else:
    b = pcbnew.CreateEmptyBoard(); b.SetFileName(out)
mm = pcbnew.FromMM
if mode != 'loadsaved':
    for (x1,y1,x2,y2) in [(0,0,30,0),(30,0,30,20),(30,20,0,20),(0,20,0,0)]:
        s = pcbnew.PCB_SHAPE(b); s.SetShape(pcbnew.SHAPE_T_SEGMENT); s.SetLayer(pcbnew.Edge_Cuts)
        s.SetStart(pcbnew.VECTOR2I(mm(x1),mm(y1))); s.SetEnd(pcbnew.VECTOR2I(mm(x2),mm(y2))); s.SetWidth(mm(0.1)); b.Add(s)
    net = pcbnew.NETINFO_ITEM(b, 'GND', 0); b.Add(net)
    z = pcbnew.ZONE(b); z.SetLayer(pcbnew.B_Cu); z.SetNet(net)
    o = z.Outline(); o.NewOutline()
    for (x,y) in [(1,1),(29,1),(29,19),(1,19)]: o.Append(mm(x), mm(y))
    z.SetMinThickness(mm(0.25)); z.SetIsFilled(False); b.Add(z)
    if mode == 'savefirst':
        pcbnew.SaveBoard(out, b); print(ver, mode, 'saved before fill'); sys.exit(0)
print(ver, mode, 'zones=', len(b.Zones()), flush=True)
b.BuildConnectivity()
f = pcbnew.ZONE_FILLER(b)
ok = f.Fill(b.Zones())
z = b.Zones()[0]
print(ver, mode, 'Fill', ok, 'filled', z.IsFilled(), 'area>0', z.GetFilledPolysList(pcbnew.B_Cu).Area()>0, flush=True)
pcbnew.SaveBoard(out, b); print(ver, mode, 'saved', flush=True)
