import inspect, importlib, kipy, sys
from google.protobuf.message import Message

print("kipy version file:", kipy.__file__)
print("dir(kipy):", [n for n in dir(kipy) if not n.startswith('_')])
print()
for modname in ["kipy.kicad", "kipy.board", "kipy.schematic", "kipy.project", "kipy.board_jobs", "kipy.board_rules", "kipy.wizards", "kipy.server"]:
    try:
        m = importlib.import_module(modname)
    except Exception as e:
        print(f"!! {modname}: {e}"); continue
    print(f"=== {modname} ===")
    for cname, cls in inspect.getmembers(m, inspect.isclass):
        if cls.__module__ != modname: continue
        methods = [n for n, f in inspect.getmembers(cls, inspect.isfunction) if not n.startswith('_')]
        props = [n for n, f in inspect.getmembers(cls, lambda x: isinstance(x, property))]
        print(f"  class {cname}: methods={methods}")
        if props: print(f"      props={props}")
    print()

# enumerate proto command messages (request types)
print("=== PROTO COMMAND MESSAGES (requests/responses) ===")
for modname in ["kipy.proto.common.commands.base_commands_pb2",
                "kipy.proto.common.commands.editor_commands_pb2",
                "kipy.proto.common.commands.project_commands_pb2",
                "kipy.proto.board.board_commands_pb2",
                "kipy.proto.board.board_jobs_pb2",
                "kipy.proto.schematic.schematic_commands_pb2",
                "kipy.proto.schematic.schematic_jobs_pb2",
                "kipy.proto.common.types.jobs_pb2",
                "kipy.proto.common.types.wizards_pb2",
                "kipy.proto.common.types.generators_pb2"]:
    m = importlib.import_module(modname)
    names = [n for n in m.DESCRIPTOR.message_types_by_name.keys()]
    enums = [n for n in m.DESCRIPTOR.enum_types_by_name.keys()]
    print(f"--- {modname} ({len(names)} messages)")
    print("   ", ", ".join(names))
    if enums: print("    enums:", ", ".join(enums))
print()
print("=== ENUM VALUES of interest ===")
from kipy.proto.common.types import enums_pb2, base_types_pb2
from kipy.proto.common.commands import base_commands_pb2, editor_commands_pb2
print("KiCadObjectType:", [v.name for v in enums_pb2.KiCadObjectType.DESCRIPTOR.values])
print("DocumentType:", [v.name for v in base_types_pb2.DocumentType.DESCRIPTOR.values])
print("ApiStatusCode:", [v.name for v in importlib.import_module("kipy.proto.common.envelope_pb2").ApiStatusCode.DESCRIPTOR.values])
print("RunActionStatus:", [v.name for v in base_commands_pb2.RunActionStatus.DESCRIPTOR.values])
