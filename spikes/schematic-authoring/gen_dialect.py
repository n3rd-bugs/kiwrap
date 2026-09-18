"""Plain-Python generator with per-KiCad-version *dialects* (7, 8, 9, 10).
Same circuit as gen_plain.py (LED + R + Conn_01x02 + 2x GND + PWR_FLAG, labels).
The lib_symbols are copied verbatim from the *target* KiCad version's own symbol libraries,
so the embedded symbol text is always in that version's syntax.

Usage: python gen_dialect.py <outdir> <dialect 7|8|9|10>
"""
import sys, os, json, uuid, re

DIALECTS = {
    7:  dict(version=20230121, gen_version=None,  share=r"C:/Program Files/KiCad/7.0/share/kicad"),
    8:  dict(version=20231120, gen_version="8.0", share=r"C:/Program Files/KiCad/8.0/share/kicad"),
    9:  dict(version=20250114, gen_version="9.0", share=r"C:/Program Files/KiCad/9.0/share/kicad"),
    10: dict(version=20260306, gen_version="10.0", share=r"C:/Program Files/KiCad/10.0/share/kicad"),
}

def extract_symbol(lib_path, name):
    txt = open(lib_path, encoding="utf-8").read()
    i = txt.find('(symbol "%s"' % name)
    assert i >= 0, (lib_path, name)
    depth = 0; j = i
    while True:
        c = txt[j]
        if c == '(':
            depth += 1
        elif c == ')':
            depth -= 1
            if depth == 0:
                return txt[i:j + 1]
        elif c == '"':
            j += 1
            while txt[j] != '"':
                if txt[j] == '\\':
                    j += 1
                j += 1
        j += 1

def symbol_pins(raw):
    pins = {}
    for m in re.finditer(r'\(pin\s+\w+\s+\w+\s*\(at\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\)\s*\(length\s+[-\d.]+\)\s*(?:hide\s*)?\(name\s+"[^"]*".*?\(number\s+"([^"]*)"', raw, re.S):
        pins[m.group(4)] = (float(m.group(1)), float(m.group(2)))
    return pins

def pin_pos_on_sheet(sym_at, rot, lib_xy):
    lx, ly = lib_xy; X, Y = sym_at
    if rot == 0:   sx, sy = lx, -ly
    elif rot == 90:  sx, sy = -ly, -lx
    elif rot == 180: sx, sy = -lx, ly
    elif rot == 270: sx, sy = ly, lx
    return (round(X + sx, 4), round(Y + sy, 4))

def uid(): return str(uuid.uuid4())
def f(v):
    s = ("%.4f" % v).rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s

