import sexp, uuid, copy, sys, os
from sexp import Sym as S
L='C:/Program Files/KiCad/10.0/share/kicad/symbols/'
_libs={}
def lib(name):
    if name not in _libs: _libs[name]=sexp.parse(open(L+name+'.kicad_sym',encoding='utf-8').read())
    return _libs[name]
def rawsym(libname,name):
    for s in sexp.find(lib(libname),'symbol'):
        if s[1]==name: return s
    raise KeyError(name)

def flatten(libname,name):
    """return verbatim copy of symbol, with extends flattened, renamed to lib:name"""
    s=copy.deepcopy(rawsym(libname,name))
    ext=sexp.find1(s,'extends')
    if ext:
        parent=flatten_raw(libname,ext[1])   # parent flattened (recursive), still named parent
        child_props={p[1]:p for p in sexp.find(s,'property')}
        out=[S('symbol'),name]
        for c in parent[2:]:
            if isinstance(c,list) and c[0]=='property':
                if c[1] in child_props: out.append(child_props.pop(c[1])); 
                else: out.append(c)
            elif isinstance(c,list) and c[0]=='symbol':
                u=copy.deepcopy(c); 
                assert u[1].startswith(parent[1]+'_'), u[1]
                u[1]=name+u[1][len(parent[1]):]
                out.append(u)
            else: out.append(c)
        # any child-only props (e.g. ki_fp_filters) -> insert before first sub-symbol
        for p in child_props.values():
            idx=next(i for i,c in enumerate(out) if isinstance(c,list) and c[0]=='symbol')
            out.insert(idx,p)
        s=out
    return s
def flatten_raw(libname,name):
    return flatten(libname,name)
def libsym(libname,name):
    import copy as _c; s=_c.deepcopy(rawsym(libname,name))
    s[1]=libname+':'+name
    return s

def pins_of(s):
    out={}
    for u in sexp.find(s,'symbol'):
        for p in sexp.find(u,'pin'):
            at=sexp.find1(p,'at'); out[sexp.find1(p,'number')[1]]=(float(at[1]),float(at[2]),p[1])
    return out

ROOT=str(uuid.uuid4())
PROJECT=sys.argv[1] if len(sys.argv)>1 else 'ldo_test'
lib_symbols=[S('lib_symbols')]
symbols=[]; wires=[]; junctions=[]
placed={}
def fmt(v):
    v=round(v,4); 
    return S(('%g'%v))
def prop(name,val,x,y,hide=False):
    p=[S('property'),name,val,[S('at'),fmt(x),fmt(y),S('0')]]
    if hide: p.append([S('hide'),S('yes')])
    p.append([S('effects'),[S('font'),[S('size'),S('1.27'),S('1.27')]]])
    return p
def place(libname,name,ref,x,y,value=None,footprint=None):
    key=libname+':'+name
    if key not in placed:
        ls=libsym(libname,name); placed[key]=ls; lib_symbols.append(ls)
    ls=placed[key]
    props={p[1]:p[2] for p in sexp.find(ls,'property')}
    val=value if value is not None else props['Value']
    fp=footprint if footprint is not None else props['Footprint']
    u=str(uuid.uuid4())
    sym=[S('symbol'),[S('lib_id'),key],[S('at'),fmt(x),fmt(y),S('0')],[S('unit'),S('1')],[S('body_style'),S('1')],
         [S('exclude_from_sim'),S('no')],[S('in_bom'),S('yes')],[S('on_board'),S('yes')],[S('in_pos_files'),S('yes')],[S('dnp'),S('no')],
         [S('uuid'),u],
         prop('Reference',ref,x+2.54,y-2.54, hide=ref.startswith('#')),
         prop('Value',val,x+2.54,y),
         prop('Footprint',fp,x,y,hide=True),
         prop('Datasheet',props.get('Datasheet',''),x,y,hide=True),
         prop('Description',props.get('Description',''),x,y,hide=True)]
    pins=pins_of(ls) or pins_of(flatten(libname,name)); ep={}
    for num,(px,py,typ) in pins.items():
        sym.append([S('pin'),num,[S('uuid'),str(uuid.uuid4())]])
        ep[num]=(x+px, y-py)   # rotation 0: lib Y-up -> sch Y-down
    sym.append([S('instances'),[S('project'),PROJECT,[S('path'),'/'+ROOT,[S('reference'),ref],[S('unit'),S('1')]]]])
    symbols.append(sym)
    return ep
