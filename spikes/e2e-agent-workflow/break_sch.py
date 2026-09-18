# remove first N (wire ...) blocks from a .kicad_sch to provoke ERC errors
import sys, re
src, dst, n = sys.argv[1], sys.argv[2], int(sys.argv[3])
t = open(src, encoding='utf-8').read()
out = t
for _ in range(n):
    i = out.find('\n\t(wire')
    if i < 0: break
    j = i + 1; depth = 0
    k = j
    while True:
        c = out[k]
        if c == '(': depth += 1
        elif c == ')':
            depth -= 1
            if depth == 0: break
        k += 1
    out = out[:i] + out[k+1:]
open(dst, 'w', encoding='utf-8').write(out)
print("removed", n, "wires")
