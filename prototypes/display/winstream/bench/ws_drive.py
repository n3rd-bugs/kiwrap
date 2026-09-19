"""Quick functional driver over the winstream WebSocket (no browser): sends input JSON, grabs /shot
PNGs and window lists, and records the real cursor/foreground before and after.

    python ws_drive.py <port> <outdir> <script.json>
script: list of {"a": "move|click|down|up|drag|wheel|key|keydown|keyup|type|sleep|shot|windows|cursor|stats|inj"}
"""
from __future__ import annotations

import asyncio
import json
import sys
import time

import aiohttp
import win32gui

port, out, script = int(sys.argv[1]), sys.argv[2], sys.argv[3]
base = f"http://127.0.0.1:{port}"
actions = json.load(open(script, encoding="utf-8"))
results: dict = {}


async def main() -> None:
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect(f"{base}/ws") as ws:
            hello = json.loads((await ws.receive()).data)
            results["hello"] = {k: hello.get(k) for k in ("main", "w", "h", "dpi", "inject")}
            main_hwnd = hello["main"]

            async def send(ev: dict) -> None:
                await ws.send_str(json.dumps(ev))

            async def shot(name: str) -> None:
                async with s.get(f"{base}/shot?hwnd={main_hwnd}") as r:
                    data = await r.read()
                open(f"{out}/{name}.png", "wb").write(data)

            async def get(path: str):
                async with s.get(f"{base}{path}") as r:
                    return await r.json()

            def fg() -> dict:
                h = win32gui.GetForegroundWindow()
                return {"cursor": win32gui.GetCursorPos(), "foreground": h, "title": win32gui.GetWindowText(h)[:60]}

            results["before"] = fg()
            for i, act in enumerate(actions):
                a = act["a"]
                t0 = time.time()
                if a == "move":
                    await send({"t": "move", "x": act["x"], "y": act["y"]})
                elif a in ("down", "up"):
                    await send({"t": a, "x": act["x"], "y": act["y"], "button": act.get("button", 0)})
                elif a == "click":
                    await send({"t": "move", "x": act["x"], "y": act["y"]})
                    await asyncio.sleep(0.03)
                    await send({"t": "down", "x": act["x"], "y": act["y"], "button": act.get("button", 0)})
                    await asyncio.sleep(0.05)
                    await send({"t": "up", "x": act["x"], "y": act["y"], "button": act.get("button", 0)})
                elif a == "dblclick":
                    for _ in range(2):
                        await send({"t": "down", "x": act["x"], "y": act["y"], "button": 0})
                        await asyncio.sleep(0.04)
                        await send({"t": "up", "x": act["x"], "y": act["y"], "button": 0})
                        await asyncio.sleep(0.08)
                elif a == "drag":
                    n = act.get("steps", 20)
                    x1, y1, x2, y2 = act["x1"], act["y1"], act["x2"], act["y2"]
                    await send({"t": "move", "x": x1, "y": y1})
                    await asyncio.sleep(0.03)
                    await send({"t": "down", "x": x1, "y": y1, "button": act.get("button", 0)})
                    await asyncio.sleep(0.06)
                    for k in range(1, n + 1):
                        await send({"t": "move", "x": x1 + (x2 - x1) * k / n, "y": y1 + (y2 - y1) * k / n})
                        await asyncio.sleep(act.get("step_ms", 15) / 1000)
                    await asyncio.sleep(0.06)
                    await send({"t": "up", "x": x2, "y": y2, "button": act.get("button", 0)})
                elif a == "wheel":
                    await send({"t": "wheel", "x": act["x"], "y": act["y"], "dy": act["dy"], "dx": 0})
                elif a == "key":
                    await send({"t": "key", "code": act["code"], "down": True})
                    await asyncio.sleep(0.03)
                    await send({"t": "key", "code": act["code"], "down": False})
                elif a == "keydown":
                    await send({"t": "key", "code": act["code"], "down": True})
                elif a == "keyup":
                    await send({"t": "key", "code": act["code"], "down": False})
                elif a == "chord":
                    for c in act["codes"]:
                        await send({"t": "key", "code": c, "down": True})
                        await asyncio.sleep(0.02)
                    for c in reversed(act["codes"]):
                        await send({"t": "key", "code": c, "down": False})
                        await asyncio.sleep(0.02)
                elif a == "type":
                    for ch in act["text"]:
                        code = "Space" if ch == " " else (f"Key{ch.upper()}" if ch.isalpha() else f"Digit{ch}" if ch.isdigit() else ch)
                        await send({"t": "key", "code": code, "down": True})
                        await asyncio.sleep(0.02)
                        await send({"t": "key", "code": code, "down": False})
                        await asyncio.sleep(0.03)
                elif a == "sleep":
                    await asyncio.sleep(act["ms"] / 1000)
                elif a == "shot":
                    await shot(act["name"])
                elif a == "waitwin":
                    # poll until a window whose title contains act["title"] exists (or is gone with act["gone"])
                    t1 = time.time()
                    found = None
                    while time.time() - t1 < act.get("timeout", 8):
                        st = await get("/stats")
                        hit = next((w for w in st["windows"] if act["title"] in w["title"]), None)
                        if (hit and not act.get("gone")) or (not hit and act.get("gone")):
                            found = hit
                            break
                        await asyncio.sleep(0.02)
                    results[act["name"]] = {"ms": round(1000 * (time.time() - t1)), "rect": found and found["rect"], "hwnd": found and found["hwnd"]}
                elif a == "clickwin":
                    # click at (dx, dy) inside the top-most window whose title contains act["title"]
                    st = await get("/stats")
                    mx, my = st["main_rect"][0], st["main_rect"][1]
                    target = next((w for w in st["windows"] if act["title"] in w["title"]), None)
                    results[act["name"]] = {"found": bool(target), "rect": target and target["rect"]}
                    if target:
                        x = target["rect"][0] - mx + act["dx"]
                        y = target["rect"][1] - my + act["dy"]
                        await send({"t": "move", "x": x, "y": y})
                        await asyncio.sleep(0.03)
                        await send({"t": "down", "x": x, "y": y, "button": act.get("button", 0)})
                        await asyncio.sleep(0.05)
                        await send({"t": "up", "x": x, "y": y, "button": act.get("button", 0)})
                elif a == "shotall":
                    st = await get("/stats")
                    saved = []
                    for w in st["windows"]:
                        async with s.get(f"{base}/shot?hwnd={w['hwnd']}") as r:
                            if r.status == 200:
                                data = await r.read()
                                fn = f"{out}/{act['name']}_{w['hwnd']}.png"
                                open(fn, "wb").write(data)
                                saved.append({"hwnd": w["hwnd"], "cls": w["cls"], "title": w["title"], "rect": w["rect"], "file": fn, "bytes": len(data)})
                    results[act["name"]] = saved
                elif a == "windows":
                    st = await get("/stats")
                    results[act["name"]] = [{k: w[k] for k in ("hwnd", "cls", "title", "rect")} for w in st["windows"]]
                elif a == "cursor":
                    results[act["name"]] = {**(await get("/cursor")), **fg()}
                elif a == "stats":
                    st = await get("/stats")
                    results[act["name"]] = {k: st.get(k) for k in ("sent_frames", "input_events", "sessions", "cpu", "input_log")}
                elif a == "clipboard":
                    results[act["name"]] = await get("/clipboard")
                print(f"{i:3d} {a:8s} {json.dumps({k: v for k, v in act.items() if k != 'a'})[:70]:72s} {1000*(time.time()-t0):6.1f} ms", flush=True)
            results["after"] = fg()
    json.dump(results, open(f"{out}/results.json", "w"), indent=1, default=str)


asyncio.run(main())
