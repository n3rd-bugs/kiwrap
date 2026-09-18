import struct, sys, uuid
def pdb_info(path):
    d = open(path, "rb").read()
    pe = struct.unpack_from("<I", d, 0x3c)[0]
    assert d[pe:pe+4] == b"PE\0\0"
    nsec = struct.unpack_from("<H", d, pe+6)[0]; optsz = struct.unpack_from("<H", d, pe+20)[0]
    opt = pe+24; magic = struct.unpack_from("<H", d, opt)[0]
    dd_off = opt + (112 if magic == 0x20b else 96)
    dbg_rva, dbg_size = struct.unpack_from("<II", d, dd_off + 6*8)
    secs = []
    so = opt + optsz
    for i in range(nsec):
        name, vsz, va, rsz, ro = struct.unpack_from("<8sIIII", d, so + i*40)
        secs.append((va, vsz, ro, rsz))
    def rva2off(rva):
        for va, vsz, ro, rsz in secs:
            if va <= rva < va + max(vsz, rsz): return ro + (rva - va)
        raise ValueError(rva)
    off = rva2off(dbg_rva)
    for i in range(dbg_size // 28):
        e = off + i*28
        typ, sz, addr, ptr = struct.unpack_from("<IIII", d, e+12)
        if typ == 2:  # CODEVIEW
            cv = d[ptr:ptr+sz]
            if cv[:4] == b"RSDS":
                g = uuid.UUID(bytes_le=cv[4:20]); age = struct.unpack_from("<I", cv, 20)[0]
                name = cv[24:].split(b"\0")[0].decode()
                return name, f"{g.hex.upper()}{age:X}"
    return None
for p in sys.argv[1:]:
    print(p, "->", pdb_info(p))
