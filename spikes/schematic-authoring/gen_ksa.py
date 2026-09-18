"""Generate the same LED circuit with kicad-sch-api (pip: kicad-sch-api).
Usage: venv/Scripts/python.exe gen_ksa.py <outdir>
"""
import sys, os, json, traceback
os.environ.setdefault("KICAD_SYMBOL_DIR", r"C:/Program Files/KiCad/10.0/share/kicad/symbols")
import kicad_sch_api as ksa

outdir = os.path.abspath(sys.argv[1]); os.makedirs(outdir, exist_ok=True)
name = os.path.basename(outdir)

sch = ksa.create_schematic(name)
R1 = sch.components.add("Device:R", reference="R1", value="330R", position=(63.5, 50.8), rotation=0,
                        footprint="Resistor_SMD:R_0603_1608Metric")
D1 = sch.components.add("Device:LED", reference="D1", value="LED", position=(63.5, 63.5), rotation=90,
                        footprint="LED_SMD:LED_0603_1608Metric")
J1 = sch.components.add("Connector_Generic:Conn_01x02", reference="J1", value="Conn_01x02", position=(81.28, 46.99),
                        footprint="Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical")
G1 = sch.components.add("power:GND", reference="#PWR01", value="GND", position=(63.5, 73.66))
G2 = sch.components.add("power:GND", reference="#PWR02", value="GND", position=(76.2, 73.66))
F1 = sch.components.add("power:PWR_FLAG", reference="#FLG01", value="PWR_FLAG", position=(76.2, 60.96))

pos = {}
for ref in ("R1", "D1", "J1", "#PWR01", "#PWR02", "#FLG01"):
    for pin in ("1", "2"):
        try:
            p = sch.get_component_pin_position(ref, pin)
            if p is not None:
                pos["%s.%s" % (ref, pin)] = (p.x, p.y) if hasattr(p, "x") else tuple(p)
        except Exception as e:
            pass
print("pin positions computed by kicad-sch-api:", json.dumps(pos))

# wires (explicit, same topology as the plain generator)
def W(a, b):
    sch.add_wire(a, b)
W(pos["J1.1"], pos["R1.1"])
W(pos["R1.2"], pos["D1.2"])
W(pos["D1.1"], pos["#PWR01.1"])
W(pos["J1.2"], pos["#FLG01.1"])
W(pos["#FLG01.1"], pos["#PWR02.1"])
mid = (pos["R1.2"][0], round((pos["R1.2"][1] + pos["D1.2"][1]) / 2, 4))
sch.add_label("LED_A", position=mid)
sch.add_global_label("VIN", position=(round((pos["J1.1"][0] + pos["R1.1"][0]) / 2, 4), pos["J1.1"][1]))

out = os.path.join(outdir, name + ".kicad_sch")
sch.save(out)
print("saved", out)
# also a minimal project file next to it
pro = {"meta": {"filename": name + ".kicad_pro", "version": 1}}
json.dump(pro, open(os.path.join(outdir, name + ".kicad_pro"), "w"), indent=2)
