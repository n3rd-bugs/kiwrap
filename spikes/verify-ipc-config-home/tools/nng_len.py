import pynng, sys
base = r"C:\Users\usama\AppData\Local\Temp\claude\c--Upwork-KiCad\48a8df66-7213-479f-ab2e-a63b89135016\scratchpad\verify-ipc-config-home\tmp10\kicad\api.sock"
tests = {
  "short": r"C:\Users\usama\AppData\Local\Temp\kicad\api2.sock",
  "long_full": base,
}
for name, path in tests.items():
    addr = "ipc://" + path
    try:
        s = pynng.Rep0(); s.listen(addr); s.close(); print(f"{name} (len {len(path)}): listen OK")
    except Exception as e:
        print(f"{name} (len {len(path)}): listen FAIL {type(e).__name__}: {e}")
# find the threshold
import string
for n in (100, 120, 140, 160, 200, 250):
    p = r"C:\Users\usama\AppData\Local\Temp\kicad\\" + "x"*(n-40) + ".sock"
    try:
        s = pynng.Rep0(); s.listen("ipc://"+p); s.close(); print(f"len {len(p)}: OK")
    except Exception as e:
        print(f"len {len(p)}: FAIL {e}")
