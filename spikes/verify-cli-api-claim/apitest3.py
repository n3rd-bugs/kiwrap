from kipy import KiCad
from kipy.proto.common.commands import editor_commands_pb2 as ec
k = KiCad(timeout_ms=15000); b = k.get_board()
print("footprints before:", len(b.get_footprints()), flush=True)
mod = open(r"C:\Program Files\KiCad\10.0\share\kicad\footprints\Resistor_SMD.pretty\R_0603_1608Metric.kicad_mod", encoding="utf-8").read()
req2 = ec.ParseAndCreateItemsFromString()
req2.document.CopyFrom(b.document)
req2.contents = "(kicad_pcb (version 20250520) (generator \"pcbnew\") (generator_version \"10.0\") (general (thickness 1.6) (legacy_teardrops no)) (paper \"A4\") (layers (0 \"F.Cu\" signal) (2 \"B.Cu\" signal)) " + mod + ")"
try:
    resp2 = k._client.send(req2, ec.CreateItemsResponse)
    for r in resp2.created_items:
        print("ParseAndCreateItemsFromString(.kicad_mod) -> status:", ec.ItemStatusCode.Name(r.status.code), "msg:", repr(r.status.error_message), flush=True)
    fps = b.get_footprints(); print("footprints now:", len(fps), flush=True)
    for f in fps:
        if str(f.definition.id) == "Resistor_SMD:R_0603_1608Metric":
            print("  fp", f.reference_field.text.value, "pads:", len(f.definition.pads), "items:", len(f.definition.items), flush=True)
except Exception as e:
    print("ParseAndCreateItemsFromString FAILED:", type(e).__name__, e, flush=True)
