# swig-pcbnew: headless KiCad 10 SWIG "import pcbnew" on Windows (with 7/8/9 drift), Freerouting round-trip, DRC, DSN/SES, netlist, DLL pitfalls, deprecation status

## Findings

- **[high, VERIFIED LOCALLY]** SWIG pcbnew is present and functional in KiCad 10.0.0 on Windows: C:/Program Files/KiCad/10.0/bin/Lib/site-packages/{pcbnew.py (23,926 lines, 175 classes), _pcbnew.pyd (30.8 MB, links python311.dll)}. Bundled interpreter is Python 3.11.5 (harness said 3.11.8 – that is the system python). 7.0 bundles Python 3.9.16 (pyd links python39.dll); 8.0/9.0/10.0 all bundle 3.11.5.
  - Evidence: python.exe --version; pcbnew.GetBuildVersion()=='10.0.0'; regex scan of _pcbnew.pyd for python3xx.dll per version; wc -l pcbnew.py per version (7.0:18733, 8.0:19161, 9.0:21784, 10.0:23926).
- **[high, VERIFIED LOCALLY]** Complete headless board construction works in KiCad 10 from the bundled python: NewBoard(path) -> Edge.Cuts PCB_SHAPE segments -> FootprintLoad(Resistor_SMD.pretty, R_0603_1608Metric) x2 (placed, rotated with SetOrientationDegrees, SetFPIDAsString) -> NETINFO_ITEM nets (VCC/GND/SIG) -> pad.SetNet -> PCB_TRACK -> PCB_VIA (SetDrill/SetWidth/SetLayerPair) -> ZONE on B_Cu net GND -> ZONE_FILLER(board).Fill(board.Zones()) (filled area 503.99 mm^2) -> BuildConnectivity (0 unconnected) -> SaveBoard -> LoadBoard reload (2 fp, 3 tracks, 1 zone, 4 nets, 4 drawings). The saved file passes `kicad-cli pcb drc` with 0 violations / 0 unconnected and `kicad-cli pcb export svg` and `pcb render` (PNG visually correct).
  - Evidence: test_swig_board.py run under 10.0: all steps 01-13 and 15-20 OK; out10/swig_test.kicad_pcb (10172 bytes, version 20260206); out10/swig_drc_cli.json: violations 0, unconnected 0; out10/swig_test_top.png inspected.
- **[high, VERIFIED LOCALLY]** CRITICAL OWNERSHIP PITFALL #1 (all versions): ZONE.SetOutline(SHAPE_POLY_SET) takes a raw pointer and the ZONE takes ownership, but the Python proxy still owns the object; when the proxy is garbage-collected (e.g. created inside a function) the zone's outline dangles -> zone fill silently returns 0 mm^2 and SaveBoard randomly access-violates (~50% of runs). Safe patterns (all verified): mutate `z.Outline()` in place (NewOutline()/Append()), or `z.AppendCorner(pt, -1)`, or `outline.thisown = 0` after SetOutline. BOARD.Add() does the thisown=0 dance for you (pcbnew.py line 11184) but SetOutline does not.
  - Evidence: Bisect: identical script crashed 4/8 runs in SaveBoard with 'Windows fatal exception: access violation' (faulthandler) and fill area 0.0; after switching to Outline()-in-place, 8/8 runs pass and fill area = 503.99 mm^2; OUTLINE_MODE env in test_swig_board.py reproduces both.
- **[high, VERIFIED LOCALLY]** CRITICAL PITFALL #2: exactly ONE BOARD object per interpreter process. Creating a second BOARD (pcbnew.BOARD() or pcbnew.CreateEmptyBoard()) in the same process, even if kept alive, makes SaveBoard flaky (Fatal Python error: Aborted / segfault) on 8.0 (~50-80%), 9.0 (~20%) and 10.0 (~15%); control runs with a single board are 100% stable (30+ runs). Also GetBoard() returns None outside the GUI.
  - Evidence: reps.sh 8.0/9.0/10.0: 'newboard,fp,zone,fill' 5/5 ok; 'newboard,boardctor2,fp,zone,fill' 8.0 exit codes 3 3 0 3 0 / 3 3 139 139 0 0; 9.0 3 0 0 0 0 / 0 139 3 0 0; 10.0 139 0 0 0 0. Crash traceback always in pcbnew.SaveBoard.
- **[high, VERIFIED LOCALLY]** Zone fill headless: call board.BuildConnectivity() BEFORE ZONE_FILLER.Fill(); otherwise island removal cannot see the via/pad connections (fill area differed 503.98 vs 501.96 mm^2 and unconnected count 1 vs 0). ZONE_FILLER.Fill(zones, aCheck=False, aParent=None) works with no wx app; ISLAND_REMOVAL_MODE_* and ZONE_CONNECTION_* constants exist in 10.0.
  - Evidence: bisect4.py modes base/prefill_conn: area 503.98 & unconnected(True)=1 without prefill; 501.96 & unconnected=0 with prefill.
