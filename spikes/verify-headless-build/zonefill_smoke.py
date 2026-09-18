import sys, os, ctypes, glob
import pcbnew
ver = pcbnew.GetBuildVersion()
out = sys.argv[1]
# null wxTheAssertHandler via ctypes (mangled MSVC export)
dll = glob.glob(os.path.join(os.path.dirname(sys.executable), 'wxbase*u_vc*_custom.dll'))[0]
h = ctypes.WinDLL(dll)
ctypes.c_void_p.from_address(ctypes.addressof(getattr(h, '?wxTheAssertHandler@@3P6AXAEBVwxString@@H000@ZEA'))).value = 0
print(ver, 'assert handler nulled')
b = pcbnew.CreateEmptyBoard() if hasattr(pcbnew,'CreateEmptyBoard') else pcbnew.BOARD()
b.SetFileName(out)
mm = pcbnew.FromMM
# outline
for (x1,y1,x2,y2) in [(0,0,30,0),(30,0,30,20),(30,20,0,20),(0,20,0,0)]:
    s = pcbnew.PCB_SHAPE(b); s.SetShape(pcbnew.SHAPE_T_SEGMENT); s.SetLayer(pcbnew.Edge_Cuts)
    s.SetStart(pcbnew.VECTOR2I(mm(x1),mm(y1))); s.SetEnd(pcbnew.VECTOR2I(mm(x2),mm(y2))); s.SetWidth(mm(0.1)); b.Add(s)
net = pcbnew.NETINFO_ITEM(b, 'GND', 0); b.Add(net)
z = pcbnew.ZONE(b); z.SetLayer(pcbnew.B_Cu); z.SetNet(net)
o = z.Outline(); o.NewOutline()
for (x,y) in [(1,1),(29,1),(29,19),(1,19)]:
    o.Append(mm(x), mm(y))
z.SetLocalClearance(mm(0.2)) if hasattr(z,'SetLocalClearance') else None
z.SetIsFilled(False); b.Add(z)
b.BuildConnectivity()
f = pcbnew.ZONE_FILLER(b)
ok = f.Fill(b.Zones())
print(ver, 'Fill returned', ok, 'filled=', z.IsFilled(), 'area>0:', z.GetFilledPolysList(pcbnew.B_Cu).Area() > 0)
pcbnew.SaveBoard(out, b)
print(ver, 'saved', os.path.getsize(out))
