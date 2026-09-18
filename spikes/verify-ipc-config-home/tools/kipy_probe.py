import sys, os, time, traceback
from kipy import KiCad
t0=time.time()
try:
    k = KiCad()
    v = k.get_version()
    print("CONNECTED socket=", os.environ.get('KICAD_API_SOCKET','<default>'), "version=", v)
    try:
        b = k.get_board()
        print("board:", b.name if hasattr(b,'name') else b, "footprints=", len(b.get_footprints()))
    except Exception as e:
        print("get_board failed:", type(e).__name__, e)
except Exception as e:
    print("FAILED after %.1fs:"%(time.time()-t0), type(e).__name__, e)
    sys.exit(1)
