import base64, json, time, urllib.request, sys, uuid
BASE = "http://127.0.0.1:37864/v1"
HDR = {"Content-Type": "application/json", "Freerouting-Profile-ID": str(uuid.uuid4()), "Freerouting-Environment-Host": "KiCad/10.0"}
def call(method, path, body=None):
    req = urllib.request.Request(BASE + path, method=method, headers=HDR, data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
dsn = open(sys.argv[1], "rb").read()
st, s = call("POST", "/sessions/create", {"host_name": "wrapper", "host_version": "0.1"}); print("create session:", st, s)
sid = s["id"]
st, j = call("POST", "/jobs/enqueue", {"session_id": sid, "name": "tiny", "priority": "NORMAL"}); print("enqueue:", st, j)
jid = j["id"]
st, r = call("POST", f"/jobs/{jid}/settings", {"max_passes": 20, "via_costs": 50}); print("settings:", st, str(r)[:200])
st, r = call("POST", f"/jobs/{jid}/input", {"filename": "tiny.dsn", "data": base64.b64encode(dsn).decode()}); print("input:", st, str(r)[:300])
st, r = call("PUT", f"/jobs/{jid}/start"); print("start:", st, str(r)[:200])
for _ in range(60):
    st, r = call("GET", f"/jobs/{jid}")
    print("poll:", st, r.get("state") if isinstance(r, dict) else r)
    if isinstance(r, dict) and r.get("state") in ("COMPLETED", "FAILED", "CANCELLED", "TERMINATED"): break
    time.sleep(1)
st, out = call("GET", f"/jobs/{jid}/output"); print("output:", st, {k: (v if k != "data" else f"<{len(v)} b64 chars>") for k, v in out.items()} if isinstance(out, dict) else out)
if isinstance(out, dict) and out.get("data"):
    open(sys.argv[2], "wb").write(base64.b64decode(out["data"])); print("wrote", sys.argv[2])
st, drc = call("GET", f"/jobs/{jid}/drc"); print("drc:", st, str(drc)[:500])
st, drcs = call("GET", f"/jobs/{jid}/drc/summary"); print("drc summary:", st, str(drcs)[:500])
