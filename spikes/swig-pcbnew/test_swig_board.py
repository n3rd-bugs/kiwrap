"""
Headless SWIG pcbnew test for KiCad 7/8/9/10 bundled python.
Usage:  "C:/Program Files/KiCad/10.0/bin/python.exe" test_swig_board.py <outdir> <kicad_version e.g. 10.0>
Every step is wrapped so one failure does not hide the rest.
"""
import sys, os, traceback, time, json, functools

OUTDIR = sys.argv[1] if len(sys.argv) > 1 else os.getcwd()
KVER = sys.argv[2] if len(sys.argv) > 2 else "10.0"
os.makedirs(OUTDIR, exist_ok=True)
FP_LIB = f"C:/Program Files/KiCad/{KVER}/share/kicad/footprints/Resistor_SMD.pretty"

RESULTS = {}
def step(name):
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*a, **k):
            t0 = time.time()
            try:
                r = fn(*a, **k)
                RESULTS[name] = {"ok": True, "detail": r, "secs": round(time.time()-t0, 3)}
                print(f"[OK  ] {name}: {r}")
                return r
            except Exception as e:
                RESULTS[name] = {"ok": False, "detail": f"{type(e).__name__}: {e}", "secs": round(time.time()-t0, 3)}
                print(f"[FAIL] {name}: {type(e).__name__}: {e}")
                traceback.print_exc()
                return None
        return wrapper
    return deco

print("python:", sys.version)
print("executable:", sys.executable)

import pcbnew
print("pcbnew module:", pcbnew.__file__)
if os.environ.get("NOASSERT","0") == "1":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from wx_noassert import disable_wx_asserts
    print("wx asserts disabled:", disable_wx_asserts())
EDA_UNITS_MM = getattr(pcbnew, "EDA_UNITS_MM", None) or getattr(pcbnew, "EDA_UNITS_MILLIMETRES")  # renamed in 8.0
print("pcbnew.GetBuildVersion():", pcbnew.GetBuildVersion())
try:
    print("pcbnew.GetSemanticVersion():", pcbnew.GetSemanticVersion())
except Exception as e:
    print("GetSemanticVersion n/a", e)

MM = pcbnew.FromMM
def V(x, y):
    return pcbnew.VECTOR2I(MM(x), MM(y))

board = None
board_path = os.path.join(OUTDIR, "swig_test.kicad_pcb")

@step("01_new_board")
def s01():
    global board
    try:
        b = pcbnew.NewBoard(board_path)
        how = "pcbnew.NewBoard(path)"
    except Exception as e:
        print("  NewBoard failed:", e, "-> trying BOARD()")
        b = pcbnew.BOARD()
        b.SetFileName(board_path)
        how = "pcbnew.BOARD()"
    board = b
    return f"{how}; type={type(b).__name__}; copper layers={b.GetCopperLayerCount()}; file={b.GetFileName()}"

@step("01b_CreateEmptyBoard")
def s01b():
    # PITFALL: a second BOARD object (BOARD() or CreateEmptyBoard()) in the same process makes SaveBoard
    # flaky (abort/segfault) on 8.0/9.0/10.0. Opt-in only via EXTRA_BOARD=1.
    if os.environ.get("EXTRA_BOARD") != "1":
        return "skipped (set EXTRA_BOARD=1 to reproduce the one-BOARD-per-process hazard)"
    b2 = pcbnew.CreateEmptyBoard()
    return f"CreateEmptyBoard OK: {type(b2).__name__} layers={b2.GetCopperLayerCount()}"

@step("02_outline_edge_cuts")
def s02():
    pts = [(0,0),(30,0),(30,20),(0,20)]
    n = 0
    for i in range(4):
        a = pts[i]; b = pts[(i+1)%4]
        seg = pcbnew.PCB_SHAPE(board)
        seg.SetShape(pcbnew.SHAPE_T_SEGMENT)
        seg.SetStart(V(*a)); seg.SetEnd(V(*b))
        seg.SetLayer(pcbnew.Edge_Cuts)
        seg.SetWidth(MM(0.1))
        board.Add(seg)
        n += 1
    has_rect = hasattr(pcbnew, "SHAPE_T_RECT") or hasattr(pcbnew, "SHAPE_T_RECTANGLE")
    return f"added {n} Edge.Cuts segments; SHAPE_T_RECT/RECTANGLE available={has_rect}"

