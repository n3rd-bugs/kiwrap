import os, re, sys
from collections import Counter
SP = os.path.dirname(os.path.abspath(__file__))
def toks(t): return Counter(re.findall(r"\((\w+)", t))
import kicad_tools
print("kicad_tools", getattr(kicad_tools, "__version__", "?"))
from kicad_tools.schematic import Schematic
from kicad_tools.parser import PCBParser
for v in ["7.0","8.0","9.0","10.0"]:
    sch = f"{SP}/samples/ecc83_{v}/ecc83-pp.kicad_sch"; pcb = f"{SP}/samples/ecc83_{v}/ecc83-pp.kicad_pcb"
    try:
        s = Schematic.load(sch)
        print(f"[{v}] sch OK symbols={len(s.symbols)}", end="; ")
        out = f"{SP}/out_kct_{v.replace('.','_')}.kicad_sch"; s.save(out)
        a=open(sch,encoding='utf-8').read(); b=open(out,encoding='utf-8').read()
        ta,tb=toks(a),toks(b); lost={k:(ta[k],tb.get(k,0)) for k in ta if tb.get(k,0)<ta[k]}
        print(f"identical={a==b} lost={dict(list(lost.items())[:5])}")
    except Exception as e:
        print(f"[{v}] sch FAIL {type(e).__name__}: {str(e)[:150]}")
    try:
        p = PCBParser(pcb); board = p.parse() if hasattr(p,'parse') else p
        fps = getattr(board,'footprints',None)
        print(f"[{v}] pcb OK footprints={len(fps) if fps is not None else '?'}")
    except Exception as e:
        print(f"[{v}] pcb FAIL {type(e).__name__}: {str(e)[:150]}")
