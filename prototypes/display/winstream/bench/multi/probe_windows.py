"""Open the footprint chooser, a modal properties dialog and the 3D viewer in the session's pcbnew
frame (SendInput, window foreground on monitor 2), dump the process's window list after each step
and grab monitor-2 screenshots (Win32 via PIL ImageGrab, physical pixels)."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, r"C:\Upwork\KiCad\prototypes\display\winstream")
from PIL import ImageGrab  # noqa: E402

import session as S  # noqa: E402
from winstream import inject as I  # noqa: E402
from winstream import windows as W  # noqa: E402

HERE = Path(__file__).parent
MON2 = (3840, 0, 7680, 2160)


def shot(name: str, bbox=MON2, scale=0.5):
    im = ImageGrab.grab(bbox=bbox, all_screens=True)
    if scale != 1:
        im = im.resize((int(im.width * scale), int(im.height * scale)))
    p = HERE / "shots" / f"{name}.png"
    im.save(p, optimize=True)
    return str(p)


def dump(pid: int, tag: str, log: dict):
    wins = [w for w in S.all_windows_of_pid(pid) if w["visible"]]
    log[tag] = wins
    print(f"--- {tag}: {len(wins)} visible top-level windows")
    for w in wins:
        print(f"  hwnd={w['hwnd']} owner={w['owner']} en={int(w['enabled'])} cls={w['cls']} rect={w['rect']} title={w['title']!r}")
    return wins


def click(sx, sy, button=0):
    inj.move(sx, sy)
    time.sleep(0.05)
    inj.button(sx, sy, button, True)
    time.sleep(0.05)
    inj.button(sx, sy, button, False)


def key(vk, ext=False):
    I._send([I._key_input(vk, ext, False)])
    time.sleep(0.03)
    I._send([I._key_input(vk, ext, True)])


W.set_dpi_aware()
sess = json.load(open(HERE / "session.json"))
pid, pcb, sch, mgr = sess["pid"], sess["pcb"], sess["sch"], sess["manager"]
inj = I.SendInputInjector(pcb, pid)
log: dict = {}
px, py = W.frame_bounds(pcb)[:2]

S.focus(pcb)
key(0x24, True)  # Home: zoom fit
time.sleep(0.6)
dump(pid, "base", log)
shot("00-monitor2-base")

# modal properties dialog: click the R3 reference text, then E
click(px + 778, py + 820)
time.sleep(0.4)
t = time.time()
key(ord("E"))
for _ in range(40):
    time.sleep(0.1)
    if any(w["cls"] == "#32770" or ("Properties" in w["title"]) for w in S.all_windows_of_pid(pid) if w["visible"]):
        break
log["t_dialog_s"] = round(time.time() - t, 2)
time.sleep(0.5)
dump(pid, "properties_dialog", log)
shot("01-monitor2-properties-dialog")
key(0x1B)
time.sleep(0.5)
key(0x1B)
time.sleep(0.3)

# footprint chooser: A
S.focus(pcb)
inj.move(px + 1000, py + 600)
t = time.time()
key(ord("A"))
for _ in range(80):
    time.sleep(0.1)
    if any("Choose Footprint" in w["title"] for w in S.all_windows_of_pid(pid) if w["visible"]):
        break
log["t_chooser_s"] = round(time.time() - t, 2)
time.sleep(1.5)
dump(pid, "footprint_chooser", log)
shot("02-monitor2-footprint-chooser")
key(0x1B)
time.sleep(0.8)
key(0x1B)
time.sleep(0.3)

# 3D viewer: Alt+3
S.focus(pcb)
t = time.time()
I._send([I._key_input(0xA4, False, False)])
time.sleep(0.03)
key(ord("3"))
I._send([I._key_input(0xA4, False, True)])
for _ in range(120):
    time.sleep(0.1)
    if any("3D Viewer" in w["title"] for w in S.all_windows_of_pid(pid) if w["visible"]):
        break
log["t_3d_s"] = round(time.time() - t, 2)
time.sleep(4.0)
wins = dump(pid, "viewer3d", log)
shot("03-monitor2-3d-viewer")
v3 = [w for w in wins if "3D Viewer" in w["title"]]
if v3:
    W.resize_window(v3[0]["hwnd"], 5700, 1200, 1400, 900)
    time.sleep(0.5)
    dump(pid, "viewer3d_moved", log)
    shot("04-monitor2-all-frames")

# eeschema modal dialog too (symbol properties via E on a symbol is fragile; use the page settings? use Ctrl+Shift+S? no) -> ERC dialog (modeless in 10)
S.focus(sch)
time.sleep(0.3)
key(0x24, True)
time.sleep(0.5)
dump(pid, "final", log)
json.dump(log, open(HERE / "results" / "probe_windows.json", "w"), indent=1)
print("timings", {k: v for k, v in log.items() if k.startswith("t_")})
