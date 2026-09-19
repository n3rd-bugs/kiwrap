"""Desktop manipulations for the multi-window tests:  python desk.py <action>
  overlap_on   move the 3D viewer over the pcbnew canvas (KiCad-over-KiCad)
  overlap_off  put it back
  foreign_on   start notepad over the pcbnew canvas (non-KiCad window on top)
  foreign_off  close it
  min_on / min_off   minimise / restore the eeschema frame
  dialog_on / dialog_off   footprint reference properties dialog in pcbnew (modal, owned)
  chooser_on / chooser_off  footprint chooser (A) in pcbnew
  regions      refresh session.json regions
"""
from __future__ import annotations

import json
import subprocess
import sys
import time

sys.path.insert(0, r"C:\Upwork\KiCad\prototypes\display\winstream")
import win32con  # noqa: E402
import win32gui  # noqa: E402

import session as S  # noqa: E402
from winstream import inject as I  # noqa: E402
from winstream import windows as W  # noqa: E402

W.set_dpi_aware()
SESS = "session.json"
sess = json.load(open(SESS))
pid, pcb, sch, v3d = sess["pid"], sess["pcb"], sess["sch"], sess.get("v3d")
inj = I.SendInputInjector(pcb, pid)


def key(vk, ext=False):
    I._send([I._key_input(vk, ext, False)])
    time.sleep(0.03)
    I._send([I._key_input(vk, ext, True)])


def click(sx, sy):
    inj.move(sx, sy)
    time.sleep(0.05)
    inj.button(sx, sy, 0, True)
    time.sleep(0.05)
    inj.button(sx, sy, 0, False)


def wait_title(sub, present=True, timeout=15):
    t0 = time.time()
    while time.time() - t0 < timeout:
        have = any(sub in w["title"] and w["visible"] for w in S.all_windows_of_pid(pid))
        if have == present:
            return round(time.time() - t0, 2)
        time.sleep(0.05)
    return -1


def save_regions():
    vis = [w for w in S.all_windows_of_pid(pid) if w["visible"] and w["caption"]]
    sess["windows_now"] = vis
    r = sess.setdefault("regions", {})
    for w in vis:
        if "PCB Editor" in w["title"]:
            r["pcb"] = w["rect"]
        elif "Schematic Editor" in w["title"]:
            r["sch"] = w["rect"]
        elif "KiCad 10" in w["title"]:
            r["mgr"] = w["rect"]
        elif w["title"] == "3D Viewer":
            r["v3d"] = w["rect"]
        elif "Properties" in w["title"]:
            r["dialog"] = w["rect"]
        elif "Chooser" in w["title"]:
            r["chooser"] = w["rect"]
    json.dump(sess, open(SESS, "w"), indent=1)
    return r


a = sys.argv[1]
if a == "overlap_on":
    W.resize_window(v3d, 6300, 400, 1400, 900)
    win32gui.SetWindowPos(v3d, win32con.HWND_TOP, 0, 0, 0, 0, win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE)
    sess["regions"]["overlap"] = [6300, 400, 7100, 1140]  # part of the 3D viewer that covers pcbnew
elif a == "overlap_off":
    W.resize_window(v3d, 5700, 1200, 1400, 900)
elif a == "foreign_on":
    p = subprocess.Popen(["notepad.exe"])
    time.sleep(1.5)
    hs = []
    def cb(h, _):
        import win32process
        if win32process.GetWindowThreadProcessId(h)[1] == p.pid and win32gui.IsWindowVisible(h) and win32gui.GetWindowText(h):
            hs.append(h)
        return True
    for _ in range(20):
        hs.clear(); win32gui.EnumWindows(cb, None)
        if hs: break
        time.sleep(0.25)
    if not hs:  # notepad on Win11 is a packaged app: find by title instead
        def cb2(h, _):
            if win32gui.IsWindowVisible(h) and "Notepad" in win32gui.GetWindowText(h): hs.append(h)
            return True
        win32gui.EnumWindows(cb2, None)
    W.resize_window(hs[0], 6000, 300, 800, 600)
    win32gui.SetWindowPos(hs[0], win32con.HWND_TOPMOST, 0, 0, 0, 0, win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE)
    sess["foreign_hwnd"] = hs[0]; sess["foreign_pid"] = p.pid
    sess["regions"]["foreign"] = [6000, 300, 6800, 900]
elif a == "foreign_off":
    import psutil
    try:
        win32gui.PostMessage(sess["foreign_hwnd"], win32con.WM_CLOSE, 0, 0)
        time.sleep(0.5)
        for pr in psutil.process_iter(["name"]):
            if pr.info["name"] and pr.info["name"].lower() == "notepad.exe":
                pr.kill()
    except Exception as e:  # noqa: BLE001
        print("foreign_off", e)
elif a == "min_on":
    win32gui.ShowWindow(sch, win32con.SW_MINIMIZE)
elif a == "min_off":
    win32gui.ShowWindow(sch, win32con.SW_RESTORE)
elif a == "dialog_on":
    S.focus(pcb)
    px, py = W.frame_bounds(pcb)[:2]
    key(0x24, True); time.sleep(0.5)
    click(px + 778, py + 820); time.sleep(0.4)
    key(ord("E"))
    print("dialog appeared after s", wait_title("Properties"))
    time.sleep(0.8)
elif a == "dialog_off":
    d=[w for w in S.all_windows_of_pid(pid) if w["visible"] and "Properties" in w["title"]]
    if d: S.focus(d[0]["hwnd"]); time.sleep(0.2)
    key(0x1B); time.sleep(0.4)
    print("dialog gone", wait_title("Properties", present=False))
    sess["regions"].pop("dialog", None)
elif a == "chooser_on":
    S.focus(pcb)
    px, py = W.frame_bounds(pcb)[:2]
    inj.move(px + 1000, py + 600)
    key(ord("A"))
    print("chooser appeared after s", wait_title("Chooser", timeout=30))
    time.sleep(1.5)
elif a == "chooser_off":
    d=[w for w in S.all_windows_of_pid(pid) if w["visible"] and "Chooser" in w["title"]]
    if d: S.focus(d[0]["hwnd"]); time.sleep(0.2)
    key(0x1B); time.sleep(0.6)
    print("chooser gone", wait_title("Chooser", present=False))
    sess["regions"].pop("chooser", None)
time.sleep(0.5)
print(save_regions())
