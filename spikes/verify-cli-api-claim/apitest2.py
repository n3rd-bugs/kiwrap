import os
from kipy import KiCad
from kipy.board_types import FootprintInstance
from kipy.geometry import Vector2
from kipy.proto.common.commands import editor_commands_pb2 as ec
from kipy.proto.common.types import base_types_pb2 as base
from kipy.proto.board import board_types_pb2 as bt
from google.protobuf.any_pb2 import Any
k = KiCad(timeout_ms=15000)
b = k.get_board()
print("board:", b.name, "footprints:", len(b.get_footprints()), flush=True)

# --- raw CreateItems with FootprintInstance carrying only a LIB_ID (no geometry) ---
fi = bt.FootprintInstance()
fi.definition.id.library_nickname = "Resistor_SMD"
fi.definition.id.entry_name = "R_0603_1608Metric"
fi.reference_field.text.text.text = "R901"
fi.position.x_nm = 50_000_000; fi.position.y_nm = 50_000_000
fi.layer = bt.BoardLayer.BL_F_Cu
req = ec.CreateItems()
req.header.document.CopyFrom(b.document)
a = Any(); a.Pack(fi); req.items.append(a)
resp = k._client.send(req, ec.CreateItemsResponse)
for r in resp.created_items:
    print("CreateItems by LIB_ID -> status:", ec.ItemStatusCode.Name(r.status.code), "msg:", repr(r.status.error_message), flush=True)
    if r.status.code == ec.ItemStatusCode.ISC_OK:
        got = bt.FootprintInstance(); r.item.Unpack(got)
        print("   returned libid:", got.definition.id.library_nickname, ":", got.definition.id.entry_name, "definition items:", len(got.definition.items), "ref:", got.reference_field.text.text.text, flush=True)
fps = b.get_footprints()
print("footprints now:", len(fps), flush=True)
for f in fps:
    if f.reference_field.text.value == "R901" if hasattr(f.reference_field.text,"value") else f.reference_field.text.text.text == "R901":
        print("  R901 on board: pads:", len(f.definition.pads), "items:", len(f.definition.items), "libid:", f.definition.id, flush=True)

# --- workaround: client reads .kicad_mod from disk and uses ParseAndCreateItemsFromString ---
mod = open(r"C:\Program Files\KiCad\10.0\share\kicad\footprints\Resistor_SMD.pretty\R_0603_1608Metric.kicad_mod", encoding="utf-8").read()
req2 = ec.ParseAndCreateItemsFromString()
req2.header.document.CopyFrom(b.document)
req2.text = mod
try:
    resp2 = k._client.send(req2, ec.CreateItemsResponse)
    for r in resp2.created_items:
        print("ParseAndCreateItemsFromString(.kicad_mod) -> status:", ec.ItemStatusCode.Name(r.status.code), "msg:", repr(r.status.error_message), flush=True)
    print("footprints now:", len(b.get_footprints()), flush=True)
except Exception as e:
    print("ParseAndCreateItemsFromString FAILED:", type(e).__name__, e, flush=True)
