"""Validate a generated KiCad project directory with kicad-cli.

Usage: python validate.py <project_dir> [kicad_version 10.0|9.0|8.0|7.0] [--no-upgrade]
Runs: sch erc (json), sch export netlist (kicadsexpr), sch export svg, then copies the .kicad_sch to
<name>.upgraded.kicad_sch and runs `sch upgrade --force` on it and diffs.
Prints a compact summary: ERC counts, nets with nodes, and whether upgrade changed the file.
"""
import sys, os, subprocess, json, re, shutil, glob, difflib

def run(cmd, cwd):
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    return p.returncode, (p.stdout or "") + (p.stderr or "")

def parse_netlist(txt):
    """Real s-expr parse of the kicadsexpr netlist: returns {netname: [(ref, pin), ...]}."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from sexpr_diff import parse
    tree = parse(txt)
    def val(x):
        return x[1] if isinstance(x, tuple) else x
    nets = {}
    def kids(node, key):
        return [k for k in node[1:] if isinstance(k, list) and k and val(k[0]) == key]
    nets_node = kids(tree, 'nets')
    if not nets_node:
        return nets
    for net in kids(nets_node[0], 'net'):
        name = val(kids(net, 'name')[0][1])
        nodes = []
        for nd in kids(net, 'node'):
            ref = val(kids(nd, 'ref')[0][1]); pin = kids(nd, 'pin')[0][1]
            pin = val(pin)
            if isinstance(pin, float): pin = str(int(pin))
            nodes.append((ref, str(pin)))
        nets[name] = sorted(nodes)
    return nets

def main():
    pdir = os.path.abspath(sys.argv[1])
    ver = sys.argv[2] if len(sys.argv) > 2 and not sys.argv[2].startswith("--") else "10.0"
    do_upgrade = "--no-upgrade" not in sys.argv
    K = r"C:\Program Files\KiCad\%s\bin\kicad-cli.exe" % ver
    schs = [f for f in glob.glob(os.path.join(pdir, "*.kicad_sch")) if ".upgraded." not in f]
    pros = glob.glob(os.path.join(pdir, "*.kicad_pro"))
    # root sheet = the one named like the .kicad_pro, else first
    sch = schs[0]
    if pros:
        cand = os.path.splitext(pros[0])[0] + ".kicad_sch"
        if os.path.exists(cand):
            sch = cand
    name = os.path.basename(sch)
    out = {"kicad": ver, "sch": name}

    rc, log = run([K, "sch", "erc", "--severity-all", "--exit-code-violations", "--format", "json", "-o", "erc_%s.json" % ver, sch], pdir)
    out["erc_rc"] = rc
    try:
        j = json.load(open(os.path.join(pdir, "erc_%s.json" % ver), encoding="utf-8"))
        viol = []
        for sh in j.get("sheets", []):
            for v in sh.get("violations", []):
                viol.append((v.get("severity"), v.get("type"), v.get("description")))
        out["erc_violations"] = viol
    except Exception as e:
        out["erc_violations"] = "PARSE_FAIL: %s %s" % (e, log[-500:])

    rc, log = run([K, "sch", "export", "netlist", "--format", "kicadsexpr", "-o", "netlist_%s.net" % ver, sch], pdir)
    out["netlist_rc"] = rc
    if rc == 0:
        txt = open(os.path.join(pdir, "netlist_%s.net" % ver), encoding="utf-8").read()
        out["nets"] = parse_netlist(txt)
        out["components"] = re.findall(r'\(comp\s*\(ref\s*"([^"]*)"\)', txt)
        out["footprints"] = re.findall(r'\(footprint\s*"([^"]*)"\)', txt)
    else:
        out["netlist_log"] = log[-800:]

    svgdir = os.path.join(pdir, "svg_%s" % ver)
    os.makedirs(svgdir, exist_ok=True)
    rc, log = run([K, "sch", "export", "svg", "-o", svgdir, sch], pdir)
    out["svg_rc"] = rc
    out["svg_files"] = [os.path.basename(f) for f in glob.glob(os.path.join(svgdir, "*.svg"))]
    if rc != 0:
        out["svg_log"] = log[-800:]

    if do_upgrade:
        up = os.path.join(pdir, name.replace(".kicad_sch", ".upgraded_%s.kicad_sch" % ver))
        shutil.copy(sch, up)
        rc, log = run([K, "sch", "upgrade", "--force", up], pdir)
        out["upgrade_rc"] = rc
        out["upgrade_log"] = log.strip()[-300:]
        a = open(sch, encoding="utf-8").read().splitlines()
        b = open(up, encoding="utf-8").read().splitlines()
        diff = list(difflib.unified_diff(a, b, lineterm="", n=0))
        out["upgrade_changed"] = a != b
        out["upgrade_diff_lines"] = len(diff)
        out["upgrade_diff_head"] = diff[:80]

    print(json.dumps(out, indent=1))

if __name__ == "__main__":
    main()
