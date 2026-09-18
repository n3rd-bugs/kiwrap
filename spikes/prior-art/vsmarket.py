import json, urllib.request, sys
q = sys.argv[1]
body = {"filters":[{"criteria":[{"filterType":8,"value":"Microsoft.VisualStudio.Code"},{"filterType":10,"value":q}],"pageNumber":1,"pageSize":50,"sortBy":4,"sortOrder":0}],"assetTypes":[],"flags":914}
req = urllib.request.Request("https://marketplace.visualstudio.com/_apis/public/gallery/extensionquery", data=json.dumps(body).encode(), headers={"Content-Type":"application/json","Accept":"application/json;api-version=3.0-preview.1","User-Agent":"Mozilla/5.0"})
d = json.load(urllib.request.urlopen(req, timeout=60))
for e in d["results"][0]["extensions"]:
    st = {s["statisticName"]: s["value"] for s in e.get("statistics",[])}
    v = e["versions"][0]
    print(f'{e["publisher"]["publisherName"]}.{e["extensionName"]}\t{v["version"]}\t{v["lastUpdated"][:10]}\tinstalls={int(st.get("install",0))}\t{e["displayName"]}\t{e.get("shortDescription","")[:120]}')
