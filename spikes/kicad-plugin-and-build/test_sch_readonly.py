from kipy import KiCad
from kipy.proto.common.types import base_types_pb2 as bt
k = KiCad(timeout_ms=10000, client_name="research-probe-sch-ro")
try: print("open SCH docs:", [(d.board_filename, d.project.name) for d in k.get_open_documents(bt.DocumentType.DOCTYPE_SCHEMATIC)])
except Exception as e: print("ERR", e)
