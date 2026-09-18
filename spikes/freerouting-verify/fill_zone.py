import sys, pcbnew
pcb = sys.argv[1]
b = pcbnew.LoadBoard(pcb)
zones = list(b.Zones()); print("zones:", [(z.GetZoneName(), b.GetLayerName(z.GetLayer()), z.GetNetname()) for z in zones])
f = pcbnew.ZONE_FILLER(b); f.Fill(b.Zones())
for z in zones: print("filled area mm2:", round(z.GetFilledArea()/1e12,2), "IsFilled", z.IsFilled())
pcbnew.SaveBoard(pcb, b)
