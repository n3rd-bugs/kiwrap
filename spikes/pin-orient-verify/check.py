import json, re, sys
LX, LY = 2.54, 5.08
combos = {}
for line in open("combos.txt"):
    m = re.match(r"U(\d+) rot=(\d+) mirror=(\w+) X=([\d.]+) Y=([\d.]+)", line)
    combos[f"U{m[1]}"] = (int(m[2]), None if m[3]=="None" else m[3], float(m[4]), float(m[5]))
def claim(rot, mir, X, Y):
    dx, dy = {0:(LX,-LY), 90:(-LY,-LX), 180:(-LX,LY), 270:(LY,LX)}[rot]
    if mir == 'x': dy = -dy
    if mir == 'y': dx = -dx
    return (round(X+dx,4), round(Y+dy,4))
rep = json.load(open(sys.argv[1]))
scale = float(sys.argv[2]) if len(sys.argv)>2 else 1.0
seen = {}
for sh in rep["sheets"]:
    for v in sh["violations"]:
        if v["type"] != "pin_not_connected": continue
        it = v["items"][0]
        ref = it["description"].split()[1]
        seen[ref] = (round(it["pos"]["x"]*scale,4), round(it["pos"]["y"]*scale,4))
ok = True
for ref in sorted(combos, key=lambda r:int(r[1:])):
    rot, mir, X, Y = combos[ref]
    exp = claim(rot, mir, X, Y)
    got = seen.get(ref)
    match = got == exp
    ok &= match
    print(f"{ref:4} rot={rot:3} mirror={str(mir):4} sym=({X},{Y}) claim={exp} kicad={got} {'OK' if match else 'MISMATCH'}")
print("ALL MATCH" if ok else "SOME MISMATCH")
