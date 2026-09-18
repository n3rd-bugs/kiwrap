import struct, sys, os, uuid
def rsds(path):
    data = open(path,'rb').read()
    pe = struct.unpack_from('<I', data, 0x3c)[0]
    if data[pe:pe+4] != b'PE\0\0': return None
    nsec = struct.unpack_from('<H', data, pe+6)[0]
    opt_off = pe+24
    magic = struct.unpack_from('<H', data, opt_off)[0]
    dd_off = opt_off + (112 if magic==0x20b else 96)
    dbg_rva, dbg_size = struct.unpack_from('<II', data, dd_off+6*8)
    opt_size = struct.unpack_from('<H', data, pe+20)[0]
    secs=[]
    so = opt_off+opt_size
    for i in range(nsec):
        name, vs, va, rs, rp = struct.unpack_from('<8sIIII', data, so+i*40)
        secs.append((va,vs,rp,rs))
    def r2o(rva):
        for va,vs,rp,rs in secs:
            if va<=rva<va+max(vs,rs): return rva-va+rp
        return None
    off = r2o(dbg_rva)
    if off is None: return None
    out=[]
    for i in range(dbg_size//28):
        e = struct.unpack_from('<IIHHIIII', data, off+i*28)
        typ, size, rawaddr = e[4], e[5], e[7]
        if typ==2:
            cv = data[rawaddr:rawaddr+size]
            if cv[:4]==b'RSDS':
                g = uuid.UUID(bytes_le=cv[4:20]); age=struct.unpack_from('<I',cv,20)[0]
                pdb = cv[24:].split(b'\0')[0].decode(errors='replace')
                out.append((pdb, '%s%X' % (g.hex.upper(), age)))
    return out
for p in sys.argv[1:]:
    print(p, rsds(p))
