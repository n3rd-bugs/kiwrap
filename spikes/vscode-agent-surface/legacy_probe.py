"""Speak raw legacy JSON-RPC (initialize handshake, protocolVersion 2025-06-18) to the 2.x server over stdio,
to check that older MCP clients (VS Code/Cursor/Claude Code builds that pre-date 2026-07-28) still work."""
import json, subprocess, sys
from pathlib import Path
HERE = Path(__file__).parent
p = subprocess.Popen([sys.executable, str(HERE/"kicad_probe_server.py")], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
def send(obj):
    p.stdin.write(json.dumps(obj)+"\n"); p.stdin.flush()
def recv():
    line = p.stdout.readline()
    return json.loads(line) if line.strip() else None
for ver in ["2025-06-18"]:
    send({"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":ver,"capabilities":{},"clientInfo":{"name":"legacy-probe","version":"0"}}})
    r = recv(); print("initialize ->", json.dumps(r)[:400])
    send({"jsonrpc":"2.0","method":"notifications/initialized"})
    send({"jsonrpc":"2.0","id":2,"method":"tools/list"})
    r = recv(); print("tools/list ->", [t["name"] for t in r["result"]["tools"]], "extra keys:", [k for k in r["result"] if k!="tools"])
    send({"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"kicad_version","arguments":{"major":"9.0"}}})
    r = recv(); print("tools/call ->", json.dumps(r["result"])[:300])
# also try the new stateless discover
send({"jsonrpc":"2.0","id":4,"method":"server/discover","params":{}})
r = recv(); print("server/discover ->", json.dumps(r)[:600])
p.stdin.close(); p.terminate()
