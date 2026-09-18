#!/bin/bash
# usage: run_fr.sh <tag> <dsn> <pcb> <extra freerouting args...>
MY="C:/Users/usama/AppData/Local/Temp/claude/c--Upwork-KiCad/48a8df66-7213-479f-ab2e-a63b89135016/scratchpad/freerouting-verify"
JAVA="C:/Users/usama/AppData/Local/Temp/claude/c--Upwork-KiCad/48a8df66-7213-479f-ab2e-a63b89135016/scratchpad/autorouting/jre25/jdk-25.0.4.1+1-jre/bin/java.exe"
KPY="C:/Program Files/KiCad/10.0/bin/python.exe"
KCLI="C:/Program Files/KiCad/10.0/bin/kicad-cli.exe"
TAG=$1; DSN=$2; PCB=$3; shift 3
OUT="$MY/runs/$TAG"; mkdir -p "$OUT"
"$JAVA" -jar "$MY/freerouting-2.4.1.jar" --user_data_path="$MY/fr_userdata" -da -de "$DSN" -do "$OUT/routed.ses" --gui.enabled=false --router.result_json="$OUT/fr_result.json" --logging.file.location="$OUT" "$@" > "$OUT/fr_stdout.txt" 2>&1
echo "freerouting exit=$?"
grep -E "Auto-routing stage completed|finished with state|Applied CLI router setting" "$OUT/freerouting.log" | sed 's/.*DEBUG  //; s/.*INFO   //' | tail -8
"$KPY" "$MY/import_ses.py" "$PCB" "$OUT/routed.ses" "$OUT/routed.kicad_pcb"
"$KCLI" pcb drc --format json --severity-all --all-track-errors -o "$OUT/drc.json" "$OUT/routed.kicad_pcb" > "$OUT/drc_stdout.txt" 2>&1
python - "$OUT/drc.json" <<'PY'
import json,sys
from collections import Counter
d=json.load(open(sys.argv[1]))
print("DRC: violations", len(d['violations']), "unconnected", len(d['unconnected_items']), Counter((v['type'],v['severity']) for v in d['violations']))
for v in d['violations'][:3]: print("   e.g.", v['description'][:150])
PY
