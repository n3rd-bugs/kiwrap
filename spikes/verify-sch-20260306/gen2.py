import uuid, os, re
LIB="C:/Program Files/KiCad/10.0/share/kicad/symbols"
T="\t"
def extract(path, name):
    s=open(path,encoding='utf-8').read()
    i=s.find('\t(symbol "%s"\n'%name); assert i>=0,(name)
    d=0;j=i
    while True:
        c=s[j]
        if c=='(':d+=1
        elif c==')':
            d-=1
            if d==0: break
        j+=1
    return s[i:j+1]
def top_children(txt):
    # split body of a symbol into top-level child sexprs (depth-1)
    i=txt.find("\n")+1; body=txt[i:txt.rfind(")")]
    out=[];d=0;start=None
    for k,c in enumerate(body):
        if c=='(':
            if d==0:start=k
            d+=1
        elif c==')':
            d-=1
            if d==0: out.append(body[start:k+1])
    return out
def head(child): return child.split(None,2)[0].lstrip("(")+(" "+child.split(None,2)[1] if child.startswith("(property") else "")
def flatten(lib, child_name):
    child=extract(f"{LIB}/{lib}.kicad_sym", child_name)
    m=re.search(r'\(extends "([^"]+)"\)',child); parent_name=m.group(1)
    parent=extract(f"{LIB}/{lib}.kicad_sym", parent_name)
    pc=top_children(parent); cc=[c for c in top_children(child) if not c.startswith("(extends")]
    cheads={head(c):c for c in cc}
    merged=[]; used=set()
    for c in pc:
        h=head(c)
        if h in cheads: merged.append(cheads[h]); used.add(h)
        else: merged.append(c)
    for h,c in cheads.items():
        if h not in used: merged.append(c)
    merged=[c.replace(f'(symbol "{parent_name}_', f'(symbol "{child_name}_') for c in merged]
    return f'{T}(symbol "{lib}:{child_name}"\n'+"\n".join(T*2+c.strip() for c in merged)+f"\n{T})"
def plain(lib,name):
    t=extract(f"{LIB}/{lib}.kicad_sym",name)
    return t.replace('\t(symbol "%s"\n'%name,'\t(symbol "%s:%s"\n'%(lib,name),1)

root=str(uuid.uuid4())
def prop(n,v,x,y,hide=False):
    h = T*3+"(hide yes)\n" if hide else ""
    return f'{T*2}(property "{n}" "{v}"\n{T*3}(at {x} {y} 0)\n{h}{T*3}(effects (font (size 1.27 1.27)))\n{T*2})'
def inst(libid,ref,val,x,y,pins,proj,fp=""):
    p="\n".join(f'{T*2}(pin "{n}" (uuid "{uuid.uuid4()}"))' for n in pins)
    props="\n".join([prop("Reference",ref,x+2.54,y),prop("Value",val,x+2.54,y+1.27),prop("Footprint",fp,x,y,True),prop("Datasheet","",x,y,True),prop("Description","",x,y,True)])
    return (f'{T}(symbol\n{T*2}(lib_id "{libid}")\n{T*2}(at {x} {y} 0)\n{T*2}(unit 1)\n{T*2}(body_style 1)\n'
            f'{T*2}(exclude_from_sim no)\n{T*2}(in_bom yes)\n{T*2}(on_board yes)\n{T*2}(in_pos_files yes)\n{T*2}(dnp no)\n'
            f'{T*2}(uuid "{uuid.uuid4()}")\n{props}\n{p}\n'
            f'{T*2}(instances\n{T*3}(project "{proj}"\n{T*4}(path "/{root}"\n{T*5}(reference "{ref}")\n{T*5}(unit 1)\n{T*4})\n{T*3})\n{T*2})\n{T})')
def wire(x1,y1,x2,y2):
    return f'{T}(wire\n{T*2}(pts\n{T*3}(xy {x1} {y1}) (xy {x2} {y2})\n{T*2})\n{T*2}(stroke (width 0) (type default))\n{T*2}(uuid "{uuid.uuid4()}")\n{T})'
def nc(x,y):
    return f'{T}(no_connect\n{T*2}(at {x} {y})\n{T*2}(uuid "{uuid.uuid4()}")\n{T})'
def sch(proj, libsyms, body):
    nl="\n"
    return (f'(kicad_sch\n{T}(version 20260306)\n{T}(generator "handwritten")\n{T}(generator_version "10.0")\n{T}(uuid "{root}")\n{T}(paper "A4")\n'
            f'{T}(lib_symbols\n{nl.join(T+s.replace(nl,nl+T) for s in libsyms)}\n{T})\n{nl.join(body)}\n{T}(sheet_instances\n{T*2}(path "/"\n{T*3}(page "1")\n{T*2})\n{T})\n{T}(embedded_fonts no)\n)\n')

