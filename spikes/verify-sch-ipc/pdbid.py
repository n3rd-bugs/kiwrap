import pefile, sys, uuid
dll = sys.argv[1]
pe = pefile.PE(dll, fast_load=True)
pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_DEBUG']])
for d in pe.DIRECTORY_ENTRY_DEBUG:
    if d.struct.Type == 2:  # CODEVIEW
        off = d.struct.PointerToRawData
        size = d.struct.SizeOfData
        raw = pe.__data__[off:off + size]
        assert raw[:4] == b'RSDS', raw[:4]
        g = uuid.UUID(bytes_le=raw[4:20])
        age = int.from_bytes(raw[20:24], 'little')
        name = raw[24:].split(b'\0')[0].decode().replace('\\', '/').split('/')[-1]
        ident = g.hex.upper() + str(age)
        print(name, ident)
        print("https://symbols.kicad.org/kicad-stable/%s/%s/%s" % (name, ident, name))
        print("https://symbols.kicad.org/kicad-stable/%s/%s/%s" % (name, ident, name[:-1] + "_"))
