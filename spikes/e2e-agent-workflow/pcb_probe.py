import pcbnew, sys, json
print("pcbnew", pcbnew.GetBuildVersion())
b = pcbnew.LoadBoard(sys.argv[1])
fps = list(b.GetFootprints())
print("footprints", len(fps), "tracks", len(b.GetTracks()), "nets", b.GetNetCount(), "zones", b.Zones().size() if hasattr(b.Zones(),'size') else len(b.Zones()))
bb = b.GetBoardEdgesBoundingBox()
print("board bbox mm", pcbnew.ToMM(bb.GetX()), pcbnew.ToMM(bb.GetY()), pcbnew.ToMM(bb.GetWidth()), pcbnew.ToMM(bb.GetHeight()))
fp = b.FindFootprintByReference("D3")
print("D3 at", pcbnew.ToMM(fp.GetPosition().x), pcbnew.ToMM(fp.GetPosition().y), "layer", fp.GetLayerName(), "fpid", fp.GetFPIDAsString())
fp2 = b.FindFootprintByReference("D9")
# move D3 on top of D9 to provoke a courtyard/clearance DRC violation
fp.SetPosition(fp2.GetPosition())
# add a track shorting two nets across pads
t = pcbnew.PCB_TRACK(b)
t.SetStart(pcbnew.VECTOR2I_MM(100, 100)); t.SetEnd(pcbnew.VECTOR2I_MM(120, 100))
t.SetWidth(pcbnew.FromMM(0.25)); t.SetLayer(pcbnew.F_Cu); t.SetNetCode(1)
b.Add(t)
# design settings
ds = b.GetDesignSettings()
print("clearance mm", pcbnew.ToMM(ds.m_MinClearance), "track width mm", pcbnew.ToMM(ds.m_TrackMinWidth), "layers", b.GetCopperLayerCount())
print("netclasses:", [n for n in ds.m_NetSettings.GetNetclasses().keys()] if hasattr(ds.m_NetSettings,'GetNetclasses') else 'n/a')
# DRC via python?
print("has WriteDRCReport:", hasattr(pcbnew, 'WriteDRCReport'))
pcbnew.SaveBoard(sys.argv[2], b)
print("saved", sys.argv[2])
if hasattr(pcbnew, 'WriteDRCReport'):
    ok = pcbnew.WriteDRCReport(b, sys.argv[2] + ".drc.txt", pcbnew.EDA_UNITS_MM, True)
    print("WriteDRCReport ->", ok)
