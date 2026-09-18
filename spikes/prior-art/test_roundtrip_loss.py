import os, re, subprocess, glob
from collections import Counter
SP = os.path.dirname(os.path.abspath(__file__))
def toks(t): return Counter(re.findall(r"\((\w+)", t))
def loss(a, b):
    ta, tb = toks(a), toks(b)
    return {k:(ta[k],tb.get(k,0)) for k in ta if tb.get(k,0) < ta[k]}
pairs = []
for out in sorted(glob.glob(f"{SP}/out_*.kicad_*")):
    name = os.path.basename(out)
    m = re.match(r"out_(\w+?)_(cm5_)?(\d+)_(\d+)\.(kicad_\w+)", name)
    lib, cm5, maj, mn, ext = m.groups()
    src = f"{SP}/samples/cm5_10.0/CM5_MINIMA_3.{ext}" if cm5 else f"{SP}/samples/ecc83_{maj}.{mn}/ecc83-pp.{ext}"
    a = open(src, encoding='utf-8').read(); b = open(out, encoding='utf-8').read()
    l = loss(a, b)
    print(f"{name:32s} identical={a==b!s:5s} lost={len(l):3d} e.g. {dict(list(l.items())[:6])}")
