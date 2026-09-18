# minimal PE export-table reader (no deps) to find wxSetAssertHandler / wxTheAssertHandler exports
import struct, sys
def exports(path, needle):
    d = open(path,'rb').read()
    pe = struct.unpack_from('<I', d, 0x3c)[0]
    assert d[pe:pe+4] == b'PE\0\0'
    nsec = struct.unpack_from('<H', d, pe+6)[0]
    opt = pe+24
    magic = struct.unpack_from('<H', d, opt)[0]
    exp_rva, exp_size = struct.unpack_from('<II', d, opt + (112 if magic==0x20b else 96))
    sec = opt + struct.unpack_from('<H', d, pe+20)[0]
    secs = []
    for i in range(nsec):
        s = d[sec+i*40: sec+(i+1)*40]
        va, vsz, raw, rawsz = struct.unpack_from('<IIII', s, 8)[0], struct.unpack_from('<I', s, 8)[0], struct.unpack_from('<I', s, 20)[0], struct.unpack_from('<I', s, 16)[0]
        secs.append((struct.unpack_from('<I', s, 12)[0], struct.unpack_from('<I', s, 8)[0], struct.unpack_from('<I', s, 20)[0]))
    def r2o(rva):
        for va, vsz, raw in secs:
            if va <= rva < va+vsz: return rva - va + raw
        raise ValueError(rva)
    e = r2o(exp_rva)
    nnames = struct.unpack_from('<I', d, e+24)[0]
    names_rva = struct.unpack_from('<I', d, e+32)[0]
    no = r2o(names_rva)
    out = []
    for i in range(nnames):
        nrva = struct.unpack_from('<I', d, no+i*4)[0]
        o = r2o(nrva); end = d.index(b'\0', o)
        nm = d[o:end].decode('latin1')
        if needle.lower() in nm.lower(): out.append(nm)
    return out
for p in sys.argv[2:]:
    print("==", p)
    for n in exports(p, sys.argv[1]): print("  ", n)
