"""Raw legacy 2025-06-18 client over streamable HTTP with httpx (no SDK client code)."""
import httpx, json, base64
URL = "http://127.0.0.1:8765/mcp"
H = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
PNG_SIG = bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A])
def parse(resp):
    ct = resp.headers.get("content-type","")
    if "text/event-stream" in ct:
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
    return resp.json()
with httpx.Client(timeout=60) as c:
    r = c.post(URL, headers=H, json={"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"raw-http","version":"0"}}})
    print("HTTP", r.status_code, "content-type:", r.headers.get("content-type"))
    sid = r.headers.get("mcp-session-id"); print("mcp-session-id:", sid)
    init = parse(r); print("initialize ->", json.dumps(init)[:300])
    pv = init["result"]["protocolVersion"]
    H2 = {**H, "Mcp-Session-Id": sid, "MCP-Protocol-Version": pv}
    r = c.post(URL, headers=H2, json={"jsonrpc":"2.0","method":"notifications/initialized"}); print("initialized notif HTTP", r.status_code)
    r = c.post(URL, headers=H2, json={"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"render_board","arguments":{"width":100,"height":80}}})
    print("tools/call HTTP", r.status_code, r.headers.get("content-type"))
    res = parse(r)["result"]
    print("keys ->", list(res.keys()))
    for b in res["content"]:
        if b["type"]=="image":
            raw = base64.b64decode(b["data"]); print("image mimeType=", b["mimeType"], "bytes=", len(raw), "PNG=", raw[:8]==PNG_SIG)
    print("structuredContent ->", res["structuredContent"])
    r = c.delete(URL, headers=H2); print("DELETE session HTTP", r.status_code)
