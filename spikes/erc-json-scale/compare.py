import json, re, sys
d = sys.argv[1]
def rpt_positions(path):
    return [(float(a), float(b)) for a,b in re.findall(r'@\(([-\d.]+) mm, ([-\d.]+) mm\)', open(path, encoding='utf-8').read())]
def json_positions(path, kind):
    j = json.load(open(path, encoding='utf-8'))
    out = []
    if kind == 'erc':
        for sh in j['sheets']:
            for v in sh['violations']:
                for i in v['items']:
                    out.append((i['pos']['x'], i['pos']['y']))
    else:
        for k in ('violations', 'unconnected_items', 'schematic_parity'):
            for v in j.get(k, []):
                for i in v['items']:
                    out.append((i['pos']['x'], i['pos']['y']))
    return j.get('kicad_version'), j.get('coordinate_units'), out
for kind in ('erc', 'drc'):
    ver, units, jp = json_positions(f'{d}/{kind}.json', kind)
    rp = rpt_positions(f'{d}/{kind}.rpt')
    print(f'{kind}: kicad_version={ver} units={units} json_items={len(jp)} rpt_items={len(rp)}')
    ratios = set()
    for (jx, jy), (rx, ry) in zip(jp, rp):
        for jv, rv in ((jx, rx), (jy, ry)):
            if abs(jv) > 1e-9:
                ratios.add(round(rv / jv, 3))
    print('   ratio rpt/json distinct values:', sorted(ratios))
    print('   first 3 json:', jp[:3], ' rpt:', rp[:3])
    # ratio vs each other's tail too
    print('   last json:', jp[-1], ' rpt:', rp[-1])
