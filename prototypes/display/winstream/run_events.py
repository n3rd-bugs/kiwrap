"""Run the winstream server with the message-only injector (`winstream/inject_events.py`).

    <venv>\\python.exe run_events.py --env env-build.json --pcb <scratch>\\ecc83\\ecc83-pp.kicad_pcb \
        --size 1920x1080 --pos 5740,1060 --port 8766 [other winstream.server options]

Set WINSTREAM_ALLOW_CLOAKED=1 when the KiCad runs with KICAD_INPUT_FROM_EVENTS=cloak (invisible on
the desktop, still captured). Keep the window ON-SCREEN: DWM stops composing fully off-screen
windows and WGC then delivers no frames (measured; PrintWindow polling is the only fallback, 87 ms
per 1080p frame).

The KiCad in `--env` must be built from the fork branch `mr/display-input` and started with
`KICAD_INPUT_FROM_EVENTS=1` in its environment (put it in the env JSON, see
`bench/make_build_env.py`); with a stock KiCad this injector degrades to the plain PostMessage
behaviour measured in docs/research/display/winstream.md (clicks select nothing, dialogs ignore Esc).

This wrapper only swaps the injector factory; `winstream/server.py` is untouched so that the
multi-window work in progress there is not disturbed. Fold it into `--inject events` later.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from winstream import inject as I  # noqa: E402
from winstream import server  # noqa: E402
from winstream import windows as W  # noqa: E402
from winstream.inject_events import EventInjector  # noqa: E402

_orig_make_injector = I.make_injector

# KICAD_INPUT_FROM_EVENTS=cloak makes the patched KiCad cloak its top-level windows (DWMWA_CLOAK:
# not drawn on the desktop, still composed by DWM, still captured by WGC). The tracker normally
# skips cloaked windows (that is how windows on other virtual desktops look), so allow them here.
if os.environ.get("WINSTREAM_ALLOW_CLOAKED"):
    W.is_cloaked = lambda hwnd: False


def make_injector(kind: str, main_hwnd: int, pid: int):
    if kind == "postmessage":
        return EventInjector(main_hwnd, pid)
    return _orig_make_injector(kind, main_hwnd, pid)


I.make_injector = make_injector

if __name__ == "__main__":
    argv = [a for a in sys.argv[1:] if not a.startswith("--inject")]
    sys.exit(server.main([*argv, "--inject", "postmessage"]))
