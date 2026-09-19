"""Full KiCad project session the way a user opens it: kicad.exe (manager) on a scratch project,
then the schematic and PCB editors opened FROM the manager (Ctrl+E / Ctrl+P), arranged on monitor 2.

    python session.py start [--env env.json] [--pro <scratch>.kicad_pro] [--out session.json]
    python session.py windows [--pid N]        # dump every top-level window of the PID (visible or not)
    python session.py stop

Everything is physical pixels (per-monitor-v2 DPI aware). Sandboxed env only (kiwrap.sandbox).
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, r"C:\Upwork\KiCad\prototypes\display\winstream")
import win32con  # noqa: E402
import win32gui  # noqa: E402
import win32process  # noqa: E402

from winstream import inject as I  # noqa: E402
from winstream import windows as W  # noqa: E402

HERE = Path(__file__).parent
user32 = ctypes.windll.user32


def all_windows_of_pid(pid: int) -> list[dict]:
    """Every top-level HWND of the process, visible or not, with class/title/owner/styles/thread."""
    out: list[dict] = []

    def cb(h: int, _: object) -> bool:
        tid, p = win32process.GetWindowThreadProcessId(h)
        if p != pid:
            return True
        style = win32gui.GetWindowLong(h, win32con.GWL_STYLE)
        ex = win32gui.GetWindowLong(h, win32con.GWL_EXSTYLE)
        rect = list(W.frame_bounds(h)) if win32gui.IsWindowVisible(h) else list(win32gui.GetWindowRect(h))
        out.append({
            "hwnd": h, "tid": tid, "z": len(out),
            "cls": win32gui.GetClassName(h), "title": win32gui.GetWindowText(h),
            "owner": win32gui.GetWindow(h, win32con.GW_OWNER),
            "visible": bool(win32gui.IsWindowVisible(h)), "iconic": bool(win32gui.IsIconic(h)),
            "cloaked": W.is_cloaked(h), "enabled": bool(user32.IsWindowEnabled(h)),
            "rect": rect, "style": hex(style), "exstyle": hex(ex),
            "caption": bool(style & win32con.WS_CAPTION == win32con.WS_CAPTION),
            "popup": bool(style & win32con.WS_POPUP), "toolwindow": bool(ex & win32con.WS_EX_TOOLWINDOW),
        })
        return True

    win32gui.EnumWindows(cb, None)
    return out


def visible_frames(pid: int) -> list[dict]:
    return [w for w in all_windows_of_pid(pid) if w["visible"] and w["title"] and w["caption"]]


def wait_title(pid: int, sub: str, timeout: float = 90) -> int:
    t0 = time.time()
    while time.time() - t0 < timeout:
        for w in all_windows_of_pid(pid):
            if w["visible"] and sub in w["title"] and w["caption"]:
                return w["hwnd"]
        time.sleep(0.25)
    raise TimeoutError(sub)


def chord(*vks: int, ext: tuple[bool, ...] | None = None) -> None:
    ext = ext or tuple(False for _ in vks)
    for vk, e in zip(vks, ext):
        I._send([I._key_input(vk, e, False)])
        time.sleep(0.02)
    for vk, e in reversed(list(zip(vks, ext))):
        I._send([I._key_input(vk, e, True)])
        time.sleep(0.02)


def focus(hwnd: int) -> bool:
    ok = W.bring_to_front(hwnd)
    time.sleep(0.15)
    return ok


def place(hwnd: int, x: int, y: int, w: int, h: int) -> None:
    W.resize_window(hwnd, x, y, w, h)


def cmd_start(a: argparse.Namespace) -> None:
    spec = json.load(open(a.env, encoding="utf-8"))
    exe = str(Path(spec["root"]) / "bin" / "kicad.exe")
    pro = a.pro
    for lck in Path(pro).parent.glob("~*.lck"):
        lck.unlink()
    proc = subprocess.Popen([exe, pro], env=spec["env"], cwd=os.path.dirname(pro))
    pid = proc.pid
    mgr = wait_title(pid, "KiCad", 90)
    time.sleep(2.0)
    place(mgr, *a.mgr)
    time.sleep(0.3)
    sess = {"pid": pid, "exe": exe, "pro": pro, "manager": mgr, "t_launch": time.time()}
    # --- Schematic editor from the manager (Ctrl+E)
    focus(mgr)
    t = time.time()
    chord(0x11, ord("E"))
    sch = wait_title(pid, "Schematic Editor", 90)
    sess["t_sch_open_s"] = round(time.time() - t, 2)
    time.sleep(2.5)
    place(sch, *a.sch)
    # --- PCB editor from the manager (Ctrl+P)
    focus(mgr)
    t = time.time()
    chord(0x11, ord("P"))
    pcb = wait_title(pid, "PCB Editor", 90)
    sess["t_pcb_open_s"] = round(time.time() - t, 2)
    time.sleep(3.0)
    place(pcb, *a.pcb)
    time.sleep(0.5)
    sess.update({"sch": sch, "pcb": pcb, "windows": all_windows_of_pid(pid)})
    Path(a.out).write_text(json.dumps(sess, indent=1), encoding="utf-8")
    print(json.dumps({k: v for k, v in sess.items() if k != "windows"}, indent=1))
    for w in visible_frames(pid):
        print(f"  hwnd={w['hwnd']} tid={w['tid']} owner={w['owner']} cls={w['cls']} rect={w['rect']} title={w['title']!r}")


def cmd_windows(a: argparse.Namespace) -> None:
    pid = a.pid or json.load(open(a.out))["pid"]
    wins = all_windows_of_pid(pid)
    sel = wins if a.all else [w for w in wins if w["visible"]]
    for w in sel:
        print(f"hwnd={w['hwnd']} tid={w['tid']} owner={w['owner']} vis={int(w['visible'])} icon={int(w['iconic'])} "
              f"en={int(w['enabled'])} cls={w['cls']} rect={w['rect']} title={w['title']!r}")
    if a.json:
        Path(a.json).write_text(json.dumps(sel, indent=1), encoding="utf-8")


def cmd_stop(a: argparse.Namespace) -> None:
    try:
        pid = json.load(open(a.out))["pid"]
    except Exception:  # noqa: BLE001
        return
    import psutil

    try:
        p = psutil.Process(pid)
        for c in p.children(recursive=True):
            c.kill()
        p.kill()
        print("killed", pid)
    except psutil.Error as e:
        print("stop:", e)


def main() -> int:
    W.set_dpi_aware()
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["start", "windows", "stop"])
    ap.add_argument("--env", default=str(HERE / "env10.json"))
    ap.add_argument("--pro", default=str(HERE / "ecc83" / "ecc83-pp.kicad_pro"))
    ap.add_argument("--out", default=str(HERE / "session.json"))
    ap.add_argument("--pid", type=int, default=0)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--json", default="")
    ap.add_argument("--mgr", type=int, nargs=4, default=[3860, 60, 1000, 700])
    ap.add_argument("--sch", type=int, nargs=4, default=[3860, 820, 1800, 1200])
    ap.add_argument("--pcb", type=int, nargs=4, default=[5700, 60, 1920, 1080])
    a = ap.parse_args()
    {"start": cmd_start, "windows": cmd_windows, "stop": cmd_stop}[a.cmd](a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