- **[high, VERIFIED LOCALLY]** pcbnew.WriteDRCReport(board, path, units, allTrackErrors) in KiCad 10.0 standalone python DOES NOT WORK: it hits `pgm_base.cpp(1043): assert "process" failed in Pgm()` (no PGM_BASE/app object in the standalone module). The wx assert opens a native modal dialog and the process hangs forever (headless CI would hang); if the assert is suppressed it null-derefs (access violation). Headless DRC in 10 must go through `kicad-cli pcb drc` (which works, incl. --format json, --refill-zones --save-board new in 10, --schematic-parity, --exit-code-violations).
  - Evidence: run10.log shows the assert after step 13 and the process had to be killed; with wx asserts disabled: 'Windows fatal exception: access violation ... line 9917 in WriteDRCReport' (3/3 runs). kicad-cli 10 drc --help lists the flags.
- **[high, VERIFIED LOCALLY]** WriteDRCReport DOES work headlessly in KiCad 9.0.5, 8.0.1 and 7.0.6 IF wx assert dialogs are disabled first; it runs in ~0.3 s, is repeatable in one process (3 calls), and correctly reports an injected short (shorting_items/tracks_crossing). Disabling asserts: after `import pcbnew`, use ctypes to null the exported global `?wxTheAssertHandler@@3P6AXAEBVwxString@@H000@ZEA` in wxbase*_vc*_custom.dll (equivalent of wxDisableAsserts(); export present in 7/8/9/10). Without it, 9.0 hangs on `wxDateTime::Format(): unknown format specifier` after writing the report header (the report date shows as '2026-09-18 T' in 9.0). Set KICAD9_FOOTPRINT_DIR (resp. KICAD8_/KICAD7_) in the process environment, otherwise lib-parity checks report 'footprint library not found at }/Resistor_SMD.pretty' (env-var expansion needs Pgm()).
  - Evidence: wx_noassert.py + drc_test.py: 9.0 'WriteDRCReport -> True 0.30s' full report; 8.0 and 7.0 step 14 OK in run8.log/run7.log; drc_twice.py 3 calls ok + 6 violations after adding a VCC-GND short; with KICAD9_FOOTPRINT_DIR set: 0 violations.
- **[high, VERIFIED LOCALLY]** ExportSpecctraDSN(board, path) works in 7/8/9/10. ImportSpecctraSES(board, path) works in 8/9/10; in 7.0 only the 1-arg GUI form exists (TypeError with 2 args). SES import REPLACES all tracks/vias with the session's wiring and honours the SES (resolution ...) header. Full headless autoroute round trip verified on 10.0: unrouted 6-resistor board (5 unconnected) -> DSN -> Freerouting 1.9.0 (Java 17 JRE, `-de -do -mp 50`, 23 s, no GUI) -> SES -> ImportSpecctraSES -> 14 tracks, 0 unconnected -> SaveBoard -> kicad-cli drc: 0 violations, 0 unconnected; render PNG shows the routes.
  - Evidence: route_roundtrip.py output; route10/rr.kicad_pcb, rr.dsn, rr.ses, rr_drc.json, rr_top.png. Freerouting 2.x needs Java 21 (not available; PATH java is 1.8, Adoptium jre-17 exists).
