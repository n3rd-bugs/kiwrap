"""Plain-Python (stdlib only) generator for a minimal KiCad 10 project:
LED + resistor + 2-pin connector, wires, GND power symbols, footprints assigned.

Usage: python gen_plain.py <outdir> [version_int] [generator_version]
"""
import sys, os, json, uuid, re

KICAD_SHARE = r"C:/Program Files/KiCad/10.0/share/kicad"
SYMDIR = KICAD_SHARE + "/symbols"

# ---------------------------------------------------------------- s-expr helpers
def extract_symbol(lib_path, name):
    """Return the raw text of top-level `(symbol "name" ...)` from a .kicad_sym."""
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

def lib_symbol_for_schematic(libnick, name):
    """Embed: only the top-level symbol name gets the 'LIB:' prefix; sub-units keep 'NAME_u_b'."""
    raw = extract_symbol(os.path.join(SYMDIR, libnick + ".kicad_sym"), name)
    return raw.replace('(symbol "%s"' % name, '(symbol "%s:%s"' % (libnick, name), 1)

def symbol_pins(libnick, name):
    """Parse pin number -> (x, y, angle) in LIBRARY coordinates (Y up, mm)."""
    raw = extract_symbol(os.path.join(SYMDIR, libnick + ".kicad_sym"), name)
    pins = {}
    for m in re.finditer(r'\(pin\s+\w+\s+\w+\s*\(at\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\)\s*\(length\s+[-\d.]+\)\s*\(name\s+"[^"]*".*?\(number\s+"([^"]*)"', raw, re.S):
        pins[m.group(4)] = (float(m.group(1)), float(m.group(2)), float(m.group(3)))
    return pins

def pin_pos_on_sheet(sym_at, rot, lib_xy, mirror=None):
    """KiCad TRANSFORM: rot0 (1,0,0,-1); derived from sch_symbol.cpp SetOrientation."""
    lx, ly = lib_xy
    X, Y = sym_at
    if rot == 0:
        sx, sy = lx, -ly
    elif rot == 90:
        sx, sy = -ly, -lx
    elif rot == 180:
        sx, sy = -lx, ly
    elif rot == 270:
        sx, sy = ly, lx
    else:
        raise ValueError(rot)
    if mirror == "x":
        sy = -sy
    elif mirror == "y":
        sx = -sx
    return (round(X + sx, 4), round(Y + sy, 4))

def uid():
    return str(uuid.uuid4())

def f(v):
    s = ("%.4f" % v).rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s

# ---------------------------------------------------------------- element writers
def prop(name, value, x, y, rot=0, hide=False, size=1.27, justify=None):
    s  = '\t\t(property "%s" "%s"\n' % (name, value.replace('"', '\\"'))
    s += '\t\t\t(at %s %s %s)\n' % (f(x), f(y), rot)
    s += '\t\t\t(show_name no)\n\t\t\t(do_not_autoplace no)\n'
    if hide:
        s += '\t\t\t(hide yes)\n'
    s += '\t\t\t(effects\n\t\t\t\t(font\n\t\t\t\t\t(size %s %s)\n\t\t\t\t)\n' % (f(size), f(size))
    if justify:
        s += '\t\t\t\t(justify %s)\n' % justify
    s += '\t\t\t)\n\t\t)\n'
    return s

def symbol_instance(project, root_uuid, libnick, name, ref, value, footprint, at, rot=0,
                    pins=(), datasheet="~", description="", power=False, mirror=None):
    X, Y = at
    s  = '\t(symbol\n'
    s += '\t\t(lib_id "%s:%s")\n' % (libnick, name)
    s += '\t\t(at %s %s %s)\n' % (f(X), f(Y), rot)
    if mirror:
        s += '\t\t(mirror %s)\n' % mirror
    s += '\t\t(unit 1)\n\t\t(body_style 1)\n'
    s += '\t\t(exclude_from_sim no)\n\t\t(in_bom yes)\n\t\t(on_board yes)\n\t\t(in_pos_files yes)\n\t\t(dnp no)\n'
    s += '\t\t(uuid "%s")\n' % uid()
    s += prop("Reference", ref, X + 2.54, Y - 1.27, hide=power, justify="left")
    s += prop("Value", value, X + 2.54, Y + 1.27, justify="left")
    s += prop("Footprint", footprint, X, Y, hide=True)
    s += prop("Datasheet", datasheet, X, Y, hide=True)
    s += prop("Description", description, X, Y, hide=True)
    for p in pins:
        s += '\t\t(pin "%s"\n\t\t\t(uuid "%s")\n\t\t)\n' % (p, uid())
    s += '\t\t(instances\n\t\t\t(project "%s"\n\t\t\t\t(path "/%s"\n\t\t\t\t\t(reference "%s")\n\t\t\t\t\t(unit 1)\n\t\t\t\t)\n\t\t\t)\n\t\t)\n' % (project, root_uuid, ref)
    s += '\t)\n'
    return s

