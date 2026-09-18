import sys, os, traceback, hashlib, io
SP = os.path.dirname(os.path.abspath(__file__))
versions = ["7.0","8.0","9.0","10.0"]
files = {v: (f"{SP}/samples/ecc83_{v}/ecc83-pp.kicad_sch", f"{SP}/samples/ecc83_{v}/ecc83-pp.kicad_pcb") for v in versions}
files["cm5_10.0"] = (f"{SP}/samples/cm5_10.0/CM5_MINIMA_3.kicad_sch", f"{SP}/samples/cm5_10.0/CM5_MINIMA_3.kicad_pcb")

def hdr(t): print("\n" + "="*20, t)

hdr("kicad-skip")
try:
    import skip
    print("skip version:", getattr(skip, "__version__", "?"))
    for v,(sch,pcb) in files.items():
        try:
            s = skip.Schematic(sch)
            syms = list(s.symbol)
            out = f"{SP}/out_skip_{v.replace('.','_')}.kicad_sch"
            s.write(out)
            same = open(sch,'rb').read() == open(out,'rb').read()
            print(f"  [{v}] sch OK: {len(syms)} symbols; write roundtrip byte-identical={same}")
        except Exception as e:
            print(f"  [{v}] sch FAIL: {type(e).__name__}: {str(e)[:160]}")
        try:
            p = skip.PCB(pcb)
            fps = list(p.footprint)
            print(f"  [{v}] pcb OK: {len(fps)} footprints")
        except Exception as e:
            print(f"  [{v}] pcb FAIL: {type(e).__name__}: {str(e)[:160]}")
except Exception as e:
    traceback.print_exc()

hdr("kiutils")
try:
    import kiutils
    from kiutils.schematic import Schematic
    from kiutils.board import Board
    print("kiutils version:", getattr(kiutils, "__version__", "?"))
    for v,(sch,pcb) in files.items():
        try:
            s = Schematic.from_file(sch)
            out = f"{SP}/out_kiutils_{v.replace('.','_')}.kicad_sch"
            s.to_file(out)
            same = open(sch,'rb').read() == open(out,'rb').read()
            print(f"  [{v}] sch OK: {len(s.schematicSymbols)} symbols, version={s.version}; roundtrip identical={same}")
        except Exception as e:
            print(f"  [{v}] sch FAIL: {type(e).__name__}: {str(e)[:160]}")
        try:
            b = Board.from_file(pcb)
            out = f"{SP}/out_kiutils_{v.replace('.','_')}.kicad_pcb"
            b.to_file(out)
            same = open(pcb,'rb').read() == open(out,'rb').read()
            print(f"  [{v}] pcb OK: {len(b.footprints)} footprints, version={b.version}; roundtrip identical={same}")
        except Exception as e:
            print(f"  [{v}] pcb FAIL: {type(e).__name__}: {str(e)[:160]}")
except Exception as e:
    traceback.print_exc()

hdr("kicad-sch-api")
try:
    import kicad_sch_api as ksa
    print("kicad_sch_api version:", getattr(ksa, "__version__", "?"))
    for v,(sch,pcb) in files.items():
        try:
            s = ksa.load(sch)
            comps = list(s.components)
            out = f"{SP}/out_ksa_{v.replace('.','_')}.kicad_sch"
            s.save(out)
            same = open(sch,'rb').read() == open(out,'rb').read()
            # check global_label preservation (bug reported by kicad-mcp-pro)
            gl_in = open(sch,encoding='utf-8').read().count("(global_label")
            gl_out = open(out,encoding='utf-8').read().count("(global_label")
            lbl_in = open(sch,encoding='utf-8').read().count("(label")
            lbl_out = open(out,encoding='utf-8').read().count("(label")
            print(f"  [{v}] sch OK: {len(comps)} components; roundtrip identical={same}; global_label in/out={gl_in}/{gl_out}; label in/out={lbl_in}/{lbl_out}")
        except Exception as e:
            print(f"  [{v}] sch FAIL: {type(e).__name__}: {str(e)[:200]}")
except Exception as e:
    traceback.print_exc()
