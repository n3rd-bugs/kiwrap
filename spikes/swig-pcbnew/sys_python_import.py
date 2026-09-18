# Try to import KiCad 10's SWIG pcbnew from the SYSTEM python 3.11.8 (C:/Python311), not the bundled one.
import sys, os
print("python:", sys.version, sys.executable)
BIN = r"C:\Program Files\KiCad\10.0\bin"
mode = sys.argv[1] if len(sys.argv) > 1 else "naive"
if mode == "naive":
    sys.path.insert(0, os.path.join(BIN, "Lib", "site-packages"))
elif mode == "dlldir":
    sys.path.insert(0, os.path.join(BIN, "Lib", "site-packages"))
    os.add_dll_directory(BIN)
elif mode == "path":
    sys.path.insert(0, os.path.join(BIN, "Lib", "site-packages"))
    os.environ["PATH"] = BIN + os.pathsep + os.environ["PATH"]
try:
    import pcbnew
    print("import OK:", pcbnew.__file__, pcbnew.GetBuildVersion())
    b = pcbnew.BOARD(); print("BOARD ok, layers", b.GetCopperLayerCount())
    fp = pcbnew.FootprintLoad(r"C:\Program Files\KiCad\10.0\share\kicad\footprints\Resistor_SMD.pretty", "R_0603_1608Metric")
    print("FootprintLoad:", fp.GetReference() if fp else None)
except Exception as e:
    print("import FAILED:", type(e).__name__, e)
