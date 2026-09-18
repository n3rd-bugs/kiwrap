import os, sys
base = r"C:\Users\usama\AppData\Local\Temp\claude\c--Upwork-KiCad\48a8df66-7213-479f-ab2e-a63b89135016\scratchpad\verify-ipc-config-home\tmp10\kicad\api.sock"
for cand in (f"ipc://{base}", "ipc://" + base.replace("\\","/")):
    os.environ["KICAD_API_SOCKET"] = cand
    from kipy import KiCad
    try:
        k = KiCad(); print("OK", cand, "->", k.get_version()); 
        try: print("   board:", k.get_board().name)
        except Exception as e: print("   get_board:", type(e).__name__, e)
        break
    except Exception as e:
        print("FAIL", cand, "->", type(e).__name__, e)