def wire(a,b):
    wires.append([S('wire'),[S('pts'),[S('xy'),fmt(a[0]),fmt(a[1])],[S('xy'),fmt(b[0]),fmt(b[1])]],
                  [S('stroke'),[S('width'),S('0')],[S('type'),S('default')]],[S('uuid'),str(uuid.uuid4())]])
def junc(p):
    junctions.append([S('junction'),[S('at'),fmt(p[0]),fmt(p[1])],[S('diameter'),S('0')],[S('color'),S('0'),S('0'),S('0'),S('0')],[S('uuid'),str(uuid.uuid4())]])
def chain(*pts):
    for a,b in zip(pts,pts[1:]): wire(a,b)

# ---- circuit ----
J1=place('Connector_Generic','Conn_01x02','J1',38.10,63.50)      # pins: 1 (33.02,63.5) 2 (33.02,66.04)
lib_symbols.append(libsym('Regulator_Linear','AP1117-15'))

U1=place('Regulator_Linear','AMS1117-3.3','U1',76.20,63.50)       # VI(3) (68.58,63.5) VO(2) (83.82,63.5) GND(1) (76.2,71.12)
C1=place('Device','C','C1',55.88,71.12,value='10uF')              # 1 (55.88,67.31) 2 (55.88,74.93)
C2=place('Device','C','C2',96.52,71.12,value='10uF')
R1=place('Device','R','R1',109.22,71.12,value='330')
D1=place('Device','LED','D1',113.03,83.82)                        # K(1) (109.22,83.82) A(2) (116.84,83.82)
G1=place('power','GND','#PWR01',76.20,88.90)
P1=place('power','+3V3','#PWR02',96.52,58.42)
F1=place('power','PWR_FLAG','#FLG01',48.26,60.96)
F2=place('power','PWR_FLAG','#FLG02',48.26,88.90)
for n,e in [('J1',J1),('U1',U1),('C1',C1),('D1',D1),('G1',G1),('P1',P1),('F1',F1)]: print(n,e,file=sys.stderr)

# VIN net
a=(48.26,63.5); b=(55.88,63.5)
chain(J1['1'],a,b,U1['3']); wire(a,F1['1']); wire(b,C1['1']); junc(a); junc(b)
# +3V3 net
c=(96.52,63.5); d=(109.22,63.5)
chain(U1['2'],c,d,R1['1']); wire(c,C2['1']); wire(c,P1['1']); junc(c)
# R1 -> LED
chain(R1['2'],D1['1'])
# GND net
g0=(33.02,88.9); g1=(48.26,88.9); g2=(55.88,88.9); g3=(76.2,88.9); g4=(96.52,88.9); g5=(116.84,88.9)
chain(J1['2'],g0,g1,g2,g3,g4,g5,D1['2'])
wire(g1,F2['1']); wire(g2,C1['2']); wire(g3,U1['1']); wire(g4,C2['2'])
for j in (g1,g2,g3,g4): junc(j)

doc=[S('kicad_sch'),[S('version'),S('20260306')],[S('generator'),'test_gen'],[S('generator_version'),'0.1'],[S('uuid'),ROOT],[S('paper'),'A4'],
     lib_symbols]+junctions+wires+symbols+[[S('sheet_instances'),[S('path'),'/',[S('page'),'1']]],[S('embedded_fonts'),S('no')]]
out=sys.argv[2] if len(sys.argv)>2 else PROJECT+'.kicad_sch'
open(out,'w',encoding='utf-8',newline='\n').write(sexp.ser(doc)+'\n')
print('wrote',out,file=sys.stderr)
