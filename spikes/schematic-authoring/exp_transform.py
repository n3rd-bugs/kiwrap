"""Empirically verify KiCad's symbol placement transform (rotation 0/90/180/270 and mirror x/y)
by generating tiny schematics with one Conn_01x02 (asymmetric pins) + two labels placed exactly
where we PREDICT the pins are, then exporting the netlist and checking the label names attach.
Usage: python exp_transform.py <outdir>
"""
import sys, os, json, subprocess
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gen_plain as G

K = r"C:\Program Files\KiCad\10.0\bin\kicad-cli.exe"
out = os.path.abspath(sys.argv[1]); os.makedirs(out, exist_ok=True)
libpins = G.symbol_pins("Connector_Generic", "Conn_01x02")  # pin1 (-5.08,0) pin2 (-5.08,-2.54) lib coords
results = {}
for rot in (0, 90, 180, 270):
    for mirror in (None, "x", "y"):
        tag = "r%d_m%s" % (rot, mirror or "none")
        at = (100.0, 100.0)
        p1 = G.pin_pos_on_sheet(at, rot, libpins["1"][:2], mirror)
        p2 = G.pin_pos_on_sheet(at, rot, libpins["2"][:2], mirror)
        root = G.uid()
        sch = '(kicad_sch\n\t(version 20260306)\n\t(generator "eeschema")\n\t(generator_version "10.0")\n\t(uuid "%s")\n\t(paper "A4")\n' % root
        sch += '\t(lib_symbols\n' + G.lib_symbol_for_schematic("Connector_Generic", "Conn_01x02") + '\n\t)\n'
        # short stub wires ending on the predicted pin positions, with labels at the far end
        def stub(p, name):
            dx, dy = p[0] - at[0], p[1] - at[1]
            if abs(dx) >= abs(dy):
                q = (p[0] + (20.0 if dx > 0 else -20.0), p[1])
            else:
                q = (p[0], p[1] + (20.0 if dy > 0 else -20.0))
            return G.wire(p, q) + G.label(name, q)
        sch += stub(p1, "PIN1_%s" % tag) + stub(p2, "PIN2_%s" % tag)
        sch += G.symbol_instance("t", root, "Connector_Generic", "Conn_01x02", "J1", "Conn_01x02", "", at, rot,
                                 pins=("1", "2"), mirror=mirror)
        sch += '\t(sheet_instances\n\t\t(path "/"\n\t\t\t(page "1")\n\t\t)\n\t)\n\t(embedded_fonts no)\n)\n'
        f = os.path.join(out, tag + ".kicad_sch")
        open(f, "w", encoding="utf-8", newline="\n").write(sch)
        n = os.path.join(out, tag + ".net")
        r = subprocess.run([K, "sch", "export", "netlist", "--format", "kicadsexpr", "-o", n, f], capture_output=True, text=True)
        txt = open(n, encoding="utf-8").read() if os.path.exists(n) else ""
        ok1 = ('(name "/PIN1_%s")' % tag) in txt and ('(pin "1")' in txt.split('(name "/PIN1_%s")' % tag)[1].split("(net")[0] if ('(name "/PIN1_%s")' % tag) in txt else False)
        ok2 = ('(name "/PIN2_%s")' % tag) in txt and ('(pin "2")' in txt.split('(name "/PIN2_%s")' % tag)[1].split("(net")[0] if ('(name "/PIN2_%s")' % tag) in txt else False)
        results[tag] = {"predicted_pin1": p1, "predicted_pin2": p2, "pin1_connected": ok1, "pin2_connected": ok2}
        print("%-12s pin1@%s pin2@%s -> pin1 %s, pin2 %s" % (tag, p1, p2, "OK" if ok1 else "MISS", "OK" if ok2 else "MISS"))
json.dump(results, open(os.path.join(out, "results.json"), "w"), indent=1)
