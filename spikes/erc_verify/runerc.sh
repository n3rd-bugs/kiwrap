K="/c/Program Files/KiCad/10.0/bin/kicad-cli.exe"
f="$1"; "$K" sch erc --format json --severity-all -o "${f%.kicad_sch}.erc.json" "$f" >/dev/null 2>"${f%.kicad_sch}.err"; rc=$?
python - "$f" <<'PY'
import json,sys
f=sys.argv[1]; p=f[:-10]+'.erc.json'
try: d=json.load(open(p,encoding='utf-8'))
except Exception as e: print(f,'NO JSON', open(f[:-10]+'.err').read()[:500]); sys.exit()
v=[x for s in d['sheets'] for x in s['violations']]
print(f, 'violations=',len(v))
for x in v: print('   ',x['severity'],x['type'],'-',x['description'],[(i.get('description'),i.get('pos')) for i in x.get('items',[])])
PY
echo "  stderr: $(head -c 300 ${f%.kicad_sch}.err)"