fp1 = fp2 = None
@step("03_footprint_load")
def s03():
    global fp1, fp2
    names = pcbnew.FootprintEnumerate(FP_LIB)
    fp1 = pcbnew.FootprintLoad(FP_LIB, "R_0603_1608Metric")
    fp2 = pcbnew.FootprintLoad(FP_LIB, "R_0603_1608Metric")
    assert fp1 is not None and fp2 is not None, "FootprintLoad returned None"
    return f"lib has {len(names)} footprints; loaded {fp1.GetFPID().GetLibItemName()} pads={len(fp1.Pads())}; libid nick='{fp1.GetFPID().GetLibNickname()}'"

@step("04_place_footprints")
def s04():
    fp1.SetReference("R1"); fp1.SetValue("10k")
    fp2.SetReference("R2"); fp2.SetValue("4k7")
    fp1.SetPosition(V(8, 10))
    fp2.SetPosition(V(22, 10))
    try:
        fp2.SetOrientationDegrees(90)
        rot = "SetOrientationDegrees(90)"
    except Exception:
        fp2.SetOrientation(pcbnew.EDA_ANGLE(90, pcbnew.DEGREES_T))
        rot = "SetOrientation(EDA_ANGLE)"
    fp1.SetFPIDAsString("Resistor_SMD:R_0603_1608Metric")
    fp2.SetFPIDAsString("Resistor_SMD:R_0603_1608Metric")
    board.Add(fp1); board.Add(fp2)
    return f"placed R1@{pcbnew.ToMM(fp1.GetPosition())} R2@{pcbnew.ToMM(fp2.GetPosition())} rot={rot}; board footprints={len(board.GetFootprints())}"

nets = {}
@step("05_create_nets")
def s05():
    for nm in ["VCC", "GND", "SIG"]:
        ni = pcbnew.NETINFO_ITEM(board, nm)
        board.Add(ni)
        nets[nm] = ni
    codes = {nm: ni.GetNetCode() for nm, ni in nets.items()}
    found = board.FindNet("GND")
    return f"netcodes={codes}; FindNet('GND').GetNetCode()={found.GetNetCode() if found else None}; GetNetCount={board.GetNetCount()}"

@step("06_assign_pads_to_nets")
def s06():
    assign = {("R1","1"):"VCC", ("R1","2"):"SIG", ("R2","1"):"SIG", ("R2","2"):"GND"}
    done = []
    for fp in board.GetFootprints():
        for pad in fp.Pads():
            key = (fp.GetReference(), pad.GetNumber())
            if key in assign:
                pad.SetNet(nets[assign[key]])
                done.append(f"{key[0]}.{key[1]}->{pad.GetNetname()}({pad.GetNetCode()})")
    return "; ".join(done)

@step("07_add_track")
def s07():
    p_from = None; p_to = None
    for fp in board.GetFootprints():
        for pad in fp.Pads():
            if fp.GetReference()=="R1" and pad.GetNumber()=="2": p_from = pad.GetPosition()
            if fp.GetReference()=="R2" and pad.GetNumber()=="1": p_to = pad.GetPosition()
    t = pcbnew.PCB_TRACK(board)
    t.SetStart(p_from); t.SetEnd(p_to)
    t.SetWidth(MM(0.25))
    t.SetLayer(pcbnew.F_Cu)
    t.SetNet(nets["SIG"])
    board.Add(t)
    return f"track {pcbnew.ToMM(p_from)} -> {pcbnew.ToMM(p_to)} w=0.25mm net=SIG; tracks={len(board.GetTracks())}"

@step("08_add_via")
def s08():
    v = pcbnew.PCB_VIA(board)
    v.SetPosition(V(15, 5))
    v.SetDrill(MM(0.3))
    v.SetWidth(MM(0.6))
    try:
        v.SetViaType(pcbnew.VIATYPE_THROUGH)
    except Exception as e:
        print("  SetViaType issue:", e)
    v.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
    v.SetNet(nets["GND"])
    board.Add(v)
    p = None
    for fp in board.GetFootprints():
        for pad in fp.Pads():
            if fp.GetReference()=="R2" and pad.GetNumber()=="2": p = pad.GetPosition()
    t = pcbnew.PCB_TRACK(board); t.SetStart(p); t.SetEnd(V(15,5)); t.SetWidth(MM(0.25)); t.SetLayer(pcbnew.F_Cu); t.SetNet(nets["GND"]); board.Add(t)
    return f"via at (15,5) drill 0.3 dia 0.6 net GND; tracks now={len(board.GetTracks())}"

