import os, traceback
from kipy import KiCad
from kipy.proto.common.types import base_types_pb2 as bt
def step(label, fn):
    try: print(f"{label}: {fn()}")
    except Exception as e: print(f"{label}: ERR {type(e).__name__}: {str(e)[:200]}")
k = KiCad(timeout_ms=15000, client_name="research-probe")
print("socket used:", k._client._socket_path)
step("version", lambda: (k.get_version(), k.get_api_version()))
step("ping", k.ping)
print("token learned:", k._client._kicad_token)
step("kicad-cli path", lambda: k.get_kicad_binary_path("kicad-cli.exe"))
step("plugin settings path com.example.envdump", lambda: k.get_plugin_settings_path("com.example.envdump"))
step("plugin settings path org.kicad.test.plugin", lambda: k.get_plugin_settings_path("org.kicad.test.plugin"))
step("open PCB docs", lambda: [(d.board_filename, d.project.name, d.project.path) for d in k.get_open_documents(bt.DocumentType.DOCTYPE_PCB)])
step("open SCH docs (from pcbnew)", lambda: list(k.get_open_documents(bt.DocumentType.DOCTYPE_SCHEMATIC)))
b = k.get_board()
step("board summary", lambda: (b.name, len(b.get_footprints()), len(b.get_nets()), len(b.get_tracks()), b.get_copper_layer_count()))
step("get_as_string len", lambda: len(b.get_as_string()))
step("run_action common.Control.zoomFitScreen", lambda: k.run_action("common.Control.zoomFitScreen"))
step("run_action pcbnew.ZoneFiller.zoneFillAll", lambda: k.run_action("pcbnew.ZoneFiller.zoneFillAll"))
step("run_action pcbnew.InteractiveRouter.SingleTrack", lambda: k.run_action("pcbnew.InteractiveRouter.SingleTrack"))
step("run_action does.not.exist", lambda: k.run_action("does.not.exist"))
step("run_action pcbnew.DRCTool.runDRC (opens dialog?)", lambda: k.run_action("pcbnew.DRCTool.runDRC"))
