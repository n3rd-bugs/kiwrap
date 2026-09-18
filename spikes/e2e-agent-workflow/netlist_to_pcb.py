# Headless "Update PCB from schematic" for KiCad 7-10 using SWIG pcbnew + kicad-cli netlist
import pcbnew, re, sys, os, json
FP_ROOT = r"C:\Program Files\KiCad\10.0\share\kicad\footprints"
net_path, out_pcb = sys.argv[1], sys.argv[2]
t = open(net_path, encoding='utf-8').read()
comps = []
for m in re.finditer(r'\(comp\s*\(ref "([^"]+)"\)\s*\(value "([^"]*)"\)\s*\(footprint "([^"]*)"\)(.*?)\n\t\t\)', t, re.S):
    ref, val, fp, rest = m.groups()
    ts = re.search(r'\(tstamps "([^"]+)"\)', rest)
    comps.append((ref, val, fp, ts.group(1) if ts else ""))
nets_sec = t[t.index('(nets'):]
nets = []
for m in re.finditer(r'\(net\s*\(code "(\d+)"\)\s*\(name "([^"]*)"\)(.*?)\n\t\t\)', nets_sec, re.S):
    nodes = re.findall(r'\(ref "([^"]+)"\)\s*\(pin "([^"]+)"\)', m.group(3))
    nets.append((int(m.group(1)), m.group(2), nodes))
print("comps", comps); print("nets", [(n[1], n[2]) for n in nets])
b = pcbnew.BOARD()
netmap = {}
for code, name, nodes in nets:
    ni = pcbnew.NETINFO_ITEM(b, name)
    b.Add(ni)
    netmap[name] = ni
x = 100.0
for ref, val, fpid, ts in comps:
    lib, name = fpid.split(":")
    fp = pcbnew.FootprintLoad(os.path.join(FP_ROOT, lib + ".pretty"), name)
    assert fp, fpid
    fp.SetReference(ref); fp.SetValue(val)
    fp.SetPath(pcbnew.KIID_PATH("/" + ts))
    fp.SetPosition(pcbnew.VECTOR2I_MM(x, 100)); x += 10
    b.Add(fp)
for code, name, nodes in nets:
    for ref, pin in nodes:
        fp = b.FindFootprintByReference(ref)
        for pad in fp.Pads():
            if pad.GetNumber() == pin:
                pad.SetNet(netmap[name])
# outline
for (x1,y1),(x2,y2) in [((90,90),(140,90)),((140,90),(140,110)),((140,110),(90,110)),((90,110),(90,90))]:
    s = pcbnew.PCB_SHAPE(b); s.SetShape(pcbnew.SHAPE_T_SEGMENT); s.SetLayer(pcbnew.Edge_Cuts)
    s.SetStart(pcbnew.VECTOR2I_MM(x1,y1)); s.SetEnd(pcbnew.VECTOR2I_MM(x2,y2)); s.SetWidth(pcbnew.FromMM(0.1)); b.Add(s)
b.GetDesignSettings().m_MinClearance = pcbnew.FromMM(0.2)
pcbnew.SaveBoard(out_pcb, b)
print("saved", out_pcb, "nets", b.GetNetCount(), "fps", len(list(b.GetFootprints())))
