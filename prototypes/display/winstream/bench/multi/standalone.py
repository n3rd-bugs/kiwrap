import json, sys, time, subprocess, os
sys.path.insert(0, r"C:\Upwork\KiCad\prototypes\display\winstream")
import session as S
from winstream import windows as W
W.set_dpi_aware()
spec = json.load(open("env10.json", encoding="utf-8-sig"))
proj = os.path.join(os.path.dirname(__file__) if "__file__" in globals() else ".", "ecc83")
pcbfile = os.path.abspath("ecc83/ecc83-pp.kicad_pcb"); schfile = os.path.abspath("ecc83/ecc83-pp.kicad_sch")
p1 = subprocess.Popen([spec["pcbnew"], pcbfile], env=spec["env"], cwd=os.path.dirname(pcbfile))
p2 = subprocess.Popen([spec["eeschema"], schfile], env=spec["env"], cwd=os.path.dirname(schfile))
t0 = time.time()
h1 = S.wait_title(p1.pid, "PCB Editor", 120); h2 = S.wait_title(p2.pid, "Schematic Editor", 120)
time.sleep(3)
out = {"pcbnew_pid": p1.pid, "eeschema_pid": p2.pid, "seconds": round(time.time()-t0,1)}
for name, pid in (("pcbnew", p1.pid), ("eeschema", p2.pid)):
    vis = [w for w in S.all_windows_of_pid(pid) if w["visible"] and w["caption"]]
    out[name] = [(w["hwnd"], w["tid"], w["owner"], w["cls"], w["title"]) for w in vis]
    print(name, pid, out[name])
# possible "File Open Warning" (lock held by the manager session) dialogs
for name, pid in (("pcbnew", p1.pid), ("eeschema", p2.pid)):
    dl = [w for w in S.all_windows_of_pid(pid) if w["visible"] and w["cls"] == "#32770"]
    print(name, "dialogs", [(w["title"], w["owner"]) for w in dl])
json.dump(out, open("results/standalone.json", "w"), indent=1)
for p in (p1, p2):
    p.kill()
time.sleep(1)
print("killed", p1.poll(), p2.poll())
