p="test_swig_board.py"; s=open(p).read()
s = s.replace('''for fn in [s01, s01b, s02, s03, s04, s05, s06, s07, s08, s09, s10, s11, s12, s13, s14, s15, s16, s17, s18, s19, s20]:
    fn()''', '''SKIP = set(os.environ.get("SKIP_STEPS","").split(","))
SAVE_EACH = os.environ.get("SAVE_EACH") == "1"
STOP_AT = os.environ.get("STOP_AT")
for fn in [s01, s01b, s02, s03, s04, s05, s06, s07, s08, s09, s10, s11, s12, s13, s14, s15, s16, s17, s18, s19, s20]:
    if fn.__name__ in SKIP:
        print("  (skipped", fn.__name__, ")"); continue
    fn()
    if SAVE_EACH and board is not None and fn.__name__ not in ("s12","s13"):
        p = os.path.join(OUTDIR, f"after_{fn.__name__}.kicad_pcb")
        print("   saving after", fn.__name__, "...", flush=True)
        pcbnew.SaveBoard(p, board, True)
        print("   saved", os.path.getsize(p), flush=True)
    if STOP_AT == fn.__name__:
        print("STOP_AT reached"); break''')
open(p,"w").write(s); print("patched")
