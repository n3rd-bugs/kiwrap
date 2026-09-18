import sys, time
from kipy import KiCad
from kipy.errors import ApiError
t0=time.time()
k=None
while time.time()-t0 < 60:
    try:
        k=KiCad(timeout_ms=3000); v=k.get_version(); break
    except Exception as e:
        err=e; time.sleep(2)
if k is None:
    print("FAILED to connect:", err); sys.exit(1)
print("connected; KiCad version:", v, "| kipy api version:", k.get_api_version())
try:
    print("check_version:", k.check_version())
except Exception as e:
    print("check_version raised:", type(e).__name__, e)
print("open docs:", [ (d.type, d.board_filename) for d in k.get_open_documents(__import__('kipy.proto.common.types.base_types_pb2',fromlist=['DocumentType']).DocumentType.DOCTYPE_PCB)])
b=k.get_board()
print("footprints:", len(b.get_footprints()), "tracks:", len(b.get_tracks()), "nets:", len(b.get_nets()), "zones:", len(b.get_zones()))
print("kicad binary path:", k.get_kicad_binary_path("kicad-cli"))
