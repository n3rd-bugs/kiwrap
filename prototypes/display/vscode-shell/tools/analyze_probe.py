"""Summarise a probe.jsonl produced by drive_probe.ps1 into a per-shortcut markdown table.

Usage: python tools/analyze_probe.py <probe.jsonl> [--md out.md]

For every `sent` marker the events until the next marker are attributed to it:
  keydown   - the webview (or nested iframe) saw the DOM keydown: key/code, and `dp` (VS Code's pre-script
              had already called preventDefault on it)
  rescued   - our contributed keybinding fired in the extension host (VS Code resolved the chord)
  blur      - the webview lost focus (a palette / menu / dialog / other editor took it) -> VS Code acted
  disposed  - the panel was closed
  other     - editor/tab changes, control results
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path


def load(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip().lstrip("﻿")
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return rows


def group(rows: list[dict]) -> list[tuple[dict, list[dict]]]:
    groups: list[tuple[dict, list[dict]]] = []
    cur: list[dict] | None = None
    for r in rows:
        if r.get("kind") == "sent":
            cur = []
            groups.append((r, cur))
        elif cur is not None:
            cur.append(r)
    return groups


def summarise_key(sent: dict, evs: list[dict]) -> dict:
    keydowns = [e for e in evs if e.get("kind") == "wv" and e.get("ev") == "keydown"]
    keyups = [e for e in evs if e.get("kind") == "wv" and e.get("ev") == "keyup"]
    rescued = [e for e in evs if e.get("kind") == "rescued"]
    # only count focus loss that happens BEFORE the driver's recovery (Escape keydown / control command)
    cut = len(evs)
    for i, e in enumerate(evs):
        if e.get("kind") == "ctl" or (e.get("kind") == "wv" and e.get("ev") == "keydown" and e.get("key") == "Escape" and not sent.get("name", "").endswith("esc")):
            cut = i
            break
    pre = evs[:cut]
    blur = [e for e in pre if e.get("kind") == "wv" and e.get("ev") == "winblur"]
    panel_inactive = [e for e in pre if e.get("kind") in ("panel", "editor") and e.get("active") is False]
    disposed = [e for e in evs if e.get("kind") == "panel" and e.get("disposed")]
    fwd = [e for e in evs if e.get("kind") == "wv" and e.get("ev") == "forwardedKey"]
    srcs = sorted({e.get("src2", "") for e in keydowns})
    kd = "; ".join(
        f"{e.get('key')}/{e.get('code')}" + ("+c" if e.get("ctrl") else "") + ("+s" if e.get("shift") else "") + ("+a" if e.get("alt") else "") + (" [dp]" if e.get("dp") else "")
        for e in keydowns[:4]
    )
    return {
        "name": sent.get("name"),
        "keydown": kd or "-",
        "n_keydown": len(keydowns),
        "n_keyup": len(keyups),
        "src": ",".join(srcs),
        "rescued": ",".join(str(e.get("key")) for e in rescued) or "",
        "forwarded": len(fwd),
        "blur": bool(blur or panel_inactive),
        "disposed": bool(disposed),
        "dp": any(e.get("dp") for e in keydowns),
    }


def main(argv: list[str]) -> int:
    path = Path(argv[1])
    rows = load(path)
    groups = group(rows)
    act = next((r for r in rows if r.get("kind") == "activated"), {})
    drv = [r for r in rows if r.get("kind") == "driver"]
    out: list[str] = []
    out.append(f"# probe summary: {path}")
    out.append("")
    out.append(f"activated: vscode={act.get('vscode')} rescueKeys={act.get('rescueKeys')} httpPort={act.get('httpPort')} wsPort={act.get('wsPort')}")
    for d in drv:
        out.append(f"driver: {json.dumps({k: v for k, v in d.items() if k not in ('t', 'kind')})}")
    loaded = [r for r in rows if r.get("kind") == "wv" and r.get("ev") == "loaded"]
    for l in loaded:
        out.append(f"loaded: src={l.get('src')}/{l.get('src2')} instance={l.get('instance')} origin={l.get('origin')} dpr={l.get('dpr')} inner={l.get('iw')}x{l.get('ih')} screen={l.get('sw')}x{l.get('sh')} state={l.get('state')} focus={l.get('focus')}")
    out.append("")
    out.append("## keys")
    out.append("")
    out.append("| # | sent | webview keydown (key/code [dp]=VS Code preventDefault) | src | rescued binding | fwd | focus lost | disposed |")
    out.append("|---|---|---|---|---|---|---|---|")
    counts = Counter()
    i = 0
    for sent, evs in groups:
        name = sent.get("name", "")
        if ":" in name and not name.startswith("ctrl") and not name.startswith("alt"):
            continue
        i += 1
        s = summarise_key(sent, evs)
        counts["total"] += 1
        counts["seen"] += 1 if s["n_keydown"] else 0
        counts["blur"] += 1 if s["blur"] else 0
        counts["rescued"] += 1 if s["rescued"] else 0
        out.append(f"| {i} | `{name}` | {s['keydown']} | {s['src']} | {s['rescued']} | {s['forwarded']} | {'YES' if s['blur'] else ''} | {'YES' if s['disposed'] else ''} |")
    out.append("")
    out.append(f"totals: {dict(counts)}")
    out.append("")
    out.append("## other tests")
    out.append("")
    for sent, evs in groups:
        name = sent.get("name", "")
        if not (":" in name and not name.startswith("ctrl") and not name.startswith("alt")) and name not in ("pointerlock", "fullscreen-element"):
            continue
        out.append(f"### {name}")
        for e in evs:
            k = e.get("kind")
            if k == "wv" and e.get("ev") == "pointermove":
                out.append(f"- pointermove n={e.get('n')} last=({e.get('x')},{e.get('y')}) mx={e.get('mx')} my={e.get('my')} buttons={e.get('buttons')} locked={e.get('locked')}")
            elif k == "wv" and e.get("ev") in ("keydown", "keyup"):
                out.append(f"- {e.get('ev')} {e.get('key')} src={e.get('src')}/{e.get('src2')} focus={e.get('focus')}")
            elif k in ("wv", "ctl", "panel", "editor", "activeTextEditor", "window", "shot", "driver"):
                d = {kk: vv for kk, vv in e.items() if kk not in ("t", "kind", "ua", "href")}
                out.append(f"- {k}: {json.dumps(d)[:400]}")
        out.append("")
    text = "\n".join(out)
    if "--md" in argv:
        Path(argv[argv.index("--md") + 1]).write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
