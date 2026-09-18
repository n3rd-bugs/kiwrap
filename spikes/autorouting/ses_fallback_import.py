"""Fallback SES importer for KiCad versions whose SWIG ImportSpecctraSES needs the GUI frame (KiCad 7).
Parses the (routes (network_out ...)) block of a Specctra .ses and creates PCB_TRACK / PCB_VIA items.
Usage: python ses_fallback_import.py board.kicad_pcb routed.ses out.kicad_pcb
"""
import sys, re, pcbnew
from pcbnew import VECTOR2I

def parse_sexpr(text):
    tokens = re.findall(r'\(|\)|"[^"]*"|[^\s()]+', text)
    def build(i):
        out = []
        while i < len(tokens):
            t = tokens[i]
            if t == '(':
                sub, i = build(i + 1); out.append(sub)
            elif t == ')':
                return out, i + 1
            else:
                out.append(t.strip('"')); i += 1
        return out, i
    return build(0)[0]

def find(node, key):
    return [n for n in node if isinstance(n, list) and n and n[0] == key]

pcb, ses, out = sys.argv[1:4]
board = pcbnew.LoadBoard(pcb)
tree = parse_sexpr(open(ses, encoding='utf-8').read())
session = tree[0]
routes = find(session, 'routes')[0]
res = find(routes, 'resolution')[0]           # ['resolution', 'um', '10']
unit, per_unit = res[1], float(res[2])
scale = {'um': 1000.0, 'mm': 1_000_000.0, 'mil': 25400.0, 'inch': 25_400_000.0}[unit] / per_unit  # -> nm (KiCad IU)
def iu(v): return int(round(float(v) * scale))

vias_def = {}
for lo in find(routes, 'library_out'):
    for ps in find(lo, 'padstack'):
        m = re.match(r'Via\[\d+-\d+\]_(\d+):(\d+)_um', ps[1])
        if m: vias_def[ps[1]] = (int(m.group(1)) * 1000, int(m.group(2)) * 1000)

added = 0
for netout in find(routes, 'network_out'):
    for net in find(netout, 'net'):
        netname = net[1]
        ni = board.FindNet(netname)
        assert ni is not None, netname
        for wire in find(net, 'wire'):
            for path in find(wire, 'path'):
                layer = board.GetLayerID(path[1]); width = iu(path[2])
                pts = [(iu(path[i]), -iu(path[i + 1])) for i in range(3, len(path), 2)]
                for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
                    t = pcbnew.PCB_TRACK(board)
                    t.SetStart(VECTOR2I(x1, y1)); t.SetEnd(VECTOR2I(x2, y2))
                    t.SetWidth(width); t.SetLayer(layer); t.SetNet(ni)
                    board.Add(t); added += 1
        for via in find(net, 'via'):
            size, drill = vias_def.get(via[1], (600000, 300000))
            v = pcbnew.PCB_VIA(board)
            v.SetPosition(VECTOR2I(iu(via[2]), -iu(via[3])))
            v.SetWidth(size); v.SetDrill(drill); v.SetNet(ni)
            v.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
            board.Add(v); added += 1
print("items added:", added)
pcbnew.SaveBoard(out, board)
board.BuildConnectivity()
print("unconnected count:", board.GetConnectivity().GetUnconnectedCount(True))
