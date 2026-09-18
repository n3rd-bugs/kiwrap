"""Test how a DERIVED library symbol (Transistor_BJT:2N2219 extends Q_NPN_EBC) must be embedded in lib_symbols.
Variants: (a) raw derived text only (has (extends ...), parent missing); (b) raw derived + raw parent embedded;
(c) parent only (wrong lib_id). Validate with kicad-cli 10 netlist: does Q1 appear with 3 pins?
Usage: python exp_extends.py <outdir>
"""
import sys, os, re, subprocess, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gen_plain as G
K = r"C:\Program Files\KiCad\10.0\bin\kicad-cli.exe"
out = os.path.abspath(sys.argv[1]); os.makedirs(out, exist_ok=True)
lib = os.path.join(G.SYMDIR, "Transistor_BJT.kicad_sym")
derived = G.extract_symbol(lib, "2N2219")
parent = G.extract_symbol(lib, "Q_NPN_EBC")
print("derived has extends:", "(extends" in derived, "| derived has pins:", "(pin " in derived, "| parent pins:", derived.count("(pin "), parent.count("(pin "))
ppins = G.symbol_pins("Transistor_BJT", "Q_NPN_EBC")
print("parent pin positions:", ppins)

def make(tag, libtext):
    root = G.uid(); at = (100.0, 100.0)
    sch = '(kicad_sch\n\t(version 20260306)\n\t(generator "eeschema")\n\t(generator_version "10.0")\n\t(uuid "%s")\n\t(paper "A4")\n' % root
    sch += '\t(lib_symbols\n' + libtext + '\n\t)\n'
    for pin, (lx, ly, _) in ppins.items():
        p = G.pin_pos_on_sheet(at, 0, (lx, ly))
        dx, dy = p[0] - at[0], p[1] - at[1]
        q = (p[0] + (20 if dx > 0 else -20), p[1]) if abs(dx) >= abs(dy) else (p[0], p[1] + (20 if dy > 0 else -20))
        sch += G.wire(p, q) + G.label("N%s" % pin, q)
    sch += G.symbol_instance("t", root, "Transistor_BJT", "2N2219", "Q1", "2N2219", "Package_TO_SOT_THT:TO-39-3", at, 0, pins=tuple(ppins))
    sch += '\t(sheet_instances\n\t\t(path "/"\n\t\t\t(page "1")\n\t\t)\n\t)\n\t(embedded_fonts no)\n)\n'
    f = os.path.join(out, tag + ".kicad_sch"); open(f, "w", encoding="utf-8", newline="\n").write(sch)
    n = os.path.join(out, tag + ".net")
    r = subprocess.run([K, "sch", "export", "netlist", "-o", n, f], capture_output=True, text=True)
    txt = open(n, encoding="utf-8").read() if os.path.exists(n) else ""
    comps = re.findall(r'\(comp\s*\(ref "([^"]*)"\)', txt)
    q1nodes = re.findall(r'\(node\s*\(ref "Q1"\)\s*\(pin "([^"]*)"\)', txt)
    print("%-22s rc=%d comps=%s Q1 nodes=%s %s" % (tag, r.returncode, comps, sorted(q1nodes), (r.stdout + r.stderr).strip()[:100]))

pre = lambda name, txt: txt.replace('(symbol "%s"' % name, '(symbol "Transistor_BJT:%s"' % name, 1)
make("a_derived_only", pre("2N2219", derived))
make("b_derived_plus_parent", pre("2N2219", derived) + "\n" + pre("Q_NPN_EBC", parent))
# (b2) derived with extends pointing at the prefixed parent name
make("b2_derived_extends_prefixed", pre("2N2219", derived).replace('(extends "Q_NPN_EBC")', '(extends "Transistor_BJT:Q_NPN_EBC")') + "\n" + pre("Q_NPN_EBC", parent))
# (c) manually flattened: parent body renamed to derived name, with derived properties (approximation: take parent, rename)
flat = pre("2N2219", parent.replace('(symbol "Q_NPN_EBC"', '(symbol "2N2219"', 1).replace('"Q_NPN_EBC_', '"2N2219_'))
make("c_flattened_parent_body", flat)
