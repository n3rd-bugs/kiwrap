# schematic-authoring: programmatic creation/editing of KiCad .kicad_sch (7/8/9/10) without an eeschema API

## Findings

- **[high, VERIFIED LOCALLY]** Exact schematic file-format versions per KiCad release: 7.0.6 writes (version 20230121); 8.0.1 writes 20231120; 9.0.5 writes 20250114; 10.0.0 writes 20260306. The demos bundled with 10.0 (pic_programmer) carry 20260101 because they were saved by a 10.0 pre-release; kicad-cli 10.0.0 `sch upgrade --force` rewrites any file to (version 20260306) (generator_version "10.0"). Symbol-library versions: 7=20220914, 8=20231120, 9=20241209, 10=20251024.
  - Evidence: Local: `grep -ho '(version [0-9]*)'` over share/kicad/demos of each install; upgrade of my generated file produced `(version 20260306)`. Source: sch_file_versions.h on the 10.0 branch: `#define SEXPR_SCHEMATIC_FILE_VERSION 20260306 // Variant in_bom semantics corrected` and `#define SEXPR_SYMBOL_LIB_FILE_VERSION 20251024`, with dated history (20250114 Full paths for text variable cross references; 20250227 local power symbols; 20250318 '~' no longer means empty text; 20250827 custom body styles; 20250901 stacked pin notation; 20250922 schematic variants; 20251012 flat schematic hierarchy; 20251028 do_not_autoplace/show_name formatting; 20260101 PCB variants).
  - Sources: https://gitlab.com/kicad/code/kicad/-/raw/10.0/eeschema/sch_file_versions.h
- **[high, VERIFIED LOCALLY]** Plain-Python (stdlib only) generation of the LED+R+Conn_01x02+GND project yields ZERO ERC violations and a correct netlist on KiCad 10 (nets: VIN={J1.1,R1.1}, /LED_A={R1.2,D1.2(A)}, GND={D1.1(K),J1.2}; footprints present in netlist). The same generator with per-version 'dialects' also gives zero ERC on 9.0.5 (v9 dialect) and 8.0.1 (v8 dialect); 7.0.6 has no ERC command but exports the correct netlist and SVG from the v7 dialect.
  - Evidence: gen_plain.py / gen_dialect.py + validate.py; validate output for dialect 10 on kicad-cli 10: erc_violations [], nets correct, svg_rc 0. Full matrix in dialects/matrix.txt. Rendered PNG (led_d10_crop.png) shows correct topology.
- **[high, VERIFIED LOCALLY]** Format compatibility matrix: a file written in dialect N is loadable by kicad-cli N and every NEWER version, never by an older one (older versions fail with 'Failed to load schematic', exit code 3, because the parser throws FUTURE_FORMAT_ERROR when (version) > SEXPR_SCHEMATIC_FILE_VERSION). Reading an older dialect on a newer KiCad only produces 'lib_symbol_mismatch' / 'lib_symbol_issues' WARNINGS (embedded symbol text differs from the installed library), never errors.
  - Evidence: dialects/matrix.txt: d7 -> 7/8/9/10 OK (warnings on 8/9/10), d8 -> 8/9/10 OK, d9 -> 9/10 OK (zero warnings), d10 -> 10 only; 9.0 on a 20260101 file: 'Failed to load schematic'. Parser: `if( m_requiredVersion > SEXPR_SCHEMATIC_FILE_VERSION ) throw FUTURE_FORMAT_ERROR`.
  - Sources: https://gitlab.com/kicad/code/kicad/-/raw/10.0/eeschema/sch_io/kicad_sexpr/sch_io_kicad_sexpr_parser.cpp