class Writer:
    def __init__(self, dialect):
        self.d = dialect
        self.cfg = DIALECTS[dialect]
        self.symdir = self.cfg["share"] + "/symbols"

    def q(self, s):  # v7 wrote uuids/generator unquoted, 8+ quoted
        return '"%s"' % s if self.d >= 8 else s

    def lib_symbol(self, libnick, name):
        raw = extract_symbol(os.path.join(self.symdir, libnick + ".kicad_sym"), name)
        return raw.replace('(symbol "%s"' % name, '(symbol "%s:%s"' % (libnick, name), 1), symbol_pins(raw)

    def prop(self, name, value, x, y, hide=False, justify=None):
        s  = '\t\t(property "%s" "%s"\n' % (name, value.replace('"', '\\"'))
        s += '\t\t\t(at %s %s 0)\n' % (f(x), f(y))
        if self.d >= 10:
            if hide: s += '\t\t\t(hide yes)\n'
            s += '\t\t\t(show_name no)\n\t\t\t(do_not_autoplace no)\n'
        s += '\t\t\t(effects\n\t\t\t\t(font\n\t\t\t\t\t(size 1.27 1.27)\n\t\t\t\t)\n'
        if justify: s += '\t\t\t\t(justify %s)\n' % justify
        if hide and self.d < 10:
            s += '\t\t\t\t(hide yes)\n' if self.d >= 8 else '\t\t\t\thide\n'
        s += '\t\t\t)\n\t\t)\n'
        return s

    def symbol_instance(self, project, root_uuid, libnick, name, ref, value, footprint, at, rot, pins, description, power=False):
        X, Y = at
        s  = '\t(symbol\n\t\t(lib_id "%s:%s")\n\t\t(at %s %s %s)\n\t\t(unit 1)\n' % (libnick, name, f(X), f(Y), rot)
        if self.d >= 10: s += '\t\t(body_style 1)\n'
        if self.d >= 8:  s += '\t\t(exclude_from_sim no)\n'
        s += '\t\t(in_bom yes)\n\t\t(on_board yes)\n'
        if self.d >= 10: s += '\t\t(in_pos_files yes)\n'
        s += '\t\t(dnp no)\n\t\t(uuid %s)\n' % self.q(uid())
        s += self.prop("Reference", ref, X + 2.54, Y - 1.27, hide=power, justify="left")
        s += self.prop("Value", value, X + 2.54, Y + 1.27, justify="left")
        s += self.prop("Footprint", footprint, X, Y, hide=True)
        s += self.prop("Datasheet", "" if self.d >= 10 else "~", X, Y, hide=True)
        if self.d >= 8:
            s += self.prop("Description", description, X, Y, hide=True)
        for p in pins:
            s += '\t\t(pin "%s"\n\t\t\t(uuid %s)\n\t\t)\n' % (p, self.q(uid()))
        s += '\t\t(instances\n\t\t\t(project "%s"\n\t\t\t\t(path "/%s"\n\t\t\t\t\t(reference "%s")\n\t\t\t\t\t(unit 1)\n\t\t\t\t)\n\t\t\t)\n\t\t)\n' % (project, root_uuid, ref)
        s += '\t)\n'
        return s

    def wire(self, p1, p2):
        return ('\t(wire\n\t\t(pts\n\t\t\t(xy %s %s) (xy %s %s)\n\t\t)\n\t\t(stroke\n\t\t\t(width 0)\n\t\t\t(type default)\n\t\t)\n\t\t(uuid %s)\n\t)\n'
                % (f(p1[0]), f(p1[1]), f(p2[0]), f(p2[1]), self.q(uid())))

    def label(self, text, p):
        return ('\t(label "%s"\n\t\t(at %s %s 0)\n\t\t(effects\n\t\t\t(font\n\t\t\t\t(size 1.27 1.27)\n\t\t\t)\n\t\t\t(justify left bottom)\n\t\t)\n\t\t(uuid %s)\n\t)\n'
                % (text, f(p[0]), f(p[1]), self.q(uid())))

    def global_label(self, text, p):
        s  = '\t(global_label "%s"\n\t\t(shape input)\n\t\t(at %s %s 0)\n' % (text, f(p[0]), f(p[1]))
        s += '\t\t(effects\n\t\t\t(font\n\t\t\t\t(size 1.27 1.27)\n\t\t\t)\n\t\t\t(justify left)\n\t\t)\n\t\t(uuid %s)\n' % self.q(uid())
        s += '\t\t(property "Intersheetrefs" "${INTERSHEET_REFS}"\n\t\t\t(at %s %s 0)\n' % (f(p[0]), f(p[1]))
        if self.d >= 10:
            s += '\t\t\t(hide yes)\n\t\t\t(show_name no)\n\t\t\t(do_not_autoplace no)\n'
        s += '\t\t\t(effects\n\t\t\t\t(font\n\t\t\t\t\t(size 1.27 1.27)\n\t\t\t\t)\n\t\t\t\t(justify left)\n'
        if self.d < 10:
            s += '\t\t\t\t(hide yes)\n' if self.d >= 8 else '\t\t\t\thide\n'
        s += '\t\t\t)\n\t\t)\n\t)\n'
        return s

    def build(self, outdir):
        project = os.path.basename(outdir.rstrip("/\\"))
        root_uuid = uid()
        os.makedirs(outdir, exist_ok=True)
        parts = {"R": ("Device", "R"), "D": ("Device", "LED"), "J": ("Connector_Generic", "Conn_01x02"),
                 "G": ("power", "GND"), "F": ("power", "PWR_FLAG")}
        libtext, libpins = {}, {}
        for k, (ln, nm) in parts.items():
            libtext[k], libpins[k] = self.lib_symbol(ln, nm)

        R1_at, R1_rot = (63.5, 50.8), 0
        D1_at, D1_rot = (63.5, 63.5), 90
        J1_at, J1_rot = (81.28, 46.99), 0
        G1_at, G2_at = (63.5, 73.66), (76.2, 73.66)
        pp = lambda k, at, rot, pin: pin_pos_on_sheet(at, rot, libpins[k][pin])
        R1_1, R1_2 = pp("R", R1_at, R1_rot, "1"), pp("R", R1_at, R1_rot, "2")
        D1_K, D1_A = pp("D", D1_at, D1_rot, "1"), pp("D", D1_at, D1_rot, "2")
        J1_1, J1_2 = pp("J", J1_at, J1_rot, "1"), pp("J", J1_at, J1_rot, "2")
        G1_1, G2_1 = pp("G", G1_at, 0, "1"), pp("G", G2_at, 0, "1")
        F1_at = (J1_2[0], 60.96)
        corner = (J1_2[0], G2_1[1])

        c = self.cfg
        if self.d >= 8:
            sch = '(kicad_sch\n\t(version %d)\n\t(generator "eeschema")\n\t(generator_version "%s")\n\t(uuid "%s")\n\t(paper "A4")\n' % (c["version"], c["gen_version"], root_uuid)
        else:
            sch = '(kicad_sch (version %d) (generator eeschema)\n\t(uuid %s)\n\t(paper "A4")\n' % (c["version"], root_uuid)
        sch += '\t(lib_symbols\n' + '\n'.join(libtext[k] for k in ("R", "D", "J", "G", "F")) + '\n\t)\n'
        sch += self.wire(J1_1, R1_1) + self.wire(R1_2, D1_A) + self.wire(D1_K, G1_1)
        sch += self.wire(J1_2, F1_at) + self.wire(F1_at, corner)
        if corner != G2_1: sch += self.wire(corner, G2_1)
        sch += self.label("LED_A", (R1_2[0], round((R1_2[1] + D1_A[1]) / 2, 4)))
        sch += self.global_label("VIN", (round((J1_1[0] + R1_1[0]) / 2, 4), J1_1[1]))
        si = lambda *a, **k: self.symbol_instance(project, root_uuid, *a, **k)
        sch += si("Device", "R", "R1", "330R", "Resistor_SMD:R_0603_1608Metric", R1_at, R1_rot, ("1", "2"), "Resistor")
        sch += si("Device", "LED", "D1", "LED", "LED_SMD:LED_0603_1608Metric", D1_at, D1_rot, ("1", "2"), "Light emitting diode")
        sch += si("Connector_Generic", "Conn_01x02", "J1", "Conn_01x02", "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical", J1_at, J1_rot, ("1", "2"), "Generic connector")
        sch += si("power", "GND", "#PWR01", "GND", "", G1_at, 0, ("1",), "Power symbol", power=True)
        sch += si("power", "GND", "#PWR02", "GND", "", G2_at, 0, ("1",), "Power symbol", power=True)
        sch += si("power", "PWR_FLAG", "#FLG01", "PWR_FLAG", "", F1_at, 0, ("1",), "Power flag", power=True)
        sch += '\t(sheet_instances\n\t\t(path "/"\n\t\t\t(page "1")\n\t\t)\n\t)\n'
        if self.d >= 9: sch += '\t(embedded_fonts no)\n'
        sch += ')\n'
        open(os.path.join(outdir, project + ".kicad_sch"), "w", encoding="utf-8", newline="\n").write(sch)
        pro = {"meta": {"filename": project + ".kicad_pro", "version": 1}, "sheets": [[root_uuid, "Root"]],
               "libraries": {"pinned_footprint_libs": [], "pinned_symbol_libs": []}, "text_variables": {}}
        json.dump(pro, open(os.path.join(outdir, project + ".kicad_pro"), "w", encoding="utf-8"), indent=2)

if __name__ == "__main__":
    Writer(int(sys.argv[2])).build(sys.argv[1])
    print("wrote", sys.argv[1], "dialect", sys.argv[2])
