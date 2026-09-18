SP="/c/Users/usama/AppData/Local/Temp/claude/c--Upwork-KiCad/48a8df66-7213-479f-ab2e-a63b89135016/scratchpad/verify-headless-board"; cd "$SP"
MODE=${1:-inplace}
for V in 10.0 9.0 8.0 7.0; do
  echo "=============== KiCad $V mode=$MODE"
  mkdir -p k$V
  "/c/Program Files/KiCad/$V/bin/python.exe" build_board.py "$SP/k$V/test_$MODE.kicad_pcb" "C:/Program Files/KiCad/$V/share/kicad/footprints" $MODE 2>&1 | tail -6; echo "build exit=${PIPESTATUS[0]}"
  "/c/Program Files/KiCad/$V/bin/kicad-cli.exe" pcb drc --format json --severity-all --all-track-errors -o "k$V/drc_$MODE.json" "k$V/test_$MODE.kicad_pcb" 2>&1 | tail -3; echo "drc exit=${PIPESTATUS[0]}"
  python -c "
import json; d=json.load(open('k$V/drc_$MODE.json'))
print('  JSON: violations',len(d['violations']),'unconnected',len(d['unconnected_items']))
for v in d['violations']+d['unconnected_items']: print('   ', v['type'], v['severity'], v['description'])
"
done
