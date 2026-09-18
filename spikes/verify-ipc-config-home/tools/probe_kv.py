import os
os.environ["KICAD_API_SOCKET"] = r"ipc://C:\Users\usama\AppData\Local\Temp\kv-ipc\kicad\api.sock"
from kipy import KiCad
k = KiCad(); print("OK version:", k.get_version())
try: print("board:", k.get_board().name)
except Exception as e: print("get_board:", type(e).__name__, e)
try: print("open docs:", k.get_open_documents(__import__('kipy.proto.common.types', fromlist=['DocumentType']).DocumentType.DOCTYPE_PCB))
except Exception as e: print("get_open_documents:", type(e).__name__, e)
