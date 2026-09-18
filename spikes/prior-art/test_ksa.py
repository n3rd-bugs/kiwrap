import os, re
SP = os.path.dirname(os.path.abspath(__file__))
import kicad_sch_api as ksa
versions = ["7.0","8.0","9.0","10.0"]
files = {v: f"{SP}/samples/ecc83_{v}/ecc83-pp.kicad_sch" for v in versions}
files["cm5_10.0"] = f"{SP}/samples/cm5_10.0/CM5_MINIMA_3.kicad_sch"
def toks(t):
    from collections import Counter
    return Counter(re.findall(r"\((\w+)", t))
for v, sch in files.items():
    try:
        s = ksa.load_schematic(sch)
        comps = list(s.components)
        out = f"{SP}/out_ksa_{v.replace('.','_')}.kicad_sch"
        s.save(out)
        a = open(sch, encoding='utf-8').read(); b = open(out, encoding='utf-8').read()
        ta, tb = toks(a), toks(b)
        lost = {k:(ta[k],tb.get(k,0)) for k in ta if tb.get(k,0) < ta[k]}
        print(f"[{v}] OK {len(comps)} comps; identical={a==b}; tokens lost (in->out): {dict(list(lost.items())[:12])}")
    except Exception as e:
        print(f"[{v}] FAIL {type(e).__name__}: {str(e)[:200]}")
