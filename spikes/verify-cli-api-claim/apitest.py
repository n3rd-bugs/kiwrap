import os, sys, traceback
from kipy import KiCad
from kipy.board_types import FootprintInstance
from kipy.proto.common.types import base_types_pb2
from kipy.proto.common.commands import editor_commands_pb2 as ec
from kipy.proto.board import board_types_pb2 as bt
from kipy.errors import ApiError
sock = None
k = KiCad(socket_path=sock, timeout_ms=15000)
print("version:", k.get_version(), "api:", k.get_api_version(), flush=True)
b = k.get_board()
print("board:", b.name, "footprints:", len(b.get_footprints()), flush=True)

# 1. Try to create a footprint by LIB_ID only (no definition)
fi = FootprintInstance()
fi.definition.id.library_nickname = "Resistor_SMD"
fi.definition.id.entry_name = "R_0603_1608Metric"
fi.reference_field.text.value = "R901"
fi.position = __import__('kipy.geometry', fromlist=['Vector2']).Vector2.from_xy(50_000_000, 50_000_000)
try:
    created = b.create_items(fi)
    print("create_items(FootprintInstance by LIB_ID only) ->", created, flush=True)
    for c in created:
        print("  created fp:", c.reference_field.text.value, "libid:", c.definition.id.library_nickname, ":", c.definition.id.entry_name, "pads:", len(c.definition.items), flush=True)
except Exception as e:
    print("create_items by LIB_ID FAILED:", type(e).__name__, e, flush=True)
print("footprints now:", len(b.get_footprints()), flush=True)

# 2. RunAction for DRC / specctra / netlist import
for act in ["pcbnew.DRCTool.runDRC", "pcbnew.EditorControl.exportSpecctraDSN", "pcbnew.EditorControl.importNetlist", "pcbnew.InspectionTool.runDRC", "pcbnew.EditorControl.exportNetlist"]:
    try:
        r = k.run_action(act)
        print("run_action", act, "->", r, flush=True)
    except Exception as e:
        print("run_action", act, "FAILED:", type(e).__name__, e, flush=True)
