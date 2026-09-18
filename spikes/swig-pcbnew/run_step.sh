#!/bin/bash
# usage: run_step.sh <kicad_ver> <outdir> <skip_steps_csv> [timeout_s]
KV=$1; OUT=$2; SKIP=$3; T=${4:-90}
rm -rf "$OUT"
OUTLINE_MODE=inplace SKIP_STEPS="$SKIP" timeout -k 5 $T "/c/Program Files/KiCad/$KV/bin/python.exe" -u -X faulthandler test_swig_board.py "$OUT" $KV 2>&1
echo "exit=$?"
