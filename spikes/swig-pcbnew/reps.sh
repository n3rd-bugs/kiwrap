#!/bin/bash
# usage: reps.sh <ver> <flags> <n>
v=$1; f=$2; n=$3
export FPLIB="C:/Program Files/KiCad/$v/share/kicad/footprints/Resistor_SMD.pretty"
printf "%s %-45s " $v "$f"
for rep in $(seq 1 $n); do
  ( timeout 60 "/c/Program Files/KiCad/$v/bin/python.exe" -u -X faulthandler bisect2.py "$PWD/bisect8" "$f" >/dev/null 2>&1 ) 2>/dev/null; printf "%s " $?
done; echo
