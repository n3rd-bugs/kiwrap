"""
Proof: apply a KiCad s-expression netlist (from `kicad-cli sch export netlist`) to a NEW board via SWIG pcbnew.
There is no NETLIST reader / BOARD_NETLIST_UPDATER in the SWIG module, and kicad-cli has no "update pcb from
schematic", so a wrapper must do this itself: parse netlist -> FootprintLoad each footprint -> create NETINFO_ITEMs
-> assign pad nets -> SaveBoard.  Usage:
  "C:/Program Files/KiCad/10.0/bin/python.exe" netlist_to_pcb.py <netlist.net> <out.kicad_pcb> <kicad_ver>
"""
import sys, os, re, math
import pcbnew

KVER = sys.argv[3] if len(sys.argv) > 3 else "10.0"
FP_ROOT = f"C:/Program Files/KiCad/{KVER}/share/kicad/footprints"

# --- tiny s-expression parser -------------------------------------------------
def parse_sexpr(text):
    tokens = re.findall(r'\(|\)|"(?:[^"\\]|\\.)*"|[^\s()"]+', text)
    def build(i):
        out = []
        while i < len(tokens):
            t = tokens[i]
            if t == '(':
                sub, i = build(i + 1); out.append(sub)
            elif t == ')':
                return out, i + 1
            else:
                out.append(t[1:-1].replace('\\"', '"') if t.startswith('"') else t); i += 1
        return out, i
    return build(0)[0][0]

def find(node, key):
    return [n for n in node if isinstance(n, list) and n and n[0] == key]
def first(node, key, default=None):
    r = find(node, key); return r[0] if r else default

net = parse_sexpr(open(sys.argv[1], encoding="utf-8").read())
comps = find(first(net, "components"), "comp")
nets = find(first(net, "nets"), "net")
print(f"netlist: {len(comps)} components, {len(nets)} nets")

# --- board --------------------------------------------------------------------
out_pcb = sys.argv[2]
board = pcbnew.NewBoard(out_pcb)
board.SetFileName(out_pcb)   # NewBoard leaves it empty (see findings)

# footprints: nickname:name -> load from the standard lib dir (a real wrapper would resolve via fp-lib-table)
missing = []
fps = {}
n = 0
for c in comps:
    ref = first(c, "ref")[1]; val = first(c, "value")[1]
    fpid = first(c, "footprint", [None, ""])[1]
    if not fpid or ":" not in fpid:
        missing.append((ref, fpid)); continue
    nick, name = fpid.split(":", 1)
    lib = f"{FP_ROOT}/{nick}.pretty"
    fp = pcbnew.FootprintLoad(lib, name) if os.path.isdir(lib) else None
    if fp is None:
        missing.append((ref, fpid)); continue
    fp.SetReference(ref); fp.SetValue(val)
    fp.SetFPIDAsString(fpid)
    # grid placement 20mm pitch
    col, row = n % 8, n // 8
    fp.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(10 + col * 20), pcbnew.FromMM(10 + row * 20)))
    board.Add(fp); fps[ref] = fp; n += 1
print(f"placed {n} footprints; {len(missing)} missing: {missing[:5]}")

# nets
netinfo = {}
for nn in nets:
    name = first(nn, "name")[1]
    ni = pcbnew.NETINFO_ITEM(board, name)
    board.Add(ni); netinfo[name] = ni
assigned = 0; unassigned = []
for nn in nets:
    name = first(nn, "name")[1]
    for node in find(nn, "node"):
        ref = first(node, "ref")[1]; pin = first(node, "pin")[1]
        fp = fps.get(ref)
        if not fp: unassigned.append((ref, pin)); continue
        pad = fp.FindPadByNumber(pin)
        if pad is None: unassigned.append((ref, pin)); continue
        pad.SetNet(netinfo[name]); assigned += 1
print(f"nets created: {len(netinfo)}; pad-net assignments: {assigned}; unassigned nodes: {len(unassigned)} {unassigned[:5]}")

board.BuildConnectivity()
c = board.GetConnectivity()
print("ratsnest unconnected count:", c.GetUnconnectedCount(False))
ok = pcbnew.SaveBoard(out_pcb, board, True)
print("SaveBoard ->", ok, os.path.getsize(out_pcb), "bytes")