zone = None
@step("09_add_zone")
def s09():
    global zone
    z = pcbnew.ZONE(board)
    z.SetLayer(pcbnew.B_Cu)
    z.SetNet(nets["GND"])
    z.SetAssignedPriority(0)
    z.SetZoneName("GND_POUR")
    pts = [V(1,1), V(29,1), V(29,19), V(1,19)]
    # PITFALL: z.SetOutline(SHAPE_POLY_SET) takes a raw pointer and the ZONE takes ownership,
    # but the Python proxy still owns it -> double free / dangling m_Poly when the proxy is GC'd
    # (random access violation in SaveBoard, fill area 0). Safe pattern: mutate z.Outline() in place,
    # or use z.AppendCorner(), or set outline.thisown = 0 after SetOutline().
    MODE = os.environ.get("OUTLINE_MODE", "inplace")
    if MODE == "inplace":
        o = z.Outline(); o.NewOutline()
        for p in pts: o.Append(p)
    elif MODE == "appendcorner":
        for p in pts: z.AppendCorner(p, -1)
    elif MODE == "setoutline_disown":
        outline = pcbnew.SHAPE_POLY_SET(); outline.NewOutline()
        for p in pts: outline.Append(p)
        z.SetOutline(outline); outline.thisown = 0
    else:  # "setoutline_buggy" reproduces the crash
        outline = pcbnew.SHAPE_POLY_SET(); outline.NewOutline()
        for p in pts: outline.Append(p)
        z.SetOutline(outline)
    z.SetMinThickness(MM(0.2))
    try:
        z.SetPadConnection(pcbnew.ZONE_CONNECTION_THERMAL)
    except Exception as e:
        print("  ZONE_CONNECTION_THERMAL:", e)
    board.Add(z)
    zone = z
    return f"zone on B_Cu net GND corners={z.GetNumCorners()}; zones={len(board.Zones())}"

@step("10_fill_zones")
def s10():
    board.BuildConnectivity()   # needed headless: island removal uses connectivity; without it the fill can be discarded as an island
    filler = pcbnew.ZONE_FILLER(board)
    zones = board.Zones()
    ok = filler.Fill(zones)
    z = board.Zones()[0]
    area = z.GetFilledArea() if hasattr(z, "GetFilledArea") else -1
    return f"Fill returned {ok}; zone IsFilled={z.IsFilled()}; filled area mm2={area/1e12 if area>0 else area}"

@step("11_build_connectivity")
def s11():
    board.BuildConnectivity()
    c = board.GetConnectivity()
    unconnected = c.GetUnconnectedCount(True) if hasattr(c, "GetUnconnectedCount") else "n/a"
    return f"connectivity built; unconnected count={unconnected}"

@step("12_save_board")
def s12():
    ok = pcbnew.SaveBoard(board_path, board)
    sz = os.path.getsize(board_path)
    return f"SaveBoard -> {ok}; {board_path} {sz} bytes"

@step("13_reload_board")
def s13():
    b = pcbnew.LoadBoard(board_path)
    return f"LoadBoard OK: footprints={len(b.GetFootprints())} tracks={len(b.GetTracks())} zones={len(b.Zones())} nets={b.GetNetCount()} drawings={len(b.GetDrawings())}"

@step("14_WriteDRCReport")
def s14():
    rpt = os.path.join(OUTDIR, "swig_drc.rpt")
    ok = pcbnew.WriteDRCReport(board, rpt, EDA_UNITS_MM, True)
    txt = open(rpt, encoding="utf-8", errors="replace").read() if os.path.exists(rpt) else "<no file>"
    return f"WriteDRCReport -> {ok}; report {len(txt)} chars; head: {txt[:400]!r}"

@step("15_ExportSpecctraDSN")
def s15():
    dsn = os.path.join(OUTDIR, "swig_test.dsn")
    ok = pcbnew.ExportSpecctraDSN(board, dsn)
    sz = os.path.getsize(dsn) if os.path.exists(dsn) else -1
    return f"ExportSpecctraDSN -> {ok}; {sz} bytes"

