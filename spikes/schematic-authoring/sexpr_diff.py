"""Semantic diff of two KiCad s-expression files (order-insensitive).
Usage: python sexpr_diff.py a.kicad_sch b.kicad_sch
Prints top-level nodes that exist only in A / only in B after canonicalisation
(children sorted, numbers normalised), so pure re-ordering/re-formatting is ignored.
"""
import sys, re

def tokenize(s):
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c.isspace():
            i += 1
        elif c in '()':
            yield c; i += 1
        elif c == '"':
            j = i + 1; buf = []
            while s[j] != '"':
                if s[j] == '\\':
                    buf.append(s[j:j+2]); j += 2
                else:
                    buf.append(s[j]); j += 1
            yield ('str', ''.join(buf)); i = j + 1
        else:
            j = i
            while j < n and not s[j].isspace() and s[j] not in '()':
                j += 1
            yield ('atom', s[i:j]); i = j

def parse(s):
    stack = [[]]
    for t in tokenize(s):
        if t == '(':
            stack.append([])
        elif t == ')':
            node = stack.pop(); stack[-1].append(node)
        else:
            kind, val = t
            if kind == 'atom':
                try:
                    fv = float(val)
                    val = ('num', round(fv, 6))
                except ValueError:
                    val = ('atom', val)
            else:
                val = ('str', val)
            stack[-1].append(val)
    return stack[0][0]

def canon(node):
    if isinstance(node, list):
        if not node:
            return ('L',)
        head = node[0]
        kids = [canon(k) for k in node[1:]]
        # keep first atoms ordered, sort the sub-lists (order independent)
        atoms = [k for k in kids if not isinstance(k, tuple) or k[0] != 'L']
        lists = sorted([k for k in kids if isinstance(k, tuple) and k[0] == 'L'], key=repr)
        return ('L', canon(head)) + tuple(atoms) + tuple(lists)
    return node

def top_level(tree):
    out = []
    for n in tree[1:]:
        out.append(canon(n))
    return out

def show(c, depth=0, maxlen=200):
    return repr(c)[:maxlen]

if __name__ == '__main__':
    a = parse(open(sys.argv[1], encoding='utf-8').read())
    b = parse(open(sys.argv[2], encoding='utf-8').read())
    ta, tb = top_level(a), top_level(b)
    sa, sb = set(ta), set(tb)
    print("A top-level:", len(ta), " B top-level:", len(tb))
    only_a = [x for x in ta if x not in sb]
    only_b = [x for x in tb if x not in sa]
    print("--- only in A (%d):" % len(only_a))
    for x in only_a:
        print("  ", show(x))
    print("--- only in B (%d):" % len(only_b))
    for x in only_b:
        print("  ", show(x))
    # dig into lib_symbols if they differ
    def find(tl, key):
        for x in tl:
            if x[1] == ('atom', key):
                return x
    la, lb = find(ta, 'lib_symbols'), find(tb, 'lib_symbols')
    if la != lb and la and lb:
        ka = set(la[2:]); kb = set(lb[2:])
        print("--- lib_symbols entries only in A:", [x[2] for x in la[2:] if x not in kb])
        print("--- lib_symbols entries only in B:", [x[2] for x in lb[2:] if x not in ka])
        # deeper: for each symbol compare children
        da = {x[2]: x for x in la[2:]}; db = {x[2]: x for x in lb[2:]}
        for k in da:
            if k in db and da[k] != db[k]:
                ca = set(da[k][3:]); cb = set(db[k][3:])
                print("  symbol", k, "children only in A:", [show(x, maxlen=160) for x in da[k][3:] if x not in cb])
                print("  symbol", k, "children only in B:", [show(x, maxlen=160) for x in db[k][3:] if x not in ca])
    # symbols instances
    for key in ('symbol', 'wire', 'label', 'global_label', 'junction', 'sheet'):
        xa = [x for x in ta if x[1] == ('atom', key)]
        xb = [x for x in tb if x[1] == ('atom', key)]
        oa = [x for x in xa if x not in set(xb)]
        ob = [x for x in xb if x not in set(xa)]
        if oa or ob:
            print("=== %s differences: %d only-A, %d only-B" % (key, len(oa), len(ob)))
            for x, y in zip(sorted(oa, key=repr), sorted(ob, key=repr)):
                ca = set(x[2:]); cb = set(y[2:])
                print("   A-only children:", [show(z, maxlen=200) for z in x[2:] if z not in cb])
                print("   B-only children:", [show(z, maxlen=200) for z in y[2:] if z not in ca])
