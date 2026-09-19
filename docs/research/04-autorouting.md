# autorouting

## Findings

- **[high, VERIFIED LOCALLY]** Freerouting current release is v2.4.1 (published 2026-09-03), GPL-3.0, and REQUIRES Java 25 (class file 69). Java 17 fails with UnsupportedClassVersionError. A private Temurin JRE 25 unzipped into a folder (no system install) runs it fine.
  - Evidence: GitHub releases API: v2.4.1 assets freerouting-2.4.1.jar (64 MB), windows-x64.msi, linux zip, macos dmg; earlier: v2.3.0 2026-08-07, v2.2.4 2026-05-13, v2.1.0 2025-04-12 (v2.0.0 needed Java 21+). Local: '"C:/Program Files/Eclipse Adoptium/jre-17.0.12.7-hotspot/bin/java.exe" -jar freerouting-2.4.1.jar -help' -> 'class file version 69.0 ... only recognizes up to 61.0'. With jre25/jdk-25.0.4.1+1-jre it prints 'Freerouting v2.4.1 (build-date: 2026-09-03)'. NOTE: the machine's PATH java is 1.8.0_503 (Java 17 exists at C:\Program Files\Eclipse Adoptium\jre-17.0.12.7-hotspot and JAVA_HOME points there, but Java 8 shadows it on PATH).
  - Sources: https://github.com/freerouting/freerouting/releases, https://api.github.com/repos/freerouting/freerouting/releases, https://api.adoptium.net/v3/binary/latest/25/ga/windows/x64/jre/hotspot/normal/eclipse?project=jdk
- **[high, VERIFIED LOCALLY]** Freerouting headless CLI works on Windows: 'java -jar freerouting-2.4.1.jar --gui.enabled=false -de in.dsn -do out.ses -mp N -mt N [-da] [--user_data_path=DIR] [--logging.file.location=DIR] [--router.result_json=PATH]'. Full flag list: -de/-do/-di/-dr(.rules)/-drc(json)/-mp/-mt/-inc(net classes to ignore)/-us/-hr/-is/-l/-host/-da/-dct/-im/-ll/-dl and any settings key as --section.key=value (also env FREEROUTING__SECTION__KEY).
  - Evidence: Ran the 2-footprint tiny board: job COMPLETED in 0.24 s, SES 935 bytes, exit code 0. Ran a 15-footprint/30-net TQFP board: 46/46 connections routed in 3.5 s, 23 vias, exit 0, result manifest written by --router.result_json. '-help' output captured. Settings keys confirmed from jar classes (app/freerouting/settings/RouterSettings.class): automatic_neckdown, neck_width_um, copper_to_edge_clearance_um, strict_drc, ignore_net_classes, job_timeout, result_json, fanout.enabled.
  - Sources: https://raw.githubusercontent.com/freerouting/freerouting/master/docs/command_line_arguments.md, https://raw.githubusercontent.com/freerouting/freerouting/master/docs/settings.md
- **[high, VERIFIED LOCALLY]** GOTCHA: Freerouting 2.4.1 defaults (fanout pre-pass ON + automatic_neckdown ON) produce 0.15 mm neck-down segments that violate KiCad's 0.2 mm min track width -> KiCad DRC reported 26 'track_width' violations on an otherwise fully routed board. Passing --router.automatic_neckdown=false --router.fanout.enabled=false gives 0 violations / 0 unconnected in KiCad DRC.
  - Evidence: kicad-cli pcb drc on med10/medium_fr_routed.kicad_pcb: violations {'track_width': 26}; SES contained 13 'path F.Cu 1500' entries. Re-run with the two flags: SES only has width 2000 paths; kicad-cli drc: 0 violations, 0 unconnected (files med10/medium_fr_nn_routed.kicad_pcb, med10/drc_fr_nn.json).
  - Sources: https://raw.githubusercontent.com/freerouting/freerouting/master/docs/settings.md
