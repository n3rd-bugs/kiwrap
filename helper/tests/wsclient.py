"""Minimal RFC 6455 WebSocket client on the standard library (no `websockets` dependency in the
kiwrap workspace). Enough for the helper protocol: text + binary frames, ping/pong, close.
Client frames are masked as the RFC requires; no extensions are offered (the helper never
negotiates permessage-deflate either, docs/DISPLAY.md 7.2)."""

from __future__ import annotations

import base64
import contextlib
import json
import os
import socket
import struct
import time
from typing import Any


class WsError(RuntimeError):
    pass


class WsClient:
    def __init__(self, url: str, timeout_s: float = 5.0) -> None:
        assert url.startswith("ws://")
        rest = url[5:]
        hostport, _, path = rest.partition("/")
        host, _, port = hostport.partition(":")
        self.sock = socket.create_connection((host, int(port or 80)), timeout=timeout_s)
        self.sock.settimeout(timeout_s)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET /{path} HTTP/1.1\r\nHost: {hostport}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(req.encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise WsError("connection closed during handshake")
            head += chunk
        status_line, _, rest = head.partition(b"\r\n")
        if b" 101 " not in status_line:
            raise WsError(f"handshake refused: {status_line.decode(errors='replace')}")
        self._buf = head.split(b"\r\n\r\n", 1)[1]
        self.closed = False

    # ---------------------------------------------------------------- frames
    def _recv_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(max(65536, n - len(self._buf)))
            if not chunk:
                raise WsError("connection closed")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def recv(self, timeout_s: float | None = None) -> tuple[str, Any]:
        """Returns ('text', str) | ('binary', bytes) | ('close', code). Pings are answered here.
        Reassembles fragmented messages (a large frame is split into an initial opcode 1/2 with
        FIN=0 followed by opcode 0 continuation frames): tokio-tungstenite fragments the ~450 kB
        screenshot base64 and the JPEG stream frames, so the client must join them."""
        frag_op = 0
        frag_buf = bytearray()
        while True:
            # Only the first header byte may time out cleanly (we are at a frame boundary and
            # self._buf is empty or holds whole leftover frames). Once a frame has started, read it
            # to completion with a generous timeout so a caller's short poll cannot abort mid-body
            # and desync the stream.
            if timeout_s is not None:
                self.sock.settimeout(timeout_s)
            first = self._recv_exact(1)
            self.sock.settimeout(max(timeout_s or 0, 10.0))
            b0 = first[0]
            b1 = self._recv_exact(1)[0]
            fin = b0 & 0x80
            opcode = b0 & 0x0F
            masked = b1 & 0x80
            ln = b1 & 0x7F
            if ln == 126:
                ln = struct.unpack(">H", self._recv_exact(2))[0]
            elif ln == 127:
                ln = struct.unpack(">Q", self._recv_exact(8))[0]
            mask = self._recv_exact(4) if masked else b""
            payload = self._recv_exact(ln)
            if mask:
                payload = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
            # control frames (8/9/A) may be interleaved between data fragments
            if opcode == 0x8:
                self.closed = True
                code = struct.unpack(">H", payload[:2])[0] if len(payload) >= 2 else 1005
                return "close", code
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            if opcode in (0x1, 0x2):
                frag_op = opcode
                frag_buf = bytearray(payload)
            elif opcode == 0x0:
                frag_buf += payload
            else:
                raise WsError(f"unexpected opcode {opcode}")
            if not fin:
                continue
            if frag_op == 0x1:
                return "text", bytes(frag_buf).decode("utf-8")
            return "binary", bytes(frag_buf)

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        mask = os.urandom(4)
        head = bytes([0x80 | opcode])
        ln = len(payload)
        if ln < 126:
            head += bytes([0x80 | ln])
        elif ln < 65536:
            head += bytes([0x80 | 126]) + struct.pack(">H", ln)
        else:
            head += bytes([0x80 | 127]) + struct.pack(">Q", ln)
        masked = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
        self.sock.sendall(head + mask + masked)

    def send_text(self, text: str) -> None:
        self._send_frame(0x1, text.encode("utf-8"))

    def send_json(self, obj: dict) -> None:
        self.send_text(json.dumps(obj))

    def close(self) -> None:
        if not self.closed:
            with contextlib.suppress(OSError):
                self._send_frame(0x8, struct.pack(">H", 1000))
            self.closed = True
        with contextlib.suppress(OSError):
            self.sock.close()

    # ---------------------------------------------------------------- helpers
    def recv_json(self, timeout_s: float = 5.0, want_type: str | None = None, want_id: Any = None) -> dict:
        """Next JSON text frame (optionally of a given type / id); binary frames are skipped."""
        deadline = time.monotonic() + timeout_s
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError(f"no {want_type or 'text'} frame within {timeout_s}s")
            kind, data = self.recv(timeout_s=left)
            if kind != "text":
                if kind == "close":
                    raise WsError(f"closed ({data})")
                continue
            obj = json.loads(data)
            if want_type and obj.get("type") != want_type:
                continue
            if want_id is not None and obj.get("id") != want_id:
                continue
            return obj

    def request(self, obj: dict, timeout_s: float = 5.0) -> dict:
        rid = obj.setdefault("id", int(time.monotonic_ns() % 1_000_000_007))
        self.send_json(obj)
        return self.recv_json(timeout_s=timeout_s, want_id=rid)
