import re, uuid
LX, LY = 2.54, 5.08
def claim(rot, mir, X, Y):
    dx, dy = {0:(LX,-LY), 90:(-LY,-LX), 180:(-LX,LY), 270:(LY,LX)}[rot]
    if mir == 'x': dy = -dy
    if mir == 'y': dx = -dx
    return (round(X+dx,4), round(Y+dy,4))
src = open("pinorient.kicad_sch").read()
extra = []
for line in open("combos.txt"):
    m = re.match(r"U(\d+) rot=(\d+) mirror=(\w+) X=([\d.]+) Y=([\d.]+)", line)
    i, rot, mir, X, Y = int(m[1]), int(m[2]), None if m[3]=="None" else m[3], float(m[4]), float(m[5])
    px, py = claim(rot, mir, X, Y)
    # control for U1: wire at the WRONG (rot180-style) position to prove the test is discriminating
    if i == 1:
        px, py = round(X-LX,4), round(Y+LY,4)
    ex, ey = round(px+12.7,4), py   # wire goes to the right 12.7mm; label there
    extra.append(f'  (wire (pts (xy {px} {py}) (xy {ex} {ey})) (stroke (width 0) (type default)) (uuid "{uuid.uuid4()}"))\n')
    extra.append(f'  (label "N{i}" (at {ex} {ey} 0) (effects (font (size 1.27 1.27)) (justify left bottom)) (uuid "{uuid.uuid4()}"))\n')
src = src.replace('  (sheet_instances', "".join(extra) + '  (sheet_instances')
open("pinorient_wires.kicad_sch","w").write(src)
