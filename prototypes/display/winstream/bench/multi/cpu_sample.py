"""python cpu_sample.py <seconds> <tag>: average CPU % (one core = 100) per process group over the window.
Groups: kicad (kicad.exe), tvnserver, websockify (python child), chromium (playwright headless), winstream (server.py)."""
import json
import sys
import time

import psutil

secs = float(sys.argv[1])
tag = sys.argv[2] if len(sys.argv) > 2 else ""
groups = {"kicad": [], "tvnserver": [], "websockify": [], "chromium": [], "winstream": [], "code": []}
for p in psutil.process_iter(["name", "cmdline"]):
    n = (p.info["name"] or "").lower()
    cl = " ".join(p.info["cmdline"] or []).lower()
    if n == "kicad.exe" or n in ("pcbnew.exe", "eeschema.exe"):
        groups["kicad"].append(p)
    elif n == "tvnserver.exe":
        groups["tvnserver"].append(p)
    elif "websockify" in cl:
        groups["websockify"].append(p)
    elif (n.startswith("chrome") or n.startswith("headless")) and ("headless" in cl or "playwright" in cl or "ms-playwright" in cl):
        groups["chromium"].append(p)
    elif "winstream.server" in cl and n.startswith("python"):
        groups["winstream"].append(p)
    elif n == "code.exe" and ("display2" in cl or "extensiondevelopmentpath" in cl):
        groups["code"].append(p)
for ps in groups.values():
    for p in ps:
        try:
            p.cpu_percent(None)
        except psutil.Error:
            pass
t0 = time.time()
time.sleep(secs)
dt = time.time() - t0
out = {"tag": tag, "seconds": round(dt, 1), "cores": psutil.cpu_count()}
for k, ps in groups.items():
    tot = 0.0
    for p in ps:
        try:
            tot += p.cpu_percent(None)
        except psutil.Error:
            pass
    out[k] = round(tot, 1)
    out[k + "_n"] = len(ps)
try:
    import subprocess
    out["gpu"] = subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader"], capture_output=True, text=True, timeout=3).stdout.strip()
except Exception:  # noqa: BLE001
    pass
print(json.dumps(out))