def wire(p1, p2):
    return ('\t(wire\n\t\t(pts\n\t\t\t(xy %s %s) (xy %s %s)\n\t\t)\n\t\t(stroke\n\t\t\t(width 0)\n\t\t\t(type default)\n\t\t)\n\t\t(uuid "%s")\n\t)\n'
            % (f(p1[0]), f(p1[1]), f(p2[0]), f(p2[1]), uid()))

def junction(p):
    return '\t(junction\n\t\t(at %s %s)\n\t\t(diameter 0)\n\t\t(color 0 0 0 0)\n\t\t(uuid "%s")\n\t)\n' % (f(p[0]), f(p[1]), uid())

def label(text, p, rot=0):
    return ('\t(label "%s"\n\t\t(at %s %s %s)\n\t\t(effects\n\t\t\t(font\n\t\t\t\t(size 1.27 1.27)\n\t\t\t)\n\t\t\t(justify left bottom)\n\t\t)\n\t\t(uuid "%s")\n\t)\n'
            % (text, f(p[0]), f(p[1]), rot, uid()))

def global_label(text, p, rot=0, shape="input"):
    return ('\t(global_label "%s"\n\t\t(shape %s)\n\t\t(at %s %s %s)\n\t\t(fields_autoplaced yes)\n\t\t(effects\n\t\t\t(font\n\t\t\t\t(size 1.27 1.27)\n\t\t\t)\n\t\t\t(justify left)\n\t\t)\n\t\t(uuid "%s")\n'
            '\t\t(property "Intersheetrefs" "${INTERSHEET_REFS}"\n\t\t\t(at %s %s 0)\n\t\t\t(effects\n\t\t\t\t(font\n\t\t\t\t\t(size 1.27 1.27)\n\t\t\t\t)\n\t\t\t\t(justify left)\n\t\t\t\t(hide yes)\n\t\t\t)\n\t\t)\n\t)\n'
            % (text, shape, f(p[0]), f(p[1]), rot, uid(), f(p[0]), f(p[1])))

