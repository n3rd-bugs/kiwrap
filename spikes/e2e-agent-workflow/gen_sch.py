import os, time
os.environ.setdefault("KICAD_SYMBOL_DIR", r"C:\Program Files\KiCad\10.0\share\kicad\symbols")
import kicad_sch_api as ksa
c = ksa.get_symbol_cache()
t=time.time(); c.discover_libraries(); print("discover_libraries", f"{time.time()-t:.1f}s")
t=time.time(); res = c.search_symbols("ESP32-S3-WROOM", limit=5); print("search ESP32-S3-WROOM ->", [r.lib_id for r in res], f"{time.time()-t:.1f}s")
res = c.search_symbols("USB_C_Receptacle", limit=5); print("search USB_C ->", [r.lib_id for r in res])
res = c.search_symbols("MCP73831", limit=5); print("search MCP73831 ->", [r.lib_id for r in res])
info = ksa.get_symbol_info("Device:R")
print("Device:R pins:", [(p.number, p.name, str(p.position)) for p in info.pins])
u = ksa.get_symbol_info("RF_Module:ESP32-S3-WROOM-1")
print("ESP32-S3-WROOM-1 pin count", len(u.pins), "first:", [(p.number,p.name) for p in u.pins[:5]], "fp filter/default:", getattr(u,'footprint',None))
sch = ksa.create_schematic("agent_test")
sch.set_title_block(title="Agent Test", rev="A")
r1 = sch.components.add(lib_id="Device:R", reference="R1", value="10k", position=(100, 100), footprint="Resistor_SMD:R_0603_1608Metric")
c1 = sch.components.add(lib_id="Device:C", reference="C1", value="100nF", position=(120, 100), footprint="Capacitor_SMD:C_0603_1608Metric")
u1 = sch.components.add(lib_id="Regulator_Linear:AMS1117-3.3", reference="U1", value="AMS1117-3.3", position=(80, 60), footprint="Package_TO_SOT_SMD:SOT-223-3_TabPin2")
print("R1 pin1 at", r1.get_pin_position("1"), "pin2 at", r1.get_pin_position("2"))
print("U1 pins", [(p.number, p.name) for p in u1.pins])
for txt, comp, pin in [("VCC_3V3", r1, "1"), ("VCC_3V3", c1, "1"), ("GND", c1, "2"), ("SIG", r1, "2"), ("VCC_3V3", u1, "2"), ("GND", u1, "1"), ("VIN", u1, "3")]:
    sch.add_label(txt, position=comp.get_pin_position(pin))
sch.add_wire(start=(150,100), end=(160,100))   # dangling wire -> expect ERC warning
sch.save("gen/agent_test.kicad_sch")
print("saved")