- **[high, VERIFIED LOCALLY]** The SWIG module has NO netlist reader: no NETLIST, NETLIST_READER or BOARD_NETLIST_UPDATER symbols in 7/8/9/10 (dir(pcbnew) has zero names containing 'NETLIST'); kicad-cli has no 'update pcb from schematic' either (pcb subcommands: drc, export, import[foreign formats], render, upgrade). Nets must be built programmatically. Verified workaround: `kicad-cli sch export netlist --format kicadsexpr` on the pic_programmer demo (63 comps, 111 nets) + a 60-line s-expr parser + FootprintLoad/NETINFO_ITEM/FindPadByNumber().SetNet -> 58/63 footprints placed, 111 nets, 184 pad assignments, board loads in kicad-cli drc (105 unconnected = unrouted ratsnest). The 5 misses used a project-local lib nickname my naive resolver ignored (a real wrapper must resolve nicknames through project + global fp-lib-table; note KiCad 10's global fp-lib-table is a nested 'Table'-type entry pointing at share/kicad/template/fp-lib-table).
  - Evidence: netlist_to_pcb.py run; netlist/from_netlist.kicad_pcb (270 KB); from_netlist_drc.json.
- **[high, VERIFIED LOCALLY]** There is NO SWIG API for eeschema in any installed version: only _pcbnew.pyd/pcbnew.py exist in site-packages (no eeschema/sch module); _eeschema.dll is the KIFACE, not a python module. Official docs: 'Python bindings are provided for the PCB editor only at this time. Using Python from within the other parts of KiCad is not supported.'
  - Evidence: ls site-packages; dev-docs page fetched.
  - Sources: https://dev-docs.kicad.org/en/apis-and-binding/pcbnew/index.html
- **[high]** Official deprecation status: 'The SWIG-based Python bindings in KiCad are deprecated as of KiCad 9.0 and will be removed in a future version. The current plan is to remove the SWIG bindings in KiCad 11.0.' The bindings are 'a raw binding layer over KiCad rather than a true API, it is unstable and will generally change between KiCad major versions' with 'no guarantees on strict compatibility'. kicad-python (IPC API, v0.8.0 2026-08-30) states: 'The SWIG bindings still exist in KiCad 9 and 10, but are removed in KiCad 11.' The IPC API requires a running KiCad with the API server enabled and cannot manipulate files without KiCad running. The KiCad 10.0.0 release announcement (2026-03-20) does not mention Python/SWIG/API changes. Doxygen python docs exist for 8.0, 9.0 and 10.0 (docs.kicad.org/doxygen-python-10.0/ returns HTTP 200).
  - Evidence: WebFetch of dev-docs pages, PyPI kicad-python page, kicad.org release post; curl HEAD checks.
  - Sources: https://dev-docs.kicad.org/en/apis-and-binding/pcbnew/index.html, https://dev-docs.kicad.org/en/apis-and-binding/index.html, https://pypi.org/project/kicad-python/, https://www.kicad.org/blog/2026/03/Version-10.0.0-Released/, https://docs.kicad.org/doxygen-python-10.0/
- **[high, VERIFIED LOCALLY]** Windows DLL/path pitfalls: (a) the bundled python.exe works out of the box because sitecustomize.py rewrites sys.path to bin/DLLs, bin/Lib, bin/Lib/site-packages and Documents/KiCad/<ver>/3rdparty/Python311/site-packages (PYTHONUSERBASE); (b) a SYSTEM/venv CPython of the SAME minor (3.11.x for 8/9/10, 3.9.x for 7) can import KiCad 10's pcbnew if you add site-packages to sys.path AND call os.add_dll_directory('C:/Program Files/KiCad/10.0/bin') before import; editing os.environ['PATH'] at runtime does NOT work (Python 3.8+ DLL search rules -> 'DLL load failed while importing _pcbnew'); Python 3.12 fails with 'Module use of python311.dll conflicts with this version of Python'. A uv venv (3.11.8) imports and loads footprints fine. (c) the standalone process does not modify %APPDATA%/kicad/* in my tests, and KICAD_CONFIG_HOME=<dir> redirects the settings manager (it created <dir>/9.0/kicad_common.json), so wrapper subprocesses should always set it.
  - Evidence: sys_python_import.py modes naive/dlldir/path with C:/Python311, C:/Python312 and venv311; mtime/sha256 checks of kicad_common.json around runs; cfghome/9.0/kicad_common.json created.
- **[high, VERIFIED LOCALLY]** API drift measured across 7.0.6 / 8.0.1 / 9.0.5 / 10.0.0 with the same script: (1) PCB_LAYER_ID renumbered in 9.0: B_Cu 31->2, In1_Cu 1->4, Edge_Cuts 44->25, F_SilkS 37->5, User_1 50->39, PCB_LAYER_ID_COUNT 60->128 (never hard-code numeric layer ids); (2) VIATYPE_THROUGH/BLIND_BURIED/MICROVIA constants exist in 7/8/9 but are MISSING in 10 (PCB_VIA.GetViaType() returns int 4 by default; pass ints); (3) EDA_UNITS_MILLIMETRES (7.0) renamed EDA_UNITS_MM (8+); (4) ImportSpecctraSES(board,path) overload added in 8.0; (5) 10.0 adds classes PCB_BARCODE, PCB_POINTS, FOOTPRINT_VARIANT, ZONE_LAYER_PROPERTIES, DRILL_SPAN, EDA_GROUP, LAYERS_CHECKED, PLOT_PARAMS, TEMPLATES and functions GetColorSettings, ResolveTextVars, DescribeRef etc.; removes StrPrintf/TimestampDir; (6) 10.0 board file version 20260206; 8.0 cannot parse 10.0 .pretty libs (FootprintLoad returns None), so each KiCad version must load its own share/kicad/footprints; (7) NewBoard(path) leaves board.GetFileName() empty in all four versions (call SetFileName yourself; otherwise PLOT_CONTROLLER names files '-F_Cu.gbr' and DRC report header is blank); (8) pcbnew.GetSemanticVersion()/GetBuildVersion() available for feature gating.
  - Evidence: Per-version constant dumps; diff of `^def`/`^class` between 9.0 and 10.0 pcbnew.py; step results across versions (7.0 fails step 16 only; 8/9 pass all 21; 10 passes all except WriteDRCReport).
- **[high, VERIFIED LOCALLY]** Fabrication outputs from SWIG headless work in all four versions: PLOT_CONTROLLER (SVG, Gerber X2 with job file) and EXCELLON_WRITER (PTH/NPTH drill) produced files; there is no STEP exporter in SWIG (UTILS_STEP_MODEL only has LoadSTEP/SaveSTEP/Scale/Translate for 3D models) -> use `kicad-cli pcb export step`.
  - Evidence: out10/-swig_py_plot_FCu.svg, out10/gerbers/{-F_Cu.gbr,...,-PTH.drl,-NPTH.drl}; dir(pcbnew.UTILS_STEP_MODEL).
- **[medium]** Known upstream bug history relevant to headless DRC via SWIG: #17504 (8.0.1 random assert in WriteDRCReport from python), #14901 (7.x multiple DRC calls -> duplicate lib nickname), #11006 (locale asserts in standalone scripts), #24124 (10.0.1 segfault in WriteDRCReport from an in-GUI SWIG plugin when markers exist; still open). These corroborate that WriteDRCReport is fragile and that wx asserts are the main headless blocker on Windows.
  - Evidence: GitLab issue pages via web search/fetch.
  - Sources: https://gitlab.com/kicad/code/kicad/-/issues/17504, https://gitlab.com/kicad/code/kicad/-/issues/14901, https://gitlab.com/kicad/code/kicad/-/work_items/11006, https://gitlab.com/kicad/code/kicad/-/work_items/24124

## Capabilities

- Headless, no-GUI, no-KiCad-running board manipulation in 7/8/9/10 from the bundled python.exe (or a same-minor venv with os.add_dll_directory): create board, outline, load standard-library footprints, place/rotate, create nets, assign pad nets, add tracks/vias/zones, fill zones (multi-threaded ZONE_FILLER), build connectivity/ratsnest counts, save/load .kicad_pcb
- Netlist -> PCB ('update PCB from schematic') can be implemented headlessly ONLY via SWIG (parse kicad-cli's kicadsexpr netlist, FootprintLoad + NETINFO_ITEM + pad.SetNet); neither kicad-cli nor the IPC API offers this
- Autorouting loop fully headless: ExportSpecctraDSN -> Freerouting jar (Java 17 for 1.9.0) -> ImportSpecctraSES -> save -> kicad-cli drc = 0 violations/0 unconnected (verified on 10.0)
- In-process DRC (WriteDRCReport) on 7/8/9 once wx asserts are nulled via ctypes; ~0.3 s per run, repeatable, catches shorts; the only headless DRC available for KiCad 7 (kicad-cli 7 has no `pcb drc`)
- Plot SVG/Gerber/drill from python (PLOT_CONTROLLER, EXCELLON_WRITER) in all versions; useful when a per-layer or custom plot config is needed beyond kicad-cli
- Fine-grained geometry/queries not exposed by kicad-cli: pad positions, bounding boxes, connectivity, zone filled area, footprint enumeration (FootprintEnumerate), lib table access (GetFootprintLibraries/GetFootprints)
- Version detection at runtime (GetSemanticVersion) to gate constant names and overloads

## Limitations

- KiCad 10: WriteDRCReport is unusable in the standalone interpreter (Pgm() assert -> modal dialog hang, or null deref if asserts suppressed); use kicad-cli pcb drc (10 adds --refill-zones --save-board)
- Deprecated since 9.0, scheduled for removal in 11.0; no compatibility guarantees; layer IDs, enum names and overloads already changed 7->8->9->10 (see version_notes)
- wxWidgets debug asserts in the release DLLs turn any internal assert into a blocking native dialog in a headless process on Windows -> every SWIG subprocess must run under a watchdog/timeout and should null wxTheAssertHandler via ctypes right after import
- Memory-ownership footguns: SetOutline(SHAPE_POLY_SET*) and any raw-pointer setter (object must be disowned or kept alive); only one BOARD per process (second BOARD -> flaky abort/segfault in SaveBoard on 8/9/10); GetBoard() is None outside the GUI; Refresh/UpdateUserInterface/FocusOnItem are GUI-only
- No schematic/eeschema bindings at all; no NETLIST reader/BOARD_NETLIST_UPDATER; no STEP exporter; no interactive router (PNS) exposure -> routing must come from an external router (Freerouting) via DSN/SES
- Interpreter lock-in: _pcbnew.pyd is cp39 (7.0) / cp311 (8/9/10); pip installs into the bundled python go to Documents/KiCad/<ver>/3rdparty (PYTHONUSERBASE) – keep wrapper deps out of KiCad's tree and drive the bundled interpreter as a subprocess
- NewBoard() leaves GetFileName() empty (affects plot file names, DRC report header, DSN name) – always SetFileName; env vars like KICAD10_FOOTPRINT_DIR are not auto-defined in the standalone process (set them or resolve library paths yourself)
- SaveBoard(path, board) with aSkipSettings=False also writes <name>.kicad_pro/.kicad_prl next to the board through the settings manager; use aSkipSettings=True when the wrapper owns the project files
- Foreign-version libraries: 8.0 cannot read 10.0 .pretty files (FootprintLoad returns None); pair each KiCad version with its own share/kicad/footprints
- 7.0 lacks ImportSpecctraSES(board, path) (GUI-only 1-arg form), lacks kicad-cli pcb drc entirely, and uses Python 3.9 + EDA_UNITS_MILLIMETRES

## Version notes

Measured with the same script on 7.0.6 / 8.0.1 / 9.0.5 / 10.0.0 (Windows, bundled interpreters): 7.0: Python 3.9.16, EDA_UNITS_MILLIMETRES (no EDA_UNITS_MM), ImportSpecctraSES has only the GUI 1-arg form, layer ids old scheme (B_Cu=31, Edge_Cuts=44, COUNT=60), no UTILS_STEP_MODEL, WriteDRCReport works (asserts disabled), kicad-cli 7 has no pcb drc. 8.0: Python 3.11.5, EDA_UNITS_MM, ImportSpecctraSES(board,path) added, old layer ids, WriteDRCReport works (asserts disabled), SaveBoard most sensitive to the extra-BOARD hazard. 9.0: layer ids renumbered (B_Cu=2, In1_Cu=4, Edge_Cuts=25, F_SilkS=5, COUNT=128), SWIG officially deprecated, IPC API introduced, WriteDRCReport works (asserts disabled; wxDateTime::Format assert otherwise hangs). 10.0: same layer scheme as 9, VIATYPE_* constants removed from the module (pass ints), WriteDRCReport broken headless (Pgm() null), new classes (PCB_BARCODE, PCB_POINTS, FOOTPRINT_VARIANT, ZONE_LAYER_PROPERTIES...), board format 20260206, nested 'Table' global fp-lib-table, kicad-cli gains --refill-zones/--save-board. All four: NewBoard leaves file name empty; SetOutline ownership bug; one-BOARD-per-process; pcbnew.GetSemanticVersion() available for gating. Removal planned for 11.0 -> a version-agnostic wrapper must treat SWIG as an optional backend selected per installed version, isolate it in a subprocess using that version's own python.exe, and keep the tool surface (create/place/net/route/fill/DRC/export) independent of it so kicad-cli + IPC API can take over for 11+.

## Recommendations

- Architecture: expose each capability as a backend-neutral tool (create_board, place_footprint, apply_netlist, route, fill_zones, run_drc, export_fab) and implement it with a per-version capability matrix: kicad-cli wherever it exists (DRC/exports/netlist/ERC), SWIG subprocess for what only SWIG can do headlessly (board construction, netlist->PCB, zone fill on 7-9, DSN/SES round trip, in-process DRC on 7-9), IPC API only when a KiCad GUI is running. This keeps 11.0 (SWIG removed) viable without rewriting tools.
- Run every SWIG job as a fresh subprocess of C:/Program Files/KiCad/<ver>/bin/python.exe with: -u -X faulthandler, a hard timeout/kill (wx assert dialogs hang), KICAD_CONFIG_HOME pointing at a wrapper-owned dir, KICAD<major>_FOOTPRINT_DIR/SYMBOL_DIR/3DMODEL_DIR set, and the ctypes wxTheAssertHandler=NULL shim right after import pcbnew; exactly one BOARD per process; always board.SetFileName(path); SaveBoard(..., aSkipSettings=True).
- Codify the ownership rules in the wrapper's SWIG layer: never call raw-pointer setters (SetOutline) without disowning; build zone outlines via z.Outline()/AppendCorner; add items only through BOARD.Add; keep references to NETINFO_ITEMs in a dict; BuildConnectivity() before ZONE_FILLER.Fill().
- Netlist->PCB: implement the s-expression netlist applier (kicad-cli sch export netlist --format kicadsexpr -> parse -> FootprintLoad via project+global fp-lib-table resolution -> NETINFO_ITEM -> pad.SetNet), with diff/update semantics (existing refs keep position; changed footprints replaced; stale nets removed) – this is the one piece neither kicad-cli nor kipy provides today.
- Routing: ship Freerouting (1.9.0 on Java 17 or 2.x on Java 21) as the router; SWIG ExportSpecctraDSN -> jar -> ImportSpecctraSES, then kicad-cli drc --refill-zones --save-board (10) or SWIG fill (7-9). Verified to reach 0 unconnected / 0 DRC on 10.0.
- DRC: use kicad-cli pcb drc --format json (8/9/10) as the canonical checker; enable SWIG WriteDRCReport only for 7.0 (no kicad-cli drc) and mark it unavailable on 10.0.
- On the in-KiCad plugin <-> VS Code channel: SWIG action plugins (PYTHON_ACTION_PLUGINS) can host a socket inside pcbnew today and have Pgm()/GetBoard()/WriteDRCReport available there, but they are removed in 11 – do not build the channel on SWIG; use the official IPC API plugin path for the 'live GUI' channel and keep SWIG strictly headless. A KiCad source build is NOT required for any of the SWIG findings here (everything was reproduced with installed binaries + faulthandler); it would only be needed to debug KiCad-side crashes like the WriteDRCReport Pgm() null (10.0) or #24124, or to build a patched pcbnew with a headless PGM init.
- Never hard-code numeric layer ids or enum ints; always use pcbnew.<symbol> at runtime and gate by GetSemanticVersion(); persist layer names ('B.Cu') not numbers in wrapper state.
- Use a same-minor system/venv Python only as a convenience for developers (os.add_dll_directory + sys.path); production should use the bundled interpreter to avoid ABI mismatch (3.12 fails, 3.9 needed for 7.0).
- Keep all test scripts from this task (C:/Users/usama/AppData/Local/Temp/claude/c--Upwork-KiCad/48a8df66-7213-479f-ab2e-a63b89135016/scratchpad/swig-pcbnew/{test_swig_board.py, wx_noassert.py, netlist_to_pcb.py, route_roundtrip.py, sys_python_import.py}) as the seed of the wrapper's SWIG conformance test suite run against every installed KiCad version.

## Experiments run

- **Main headless SWIG test (20 steps: NewBoard, Edge.Cuts, FootprintLoad x2, nets, pad nets, track, via, zone, ZONE_FILLER, connectivity, SaveBoard, LoadBoard, WriteDRCReport, ExportSpecctraDSN, ImportSpecctraSES, netlist symbol check, PLOT_CONTROLLER SVG, Gerber+drill, STEP symbol check) run on 10.0 / 9.0 / 8.0 / 7.0 bundled pythons**
  - Result: 9.0 and 8.0: 21/21 OK. 7.0: 20/21 (ImportSpecctraSES 2-arg overload missing). 10.0: all OK except 14_WriteDRCReport (Pgm() assert -> hang; access violation with asserts disabled). Outputs in out10/out9/out8/out7 incl. results.json, swig_test.kicad_pcb, .dsn, .ses, gerbers/, svg.
  - Command/code:

```
cd <scratch>/swig-pcbnew; for v in 10.0 9.0 8.0 7.0; do NOASSERT=1 OUTLINE_MODE=inplace timeout -k 5 180 "/c/Program Files/KiCad/$v/bin/python.exe" -u -X faulthandler test_swig_board.py "$PWD/out${v%%.*}" $v > run${v%%.*}.log 2>&1; done   (10.0 additionally with SKIP_STEPS=s14 NOASSERT=0 to get past the DRC crash)
```

  - Artifact: `C:/Users/usama/AppData/Local/Temp/claude/c--Upwork-KiCad/48a8df66-7213-479f-ab2e-a63b89135016/scratchpad/swig-pcbnew/test_swig_board.py`
- **Bisect of the random SaveBoard access violation (found ZONE.SetOutline ownership bug) and repetition tests of the three safe outline patterns**
  - Result: Buggy pattern: fill area 0.0 mm^2 and ~50% 'Windows fatal exception: access violation' in SaveBoard; all three safe patterns: 504 mm^2 fill and 0 crashes (8/8 additional inplace runs).
  - Command/code:

```
for mode in inplace appendcorner setoutline_disown; do for rep in 1 2 3; do OUTLINE_MODE=$mode STOP_AT=s12 "/c/Program Files/KiCad/10.0/bin/python.exe" -u -X faulthandler test_swig_board.py "$PWD/out10m" 10.0; done; done   (bisect_save.py / bisect2.py / bisect3.py / bisect4.py with feature flags; SAVE_EACH=1 instrumentation)
```

  - Artifact: `C:/Users/usama/AppData/Local/Temp/claude/c--Upwork-KiCad/48a8df66-7213-479f-ab2e-a63b89135016/scratchpad/swig-pcbnew/bisect2.py`
- **One-BOARD-per-process hazard: 5-6 repetitions per version with/without a second BOARD()/CreateEmptyBoard() object, with and without aSkipSettings**
  - Result: Single board: 0 failures in all versions. Extra board: 8.0 abort(3)/segv(139) in 3-5 of 6 runs; 9.0 1-2 of 5; 10.0 1 of 5; skip-settings does not fully avoid it. Traceback always inside pcbnew.SaveBoard.
  - Command/code:

```
./reps.sh 8.0 "newboard,fp,zone,fill" 5; ./reps.sh 8.0 "newboard,boardctor2,fp,zone,fill" 6; ./reps.sh 8.0 "newboard,emptyboard2,fp,zone,fill" 5; (same for 9.0 and 10.0; plus ',skip' and ',bsave' variants)
```

  - Artifact: `C:/Users/usama/AppData/Local/Temp/claude/c--Upwork-KiCad/48a8df66-7213-479f-ab2e-a63b89135016/scratchpad/swig-pcbnew/reps.sh`
- **Disable wx assert dialogs via ctypes (null the exported wxTheAssertHandler) and run WriteDRCReport headless; repeated calls; injected short; env var for footprint dir**
  - Result: Export ?wxTheAssertHandler@@3P6AXAEBVwxString@@H000@ZEA present in 7/8/9/10 wx base DLLs. 9.0: WriteDRCReport True in 0.30s, 0 violations with env var (2 lib_footprint_issues warnings without); 3 calls ok; 6 violations after adding a VCC-GND short. 8.0 and 7.0 also OK. 10.0: access violation in WriteDRCReport (Pgm() null).
  - Command/code:

```
"/c/Python311/python.exe" pe_exports.py AssertHandler <wxbase dlls>  ->  KICAD9_FOOTPRINT_DIR="C:/Program Files/KiCad/9.0/share/kicad/footprints" "/c/Program Files/KiCad/9.0/bin/python.exe" -u -X faulthandler drc_test.py out9/swig_test.kicad_pcb out9/py_drc_env.rpt ; drc_twice.py (3 calls + short)
```

  - Artifact: `C:/Users/usama/AppData/Local/Temp/claude/c--Upwork-KiCad/48a8df66-7213-479f-ab2e-a63b89135016/scratchpad/swig-pcbnew/wx_noassert.py`
- **kicad-cli validation of the SWIG-built board (DRC json+report, SVG export, 3D render PNG)**
  - Result: Found 0 violations, 0 unconnected items; SVG 6411 bytes; PNG rendered and visually matches (outline, R1, rotated R2, SIG track, GND track + via).
  - Command/code:

```
cd out10; kicad-cli pcb drc --format json --severity-all --units mm -o swig_drc_cli.json swig_test.kicad_pcb; kicad-cli pcb export svg --layers F.Cu,B.Cu,Edge.Cuts,F.SilkS --page-size-mode 2 --exclude-drawing-sheet -o swig_test_cli.svg swig_test.kicad_pcb; kicad-cli pcb render --side top --width 800 --height 600 --background opaque -o swig_test_top.png swig_test.kicad_pcb
```

  - Artifact: `C:/Users/usama/AppData/Local/Temp/claude/c--Upwork-KiCad/48a8df66-7213-479f-ab2e-a63b89135016/scratchpad/swig-pcbnew/out10/swig_drc_cli.json`
- **Headless autoroute round trip: SWIG unrouted board -> ExportSpecctraDSN -> Freerouting 1.9.0 (Java 17) -> ImportSpecctraSES -> SaveBoard -> kicad-cli drc + render**
  - Result: unrouted: 0 tracks, 5 unconnected -> DSN 3260 bytes -> freerouting exit 0 in 23.2 s (no GUI) -> SES imported: 14 tracks, 0 unconnected -> saved 23463 bytes -> kicad-cli: 0 violations, 0 unconnected; rr_top.png shows routes.
  - Command/code:

```
curl -sL -o freerouting/freerouting-1.9.0.jar https://github.com/freerouting/freerouting/releases/download/v1.9.0/freerouting-1.9.0.jar; "/c/Program Files/KiCad/10.0/bin/python.exe" -u -X faulthandler route_roundtrip.py "$PWD/route10" 10.0 "C:/Program Files/Eclipse Adoptium/jre-17.0.12.7-hotspot/bin/java.exe" "$PWD/freerouting/freerouting-1.9.0.jar"; kicad-cli pcb drc --format json --severity-all -o route10/rr_drc.json route10/rr.kicad_pcb
```

  - Artifact: `C:/Users/usama/AppData/Local/Temp/claude/c--Upwork-KiCad/48a8df66-7213-479f-ab2e-a63b89135016/scratchpad/swig-pcbnew/route_roundtrip.py`
- **Netlist -> PCB through SWIG (no netlist reader exists): kicad-cli netlist export of the pic_programmer demo, s-expr parse, FootprintLoad/NETINFO_ITEM/pad.SetNet, save, kicad-cli drc**
  - Result: 63 components / 111 nets parsed; 58 footprints placed (5 in a project-local lib skipped by the naive resolver); 111 nets, 184 pad assignments; board saved 270 KB; kicad-cli: 105 unconnected (unrouted), 30 placement-related violations from the naive grid.
  - Command/code:

```
cp -r "/c/Program Files/KiCad/10.0/share/kicad/demos/pic_programmer" netlist/; kicad-cli sch export netlist --format kicadsexpr -o pic_programmer.net pic_programmer/pic_programmer.kicad_sch; "/c/Program Files/KiCad/10.0/bin/python.exe" -u -X faulthandler ../netlist_to_pcb.py pic_programmer.net "$PWD/from_netlist.kicad_pcb" 10.0; kicad-cli pcb drc --format json -o from_netlist_drc.json from_netlist.kicad_pcb
```

  - Artifact: `C:/Users/usama/AppData/Local/Temp/claude/c--Upwork-KiCad/48a8df66-7213-479f-ab2e-a63b89135016/scratchpad/swig-pcbnew/netlist_to_pcb.py`
- **DLL/path pitfalls: import KiCad 10 pcbnew from system Python 3.11.8, Python 3.12.3 and a uv venv (3.11) with sys.path only / os.add_dll_directory / runtime PATH edit**
  - Result: naive/path: 'DLL load failed while importing _pcbnew'; dlldir (os.add_dll_directory): import OK, BOARD ok, FootprintLoad ok on both system 3.11 and venv; 3.12: 'Module use of python311.dll conflicts with this version of Python'.
  - Command/code:

```
for m in naive dlldir path; do "/c/Python311/python.exe" sys_python_import.py $m; done; "/c/Python312/python.exe" sys_python_import.py dlldir; uv venv --python 3.11 venv311; ./venv311/Scripts/python.exe sys_python_import.py dlldir
```

  - Artifact: `C:/Users/usama/AppData/Local/Temp/claude/c--Upwork-KiCad/48a8df66-7213-479f-ab2e-a63b89135016/scratchpad/swig-pcbnew/sys_python_import.py`
- **User-settings safety: sha256/mtime of %APPDATA%/kicad/{9.0,10.0}/kicad_common.json before/after SWIG runs; KICAD_CONFIG_HOME redirection**
  - Result: Files unchanged across my controlled runs (changes observed earlier coincided with a sibling task's pcbnew.exe GUI); with KICAD_CONFIG_HOME set, cfghome/9.0/kicad_common.json was created and the real config untouched.
  - Command/code:

```
stat/sha256sum kicad_common.json; run NewBoard+SaveBoard and WriteDRCReport in 9.0/10.0; KICAD_CONFIG_HOME="$PWD/cfghome" python drc_test.py ...; find cfghome -type f
```

  - Artifact: `C:/Users/usama/AppData/Local/Temp/claude/c--Upwork-KiCad/48a8df66-7213-479f-ab2e-a63b89135016/scratchpad/swig-pcbnew/cfghome`
- **API drift inventory: per-version constants, diff of top-level defs/classes 9.0 vs 10.0, VIATYPE presence, kicad-cli flag availability, doxygen doc sites**
  - Result: Layer renumbering in 9.0 (B_Cu 31->2, Edge_Cuts 44->25, COUNT 60->128); VIATYPE_* present 7/8/9, absent 10; 12 new defs + 11 new classes in 10.0, 2 removed; --refill-zones/--save-board only in kicad-cli 10; kicad-cli 7 has only 'pcb export'; doxygen-python-8.0/9.0/10.0 all HTTP 200.
  - Command/code:

```
for v in 7.0 8.0 9.0 10.0: python -c 'import pcbnew as p; print(p.F_Cu,p.B_Cu,p.In1_Cu,p.Edge_Cuts,p.F_SilkS,p.User_1,p.PCB_LAYER_ID_COUNT)'; grep VIATYPE_THROUGH pcbnew.py; diff <(grep -oE '^def [A-Za-z_0-9]+' 9.0/pcbnew.py|sort -u) <(... 10.0 ...); kicad-cli pcb drc --help | grep -oE -- '--refill-zones|--save-board'; curl -I https://docs.kicad.org/doxygen-python-10.0/
```

  - Artifact: `C:/Users/usama/AppData/Local/Temp/claude/c--Upwork-KiCad/48a8df66-7213-479f-ab2e-a63b89135016/scratchpad/swig-pcbnew/run10.log`
