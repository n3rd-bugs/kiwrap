"""Minimal OutputDebugString listener (what DebugView does): prints `pid: text` lines for the given
pid (or all). python dbgview.py <pid|0> <outfile>"""
import ctypes
import ctypes.wintypes as wt
import mmap
import struct
import sys
import time

k32 = ctypes.windll.kernel32
want_pid, outfile = int(sys.argv[1]), sys.argv[2]
EVENT_ALL_ACCESS = 0x1F0003
buffer_ready = k32.CreateEventW(None, False, False, "DBWIN_BUFFER_READY")
data_ready = k32.CreateEventW(None, False, False, "DBWIN_DATA_READY")
mm = mmap.mmap(-1, 4096, "DBWIN_BUFFER")
k32.SetEvent(buffer_ready)
out = open(outfile, "a", encoding="utf-8", buffering=1)
print("listening", flush=True)
while True:
    if k32.WaitForSingleObject(data_ready, 500) == 0:
        mm.seek(0)
        pid = struct.unpack("<I", mm.read(4))[0]
        raw = mm.read(4092)
        text = raw.split(b"\0", 1)[0].decode("utf-8", "replace").rstrip("\r\n")
        if not want_pid or pid == want_pid:
            out.write(f"{time.time() % 1000:8.3f} {pid} {text}\n")
        k32.SetEvent(buffer_ready)