# t3: regulator. U1 at (101.6, 50.8). pins (lib coords -> sch: y flipped):
# GND pin1 at (0,-7.62) -> (101.6, 58.42); VI pin2 at (-7.62,0) -> (93.98,50.8); VO pin5 at (7.62,0)->(109.22,50.8)
# hidden passive pins 3,4 at VI pos, 6,7,8? at VO pos (hidden duplicates) - let them be.
libs=[plain("Device","R"),plain("power","GND"),plain("power","+5V"),plain("power","+3V3"),plain("power","PWR_FLAG"),flatten("Regulator_Linear","LM1117LD-3.3")]
fp="Package_SON:WSON-8-1EP_4x4mm_P0.8mm_EP2.2x3mm"
body=[
 inst("Regulator_Linear:LM1117LD-3.3","U1","LM1117LD-3.3",101.6,50.8,["1","2","3","4","5","6","7","8"],"t3",fp),
 inst("power:+5V","#PWR01","+5V",88.9,50.8,["1"],"t3"),
 inst("power:PWR_FLAG","#FLG01","PWR_FLAG",88.9,48.26,["1"],"t3"),
 inst("power:+3V3","#PWR02","+3V3",114.3,50.8,["1"],"t3"),
 inst("Device:R","R1","10k",114.3,55.88,["1","2"],"t3","Resistor_SMD:R_0603_1608Metric"),
 inst("power:GND","#PWR03","GND",114.3,62.23,["1"],"t3"),
 inst("power:GND","#PWR04","GND",101.6,60.96,["1"],"t3"),
 inst("power:PWR_FLAG","#FLG02","PWR_FLAG",101.6,63.5,["1"],"t3"),
 wire(88.9,48.26,88.9,50.8), wire(88.9,50.8,93.98,50.8),
 wire(109.22,50.8,114.3,50.8), wire(114.3,50.8,114.3,52.07),  # R1 pin1 at (114.3, 55.88-3.81=52.07)
 wire(114.3,59.69,114.3,62.23),  # R1 pin2 at 59.69 -> GND
 wire(101.6,58.42,101.6,60.96), wire(101.6,60.96,101.6,63.5),
]
os.makedirs("t3",exist_ok=True); open("t3/t3.kicad_sch","w",encoding="utf-8").write(sch("t3",libs,body))

# t4: same as t1 but middle wire NOT split: one wire from R1 pin1 (46.99) straight through pin2/pin1 to R2 pin2 (67.31)? That would short. Instead: R1 pin2 (54.61) - R2 pin1 (59.69) as one wire, plus a wire passing THROUGH +3V3 pin: PWR_FLAG(39.37) to R1 pin1 (46.99) as a single wire passing through +3V3 pin at 41.91.
libs1=[plain("Device","R"),plain("power","GND"),plain("power","+3V3"),plain("power","PWR_FLAG")]
def base(proj, mid_split=True, top_split=True, offgrid=False):
    b=[inst("power:PWR_FLAG","#FLG01","PWR_FLAG",50.8,39.37,["1"],proj),
       inst("power:+3V3","#PWR01","+3V3",50.8,41.91,["1"],proj),
       inst("Device:R","R1","10k",50.8,50.8,["1","2"],proj),
       inst("Device:R","R2","4k7",50.8,63.5,["1","2"],proj),
       inst("power:GND","#PWR02","GND",50.8,69.85,["1"],proj),
       inst("power:PWR_FLAG","#FLG02","PWR_FLAG",50.8,72.39,["1"],proj)]
    if top_split: b+= [wire(50.8,39.37,50.8,41.91), wire(50.8,41.91,50.8,46.99)]
    else: b+= [wire(50.8,39.37,50.8,46.99)]
    b+= [wire(50.8,54.61,50.8,59.69)]
    if offgrid: b+= [wire(50.8,67.31,50.8,69.85), wire(50.8,69.85,51.435,71.12), wire(51.435,71.12,50.8,72.39)]
    else: b+= [wire(50.8,67.31,50.8,69.85), wire(50.8,69.85,50.8,72.39)]
    return b
os.makedirs("t4",exist_ok=True); open("t4/t4.kicad_sch","w",encoding="utf-8").write(sch("t4",libs1,base("t4",top_split=False)))
os.makedirs("t5",exist_ok=True); open("t5/t5.kicad_sch","w",encoding="utf-8").write(sch("t5",libs1,base("t5",offgrid=True)))
print("ok")