- **[high, VERIFIED LOCALLY]** Syntax drift that a version-agnostic writer must handle (verified by reading each version's own library/demo files and by kicad-cli 10 upgrade diffs): v7: unquoted `(generator eeschema)`, unquoted uuids, `(pin_numbers hide)`, bare `hide` inside effects, `ki_description` property, no exclude_from_sim, pin name "~" = empty. v8: quoted strings, `(generator_version "8.0")`, `(exclude_from_sim no)` on symbols, `(hide yes)` INSIDE effects, `Description` property. v9: `(pin_numbers (hide yes))`, `(embedded_fonts no)` at end of file and inside each lib symbol. v10 (20260306): `(hide yes)` moves to PROPERTY level (before effects), every property gets `(show_name no) (do_not_autoplace no)`, symbols get `(body_style 1)` and `(in_pos_files yes)`, lib symbols get `(duplicate_pin_numbers_are_jumpers no)`, `(power)` becomes `(power global)`/`(power local)`, Datasheet "~" must be written as "" (since 20250318 '~' is literal), global_label's Intersheetrefs property also gets do_not_autoplace/show_name.
  - Evidence: Condensed dumps of Device:R from 7.0/8.0/9.0/10.0 Device.kicad_sym; sexpr_diff.py of led_d9.kicad_sch vs its kicad-cli-10 upgraded copy lists exactly these added/changed tokens; my dialect-10 file round-trips through `sch upgrade` with the only semantic change being (project "led_d10") -> (project "").
  - Sources: https://gitlab.com/kicad/code/kicad/-/raw/10.0/eeschema/sch_file_versions.h
- **[high, VERIFIED LOCALLY]** lib_symbols embedding is MANDATORY and is the source of truth: if a symbol's definition is missing from lib_symbols, kicad-cli 10 does NOT fall back to the sym-lib-table; the symbol loads as a pin-less placeholder, disappears from the netlist components list and all its wires dangle (ERC pin_not_connected/unconnected_wire_endpoint). A completely absent (lib_symbols ...) block or an empty `(lib_symbols)` makes the file fail to load. Embedding rule: copy the `(symbol "R" ...)` block verbatim from the installed version's .kicad_sym and rename ONLY the top-level name to "Device:R"; the unit/body sub-symbols keep their unprefixed names ("R_0_1", "R_1_1"). Pins may live in unit 0 sub-symbols (e.g. PWR_FLAG's only pin is in "PWR_FLAG_0_0") so a pin lookup must scan sub-symbols with unit 0 or the instance unit and body style 0 or the instance body style.
  - Evidence: minimal_variants experiment: removing Device:R from lib_symbols -> comps ['D1','J1'] and net 'Net-(R1-Pad??)'; no lib_symbols token -> 'Failed to load schematic'; `(lib_symbols)` empty -> load fails. pic_programmer demo shows `(symbol "pic_programmer:74LS125" ... (symbol "74LS125_0_0"`.
- **[high, VERIFIED LOCALLY]** Derived library symbols (`(extends "Parent")`, e.g. Transistor_BJT:2N2219 extends Q_NPN_EBC; 258 such symbols in Transistor_BJT.kicad_sym, 1319 in Regulator_Linear.kicad_sym) must be FLATTENED before embedding. Copying the derived block raw (with or without also embedding the parent, prefixed or not) loads without error but the symbol has NO pins in the netlist. KiCad itself never writes `extends` into lib_symbols (0 occurrences across all bundled demo schematics).
  - Evidence: exp_extends.py: a_derived_only, b_derived_plus_parent, b2_derived_extends_prefixed -> Q1 nodes []; c_flattened_parent_body (parent graphics/pins renamed to the derived name) -> Q1 nodes ['1','2','3'].
- **[high, VERIFIED LOCALLY]** Symbol placement transform (library coords are Y-up, sheet coords Y-down). For a symbol at (X,Y) and a library pin at (lx,ly): rot 0 -> (X+lx, Y-ly); rot 90 -> (X-ly, Y-lx); rot 180 -> (X-lx, Y+ly); rot 270 -> (X+ly, Y+lx); then `(mirror x)` negates the resulting Y offset and `(mirror y)` negates the X offset. Verified for all 12 rotation/mirror combinations against KiCad 10's own connectivity. Consequence: Device:LED placed with `(at x y 90)` has its anode (pin 2) ABOVE the centre and cathode below.
  - Evidence: exp_transform.py places Conn_01x02 (asymmetric pins) in all 12 orientations with labelled stub wires at the predicted pin positions; kicad-cli netlist shows every predicted pin connected (12/12 'OK'). Derived from SCH_SYMBOL::SetOrientation TRANSFORM math (rot0 = (1,0,0,-1)).
- **[high, VERIFIED LOCALLY]** Connectivity rules verified with kicad-cli 10: (1) a symbol pin connects ONLY to a wire ENDPOINT; a pin landing mid-segment is 'pin_not_connected' (so wires must be split at every pin, and a PWR_FLAG placed mid-wire does not count); (2) local and global labels DO connect anywhere along a wire segment (label anchor at the segment midpoint named the net); (3) two wires meeting end-to-end connect without a `(junction)`; (4) every GND/VCC power-input net needs a power:PWR_FLAG (or a power_out pin) or ERC reports error 'power_pin_not_driven'; (5) all pin/wire endpoints must lie on the 1.27 mm (50 mil) connection grid or ERC warns 'endpoint_off_grid'; (6) two GND symbols anywhere merge into one global net 'GND'; hierarchical-label nets are named '/sheetname/NAME', local labels '/NAME' on the root.
  - Evidence: plain_mid (PWR_FLAG mid-wire) -> pin_not_connected + power_pin_not_driven; plain_v10 endpoint variant -> zero violations; label at wire midpoint produced net '/LED_A'; skip clone placed at (20,-3.81) -> endpoint_off_grid; hier_out netlist names '/led_sub/VIN'.
  - Sources: https://docs.kicad.org/10.0/en/eeschema/eeschema.html
- **[high, VERIFIED LOCALLY]** Instances block semantics: `(instances (project "<name>" (path "/<ROOT_FILE_UUID>[/<SHEET_SYMBOL_UUID>...]" (reference "R1") (unit 1))))`. The path uses the ROOT schematic file uuid followed by the uuid of each `(sheet ...)` SYMBOL (not the child file's uuid). When present, the reference in the instances block WINS over the Reference property (property RX + instance R1 -> netlist says R1); when absent, KiCad falls back to the Reference property. kicad-cli tolerates a wrong project name, a wrong path, missing pin uuids, missing symbol uuids, missing sheet_instances, missing generator/paper/embedded_fonts and a missing .kicad_pro (all still gave correct nets); only `(version N)` and lib_symbols are hard requirements. kicad-cli standalone commands rewrite `(project "name")` to `(project "")` on save/upgrade.
  - Evidence: minimal_variants experiments (no_instances, no_pin_uuids, no_sheet_instances, no_project_file, no_symbol_uuids, wrong_project_name, wrong_instance_path, ref_prop_vs_instance, minimal_header all OK; no_version fails); kicad-skip clone experiment (instances reference stale -> two 'R1' comps in netlist); hier_out child instances path '/<root>/<sheet-uuid>' -> netlist sheetpath tstamps '/2047be3c-.../'.
- **[high, VERIFIED LOCALLY]** Hierarchical sheets: root file holds `(sheet (at x y) (size w h) ... (uuid SHEET_UUID) (property "Sheetname" ..) (property "Sheetfile" "child.kicad_sch") (pin "NAME" input|output|bidirectional|tri_state|passive (at x y rot) (uuid ..) (effects ..)) (instances (project "p" (path "/ROOT_UUID" (page "2")))))`; the child file has its own (uuid), `(hierarchical_label "NAME" (shape input) ...)` items and symbol instances whose path is /ROOT_UUID/SHEET_UUID; only the root carries `(sheet_instances (path "/" (page "1")))`. A 2-sheet project generated this way passed ERC with zero violations and exported per-sheet SVGs. KiCad 10 additionally supports FLAT 'top-level sheets' (siblings of the root, connected only via global labels/power symbols) recorded in .kicad_pro `schematic.top_level_sheets: [{filename,name,uuid}]` and a top-level `(page ..)` token 'only saved for top-level sniffing'.
  - Evidence: gen_hier.py -> hier_out/ledhier: erc [], nets {'/led_sub/LED_A','/led_sub/VIN','GND'}, svg files ledhier.svg + ledhier-led_sub.svg. pic_programmer.kicad_pro (saved by 10.0) contains schematic.top_level_sheets; docs 10.0 'Adding top-level sheets to a design'.
  - Sources: https://docs.kicad.org/10.0/en/eeschema/eeschema.html, https://gitlab.com/kicad/code/kicad/-/raw/10.0/eeschema/sch_io/kicad_sexpr/sch_io_kicad_sexpr_parser.cpp
- **[high, VERIFIED LOCALLY]** .kicad_pro linkage: the root sheet is simply `<project>.kicad_sch` next to `<project>.kicad_pro` (no explicit reference is required); the .kicad_pro `sheets` array lists [uuid, name] pairs (root file uuid first) and, in KiCad 10, `schematic.top_level_sheets`. A minimal `{"meta": {"filename": "x.kicad_pro", "version": 1}}` is enough for kicad-cli; kicad-cli erc/netlist runs create a `<project>.kicad_prl` next to it. Property text angle in a symbol instance is RELATIVE to the symbol orientation (a 90-degree-rotated symbol needs property angle 90 to render horizontally) while the property position is absolute.
  - Evidence: pic_programmer.kicad_pro inspection (meta.version 3, sheets, schematic.top_level_sheets); minimal .kicad_pro used for all dialect tests; exp_fieldrot render PNG.
- **[high, VERIFIED LOCALLY]** kicad-cli capability drift: 7.0.6 `sch` has only `export {dxf,hpgl,netlist,pdf,ps,python-bom,svg}` (no erc, no upgrade); 8.0.1 and 9.0.5 add `sch erc` and `export bom`; 10.0.0 adds `sch upgrade [--force]` and `--variant`. kicadsexpr netlist differences: 10.0 writes `(pinfunction "A_2")` (name_number) where 7/8/9 write `(pinfunction "A")`, and adds empty `(groups)` and `(variants)` sections; export version stays "E". ERC `--format json` positions are 1/100 of the true mm value in BOTH 9.0.5 and 10.0.0 (x 0.635 for a pin at 63.50 mm while the text report says '@(63.50 mm, 73.66 mm)') despite coordinate_units 'mm'. ERC exit codes: 0 clean, 5 violations (with --exit-code-violations), 3 load failure.
  - Evidence: `kicad-cli sch --help` on each install; netlist_*.net diffs in dialects/d9/led_d9; erc_units/erc_9.0.json and erc_10.0.json vs erc.rpt text.
- **[high, VERIFIED LOCALLY]** kicad-cli gives NO diagnostic for malformed input ('Failed to load schematic', rc 3, no line number) because EESCHEMA_HELPERS::LoadSchematic wraps the parse in `catch( ... ) { return nullptr; }`; the GUI would show the parse error with line/offset. `kicad-cli sch upgrade --force` writes IN PLACE and, on the circuit-synth output (duplicate sheet_instances blocks, hierarchical labels on the root), SEGFAULTED (rc 139 / 0xC0000005) leaving the target file truncated to 0 bytes.
  - Evidence: broken/broken.kicad_sch test; csynth_out/ledcs/crashtest.kicad_sch = 0 bytes after crash; eeschema_helpers.cpp 10.0 lines 197-200.
  - Sources: https://gitlab.com/kicad/code/kicad/-/raw/10.0/eeschema/eeschema_helpers.cpp
- **[high, VERIFIED LOCALLY]** KiCad 10.0 IPC API has NO usable schematic support: the 10.0 branch's schematic_commands.proto defines zero messages and schematic_types.proto only Line/Text/LocalLabel/GlobalLabel/HierarchicalLabel/DirectiveLabel; API_HANDLER_SCH registers only GetOpenDocuments (deleteItemsInternal and getItemFromDocument are 'TODO'). Live test against eeschema 10.0.0 with the API enabled: GetOpenDocuments(DOCTYPE_SCHEMATIC) works (returns board_filename "ledhier.kicad_sch"), GetItems -> 'no handler available', Ping/GetVersion fail in standalone eeschema (common handler is only registered by kicad.exe), BeginCommit -> request timed out and the server stopped listening, CreateItems(Line) -> timeout and the eeschema PROCESS CRASHED. KiCad master (= 11) has the full API: GetItems/CreateItems/UpdateItems for SchematicSymbol, Junction, SheetSymbol, etc., PlaceSymbolFromLibrary, GetSchematicNetlist, GetSchematicHierarchy, SaveDocument/RevertDocument, RunSchematicJobExport{Svg,Pdf,Netlist,BOM,...}, variants, cross-probe. kipy 0.8.0's `Schematic` class is explicitly marked '.. versionadded:: (KiCad 11)' and KiCad.get_schematic() does not exist.
  - Evidence: protos/*.proto.10 vs .master (grep ^message); api_handler_sch.cpp.10 (303 lines, one registerHandler) vs master (2986 lines, ~55 handlers); live kipy/pynng probes (probe outputs above); kipy source docstring; devlist thread (Jon Evans, May 2025: schematic API not started 'until we can consider it stable').
  - Sources: https://gitlab.com/kicad/code/kicad/-/raw/10.0/api/proto/schematic/schematic_commands.proto, https://gitlab.com/kicad/code/kicad/-/raw/master/api/proto/schematic/schematic_types.proto, https://gitlab.com/kicad/code/kicad/-/raw/10.0/eeschema/api/api_handler_sch.cpp, https://gitlab.com/kicad/code/kicad/-/raw/master/eeschema/api/api_handler_sch.cpp, https://groups.google.com/a/kicad.org/g/devlist/c/ViU9P2nvCEA, https://www.kicad.org/blog/2026/03/Version-10.0.0-Released/
- **[high, VERIFIED LOCALLY]** kiutils 1.4.8 (last release Feb 2024, KiCad 7-era dataclass model) parses KiCad 7/8/9/10 .kicad_sym and .kicad_sch files without error but its round trip is LOSSY: on the 10.0 pic_programmer demo it drops exclude_from_sim (135), in_pos_files (126), body_style (105), do_not_autoplace (641), duplicate_pin_numbers_are_jumpers (21), embedded_fonts (22), generator_version and turns (power global) into (power). It writes v7-style syntax (unquoted uuids, `hide` flag) under whatever version header was loaded; KiCad 10 is tolerant enough to load that output (ERC 0, netlist correct for my simple file), but it is unsafe for editing real 8+/9/10 designs.
  - Evidence: kiutils_rt/ token-count table; validate on kiutils_rt/a -> erc [] nets correct; kiutils CHANGELOG (v1.4.8 2024-02-03, 'initial KiCad v7 support').
  - Sources: https://github.com/mvnmgrx/kiutils/blob/master/CHANGELOG.md
- **[high, VERIFIED LOCALLY]** kicad-sch-api 0.5.6 (reports __version__ 0.5.5; pulls in fastmcp/mcp and ships an MCP server `kicad-sch-mcp`; wraps kicad-cli for run_erc/export_netlist/export_svg; README claims KiCad 7/8 output) generated the LED circuit in v9 format (20250114) but with WRONG connectivity: its pin transform (core/geometry.py apply_transformation) is wrong for rotation 90/270 and for every mirror case (10 of 12 orientations disagree with KiCad), which put the LED anode/cathode swapped, shorted D1 and produced net GND={D1.1,D1.2,J1.2,R1.2}. add_global_label() returns a uuid but writes nothing (parser/serializer have no global_label branch), and loading+saving my KiCad 10 file dropped the global_label (1->0) plus in_pos_files, body_style, duplicate_pin_numbers_are_jumpers and (power global).
  - Evidence: gen_ksa.py output + validate (erc label_multiple_wires/multiple_net_names, nets wrong); apply_transformation table vs exp_transform truth; ksa_out/rt_d10 token counts.
  - Sources: https://github.com/circuit-synth/kicad-sch-api
- **[high, VERIFIED LOCALLY]** kicad-skip 0.2.5 is a generic s-expression read-modify-write layer: round trip of my KiCad 10 file is semantically LOSSLESS (0 diffs), edits (value change, symbol clone+move, wire clone) load in KiCad 10. Gotchas: it cannot create symbols from scratch (clone only); clone() assigns a new symbol uuid but keeps the pin uuids and the `(instances ... (reference))` of the original, so after `property.Reference.value='R2'` the netlist still reports 'R1' (duplicate) until `instances.project.path.reference.value` is also set; `move(x,y)` sets an absolute position; it prints 'Passed key -- can't parsy' noise on 10.0 files; nothing snaps to grid.
  - Evidence: skip_out/skip_rt vs original: only-in-A 0 / only-in-B 0; skip_mod netlist showed two R1 refs; skip_mod2 (instances fixed) shows R2.
  - Sources: https://github.com/psychogenic/kicad-skip
- **[high, VERIFIED LOCALLY]** sexpdata 1.0.2 is a viable minimal foundation: loads the 13.9k-line KiCad 10 demo in 0.03 s, dumps in 0.04 s, round trip of my file is lossless apart from the intended edit, and KiCad 10 loads the single-line output (ERC 0, netlist correct). It carries no schema, so all KiCad semantics (transform, connectivity, dialects) must be implemented on top.
  - Evidence: sexpdata_out/rt.kicad_sch validate: erc [] nets correct; sexpr_diff shows only the Value change.
- **[high, VERIFIED LOCALLY]** skidl 2.3.0 (tool KICAD10, needs KICAD10_SYMBOL_DIR) generates a correct netlist and can `generate_schematic()`, but the schematic it wrote (header version 20230409, generator "skidl", its own re-serialised lib symbols) had J1 pin 1 unconnected (VIN net contained only R1.1), 3 lib_symbol_mismatch warnings and cluttered auto-placement; not usable as-is for a KiCad-native schematic. circuit-synth 0.1.0 `generate_kicad_project()` wrote a v9-header file with hierarchical labels on the root sheet (5 ERC errors), wrong nets (LED_A merged with VIN, R1.2 dropped), two sheet_instances blocks, and crashed kicad-cli upgrade.
  - Evidence: skidl_out/ validate + rendered skidl_crop.png; csynth_out/ledcs validate output.
- **[high, VERIFIED LOCALLY]** GUI/IPC operational facts: KiCad 10 first run shows a modal 'KiCad Setup' wizard unless kicad_common.json has `system.first_run_shown: true`; the API is enabled via `api.enable_server: true` in kicad_common.json; the server listens on ipc://<TEMP>/kicad/api.sock (Windows named pipe `\\.\pipe\C:\Users\...\Temp\kicad\api.sock`) and falls back to api-<pid>.sock if the path exists; a killed/crashed eeschema leaves `~<name>.kicad_sch.lck` / `~<name>.kicad_pro.lck` files; KiCad launched without KICAD_CONFIG_HOME writes `system.working_dir` into the user's %APPDATA%/kicad/10.0/kicad_common.json (observed: another agent's scratchpad path is now in the user's config).
  - Evidence: Binary string search (system.first_run_shown), api_server.cpp 10.0, PowerShell pipe listing, lock files in hier_out after crash, user's kicad_common.json content.
  - Sources: https://gitlab.com/kicad/code/kicad/-/raw/10.0/common/api/api_server.cpp

## Capabilities

- Create complete, ERC-clean KiCad projects from scratch with ~250 lines of stdlib Python: .kicad_pro + .kicad_sch with embedded lib_symbols copied verbatim from the installed KiCad's own .kicad_sym files, symbol instances (lib_id/at/rot/mirror, properties incl. Footprint, pin uuids, instances block), wires, junctions, local/global/hierarchical labels, power symbols, PWR_FLAG, hierarchical sub-sheets and sheet pins (gen_plain.py, gen_dialect.py, gen_hier.py).
- Emit any of four format dialects (7: 20230121, 8: 20231120, 9: 20250114, 10: 20260306) and read them back with the matching or newer kicad-cli; a file written in the oldest dialect the user needs is forward-compatible with every newer KiCad.
- Compute exact pin sheet-positions for any rotation/mirror from the library pin coordinates (verified transform), which is all that is needed to route wires programmatically and to place labels/PWR_FLAGs correctly.
- Validate headlessly with kicad-cli (8+): `sch erc --format json --exit-code-violations`, `sch export netlist --format kicadsexpr|kicadxml|spice|...`, `sch export svg|pdf|bom`, and (10 only) `sch upgrade --force` to normalise to the current format; parse the netlist with a small s-expr parser (validate.py) to assert connectivity.
- Edit existing schematics losslessly with a generic s-expression layer (sexpdata or kicad-skip style): change values/footprints, clone symbols, add wires/labels, while preserving unknown tokens across KiCad versions.
- Render generated schematics to PNG for visual/LLM review via `kicad-cli sch export pdf` + PyMuPDF (pymupdf), since kicad-cli sch has no PNG export.
- Read KiCad's own symbol libraries (Device, Connector_Generic, power, ...) for pin numbers/names/types/positions, keywords and footprint filters directly from the s-expression text.

## Limitations

- There is no schematic API in KiCad 7/8/9/10: no SWIG for eeschema and the 10.0 IPC API only answers GetOpenDocuments for schematics (CreateItems crashed eeschema 10.0.0 in my test). Everything must be file-based until KiCad 11; kipy's Schematic class targets KiCad 11 only.
- kicad-cli reports parse failures as 'Failed to load schematic' with no reason or line number (exception swallowed by catch(...)); precise diagnostics require the GUI or a patched source build.
- Older kicad-cli versions cannot read newer files at all; newer ones read older files but emit lib_symbol_mismatch warnings whenever the embedded symbol text differs from the installed library.
- Every symbol used must be embedded (flattened, prefixed 'Lib:Name', sub-units unprefixed); derived `(extends)` symbols copied raw silently lose all pins; missing lib_symbols entries silently drop the component from the netlist.
- Pins connect only at wire endpoints and only on the 1.27 mm grid; the generator must split wires at every pin/PWR_FLAG position and snap all coordinates; kicad-cli has no auto-junction/cleanup step.
- `kicad-cli sch upgrade` edits in place and can crash on malformed input, truncating the file to 0 bytes; always run it on a copy. Standalone kicad-cli saves write (project "") in instances.
- ERC JSON positions are scaled by 1/100 in 9.0.5 and 10.0.0; the text report is correct.
- Third-party libraries: kiutils (KiCad-7 model, lossy on 8/9/10, unmaintained since Feb 2024), kicad-sch-api (wrong rotation/mirror pin math, drops global labels and several 10.0 tokens), skidl/circuit-synth (auto-placed schematics with broken connectivity) are not safe as the core of the wrapper; only sexpdata / kicad-skip-style generic parsing proved lossless.
- kicad-cli 7.0.6 has no ERC and no BOM export; 8/9 have no `sch upgrade`; `--variant` exists only in 10.
- No headless way to run the GUI's 'annotate', 'autoplace fields', or 'update PCB from schematic' from kicad-cli; annotation (unique references + instances block) is the generator's responsibility.
- GUI-side tests need an isolated KICAD_CONFIG_HOME with kicad_common.json {api.enable_server:true, system.first_run_shown:true} plus copied sym-lib-table/fp-lib-table; crashed sessions leave .lck files that must be deleted.

## Version notes

Schematic file versions: 7.0.6=20230121 (symbol lib 20220914), 8.0.1=20231120 (lib 20231120), 9.0.5=20250114 (lib 20241209), 10.0.0=20260306 (lib 20251024; demos shipped with 20260101). Newer KiCad reads all older versions (warnings only); older KiCad rejects newer versions (FUTURE_FORMAT_ERROR -> 'Failed to load schematic'). Writer dialect differences: v7 unquoted generator/uuids, `(pin_numbers hide)`, bare `hide` in effects, ki_description; v8 quoted, generator_version, exclude_from_sim, `(hide yes)` inside effects, Description; v9 `(pin_numbers (hide yes))`, embedded_fonts; v10 hide/show_name/do_not_autoplace at property level, body_style, in_pos_files, duplicate_pin_numbers_are_jumpers, (power global|local), Datasheet '' instead of '~' (since 20250318 '~' is literal), top_level_sheets in .kicad_pro, variants, jumper groups, groups in schematics. kicad-cli: 7 export-only (no erc); 8/9 erc+bom; 10 adds upgrade and --variant; 10 netlist pinfunction is 'name_number' and adds (groups)/(variants). IPC API: 10.0 schematic = GetOpenDocuments only (CreateItems crashes), master/11 = full schematic CRUD + PlaceSymbolFromLibrary + netlist/hierarchy + jobs; kipy 0.8.0 Schematic marked KiCad 11. Recommended wrapper strategy: detect installed version (kicad-cli version), copy lib symbols from THAT version's share/kicad/symbols, write that version's dialect (or the oldest dialect >= 8 that the user needs for forward compatibility), and validate with that version's kicad-cli; keep a per-version token table (this report) and a golden-file test that round-trips through `sch upgrade` on 10 and compares semantically (sexpr_diff.py).

## Recommendations

- Build the schematic layer as an in-house s-expression writer/editor (stdlib or sexpdata for parsing) with an explicit dialect table per KiCad major version; do not build on kiutils, kicad-sch-api, skidl or circuit-synth (each failed connectivity or fidelity tests here). kicad-skip-style lossless editing is the model for modifying user files.
- Always embed symbols by copying from the installed KiCad's own libraries (or the project's libs), flattening `extends` chains, prefixing only the top-level name; keep a pin table (number -> lib xy, unit, body style) and use the verified transform to place wires exactly on pin endpoints, splitting wires at every pin and snapping to 1.27 mm.
- Make the generator emit: unique references, an `instances` block per symbol with path /ROOT_UUID[/SHEET_UUID], PWR_FLAG on every power net, `(sheet_instances (path "/" (page "1")))` in the root only, Datasheet '' for v10, and a minimal .kicad_pro; then gate every write with `kicad-cli sch erc --format json --exit-code-violations` + `export netlist` and assert the expected nets (validate.py pattern).
- Use kicad-cli (8+) as the sole validation/export backend; for 10 run `sch upgrade --force` on a COPY as a format-drift canary and diff semantically (sexpr_diff.py) so drift in future releases is detected automatically.
- For the 'in-KiCad plugin <-> VS Code' question, adopt the channel only for what it uniquely gives: on KiCad 10 that is nothing for schematics (API is GetOpenDocuments-only and crashes on CreateItems); on KiCad 11/master the IPC schematic API becomes the natural live channel (place symbols, get netlist, save/revert, jobs) and should be planned as an optional backend behind the same wrapper interface, with the file-based path remaining the default for 7-10.
- A KiCad source build is NOT required for schematic authoring on installed 7-10 binaries; it becomes worthwhile only (a) to get parse-error messages that kicad-cli swallows (one-line patch in EESCHEMA_HELPERS::LoadSchematic), (b) to run master/11 to prototype the IPC schematic API, or (c) to debug crashes like `sch upgrade`/CreateItems. Prefer a KiCad nightly (11) install over a build for (b).
- Parse ERC JSON positions with a x100 correction on 9.0.5/10.0.0 (or use the text report), and do not rely on netlist `pinfunction` text (format changed in 10).
- When driving the GUI from tests/CI, set KICAD_CONFIG_HOME to a scratch dir seeded with kicad_common.json {api.enable_server, system.first_run_shown} and copied lib tables, launch one process at a time, and clean up ~*.lck files after abnormal exits.

## Experiments run

- **Survey schematic/symbol format versions across the four installed KiCad versions (demos + bundled libraries)**
  - Result: 7.0: 20220404/20230121; 8.0: up to 20231120; 9.0: up to 20250114; 10.0 demos: mostly 20250114 plus 20250610/20250901/20260101 (pic_programmer saved by '10.0' with 20260101). Symbol libs: 7=20220914, 8=20231120, 9=20241209, 10=20251024.
  - Command/code:

```
cd "/c/Program Files/KiCad" && for v in 7.0 8.0 9.0 10.0; do find "$v/share/kicad/demos" -name "*.kicad_sch" -exec grep -ho "(version [0-9]*)" {} \; | sort | uniq -c; done; grep -ho "(version [0-9]*)" 10.0/share/kicad/symbols/Device.kicad_sym
```

- **Plain-Python generator (no third-party libs): LED + R + Conn_01x02 + 2x GND + PWR_FLAG + local and global labels, footprints assigned, lib symbols extracted verbatim from KiCad 10 libraries**
  - Result: erc_rc 0, erc_violations [], nets {'/LED_A': [D1.2, R1.2], 'GND': [D1.1, J1.2], 'VIN': [J1.1, R1.1]}, footprints present, svg exported, upgrade changed file (version 20260101->20260306, reordering, (project "ledplain")->(project "")).
  - Command/code:

```
cd <scratch>/schematic-authoring && python gen_plain.py "$PWD/plain_v10/ledplain" 20260101 10.0 endpoint && python validate.py plain_v10/ledplain 10.0   # validate.py runs: kicad-cli sch erc --severity-all --exit-code-violations --format json; sch export netlist --format kicadsexpr; sch export svg; copy + sch upgrade --force; difflib
```

  - Artifact: `<scratchpad>/schematic-authoring/gen_plain.py (+ validate.py, plain_v10/ledplain/)`
- **PWR_FLAG pin placed mid-wire vs at a wire endpoint (connectivity rule for pins)**
  - Result: midwire: ERC errors power_pin_not_driven + pin_not_connected (pin does not connect mid-segment); endpoint: zero violations. Labels placed mid-segment DID connect (net /LED_A).
  - Command/code:

```
python gen_plain.py "$PWD/plain_mid/ledmid" 20260101 10.0 midwire && python validate.py plain_mid/ledmid 10.0 --no-upgrade
```

  - Artifact: `<scratch>/schematic-authoring/plain_mid/ledmid/`
- **Per-version dialect generator x kicad-cli compatibility matrix (dialects 7/8/9/10 read by kicad-cli 7.0.6/8.0.1/9.0.5/10.0.0)**
  - Result: d7: readable by 7/8/9/10 (warnings on newer); d8: 8/9/10 (8 zero warnings); d9: 9/10 zero warnings; d10: 10 only. Older versions: 'Failed to load schematic' rc 3. kicad-cli 7 has no `sch erc`.
  - Command/code:

```
for d in 7 8 9 10; do python gen_dialect.py "$PWD/dialects/d$d/led_d$d" $d; done; for d in 7 8 9 10; do for v in 7.0 8.0 9.0 10.0; do python validate.py dialects/d$d/led_d$d $v --no-upgrade; done; done | tee dialects/matrix.txt
```

  - Artifact: `<scratch>/schematic-authoring/gen_dialect.py, dialects/matrix.txt`
- **Semantic diff of generated files vs kicad-cli 10 `sch upgrade` output to measure format drift**
  - Result: d10: only (project name) changes; d9->10: version/generator_version, +duplicate_pin_numbers_are_jumpers, +in_pos_files, +body_style, (power)->(power global), Datasheet '~'->'', property hide/show_name/do_not_autoplace restructuring.
  - Command/code:

```
python sexpr_diff.py dialects/d10/led_d10/led_d10.kicad_sch dialects/d10/led_d10/led_d10.upgraded_10.0.kicad_sch; python sexpr_diff.py dialects/d9/led_d9/led_d9.kicad_sch dialects/d9/led_d9/led_d9.upgraded_10.0.kicad_sch
```

  - Artifact: `<scratch>/schematic-authoring/sexpr_diff.py`
- **Empirical verification of the symbol placement transform for all 12 rotation/mirror combinations**
  - Result: 12/12 predicted pin positions connected in the KiCad 10 netlist (rot0 (X+lx,Y-ly); rot90 (X-ly,Y-lx); rot180 (X-lx,Y+ly); rot270 (X+ly,Y+lx); mirror x flips Y offset, mirror y flips X offset).
  - Command/code:

```
python exp_transform.py exp_transform_out
```

  - Artifact: `<scratch>/schematic-authoring/exp_transform.py, exp_transform_out/results.json`
- **Property text angle vs symbol rotation**
  - Result: Property angle is relative to symbol orientation: angle 90 on a 90-degree-rotated LED renders horizontal; position is absolute.
  - Command/code:

```
python (edit D1 property angles to 90 in a copy) ; kicad-cli sch export pdf ; pymupdf crop -> exp_fieldrot/fr_crop.png
```

  - Artifact: `<scratch>/schematic-authoring/exp_fieldrot/fr_crop.png`
- **Which parts of the file KiCad 10 actually requires (remove instances, pin uuids, sheet_instances, .kicad_pro, symbol uuids, wrong project name/path, ref-vs-instance mismatch, lib_symbols, header tokens, version)**
  - Result: All optional except (version N) and lib_symbols; instances reference overrides Reference property; missing one lib symbol -> component vanishes from netlist (Net-(R1-Pad??)).
  - Command/code:

```
python (minimal_variants script, see transcript) -> kicad-cli sch export netlist + sch erc per variant
```

  - Artifact: `<scratch>/schematic-authoring/minimal_variants/`
- **Derived (extends) symbol embedding test with Transistor_BJT:2N2219**
  - Result: Raw derived (with/without parent, prefixed or not): Q1 has 0 pins in netlist; flattened parent body under the derived name: pins 1,2,3 connected.
  - Command/code:

```
python exp_extends.py exp_extends_out
```

  - Artifact: `<scratch>/schematic-authoring/exp_extends.py`
- **Hierarchical two-sheet project (root with sheet symbol + child with hierarchical labels)**
  - Result: ERC [], nets {'/led_sub/LED_A','/led_sub/VIN','GND'}, two SVG pages, child instances path /ROOT/SHEET_UUID reflected in netlist sheetpath tstamps.
  - Command/code:

```
python gen_hier.py "$PWD/hier_out/ledhier" && python validate.py hier_out/ledhier 10.0
```

  - Artifact: `<scratch>/schematic-authoring/gen_hier.py, hier_out/ledhier/`
- **kiutils 1.4.8 load + round-trip of KiCad 7/8/9/10 files and token loss count**
  - Result: Loads all versions; drops exclude_from_sim/in_pos_files/body_style/do_not_autoplace/duplicate_pin_numbers_are_jumpers/embedded_fonts/generator_version, (power global)->(power); KiCad 10 still loads output (ERC 0).
  - Command/code:

```
uv venv venv; uv pip install kiutils kicad-sch-api kicad-skip sexpdata skidl kicad-python pymupdf circuit-synth; venv/Scripts/python.exe (Schematic.from_file(...).to_file(...)); token counts before/after; validate kiutils_rt/a 10.0
```

  - Artifact: `<scratch>/schematic-authoring/kiutils_rt/`
- **kicad-sch-api 0.5.6 generation of the same circuit + transform check + round trip**
  - Result: Wrong netlist (GND={D1.1,D1.2,J1.2,R1.2}), ERC label_multiple_wires/multiple_net_names; transform wrong in 10/12 cases; global label not written and dropped on round trip; writes version 20250114.
  - Command/code:

```
venv/Scripts/python.exe gen_ksa.py "$PWD/ksa_out/ledksa" && python validate.py ksa_out/ledksa 10.0; apply_transformation((-5.08,-2.54),Point(100,100),rot,mirror) vs exp_transform truth
```

  - Artifact: `<scratch>/schematic-authoring/gen_ksa.py, ksa_out/`
- **kicad-skip 0.2.5 lossless round trip and clone/move/wire edits**
  - Result: Round trip 0 semantic diffs; clone kept instances reference -> duplicate 'R1' in netlist until instances.project.path.reference fixed; off-grid clone -> endpoint_off_grid.
  - Command/code:

```
venv/Scripts/python.exe (skip.Schematic(...); write; clone R1 -> R2; move; wire clone; write) ; python sexpr_diff.py ...; kicad-cli sch export netlist / sch erc
```

  - Artifact: `<scratch>/schematic-authoring/skip_out/`
- **sexpdata parse/modify/dump and KiCad acceptance + timing on the 13.9k-line demo**
  - Result: Parse 0.03 s / dump 0.04 s for pic_programmer; lossless; KiCad 10 ERC 0, nets correct.
  - Command/code:

```
venv/Scripts/python.exe (sexpdata.loads/dumps, change R1 Value) ; validate sexpdata_out 10.0
```

  - Artifact: `<scratch>/schematic-authoring/sexpdata_out/`
- **skidl 2.3.0 netlist + generate_schematic(tool=KICAD10) and circuit-synth 0.1.0 generate_kicad_project**
  - Result: skidl: file loads (20230409 header) but J1.1 unconnected + lib_symbol_mismatch x3; circuit-synth: 5 ERC errors (hier labels on root), wrong nets, `kicad-cli sch upgrade` segfault (rc 139) truncating file to 0 bytes.
  - Command/code:

```
KICAD10_SYMBOL_DIR=... venv/Scripts/python.exe (skidl script) ; validate skidl_out 10.0 ; venv/Scripts/python.exe (circuit_synth script) ; validate csynth_out/ledcs 10.0
```

  - Artifact: `<scratch>/schematic-authoring/skidl_out/, csynth_out/ledcs/`
- **Live IPC API probe against eeschema 10.0.0 (isolated KICAD_CONFIG_HOME, api.enable_server true, system.first_run_shown true)**
  - Result: GetOpenDocuments OK (board_filename 'ledhier.kicad_sch'); Ping/GetVersion 'no handler'; GetItems 'no handler available'; BeginCommit timed out and server stopped; CreateItems timed out and eeschema process crashed (no file modification). First launch without first_run_shown showed the modal 'KiCad Setup' wizard.
  - Command/code:

```
PowerShell: $env:KICAD_CONFIG_HOME=<scratch>/scratch_config; Start-Process eeschema.exe hier_out/ledhier/ledhier.kicad_sch; venv/Scripts/python.exe (kipy KiCadClient: GetOpenDocuments, GetItems, BeginCommit, CreateItems(Line), SaveDocument); Stop-Process eeschema
```

  - Artifact: `<scratch>/schematic-authoring/scratch_config/`
- **ERC JSON position scaling across kicad-cli 9/10**
  - Result: JSON pos x=0.635,y=0.7366 for the pin the text report places at (63.50 mm, 73.66 mm) in both 9.0.5 and 10.0.0.
  - Command/code:

```
kicad-cli sch erc --severity-all --format json -o erc_units/erc_$v.json erc_units/v9.kicad_sch (9.0, 10.0); compare with text report
```

  - Artifact: `<scratch>/schematic-authoring/erc_units/`
- **KiCad source inspection (10.0 branch vs master) for schematic IPC API, parser version gates and kicad-cli error handling**
  - Result: 10.0: schematic_commands.proto has no messages, handler registers only GetOpenDocuments; master: ~55 handlers incl. GetItems/CreateItems/PlaceSymbolFromLibrary/GetSchematicNetlist/SaveDocument/jobs; LoadSchematic catch(...) swallows errors; parser top-level tokens for 20260306 enumerated (group, generator, generator_version, uuid, paper, page, title_block, lib_symbols, symbol, image, sheet, junction, no_connect, bus_entry, polyline, bus, wire, arc, circle, rectangle, bezier, rule_area, text, label, global_label, hierarchical_label, directive_label, text_box, table, sheet_instances, symbol_instances, bus_alias, embedded_fonts, embedded_files).
  - Command/code:

```
curl -s -L https://gitlab.com/kicad/code/kicad/-/raw/{10.0,master}/api/proto/schematic/schematic_{commands,types}.proto ; .../eeschema/api/api_handler_sch.cpp ; .../eeschema/eeschema_helpers.cpp ; .../eeschema/sch_io/kicad_sexpr/sch_io_kicad_sexpr_parser.cpp ; .../common/api/api_server.cpp
```

  - Artifact: `<scratch>/schematic-authoring/protos/`
