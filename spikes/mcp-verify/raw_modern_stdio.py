import json, subprocess, sys, base64
PY = sys.executable
p = subprocess.Popen([PY, "server.py", "stdio"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
def send(o): p.stdin.write(json.dumps(o)+"\n"); p.stdin.flush()
def recv():
    line = p.stdout.readline()
    if not line: raise SystemExit("EOF. stderr:\n"+p.stderr.read())
    return json.loads(line)
META = {"io.modelcontextprotocol/protocolVersion": "2026-07-28", "io.modelcontextprotocol/clientCapabilities": {}}
send({"jsonrpc":"2.0","id":1,"method":"server/discover","params":{"_meta":META}})
print("server/discover ->", json.dumps(recv())[:700])
send({"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"_meta":META,"name":"render_board","arguments":{"width":200,"height":150}}})
r = recv(); res = r.get("result", r)
print("tools/call keys ->", list(res.keys()))
for c in res.get("content", []):
    if c["type"]=="image":
        raw=base64.b64decode(c["data"]); print("image", c.get("mimeType"), len(raw), "PNG=", raw[:8]==b"\x89PNG\r\n\x1a\n")
print("structuredContent ->", res.get("structuredContent"))
# now: does a legacy initialize still work on this same connection after modern use?
send({"jsonrpc":"2.0","id":3,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"raw","version":"0"}}})
print("initialize after modern ->", json.dumps(recv())[:400])
p.stdin.close(); p.wait(timeout=10)
