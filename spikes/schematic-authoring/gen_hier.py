"""Plain-Python generator for a 2-sheet HIERARCHICAL KiCad 10 project.
root.kicad_sch: J1 (Conn_01x02) + sheet symbol 'led_sub' (pins VIN in, LED_K out) + 2x GND + PWR_FLAG
led_sub.kicad_sch: R1 + D1 in series between hierarchical labels VIN and LED_K, local label LED_A.
Usage: python gen_hier.py <outdir>
"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gen_plain as G

def hier_label(text, p, shape, rot=0, justify="left"):
    return ('\t(hierarchical_label "%s"\n\t\t(shape %s)\n\t\t(at %s %s %s)\n\t\t(effects\n\t\t\t(font\n\t\t\t\t(size 1.27 1.27)\n\t\t\t)\n\t\t\t(justify %s)\n\t\t)\n\t\t(uuid "%s")\n\t)\n'
            % (text, shape, G.f(p[0]), G.f(p[1]), rot, justify, G.uid()))

def sheet_symbol(project, root_uuid, sheet_uuid, name, filename, at, size, pins, page):
    X, Y = at; W, H = size
    s  = '\t(sheet\n\t\t(at %s %s)\n\t\t(size %s %s)\n' % (G.f(X), G.f(Y), G.f(W), G.f(H))
    s += '\t\t(exclude_from_sim no)\n\t\t(in_bom yes)\n\t\t(on_board yes)\n\t\t(dnp no)\n'
    s += '\t\t(stroke\n\t\t\t(width 0.1524)\n\t\t\t(type solid)\n\t\t)\n\t\t(fill\n\t\t\t(color 0 0 0 0.0000)\n\t\t)\n'
    s += '\t\t(uuid "%s")\n' % sheet_uuid
    s += '\t\t(property "Sheetname" "%s"\n\t\t\t(at %s %s 0)\n\t\t\t(show_name no)\n\t\t\t(do_not_autoplace no)\n\t\t\t(effects\n\t\t\t\t(font\n\t\t\t\t\t(size 1.27 1.27)\n\t\t\t\t)\n\t\t\t\t(justify left bottom)\n\t\t\t)\n\t\t)\n' % (name, G.f(X), G.f(Y - 0.7))
    s += '\t\t(property "Sheetfile" "%s"\n\t\t\t(at %s %s 0)\n\t\t\t(show_name no)\n\t\t\t(do_not_autoplace no)\n\t\t\t(effects\n\t\t\t\t(font\n\t\t\t\t\t(size 1.27 1.27)\n\t\t\t\t)\n\t\t\t\t(justify left top)\n\t\t\t)\n\t\t)\n' % (filename, G.f(X), G.f(Y + H + 0.6))
    for pname, ptype, (px, py), prot in pins:
        s += '\t\t(pin "%s" %s\n\t\t\t(at %s %s %s)\n\t\t\t(uuid "%s")\n\t\t\t(effects\n\t\t\t\t(font\n\t\t\t\t\t(size 1.27 1.27)\n\t\t\t\t)\n\t\t\t\t(justify %s)\n\t\t\t)\n\t\t)\n' % (pname, ptype, G.f(px), G.f(py), prot, G.uid(), "right" if prot == 180 else "left")
    s += '\t\t(instances\n\t\t\t(project "%s"\n\t\t\t\t(path "/%s"\n\t\t\t\t\t(page "%s")\n\t\t\t\t)\n\t\t\t)\n\t\t)\n\t)\n' % (project, root_uuid, page)
    return s

def header(file_uuid):
    return '(kicad_sch\n\t(version 20260306)\n\t(generator "eeschema")\n\t(generator_version "10.0")\n\t(uuid "%s")\n\t(paper "A4")\n' % file_uuid

def build(outdir):
    project = os.path.basename(outdir.rstrip("/\\"))
    os.makedirs(outdir, exist_ok=True)
    root_uuid, child_file_uuid, sheet_uuid = G.uid(), G.uid(), G.uid()
    pins = {k: G.symbol_pins(*v) for k, v in {"R": ("Device", "R"), "D": ("Device", "LED"), "J": ("Connector_Generic", "Conn_01x02"),
                                                "G": ("power", "GND"), "F": ("power", "PWR_FLAG")}.items()}
    pp = lambda k, at, rot, pin: G.pin_pos_on_sheet(at, rot, pins[k][pin][:2])

    # ---------------- child sheet: path for instances = /ROOT_UUID/SHEET_SYMBOL_UUID
    child_path = "/%s/%s" % (root_uuid, sheet_uuid)
    R1_at, D1_at = (63.5, 50.8), (63.5, 63.5)
    R1_1, R1_2 = pp("R", R1_at, 0, "1"), pp("R", R1_at, 0, "2")
    D1_K, D1_A = pp("D", D1_at, 90, "1"), pp("D", D1_at, 90, "2")
    vin_lbl = (R1_1[0], R1_1[1] - 5.08)
    k_lbl = (D1_K[0], D1_K[1] + 5.08)
    c  = header(child_file_uuid)
    c += '\t(lib_symbols\n' + G.lib_symbol_for_schematic("Device", "R") + '\n' + G.lib_symbol_for_schematic("Device", "LED") + '\n\t)\n'
    c += G.wire(vin_lbl, R1_1) + G.wire(R1_2, D1_A) + G.wire(D1_K, k_lbl)
    c += hier_label("VIN", vin_lbl, "input", rot=90, justify="left")
    c += hier_label("LED_K", k_lbl, "output", rot=270, justify="right")
    c += G.label("LED_A", (R1_2[0], round((R1_2[1] + D1_A[1]) / 2, 4)))
    # symbol_instance() writes path "/<root>" - patch to the child path
    si = lambda *a, **k: G.symbol_instance(project, root_uuid, *a, **k).replace('(path "/%s"' % root_uuid, '(path "%s"' % child_path)
    c += si("Device", "R", "R1", "330R", "Resistor_SMD:R_0603_1608Metric", R1_at, 0, pins=("1", "2"), description="Resistor")
    c += si("Device", "LED", "D1", "LED", "LED_SMD:LED_0603_1608Metric", D1_at, 90, pins=("1", "2"), description="LED")
    c += '\t(embedded_fonts no)\n)\n'
    open(os.path.join(outdir, "led_sub.kicad_sch"), "w", encoding="utf-8", newline="\n").write(c)

    # ---------------- root sheet
    J1_at = (101.6, 50.8)
    J1_1, J1_2 = pp("J", J1_at, 0, "1"), pp("J", J1_at, 0, "2")   # (96.52, 50.8), (96.52, 53.34)
    sh_at, sh_size = (63.5, 45.72), (25.4, 20.32)
    pin_vin = (sh_at[0] + sh_size[0], 50.8)           # right edge, y aligned with J1.1
    pin_k = (sh_at[0] + sh_size[0], 58.42)            # right edge lower
    G1_at, G2_at = (96.52, 73.66), (91.44, 73.66)
    G1_1, G2_1 = pp("G", G1_at, 0, "1"), pp("G", G2_at, 0, "1")
    F1_at = (96.52, 63.5)
    r  = header(root_uuid)
    r += '\t(lib_symbols\n' + '\n'.join(G.lib_symbol_for_schematic(*x) for x in (("Connector_Generic", "Conn_01x02"), ("power", "GND"), ("power", "PWR_FLAG"))) + '\n\t)\n'
    r += G.wire(pin_vin, J1_1)                                   # sheet pin VIN -> J1.1 (same y)
    r += G.wire(J1_2, F1_at) + G.wire(F1_at, G1_1)                # J1.2 -> PWR_FLAG -> GND
    r += G.wire(pin_k, (91.44, 58.42)) + G.wire((91.44, 58.42), G2_1)  # sheet pin LED_K -> GND#2
    r += sheet_symbol(project, root_uuid, sheet_uuid, "led_sub", "led_sub.kicad_sch", sh_at, sh_size,
                      [("VIN", "input", pin_vin, 0), ("LED_K", "output", pin_k, 0)], page="2")
    si = lambda *a, **k: G.symbol_instance(project, root_uuid, *a, **k)
    r += si("Connector_Generic", "Conn_01x02", "J1", "Conn_01x02", "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical", J1_at, 0, pins=("1", "2"), description="conn")
    r += si("power", "GND", "#PWR01", "GND", "", G1_at, 0, pins=("1",), datasheet="", description="gnd", power=True)
    r += si("power", "GND", "#PWR02", "GND", "", G2_at, 0, pins=("1",), datasheet="", description="gnd", power=True)
    r += si("power", "PWR_FLAG", "#FLG01", "PWR_FLAG", "", F1_at, 0, pins=("1",), datasheet="", description="flag", power=True)
    r += '\t(sheet_instances\n\t\t(path "/"\n\t\t\t(page "1")\n\t\t)\n\t)\n\t(embedded_fonts no)\n)\n'
    open(os.path.join(outdir, project + ".kicad_sch"), "w", encoding="utf-8", newline="\n").write(r)
    pro = {"meta": {"filename": project + ".kicad_pro", "version": 1},
           "sheets": [[root_uuid, "Root"], [sheet_uuid, "led_sub"]]}
    json.dump(pro, open(os.path.join(outdir, project + ".kicad_pro"), "w"), indent=2)
    print("wrote", outdir, "root", root_uuid, "sheet", sheet_uuid)

if __name__ == "__main__":
    build(sys.argv[1])