SES_TEMPLATE = """(session "swig_test.ses"
  (base_design "swig_test.dsn")
  (placement
    (resolution um 10)
    (component "Resistor_SMD:R_0603_1608Metric"
      (place R1 8000 -10000 front 0)
      (place R2 22000 -10000 front 90)
    )
  )
  (was_is
  )
  (routes
    (resolution um 10)
    (parser
      (host_cad "KiCad's Pcbnew")
      (host_version "10.0.0")
    )
    (library_out
      (padstack "Via[0-1]_600:300_um"
        (shape
          (circle F.Cu 600 0 0)
        )
        (shape
          (circle B.Cu 600 0 0)
        )
        (attach off)
      )
    )
    (network_out
      (net SIG
        (wire
          (path F.Cu 250
            8825 -10000
            15000 -12000
            22000 -10825
          )
        )
      )
      (net GND
        (via "Via[0-1]_600:300_um" 15000 -5000)
      )
    )
  )
)
"""

@step("16_ImportSpecctraSES")
def s16():
    ses = os.path.join(OUTDIR, "swig_test.ses")
    if not os.path.exists(ses):
        with open(ses, "w") as f: f.write(SES_TEMPLATE)
    n_before = len(board.GetTracks())
    ok = pcbnew.ImportSpecctraSES(board, ses)
    n_after = len(board.GetTracks())
    return f"ImportSpecctraSES -> {ok}; tracks before={n_before} after={n_after}"

@step("17_netlist_api_check")
def s17():
    names = [n for n in dir(pcbnew) if "NETLIST" in n.upper()]
    return f"symbols containing 'netlist': {names}"

@step("18_plot_svg_from_python")
def s18():
    pctl = pcbnew.PLOT_CONTROLLER(board)
    popt = pctl.GetPlotOptions()
    popt.SetOutputDirectory(OUTDIR)
    popt.SetPlotFrameRef(False)
    try:
        popt.SetSvgPrecision(4)
    except Exception as e:
        print("  SetSvgPrecision:", e)
    pctl.OpenPlotfile("swig_py_plot_FCu", pcbnew.PLOT_FORMAT_SVG, "F.Cu via python")
    pctl.SetLayer(pcbnew.F_Cu)
    pctl.PlotLayer()
    pctl.ClosePlot()
    f = pctl.GetPlotFileName()
    return f"PLOT_CONTROLLER svg -> {f} exists={os.path.exists(f)}"

@step("19_gerber_drill_from_python")
def s19():
    pctl = pcbnew.PLOT_CONTROLLER(board)
    popt = pctl.GetPlotOptions()
    gdir = os.path.join(OUTDIR, "gerbers")
    os.makedirs(gdir, exist_ok=True)
    popt.SetOutputDirectory(gdir)
    popt.SetUseGerberProtelExtensions(False)
    popt.SetCreateGerberJobFile(True)
    n = 0
    for layer, name in [(pcbnew.F_Cu,"F_Cu"),(pcbnew.B_Cu,"B_Cu"),(pcbnew.Edge_Cuts,"Edge_Cuts"),(pcbnew.F_Mask,"F_Mask"),(pcbnew.B_Mask,"B_Mask")]:
        pctl.SetLayer(layer)
        pctl.OpenPlotfile(name, pcbnew.PLOT_FORMAT_GERBER, name)
        pctl.PlotLayer(); n += 1
    pctl.ClosePlot()
    drl = pcbnew.EXCELLON_WRITER(board)
    drl.SetFormat(True)
    drl.SetOptions(False, False, pcbnew.VECTOR2I(0,0), False)
    drl.CreateDrillandMapFilesSet(gdir, True, False)
    files = sorted(os.listdir(gdir))
    return f"{n} gerber layers + drill: {files}"

@step("20_step_export_check")
def s20():
    names = [n for n in dir(pcbnew) if "STEP" in n.upper()]
    return f"STEP-related symbols: {names[:10]}"

SKIP = set(os.environ.get("SKIP_STEPS","").split(","))
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
        print("STOP_AT reached"); break

try:
    pcbnew.SaveBoard(os.path.join(OUTDIR, "swig_test_after_ses.kicad_pcb"), board)
except Exception as e:
    print("resave after SES failed", e)

with open(os.path.join(OUTDIR, "results.json"), "w") as f:
    json.dump({"python": sys.version, "kicad": pcbnew.GetBuildVersion(), "results": RESULTS}, f, indent=2, default=str)
print("\nSUMMARY:")
for k, v in RESULTS.items():
    print(("OK   " if v["ok"] else "FAIL "), k, f"({v['secs']}s)")