- **[high, VERIFIED LOCALLY]** Freerouting embeds a local REST API server (--api_server.enabled=true --api_server.authentication.enabled=false, default http://127.0.0.1:37864, Swagger at /swagger-ui, OpenAPI at /openapi/openapi.json). Workflow verified: POST /v1/sessions/create -> POST /v1/jobs/enqueue -> POST /v1/jobs/{id}/settings -> POST /v1/jobs/{id}/input (base64 DSN) -> PUT /v1/jobs/{id}/start -> GET /v1/jobs/{id} (poll state) -> GET /v1/jobs/{id}/output (base64 SES + statistics) -> GET /v1/jobs/{id}/drc (KiCad drc.v1 schema JSON). Also /v1/jobs/{id}/logs/stream and /output/stream (SSE) for live progress, and /input/json + /output/json for the experimental KiCad JSON path. Even with auth disabled, requests need headers Freerouting-Profile-ID (UUID) and Freerouting-Environment-Host (e.g. 'KiCad/10.0'). The documented composite POST /v1/autoroute and /drc/summary return 404 in 2.4.1 (docs ahead of release).
  - Evidence: api_client_test.py output: create session 200, enqueue 200 (state QUEUED), settings 200, input 200, start 200 (READY_TO_START), poll -> COMPLETED, output 200 format SES size 935, drc 200 with '$schema': 'https://schemas.kicad.org/drc.v1.json'. OpenAPI path list dumped from openapi.json (31 paths).
  - Sources: https://raw.githubusercontent.com/freerouting/freerouting/master/docs/API/API_v1.md
- **[high, VERIFIED LOCALLY]** Freerouting also ships an MCP server (--mcp_server.enabled=true, default http://127.0.0.1:37964/v1/mcp JSON-RPC, plus SSE /v1/mcp/events and WebSocket /v1/mcp/ws, and --mcp_server.stdio=true for stdio) exposing 28 tools incl. create_session, enqueue_job, upload_job_input_from_local_file(jobId,filePath), update_job_settings, start_job, get_job_details, download_job_output_to_local_file, get_job_drc_report, stream_job_logs. There is also an online NPX bridge (@freerouting/freerouting-mcp-server) to api.freerouting.app. An AI agent could talk to it directly, but it still needs a DSN from KiCad and SES import back, so it does not remove the need for the wrapper.
  - Evidence: MCP initialize returned serverName 'Freerouting MCP' 2.4.1 (protocolVersion 2024-11-05); tools/list returned 28 tools (list captured). Same Freerouting-Profile-ID header requirement applies (without it: 'Authentication failed').
  - Sources: https://raw.githubusercontent.com/freerouting/freerouting/master/docs/API/MCP.md
- **[high, VERIFIED LOCALLY]** KiCad Specctra DSN export / SES import is available ONLY through the SWIG pcbnew module (bundled python) and the GUI. kicad-cli 7/8/9/10 has NO specctra export/import (pcb export subcommands: 3dpdf,brep,drill,dxf,gencad,gerbers,glb,hpgl,ipc2581,ipcd356,odb,pdf,ply,pos,ps,stats,step,stl,stpz,svg,u3d,vrml,xao; pcb import only pads/altium/eagle/cadstar/fabmaster/pcad/solidworks). The IPC API (kipy 0.8.0 / KiCad 10 protos) has no Specctra RPC, no router RPC and no autoplacer RPC.
  - Evidence: kicad-cli pcb export --help and pcb import --help captured. Enumerated all message classes in kipy/proto/board/board_commands_pb2.pyi and common/commands/editor_commands_pb2.pyi: RunAction, CreateItems/UpdateItems/DeleteItems, GetItems, BeginCommit/EndCommit, RefillZones, InteractiveMoveItems, board jobs (export gerbers/drill/pos/step/svg/pdf/odb/ipc2581/stats) - nothing for Specctra/routing.
  - Sources: https://pypi.org/project/kicad-python/, https://dev-docs.kicad.org/en/apis-and-binding/ipc-api/
- **[high, VERIFIED LOCALLY]** pcbnew.ExportSpecctraDSN(board, path) works headlessly in ALL of KiCad 7.0.6, 8.0.1, 9.0.5, 10.0.0 (two overloads: (path) and (BOARD, path)). pcbnew.ImportSpecctraSES(board, path) works headlessly in 8/9/10, but KiCad 7.0.6 only has the 1-arg ImportSpecctraSES(path) which needs the PCB_EDIT_FRAME (GetBoard() is None standalone) and returns False without importing. A ~60-line fallback SES parser that creates PCB_TRACK/PCB_VIA via SWIG works on 7 (and is version-agnostic).
  - Evidence: Per-version loop: 7.0 ExportSpecctraDSN -> True; ImportSpecctraSES(board, ses) -> 'TypeError: takes 1 positional argument'; ImportSpecctraSES(ses) -> False, tracks 0. 8.0/9.0/10.0: ImportSpecctraSES -> True, 6 segments, unconnected 0. ses_fallback_import.py on 7.0: 'items added: 6', unconnected 0, WriteDRCReport -> only lib_footprint_issues warnings.
- **[high, VERIFIED LOCALLY]** End-to-end round trip verified on KiCad 10 (and 8, 9): SWIG-created board (2x R_0805, nets NET1/NET2, 20x15 mm outline) -> ExportSpecctraDSN -> freerouting headless -> ImportSpecctraSES -> 6 track segments, 0 vias, connectivity unconnected=0 -> 'kicad-cli pcb drc --format json --severity-all --exit-code-violations' = 0 violations, 0 unconnected, exit 0. KiCad 8 shows 2 'lib_footprint_issues' warnings only because the test footprints have no library nickname (not routing related). KiCad 7 kicad-cli has no 'pcb drc' at all (only 'pcb export'); use SWIG pcbnew.WriteDRCReport(board, path, EDA_UNITS_MILLIMETRES, True) there.
  - Evidence: Outputs in k10/, k8.0/, k9.0/, k7.0/ (tiny_routed.kicad_pcb, drc.json / drc.txt). Track list printed: 3 F.Cu segments per net, width 0.2 mm.
- **[high]** KiCad SES import semantics (10.0 source): FromSESSION deletes ALL non-locked tracks/vias, keeps locked ones (they are exported to DSN as 'fix' wires), deletes DRC markers, and also applies the SES (placement ...) section (footprint positions). So pre-routed critical nets must be LOCKED before export or they are replaced by Freerouting's version; the wrapper should lock hand/agent-routed tracks before the DSN round trip.
  - Evidence: pcbnew/specctra_import_export/specctra_import.cpp @10.0.0 lines 363-430: 'delete the old tracks and vias but save locked tracks/vias; they will be re-added later', aBoard->RemoveAll({PCB_TRACE_T}), placement walk calling footprint->SetPosition.
  - Sources: https://gitlab.com/kicad/code/kicad/-/raw/10.0.0/pcbnew/specctra_import_export/specctra_import.cpp
- **[medium]** Known KiCad DSN export gaps that affect routing quality: copper-to-edge clearance is not exported (Freerouting issue #558; mitigate with --router.copper_to_edge_clearance_um=N), copper-to-hole clearance handling issues (#14439), net-class clearance export bugs (#14713), mounting-hole footprints (#14656). KiCad 10 does export footprint rule areas as keepouts (keepout/via_keepout/wire_keepout) and board-level zones as planes. DSN is plain text, so the wrapper can post-process it (add rules/keepouts) without a source build.
  - Evidence: specctra_export.cpp @10.0.0 lines 851-936 (footprint rule areas -> KEEPOUT), rule block in exported medium.dsn: (rule (width 200) (clearance 200) (clearance 50 (type smd_smd))) and per-class (class kicad_default ... (rule ...)).
  - Sources: https://gitlab.com/kicad/code/kicad/-/issues/14439, https://gitlab.com/kicad/code/kicad/-/issues/14713, https://gitlab.com/kicad/code/kicad/-/work_items/14656, https://gitlab.com/kicad/code/kicad/-/issues/7684, https://gitlab.com/kicad/code/kicad/-/raw/10.0.0/pcbnew/specctra_import_export/specctra_export.cpp
- **[high, VERIFIED LOCALLY]** KiCad's built-in push-and-shove (PNS) router is NOT scriptable headlessly: it is not in SWIG (KiCad 7-10 pcbnew.py contains no ROUTER_TOOL/PNS::ROUTER symbols, only PNS::MEANDER_SETTINGS types) and not in the IPC API. Through IPC RunAction (KiCad 9+, GUI running) the actions 'pcbnew.InteractiveRouter.RouteSelected' and 'pcbnew.InteractiveRouter.Autoroute' ("Attempt Finish Selected (Autoroute)") return RAS_OK and DO activate the interactive router from the selected pad, but attempt-finish is a heuristic that fails on anything non-trivial and leaves the tool waiting for mouse input (0 tracks created; screenshot shows stub segments at J1.1/U1.1). RunAction is explicitly documented as 'not an API'. Driving PNS as a batch engine requires a patched source build (what LG AI Research's PCBWorld-Engine does: patched KiCad 9.0.8 + pybind11 module + unix-socket server, Linux-only, GPLv3).
  - Evidence: Local: kipy RunAction -> 'status: RAS_OK' for both actions, tracks stayed 0; screenshot ipc/screen1_small.png. Source: pcb_actions.cpp tags 7.0.6/8.0.1/9.0.5/10.0.0 all define RouteSelected/RouteSelectedFromEnd/Autoroute/Autoplacer.autoplaceSelected. kipy editor_commands_pb2.pyi RunAction docstring: 'WARNING: The TOOL_ACTIONs are specifically *not* an API'.
  - Sources: https://gitlab.com/kicad/code/kicad/-/raw/master/pcbnew/tools/pcb_actions.cpp, https://github.com/LGAI-Research/PCBWorld, https://github.com/LGAI-Research/PCBWorld-Engine
- **[high, VERIFIED LOCALLY]** KiCad's legacy built-in autoplacer IS reachable via the IPC API on KiCad 9/10 (GUI required): kicad.run_action('pcbnew.Autoplacer.autoplaceOffboard') placed 13 footprints that had been moved off-board back inside the outline within ~5 s; 'pcbnew.Autoplacer.autoplaceSelected' also exists. Quality is crude (grid packing near the largest part, connectivity only loosely considered) - usable as a seed, not a result.
  - Evidence: ipc/autoplace.py: moved 13 parts to x=-20 mm via board.update_items, run_action -> RAS_OK, afterwards 15/15 footprints at x>0 (positions printed, e.g. U1 (20,15)->(29,13), R1->(22,15)); screenshot ipc/screen2_small.png.
  - Sources: https://gitlab.com/kicad/code/kicad/-/raw/master/pcbnew/tools/pcb_actions.cpp
- **[high, VERIFIED LOCALLY]** KiCadRoutingTools (github.com/drandyhaas/KiCadRoutingTools, MIT, v0.22.1, commit 2026-09-18, 465 stars, 4200+ commits) is a file-based Rust-accelerated A* autorouter + placement toolkit for KiCad 9/10 that reads/writes .kicad_pcb directly with NO KiCad process or SWIG: 'python py_router/route.py in.kicad_pcb out.kicad_pcb --nets "*"'. Needs numpy/scipy/shapely/Pillow and a prebuilt grid_router.pyd fetched by build_router.py. Verified locally: tiny board 2/2 nets in 0.12 s; medium board 30/30 nets, 45 vias, 0.7 s router time (~4 s wall), KiCad DRC 0 violations / 0 unconnected. It also has diff pairs, planes, BGA/QFN fanout, length matching, a Claude Code 'AI tab' and 9 Claude skills. Side effect: it writes DRC constraint values into the sibling .kicad_pro. Limitations: no push-and-shove, more vias than Freerouting (45 vs 23 on the same board), no blind/buried vias.
  - Evidence: Outputs: krt_tiny_routed.kicad_pcb (DRC 0/0), med10/medium_krt_routed.kicad_pcb (DRC 0/0, 370 track items, 45 vias), JSON_SUMMARY_MIN lines captured, render med10/render_krt.png.
  - Sources: https://github.com/drandyhaas/KiCadRoutingTools, https://hackaday.io/project/204891-kicad-automated-routing-tools
- **[high, VERIFIED LOCALLY]** Agent-driven placement loop is feasible today with KiCadRoutingTools' intent-driven seeder: an agent writes a small floorplan-intent JSON (zones with ref globs, edge_connectors with edge + along_edge_band, keepouts, decaps max_distance, must_lock), then 'py_placer/place_seed.py unplaced.kicad_pcb seeded.kicad_pcb --intent intent.json --force' produced a legal placement from a board with all 15 parts stacked at one point in ~1 s (J1 west, J2 east, MCU+decaps in the centre zone, resistors in the bottom zone), which then routed 30/30 with 0 DRC violations (only silk warnings). The project also has place_optimize.py (perturbative quench, --max-displacement), place_route_loop.py (router-in-the-loop), place_portfolio.py, check_floorplan.py (grades a placement against intent, --emit-intent) and py_tools/render_placement.py (before/after PNG with ratsnest).
  - Evidence: place/intent.json, place/seeded.kicad_pcb, place/seeded_routed.kicad_pcb, place/drc.json ({'silk_edge_clearance': 1, 'silk_over_copper': 2}, unconnected 0), render place/render_seeded_routed.png. Seeder log: 'Seeded 15 part(s); 0 unseated', quench crossings 101 -> 67.
  - Sources: https://github.com/drandyhaas/KiCadRoutingTools/blob/main/docs/placement-optimization.md, https://github.com/drandyhaas/KiCadRoutingTools/blob/main/docs/floorplan-intent.md
- **[high, VERIFIED LOCALLY]** Image feedback for the agent is cheap and headless: 'kicad-cli pcb render --side top --width W --height H --background opaque --quality basic -o out.png board.kicad_pcb' renders a raytraced top view in ~0.5 s on KiCad 10 (used to compare Freerouting vs KRT results), and 'kicad-cli pcb export svg --layers F.Cu,B.Cu,Edge.Cuts' gives a vector plot. Combined with DRC JSON, connectivity counts and KRT's placement/routability scores this gives the agent a numeric + visual loop without any GUI.
  - Evidence: med10/render_fr.png, med10/render_krt.png, place/render_seeded_routed.png created ('Successfully created 3D render image', 1 s for two renders); med10/fr.svg 66 KB.
- **[high]** Freerouting's own KiCad plugin (integrations/KiCad, PCM package 'Freerouting' 2.4.1, kicad_version>=6.0) is a SWIG ACTION plugin that runs inside the pcbnew GUI: DSN mode calls pcbnew.ExportSpecctraDSN / ImportSpecctraSES exactly like our headless path, spawns java, then pcbnew.Refresh(); the experimental 'JSON/API' mode (KiCad 9+) walks the board via SWIG into Freerouting's own JSON, POSTs to the local REST server, and applies traces/vias with pcbnew.PCB_TRACK/PCB_VIA in a BOARD_COMMIT. It is NOT the official protobuf IPC API; Freerouting's research doc says IPC cannot replace DSN yet (edge clearance/rules gaps) and that headless IPC is only expected in KiCad 11.
  - Evidence: Downloaded plugin.py/router_dsn.py/router_json_api.py/board_json_helpers.py (fr_plugin/): router_dsn.py lines 102-258, router_json_api.py lines 289-414. README table: DSN default/recommended, JSON/API experimental.
  - Sources: https://raw.githubusercontent.com/freerouting/freerouting/master/integrations/KiCad/README.md, https://raw.githubusercontent.com/freerouting/freerouting/master/docs/research/kicad_ipc_api_research.md
- **[medium]** Other routers: OrthoRoute (bbenchoff, MIT, IPC-API plugin, CUDA/Metal GPU PathFinder on a Manhattan lattice; aimed at huge dense backplanes/BGA escapes, not general boards, needs GPU + GUI). Topola (Codeberg topola/topola, MIT, Rust, NLnet-funded topological router, DSN/SES tags, 'work in progress', interactive focus). JustRoute (charlesrwest, KiCad autorouter, small project). tscircuit-autorouter (@tscircuit/capacity-autorouter, MIT, TypeScript/Bun, input is its own SimpleRouteJson, offline library + cloud API; no KiCad DSN I/O documented, and no node/npm on this machine). Commercial cloud: DeepPCB (instadeep, open-source Apache-2.0 KiCad plugin at github.com/instadeepai/deeppcb-kicad-plugin, KiCad 10 recommended, needs account/API key, no public headless REST documented), Quilter (placement+routing, uploads KiCad projects, hours per board, no public API found), Flux.ai (own EDA). TopoR/Electra: commercial Specctra-DSN routers. ML/RL: PCBWorld (LG AI Research, KDD 2026 workshop) is an RL env on a patched headless KiCad PNS engine, Linux; DreamerV3+Freerouting paper; none production-usable.
  - Evidence: Web research only; see sources.
  - Sources: https://github.com/bbenchoff/OrthoRoute, https://forum.kicad.info/t/orthoroute-or-a-new-autorouter-plugin-for-ipc-apis/65177, https://github.com/mikwielgus/topola, https://codeberg.org/topola/topola, https://github.com/charlesrwest/JustRoute, https://github.com/tscircuit/tscircuit-autorouter, https://docs.tscircuit.com/web-apis/autorouting-api, https://github.com/instadeepai/deeppcb-kicad-plugin, https://deeppcb.ai/deeppcb-kicad-plugin-ai-pcb-routing/, https://www.quilter.ai/product, https://github.com/LGAI-Research/PCBWorld, https://www.sciencedirect.com/science/article/abs/pii/S0957417426003374
- **[medium]** Relevant prior art for the 'AI agent drives KiCad' idea that already bundles routing: Konnect (mixelpixx/Konnect, AGPL-3.0 + commercial, single Rust MCP binary, KiCad 10 IPC API + S-expression parsing, 226 tools; autorouting = local Freerouting via DSN/SES, placement has a headless fallback) and kicad-happy (aklofas, Claude Code/Codex skills). KRT itself ships Claude skills. Freerouting's own docs say GPL-3 applies to the router; calling it as a separate process from a proprietary wrapper is the standard arm's-length arrangement (not legal advice).
  - Evidence: Web research only.
  - Sources: https://github.com/mixelpixx/Konnect, https://github.com/aklofas/kicad-happy, https://github.com/freerouting/freerouting
- **[high, VERIFIED LOCALLY]** KiCad 10 first-run 'KiCad Setup' wizard (common/startwizard) blocks pcbnew when launched with a fresh KICAD_CONFIG_HOME; to launch a GUI headlessly-ish for IPC tests the config dir must contain kicad_common.json (with do_not_show_again.update_check_prompt and data_collection_prompt = true), fp-lib-table, sym-lib-table and design-block-lib-table, and pcbnew.json with system.first_run_shown=true. The API socket on Windows is a named pipe (ipc://%TEMP%\kicad\api.sock string, no file appears); kipy 0.8.0 connected to KiCad 10.0.0 (reported API 10.0.6). Standalone SWIG python does NOT rewrite kicad_common.json (tested with isolated config), but GUI apps do on exit (working_dir etc.).
  - Evidence: startwizard.cpp/startwizard_provider_*.cpp @10.0.0 NeedsUserInput() conditions read from source; local launches: first two attempts showed window title 'KiCad Setup', third (after adding tables + first_run_shown) showed 'ipc_test - PCB Editor' and kipy connected. Isolated kicad_cfg/10.0/kicad_common.json mtime unchanged after SWIG load/save/export/FootprintLoad.
  - Sources: https://gitlab.com/kicad/code/kicad/-/raw/10.0.0/common/startwizard/startwizard.cpp
- **[high, VERIFIED LOCALLY]** Freerouting '-drc report.json' is a standalone DRC-only mode on a DSN (it does not route; exits 2 when unconnected items exist) and in 2.4.1 it writes the report into the current working directory ignoring the directory part of the given path. The REST /v1/jobs/{id}/drc endpoint is the reliable way to get Freerouting's DRC JSON after routing; KiCad's own kicad-cli DRC should remain the authority anyway.
  - Evidence: Run with -drc C:/.../med10/fr_drc.json produced ./fr_drc.json in the cwd ('unconnectedItems': 30, 'violations': 0), exit code 2; a normal route run exits 0.

## Capabilities

- Fully headless autorouting on Windows with installed KiCad binaries only: SWIG pcbnew (bundled python) ExportSpecctraDSN -> freerouting-2.4.1.jar (private JRE 25 folder, no install) -> ImportSpecctraSES (8/9/10) or fallback SES parser (7) -> kicad-cli pcb drc JSON (8+) / SWIG WriteDRCReport (7). Verified on 7.0.6, 8.0.1, 9.0.5, 10.0.0.
- Second, KiCad-free router with placement tooling: KiCadRoutingTools (MIT) reads/writes .kicad_pcb directly, 9/10 file formats, CLI with JSON summaries, plus diff pairs, planes, fanout, length matching, placement seed/optimize/route-loop, before/after rendering.
- Long-running/streaming routing via Freerouting's local REST API (sessions/jobs, SSE logs and in-progress output, cancel, per-job settings, KiCad-schema DRC JSON) - suitable for a VS Code extension progress UI without any KiCad plugin.
- Direct AI-agent access to Freerouting via its MCP server (28 tools, local stdio or HTTP) if the orchestrator wants to bypass the wrapper for the router step.
- Agent placement loop: intent JSON (zones, edge connectors, keepouts, decap tethering, locks) -> place_seed -> route -> kicad-cli drc + connectivity + kicad-cli pcb render PNG/SVG -> iterate with place_optimize/place_route_loop; or explicit coordinates via SWIG/IPC update_items with the same feedback.
- KiCad's legacy autoplacer reachable via IPC RunAction('pcbnew.Autoplacer.autoplaceOffboard'/'autoplaceSelected') on KiCad 9/10 with the GUI open - usable as a crude seed.
- Rich, machine-readable quality signals: Freerouting result_json manifest and score (0 unrouted/0 violations), KRT JSON_SUMMARY/JSON_SUMMARY_MIN, KiCad DRC JSON (drc.v1 schema) with typed violations and unconnected items, SWIG GetConnectivity().GetUnconnectedCount().
- Pre-routed nets can be preserved through the Freerouting round trip by locking tracks (exported as 'fix', re-added on import); -inc lets the router ignore net classes (e.g. GND/VCC planes).

## Limitations

- No Specctra DSN/SES in kicad-cli or the IPC API on 7-10: the DSN round trip depends on SWIG (deprecated-but-present in 10) or a GUI action plugin. Long-term risk if a future KiCad drops SWIG; mitigation: the wrapper's own DSN writer/SES parser (SES parser already prototyped) or Freerouting's JSON path.
- KiCad 7 cannot ImportSpecctraSES headlessly (1-arg overload needs the editor frame) and has no kicad-cli drc/render; needs the fallback SES importer and SWIG WriteDRCReport.
- Freerouting requires Java 25 (2.4.x) - must ship/download a JRE; the machine's PATH Java is 1.8 and the task brief's 'Java 17 on PATH' is wrong. Freerouting is GPL-3: keep it a separate process.
- Freerouting defaults can violate KiCad rules (neck-down to 0.15 mm, fanout vias) - wrapper must set automatic_neckdown/neck_width_um/fanout per board min width and always re-run KiCad DRC as the authority; DSN export loses copper-to-edge clearance (set --router.copper_to_edge_clearance_um).
- SES import wipes all non-locked tracks and applies SES placement; the wrapper must lock what it wants kept and should snapshot the board before import.
- KiCad's PNS push-and-shove router cannot be driven as a batch engine without a patched source build (PCBWorld-Engine style, Linux); RunAction('...Autoroute') is GUI-only, blocks the API waiting for mouse input, and is documented as not an API.
- IPC API needs a running GUI on 9/10 (headless IPC expected only in KiCad 11 per Freerouting's research doc - unverified), needs api.enable_server=true in kicad_common.json, and a fresh config dir triggers the KiCad Setup wizard unless pre-seeded. GUI apps rewrite kicad_common.json on exit - always use KICAD_CONFIG_HOME for automated GUI sessions.
- KiCadRoutingTools is a fast-moving single-maintainer project (v0.22.1, daily commits) with its own parser/writer for .kicad_pcb (KiCad 9/10 only) and it writes DRC constraints into the .kicad_pro; pin a version and run KiCad DRC after it.
- Freerouting -drc mode writes to CWD and does not route; documented composite /v1/autoroute and /drc/summary endpoints are absent in 2.4.1.
- Other open-source routers are niche or immature: OrthoRoute (GPU, backplanes), Topola (WIP), tscircuit (own JSON format, needs Node which is not installed here). Commercial AI routers (DeepPCB, Quilter) are cloud, account-gated, no documented headless API for DeepPCB's KiCad plugin.
- From-scratch fully automatic placement without declared intent remains unsolved (Altium removed its autoplacer; KRT docs and Cypress ISPD'25 concur); the agent must supply constraints (edge connectors, zones, decap tethers, locks) - that is the realistic loop.
- Speed numbers here are for a 30-net 2-layer board (Freerouting 3.5 s, KRT ~1 s); dense boards can take minutes to hours in Freerouting (score-based early stop helps); PCBench corpus in the repo can be used for regression benchmarking.

## Version notes

SWIG: ExportSpecctraDSN(BOARD, path) headless OK on 7.0.6/8.0.1/9.0.5/10.0.0; ImportSpecctraSES(BOARD, path) headless OK on 8/9/10 only - 7.0.6 has ImportSpecctraSES(path) which requires the PCB_EDIT_FRAME and returns False standalone (use fallback SES->PCB_TRACK/PCB_VIA parser). pcbnew.py exposes no router/autoplacer in any version (8/9/10 only reference PNS::MEANDER_SETTINGS). kicad-cli: 7.0 has only 'pcb export' (no drc, no render); 8+ has 'pcb drc --format json --severity-all --exit-code-violations'; 'pcb render' verified on 10 (present since 8 per docs, unverified); no Specctra in any version; 10 dropped hpgl. IPC API: 9.0+ only, GUI required, disabled by default (kicad_common.json api.enable_server); kipy 0.8.0 works with 10.0.0 (API 10.0.6). RunAction exists in 9/10; the actions pcbnew.InteractiveRouter.{RouteSelected,RouteSelectedFromEnd,Autoroute} and pcbnew.Autoplacer.{autoplaceSelected,autoplaceOffboard} exist in source for 7.0.6/8.0.1/9.0.5/10.0.0 but are only reachable programmatically via IPC on 9+. DRC JSON schema drc.v1 is produced by kicad-cli 8+ and by Freerouting 2.2+. SES import behaviour (delete non-locked tracks, keep locked, apply placement) read from 10.0 source; 7/8/9 believed same (older code) but not re-checked. KiCad 10 exports footprint rule areas as DSN keepouts (older 7.x issue #7684 was about this). Freerouting: 1.9.x Java 17 era, 2.0-2.1 Java 21, 2.2-2.4 Java 25; its PCM KiCad plugin declares kicad_version>=6.0 and works on 7-10 as a SWIG action plugin; its JSON/API mode is 9+ only. KiCadRoutingTools supports KiCad 9 and 10 file formats only (not 7/8). Wrapper design consequence: version-agnostic adapters = (a) 'specctra' capability via SWIG with 7-specific import fallback, (b) 'drc' via kicad-cli on 8+ / SWIG on 7, (c) 'render' via kicad-cli on 9/10 (SVG export fallback on 7/8), (d) optional IPC 'apply/select/run_action' adapter on 9+ when a GUI is open.

## Recommendations

- Adopt Freerouting 2.4.1 as the primary autorouter behind a 'route' tool, invoked as a separate process (GPL arm's length) via the local REST API (for progress streaming, cancel, DRC JSON) with the plain CLI as fallback. Bundle/download a Temurin JRE 25 into the extension's storage (no system install); do not rely on PATH java.
- Implement the KiCad side of the round trip in the bundled KiCad python (SWIG): ExportSpecctraDSN(board, dsn) and ImportSpecctraSES(board, ses) on 8/9/10, and the fallback SES parser (ses_fallback_import.py) on 7 - keep the fallback parser as the version-agnostic safety net in case SWIG disappears. Lock any tracks that must survive before export; snapshot the board file before import.
- Default Freerouting settings from the board's rules: --router.automatic_neckdown=false (or neck_width_um >= min track width), --router.fanout.enabled=false unless BGAs, --router.copper_to_edge_clearance_um from BOARD_DESIGN_SETTINGS.m_CopperEdgeClearance, -inc for plane nets, --router.result_json for a manifest, -da, --user_data_path and --logging.file.location under the extension's storage. Always finish with kicad-cli pcb drc (8+) / SWIG WriteDRCReport (7) as the authority.
- Add KiCadRoutingTools (MIT) as a second 'route' backend and as the PLACEMENT backend: place_seed (intent JSON) -> place_optimize/place_route_loop -> route.py, all file-based with JSON summaries; pin its version, run in a dedicated venv (numpy/scipy/shapely/Pillow) with the prebuilt Rust .pyd, and run KiCad DRC after it (it rewrites .kicad_pro constraints).
- Design the agent placement loop as: agent emits/edits a floorplan-intent JSON (edge connectors, zones, keepouts, decap tethers, locks) -> seed -> route -> DRC + connectivity + kicad-cli pcb render PNG (and SVG) + KRT placement grade -> agent adjusts intent or explicit coordinates (SWIG SetPosition / IPC update_items) -> repeat. Do not build a from-scratch autoplacer; expose KiCad's legacy autoplacer via IPC only as an optional seed.
- On the 'in-KiCad plugin <-> VS Code extension' channel for routing/placement: NOT needed for v1. Everything above runs headless on files; the only things a KiCad-side plugin adds are live in-editor visualisation and undo-integrated commits while the user has the board open. Cover that case with a thin optional IPC adapter (KiCad 9+): detect an open document via GetOpenDocuments, apply routed tracks/vias with CreateItems inside BeginCommit/EndCommit (or ask the user to save/close), and use RunAction only for the autoplacer seed. Freerouting's and DeepPCB's own plugins prove the SWIG-action-plugin route works but it ties the wrapper to the GUI and to deprecated SWIG.
- A KiCad SOURCE BUILD is genuinely required only if you want to (1) drive the push-and-shove router as a batch engine (patched PNS + pybind11 like PCBWorld-Engine) or (2) fix DSN export gaps inside KiCad instead of post-processing DSN text. Neither is needed for v1; treat the source build as a later debugging aid (e.g. reproducing specctra_export/import issues, pns_log_viewer) and keep it out of the critical path.
- Provide the wrapper's 'route' tool with explicit knobs the agent can reason about: router backend, passes/timeout, via cost, ignored net classes, layer preferred directions, lock/keep sets, and return a structured result (routed/unrouted nets, vias, DRC summary, render path). Use the PCBench corpus (in the Freerouting repo) plus your own boards for regression benchmarks of both backends.
- Do not spend effort on OrthoRoute/Topola/tscircuit/RL routers for now; re-evaluate Topola when it reaches a CLI release, and DeepPCB/Quilter only if a cloud, account-based backend is acceptable to the user.

## Experiments run

- **Download Freerouting 2.4.1 jar and a private Temurin JRE 25 into the scratchpad; confirm Java 17 fails and Java 25 runs -help**
  - Result: Java 17: UnsupportedClassVersionError (class file 69 vs max 61). Java 25: 'Freerouting v2.4.1 (build-date: 2026-09-03)' + usage text.
  - Command/code:

```
cd <scratchpad>/autorouting && curl -sL -o freerouting-2.4.1.jar https://github.com/freerouting/freerouting/releases/download/v2.4.1/freerouting-2.4.1.jar && curl -sL -o temurin-jre25.zip "https://api.adoptium.net/v3/binary/latest/25/ga/windows/x64/jre/hotspot/normal/eclipse?project=jdk" && unzip -q temurin-jre25.zip -d jre25 && "/c/Program Files/Eclipse Adoptium/jre-17.0.12.7-hotspot/bin/java.exe" -jar freerouting-2.4.1.jar -help; ./jre25/jdk-25.0.4.1+1-jre/bin/java.exe -jar freerouting-2.4.1.jar --user_data_path="$PWD/fr_userdata" -da -help
```

  - Artifact: `<scratchpad>\autorouting\freerouting-2.4.1.jar`
- **Create tiny 2-footprint/2-net board with pcbnew SWIG (KiCad 10 bundled python) and export Specctra DSN**
  - Result: saved tiny.kicad_pcb; ExportSpecctraDSN -> True (2321-byte DSN with (rule (width 200)(clearance 200)) and Via[0-1]_600:300_um); tracks before routing: 0
  - Command/code:

```
"/c/Program Files/KiCad/10.0/bin/python.exe" make_board.py "$PWD/k10"   # make_board.py: BOARD(), SetCopperLayerCount(2), 4 PCB_SHAPE Edge.Cuts segments 20x15mm, NETINFO_ITEM NET1/NET2, FootprintLoad(Resistor_SMD.pretty, R_0805_2012Metric) x2 at (5,7.5)/(15,7.5) mm, pad1->NET1 pad2->NET2, BuildListOfNets, SaveBoard, pcbnew.ExportSpecctraDSN(board, dsn)
```

  - Artifact: `<scratchpad>\autorouting\make_board.py`
- **Route tiny DSN with Freerouting headless, import SES via SWIG, count tracks, run kicad-cli DRC (KiCad 10)**
  - Result: Job COMPLETED in 0.24 s, SES 935 bytes; ImportSpecctraSES -> True; 6 F.Cu segments (0.2 mm), 0 vias, unconnected 0; DRC: 0 violations, 0 unconnected, exit 0
  - Command/code:

```
./jre25/jdk-25.0.4.1+1-jre/bin/java.exe -jar freerouting-2.4.1.jar --user_data_path="$PWD/fr_userdata" -da -de "$PWD/k10/tiny.dsn" -do "$PWD/k10/tiny.ses" -mp 20 -mt 2 --gui.enabled=false --logging.file.location="$PWD/fr_userdata"
"/c/Program Files/KiCad/10.0/bin/python.exe" import_ses.py k10/tiny.kicad_pcb k10/tiny.ses k10/tiny_routed.kicad_pcb   # LoadBoard, pcbnew.ImportSpecctraSES(board, ses), list PCB_TRACK/PCB_VIA, SaveBoard, GetConnectivity().GetUnconnectedCount(True)
"/c/Program Files/KiCad/10.0/bin/kicad-cli.exe" pcb drc --format json --severity-all --exit-code-violations -o k10/drc.json k10/tiny_routed.kicad_pcb
```

  - Artifact: `<scratchpad>\autorouting\k10`
- **Repeat DSN->Freerouting->SES->DRC round trip with KiCad 7.0.6, 8.0.1, 9.0.5 bundled pythons**
  - Result: 8.0 and 9.0: import True, 6 segments, unconnected 0, DRC 0 unconnected (8.0 shows 2 lib_footprint_issues warnings only). 7.0: export True but ImportSpecctraSES(board, ses) TypeError (1-arg only) and ImportSpecctraSES(ses) -> False (GetBoard() is None); kicad-cli 7.0 has no 'pcb drc'.
  - Command/code:

```
for v in 7.0 8.0 9.0; do "/c/Program Files/KiCad/$v/bin/python.exe" make_board.py "$PWD/k$v" "C:/Program Files/KiCad/$v/share/kicad"; ./jre25/jdk-25.0.4.1+1-jre/bin/java.exe -jar freerouting-2.4.1.jar --user_data_path="$PWD/fr_userdata" -da -de "$PWD/k$v/tiny.dsn" -do "$PWD/k$v/tiny.ses" -mp 20 -mt 2 --gui.enabled=false; "/c/Program Files/KiCad/$v/bin/python.exe" import_ses.py k$v/tiny.kicad_pcb k$v/tiny.ses k$v/tiny_routed.kicad_pcb; "/c/Program Files/KiCad/$v/bin/kicad-cli.exe" pcb drc --format json --severity-all --exit-code-violations -o k$v/drc.json k$v/tiny_routed.kicad_pcb; done
```

  - Artifact: `<scratchpad>\autorouting\k7.0`
- **Fallback SES importer (S-expression parser creating PCB_TRACK/PCB_VIA via SWIG) validated on KiCad 7 plus SWIG DRC**
  - Result: items added: 6; unconnected 0; WriteDRCReport True; report: 2 lib_footprint_issues warnings, 0 unconnected pads
  - Command/code:

```
"/c/Program Files/KiCad/7.0/bin/python.exe" ses_fallback_import.py k7.0/tiny.kicad_pcb k7.0/tiny.ses k7.0/tiny_routed.kicad_pcb && "/c/Program Files/KiCad/7.0/bin/python.exe" -c "import pcbnew; b=pcbnew.LoadBoard('k7.0/tiny_routed.kicad_pcb'); print(pcbnew.WriteDRCReport(b,'k7.0/drc.txt',pcbnew.EDA_UNITS_MILLIMETRES,True))"
```

  - Artifact: `<scratchpad>\autorouting\ses_fallback_import.py`
- **Freerouting embedded REST API: start server, drive session/job/input/start/poll/output/drc with urllib; dump OpenAPI endpoint list; MCP tools/list**
  - Result: REST: session 200, enqueue 200, settings 200, input 200, start 200, poll COMPLETED, output SES 935 B with statistics, /drc 200 (drc.v1 schema). 31 OpenAPI paths (no /v1/autoroute, no /drc/summary). MCP: 28 tools listed (serverVersion 2.4.1); without Profile-ID header: 'Authentication failed'.
  - Command/code:

```
./jre25/jdk-25.0.4.1+1-jre/bin/java.exe -jar freerouting-2.4.1.jar --user_data_path="$PWD/fr_userdata" -da --gui.enabled=false --api_server.enabled=true --api_server.authentication.enabled=false --api_server.endpoints=http://127.0.0.1:37864 --mcp_server.enabled=true --mcp_server.authentication.enabled=false &
python api_client_test.py k10/tiny.dsn k10/tiny_api.ses   # headers Freerouting-Profile-ID=<uuid>, Freerouting-Environment-Host=KiCad/10.0
curl -s http://127.0.0.1:37864/openapi/openapi.json -o openapi.json
curl -s -X POST http://127.0.0.1:37964/v1/mcp -H 'Content-Type: application/json' -H 'Freerouting-Profile-ID: <uuid>' -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}'
# stopped with PowerShell Stop-Process on the java PIDs I started
```

  - Artifact: `<scratchpad>\autorouting\api_client_test.py`
- **Medium board (TQFP-32 + 2x 1x10 headers + 8 R0603 + 4 C0603, 30 nets, 40x30 mm) generated with SWIG; routed with Freerouting (default vs neckdown/fanout off) and with KiCadRoutingTools; KiCad DRC and renders**
  - Result: Freerouting default: 46/46 routed in 3.0 s but 26 track_width DRC violations (0.15 mm neckdown). Freerouting with neckdown+fanout off: 46/46 in 3.5 s, 23 vias, DRC 0/0. KRT: 30/30 nets, 45 vias, 0.7 s router (4 s wall), DRC 0/0. Renders show both are sane; Freerouting cleaner (45-degree, fewer vias). (First attempt had my J1 header rotated off-board -> 8 legitimately unroutable nets; fixed.)
  - Command/code:

```
"/c/Program Files/KiCad/10.0/bin/python.exe" make_medium.py "$PWD/med10" "C:/Program Files/KiCad/10.0/share/kicad"
./jre25/jdk-25.0.4.1+1-jre/bin/java.exe -jar freerouting-2.4.1.jar --user_data_path="$PWD/fr_userdata" -da -de "$PWD/med10/medium.dsn" -do "$PWD/med10/medium_nn.ses" -mp 100 -mt 4 --gui.enabled=false --router.automatic_neckdown=false --router.fanout.enabled=false --router.result_json="$PWD/med10/fr_result.json"
"/c/Program Files/KiCad/10.0/bin/python.exe" import_ses.py med10/medium.kicad_pcb med10/medium_nn.ses med10/medium_fr_nn_routed.kicad_pcb
krtvenv/Scripts/python.exe krt/py_router/route.py med10/medium_krt_in.kicad_pcb med10/medium_krt_routed.kicad_pcb --nets "*"
"/c/Program Files/KiCad/10.0/bin/kicad-cli.exe" pcb drc --format json --severity-all --exit-code-violations -o med10/drc_fr_nn.json med10/medium_fr_nn_routed.kicad_pcb
"/c/Program Files/KiCad/10.0/bin/kicad-cli.exe" pcb render --width 1000 --height 750 --side top --background opaque --quality basic -o med10/render_fr.png med10/medium_fr_nn_routed.kicad_pcb
```

  - Artifact: `<scratchpad>\autorouting\med10`
- **Install KiCadRoutingTools (clone + venv numpy/scipy/shapely/Pillow + prebuilt Rust router) and route the tiny board**
  - Result: grid_router v0.22.0 .pyd downloaded from release v0.22.1; 2/2 nets routed in 0.12 s; DRC 0 violations/0 unconnected; note it wrote 9 DRC constraint values into krt_tiny_routed.kicad_pro
  - Command/code:

```
git clone --depth 1 https://github.com/drandyhaas/KiCadRoutingTools.git krt && uv venv krtvenv --python 3.11 && uv pip install --python krtvenv/Scripts/python.exe numpy scipy shapely Pillow && (cd krt && ../krtvenv/Scripts/python.exe build_router.py) && krtvenv/Scripts/python.exe krt/py_router/route.py krt_tiny.kicad_pcb krt_tiny_routed.kicad_pcb --nets "*" && "/c/Program Files/KiCad/10.0/bin/kicad-cli.exe" pcb drc --format json --severity-all --exit-code-violations -o krt_tiny_drc.json krt_tiny_routed.kicad_pcb
```

  - Artifact: `<scratchpad>\autorouting\krt`
- **Intent-driven placement from scratch with KRT place_seed on a board with all 15 parts stacked at one point, then route + DRC + render**
  - Result: Seeded 15 parts, 0 unseated, ~1 s (quench crossings 101->67); routed 30/30 nets, 71 vias, 0.5 s; DRC: only silk warnings (silk_edge_clearance 1, silk_over_copper 2), 0 unconnected; render shows J1 west, J2 east, MCU centred with decaps adjacent, resistors in bottom zone
  - Command/code:

```
"/c/Program Files/KiCad/10.0/bin/python.exe" -c "import pcbnew; b=pcbnew.LoadBoard('med10/medium.kicad_pcb'); [ (fp.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(20),pcbnew.FromMM(15))), fp.SetOrientationDegrees(0)) for fp in b.GetFootprints()]; pcbnew.SaveBoard('place/unplaced.kicad_pcb', b)"
# place/intent.json: blocks mcu {refs [U1, C?], zone [10,4,30,20]}, pulls {refs [R?], zone [8,22,34,29]}; edge_connectors J1 west / J2 east along_edge_band 0.1-0.9; decaps max_distance_mm 4
krtvenv/Scripts/python.exe krt/py_placer/place_seed.py place/unplaced.kicad_pcb place/seeded.kicad_pcb --intent place/intent.json --force
krtvenv/Scripts/python.exe krt/py_router/route.py place/seeded.kicad_pcb place/seeded_routed.kicad_pcb --nets "*"
"/c/Program Files/KiCad/10.0/bin/kicad-cli.exe" pcb drc --format json --severity-all --exit-code-violations -o place/drc.json place/seeded_routed.kicad_pcb
"/c/Program Files/KiCad/10.0/bin/kicad-cli.exe" pcb render --width 1000 --height 750 --side top --background opaque --quality basic -o place/render_seeded_routed.png place/seeded_routed.kicad_pcb
```

  - Artifact: `<scratchpad>\autorouting\place`
- **Install kipy (kicad-python 0.8.0) in a venv and enumerate IPC API protobuf commands for routing/placement/specctra**
  - Result: No Specctra/router/autoplacer RPCs; RunAction present with 'not an API' warning; InteractiveMoveItems, RefillZones, CreateItems/UpdateItems, Begin/EndCommit, board export jobs available
  - Command/code:

```
uv venv kipyvenv --python 3.11 && uv pip install --python kipyvenv/Scripts/python.exe kicad-python && grep -E '^class [A-Z]' kipyvenv/Lib/site-packages/kipy/proto/board/board_commands_pb2.pyi kipyvenv/Lib/site-packages/kipy/proto/common/commands/editor_commands_pb2.pyi
```

  - Artifact: `<scratchpad>\autorouting\kipyvenv`
- **Launch pcbnew 10 GUI with isolated KICAD_CONFIG_HOME (API enabled, wizard suppressed) and test RunAction router/autoplacer actions via kipy; screenshots; process cleanup**
  - Result: KiCad 10.0.0 / API 10.0.6 connected. Router actions: RAS_OK but 0 tracks; screenshot shows interactive router started (stubs at J1.1/U1.1) waiting for mouse. Autoplacer: RAS_OK, all 15 footprints back inside the outline in ~5 s (crude grid placement). First two launches were blocked by the 'KiCad Setup' wizard until lib tables + first_run_shown were added. User's %APPDATA% kicad_common.json api.enable_server remained False.
  - Command/code:

```
# kicad_cfg/10.0/: copies of user's kicad_common.json (api.enable_server=true, do_not_show_again.* true), pcbnew.json + kicad.json (system.first_run_shown=true), fp-lib-table, sym-lib-table, design-block-lib-table
PowerShell: $env:KICAD_CONFIG_HOME="...\kicad_cfg"; Start-Process "C:\Program Files\KiCad\10.0\bin\pcbnew.exe" -ArgumentList "...\ipc\ipc_test.kicad_pcb"
kipyvenv/Scripts/python.exe ipc/probe.py      # KiCad(); get_board(); add_to_selection(SIG1 pads); run_action('pcbnew.InteractiveRouter.Autoroute'); run_action('pcbnew.InteractiveRouter.RouteSelected'); count tracks
PowerShell SendKeys ESC x3 to pcbnew; kipyvenv/Scripts/python.exe ipc/autoplace.py   # move 13 footprints to x=-20mm via update_items; run_action('pcbnew.Autoplacer.autoplaceOffboard'); read positions
PowerShell: Stop-Process on my pcbnew PID (no save)
```

  - Artifact: `<scratchpad>\autorouting\ipc`
- **Check whether standalone SWIG pcbnew rewrites kicad_common.json (using the isolated config home)**
  - Result: kicad_common.json mtime and working_dir unchanged -> standalone SWIG does not write settings (GUI apps do)
  - Command/code:

```
cd swigwd && KICAD_CONFIG_HOME="$PWD/../kicad_cfg" "/c/Program Files/KiCad/10.0/bin/python.exe" -c "import pcbnew; b=pcbnew.LoadBoard('tiny.kicad_pcb'); pcbnew.SaveBoard('tiny2.kicad_pcb', b); pcbnew.FootprintLoad(r'C:\Program Files\KiCad\10.0\share\kicad\footprints\Resistor_SMD.pretty','R_0805_2012Metric'); pcbnew.ExportSpecctraDSN(b,'tiny2.dsn')"; stat -c %y ../kicad_cfg/10.0/kicad_common.json
```

  - Artifact: `<scratchpad>\autorouting\swigwd`
- **Freerouting -drc standalone mode and exit codes; settings key names extracted from jar classes**
  - Result: -drc wrote ./fr_drc.json in CWD (30 unconnectedItems, 0 violations), did not route, exit 2; normal route exit 0. Keys: router.automatic_neckdown, router.neck_width_um, router.copper_to_edge_clearance_um, router.strict_drc, router.ignore_net_classes, router.job_timeout, router.result_json, router.fanout.enabled
  - Command/code:

```
./jre25/jdk-25.0.4.1+1-jre/bin/java.exe -jar freerouting-2.4.1.jar --user_data_path="$PWD/fr_userdata" -da -de "$PWD/med10/medium.dsn" -drc "$PWD/med10/fr_drc.json" --gui.enabled=false; echo $?
unzip -q -o freerouting-2.4.1.jar 'app/freerouting/settings/*' -d jarx && python -c "<regex over RouterSettings.class for copper|edge|neck|strict|fanout|result_json|ignore_net|timeout>"
```

  - Artifact: `<scratchpad>\autorouting\fr_drc.json`
- **Source-level checks on GitLab: action names across KiCad tags, Specctra import/export behaviour, first-run wizard conditions**
  - Result: All four tags define the 4 actions; FromSESSION removes non-locked tracks and applies SES placement; wizard shows when kicad_common.json missing, global lib tables invalid, or privacy prompts not acknowledged
  - Command/code:

```
curl -sL https://gitlab.com/kicad/code/kicad/-/raw/<tag>/pcbnew/tools/pcb_actions.cpp | grep -c -E 'InteractiveRouter.Autoroute|InteractiveRouter.RouteSelected|Autoplacer.autoplaceSelected'   # tags 7.0.6 8.0.1 9.0.5 10.0.0
curl -sL https://gitlab.com/kicad/code/kicad/-/raw/10.0.0/pcbnew/specctra_import_export/specctra_import.cpp | sed -n 363,400p
curl -sL https://gitlab.com/kicad/code/kicad/-/raw/10.0.0/common/startwizard/startwizard_provider_{settings,libraries,privacy}.cpp | grep -A6 NeedsUserInput
```

  - Artifact: `<scratchpad>\autorouting\specctra_import.cpp`
