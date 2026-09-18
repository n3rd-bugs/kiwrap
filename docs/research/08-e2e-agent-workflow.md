# e2e-agent-workflow: natural-language spec -> schematic -> ERC -> PCB -> placement -> routing -> DRC -> fabrication, as a version-agnostic KiCad wrapper driven by AI agents (tool catalog + skills)

## Findings

- **[high, VERIFIED LOCALLY]** kicad-cli 10.0.0 `sch erc --format json` and `pcb drc --format json` produce stable, machine-readable reports. ERC JSON shape: {"$schema":"https://schemas.kicad.org/erc.v1.json", coordinate_units, date, ignored_checks[{key,description}], included_severities[], kicad_version, source, sheets[{path (e.g. "/ampli_ht_vertical/"), uuid_path, violations[{type, severity, description, items[{description, pos{x,y}, uuid}]}]}]}. DRC JSON shape: {"$schema":"https://schemas.kicad.org/drc.v1.json", coordinate_units, date, ignored_checks, included_severities, kicad_version, source, violations[...], unconnected_items[...], schematic_parity[...]} with the same violation/item objects. KiCad 8.0.1 emits the same shapes minus ignored_checks/included_severities. Observed ERC types: pin_not_connected, unconnected_wire_endpoint, label_dangling, pin_not_driven, power_pin_not_driven, wire_dangling, isolated_pin_label, endpoint_off_grid. Observed DRC types: clearance, shorting_items, courtyards_overlap, silk_over_copper, solder_mask_bridge, silk_overlap, hole_clearance, hole_to_hole, track_dangling, holes_co_located, pth_inside_courtyard, lib_footprint_issues, unconnected_items, net_conflict, footprint_symbol_mismatch.
  - Evidence: Ran ERC on complex_hierarchy demo (3 sheets, 0 violations, 11.8 s) and on pic_programmer with 6 wires deleted (12 violations); ran DRC on pic_programmer with a footprint moved onto another and a stray track (37 violations, 2 unconnected). Printed key sets with python json. Files: scratchpad/e2e-agent-workflow/out/erc_pic.json, out/drc_broken.json, out/erc_pic_8.0.json, out/drc_8.json.
  - Sources: local: kicad-cli.exe 10.0.0, 9.0.5, 8.0.1
- **[high, VERIFIED LOCALLY]** BUG the wrapper must correct: ERC JSON item positions are 100x too small in KiCad 8.0.1, 9.0.5 and 10.0.0 (schematic IU scale 1e4/mm is divided as if 1e6/mm). The text report is correct. DRC JSON positions are correct. Wrapper rule: pos_mm = json_pos * 100 (also wrong when --units in).
  - Evidence: D3 pin 1 is at (87.63, 46.99) mm in the file and in the `--format report` output ('@(87.63 mm, 46.99 mm)'), while JSON says {x:0.8763,y:0.4699} (mm) and {x:0.0345,y:0.0185} (in). Same 100x factor in 8.0.1 (RV1 pin at 2.4384) and 9.0.5. Independent confirmation: mixelpixx/Konnect issue #541 reports identical 100x error on KiCad 10.0.5/10.0.6.
  - Sources: https://github.com/mixelpixx/Konnect/issues/541, https://gitlab.com/kicad/code/kicad/-/issues/16924
- **[high, VERIFIED LOCALLY]** kicad-cli capability matrix: 7.0.6 has ONLY `pcb export {drill,dxf,gerber,gerbers,pdf,pos,step,svg}` and `sch export {dxf,hpgl,netlist,pdf,ps,python-bom,svg}` -> no ERC, no DRC, no BOM, no render, no stats from the CLI. 8.0.1 adds `pcb drc`, `sch erc` (both with --format json), `sch export bom`, glb/ipc2581/vrml. 9.0.5 adds `pcb render` (3D PNG/JPEG), `jobset run`, odb, ipcd356, gencad, brep/stl/ply/xao. 10.0.0 adds `pcb export stats --format json`, `pcb upgrade`/`sch upgrade`, `pcb import`, 3dpdf/stpz/u3d, `--variant` on exports, DRC `--refill-zones --save-board`, ERC/DRC `--exit-code-violations` (exit code 5 when violations exist).
  - Evidence: Ran `kicad-cli <pcb|sch> [export] --help` on all four installs and captured the subcommand lists; ran `--exit-code-violations` on a schematic with errors (exit 5) and a clean one (exit 0).
  - Sources: local: C:/Program Files/KiCad/{7.0,8.0,9.0,10.0}/bin/kicad-cli.exe
- **[high, VERIFIED LOCALLY]** Visual feedback is fully headless in 9/10: `pcb render` (raytraced PNG, options --side top|bottom|left|right|front|back, --rotate X,Y,Z, --perspective, --zoom, --pan, --pivot, --quality basic|high|user, --floor, --background transparent|opaque, --preset, lights) takes ~2.4 s for a 63-part board at 1200x800 and resolves standard 3D models without env setup. 2D views: `pcb export svg --mode-single --layers F.Cu,B.Cu,F.SilkS,Edge.Cuts --page-size-mode 2 --exclude-drawing-sheet` (0.4 s) and `sch export svg --pages N` (0.5 s, one SVG per sheet) — the SVGs are 0.5–3 MB so they must be rasterized; PyMuPDF opens KiCad SVGs directly and rasterizes in 0.1–0.2 s (1457x996 board, 1684x1191 schematic, both legible). KiCad 8 has no `pcb render`; 7 has neither render nor ERC/DRC; for those use svg->PNG or pcbnew PLOT_CONTROLLER.
  - Evidence: Produced out/render_top.png, out/render_iso.png, out/board_top.svg->png, out/sch_svg/*.svg->png and inspected the images (traces, refs, layer colours visible; schematic text readable at 2x zoom).
  - Sources: local kicad-cli 10.0.0; PyMuPDF 1.28.2
- **[high, VERIFIED LOCALLY]** The IPC API in KiCad 9/10 is PCB-editor only and GUI-only. Live probe of the running pcbnew 10.0.0: KiCad.get_version() -> 10.0.0, api 10.0.6; get_open_documents(DOCTYPE_SCHEMATIC) -> ApiError 'no handler available for request of type kiapi.common.commands.GetOpenDocuments'; get_board() works (63 footprints, 112 nets, 371 tracks, 7 vias, 1 zone). KiCad 10.0 registered PCB handlers (from source): GetItems/GetItemsById/CreateItems/UpdateItems/DeleteItems, Begin/EndCommit (undoable), GetNets/GetNetClasses/SetNetClasses, GetBoardStackup, Get/SetBoardEnabledLayers, RefillZones, InteractiveMoveItems, HitTest, GetBoundingBox, GetConnectedItems, GetItemsByNet, SaveDocument/SaveCopyOfDocument/RevertDocument, SaveDocumentToString/ParseAndCreateItemsFromString, selection, variants, embedded files, GetTitleBlockInfo, InjectDrcError, RunAction. There is NO DRC run, NO plot/export, NO netlist import, NO footprint-from-library in the 10 API.
  - Evidence: Connected read-only with kipy to the already-running pcbnew (ipc://%TEMP%/kicad/api.sock); grep of registerHandler<> in pcbnew/api/api_handler_pcb.cpp (10.0 branch) and common/api/api_handler_editor.cpp.
  - Sources: https://dev-docs.kicad.org/en/apis-and-binding/ipc-api/for-addon-developers/index.html, https://gitlab.com/kicad/code/kicad/-/raw/10.0/pcbnew/api/api_handler_pcb.cpp
- **[high]** IPC RunAction in 10.0 is a thin pass-through: `frame()->GetToolManager()->RunAction(name, true)`. Any registered tool action can be fired by name, e.g. pcbnew.ZoneFiller.zoneFillAll, pcbnew.InteractiveRouter.Autoroute ('Attempt Finish Selected (Autoroute)', Shift+F: sequentially autoroutes all selected pads with KiCad's own PNS router, new in 10), pcbnew.Autoplacer.autoplaceSelected / autoplaceOffboard, pcbnew.GlobalEdit.cleanupTracksAndVias, pcbnew.Control.repairBoard. Actions that open dialogs (pcbnew.DRCTool.runDRC, pcbnew.EditorControl.importNetlist / generateGerbers / exportSpecctraDSN / importSpecctraSession) block on the GUI and are not usable unattended. This is the ONLY way to reach KiCad's rule-aware PNS router programmatically in 9/10, and it needs a running GUI.
  - Evidence: Read handleRunAction in api_handler_pcb.cpp (10.0) and action names/tooltips in pcbnew/tools/pcb_actions.cpp (10.0). Not executed (a user pcbnew instance was already running; rule: never start a second GUI, never touch someone else's session).
  - Sources: https://gitlab.com/kicad/code/kicad/-/raw/10.0/pcbnew/api/api_handler_pcb.cpp, https://gitlab.com/kicad/code/kicad/-/raw/10.0/pcbnew/tools/pcb_actions.cpp
- **[high, VERIFIED LOCALLY]** KiCad 11 (master) is where the API becomes complete: eeschema/api/api_handler_sch.cpp registers GetItems/GetItemsById, PlaceSymbolFromLibrary, GetSchematicNetlist, GetSchematicHierarchy, RunSchematicJobExport{BOM,Netlist,Pdf,Png,Svg,Dxf,Ps}, HighlightNets, CrossProbeAnnounce, SyncSelection, variants, and inherits CreateItems/UpdateItems/DeleteItems/commits from the editor base; PCB gains ImportNetlist, PlaceFootprintFromLibrary, Get/SetBoardDesignRules, Get/SetCustomDesignRules, Get/SetBoardPlotSettings, RunBoardJobExport{Gerbers,Drill,Pos,Pdf,Png,Svg,Render,Stats,3D,Odb,Ipc2581,IpcD356,Gencad,Dxf,Ps}. kicad-cli gains `api-server` (headless API host, kicad/cli/command_api_server.cpp, eeschema headless_sch_context), `pcb diff`, `sch diff`, `fp diff`, `sym diff`, `gerber diff`, `git-mergedriver`, `mergetool`, `pcb export png`, `pcb export stackup`. The SWIG pcbnew bindings are removed in 11 (kicad-python README). kicad-python (kipy) 0.8.0 on PyPI (2026-08-30) ships a `schematic.py` wrapper but it fails to import (ImportError: PageSettings) because the bundled protos are older; the README lists schematic support and headless api-server launch under '0.x.y (TBD)'. A KiCad dev (craftyjon) confirmed api-server 'is a new feature for V11', not 10.x.
  - Evidence: GitLab API tree listings of eeschema/api, common/jobs, kicad/cli on master vs 10.0; grep of registerHandler<> in master handlers; `uv pip install kicad-python==0.8.0` then `import kipy.schematic` raised ImportError; README fetched from gitlab main branch.
  - Sources: https://gitlab.com/kicad/code/kicad-python/-/raw/main/README.md, https://gitlab.com/kicad/code/kicad/-/raw/master/eeschema/api/api_handler_sch.cpp, https://gitlab.com/kicad/code/kicad/-/raw/master/pcbnew/api/api_handler_pcb.cpp, https://forum.kicad.info/t/is-the-kicad-cli-api-server-option-going-to-be-in-kicad-v11-or-v10-x-x/69411
- **[high, VERIFIED LOCALLY]** The bundled SWIG `pcbnew` module in 7.0.6, 8.0.1, 9.0.5 and 10.0.0 all expose FootprintLoad/FootprintEnumerate, ExportSpecctraDSN, ImportSpecctraSES, ZONE_FILLER, PLOT_CONTROLLER, EXCELLON_WRITER, VECTOR2I_MM, CONNECTIVITY_DATA and WriteDRCReport. In 10.0 standalone python, LoadBoard/SaveBoard, footprint move, track add, NETINFO_ITEM creation, pad.SetNet, ZONE_FILLER.Fill (0.44 s), GetConnectivity().GetUnconnectedCount(), ExportSpecctraDSN and ImportSpecctraSES all work headless; WriteDRCReport does NOT (asserts `"process" failed in Pgm()` because it needs the running app) -> DRC must go through kicad-cli. There is no netlist reader in SWIG (no NETLIST_READER since v6). Load->Save round trip of a 10.0 board is byte-identical (0 diff lines), so git diffs stay minimal.
  - Evidence: pcb_probe.py and inline scripts run with each version's bin/python.exe; diff of out/roundtrip1.kicad_pcb vs original = 0 lines.
  - Sources: local: C:/Program Files/KiCad/*/bin/python.exe
