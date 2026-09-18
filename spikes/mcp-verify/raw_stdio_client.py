"""Raw JSON-RPC over stdio: pretend to be a legacy 2025-06-18 client (and optionally a modern one)."""
import json, subprocess, sys, base64
PY = sys.executable
proto = sys.argv[1] if len(sys.argv) > 1 else "2025-06-18"
p = subprocess.Popen([PY, "server.py", "stdio"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
def send(o): p.stdin.write(json.dumps(o)+"\n"); p.stdin.flush()
def recv():
    line = p.stdout.readline()
    if not line: raise SystemExit("EOF from server. stderr:\n" + p.stderr.read())
    return json.loads(line)
send({"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":proto,"capabilities":{},"clientInfo":{"name":"raw","version":"0"}}})
r = recv(); print("initialize ->", json.dumps(r)[:600])
send({"jsonrpc":"2.0","method":"notifications/initialized"})
send({"jsonrpc":"2.0","id":2,"method":"tools/list"})
r = recv(); print("tools/list ->", json.dumps(r)[:400])
send({"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"render_board","arguments":{"width":320,"height":240}}})
r = recv()
res = r.get("result", r)
print("tools/call keys ->", list(res.keys()) if isinstance(res, dict) else res)
if "content" in res:
    for c in res["content"]:
        if c["type"] == "image":
            raw = base64.b64decode(c["data"]); print("image mimeType=", c.get("mimeType"), "bytes=", len(raw), "PNG sig=", raw[:8] == b"\x89PNG\r\n\x1a\n")
        else: print("content:", c)
    print("structuredContent ->", res.get("structuredContent"))
else:
    print("FULL:", json.dumps(r)[:1500])
p.stdin.close(); p.wait(timeout=10)
