import os, sys, time
os.environ["KICAD_SYMBOL_DIR"] = r"C:\Program Files\KiCad\10.0\share\kicad\symbols"
os.environ["KICAD10_SYMBOL_DIR"] = r"C:\Program Files\KiCad\10.0\share\kicad\symbols"
os.environ["KICAD9_SYMBOL_DIR"] = r"C:\Program Files\KiCad\9.0\share\kicad\symbols"
import skidl
print("skidl", skidl.__version__, "tools:", [t for t in dir(skidl) if t.startswith("KICAD")])
from skidl import *
set_default_tool(KICAD10 if hasattr(skidl,"KICAD10") else KICAD9)
print("default tool:", skidl.config.tool if hasattr(skidl,'config') else '?')
vin, gnd, out = Net("VIN"), Net("GND"), Net("OUT")
r1 = Part("Device", "R", value="10k", footprint="Resistor_SMD:R_0603_1608Metric")
r2 = Part("Device", "R", value="1k", footprint="Resistor_SMD:R_0603_1608Metric")
c1 = Part("Device", "C", value="100n", footprint="Capacitor_SMD:C_0603_1608Metric")
vin & r1 & out & r2 & gnd
c1[1] += out; c1[2] += gnd
ERC()
t=time.time(); generate_netlist(file_="divider.net"); print("netlist bytes:", os.path.getsize("divider.net"), round(time.time()-t,2), "s")
try:
    t=time.time(); generate_schematic(file_="divider.kicad_sch"); print("schematic ok:", os.path.exists("divider.kicad_sch"), round(time.time()-t,2), "s")
    print(open("divider.kicad_sch").read(120).replace("\n"," "))
except Exception as e:
    print("generate_schematic FAIL:", type(e).__name__, str(e)[:300])
try:
    generate_pcb(file_="divider.kicad_pcb"); print("pcb ok:", os.path.exists("divider.kicad_pcb"))
except Exception as e:
    print("generate_pcb FAIL:", type(e).__name__, str(e)[:300])
