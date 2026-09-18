import uuid, os, sys
LIB="C:/Program Files/KiCad/10.0/share/kicad/symbols"
T="\t"
def extract(path, name):
    s=open(path,encoding='utf-8').read()
    i=s.find('\t(symbol "%s"\n'%name); assert i>=0
    d=0;j=i
    while True:
        c=s[j]
        if c=='(':d+=1
        elif c==')':
            d-=1
            if d==0: break
        j+=1
    return s[i:j+1]
syms={}
for (lib,name) in [("Device","R"),("power","GND"),("power","+3V3"),("power","PWR_FLAG")]:
    syms[(lib,name)]=extract(f"{LIB}/{lib}.kicad_sym",name)

def libsyms(prefixed):
    out=[]
    for (lib,name),txt in syms.items():
        if prefixed: txt=txt.replace('\t(symbol "%s"\n'%name,'\t(symbol "%s:%s"\n'%(lib,name),1)
        out.append(T+txt.replace("\n","\n"+T))
    return "\n".join(out)

root=str(uuid.uuid4())
def prop(n,v,x,y,hide=False):
    h = T*3+"(hide yes)\n" if hide else ""
    return f'{T*2}(property "{n}" "{v}"\n{T*3}(at {x} {y} 0)\n{h}{T*3}(effects (font (size 1.27 1.27)))\n{T*2})'
def inst(libid,ref,val,x,y,pins,proj):
    p="\n".join(f'{T*2}(pin "{n}" (uuid "{uuid.uuid4()}"))' for n in pins)
    props="\n".join([prop("Reference",ref,x+2.54,y),prop("Value",val,x+2.54,y+1.27),prop("Footprint","",x,y,True),prop("Datasheet","",x,y,True),prop("Description","",x,y,True)])
    return (f'{T}(symbol\n{T*2}(lib_id "{libid}")\n{T*2}(at {x} {y} 0)\n{T*2}(unit 1)\n{T*2}(body_style 1)\n'
            f'{T*2}(exclude_from_sim no)\n{T*2}(in_bom yes)\n{T*2}(on_board yes)\n{T*2}(in_pos_files yes)\n{T*2}(dnp no)\n'
            f'{T*2}(uuid "{uuid.uuid4()}")\n{props}\n{p}\n'
            f'{T*2}(instances\n{T*3}(project "{proj}"\n{T*4}(path "/{root}"\n{T*5}(reference "{ref}")\n{T*5}(unit 1)\n{T*4})\n{T*3})\n{T*2})\n{T})')
def wire(x1,y1,x2,y2):
    return f'{T}(wire\n{T*2}(pts\n{T*3}(xy {x1} {y1}) (xy {x2} {y2})\n{T*2})\n{T*2}(stroke (width 0) (type default))\n{T*2}(uuid "{uuid.uuid4()}")\n{T})'
def build(proj,prefixed):
    body=[]
    body.append(inst("power:PWR_FLAG","#FLG01","PWR_FLAG",50.8,39.37,["1"],proj))
    body.append(inst("power:+3V3","#PWR01","+3V3",50.8,41.91,["1"],proj))
    body.append(inst("Device:R","R1","10k",50.8,50.8,["1","2"],proj))
    body.append(inst("Device:R","R2","4k7",50.8,63.5,["1","2"],proj))
    body.append(inst("power:GND","#PWR02","GND",50.8,69.85,["1"],proj))
    body.append(inst("power:PWR_FLAG","#FLG02","PWR_FLAG",50.8,72.39,["1"],proj))
    body.append(wire(50.8,39.37,50.8,41.91))
    body.append(wire(50.8,41.91,50.8,46.99))
    body.append(wire(50.8,54.61,50.8,59.69))
    body.append(wire(50.8,67.31,50.8,69.85))
    body.append(wire(50.8,69.85,50.8,72.39))
    nl="\n"
    return (f'(kicad_sch\n{T}(version 20260306)\n{T}(generator "handwritten")\n{T}(generator_version "10.0")\n{T}(uuid "{root}")\n{T}(paper "A4")\n'
            f'{T}(lib_symbols\n{libsyms(prefixed)}\n{T})\n{nl.join(body)}\n{T}(sheet_instances\n{T*2}(path "/"\n{T*3}(page "1")\n{T*2})\n{T})\n{T}(embedded_fonts no)\n)\n')
for d,pre in [("t1",True),("t2",False)]:
    os.makedirs(d,exist_ok=True)
    open(f"{d}/{d}.kicad_sch","w",encoding="utf-8").write(build(d,pre))
print("ok")
