import struct, sys, os, uuid
def pdbid(path):
    with open(path,'rb') as f: d=f.read()
    pe=struct.unpack_from('<I',d,0x3c)[0]
    assert d[pe:pe+4]==b'PE\0\0'
    nsec=struct.unpack_from('<H',d,pe+6)[0]
    optsz=struct.unpack_from('<H',d,pe+20)[0]
    opt=pe+24
    magic=struct.unpack_from('<H',d,opt)[0]
    ddoff=opt+(112 if magic==0x20b else 96)
    dbg_rva,dbg_sz=struct.unpack_from('<II',d,ddoff+6*8)
    secs=[]
    so=opt+optsz
    for i in range(nsec):
        s=so+i*40
        name=d[s:s+8]; vsz,va,rsz,rp=struct.unpack_from('<IIII',d,s+8)
        secs.append((va,vsz,rp,rsz))
    def r2o(rva):
        for va,vsz,rp,rsz in secs:
            if va<=rva<va+max(vsz,rsz): return rva-va+rp
        return None
    off=r2o(dbg_rva)
    out=[]
    for i in range(dbg_sz//28):
        e=off+i*28
        typ,szd,addrrva,ptr=struct.unpack_from('<IIII',d,e+12)
        if typ==2:
            if d[ptr:ptr+4]==b'RSDS':
                g=uuid.UUID(bytes_le=d[ptr+4:ptr+20]); age=struct.unpack_from('<I',d,ptr+20)[0]
                name=d[ptr+24:d.index(b'\0',ptr+24)].decode(errors='replace')
                out.append((g.hex.upper(),age,name))
        else:
            out.append(('type',typ))
    return out
for p in sys.argv[1:]:
    print(p, pdbid(p))