# ---------------------------------------------------------------- build
def build(outdir, version=20260101, generator_version="10.0", with_label=True, with_pro=True, pwr_flag="endpoint"):
    project = os.path.basename(outdir.rstrip("/\\"))
    root_uuid = uid()
    os.makedirs(outdir, exist_ok=True)

    parts = {
        "R":  ("Device", "R"),
        "D":  ("Device", "LED"),
        "J":  ("Connector_Generic", "Conn_01x02"),
        "G":  ("power", "GND"),
        "F":  ("power", "PWR_FLAG"),
    }
    libpins = {k: symbol_pins(*v) for k, v in parts.items()}

    # Placement (mm, 1.27 grid). Y grows downward on the sheet.
    R1_at, R1_rot = (63.5, 50.8), 0      # vertical: pin1 top, pin2 bottom
    D1_at, D1_rot = (63.5, 63.5), 90     # rot90 -> anode (pin2) top, cathode (pin1) bottom
    J1_at, J1_rot = (81.28, 46.99), 0    # pins point left
    G1_at = (63.5, 73.66)
    G2_at = (76.2, 73.66)

    def pp(key, at, rot, pin):
        return pin_pos_on_sheet(at, rot, libpins[key][pin][:2])

    R1_1, R1_2 = pp("R", R1_at, R1_rot, "1"), pp("R", R1_at, R1_rot, "2")
    D1_K, D1_A = pp("D", D1_at, D1_rot, "1"), pp("D", D1_at, D1_rot, "2")
    J1_1, J1_2 = pp("J", J1_at, J1_rot, "1"), pp("J", J1_at, J1_rot, "2")
    G1_1 = pp("G", G1_at, 0, "1")
    G2_1 = pp("G", G2_at, 0, "1")
    pinpos = dict(R1_1=R1_1, R1_2=R1_2, D1_K=D1_K, D1_A=D1_A, J1_1=J1_1, J1_2=J1_2, G1_1=G1_1, G2_1=G2_1)

    sch  = '(kicad_sch\n\t(version %d)\n\t(generator "eeschema")\n\t(generator_version "%s")\n' % (version, generator_version)
    sch += '\t(uuid "%s")\n\t(paper "A4")\n' % root_uuid
    sch += '\t(lib_symbols\n'
    for k in (("R", "D", "J", "G", "F") if pwr_flag else ("R", "D", "J", "G")):
        sch += lib_symbol_for_schematic(*parts[k]) + '\n'
    sch += '\t)\n'

    # wires
    sch += wire(J1_1, R1_1)                    # J1.1 -> R1.1 (horizontal, same y)
    sch += wire(R1_2, D1_A)                    # R1.2 -> D1.A
    sch += wire(D1_K, G1_1)                    # D1.K -> GND
    corner = (J1_2[0], G2_1[1])
    F1_at = (J1_2[0], 60.96)                   # on the vertical J1.2 -> GND#2 wire
    if pwr_flag == "midwire":
        sch += wire(J1_2, corner)              # single wire; PWR_FLAG pin lands mid-segment
    elif pwr_flag == "endpoint":
        sch += wire(J1_2, F1_at)               # wire split at the PWR_FLAG pin
        sch += wire(F1_at, corner)
    else:
        sch += wire(J1_2, corner)
    if corner != G2_1:
        sch += wire(corner, G2_1)              # -> GND #2 (only if not already aligned)
    if with_label:
        # local label placed ON the R1.2->D1.A wire, mid-segment (not at an endpoint)
        mid = (R1_2[0], round((R1_2[1] + D1_A[1]) / 2, 4))
        sch += label("LED_A", mid)
        # global label placed mid-segment on the J1.1 -> R1.1 wire
        sch += global_label("VIN", (round((J1_1[0] + R1_1[0]) / 2, 4), J1_1[1]), rot=0)

    sch += symbol_instance(project, root_uuid, "Device", "R", "R1", "330R", "Resistor_SMD:R_0603_1608Metric",
                           R1_at, R1_rot, pins=("1", "2"), description="Resistor")
    sch += symbol_instance(project, root_uuid, "Device", "LED", "D1", "LED", "LED_SMD:LED_0603_1608Metric",
                           D1_at, D1_rot, pins=("1", "2"), description="Light emitting diode")
    sch += symbol_instance(project, root_uuid, "Connector_Generic", "Conn_01x02", "J1", "Conn_01x02",
                           "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical",
                           J1_at, J1_rot, pins=("1", "2"), description="Generic connector, single row, 01x02")
    sch += symbol_instance(project, root_uuid, "power", "GND", "#PWR01", "GND", "", G1_at, 0, pins=("1",),
                           datasheet="", description="Power symbol creates a global label with name \"GND\" , ground", power=True)
    sch += symbol_instance(project, root_uuid, "power", "GND", "#PWR02", "GND", "", G2_at, 0, pins=("1",),
                           datasheet="", description="Power symbol creates a global label with name \"GND\" , ground", power=True)
    if pwr_flag:
        sch += symbol_instance(project, root_uuid, "power", "PWR_FLAG", "#FLG01", "PWR_FLAG", "", F1_at, 0, pins=("1",),
                               datasheet="", description="Special symbol for telling ERC where power comes from", power=True)
    sch += '\t(sheet_instances\n\t\t(path "/"\n\t\t\t(page "1")\n\t\t)\n\t)\n'
    sch += '\t(embedded_fonts no)\n)\n'

    open(os.path.join(outdir, project + ".kicad_sch"), "w", encoding="utf-8", newline="\n").write(sch)

    if with_pro:
        pro = {
            "board": {"design_settings": {"defaults": {}, "rule_severities": {}, "rules": {}}, "layer_presets": [], "viewports": []},
            "boards": [],
            "cvpcb": {"equivalence_files": []},
            "libraries": {"pinned_footprint_libs": [], "pinned_symbol_libs": []},
            "meta": {"filename": project + ".kicad_pro", "version": 3},
            "net_settings": {"classes": [{"name": "Default", "clearance": 0.2, "track_width": 0.2, "via_diameter": 0.6, "via_drill": 0.3,
                                          "wire_width": 6, "bus_width": 12, "line_style": 0, "priority": 2147483647,
                                          "diff_pair_gap": 0.25, "diff_pair_via_gap": 0.25, "diff_pair_width": 0.2,
                                          "microvia_diameter": 0.3, "microvia_drill": 0.1, "pcb_color": "rgba(0, 0, 0, 0.000)",
                                          "schematic_color": "rgba(0, 0, 0, 0.000)"}],
                             "meta": {"version": 4}, "net_colors": None, "netclass_assignments": None, "netclass_patterns": []},
            "pcbnew": {"last_paths": {"gencad": "", "idf": "", "netlist": "", "plot": "", "pos_files": "", "specctra_dsn": "", "step": "", "svg": "", "vrml": ""},
                       "page_layout_descr_file": ""},
            "schematic": {"legacy_lib_dir": "", "legacy_lib_list": []},
            "sheets": [[root_uuid, "Root"]],
            "text_variables": {},
        }
        json.dump(pro, open(os.path.join(outdir, project + ".kicad_pro"), "w", encoding="utf-8"), indent=2)
    json.dump(pinpos, open(os.path.join(outdir, "pinpos.json"), "w"), indent=1)
    return outdir

if __name__ == "__main__":
    out = sys.argv[1]
    ver = int(sys.argv[2]) if len(sys.argv) > 2 else 20260101
    gv = sys.argv[3] if len(sys.argv) > 3 else "10.0"
    pf = sys.argv[4] if len(sys.argv) > 4 else "endpoint"
    build(out, ver, gv, pwr_flag=(None if pf == "none" else pf))
    print("wrote", out)