- **[high, VERIFIED LOCALLY]** Headless 'Update PCB from Schematic' for 7–10 can be implemented by the wrapper in ~60 lines: `kicad-cli sch export netlist` -> parse comps/nets -> pcbnew.BOARD(), NETINFO_ITEM per net, FootprintLoad(<lib>.pretty, name) per component, SetReference/SetValue/SetPath(KIID_PATH), pad.SetNet, add Edge.Cuts, SaveBoard. `kicad-cli pcb drc --schematic-parity` then validates the sync (it locates the .kicad_sch by name next to the .kicad_pcb, no .kicad_pro needed). Gotcha: FootprintLoad does not set the library nickname, so parity reports footprint_symbol_mismatch until fp.SetFPID(LIB_ID(lib,name)) is called.
  - Evidence: netlist_to_pcb.py built gen/agent_test.kicad_pcb (3 footprints, 6 nets) from gen/agent_test.net; DRC reported 0 violations, 3 unconnected (expected, unrouted) and 3 footprint_symbol_mismatch parity warnings ('C_0603_1608Metric doesn't match ... Capacitor_SMD:C_0603_1608Metric').
  - Sources: local
- **[high, VERIFIED LOCALLY]** Autorouting is solved headlessly with Freerouting: export DSN via SWIG, run `java -jar freerouting-2.4.1.jar -de board.dsn -do board.ses -mp 20 -mt 1`, import SES via SWIG. On the 63-part/86-net pic_programmer board (tracks stripped) Freerouting fan-out + routing finished in 2.7 s (0 unrouted, 1 violation), 12 s wall including JVM start; ImportSpecctraSES added 265 tracks. Java requirement: 2.4.1 and 2.2.4 need Java 25 (class file 69), 2.1.0 needs Java 21; the machine's Java 8/11/17 cannot run them, so the wrapper must ship or download a portable JRE (Adoptium API zip, 58 MB, worked). Gotchas found: (1) stale zone fills after import produced 115 phantom `clearance` errors -> run `kicad-cli pcb drc --refill-zones --save-board` (or ZONE_FILLER) before judging DRC (115 -> 1); (2) Freerouting ignores copper-layer text, producing a shorting_items error against PCB text '1=>>' on F.Cu; (3) Freerouting warns its multi-threaded optimizer 'is known to generate clearance violations' -> use `-mt 1`; (4) `-l en` created a stray ./en/ log dir and it writes freerouting.json in %LOCALAPPDATA% -> run in a scratch cwd.
  - Evidence: Full run logged; out/pic_routed.ses, out/pic_routed.kicad_pcb, out/drc_routed.json (121 viol), out/drc_routed2.json after refill (2 viol: 1 clearance, 1 shorting_items; 2 unconnected), out/render_routed.png inspected.
  - Sources: https://github.com/freerouting/freerouting/releases, https://api.adoptium.net/v3/binary/latest/25/ga/windows/x64/jre/hotspot/normal/eclipse?project=jdk
- **[high, VERIFIED LOCALLY]** Schematic CAPTURE on 7–10 has no official API: it must be done by writing .kicad_sch s-expressions (embedding `lib_symbols` copied from the library, placing `symbol` instances, `wire`, `label`/`global_label`/`hierarchical_label`, `junction`, `no_connect`, `sheet`+`pin`, and the per-project `instances` block). KiCad 10 opens files written in the 9.0 format (version 20250114) without complaint, so a wrapper can target one older writer format for 9 and 10 (and use `kicad-cli sch upgrade` when needed). Third-party writers are unreliable on geometry: kicad-sch-api 0.5.6 computes pin positions with the wrong Y sign (KiCad symbol space is Y-up, schematic space is Y-down), so labels intended for R1 pin 1 landed on pin 2 and U1 GND stayed unconnected; ERC passed for the resistor/cap because the labels still touched *a* pin — only the exported netlist (`/SIG [R1.1]`, `/VCC_3V3 [C1.2, R1.2, U1.2]`) exposed the swap. Also its search_symbols() returned nothing for ESP32-S3-WROOM / USB_C_Receptacle / MCP73831 until libraries are discovered, while get_symbol_info('RF_Module:ESP32-S3-WROOM-1') worked (41 pins).
  - Evidence: gen_sch.py -> gen/agent_test.kicad_sch; kicad-cli 10 ERC (10 violations, listed) and `sch export netlist` parsed into net->pins map; compared with Device.kicad_sym pin definitions (pin 1 at (0,3.81,270)).
  - Sources: local; https://github.com/circuit-synth/mcp-kicad-sch-api
- **[high, VERIFIED LOCALLY]** Annotation is stored twice in KiCad 7+ schematics: the symbol's `(property "Reference" ...)` AND the `(instances (project ... (path "/<uuid>" (reference "R1") (unit 1))))` block; the netlist exporter uses the instances block (setting the property to R? still exported ref="R1"). kicad-cli has no `annotate` command, so the wrapper must assign references itself and write both places consistently; unannotated symbols are not flagged by kicad-cli ERC.
  - Evidence: sed-modified gen/unannotated.kicad_sch (property -> R?), ERC showed no annotation violation, `sch export netlist --format kicadxml` still contained ref="R1".
  - Sources: local
- **[high, VERIFIED LOCALLY]** LCSC/JLCPCB parts: easyeda2kicad 1.0.1 (`python -m easyeda2kicad --full --lcsc_id=C424093 --output <dir>/lcsc`) fetched symbol (.kicad_sym v20211014), footprint (legacy `module` format .kicad_mod) and STEP+WRL for MCP73831T-2ACI/OT in 3.9 s; KiCad 10 FootprintLoad reads it (5 pads, model path set), `kicad-cli sym upgrade`/`fp upgrade` rewrite them to current formats (20251024 / 20260206). The EasyEDA API also returns lcsc {number, price, stock, url} and package name; `datasheet` was null for this part, so datasheet lookup needs a second source (LCSC product page / jlcparts DB). Parametric 'basic parts' search: yaqwsx/jlcparts (SQLite, daily), CDFER/jlcpcb-parts-database (3 MB CSV of basic+preferred parts, daily), @typecad/jlcpcb-parts fuzzy search CLI.
  - Evidence: Ran easyeda2kicad in the venv; validated with pcbnew.FootprintLoad and kicad-cli sym/fp upgrade; queried EasyedaApi().get_info_from_easyeda_api('C424093').
  - Sources: https://github.com/yaqwsx/jlcparts, https://github.com/CDFER/jlcpcb-parts-database, https://github.com/lrks/jlcpcb-economic-parts
- **[high, VERIFIED LOCALLY]** Fabrication outputs are fast and complete from kicad-cli 10: gerbers (25 files incl. job file, --subtract-soldermask --use-drill-file-origin), drill (--excellon-separate-th --generate-map --map-format gerberx2), pos CSV (mm, both sides), stats JSON and BOM CSV together took 1.8 s; STEP with --subst-models took 1.4 s. Quirks: stats JSON encodes numbers as strings with units and a mojibake unit ('area': '15851.581 mmÂ²') -> parse with regex; pos CSV has negated Y (-78.867) by KiCad convention; BOM `--fields` accepts custom fields (e.g. LCSC) and `--group-by Value,Footprint`.
  - Evidence: fab/gerbers/*, fab/pos.csv, fab/stats.json, fab/bom.csv, fab/board.step produced and inspected.
  - Sources: local
- **[high, VERIFIED LOCALLY]** Library conventions needed by a version-agnostic wrapper: env vars are version-suffixed (`${KICAD10_SYMBOL_DIR}`, `${KICAD10_FOOTPRINT_DIR}`, `${KICAD10_3DMODEL_DIR}`; KICAD9_*, KICAD8_*, KICAD7_* likewise); KiCad 10 installs 223 symbol libs and 155 footprint libs; the user's global fp-lib-table now contains a single `(type "Table")` entry pointing at share/kicad/template/fp-lib-table (new nesting in 10), so lib resolution must follow Table entries. The IPC socket default on Windows is `ipc://%TEMP%\kicad\api.sock`; the API is enabled per-user in kicad_common.json `api.enable_server` (false by default) and can be pre-set in a scratch KICAD_CONFIG_HOME.
  - Evidence: Read-only inspection of %APPDATA%/kicad/10.0/{kicad_common.json,fp-lib-table,sym-lib-table} and share/kicad/template/fp-lib-table; kipy/kicad.py _default_socket_path().
  - Sources: local
- **[high]** Existing agent-facing KiCad tooling converges on the same architecture and lessons: blwfish/kicad-mcp (17 router tools: schematic, pcb, audit, drc, autoroute[FreeRouting], library, project, analyze, export, lcsc, schematic_layout, design + build_pcb_from_schematic/panelize/estimate_board_size/suggest_placement) runs PCB ops as subprocesses of KiCad's bundled python (load-modify-save per call, params via JSON temp files) and schematics via kicad-sch-api in-process; its agent rules: NEVER manually route (LLMs cannot compute clearances) -> autoroute; NEVER guess lib/footprint names -> search index first; NEVER mutate the same file concurrently; delegate read-only queries to sub-agents to keep context lean; DRC triage table (must-fix: tracks_crossing, shorting_items, clearance, unconnected_items; tolerable on prototypes: courtyards_overlap, starved_thermal, silk_*). circuit-synth/mcp-kicad-sch-api, Seeed-Studio/kicad-mcp-server (analysis, KiCad 9+), mixelpixx/Konnect (KiCAD-MCP-Server) are the other active projects.
  - Evidence: Fetched README.md, TOOLS.md, AGENT-INSTRUCTIONS.md from github.com/blwfish/kicad-mcp; web search results for the others.
  - Sources: https://github.com/blwfish/kicad-mcp, https://raw.githubusercontent.com/blwfish/kicad-mcp/main/AGENT-INSTRUCTIONS.md, https://github.com/circuit-synth/mcp-kicad-sch-api, https://github.com/Seeed-Studio/kicad-mcp-server, https://github.com/mixelpixx/KiCAD-MCP-Server
- **[medium]** A KiCad source build on Windows needs Visual Studio 2019/2022 (Desktop C++ + CMake tools), vcpkg pinned to commit ef7dbf94b9198bc58f45951adcf1f041fcbc5ea0 at a drive root, swigwin >= 4.1.1, and the KiCad NuGet binary cache (`nuget.exe sources add -Name kicad-gitlab -Source https://gitlab.com/api/v4/projects/27426693/packages/nuget/index.json`); with the cache the first configure+build is ~1–10 minutes, without it 'several hours'; run from build dir with KICAD_RUN_FROM_BUILD_DIR=1 and PYTHONHOME/PYTHONPATH set in launch.vs.json. It is only genuinely required to (a) step through IPC handlers / DRC / PNS internals when a KiCad-side bug blocks the wrapper, (b) add or backport API handlers (e.g. DRC-over-API, schematic API from master) and (c) build 11-nightly to prototype the headless api-server path; everything in the 7–10 workflow above ran on installed binaries.
  - Evidence: dev-docs Windows MSVC build page; all experiments here used installed binaries only.
  - Sources: https://dev-docs.kicad.org/en/build/windows-msvc/index.html

## Capabilities

- Structured ERC/DRC feedback on 8/9/10 via `kicad-cli sch erc|pcb drc --format json --severity-all [--schematic-parity] [--refill-zones --save-board] [--exit-code-violations]` (exit 5 on violations); per-sheet path in ERC lets the agent map errors to hierarchical sheets
- Visual feedback fully headless: 3D PNG (`pcb render`, 9/10), 2D layer PNG (pcb export svg -> PyMuPDF raster, 7–10), per-page schematic PNG (sch export svg --pages -> PyMuPDF), symbol/footprint preview (`sym export svg`, `fp export svg`)
- Board mutation headless on 7–10 with the bundled SWIG python: create board, load footprints from any .pretty, create nets, assign pad nets, place/rotate/flip, add tracks/vias/zones, fill zones, edge cuts, design settings, Specctra DSN export / SES import, byte-stable save
- Headless netlist->PCB sync (wrapper-implemented) verified by `drc --schematic-parity`
- Autorouting headless with Freerouting 2.x (DSN in, SES out; seconds for a 60-part board) plus KiCad-native PNS autoroute-selected via IPC run_action when a GUI is open (10)
- Netlist export in kicadsexpr/kicadxml/spice/orcadpcb2/pads/allegro; BOM CSV with custom fields and grouping; python-bom XML
- Full fab bundle in ~3 s: gerbers+job, excellon+maps, pos CSV, STEP/GLB/VRML/3D-PDF, IPC-2581, ODB++, IPC-D-356, stats JSON, PDF/SVG/DXF plots; `jobset run` (9/10) for reproducible output sets
- Live GUI session (9/10 PCB only) via IPC: read/write items in undoable commits, selection/highlight, refill zones, run_action, save/revert, document-as-string; KiCad 11 extends this to schematics, jobs (exports) and headless `kicad-cli api-server`
- LCSC/JLCPCB part import (symbol+footprint+STEP) in ~4 s with easyeda2kicad; price/stock from EasyEDA API; basic-parts parametric search from jlcparts/CDFER databases
- Git-friendly text formats: deterministic pcbnew round-trip; KiCad 11 adds pcb/sch/fp/sym/gerber diff commands and a git merge driver

## Limitations

- No schematic API before KiCad 11: schematic capture must be done by writing .kicad_sch s-expressions (own writer or kicad-skip/kicad-sch-api), and there is no headless 'annotate', 'update PCB from schematic', 'assign footprints', or 'autoplace symbols' in kicad-cli 7–10
- ERC JSON coordinates are 100x too small (8/9/10) — must be corrected by the wrapper or read from the text report
- WriteDRCReport in standalone SWIG python asserts (needs Pgm()) — DRC only via kicad-cli (8+) or inside a running GUI; KiCad 7 has no CLI ERC/DRC at all (7 would need an in-GUI SWIG action plugin or an upgrade)
- IPC API 9/10: PCB only, requires a running GUI with api.enable_server=true, no DRC/plot/netlist-import handlers; dialogs opened by run_action block; kicad-python 0.8.0's schematic module is broken/unreleased
- SWIG pcbnew is removed in 11 -> any wrapper feature built on SWIG (DSN/SES, FootprintLoad, ZONE_FILLER, headless netlist sync) needs an IPC/api-server implementation for 11+
- Third-party schematic writers have geometry bugs (kicad-sch-api Y-sign) and ERC does not catch 'label on the wrong pin' — only netlist-vs-intent comparison does
- Freerouting needs Java 21/25 (not on PATH here), ignores copper text and stale zone fills, non-deterministic across passes, multi-thread optimizer unsafe (-mt 1), writes config/log files outside the project
- Zone fills go stale after any track edit; DRC on unrefilled zones reports phantom clearance errors
- Large schematic SVGs (3 MB) and full-board images exceed what an agent should ingest; need rasterization, region crops and compact JSON summaries instead of raw files
- Concurrent writers corrupt files: every mutating tool must be serialized per project and re-load from disk (no long-lived in-memory board across calls unless using the IPC session)
- stats JSON has unit strings with mojibake; pos CSV negates Y; netlist tstamps/sheetpath parsing needs care in hierarchical designs (component `tstamps` vs sheetpath `tstamps`)
- Cannot run the GUI-dependent experiments here (a foreign pcbnew.exe was already running; policy forbids a second GUI), so IPC write paths, run_action autoroute and plugin.json discovery are source/doc-verified only

## Version notes

7.0: kicad-cli has only pcb/sch export (gerbers, drill, pos, step, svg, pdf, dxf; netlist, python-bom); no ERC, no DRC, no BOM, no render, no stats, no jobset; SWIG pcbnew present (DSN/SES, FootprintLoad, ZONE_FILLER, PLOT_CONTROLLER); no IPC API. Treat 7 as 'export-only + SWIG'; ERC/DRC would require an in-GUI SWIG plugin, so recommend documenting 7 as best-effort. 8.0: adds `sch erc` and `pcb drc` with --format json (positions in ERC 100x too small), `sch export bom`, glb/ipc2581/vrml; still no render/stats/jobset, no IPC. 9.0: adds `pcb render` (3D PNG), `jobset run`, odb/ipcd356/gencad/brep/stl/ply/xao; IPC API v1 (PCB editor only, GUI only; kicad-python 0.1–0.5); SWIG deprecated but present. 10.0: adds `pcb export stats --format json`, `pcb/sch upgrade`, `pcb import`, 3dpdf/stpz/u3d, `--variant` on exports, DRC `--refill-zones/--save-board`, ERC/DRC `--exit-code-violations`, ERC/DRC JSON gain ignored_checks/included_severities; IPC adds groups, barcodes, reference images, SetTitleBlockInfo, variants, InjectDrcError; GUI gains PNS 'Attempt Finish Selected (Autoroute)' reachable via IPC run_action; file formats: sch 20260101, pcb 20260206, sym 20251024 (10 reads 9-format files fine; older versions cannot read newer files, so the writer must emit the format of the OLDEST KiCad that will open the project); library env vars KICAD10_*; global fp-lib-table uses nested `Table` type. 11 (master, unreleased): SWIG removed; schematic IPC (items CRUD, PlaceSymbolFromLibrary, GetSchematicNetlist/Hierarchy, schematic export jobs), PCB ImportNetlist, PlaceFootprintFromLibrary, design-rule get/set, all export jobs over API; `kicad-cli api-server` headless; `pcb/sch/fp/sym/gerber diff`, `git-mergedriver`, `mergetool`, `pcb export png`; kicad-python gets get_schematic + auto-launch of api-server. Wrapper strategy: detect `kicad-cli version` at runtime and pick a backend per capability (cli-json / cli-svg / swig-subprocess / ipc-gui / ipc-headless / file-writer); keep the file writer format-versioned; gate features with a capability table rather than version numbers.

## Recommendations

- ARCHITECTURE: build the wrapper as a Python 'kicad-agent' service with pluggable backends selected per capability at runtime: (1) FILE backend — own s-expression reader/writer for .kicad_sch/.kicad_pcb/.kicad_pro/.kicad_dru (schematic capture, annotation, project setup) with per-version format profiles; (2) CLI backend — kicad-cli for ERC/DRC JSON, netlist, BOM, SVG/PNG, render, fab exports, jobsets; (3) SWIG backend — a subprocess runner using <KiCad>/bin/python.exe for board mutation, footprint loading, netlist sync, DSN/SES, zone fill (7–10 only); (4) IPC backend — kipy against a running GUI (9/10, PCB) or `kicad-cli api-server` (11+, schematic+PCB+jobs). Expose the same tool contract over MCP (stdio) so Claude Code, Copilot agent mode and Cursor can all drive it; the VS Code extension hosts the MCP server, renders images/reports, and shows diffs.
- IN-KICAD PLUGIN <-> VS CODE CHANNEL — verdict: ADOPT as an optional 'live session' adapter, NOT as the core. What it buys over kicad-cli/IPC-client-only: (a) presence/selection/cross-probe context from the human's open editor ('fix this' on a selected footprint or DRC marker), (b) undoable agent edits applied as IPC commits while the user watches, (c) access to GUI-only engines in 9/10: KiCad's PNS router (`pcbnew.InteractiveRouter.Autoroute` on a selection), Autoplacer, zoneFillAll, repairBoard, and in-GUI DRC (an action plugin can call WriteDRCReport/GetBoard in-process), (d) a launcher button inside KiCad. Implementation: an IPC plugin (plugin.json with runtime 'python' or 'exec', schema share/kicad/schemas/api.v1.schema.json) whose action starts a small client that reads KICAD_API_SOCKET/KICAD_API_TOKEN, connects to the VS Code extension over a localhost WebSocket (token-authenticated), and relays: open documents, selection, hit-test, item CRUD/commits, run_action, refill_zones, save. For KiCad 7/8 (no IPC) the same bridge can be a legacy SWIG ActionPlugin that embeds the client. Why not the core: it requires the GUI to be open, adds no capability for schematics before 11, cannot run DRC/exports in 9/10 without dialogs, and SWIG action plugins die in 11. The headless path (FILE+CLI+SWIG, then IPC-headless in 11) must work with KiCad closed for CI, tests and agents.
- SOURCE BUILD — do NOT set it up now for 7–10 development; everything needed ran on installed binaries. Prepare a build recipe (VS 2022 + pinned vcpkg + swigwin + KiCad NuGet cache, x64-Debug preset, KICAD_RUN_FROM_BUILD_DIR=1) and trigger it only when: an IPC handler misbehaves and must be stepped through, a missing handler must be added/backported (e.g. RunDRC over API, schematic API on 10.x), or the 11 nightly api-server needs testing before packages exist. Budget: ~1–10 min with the binary cache, hours without; keep it on a separate drive root.
- SELF-CORRECTION LOOP the agent must follow after every mutating step: (1) snapshot (git commit of s-expr files), (2) run the stage validator (ERC / netlist-vs-intent / parity / DRC), (3) render the affected region to PNG and look, (4) if failing, apply targeted fixes from the violation JSON (each item carries uuid + pos), (5) re-validate; never proceed to routing with open ERC errors or to fab with DRC errors of must-fix types.
- TOOL[project] project.create(name, dir, target_kicad='auto|7|8|9|10|11', template?) -> {paths, format_versions, git_head} : creates .kicad_pro/.kicad_sch/.kicad_pcb/.kicad_dru + sym/fp-lib-tables, git init; backend FILE
- TOOL[project] kicad.env() -> {versions:[{ver, cli, python, share, lib_env_vars}], ipc:{reachable, open_docs}, freerouting:{jar, java_ok}, capabilities:{erc,drc,render,stats,jobset,ipc_sch,api_server}} : capability probe the agent reads first; backend CLI+IPC
- TOOL[project] project.snapshot(project, message) -> commit_sha / project.restore(project, sha) -> {restored_files} / project.diff(project, from, to) -> {sch:{added,removed,moved}, pcb:{...}} : undo/versioning over git of s-expr files (use `kicad-cli pcb|sch diff` on 11); backend git+FILE
- TOOL[project] project.status(project) -> {files, dirty, last_erc, last_drc, unrouted, stage} : cheap idempotent state summary for re-planning
- TOOL[requirements] design.intent_set(project, intent{blocks[{name, function, parts?}], interfaces[{type, connector, signals}], power_rails[{name, V, I}], constraints{size_mm, layers, fab='jlcpcb'}, nets[{name, pins[(ref|block, pin)]}]}) -> {validation_errors} : wrapper-owned intent model; all later verification diffs against it
- TOOL[requirements] design.intent_render(project) -> png : block diagram (graphviz) for human sign-off
- TOOL[parts] lib.search_symbols(query, {lib?, min_pins?, max_pins?, keywords?}, limit) -> [{lib_id, description, keywords, fp_filters, datasheet, units, pin_count}] : SQLite index of all .kicad_sym under KICAD{N}_SYMBOL_DIR + project libs; backend FILE
- TOOL[parts] lib.symbol_info(lib_id, unit?) -> {pins[{number, name, type, at_symbol_space, orientation, length}], body_bbox, default_footprint, fp_filters, datasheet, svg_png} : ground truth for pin geometry (Y-up symbol space) and for the agent to read pin functions; backend FILE + `kicad-cli sym export svg`
- TOOL[parts] lib.search_footprints(query, {pads?, smd|tht?, pitch?}) -> [{fpid, description, pads, bbox_mm, courtyard_mm, models}] : index of .pretty dirs; geometry via SWIG FootprintLoad; backend FILE+SWIG
- TOOL[parts] lib.footprint_info(fpid) -> {pads[{number, shape, at, size, layers, drill}], courtyard, bbox, model, svg_png} : for placement math and datasheet cross-check; backend SWIG + `fp export svg`
- TOOL[parts] parts.lcsc_search(query, {basic_only, package?, min_stock?, mfr?}) -> [{lcsc, mpn, mfr, package, description, price_1, stock, basic|extended, datasheet_url}] : jlcparts/CDFER DB cache (daily) + EasyEDA API fallback
- TOOL[parts] parts.lcsc_import(project, lcsc_id) -> {lib_id, fpid, model, datasheet_url, price, stock} : easyeda2kicad --full, then sym/fp upgrade, register in project sym-lib-table/fp-lib-table, add LCSC field; backend easyeda2kicad+CLI
- TOOL[parts] parts.datasheet(lcsc|mpn|url, pages?|query?) -> {text_pages | pinout_table | matched_sections} : PDF fetch + PyMuPDF text extraction so the agent can read pinouts/reference circuits
- TOOL[schematic] sch.add_symbol(project, sheet, lib_id, ref?, value, footprint?, at, rotation=0, mirror=none, unit=1, fields{LCSC,MPN,...}, dnp?) -> {uuid, ref, pins[{number, name, at_sheet_space}]} : embeds lib_symbols, writes instances block; returns ABSOLUTE pin endpoints (Y-down, rotation/mirror applied) so the agent never computes geometry; backend FILE
- TOOL[schematic] sch.connect(project, sheet, net, endpoints[(ref,pin)|xy|label], style='label'|'wire'|'power') -> {items_created, junctions_added} : preferred netlist-first connection; labels placed exactly on pin endpoints, wires routed Manhattan with junctions; backend FILE
- TOOL[schematic] sch.add_power(project, sheet, symbol='power:+3V3'|'power:GND'|'power:PWR_FLAG', at, attach_to=(ref,pin)?) / sch.add_no_connect(project, sheet, ref, pin) / sch.add_text(project, sheet, text, at) : power symbols, NC markers, notes; backend FILE
- TOOL[schematic] sch.add_sheet(project, parent_sheet, name, file, at, size, pins[{name, dir}]) -> {sheet_uuid, path} + sch.add_hier_label(...) : hierarchy so each functional block is one sheet the agent can work on in isolation; backend FILE
- TOOL[schematic] sch.annotate(project, scheme='sheet'|'global', start_per_sheet=100) -> {mapping} : assigns refs, writes both property and instances; backend FILE
- TOOL[schematic] sch.assign_footprints(project, map{ref: fpid} | rules[{value_regex|lib_id, fpid}]) -> {assigned, unresolved[{ref, fp_filters}]} : idempotent; backend FILE
- TOOL[schematic] sch.get(project, sheet?, {refs?, bbox?, include=['symbols','labels','wires','nets']}) -> compact JSON : chunked read for large designs (by sheet, by bbox window, or by ref list) so the agent never loads a whole 3 MB file
- TOOL[schematic] sch.render(project, sheet|'all', region_mm?, max_px=1600, overlay=['erc_markers','refs']) -> [png_path] : `sch export svg --pages` -> PyMuPDF raster (+ crop); markers drawn from corrected ERC positions; backend CLI
- TOOL[schematic] sch.erc(project, severity='all') -> {ok, counts_by_type, violations[{type, severity, sheet_path, description, items[{description, pos_mm(x100 fixed), uuid}]}], ignored_checks} : kicad-cli 8+ (7: unsupported -> error explaining); backend CLI
- TOOL[schematic] sch.netlist(project) -> {components[{ref, value, footprint, fields, sheet_path, uuid, dnp}], nets[{name, class, nodes[{ref, pin, pin_name, pin_type}]}]} : `sch export netlist --format kicadxml` parsed; backend CLI
- TOOL[schematic] sch.verify_intent(project) -> {missing_connections, extra_connections, wrong_pin, unassigned_footprints, single_pin_nets} : diff of design.intent nets vs actual netlist — the check that caught the kicad-sch-api Y-sign bug; MUST pass before pcb.sync
- TOOL[board] pcb.setup(project, {outline: rect{w,h}|polygon|dxf, layers: 2|4|6, thickness, stackup?, rules{clearance, track_width, via_dia, via_drill, hole_clearance, edge_clearance}, netclasses[{name, nets|regex, width, clearance, via}], dru_text?}) -> {applied} : writes .kicad_pcb/.kicad_pro/.kicad_dru; presets for JLCPCB 2/4-layer; backend FILE(+SWIG)
- TOOL[board] pcb.sync_from_schematic(project, {remove_orphans=true, keep_positions=true}) -> {added, updated, removed, nets_changed, parity_issues} : netlist->SWIG (7–10) or IPC ImportNetlist (11); always followed by `drc --schematic-parity`; backend SWIG|IPC + CLI
- TOOL[board] pcb.get(project, {refs?, bbox?, include=['footprints','pads','nets','tracks_summary','zones','outline','unconnected']}) -> compact JSON : footprints with pos/rot/layer/bbox/courtyard and pad->net map; unconnected count from connectivity; backend SWIG (or IPC when a GUI session is bound)
- TOOL[placement] pcb.place(project, placements[{ref, x, y, rot, layer='F'|'B'}], mode='absolute'|'relative_to'(ref)) -> {new_state, overlaps[], off_board[]} : moves footprints, immediately checks courtyard overlap + outline containment; backend SWIG|IPC
- TOOL[placement] pcb.place_suggest(project, {groups[{anchor_ref, members[]}], edge_refs[], keepouts[]}) -> proposed placements : heuristic (connectivity + groups + connector edges) the agent reviews visually; optional
- TOOL[placement] pcb.render(project, view='top2d'|'bottom2d'|'3d_top'|'3d_iso', layers?, region_mm?, max_px=1600, overlay=['ratsnest','refs','drc_markers']) -> png_path : `pcb export svg` -> raster (2D) or `pcb render` (3D, 9/10); ratsnest lines from unconnected pairs; backend CLI
- TOOL[routing] pcb.autoroute(project, {engine='freerouting', passes=20, threads=1, layers?, via_costs?, timeout_s, async=false}) -> {job_id?, routed, unrouted, violations, seconds, tracks_added, vias_added} : DSN export (SWIG) -> Freerouting jar with managed JRE 25 -> SES import -> zone refill; backend SWIG+java(+IPC run_action 'pcbnew.InteractiveRouter.Autoroute' variant when GUI bound)
- TOOL[routing] pcb.route(project, ops[{net, layer, width?, points[[x,y],...]} | {via, at, net, dia?, drill?} | {unroute, net|uuid}]) -> {added_uuids, drc_delta} : manual/touch-up routing with immediate DRC of touched nets; backend SWIG|IPC
- TOOL[routing] pcb.zones(project, zones[{net, layer, polygon|'board', clearance?, thermal?, priority?}], fill=true) -> {zone_uuids, filled_area_mm2} : GND pours + fill (ZONE_FILLER or `drc --refill-zones --save-board`); backend SWIG|CLI|IPC
- TOOL[drc] pcb.drc(project, {schematic_parity=true, refill_zones=true, severity='all', all_track_errors=false}) -> {ok, must_fix_count, counts_by_type, violations[{type, severity, description, items[{description, pos_mm, uuid}], classification:'must_fix'|'review'|'cosmetic'}], unconnected_items[], schematic_parity[]} : kicad-cli 8+; backend CLI
- TOOL[drc] pcb.fix(project, actions=['unroute_violations','nudge_overlaps','cleanup_tracks','refill']) -> {before, after} : deterministic fixers (rip-up violating nets then re-autoroute, courtyard nudge, cleanupTracksAndVias equivalent); optional
- TOOL[fab] fab.export(project, targets=['gerbers','drill','pos','bom','step','ipc2581','odb','pdf','render','stats'], profile='jlcpcb'|'generic'|jobset_file, out_dir) -> {files[{path, kind, sha256}], warnings} : kicad-cli exports or `jobset run`; JLCPCB profile sets protel extensions, separate PTH/NPTH, pos CSV columns, BOM with LCSC field; backend CLI
- TOOL[fab] fab.bom(project, fields, group_by, exclude_dnp=true) -> csv_path + rows_json (with LCSC price/stock join) / fab.stats(project) -> numeric JSON (units parsed) : backend CLI
- TOOL[review] review.report(project) -> markdown+images : ERC/DRC summaries, board stats, renders (top/bottom/3D), BOM cost estimate, unmatched parts, checklist; publishable as artifact; backend composite
- SKILL 'kicad-geometry': schematic space is Y-down, symbol space is Y-up (sheet = sym_at + R(rot)*M(mirror)*(px, -py)); pin `at` is the connection endpoint; 1.27 mm grid (labels/pins must sit on it or ERC endpoint_off_grid); pcb IU 1e6/mm, sch IU 1e4/mm; footprint rotation CCW, bottom-side footprints mirrored; pos files negate Y
- SKILL 'netlist-first schematic': define intent nets first; connect with local/global labels on pin endpoints instead of long wires; one hierarchical sheet per block (MCU, USB, charger, power, headers) with hierarchical labels/sheet pins; power symbols + PWR_FLAG on externally driven rails; NC markers on unused pins; annotate per sheet (100s, 200s)
- SKILL 'erc-triage': power_pin_not_driven -> add PWR_FLAG or power-output pin; label_dangling/isolated_pin_label -> label not on a pin endpoint (recompute from lib.symbol_info); pin_not_connected -> add NC or connect; wire_dangling/unconnected_wire_endpoint -> remove or junction; endpoint_off_grid -> snap to 1.27; always confirm with sch.verify_intent because ERC cannot see a label on the wrong pin
- SKILL 'reference-circuits' (fixed templates the agent instantiates, then verifies against datasheets): ESP32-S3-WROOM-1 minimal (3V3 10uF+100nF, EN 10k+1uF RC, IO0 boot button, IO45/IO46 strap awareness, USB D+/D- on IO19/IO20, antenna keepout at board edge); USB-C 2.0 receptacle (CC1/CC2 5.1k to GND, ESD array on D+/D-/VBUS, VBUS to charger, shield to GND); MCP73831 LiPo charger (PROG resistor 2k=500 mA, 4.7uF in/out, STAT LED, VBUS diode-OR or ideal-diode to 3V3 LDO); AMS1117/ME6211 3.3 V LDO with input/output caps; I2C headers with 4.7k pull-ups on one node only; test points; mounting holes
- SKILL 'part-selection': prefer JLCPCB basic/preferred parts (no extended fee), verify stock and package; match symbol pin count/names to datasheet; pick footprints from KiCad libs by exact package (e.g. Package_TO_SOT_SMD:SOT-23-5, Connector_USB:USB_C_Receptacle_HRO_TYPE-C-31-M-12), import LCSC symbol/footprint only when no standard one exists, and always attach the LCSC field for the BOM
- SKILL 'board-setup': JLCPCB 2-layer defaults (min track/space 0.127 mm, via 0.3/0.6 mm, edge clearance 0.3 mm, 1.6 mm FR4); netclasses: Default 0.25/0.2, POWER 0.5 mm, USB diff pair 90 ohm; outline from size + 3.2 mm mounting holes in corners; set origin/drill origin
- SKILL 'placement': connectors on edges facing out, antenna overhang/keepout, decoupling caps within 2 mm of the pin they serve on the same side, charger away from antenna, group by block, 2–3 mm channels between groups, keep polarised parts oriented consistently, silkscreen refs readable; verify with pcb.render(top2d) after every batch and pcb.drc(courtyards)
- SKILL 'routing': never hand-route full nets (LLMs cannot guarantee clearance) — autoroute, then refill zones, then DRC; hand-fix only short touch-ups; USB D+/D- as a diff pair, short and away from switching nodes; GND pour on B.Cu (and F.Cu) with stitching vias; power traces wider (netclass); rerun autoroute with different passes if unrouted > 0; treat shorting_items/clearance/tracks_crossing/unconnected_items as blocking, silk_* and courtyards_overlap as reviewable
- SKILL 'visual-verification': after each mutation request a render (region crop for detail, full board for context), compare against the intent (connector positions, part orientation, ratsnest sanity), and state explicitly what was checked; never claim success without the validator JSON and an image
- SKILL 'idempotency-and-safety': every tool call names the project and (optionally) the expected content hash/last snapshot; mutating calls are serialized per project; tools are re-runnable (add_symbol with the same ref updates instead of duplicating; sync/assign/annotate are convergent); snapshot before each stage; restore on failure
- SKILL 'context-budget': use sch.get/pcb.get filtered by sheet/bbox/refs; delegate library search, datasheet reading, ERC/DRC parsing to sub-agents and keep only conclusions; images capped at 1600 px
- SKILL 'version-awareness': call kicad.env first; on 7 expect no ERC/DRC/BOM; on 8 no render; on 11 expect SWIG gone and IPC headless available; never guess library names (search), never edit files a KiCad GUI has open without the IPC session (or ask the user to close/reload)

## Experiments run

- **kicad-cli 10.0.0 help capture for pcb render / pcb drc / sch erc / pcb export svg / sch export svg / pcb export stats / sch export netlist / sch export bom / gerbers / drill / pos / step / jobset**
  - Result: All options recorded (render: --side/--rotate/--perspective/--zoom/--pan/--pivot/--quality/--background/--floor/--preset/lights; drc: --format json|report, --schematic-parity, --refill-zones, --save-board, --exit-code-violations, --severity-*; erc: --format json|report, --exit-code-violations; svg: --mode-single, --page-size-mode 0|1|2, --layers, --variant; sch svg: --pages, --exclude-drawing-sheet; stats --format json).
  - Command/code:

```
K="C:/Program Files/KiCad/10.0/bin/kicad-cli.exe"; "$K" pcb render --help; "$K" pcb drc --help; "$K" sch erc --help; "$K" pcb export svg --help; "$K" sch export svg --help; "$K" pcb export stats --help; "$K" sch export netlist --help; "$K" sch export bom --help; "$K" pcb export gerbers --help; "$K" pcb export drill --help; "$K" pcb export pos --help; "$K" pcb export step --help; "$K" jobset run --help
```

- **Capability matrix of kicad-cli across 7.0.6 / 8.0.1 / 9.0.5 / 10.0.0**
  - Result: 7: export only. 8: +drc, +erc, +bom, glb/ipc2581/vrml. 9: +render, +jobset, odb/ipcd356/gencad/brep/stl/ply/xao. 10: +stats, +upgrade, +import, 3dpdf/stpz/u3d, hpgl removed.
  - Command/code:

```
for V in 7.0 8.0 9.0 10.0; do K="C:/Program Files/KiCad/$V/bin/kicad-cli.exe"; "$K" version; "$K" pcb --help; "$K" pcb export --help; "$K" sch --help; "$K" sch export --help; "$K" --help; done
```

- **ERC JSON on clean hierarchical demo and on a deliberately broken schematic (first 6 wire blocks removed) for 10.0, 9.0, 8.0 (7.0 has no erc)**
  - Result: 10.0: 12 violations (pin_not_connected x8, unconnected_wire_endpoint x2, label_dangling, pin_not_driven); 9.0: 13; 8.0: 10. JSON shapes recorded; complex_hierarchy gave 3 sheets with paths '/', '/ampli_ht_vertical/', '/ampli_ht_horizontal/'.
  - Command/code:

```
cp -r "C:/Program Files/KiCad/10.0/share/kicad/demos/pic_programmer" .; python break_sch.py pic_programmer/pic_programmer.kicad_sch pic_programmer/pic_programmer.kicad_sch 6; "$K" sch erc --format json --severity-all -o out/erc_pic.json pic_programmer/pic_programmer.kicad_sch; (same with 9.0/8.0 demos into v9.0/, v8.0/)  # break_sch.py: paren-balanced removal of N '\n\t(wire' blocks
```

  - Artifact: `C:/Users/usama/AppData/Local/Temp/claude/c--Upwork-KiCad/48a8df66-7213-479f-ab2e-a63b89135016/scratchpad/e2e-agent-workflow/out/erc_pic.json`
- **ERC JSON coordinate scale check vs text report**
  - Result: Report: @(87.63 mm, 46.99 mm); JSON mm: {0.8763, 0.4699}; JSON in: {0.0345, 0.0185}; symbol D3 at (87.63 50.8) -> JSON is exactly 100x too small in 8.0.1, 9.0.5, 10.0.0.
  - Command/code:

```
"$K" sch erc --format report --severity-all -o out/erc_pic.rpt pic_programmer/pic_programmer.kicad_sch; grep -A2 'D3 Pin 1' out/erc_pic.rpt; "$K" sch erc --format json --units in -o out/erc_pic_in.json ...; grep -n -A2 '(property "Reference" "D3"' pic_programmer/pic_programmer.kicad_sch
```

- **DRC JSON with schematic parity on demo board, then on a board broken with pcbnew python (D3 moved onto D9, stray 20 mm F.Cu track on net 1)**
  - Result: Demo: 0 violations, 0 unconnected, 8 schematic_parity net_conflict (because the schematic had been broken). Broken board: 37 violations (silk_over_copper 7, solder_mask_bridge 6, pth_inside_courtyard 5, lib_footprint_issues 5, silk_overlap 4, shorting_items 3, courtyards_overlap 2, hole_clearance, clearance, hole_to_hole, track_dangling, holes_co_located) + 2 unconnected_items; DRC positions correct in mm.
  - Command/code:

```
"$K" pcb drc --format json --severity-all --schematic-parity -o out/drc_pic.json pic_programmer/pic_programmer.kicad_pcb; "C:/Program Files/KiCad/10.0/bin/python.exe" - <<EOF
import pcbnew
b=pcbnew.LoadBoard('pic_programmer/pic_programmer.kicad_pcb'); fp=b.FindFootprintByReference('D3'); fp.SetPosition(b.FindFootprintByReference('D9').GetPosition())
t=pcbnew.PCB_TRACK(b); t.SetStart(pcbnew.VECTOR2I_MM(100,100)); t.SetEnd(pcbnew.VECTOR2I_MM(120,100)); t.SetWidth(pcbnew.FromMM(0.25)); t.SetLayer(pcbnew.F_Cu); t.SetNetCode(1); b.Add(t)
pcbnew.SaveBoard('out/pic_broken.kicad_pcb', b)
EOF
"$K" pcb drc --format json --severity-all --all-track-errors -o out/drc_broken.json out/pic_broken.kicad_pcb
```

  - Artifact: `C:/Users/usama/AppData/Local/Temp/claude/c--Upwork-KiCad/48a8df66-7213-479f-ab2e-a63b89135016/scratchpad/e2e-agent-workflow/out/drc_broken.json`
- **pcbnew.WriteDRCReport from standalone bundled python (10.0)**
  - Result: Assertion 'C:\jenkins\...\common\pgm_base.cpp(1043): assert "process" failed in Pgm().' — no report written; DRC needs the app context (GUI) or kicad-cli.
  - Command/code:

```
"C:/Program Files/KiCad/10.0/bin/python.exe" -c "import pcbnew; b=pcbnew.LoadBoard('out/pic_broken.kicad_pcb'); print(pcbnew.WriteDRCReport(b,'out/py_drc.txt',pcbnew.EDA_UNITS_MM,True))"
```

- **Exit codes with --exit-code-violations**
  - Result: 5 with violations (ERC and DRC), 0 when clean.
  - Command/code:

```
"$K" sch erc --format json --severity-error --exit-code-violations -o gen/erc_unann.json gen/unannotated.kicad_sch; echo $?; (clean complex_hierarchy) ; "$K" pcb drc --format json --exit-code-violations -o gen/drc_x.json out/pic_broken.kicad_pcb; echo $?
```

- **Visual feedback: 3D render top + isometric, 2D layer SVG, schematic SVG; rasterize SVGs with PyMuPDF**
  - Result: render top 2.4 s (render time 0.063 s), iso high quality 2.6 s; board SVG 0.43 s (500 KB); schematic SVGs 0.48 s (3.0 MB + 1.0 MB, one per sheet); PyMuPDF raster 0.08 s (1457x996) and 0.21 s (1684x1191); images inspected and legible.
  - Command/code:

```
"$K" pcb render -w 1200 -h 800 --side top --quality basic --background opaque -o out/render_top.png pic_programmer/pic_programmer.kicad_pcb; "$K" pcb render -w 1200 -h 800 --rotate "-45,0,45" --perspective --quality high --floor -o out/render_iso.png ...; "$K" pcb export svg --mode-single --layers "F.Cu,B.Cu,F.SilkS,Edge.Cuts,F.Mask" --page-size-mode 2 --exclude-drawing-sheet -o out/board_top.svg ...; "$K" sch export svg --exclude-drawing-sheet -o out/sch_svg pic_programmer/pic_programmer.kicad_sch; .venv/Scripts/python.exe -c "import fitz; [fitz.open(s)[0].get_pixmap(matrix=fitz.Matrix(z,z)).save(d) for s,d,z in [('out/board_top.svg','out/board_top.png',3),('out/sch_svg/pic_programmer.svg','out/sch_top.png',2)]]"
```

  - Artifact: `C:/Users/usama/AppData/Local/Temp/claude/c--Upwork-KiCad/48a8df66-7213-479f-ab2e-a63b89135016/scratchpad/e2e-agent-workflow/out/board_top.png`
- **Install and inspect kicad-python (kipy) 0.8.0, kicad-skip, kicad-sch-api, easyeda2kicad, pymupdf in a scratch venv**
  - Result: kicad-python 0.8.0 (KICAD_API_VERSION 10.0.6-0-gcaf7377e9c), kicad-skip 0.2.5, kicad-sch-api 0.5.6, easyeda2kicad 1.0.1, pymupdf 1.28.2, pynng 0.9.0, protobuf 5.29.6. `import kipy.schematic` -> ImportError: cannot import name 'PageSettings' from 'kipy.common_types' (proto/schematic .pyi only has Line/Text/labels; wrapper references Junction/SheetSymbol/SchematicSymbolInstance...).
  - Command/code:

```
"C:/Python312/Scripts/uv" venv .venv --python 3.11; "C:/Python312/Scripts/uv" pip install --python .venv/Scripts/python.exe kicad-python kicad-skip sexpdata pymupdf kicad-sch-api easyeda2kicad; .venv/Scripts/python.exe -c "import kipy.schematic"
```

- **Live read-only probe of the IPC API on the already-running pcbnew 10.0.0 (not started by me; nothing modified)**
  - Result: Connected via ipc://%TEMP%\kicad\api.sock: version 10.0.0, api 10.0.6; open PCB pic_programmer.kicad_pcb; DOCTYPE_SCHEMATIC -> ApiError 'no handler available for request of type kiapi.common.commands.GetOpenDocuments'; board: 63 footprints, 112 nets, 371 tracks, 7 vias, 1 zone; Board methods include begin_commit/push_commit, create_items/update_items/remove_items, refill_zones, interactive_move, hit_test, get_connected_items, get_items_by_net, save/save_as/revert, get_as_string, run_action (on KiCad).
  - Command/code:

```
.venv/Scripts/python.exe -c "from kipy import KiCad; from kipy.proto.common.types import base_types_pb2 as t; k=KiCad(timeout_ms=3000); print(k.get_version(), k.get_api_version()); print([(d.board_filename,d.project.name) for d in k.get_open_documents(t.DocumentType.DOCTYPE_PCB)]); k.get_open_documents(t.DocumentType.DOCTYPE_SCHEMATIC)"; then b=k.get_board(); b.get_footprints()/get_nets()/get_tracks()/get_vias()/get_zones(); dir(b)
```

- **KiCad source inspection (GitLab raw/API): registered IPC handlers 10.0 vs master, RunAction implementation, action names, job types, kicad-cli commands**
  - Result: Lists captured (see findings). handleRunAction = frame()->GetToolManager()->RunAction(name, true). 10.0 has pcbnew.InteractiveRouter.Autoroute ('Attempt Finish Selected (Autoroute)', Shift+F), Autoplacer.autoplaceSelected/Offboard, ZoneFiller.zoneFillAll, DRCTool.runDRC, EditorControl.importNetlist/exportSpecctraDSN/importSpecctraSession/generateGerbers. master adds command_api_server, *_diff, git_mergedriver, mergetool, pcb_export_png, export_stackup and eeschema headless_sch_context.
  - Command/code:

```
curl -sL https://gitlab.com/kicad/code/kicad/-/raw/10.0/pcbnew/api/api_handler_pcb.cpp | grep -o 'registerHandler<[A-Za-z:]*' ; same for master pcbnew/api/api_handler_pcb.cpp, master eeschema/api/api_handler_sch.cpp, 10.0 and master common/api/api_handler_editor.cpp; grep -o '"pcbnew\.[A-Za-z]*\.[A-Za-z]*"' pcb_actions.cpp; GitLab API tree of common/jobs and kicad/cli on 10.0 and master
```

  - Artifact: `C:/Users/usama/AppData/Local/Temp/claude/c--Upwork-KiCad/48a8df66-7213-479f-ab2e-a63b89135016/scratchpad/e2e-agent-workflow/web/`
- **SWIG pcbnew feature presence across 7/8/9/10 bundled pythons and standalone zone fill / connectivity in 10**
  - Result: All True on 7.0.6/8.0.1/9.0.5/10.0.0. ZONE_FILLER.Fill -> True in 0.44 s (GND zone 11691 mm2 filled); unconnected count 2.
  - Command/code:

```
for V in 7.0 8.0 9.0 10.0; do "C:/Program Files/KiCad/$V/bin/python.exe" -c "import pcbnew; n=dir(pcbnew); print(pcbnew.GetBuildVersion(), 'ExportSpecctraDSN' in n, 'ImportSpecctraSES' in n, 'WriteDRCReport' in n, 'ZONE_FILLER' in n, 'FootprintLoad' in n, 'PLOT_CONTROLLER' in n, 'VECTOR2I_MM' in n)"; done; "C:/Program Files/KiCad/10.0/bin/python.exe" -c "import pcbnew; b=pcbnew.LoadBoard('out/pic_routed.kicad_pcb'); f=pcbnew.ZONE_FILLER(b); print(f.Fill(b.Zones())); c=b.GetConnectivity(); c.RecalculateRatsnest(); print(c.GetUnconnectedCount(True))"
```

- **Load/save round-trip determinism (git friendliness)**
  - Result: 0 differing lines original vs roundtrip1 and roundtrip1 vs roundtrip2 (byte-identical).
  - Command/code:

```
"C:/Program Files/KiCad/10.0/bin/python.exe" -c "import pcbnew; b=pcbnew.LoadBoard('pic_programmer/pic_programmer.kicad_pcb'); pcbnew.SaveBoard('out/roundtrip1.kicad_pcb',b); b=pcbnew.LoadBoard('out/roundtrip1.kicad_pcb'); pcbnew.SaveBoard('out/roundtrip2.kicad_pcb',b)"; diff pic_programmer/pic_programmer.kicad_pcb out/roundtrip1.kicad_pcb | grep -c '^[<>]'
```

- **Headless autoroute: strip tracks, export DSN (SWIG), Freerouting 2.4.1 with portable JRE 25, import SES, DRC before/after zone refill, render**
  - Result: Java 8/11/17 on this machine fail (class file version 69 => Java 25; 2.1.0 => 65/Java 21). With JRE 25: fanout 2/2 pins, 86 nets/84 items routed in 2.65 s, score 998.41 (0 unrouted, 1 violation), total 12 s; SES 26 KB; ImportSpecctraSES -> 265 tracks, 0 vias. DRC before refill: 121 violations (115 clearance vs stale GND zone fill); after --refill-zones: 2 violations (1 clearance, 1 shorting_items with copper text '1=>>') + 2 unconnected. Freerouting warned multi-threaded optimizer generates clearance violations (use -mt 1) and wrote ./en/freerouting.log + %LOCALAPPDATA%\freerouting\freerouting.json.
  - Command/code:

```
python: b=LoadBoard(...); [b.Remove(t) for t in list(b.GetTracks())]; SaveBoard('out/pic_unrouted.kicad_pcb'); pcbnew.ExportSpecctraDSN(b,'out/pic_unrouted.dsn')  # DSN 106 KB
curl -sL -o tools/jre25.zip "https://api.adoptium.net/v3/binary/latest/25/ga/windows/x64/jre/hotspot/normal/eclipse?project=jdk"; unzip; curl -sL -o tools/freerouting-2.4.1.jar https://github.com/freerouting/freerouting/releases/download/v2.4.1/freerouting-2.4.1.jar
"tools/jdk-25.0.4.1+1-jre/bin/java.exe" -jar tools/freerouting-2.4.1.jar -de out/pic_unrouted.dsn -do out/pic_routed.ses -mp 20
python: b=LoadBoard('out/pic_unrouted.kicad_pcb'); pcbnew.ImportSpecctraSES(b,'out/pic_routed.ses'); SaveBoard('out/pic_routed.kicad_pcb')
"$K" pcb drc --format json --severity-all -o out/drc_routed.json out/pic_routed.kicad_pcb; "$K" pcb drc --format json --severity-all --refill-zones --save-board -o out/drc_routed2.json out/pic_routed.kicad_pcb; "$K" pcb render ... -o out/render_routed.png
```

  - Artifact: `C:/Users/usama/AppData/Local/Temp/claude/c--Upwork-KiCad/48a8df66-7213-479f-ab2e-a63b89135016/scratchpad/e2e-agent-workflow/out/pic_routed.kicad_pcb`
- **Programmatic schematic creation with kicad-sch-api from KiCad 10 standard symbols, then kicad-cli ERC + netlist verification**
  - Result: File written as version 20250114 / generator_version 9.0; KiCad 10 accepted it. ERC: 10 violations (label_dangling GND, pin_not_connected U1.1, power_pin_not_driven x2, wire_dangling, isolated_pin_label x2, endpoint_off_grid, unconnected_wire_endpoint x2). Netlist: /GND [C1.1], /SIG [R1.1], /VCC_3V3 [C1.2, R1.2, U1.2], /VIN [U1.3], unconnected-(U1-GND-Pad1) [U1.1] => labels landed on the opposite pins (Y-sign bug in kicad-sch-api get_pin_position; Device:R pin 1 is at (0 3.81 270) in symbol space). get_symbol_info('RF_Module:ESP32-S3-WROOM-1') returned 41 pins; search_symbols('ESP32-S3-WROOM') returned [] (index not built).
  - Command/code:

```
.venv/Scripts/python.exe gen_sch.py   # KICAD_SYMBOL_DIR=C:\Program Files\KiCad\10.0\share\kicad\symbols; creates Device:R, Device:C, Regulator_Linear:AMS1117-3.3 with footprints, labels VCC_3V3/GND/SIG/VIN placed at get_pin_position(), one wire, save gen/agent_test.kicad_sch
"$K" sch erc --format json --severity-all -o gen/erc.json gen/agent_test.kicad_sch; "$K" sch export netlist --format kicadsexpr -o gen/agent_test.net gen/agent_test.kicad_sch; python regex over (nets ...) to print net -> [(ref,pin)]
```

  - Artifact: `C:/Users/usama/AppData/Local/Temp/claude/c--Upwork-KiCad/48a8df66-7213-479f-ab2e-a63b89135016/scratchpad/e2e-agent-workflow/gen/agent_test.kicad_sch`
- **Annotation storage check: reference property vs instances block**
  - Result: ERC reported no annotation problem; netlist still exported ref="R1" (instances block wins). No kicad-cli annotate exists.
  - Command/code:

```
sed 's/(property "Reference" "R1"/(property "Reference" "R?"/' gen/agent_test.kicad_sch > gen/unannotated.kicad_sch; "$K" sch erc ... gen/unannotated.kicad_sch; "$K" sch export netlist --format kicadxml -o gen/unann.xml gen/unannotated.kicad_sch; grep -o 'ref="[^"]*"' gen/unann.xml | sort | uniq -c
```

- **Headless netlist -> PCB (wrapper-implemented 'Update PCB from Schematic') + parity DRC**
  - Result: Board with 3 footprints and 6 nets saved; DRC: 0 violations, 3 unconnected_items (unrouted), 3 schematic_parity footprint_symbol_mismatch ('C_0603_1608Metric doesn't match footprint given by symbol (Capacitor_SMD:C_0603_1608Metric)') -> need fp.SetFPID(LIB_ID) after FootprintLoad.
  - Command/code:

```
"C:/Program Files/KiCad/10.0/bin/python.exe" netlist_to_pcb.py gen/agent_test.net gen/agent_test.kicad_pcb   # parses (comp ...) and (net ...) from kicad-cli netlist; pcbnew.BOARD(); NETINFO_ITEM per net; FootprintLoad(<share>/footprints/<lib>.pretty, name); SetReference/SetValue/SetPath; pad.SetNet; Edge.Cuts rectangle; m_MinClearance; SaveBoard
"$K" pcb drc --format json --severity-all --schematic-parity -o gen/drc.json gen/agent_test.kicad_pcb
```

  - Artifact: `C:/Users/usama/AppData/Local/Temp/claude/c--Upwork-KiCad/48a8df66-7213-479f-ab2e-a63b89135016/scratchpad/e2e-agent-workflow/netlist_to_pcb.py`
- **LCSC part import with easyeda2kicad and validation in KiCad 10**
  - Result: 3.9 s: MCP73831T-2ACI/OT symbol (kicad_symbol_lib v20211014), footprint SOT-23-5 (legacy 'module' format), STEP 2.9 MB + WRL. sym export svg OK; sym upgrade -> 20251024; fp upgrade -> 20260206; FootprintLoad -> 5 pads, WRL model path. API: lcsc {number C424093, price 0.483334, stock 2935, url}, szlcsc, packageDetail; datasheet None.
  - Command/code:

```
.venv/Scripts/python.exe -m easyeda2kicad --full --lcsc_id=C424093 --output "$(pwd)/lcsc/lcsc" --overwrite; "$K" sym export svg -o lcsc/svg lcsc/lcsc.kicad_sym; "$K" sym upgrade -o lcsc/lcsc_up.kicad_sym lcsc/lcsc.kicad_sym; "$K" fp upgrade -o lcsc/lcsc_up.pretty lcsc/lcsc.pretty; "C:/Program Files/KiCad/10.0/bin/python.exe" -c "import pcbnew; fp=pcbnew.FootprintLoad(r'lcsc/lcsc.pretty','SOT-23-5_L3.0-W1.7-P0.95-LS2.8-BL'); print(fp.Pads().size(), [m.m_Filename for m in fp.Models()])"; python: EasyedaApi().get_info_from_easyeda_api(lcsc_id='C424093')
```

- **Fabrication bundle with kicad-cli 10 and stats/BOM/pos inspection**
  - Result: 25 gerber/drill files + .gbrjob, pos.csv, stats.json, bom.csv in 1.76 s; STEP in 1.39 s. stats.json: board width/height/area/copper areas/min clearance/min track/min drill as strings with units ('mmÂ²' mojibake), pads/vias/components counts, drill_holes table. pos.csv Y values negative. BOM header Reference,Value,Footprint,QUANTITY,LCSC.
  - Command/code:

```
"$K" pcb export gerbers -o fab/gerbers/ --subtract-soldermask --use-drill-file-origin out/pic_routed.kicad_pcb; "$K" pcb export drill -o fab/gerbers/ --excellon-separate-th --generate-map --map-format gerberx2 ...; "$K" pcb export pos --format csv --units mm --side both -o fab/pos.csv ...; "$K" pcb export stats --format json -o fab/stats.json ...; "$K" sch export bom --fields "Reference,Value,Footprint,\${QUANTITY},LCSC" --group-by "Value,Footprint" -o fab/bom.csv pic_programmer/pic_programmer.kicad_sch; "$K" pcb export step --subst-models -f -o fab/board.step ...
```

  - Artifact: `C:/Users/usama/AppData/Local/Temp/claude/c--Upwork-KiCad/48a8df66-7213-479f-ab2e-a63b89135016/scratchpad/e2e-agent-workflow/fab/`
- **Read-only inspection of KiCad 10 user config and library tables (no modification)**
  - Result: api.enable_server false (user config untouched), interpreter_path pythonw.exe; user fp-lib-table = one (type "Table") entry to template fp-lib-table; template uses ${KICAD10_FOOTPRINT_DIR}; 223 symbol libs, 155 footprint libs; schemas/api.v1.schema.json = IPC plugin.json schema (runtime.type python|exec, actions).
  - Command/code:

```
grep -A4 '"api"' "$APPDATA/kicad/10.0/kicad_common.json"; cat "$APPDATA/kicad/10.0/fp-lib-table"; grep -m1 '(lib (name' "C:/Program Files/KiCad/10.0/share/kicad/template/fp-lib-table"; ls share/kicad/symbols | wc -l; ls share/kicad/footprints | wc -l
```
