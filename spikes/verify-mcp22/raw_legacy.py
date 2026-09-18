import json, sys, subprocess, urllib.request, base64

INIT = {"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"raw-legacy","version":"0"}}}
INITD = {"jsonrpc":"2.0","method":"notifications/initialized"}
CALL = {"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"render","arguments":{"name":"raw"}}}

def summarize(label, res):
    r = res["result"]
    kinds = [c["type"] for c in r["content"]]
    img = [c for c in r["content"] if c["type"]=="image"][0]
    magic = base64.b64decode(img["data"])[:8] == bytes([0x89,0x50,0x4E,0x47,0x0D,0x0A,0x1A,0x0A])
    print(f"[{label}] tools/call wire keys={sorted(r.keys())} content types={kinds} mimeType={img.get('mimeType')} png={magic} structuredContent={r.get('structuredContent')}")

# ---- stdio ----
p = subprocess.Popen([sys.executable, "server.py", "stdio"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding="utf-8")
for m in (INIT, INITD, CALL):
    p.stdin.write(json.dumps(m) + "\n"); p.stdin.flush()
init = json.loads(p.stdout.readline())
print("[stdio raw 2025-06-18] initialize ->", init["result"]["protocolVersion"], "serverInfo=", init["result"]["serverInfo"])
call = json.loads(p.stdout.readline())
summarize("stdio raw 2025-06-18", call)
p.stdin.close(); p.terminate()

# ---- streamable http ----
url = "http://127.0.0.1:8765/mcp"
def post(body, sid=None, pv=None):
    h = {"Content-Type":"application/json","Accept":"application/json, text/event-stream"}
    if sid: h["Mcp-Session-Id"] = sid
    if pv: h["MCP-Protocol-Version"] = pv
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=h, method="POST")
    try:
        resp = urllib.request.urlopen(req)
    except urllib.error.HTTPError as e:
        print("HTTP", e.code, e.read()[:300]); raise
    ct = resp.headers.get("Content-Type",""); sid = resp.headers.get("Mcp-Session-Id"); data = resp.read().decode()
    if "text/event-stream" in ct:
        msgs = [json.loads(l[5:]) for l in data.splitlines() if l.startswith("data:")]
    elif data.strip():
        msgs = [json.loads(data)]
    else:
        msgs = []
    return resp.status, ct, sid, msgs
st, ct, sid, msgs = post(INIT)
print(f"[http raw 2025-06-18] initialize -> HTTP {st} ct={ct} session={bool(sid)} protocolVersion={msgs[0]['result']['protocolVersion']}")
st, ct, _, _ = post(INITD, sid, "2025-06-18"); print(f"[http raw 2025-06-18] initialized notif -> HTTP {st}")
st, ct, _, msgs = post(CALL, sid, "2025-06-18")
summarize(f"http raw 2025-06-18 (HTTP {st}, {ct.split(';')[0]})", msgs[-1])
