# Verification record

Adversarial verification of the 12 load-bearing claims in [ARCHITECTURE.md](ARCHITECTURE.md). Each claim was given to two independent verifiers instructed to *refute* it: one empirical (run it on this machine against the installed KiCad 7.0.6 / 8.0.1 / 9.0.5 / 10.0.0) and one documentary (KiCad source at the exact tags, release notes, package docs). `refuted=true` includes "partially true"; the correction is the precise statement. Scripts referenced under `scratchpad/...` are preserved in [../spikes/](../spikes/).

Date: 2026-09-18. KiCad point releases current at that date: 10.0.6, Freerouting 2.4.1, kicad-python 0.8.0, mcp 2.2.0.


## Claim 1 — SURVIVED

> On KiCad 8.0.1, 9.0.5 and 10.0.0, `kicad-cli sch erc --format json` reports item positions exactly 100 times smaller than the text report, while `kicad-cli pcb drc --format json` positions are correct in millimetres.

### Empirical verifier — refuted=false

Ran kicad-cli from all three installs (8.0.1, 9.0.5, 10.0.0) against copies of the bundled demo projects and a hand-written minimal schematic in the scratchpad (<scratchpad>/erc-json-scale/, with KICAD_CONFIG_HOME pointed at scratchpad cfg folders). Both `--format report` and `--format json` were produced for each run and every item position was compared programmatically (compare.py).

ERC (schematic):
- KiCad 10.0.0, demo pic_programmer.kicad_sch: 44 violations; report says e.g. `@(71.12 mm, 50.80 mm): Symbol R2`, JSON says `{"x":0.7112,"y":0.508}` with `"coordinate_units":"mm"`. All 44 items: ratio report/json = exactly 100.0 on every x and y. The .kicad_sch confirms R2 really is at y=50.8 mm, so the report is right and the JSON is wrong.
- Minimal schematic mini.kicad_sch (wire at (76.2,50.8), global label at (50.8,50.8), no_connect at (101.6,50.8), R1 at (63.5,63.5)):
  - 8.0.1: report `@(76.2000 mm, 50.8000 mm)` wire, `@(50.8000 mm, 50.8000 mm)` label, `@(101.6000 mm, 50.8000 mm)` NC, pins at (63.5,59.69)/(63.5,67.31); JSON gives (0.762,0.508), (0.508,0.508), (1.016,0.508), (0.635,0.5969)/(0.635,0.6731). All exactly 100x smaller.
  - 9.0.5: identical result, 8 violations, every JSON pos = report/100.
  - 10.0.0: identical result, 8 violations, every JSON pos = report/100.
  - Additional detail: the JSON item *description* text also carries the bug: "Horizontal Wire, length 0.1270 mm" in JSON vs "length 12.70 mm" in the report (all three versions).

DRC (board):
- 10.0.0 pic_programmer.kicad_pcb: 41 items, JSON (110.49, 78.867) == report `@(110.4900 mm, 78.8670 mm)`; ratio exactly 1.0 for all 41.
- 8.0.1 pic_programmer.kicad_pcb: 11 items, ratio 1.0 for all (e.g. (142.24, 97.79) both).
- 9.0.5 pic_programmer.kicad_pcb (1 item, (148.082, 97.79)) and ecc83-pp.kicad_pcb with --all-track-errors (4 items, e.g. (121.285,136.525)): ratio 1.0 for all.

So the claim is confirmed as stated on every version tested. A third-party bug report (mixelpixx/Konnect #541) independently describes the same 100x ERC JSON error and states it is still present upstream in KiCad 10.0.5/10.0.6, i.e. it is not fixed in later 10.0.x point releases either.

Side observations (not part of the claim, but relevant to anyone scripting this): on 8.0.1 and 9.0.5 the very first `kicad-cli sch erc --format report` invocation with a brand-new empty KICAD_CONFIG_HOME segfaulted (exit 139, no output file); re-running immediately succeeded. 10.0.0 did not exhibit this. Also, ERC violation type names differ across versions (8: `label_dangling`; 9: `global_label_dangling` + `unconnected_wire_endpoint`; 10: `label_dangling` + `unconnected_wire_endpoint`), so a consumer should not hard-code them.

Sources:
- Local runs of C:/Program Files/KiCad/{8.0,9.0,10.0}/bin/kicad-cli.exe; outputs under <scratchpad>/erc-json-scale/{v8.0,v9.0,v10.0}/pic_programmer/{erc,drc}.{rpt,json}, .../mini/v{8.0,9.0,10.0}/erc.{rpt,json}, .../v9.0/ecc83/drc.{rpt,json}, .../compare.py
- https://github.com/mixelpixx/Konnect/issues/541
- https://docs.kicad.org/10.0/en/cli/cli.html

### Empirical verifier — refuted=false

NOT REFUTED - the claim is confirmed by source code at the exact tags and by local reproduction on all three installs.

SOURCE (documentary): In eeschema ERC report code, the text report and the JSON report use different internal-unit scales:
- Tag 8.0.1, eeschema/erc_report.cpp: line 46 `UNITS_PROVIDER unitsProvider( schIUScale, m_reportUnits );` (text report, GetTextReport) vs line 113 `UNITS_PROVIDER unitsProvider( pcbIUScale, m_reportUnits );` (WriteJsonReport).
- Tag 9.0.5, eeschema/erc/erc_report.cpp: line 46 schIUScale (text) vs line 124 pcbIUScale (JSON).
- Tag 10.0.0, eeschema/erc/erc_report.cpp: line 63 schIUScale (text) vs line 161 pcbIUScale (JSON). Branch `master` still has the same bug (lines 63 vs 163) as of 2026-09-18.
- include/base_units.h (10.0): `PCB_IU_PER_MM = 1e6` (line 70) and `SCH_IU_PER_MM = 1e4` (line 72). Since schematic coordinates are stored in schematic IU (1e4/mm) but the JSON path divides by the PCB scale (1e6/mm), every JSON position is exactly 1e6/1e4 = 100x too small. The conversion is done in common/rc_item.cpp RC_ITEM::GetJsonViolation via `EDA_UNIT_UTILS::UI::ToUserUnit( aUnitsProvider->GetIuScale(), aUnitsProvider->GetUserUnits(), mainItem->GetPosition().x )`, so the factor is independent of --units (mm or in).
- DRC: pcbnew/drc/drc_report.cpp at 8.0.1 (lines 57/118), 9.0.5 (57/118) and 10.0 (67/147) uses pcbIUScale for BOTH text and JSON, so board positions are correct.

LOCAL TEST (scratchpad <scratchpad>/verify-erc-json-units): a schematic with a single dangling wire from (100.33, 50.8) mm, run with each install's kicad-cli:
- 8.0.1: text `@(100.3300 mm, 50.8000 mm): Horizontal Wire, length 20.3200 mm`; JSON kicad_version "8.0.1", coordinate_units "mm", pos {x: 1.0033, y: 0.508}, description "length 0.2032 mm".
- 9.0.5: text `@(100.33 mm, 50.80 mm)`; JSON pos {x: 1.0033, y: 0.508}.
- 10.0.0: text `@(100.33 mm, 50.80 mm)`; JSON pos {x: 1.0033, y: 0.508}.
A board with a dangling track starting at (100.33, 50.8) mm: DRC text `@(100.3300 mm, 50.8000 mm)` and DRC JSON pos {x: 100.33, y: 50.8} on 8.0.1, 9.0.5 and 10.0.0 - correct.

Additional notes (do not contradict the claim, but relevant to an architecture relying on it): (1) in ERC JSON the item `description` strings are also scaled (wire length reported as 0.2032 mm instead of 20.32 mm), so any parsing of lengths from descriptions is affected too; (2) the factor is 100 for both --units mm and --units in (the Konnect issue confirms the same factor with inches); (3) the bug is still present in KiCad master, and third-party reports show it persisting in 10.0.5 and 10.0.6, so a consumer should multiply ERC JSON positions by 100 (or better, detect by comparing to the text report / sheet size) rather than assume a future fix; (4) no dedicated upstream GitLab issue for this specific scaling bug was found - issue #16924 (8.0.0-rc2) shows ERC JSON positions like x 1.1176, y 0.635 without noticing they are 100x off.

Sources:
- https://gitlab.com/kicad/code/kicad/-/raw/8.0.1/eeschema/erc_report.cpp
- https://gitlab.com/kicad/code/kicad/-/raw/9.0.5/eeschema/erc/erc_report.cpp
- https://gitlab.com/kicad/code/kicad/-/raw/10.0.0/eeschema/erc/erc_report.cpp
- https://gitlab.com/kicad/code/kicad/-/raw/master/eeschema/erc/erc_report.cpp
- https://gitlab.com/kicad/code/kicad/-/raw/10.0/include/base_units.h
- https://gitlab.com/kicad/code/kicad/-/raw/10.0/common/rc_item.cpp
- https://gitlab.com/kicad/code/kicad/-/raw/8.0.1/pcbnew/drc/drc_report.cpp
- https://gitlab.com/kicad/code/kicad/-/raw/9.0.5/pcbnew/drc/drc_report.cpp
- https://gitlab.com/kicad/code/kicad/-/raw/10.0/pcbnew/drc/drc_report.cpp
- https://github.com/mixelpixx/Konnect/issues/541
- https://gitlab.com/kicad/code/kicad/-/issues/16924
- https://docs.kicad.org/10.0/en/cli/cli.html


## Claim 2 — CORRECTED

> A .kicad_sch at version 20260306 with lib_symbols copied verbatim from KiCad 10's libraries (extends flattened), wires split at pin endpoints on the 1.27 mm grid and PWR_FLAGs on power-input nets passes kicad-cli 10 ERC with zero violations and exports the intended netlist.

### Empirical verifier — refuted=true

CORE MECHANISM CONFIRMED on this machine with kicad-cli 10.0.0, but the claim as stated is incomplete/over-general (hence refuted=partially). What worked: generator <scratchpad>\erc_verify\gen.py (+ sexp.py) built t1\ldo_test.kicad_sch: (version 20260306) — confirmed to be exactly what KiCad 10.0.0 writes (kicad-cli sch upgrade --force of the ecc83 demo emits 20260306; the shipped demos themselves are at most 20260101). 8 lib_symbols copied from KiCad 10 Device/power/Connector_Generic/Regulator_Linear libs (lib version 20251024), AMS1117-3.3 flattened from AP1117-15; J1 Conn_01x02 -> C1 -> U1 AMS1117-3.3 -> C2 -> +3V3 -> R1 -> D1 LED -> GND, PWR_FLAG on VIN and GND, all symbols rotation 0, every wire ending exactly on a pin endpoint or junction, all coordinates multiples of 1.27 mm. Result: `kicad-cli sch erc --format json --severity-all --exit-code-violations` -> "Found 0 violations", exit 0, reproduced 5x; JSON shows only the 4 default-ignored checks (single_global_label, four_way_junction, simulation_model_issue, footprint_filter), so lib_symbol_mismatch/lib_symbol_issues/endpoint_off_grid etc. all ran. `sch export netlist --format kicadsexpr` -> nets exactly as intended: +3V3{C2.1,R1.1,U1.2 power_out}, GND{C1.2,C2.2,D1.2,J1.2,U1.1}, Net-(D1-K){D1.1,R1.2}, Net-(J1-Pin_1){C1.1,J1.1,U1.3 power_in}; components carry correct libsource lib/part. `sch upgrade --force` re-save (t3\resaved.kicad_sch) gives 0 diff lines for every lib_symbol incl. the flattened AMS1117-3.3, i.e. the flatten exactly matches KiCad's own. `sch export pdf` also succeeds. Negative controls prove ERC is sensitive to each precondition: (A) drop VIN PWR_FLAG -> error power_pin_not_driven on U1.VI; (B) one wire running THROUGH R1 instead of ending at its pins -> 2x error pin_not_connected (R1.1, R1.2); (F) R1 shifted 0.635 mm -> 3x warning endpoint_off_grid. HIDDEN PRECONDITIONS the claim omits, each verified to break it: (1) `extends` left unflattened (child + parent both embedded verbatim) -> parser accepts the file but U1 has NO pins: warning lib_symbol_mismatch, error power_pin_not_driven on +3V3, 3x unconnected_wire_endpoint (5 violations). (2) lib_symbols top-level names must be renamed to "Lib:Name" (e.g. "Device:R"); literally verbatim name "R" with lib_id "Device:R" -> 25 violations (all pins '???', pin_not_connected everywhere); "R"/"R" -> 10x warning lib_symbol_issues "does not include the symbol library ''". (3) each symbol needs an `(instances (project ...) (path "/<root-uuid>" (reference ..)(unit 1)))` block: with it removed ERC gives a spurious error wire_dangling and the netlist is WRONG (Net-(D1-K) disappears, VIN net renamed unconnected-(#FLG01-Pad1)); a non-matching project name is fine (0 violations, correct netlist). (4) Zero violations is ENVIRONMENT-DEPENDENT: with KICAD_CONFIG_HOME pointing at an empty dir (fresh/headless machine with no global sym-lib-table) the identical file gets 11 warnings (10x lib_symbol_issues "The current configuration does not include the symbol library 'Device'/'power'/..." + 1x footprint_link_issues for Package_TO_SOT_SMD). Fix verified: a .kicad_pro beside the schematic plus project-local sym-lib-table/fp-lib-table using ${KICAD10_SYMBOL_DIR}/${KICAD10_FOOTPRINT_DIR} -> 0 violations under the isolated config; the local tables are IGNORED when no .kicad_pro exists (still 11 warnings). On this user's machine the run passed because %APPDATA%\kicad\10.0\sym-lib-table (nested "Table" entry pointing at KiCad 10's template sym-lib-table) exists. (5) KiCad 9.0.5 kicad-cli cannot load the 20260306 file at all ("Failed to load schematic", exit 3) for both erc and netlist export. Not tested: opening in the eeschema GUI (another agent's eeschema/pcbnew were running; rules forbid a second GUI instance), rotated/mirrored placements, multi-unit symbols, hierarchical sheets, symbols with non-trivial extends chains beyond one level. Side note: kicad-cli 10 ERC JSON reports positions as e.g. x=0.6858 for 68.58 mm despite coordinate_units "mm" (values appear to be mm/100).

**Correction:** A .kicad_sch at version 20260306 passes kicad-cli 10.0.0 ERC with zero violations and exports the intended netlist ONLY when all of the following hold: (a) lib_symbols are copied from KiCad 10's libraries with the top-level symbol name rewritten to "<LibNickname>:<Name>" (sub-unit names like "R_1_1" stay unprefixed) and every `extends` flattened exactly as KiCad does (parent body + sub-units renamed to the child name, child properties overriding) — an unflattened extends yields a pinless symbol and lib_symbol_mismatch; (b) each placed symbol carries an `instances` block with the root sheet path, otherwise the netlist is wrong and a spurious wire_dangling error appears; (c) wires end exactly on pin endpoints/junctions on the 1.27 mm grid and every net with only power_in + passive pins has a PWR_FLAG (verified necessary); AND (d) the referenced symbol and footprint libraries are resolvable in the environment where kicad-cli runs — either a global sym-lib-table/fp-lib-table (created by a KiCad GUI first run) or a .kicad_pro next to the schematic plus project-local sym-lib-table/fp-lib-table; with a bare config the same file reports lib_symbol_issues/footprint_link_issues warnings (11 in the test), and project-local tables are ignored without the .kicad_pro. The file is not loadable by KiCad 9 (exit 3). Only rotation-0, single-unit, flat (non-hierarchical) schematics were verified; GUI load was not tested.

Sources:
- <scratchpad>\erc_verify\gen.py
- <scratchpad>\erc_verify\t1\ldo_test.kicad_sch
- <scratchpad>\erc_verify\t1\erc1.json
- <scratchpad>\erc_verify\t1\net1.net
- <scratchpad>\erc_verify\t1\ldo_E.erc.json (isolated config, 11 warnings)
- <scratchpad>\erc_verify\t2\ (ldo_A/B/C/D/D2/F/P/Q variants + .erc.json)
- <scratchpad>\erc_verify\t3\ (kicad_pro + local lib tables -> 0 violations; resaved.kicad_sch round-trip)
- C:\Program Files\KiCad\10.0\share\kicad\symbols\{Device,power,Connector_Generic,Regulator_Linear}.kicad_sym (version 20251024)
- C:\Program Files\KiCad\10.0\share\kicad\demos\ecc83 (upgraded by kicad-cli 10 -> version 20260306)
- kicad-cli.exe 10.0.0 (build Mar 25 2026) and 9.0.5 outputs captured in the scratchpad *.err / *.erc.json files

### Empirical verifier — refuted=true

DOCUMENTARY (KiCad 10.0.0 tag): eeschema/sch_file_versions.h defines SEXPR_SCHEMATIC_FILE_VERSION 20260306 ("Variant in_bom semantics corrected") and SEXPR_SYMBOL_LIB_FILE_VERSION 20251024; the parser throws FUTURE_FORMAT_ERROR only if version > 20260306. eeschema/erc/erc.cpp: TestLibSymbolIssues checks the library-table row exists ("The current configuration does not include the symbol library '%s'"), loads the library symbol, Flatten()s it and runs Compare(*libSymbolInSchematic, EQUALITY|ERC) -> lib_symbol_mismatch if different; TestOffGridEndpoints uses m_schematic->Settings().m_ConnectionGridSize (schematic_settings.h: DEFAULT_CONNECTION_GRID_MILS 50 = 1.27 mm) on wire ends, bus entries and non-NC pins; TestPinToPin flags power_in pins with no PT_POWER_OUT driver (ERCE_POWERPIN_NOT_DRIVEN). erc_settings.cpp defaults: lib_symbol_issues, lib_symbol_mismatch, endpoint_off_grid, footprint_link_issues, missing_unit, unconnected_wire_endpoint, similar_labels, pin_to_pin_warning = WARNING; single_global_label, footprint_filters, simulation_model, four_way_junction = IGNORE; everything else ERROR. sch_io_kicad_sexpr_parser.cpp: lib_symbols names are validated with LIB_ID::Parse and linked via screen->UpdateLocalLibSymbolLinks(); sch_screen.cpp line 875 looks up m_libSymbols by symbol->GetSchSymbolLibraryName() (the "Lib:Name" string). docs.kicad.org/10.0 cli: sch erc --exit-code-violations returns 0/5; sch export netlist --format kicadsexpr. LOCAL TESTS (kicad-cli 10.0.0, KICAD_CONFIG_HOME pointed at a scratch copy of the user's 10.0 config; files in .../scratchpad/verify-sch-20260306/): kicad-cli sch upgrade of a demo confirms 10.0.0 writes (version 20260306). t1 (R1,R2,+3V3,GND,2xPWR_FLAG; lib_symbols copied from Device/power .kicad_sym with only the "Lib:" prefix added; 5 wires all ending at pin endpoints on 1.27 mm multiples): "Found 0 violations", exit 0; netlist nets +3V3{R1.1}, GND{R2.2}, Net-(R1-Pad2){R1.2,R2.1} = intended. t2 (identical but names literally verbatim "R","GND",...): 11 violations (3 errors), netlist Net-(R1-Pad??) with pintype unspecified. t3 (hand-flattened Regulator_Linear:LM1117LD-3.3 extends LM1117LD-1.8, footprints set, VO power_out driving +3V3, PWR_FLAG on +5V and GND): 0 violations, netlist +3V3{R1.1,U1.5,6,7,9} +5V{U1.2,3,4} GND{R1.2,U1.1} unconnected-(U1-NC-Pad8) = intended, proving the hand flatten passes lib_symbol_mismatch and footprint_link_issues. t4 (one wire passing through the +3V3 pin end instead of two wires meeting there): pin_not_connected + power_pin_not_driven errors. t5 (wire vertex at 51.435 mm, a 0.635 offset): 2x endpoint_off_grid warnings. Also observed: with a config home lacking 10.0/sym-lib-table every symbol got a lib_symbol_issues warning (6 violations, exit 5) even though the design was otherwise clean; the KiCad demo amplifier-ac shows lib_symbol_mismatch and power_pin_not_driven firing in a real project. Note: KICAD_CONFIG_HOME gets "10.0" appended by KiCad; kicad-cli rewrites kicad_common.json on exit (my first sch-upgrade run before isolating the config re-serialized the user's %APPDATA%/kicad/10.0/kicad_common.json; a sibling kicad-cli process not started by me was also running).

**Correction:** The recipe works, but two of its premises are imprecise enough to break it if taken literally: (1) lib_symbols entries are NOT verbatim copies: the top-level symbol name must be prefixed with the library nickname ("Device:R", "power:GND", "Regulator_Linear:LM1117LD-3.3"), because KiCad 10 links placed symbols to lib_symbols by the full lib_id string (SCH_SCREEN::UpdateLocalLibSymbolLinks -> m_libSymbols.find(symbol->GetSchSymbolLibraryName())). A literal verbatim copy ("R") parses without error but leaves every symbol pin-less: tested locally it produced 3 errors + 8 warnings (pin_not_connected, wire_dangling, pin_to_pin "Unspecified") and a garbage netlist ("Net-(R1-Pad??)"). Sub-unit blocks ("R_0_1", "R_1_1") stay unprefixed; when hand-flattening an "extends" symbol the child's properties override the parent's and the parent's unit blocks must be renamed to the child's name ("<child>_0_1"). (2) "Zero violations" also requires that kicad-cli can resolve the global symbol library table: ERC_TESTER::TestLibSymbolIssues emits a lib_symbol_issues WARNING per symbol ("The current configuration does not include the symbol library 'Device'") when the config dir (KICAD_CONFIG_HOME/10.0 or %APPDATA%/kicad/10.0) lacks sym-lib-table; --severity-all counts warnings, so the run exits 5. With those two corrections applied, the claim holds: file version 20260306 is exactly KiCad 10.0.0's SEXPR_SCHEMATIC_FILE_VERSION; the ERC off-grid test uses m_ConnectionGridSize = DEFAULT_CONNECTION_GRID_MILS 50 mil = 1.27 mm; wires must end at pin endpoints (a wire passing through a pin end is NOT connected: tested -> pin_not_connected + power_pin_not_driven); and PWR_FLAG (power_out pin) or any other power_out pin (e.g. a regulator VO) on each power_in net satisfies power_pin_not_driven (error by default).

Sources:
- https://gitlab.com/kicad/code/kicad/-/blob/10.0.0/eeschema/sch_file_versions.h
- https://gitlab.com/kicad/code/kicad/-/blob/10.0.0/eeschema/erc/erc.cpp
- https://gitlab.com/kicad/code/kicad/-/blob/10.0.0/eeschema/erc/erc_settings.cpp
- https://gitlab.com/kicad/code/kicad/-/blob/10.0.0/eeschema/sch_io/kicad_sexpr/sch_io_kicad_sexpr_parser.cpp
- https://gitlab.com/kicad/code/kicad/-/blob/10.0.0/eeschema/sch_screen.cpp
- https://gitlab.com/kicad/code/kicad/-/blob/10.0.0/eeschema/schematic_settings.cpp
- https://gitlab.com/kicad/code/kicad/-/blob/10.0.0/eeschema/schematic_settings.h
- https://docs.kicad.org/10.0/en/cli/cli.html
- <scratchpad>/verify-sch-20260306/gen.py
- <scratchpad>/verify-sch-20260306/gen2.py
- <scratchpad>/verify-sch-20260306/t1/t1.erc.rpt
- <scratchpad>/verify-sch-20260306/t2/t2.erc.rpt
- <scratchpad>/verify-sch-20260306/t3/t3.net
- <scratchpad>/verify-sch-20260306/t4/t4.erc.rpt
- <scratchpad>/verify-sch-20260306/t5/t5.erc.rpt


## Claim 3 — SURVIVED

> Symbol pin sheet positions follow rot0 (X+lx, Y-ly), rot90 (X-ly, Y-lx), rot180 (X-lx, Y+ly), rot270 (X+ly, Y+lx), with mirror x negating the Y offset and mirror y negating the X offset, for all 12 orientation combinations.

### Empirical verifier — refuted=false

Could not refute; the claim held exactly for all 12 combinations on KiCad 10.0.0 and 9.0.5, using two independent methods.

Setup (scratchpad <scratchpad>/pin-orient-verify/): generated pinorient.kicad_sch (gen.py) with a custom lib_symbol "T:PINT" having one input pin whose lib connection point is (lx,ly)=(2.54,5.08) (asymmetric so axis swaps/sign errors are detectable), placed 12 instances U1..U12 covering rot 0/90/180/270 x {no mirror, (mirror x), (mirror y)} at distinct (X,Y). Formula under test: offsets rot0 (+lx,-ly), rot90 (-ly,-lx), rot180 (-lx,+ly), rot270 (+ly,+lx); mirror x flips dy, mirror y flips dx.

Method 1 - ERC pin positions: `kicad-cli sch erc --format json --severity-all` (KiCad 10 on v20250610 file; KiCad 9 on a v20250114 copy). check.py compared every "pin_not_connected" position against the formula: 12/12 OK on KiCad 10, 12/12 OK on KiCad 9 (e.g. U4 rot90 at (50.8,81.28) -> KiCad (45.72,78.74) = (X-ly, Y-lx); U5 rot90 mirror x -> (76.2,83.82); U11 rot270 mirror x at (142.24,111.76) -> (147.32,109.22); U12 rot270 mirror y at (50.8,142.24) -> (45.72,144.78)). The text report (--format report) shows the same values in real mm (e.g. "@(45.72 mm, 78.74 mm): Symbol U4 Pin 1").

Method 2 - connectivity: gen_wires.py added, for each symbol, a wire starting at the formula-predicted pin point and ending at a label N<i>; `kicad-cli sch export netlist --format kicadxml` (KiCad 10) shows U2..U12 pin 1 each on its own net /N2../N12, i.e. the wire endpoint landed exactly on the pin. Negative control: U1's wire was deliberately placed at the rot180 position instead of rot0 -> netlist reports "unconnected-(U1-IN-Pad1)", proving the test discriminates (U1 rot0 itself was confirmed by ERC in both versions).

Side findings relevant to an architecture relying on this: (1) In kicad-cli 10.0.0 and 9.0.5 the ERC JSON report's "pos" values are 1/100 of the true mm value despite "coordinate_units":"mm" (0.8382 for 83.82 mm); the text report is correct. (2) lx,ly are the lib_symbol pin's `(at lx ly angle)` connection point in library (Y-up) coordinates; the formula maps to sheet (Y-down) coordinates. Only multiples of 90 deg were tested since those are the only legal symbol rotations. No GUI process was launched; nothing written outside the scratchpad.

Sources:
- Local test files: <scratchpad>/pin-orient-verify/{gen.py,check.py,gen_wires.py,pinorient.kicad_sch,pinorient9.kicad_sch,pinorient_wires.kicad_sch,erc10.json,erc10.rpt,erc9.json,net10.xml}
- kicad-cli 10.0.0 (C:/Program Files/KiCad/10.0/bin/kicad-cli.exe) sch erc / sch export netlist
- kicad-cli 9.0.5 (C:/Program Files/KiCad/9.0/bin/kicad-cli.exe) sch erc

### Empirical verifier — refuted=false

CLAIM HOLDS (with lx, ly = the pin's (at x y) coordinates as written in the .kicad_sym / lib_symbols file, Y-up; X, Y = the schematic symbol's (at X Y rot) position, and (mirror x|y) written after (at ...) as KiCad always emits it).

DOCUMENTARY (KiCad 10.0 branch source, identical on master):
1. Library pin coords are Y-inverted on load: sch_io_kicad_sexpr_parser.cpp:1672 `pin->SetPosition( parseXY( true ) );` where parseXY(aInvertY) (parser .h:170-179) does `xy.y = aInvertY ? -parseInternalUnits(...) : ...`. So internal lib pin = (lx, -ly).
2. Default TRANSFORM is identity in 10.0: libs/kimath/include/transform.h:56-60 `TRANSFORM() : x1(1), y1(0), x2(0), y2(1)`. TransformCoordinate (libs/kimath/src/transform.cpp:44-47): out = (x1*px + y1*py, x2*px + y2*py).
3. Rotation parsing (parser .cpp:3188-3202): `case 0: TRANSFORM(); case 90: TRANSFORM(0,1,-1,0); case 180: TRANSFORM(-1,0,0,-1); case 270: TRANSFORM(0,-1,1,0)`, then `symbol->SetTransform(transform)`. Applied to (lx,-ly): rot0 -> (lx,-ly); rot90 -> (-ly,-lx); rot180 -> (-lx,+ly); rot270 -> (+ly,+lx). Exactly the claimed four formulas.
4. Mirror parsing (parser .cpp:3204-3215): `(mirror x)` -> `SetOrientation(SYM_MIRROR_X)` (temp = diag(1,-1)), `(mirror y)` -> `SetOrientation(SYM_MIRROR_Y)` (temp = diag(-1,1)) (sch_symbol.cpp SetOrientation). The incremental composition `newTransform.x1 = m.x1*temp.x1 + m.x2*temp.y1; y1 = m.y1*temp.x1 + m.y2*temp.y1; x2 = m.x1*temp.x2 + m.x2*temp.y2; y2 = m.y1*temp.x2 + m.y2*temp.y2` evaluates to new = temp * old, i.e. the mirror is applied AFTER rotation in sheet coordinates: mirror x negates the sheet Y offset, mirror y negates the sheet X offset, for every rotation. (The comment in the code saying "old * temp" is misleading; the arithmetic is temp*old.)
5. Pin sheet position: sch_pin.cpp:254-260 `SCH_PIN::GetPosition() = symbol->GetTransform().TransformCoordinate(m_position) + symbol->GetPosition()`; sch_symbol.cpp:2982-2988 GetPinPhysicalPosition does the same.
6. Writer (sch_io_kicad_sexpr.cpp:699-710) emits `(at X Y angle)` from GetOrientation() & ~mirror bits and then `(mirror x|y)`, so files round-trip with this same semantic. Note: 12 combos map onto only 8 distinct matrices (e.g. rot0+mirror x == rot180+mirror y); formulas still hold for each of the 12 as written.

LOCAL TEST (KiCad 10.0.0 kicad-cli, scratchpad <scratchpad>/verify-pin-orient/test/t.kicad_sch): custom symbol with one pin at lib (2.54, 5.08) placed 12 times, one per orientation combo; `kicad-cli sch erc --format report --units mm` reports each unconnected pin's sheet position. All 12 ERC positions equal the claimed formulas exactly (e.g. rot0 @ (50.8,50.8) -> (53.34,45.72); rot90 mirror x @ (50.8,76.2) -> (45.72,78.74); rot270 mirror y @ (127,101.6) -> (121.92,104.14)). KiCad 8.0.1 and 9.0.5 kicad-cli give identical positions. Caveat unrelated to the claim: the ERC JSON output's "pos" values are the mm values divided by 100 (0.5334 for 53.34 mm); the text report shows the correct mm values.

Sources:
- https://gitlab.com/kicad/code/kicad/-/raw/10.0/eeschema/sch_io/kicad_sexpr/sch_io_kicad_sexpr_parser.cpp
- https://gitlab.com/kicad/code/kicad/-/raw/10.0/eeschema/sch_io/kicad_sexpr/sch_io_kicad_sexpr_parser.h
- https://gitlab.com/kicad/code/kicad/-/raw/10.0/eeschema/sch_symbol.cpp
- https://gitlab.com/kicad/code/kicad/-/raw/10.0/eeschema/sch_pin.cpp
- https://gitlab.com/kicad/code/kicad/-/raw/10.0/eeschema/sch_io/kicad_sexpr/sch_io_kicad_sexpr.cpp
- https://gitlab.com/kicad/code/kicad/-/raw/10.0/libs/kimath/include/transform.h
- https://gitlab.com/kicad/code/kicad/-/raw/10.0/libs/kimath/src/transform.cpp
- https://gitlab.com/kicad/code/kicad/-/raw/master/eeschema/sch_io/kicad_sexpr/sch_io_kicad_sexpr_parser.cpp
- https://gitlab.com/kicad/code/kicad/-/raw/master/eeschema/sch_symbol.cpp


## Claim 4 — CORRECTED

> KiCad 10.0.0's schematic IPC handler answers only GetOpenDocuments; BeginCommit, CreateItems and HitTest crash eeschema.exe with a null m_frame in API_HANDLER_EDITOR::checkForBusy (upstream #24966).

### Empirical verifier — refuted=false

CONFIRMED empirically on this machine against KiCad 10.0.0 (C:/Program Files/KiCad/10.0), plus symbolized crash address and upstream source.

Test setup (scratchpad <scratchpad>/verify-sch-ipc): isolated KICAD_CONFIG_HOME (cfg10/10.0/kicad_common.json with api.enable_server=true and do_not_show_again.* set to skip the KiCad 10 first-run wizard), the ecc83 demo copied to proj/, eeschema.exe launched fresh for each RPC (isolate.py / runall.sh), raw protobuf messages sent via kipy 0.8.0 KiCadClient (the kipy 0.8.0/0.7.1 wheels' high-level Schematic class does not even import: stale schematic_types_pb2), liveness checked by PID, crashes confirmed from the Windows Application event log (Id 1000). Note: WER silently restarts a crashed eeschema.exe with the same command line, so a by-image-name liveness check gives a false "alive"; PID tracking was required.

Results, one fresh eeschema.exe 10.0.0 per line (logs runall_10.log, runall_10b.log):
- GetOpenDocuments(DOCTYPE_SCHEMATIC) -> answered: [type: DOCTYPE_SCHEMATIC board_filename: "ecc83-pp.kicad_sch"].
- BeginCommit -> client times out; eeschema.exe PID dies; event log: Faulting module _eeschema.dll 10.0.0.49194, fault offset 0xa8d19d, c0000005.
- CreateItems (one sch Line) -> same crash, same offset 0xa8d19d.
- HitTest -> same crash, same offset 0xa8d19d.
- EndCommit, UpdateItems, DeleteItems -> ALSO crash, same offset 0xa8d19d (claim is under-inclusive here).
- GetItems, GetSelection, GetTitleBlockInfo, SaveDocumentToString, RunAction, Ping, GetVersion -> "no handler available for request of type ..." (no crash). Ping/GetVersion are served by API_HANDLER_COMMON, which lives in kicad.exe only; in the kicad.exe-hosted run they returned 10.0.0.

Symbolization: extracted the RSDS GUID from _eeschema.dll (BCDE735CB4DA4973B0158155E57740C06), downloaded the matching PDB from KiCad's public symbol server, and resolved with dbghelp: _eeschema.dll+0xa8d19d = API_HANDLER_EDITOR::checkForBusy+0xd, common/api/api_handler_editor.cpp:207 (build-win-10.0-stable). Line 207 in the 10.0.0 tag is `if( !m_frame->CanAcceptApiCommands() )`.

Root cause in 10.0.0 source: API_HANDLER_SCH::API_HANDLER_SCH(SCH_EDIT_FRAME* aFrame) : API_HANDLER_EDITOR(), m_frame(aFrame) — it initializes its own shadowing SCH_EDIT_FRAME* m_frame and leaves the base API_HANDLER_EDITOR::m_frame at its default nullptr; the base constructor registers BeginCommit/EndCommit/CreateItems/UpdateItems/DeleteItems/HitTest, each of which calls checkForBusy() first, which dereferences the null base m_frame. API_HANDLER_SCH itself registers only GetOpenDocuments (confirmed both in api_handler_sch.cpp at tag 10.0.0 and from RTTI strings in the shipped _eeschema.dll: registerHandler<...API_HANDLER_SCH> appears only for GetOpenDocuments).

Upstream: the fix commit 6545d4a2 "Fix shadowed m_frame in API_HANDLER_SCH" (2026-09-06, 10.0 branch, changes to API_HANDLER_EDITOR(aFrame) and drops the shadow member) says "Fixes https://gitlab.com/kicad/code/kicad/-/work_items/24966". Issue #24966 (closed 2026-09-06, milestone 10.0.7) is titled about Board.update_items() crashing in _eeschema.dll on 10.0.4 with the schematic editor open, i.e. the same null-m_frame bug reached via the API server trying each editor's handler. It is not in any released build: latest tag is 10.0.6 (2026-08-28), fix is after that. A related earlier commit 1f54157e (2026-08-21, likely in 10.0.6) moves validateItemHeaderDocument before checkForBusy for Create/Update/DeleteItems, which avoids the crash for PCB-targeted requests but not for schematic-targeted ones.

Additional findings relevant to the architecture:
- When eeschema runs inside kicad.exe (project opened with kicad.exe, schematic editor opened with Ctrl+E), BeginCommit kills the entire kicad.exe process (event log: Faulting application kicad.exe, module _eeschema.dll, offset 0xa8d19d) — so a plugin hitting this takes down the project manager and any open PCB editor too (mgr_test.log).
- KiCad 9.0.1 has the identical handler structure (RTTI strings) and its eeschema.exe also died on BeginCommit in the same test harness (runall_9.log; no WER event captured in the window checked).
- KiCad 10.0.0 does NOT honour KICAD_API_SOCKET as a server-side override (it only sets that variable for plugins it launches); the server always binds ipc://%TEMP%\kicad\api.sock, so concurrent KiCad instances on one machine share/contend for one socket.

**Correction:** Claim is accurate but under-inclusive: (1) all six API_HANDLER_EDITOR RPCs crash, not just three — BeginCommit, EndCommit, CreateItems, UpdateItems, DeleteItems and HitTest all die at API_HANDLER_EDITOR::checkForBusy+0xd (api_handler_editor.cpp:207); every other command sent to standalone eeschema returns "no handler available" (Ping/GetVersion are answered only when hosted in kicad.exe via API_HANDLER_COMMON). (2) When the schematic editor is hosted in kicad.exe, the crash kills the whole kicad.exe process (manager + any other editors), not only eeschema.exe. (3) Issue #24966's title is about Board.update_items() crashing in _eeschema.dll on 10.0.4; the same shadowed-m_frame bug is the cause, and the fix commit 6545d4a2 (2026-09-06, 10.0 branch, milestone 10.0.7) explicitly references it. No released build (latest 10.0.6, 2026-08-28) contains the fix. (4) KiCad 9.0.1 eeschema has the same handler layout and also died on BeginCommit locally.

Sources:
- https://gitlab.com/kicad/code/kicad/-/issues/24966
- https://gitlab.com/kicad/code/kicad/-/commit/6545d4a28d4978edb96d038328500d8fde58b3dd
- https://gitlab.com/kicad/code/kicad/-/commit/1f54157eb06b3f16a92c21e734a6d5645ca3d76b
- https://gitlab.com/kicad/code/kicad/-/raw/10.0.0/eeschema/api/api_handler_sch.cpp
- https://gitlab.com/kicad/code/kicad/-/raw/10.0.0/common/api/api_handler_editor.cpp
- https://gitlab.com/kicad/code/kicad/-/raw/10.0.0/include/api/api_handler_editor.h
- https://symbols.kicad.org/kicad-stable/_eeschema.pdb/BCDE735CB4DA4973B0158155E57740C06/_eeschema.pd_
- https://gitlab.com/api/v4/projects/kicad%2Fcode%2Fkicad/repository/tags
- <scratchpad>/verify-sch-ipc/runall_10.log
- <scratchpad>/verify-sch-ipc/runall_10b.log
- <scratchpad>/verify-sch-ipc/mgr_test.log
- <scratchpad>/verify-sch-ipc/runall_9.log
- <scratchpad>/verify-sch-ipc/isolate.py
- <scratchpad>/verify-sch-ipc/symbolize.py

### Documentary verifier — refuted=true

Documentary check against the KiCad GitLab source at tag 10.0.0 and the upstream issue. The core mechanism is CONFIRMED, but the claim is imprecise in four ways.

1. Root cause confirmed in source (tag 10.0.0). eeschema/api/api_handler_sch.cpp lines 37-39: `API_HANDLER_SCH::API_HANDLER_SCH( SCH_EDIT_FRAME* aFrame ) : API_HANDLER_EDITOR(), m_frame( aFrame )` -- the base is constructed with its default `aFrame = nullptr` (include/api/api_handler_editor.h line 42) and the derived class declares its own shadowing `SCH_EDIT_FRAME* m_frame` (api_handler_sch.h line 70). common/api/api_handler_editor.cpp line 207: `if( !m_frame->CanAcceptApiCommands() )` inside `API_HANDLER_EDITOR::checkForBusy()` dereferences the base (null) pointer. Upstream fix commit 6545d4a28d4978edb96d038328500d8fde58b3dd (Jon Evans, 2026-09-06) is titled "Fix shadowed m_frame in API_HANDLER_SCH" and cites work item 24966.

2. "Answers only GetOpenDocuments" is only true in effect, not by registration. The base ctor (api_handler_editor.cpp lines 34-39) registers SIX handlers for every editor handler including the schematic one: BeginCommit, EndCommit, CreateItems, UpdateItems, DeleteItems, HitTest. API_HANDLER_SCH adds GetOpenDocuments (line 41). All six inherited handlers call checkForBusy() as their first statement (lines 46, 75, 222, 249, 276, 325), so all six crash -- not just the three named in the claim. GetOpenDocuments (which uses the derived, valid m_frame) is the only one that survives. The reporter of #24966 confirms read-only calls (ping, get_version, get_open_documents, get_board, get_footprints) work and the crash is at `API_HANDLER_EDITOR::checkForBusy+0xd [common\api\api_handler_editor.cpp @ 207]` via `handleUpdateItems` (comment 2026-07-18T14:21Z).

3. Not specific to 10.0.0 and not eeschema.exe. The identical shadowed-m_frame ctor exists at tags 9.0.0, 10.0.0 and 10.0.6 (checked). Issue #24966 was filed against 10.0.4 (2026-07-17) and the duplicate #25322 against 10.0.5-261-g9cdda08927 and 10.0.6-rc2 (Windows and Ubuntu). The crash is in `_eeschema.dll` loaded into the kicad.exe process; the issue's dump is `kicad.exe.35752` and the reporter says "the whole KiCad process" dies. Standalone eeschema.exe shares the code but is not what the reports describe.

4. Intermittency / trigger. api_server.cpp (10.0.0, line 244) iterates `std::set<API_HANDLER*> m_handlers` (api_server.h line 95) and stops at the first handler that does not return AS_UNHANDLED. With pcbnew AND eeschema open, whether a board-targeted BeginCommit/UpdateItems reaches the eeschema handler first depends on pointer ordering, which explains the reported intermittency (#25322: ~2-3 of 5 runs on Windows, 5/5 on Linux). This ordering inference is mine from the source; the observed condition in both reports is "schematic editor open in the session". With only eeschema open, any of the six commands would hit the null pointer deterministically.

5. Status: #24966 is closed (status::fix-committed, priority::high, milestone 10.0.7). Fix commits 6545d4a2 and f3105844c6dbe68bc3a0154f11c83e6de816c4ec (adds a `document` DocumentSpecifier field to BeginCommit/EndCommit, "Since: 10.0.7", and rejects document-less BeginCommit/EndCommit when more than one editor is open) landed on both master and the 10.0 branch on 2026-09-06. As of 2026-09-18 the latest tag is 10.0.6 (2026-08-28), so NO released KiCad contains the fix; the locally installed 10.0.0 is affected. Note the f3105844 change means a post-10.0.7 client must send `document` on BeginCommit/EndCommit when both editors are open, otherwise it gets AS_BAD_REQUEST.

Not tested locally (documentary method only; no GUI launched).

**Correction:** In KiCad 9.0.0 through 10.0.6 (including 10.0.0), API_HANDLER_SCH constructs its base API_HANDLER_EDITOR with a null frame and shadows m_frame, so the base's checkForBusy() (common/api/api_handler_editor.cpp:207) dereferences nullptr. The schematic handler still REGISTERS seven commands (GetOpenDocuments plus the six inherited BeginCommit, EndCommit, CreateItems, UpdateItems, DeleteItems, HitTest); GetOpenDocuments is the only one that works, and ALL SIX inherited commands (not just BeginCommit/CreateItems/HitTest) crash the hosting process -- normally kicad.exe, where eeschema runs as _eeschema.dll -- whenever the request is dispatched to the schematic handler. With pcbnew also open the crash is intermittent because the API server iterates a std::set<API_HANDLER*> ordered by pointer, so even board-targeted writes crash in a fraction of sessions (upstream #24966, filed against 10.0.4; duplicate #25322). Fixed on 2026-09-06 by commits 6545d4a2 (shadowed m_frame) and f3105844 (adds BeginCommit/EndCommit.document, Since 10.0.7) for milestone 10.0.7, which is not yet released as of 2026-09-18 (latest tag 10.0.6).

Sources:
- https://gitlab.com/kicad/code/kicad/-/issues/24966
- https://gitlab.com/kicad/code/kicad/-/issues/25322
- https://gitlab.com/kicad/code/kicad/-/commit/6545d4a28d4978edb96d038328500d8fde58b3dd
- https://gitlab.com/kicad/code/kicad/-/commit/f3105844c6dbe68bc3a0154f11c83e6de816c4ec
- https://gitlab.com/kicad/code/kicad/-/blob/10.0.0/eeschema/api/api_handler_sch.cpp
- https://gitlab.com/kicad/code/kicad/-/blob/10.0.0/eeschema/api/api_handler_sch.h
- https://gitlab.com/kicad/code/kicad/-/blob/10.0.0/common/api/api_handler_editor.cpp
- https://gitlab.com/kicad/code/kicad/-/blob/10.0.0/include/api/api_handler_editor.h
- https://gitlab.com/kicad/code/kicad/-/blob/10.0.0/common/api/api_server.cpp
- https://gitlab.com/kicad/code/kicad/-/blob/10.0.0/include/api/api_server.h
- https://gitlab.com/kicad/code/kicad/-/tags


## Claim 5 — CORRECTED

> Neither kicad-cli 7-10 nor the 9/10 IPC API can run DRC/ERC, import a netlist, place a library footprint by LIB_ID or export Specctra DSN; on 7-10 only SWIG pcbnew does the latter three headlessly.

### Empirical verifier — refuted=true

EMPIRICAL, run on this machine against KiCad 7.0.6 / 8.0.1 / 9.0.5 / 10.0.0 (scratchpad <scratchpad>/verify-cli-api-claim).

1) DRC/ERC via kicad-cli -- CLAIM FALSE for 8, 9, 10. `kicad-cli pcb drc --format json` and `kicad-cli sch erc --format json` exist and succeed headlessly on the ecc83 demo: 8.0.1 (drc8.json: 2 violations, erc8.json), 9.0.5 (drc9.json 2 violations, erc9.json 0 violations), 10.0.0 (drc10.json 2 violations, erc10.json). Exit code 0 each. kicad-cli 7.0.6 has only `pcb export {drill,dxf,gerber,gerbers,pdf,pos,step,svg}` and `sch export`: no drc/erc (`pcb drc` -> usage error). Jobset job types in 10.0 kicommon.dll also include pcb_drc and sch_erc.

2) Netlist import / DSN export / footprint-by-LIB_ID via kicad-cli -- CLAIM HOLDS. kicad-cli 10 `pcb export` list has no specctra/dsn; `pcb import` is only foreign-CAD board import (pads, altium, eagle, cadstar, fabmaster, pcad, solidworks); jobset job names (strings of kicommon.dll) contain no specctra/netlist-import/footprint-place job. Same for 7/8/9.

3) IPC API (9.0.5 and 10.0.0 kiapi.dll message list, plus live test against pcbnew 10.0.0 with API enabled, kicad-python 0.8.0): no RunDRC/ImportNetlist/ExportSpecctra/library-load messages exist (10 adds only InjectDrcError). `RunAction("pcbnew.DRCTool.runDRC")` returned RAS_OK but merely opened the interactive "Design Rules Checker" dialog; `RunAction("pcbnew.EditorControl.exportSpecctraDSN")` opened a modal "Specctra DSN File" save dialog and subsequent API calls timed out, so neither is headless. `CreateItems` with a FootprintInstance carrying only LibraryIdentifier Resistor_SMD:R_0603_1608Metric returned ISC_OK but created an EMPTY footprint (0 pads, 0 items) -- KiCad does not resolve the LIB_ID from the library. `ParseAndCreateItemsFromString` with .kicad_mod content (bare or wrapped in kicad_pcb) created nothing. kipy 0.8.0 ships RunBoardJob*/RunSchematicJob* protos (exports only: gerbers, drill, pdf, netlist EXPORT, etc.; still no DRC/DSN/netlist-import), and none of them are compiled into 10.0.0 binaries (0 RunJob hits in kiapi.dll). So the API part of the claim holds for 9.0.5/10.0.0.

4) "Only SWIG pcbnew does the latter three" -- PARTLY FALSE. SWIG 7/8/9/10 does `pcbnew.FootprintLoad(lib_path, name)` + board.Add (verified: R_0603 with 2 pads added, board saved) and `pcbnew.ExportSpecctraDSN(board, path)` (verified: 40-44 KB .dsn written in all four versions). BUT SWIG has NO netlist import: dir(pcbnew) and pcbnew.py in 9/10 contain no NETLIST/NETLIST_READER/BOARD_NETLIST_UPDATER/ReadNetlist symbols (only Get/SetIncludeGerberNetlistInfo). Also FootprintLoad takes a filesystem library path, not a LIB_ID nickname; no FP_LIB_TABLE wrapper is exposed, so nickname resolution must be done by the caller. Bonus finding: SWIG also exposes WriteDRCReport, but from standalone python it hits a wx assert dialog (10.0: pgm_base.cpp(1043) assert "process" failed in Pgm(); 9.0.5: wxDateTime::Format assert, empty report) and hangs, so kicad-cli is the practical headless DRC route.

Web: https://docs.kicad.org/master/en/cli/cli.html documents "pcb drc" and "sch erc" and no specctra/netlist-import command.

**Correction:** kicad-cli 8.0, 9.0 and 10.0 DO run DRC (`kicad-cli pcb drc`) and ERC (`kicad-cli sch erc`) headlessly with json/report output (verified on 8.0.1, 9.0.5, 10.0.0); only kicad-cli 7.0 lacks them. The KiCad 9/10 IPC API cannot run DRC/ERC, import a netlist, load a footprint from a library by LIB_ID (CreateItems with only a LibraryIdentifier yields an empty footprint) or export Specctra DSN; RunAction can only open the corresponding GUI dialogs. Neither kicad-cli nor the API can import a netlist, place a library footprint or export DSN. SWIG pcbnew (7-10) headlessly does two of the three: FootprintLoad(library_path, name) + BOARD.Add (path-based, caller must resolve the nickname from fp-lib-table) and ExportSpecctraDSN. SWIG pcbnew has NO netlist import API at all (no NETLIST_READER/BOARD_NETLIST_UPDATER wrapper in 7-10); netlist import into a board is not available headlessly through any of the three interfaces without writing your own importer (e.g. parse the netlist and use FootprintLoad + pad net assignment). SWIG's WriteDRCReport exists but hangs on a wx assert dialog from standalone python on Windows in 9.0.5/10.0.0.

Sources:
- https://docs.kicad.org/master/en/cli/cli.html
- local: kicad-cli.exe 7.0.6/8.0.1/9.0.5/10.0.0 --help output and DRC/ERC runs on share/kicad/demos/ecc83
- local: strings of C:/Program Files/KiCad/{9.0,10.0}/bin/kiapi.dll and kicommon.dll
- local: live pcbnew 10.0.0 IPC session with kicad-python 0.8.0 (KICAD_API_VERSION 10.0.6-0-gcaf7377e9c)
- local: C:/Program Files/KiCad/{7.0,8.0,9.0,10.0}/bin/python.exe SWIG pcbnew tests (FootprintLoad, ExportSpecctraDSN, WriteDRCReport, dir(pcbnew))
- scratchpad: <scratchpad>/verify-cli-api-claim/{drc8,drc9,drc10,erc8,erc9,erc10}.json, swig_{7,8,9,10}.dsn, apitest2.py, apitest3.py, step3.py

### Empirical verifier — refuted=true

1) DRC/ERC via kicad-cli is WRONG for 8/9/10. Ran locally: `kicad-cli pcb --help` / `sch --help` for 7.0.6 shows only {export}; 8.0.1, 9.0.5 and 10.0.0 all list `pcb drc` ("Runs the Design Rules Check (DRC) on the PCB and creates a report") and `sch erc` ("Runs the Electrical Rules Check (ERC)..."), with --format json|report, --severity-*, --exit-code-violations, --schematic-parity, --refill-zones/--save-board (10.0). Official 8.0 CLI docs document `pcb drc` and `sch erc`; the 8.0.0 release blog states "DRC and ERC can be run from the CLI ... JSON ... for use in CI pipelines". Source tree kicad/cli on 10.0 branch contains command_pcb_drc.cpp and command_sch_erc.cpp. Only KiCad 7 lacks it.
2) kicad-cli netlist import / footprint placement / Specctra DSN: CONFIRMED absent in 7-10. `kicad-cli pcb export` subcommand lists for 8/9/10 contain no specctra/dsn; `pcb import` (10.0) only converts foreign PCB formats (pads, altium, eagle, cadstar, fabmaster, pcad, solidworks); `sch export netlist` is export-only. kicad/cli on 10.0 branch has no specctra or netlist-import command files.
3) IPC API 9.0/10.0 branches (downloaded api/proto/* from gitlab 9.0 and 10.0 branches): no RunDrc/RunErc, ImportNetlist, PlaceFootprintFromLibrary, or Specctra export messages. schematic_commands.proto is header-only (24 lines) on both branches. 10.0 adds only InjectDrcError (inject a marker, not run DRC). CreateItems accepts a FootprintInstance but requires the client to supply the full `Footprint definition` (repeated items); FOOTPRINT::Deserialize (pcbnew/footprint.cpp, 10.0) just calls SetFPID() and deserializes the provided items, no library lookup. RunAction exists but proto comment warns "TOOL_ACTIONs are specifically *not* an API ... for low-level prototyping purposes only" and needs an open GUI frame. NOTE: master (KiCad 11 dev) HAS `ImportNetlist` (commit 6fd26c31, 2026-06-06) and `PlaceFootprintFromLibrary` (marked "Since: 11.0"), plus "API: Add protos for library management" (4437bf38, 2026-09-15); none of these were cherry-picked to the 10.0 branch (10.0 branch log ends with FlipItems/GetBoardLayerByName/embedded files).
4) SWIG "does the latter three": PARTIALLY WRONG. Bundled python for 7.0.6 and 10.0.0: `pcbnew.ExportSpecctraDSN(board, filename)` and `pcbnew.FootprintLoad(libPath, fpName)` exist, but there is NO netlist reader/updater exposed (no NETLIST, KICAD_NETLIST_READER, BOARD_NETLIST_UPDATER symbols; only LINE_READER classes) and no FP_LIB_TABLE class (only the FP_LIB_TABLE_T enum tag), so footprint loading is by library *path* + name, not by fp-lib-table LIB_ID resolution (nickname lookup must be done by the script). Netlist import in SWIG must be hand-scripted by parsing the netlist and adding footprints/nets.

**Correction:** Corrected statement: kicad-cli 7 cannot run DRC/ERC, but kicad-cli 8.0, 9.0 and 10.0 CAN (`kicad-cli pcb drc` and `kicad-cli sch erc`, JSON or text report, non-zero exit on violations). Neither kicad-cli 7-10 nor the released 9.x/10.x IPC API can import a netlist, place a library footprint by LIB_ID, or export Specctra DSN (the IPC API also cannot run DRC/ERC; 10.0 only adds InjectDrcError). Those IPC features (ImportNetlist, PlaceFootprintFromLibrary, library management) exist only on master and are marked "Since: 11.0". On 7-10 SWIG pcbnew headlessly provides Specctra export (ExportSpecctraDSN) and library footprint loading (FootprintLoad by library path + name; LIB_ID nickname must be resolved from fp-lib-table by the script), but SWIG exposes NO netlist-import function; netlist import must be hand-implemented in Python (or done via the GUI / KiCad 11 IPC API).

Sources:
- https://docs.kicad.org/8.0/en/cli/cli.html
- https://www.kicad.org/blog/2024/02/Version-8.0.0-Released/
- https://gitlab.com/kicad/code/kicad/-/work_items/13954
- https://gitlab.com/kicad/code/kicad/-/raw/10.0/api/proto/board/board_commands.proto
- https://gitlab.com/kicad/code/kicad/-/raw/9.0/api/proto/board/board_commands.proto
- https://gitlab.com/kicad/code/kicad/-/raw/10.0/api/proto/common/commands/editor_commands.proto
- https://gitlab.com/kicad/code/kicad/-/raw/10.0/api/proto/schematic/schematic_commands.proto
- https://gitlab.com/kicad/code/kicad/-/raw/10.0/pcbnew/footprint.cpp
- https://gitlab.com/kicad/code/kicad/-/raw/10.0/pcbnew/api/api_handler_pcb.cpp
- https://gitlab.com/kicad/code/kicad/-/raw/master/api/proto/board/board_commands.proto
- https://gitlab.com/kicad/code/kicad/-/commit/6fd26c31
- https://gitlab.com/kicad/code/kicad/-/commit/4437bf38
- https://gitlab.com/kicad/code/kicad/-/tree/10.0/kicad/cli
- https://docs.kicad.org/kicad-python-main/
- Local: C:/Program Files/KiCad/{7.0,8.0,9.0,10.0}/bin/kicad-cli.exe --help output and C:/Program Files/KiCad/{7.0,10.0}/bin/python.exe dir(pcbnew)


## Claim 6 — CORRECTED

> Each installed KiCad's bundled python.exe can headlessly build a board (FootprintLoad, NETINFO_ITEM, pad.SetNet, tracks/vias/zones, ZONE_FILLER after BuildConnectivity, SaveBoard) that kicad-cli DRC reports as 0 violations / 0 unconnected, provided one BOARD per process, outlines built via Outline() in place and wxTheAssertHandler nulled.

### Empirical verifier — refuted=true

Scratchpad: <scratchpad>/verify-headless-board (build_board.py, run_all.sh, multi_board.py, setoutline_scope.py, null_assert_test.py, build_nofill.py; outputs in k7.0/ k8.0/ k9.0/ k10.0/). Board: 30x20mm Edge.Cuts, nets GND/VCC/SIG via NETINFO_ITEM, R1/R2 R_0805_2012Metric + J1 PinHeader_1x02 from each version's own share/kicad/footprints via FootprintLoad, pad.SetNet, F.Cu tracks, 2 vias, B.Cu GND zone (Outline().NewOutline()/Append), BuildConnectivity, ZONE_FILLER.Fill, SaveBoard. Results with `kicad-cli pcb drc --format json --severity-all --all-track-errors`: 10.0.0 -> 0 violations/0 unconnected; 9.0.5 -> 0/0; 8.0.1 -> 0/0 (also 0/0 with default severity on 10.0). 7.0.6: python build OK (Fill True, SaveBoard True) but `kicad-cli pcb` has only `export`; DRC of that file by cli 8.0 -> 0/0, cli 9.0 -> 1 lib_footprint_mismatch warning, cli 10.0 -> 3 lib_footprint_mismatch warnings. Multi-board: multi_board.py building 3 boards per process (keep and del variants) on 10/9/8 all exit 0 and all 9 files DRC 0/0. SetOutline with poly out of scope (setoutline_scope.py): 10.0 and 9.0 print kimath util.cpp(61) overflow asserts then exit 139 (segfault); 8.0 exit 0 but OutlineCount()==0; with poly kept alive (build_board.py setoutline mode) all three give 0/0. Nulling wxTheAssertHandler via ctypes (null_assert_test.py): handler address read as non-null then set to None; assert text disappears but the use-after-free still segfaulted in 1 of 3 runs on 9.0 (exit 139). Without SetFPID, 8.0 DRC reported 2x lib_footprint_issues "The current configuration does not include the library ''" (9/10 did not). build_nofill.py (no BuildConnectivity, no ZONE_FILLER): 10/9/8 all report 1 violation (via_dangling) + 1 unconnected_items error, and the saved file has no filled_polygon. grep of pcbnew.py in 10.0 for wxTheAssertHandler/wxSetAssertHandler/wxDisableAsserts: 0 hits. No KiCad GUI was launched; an unrelated pre-existing eeschema.exe (PID 89232) was left untouched.

**Correction:** The core flow holds for KiCad 8.0.1, 9.0.5 and 10.0.0 but NOT for "each installed KiCad": KiCad 7.0.6's bundled python builds/fills/saves the same board fine, but kicad-cli 7.0 has no `pcb drc` subcommand at all (`kicad-cli pcb --help` lists only `export`), so no "kicad-cli DRC 0/0" result is obtainable with 7.0's own toolchain (the 7.0-built file passes 0/0 only under kicad-cli 8.0; under 9.0/10.0 it gets lib_footprint_mismatch warnings because the footprint libraries differ by version). The stated preconditions are also partly wrong/over-constrained: (1) "one BOARD per process" is unnecessary - 3 boards built and saved sequentially in one process (kept alive or deleted) on 8/9/10 all pass DRC 0/0; (2) "outlines via Outline() in place" is a real requirement only because ZONE.SetOutline(poly) takes ownership of the pointer: if python drops the SHAPE_POLY_SET, 9.0/10.0 segfault nondeterministically (use-after-free, kimath overflow asserts) and 8.0 silently produces an empty outline; SetOutline works if the poly object is kept referenced; (3) "wxTheAssertHandler nulled" is neither exposed by pcbnew.py nor needed - the clean flow triggers no asserts, and asserts that do fire print to stderr without a dialog or hang; it can be nulled via ctypes on the exported symbol `?wxTheAssertHandler@@3P6AXAEBVwxString@@H000@ZEA` in wxbase332u_vc_x64_custom.dll, which only silences the messages and does not prevent the crash. Two conditions the claim omits are actually required: fp.SetFPID(LIB_ID(lib, name)) must be set after FootprintLoad, otherwise kicad-cli 8.0 reports lib_footprint_issues warnings under --severity-all; and the ZONE_FILLER step is mandatory because kicad-cli DRC does not refill an unfilled zone (skipping it yields 1 unconnected_items error + via_dangling on 8/9/10).

Sources:
- <scratchpad>/verify-headless-board/build_board.py
- <scratchpad>/verify-headless-board/run_all.sh
- <scratchpad>/verify-headless-board/multi_board.py
- <scratchpad>/verify-headless-board/setoutline_scope.py
- <scratchpad>/verify-headless-board/null_assert_test.py
- <scratchpad>/verify-headless-board/build_nofill.py
- <scratchpad>/verify-headless-board/k10.0/drc_inplace.json
- <scratchpad>/verify-headless-board/k9.0/drc_inplace.json
- <scratchpad>/verify-headless-board/k8.0/drc_inplace.json
- C:/Program Files/KiCad/10.0/bin/Lib/site-packages/pcbnew.py
- C:/Program Files/KiCad/7.0/bin/kicad-cli.exe (pcb --help: only 'export')

### Empirical verifier — refuted=true

1) KiCad 7.0.6 has no kicad-cli DRC at all. Local: "C:/Program Files/KiCad/7.0/bin/kicad-cli.exe pcb --help" lists only {export}; 8.0/9.0/10.0 list drc. Docs: the 7.0 CLI reference (docs.kicad.org/7.0/en/cli/cli.html) documents only pcb export subcommands; the 8.0.0 release post states "DRC and ERC can be run from the CLI, and reports can be generated in a machine-readable format (JSON)". So for 7.0 the "kicad-cli DRC reports 0 violations" leg of the claim is impossible with that version's own CLI (only pcbnew.WriteDRCReport() from Python, or a newer kicad-cli, can DRC a 7.0 board).

2) The stated preconditions are incomplete: in 7.0.6, 8.0.1 and 9.0.5 ZONE_FILLER(board).Fill(board.Zones()) hard-crashes (Windows access violation, exit 139/0xC0000005, faulthandler points at pcbnew.py Fill) when the BOARD was created with pcbnew.BOARD() or pcbnew.CreateEmptyBoard(). The same script succeeds (Fill True, zone filled, SaveBoard OK) when the board comes from pcbnew.NewBoard(path). 10.0.0 succeeds either way. Source cause: 9.0 pcbnew/zone_filler.cpp dereferences bds.m_DRCEngine unconditionally (lines 1070-1164), and pcbnew_scripting_helpers.cpp only creates m_DRCEngine in LoadBoard()/NewBoard() (7.0: lines 192/238; 9.0: 249/295) but not in CreateEmptyBoard() (7.0:246, 9.0:303). 10.0 zone_filler.cpp lines 462-487 lazily create the DRC_ENGINE, which is why 10.0 tolerates it. Scripts at <scratchpad>/verify-headless-build/zf_variants.py and zonefill_smoke.py.

3) "wxTheAssertHandler nulled": pcbnew exposes no such symbol in any version (hasattr False for wxTheAssertHandler/wxSetAssertHandler; dir(pcbnew) has no *ssert* names). It is reachable only via bundled wxPython (wx 4.2.0/4.2.2 present in all four installs: wx.DisableAsserts(), which per wxWidgets include/wx/debug.h line 143 is wxSetAssertHandler(NULL)) or via ctypes on the mangled data export ?wxTheAssertHandler@@3P6AXAEBVwxString@@H000@ZEA in wxbase32u/wxbase332u_vc*_custom.dll (present in all four). It was not needed for any of the tested flows (7/8/9 NewBoard path and 10.0 both completed without it; my ctypes write was effectively a no-op).

4) Even where it works, the Python fill is not guaranteed: open upstream bug #24643 (filed 2026-06-15, KiCad 10.0.3, Win11) reports pcbnew.ZONE_FILLER.Fill() access-violating from the bundled python on a board the GUI fills fine; and dev-docs state the SWIG bindings "are deprecated as of KiCad 9.0", "unstable and will generally change between KiCad major versions", planned removal in 11.0.

5) DRC "0 violations" precision: kicad-cli drc's default severity is RPT_SEVERITY_ERROR | RPT_SEVERITY_WARNING (common/jobs/job_rc.cpp, 10.0), and --exit-code-violations returns 5 on any reported item, so warnings (e.g. isolated_copper, which my pad-less test zone triggered in 8/9/10, courtyard/silk warnings, etc.) count; the 0/0 outcome is design-dependent, not something the toolchain guarantees. Library-parity checks are skipped when the footprint's LIB_ID nickname is empty (drc_test_provider_library_parity.cpp: if( libName.IsEmpty() ) continue;), so FootprintLoad-from-path footprints do not by themselves cause lib warnings.

6) "after BuildConnectivity" is redundant (10.0 ZONE_FILLER::Fill() calls connectivity->Build itself) and "one BOARD per process" is not documented anywhere I could find; neither confirmed nor refuted.

**Correction:** Bundled python.exe in KiCad 8.0.1, 9.0.5 and 10.0.0 can headlessly build, zone-fill and save a board that its own kicad-cli DRC checks, and 7.0.6's python can build/fill/save too, BUT: (a) KiCad 7.0's kicad-cli has no `pcb drc` command (added in 8.0), so 7.0 can only be DRC'd via pcbnew.WriteDRCReport() or a newer kicad-cli; (b) on 7.0/8.0/9.0 the BOARD must come from pcbnew.NewBoard()/LoadBoard() (which attach a PROJECT and DRC_ENGINE) - ZONE_FILLER.Fill() access-violates on a pcbnew.BOARD()/CreateEmptyBoard() board because m_DRCEngine is null; only 10.0 creates the engine lazily; (c) wxTheAssertHandler is not exposed by pcbnew - use wx.DisableAsserts() from the bundled wxPython or ctypes on the wxbase DLL export, and it was not required in any tested flow; (d) "0 violations" means zero errors AND warnings under kicad-cli's default severity filter, which depends on the design, not the toolchain; (e) the SWIG API is deprecated since 9.0 (removal planned 11.0) and an open 10.0.3 bug (#24643) shows Python ZONE_FILLER.Fill can still crash on real boards, so the architecture should treat headless Python zone filling as best-effort with a per-version fallback (e.g. kicad-cli 10 `pcb drc --refill-zones --save-board`).

Sources:
- https://docs.kicad.org/7.0/en/cli/cli.html
- https://docs.kicad.org/8.0/en/cli/cli.html
- https://www.kicad.org/blog/2024/02/Version-8.0.0-Released/
- https://www.kicad.org/blog/2025/02/Version-9.0.0-Released/
- https://dev-docs.kicad.org/en/apis-and-binding/pcbnew/index.html
- https://gitlab.com/kicad/code/kicad/-/raw/9.0/pcbnew/zone_filler.cpp
- https://gitlab.com/kicad/code/kicad/-/raw/10.0/pcbnew/zone_filler.cpp
- https://gitlab.com/kicad/code/kicad/-/raw/7.0/pcbnew/python/scripting/pcbnew_scripting_helpers.cpp
- https://gitlab.com/kicad/code/kicad/-/raw/9.0/pcbnew/python/scripting/pcbnew_scripting_helpers.cpp
- https://gitlab.com/kicad/code/kicad/-/raw/10.0/pcbnew/python/scripting/pcbnew_scripting_helpers.cpp
- https://gitlab.com/kicad/code/kicad/-/raw/10.0/kicad/cli/command_pcb_drc.cpp
- https://gitlab.com/kicad/code/kicad/-/raw/10.0/common/jobs/job_rc.cpp
- https://gitlab.com/kicad/code/kicad/-/raw/10.0/common/jobs/job_pcb_drc.cpp
- https://gitlab.com/kicad/code/kicad/-/raw/10.0/pcbnew/drc/drc_test_provider_library_parity.cpp
- https://gitlab.com/kicad/code/kicad/-/work_items/24643
- https://forum.kicad.info/t/kicad-10-0-3-win11-pcbnew-zone-filler-fill-crashes-while-gui-zone-refill-succeeds/69787
- https://raw.githubusercontent.com/wxWidgets/wxWidgets/v3.2.8/include/wx/debug.h
- <scratchpad>/verify-headless-build/zf_variants.py
- <scratchpad>/verify-headless-build/zonefill_smoke.py


## Claim 7 — SURVIVED

> ExportSpecctraDSN(board, path) works headlessly on 7.0.6-10.0.0 and ImportSpecctraSES(board, path) on 8.0.1-10.0.0; SES import deletes all unlocked tracks/vias and applies SES placement.

### Empirical verifier — refuted=false

NOT REFUTED - every part of the claim was reproduced empirically with each KiCad version's bundled python.exe (no GUI, no wxApp), scratchpad <scratchpad>/specctra-verify (test.kicad_pcb / test_locked.kicad_pcb, export_dsn.py, import_ses.py, one.py, out_*.dsn, imported_*.kicad_pcb).

1) ExportSpecctraDSN(board, path): returned True and wrote a valid DSN on 7.0.6, 8.0.1, 9.0.5 and 10.0.0 (sizes 2200/2194/2193/2132 bytes). Locked tracks/vias are exported as "(type fix)", unlocked as "(type route)". Minor format differences per version (7.0 writes clearance 200.1 and 6-decimal placement, 10.0 writes integers; padstack names differ), all parseable.

2) ImportSpecctraSES(board, path): on 8.0.1, 9.0.5 and 10.0.0 returned True on a hand-written Freerouting-style SES (coordinates pre-multiplied by "(resolution um 10)", as Freerouting writes them). On 7.0.6 the 2-arg overload does not exist: "TypeError: ImportSpecctraSES() takes 1 positional argument but 2 were given" - the 7.0.6 helper only has ImportSpecctraSES(path) which needs the running PCB editor frame (returns False headlessly). This matches the claim's 8.0+ scoping (the BOARD* overload was added in 8.0; helper source confirms).

3) Deletion semantics: board had 5 track items (3 unlocked segments/vias, 1 locked segment, 1 locked via). After import on 8.0.1/9.0.5/10.0.0 all 3 unlocked items were gone, the locked segment and locked via survived unchanged, and the SES wires/via were added with the SES widths (0.25/0.3 mm) and via padstack (0.6 mm). Source (specctra_import.cpp, FromSESSION): aBoard->RemoveAll({PCB_TRACE_T}); for each track: if IsLocked() keep and re-add later, else delete.

4) Placement: R1 -> (25,25) rot 90 and R2 -> (45,25) flipped to B.Cu were applied on all three versions. Placement is applied to LOCKED footprints as well (R3 was locked and still moved to (35,45) rot 180); the placement loop has no IsLocked() check in 8.0.1 or 10.0.0 source. Placement section is optional (SES with only routes imports fine, footprints untouched). Import ends with BuildConnectivity(), so track net codes get re-propagated from touching pads.

Caveats an architecture must handle (do not refute the claim but are load-bearing for headless use):
a) Partial mutation on failure: SES with an unknown reference (R99) returned False on 10.0.0 (THROW_IO_ERROR "Reference '%s' not found" in 8.0.1 and 10.0.0 tags, caught by the SWIG helper), but unlocked tracks had ALREADY been deleted (5 -> 2 items) before the error - reload the board on any False return. Missing file and malformed SES: return False, board untouched.
b) Modal dialog hang: ExportSpecctraDSN on a board with no/invalid Edge.Cuts outline calls wxLogWarning("Board outline is malformed. Run DRC for a full analysis.") which in headless python pops a blocking wx "Message" box (process hung until I dismissed it via WM_CLOSE; after that it returned True and wrote a bounding-box boundary). Validate the outline first or run the export in a subprocess with a watchdog. Unwritable output path returns False cleanly.
c) Forward risk: the current 10.0 branch HEAD (post-10.0.0) changed unknown reference/layer/padstack handling to count them and emit wxLogWarning("%d session item(s) were skipped...") instead of throwing - in a future 10.0.x point release a partially-bad SES would therefore return True AND show a blocking dialog headlessly.
d) Unrelated to Specctra but hit during testing: on 9.0.5/10.0.0 calling PCB_VIA.GetWidth() without a layer argument raises a blocking "wxWidgets Debug Alert" assert dialog; use GetWidth(layer). Bundled python 9/10 also prints harmless "Adding duplicate image handler" lines to stderr.

**Correction:** Claim stands as written. Precise additions: (1) on 7.0.6 only the GUI-bound ImportSpecctraSES(path) exists, so SES import is headless-capable from 8.0 on, as claimed; (2) SES placement is applied to locked footprints too (no IsLocked check); (3) a failed import (unknown reference) returns False after the unlocked tracks/vias have already been deleted; (4) DSN export with a malformed/missing board outline blocks on a modal wxLogWarning dialog even headlessly.

Sources:
- https://gitlab.com/kicad/code/kicad/-/raw/10.0.0/pcbnew/specctra_import_export/specctra_import.cpp
- https://gitlab.com/kicad/code/kicad/-/raw/10.0/pcbnew/specctra_import_export/specctra_import.cpp
- https://gitlab.com/kicad/code/kicad/-/raw/8.0.1/pcbnew/specctra_import_export/specctra_import.cpp
- https://gitlab.com/kicad/code/kicad/-/raw/10.0/pcbnew/specctra_import_export/specctra_export.cpp
- https://gitlab.com/kicad/code/kicad/-/raw/10.0/pcbnew/python/scripting/pcbnew_scripting_helpers.cpp
- https://gitlab.com/kicad/code/kicad/-/raw/7.0.6/pcbnew/python/scripting/pcbnew_scripting_helpers.cpp
- Local tests: <scratchpad>/specctra-verify/{export_dsn.py,import_ses.py,one.py,test.ses,test_noplace.ses,badref.ses,out_7.0.dsn,out_10.0.dsn,imported_8.0.kicad_pcb,imported_9.0.kicad_pcb,imported_10.0.kicad_pcb,one_*.log}

### Empirical verifier — refuted=false

DOCUMENTARY (KiCad source at the exact tags/branches, pcbnew/python/scripting/pcbnew_scripting_helpers.cpp): tags 7.0.6 and 7.0.11 contain `bool ExportSpecctraDSN( BOARD* aBoard, wxString& aFullFilename )` (calls ExportBoardToSpecctraFile(aBoard, path) inside try/catch(...) -> false; no frame, no dialog) but only the frame-based `ImportSpecctraSES( wxString& )`, which returns false when s_PcbEditFrame is null (i.e. always in standalone python). Tags 8.0.0, 8.0.1 and 10.0.0 (and branch 9.0) add `bool ImportSpecctraSES( BOARD* aBoard, wxString& aFullFilename )` = `try { DSN::ImportSpecctraSession( aBoard, path ); } catch(...) { return false; } return true;`. DSN::ImportSpecctraSession(BOARD*, path) (specctra_import.cpp, branches 8.0/9.0/10.0) is `SPECCTRA_DB db; LOCALE_IO toggle; db.LoadSESSION(); db.FromSESSION(aBoard); aBoard->GetConnectivity()->ClearRatsnest(); aBoard->BuildConnectivity();` - no frame/GUI. ExportBoardToSpecctraFile (specctra_export.cpp 7.0 and 10.0) only uses BOARD*, LOCALE_IO, SynchronizeNetsAndNetClasses, FromBOARD, ExportPCB; the only DisplayErrorMessage is in the PCB_EDIT_FRAME member, not the board overload; a malformed outline only emits wxLogWarning. SPECCTRA_DB::FromSESSION: 8.0 pops every item of aBoard->Tracks() (this list holds PCB_TRACK, PCB_ARC and PCB_VIA), keeps those with IsLocked() in a `locked` vector and deletes the rest, then DeleteMARKERs() and re-adds the locked ones; 9.0/10.0 do the same via `aBoard->RemoveAll({ PCB_TRACE_T })` (BOARD::RemoveAll in board.cpp 10.0 clears m_tracks and asserts "Use PCB_TRACE_T to remove all tracks, arcs, and vias") then re-add locked. Placement: if m_session->placement exists, for each place with a vertex: FindFootprintByReference -> SetPosition(mapPt(vertex)), Flip if side mismatches, SetOrientation(rotation, +180 for back). Then routes/network_out wires and vias are added. Caveats (not refutations): (a) in 8.0/9.0 a placement reference not found on the board THROWs "Reference '%s' not found." -> wrapper returns False, but by then the unlocked tracks were already deleted (board left mutated); 10.0 instead counts it as `skipped` and continues (wxLogWarning "%d session item(s) were skipped..."). (b) Missing "session"/"routes"/"library_out" sections throw -> False. (c) The wrapper swallows all exceptions, so the only failure signal is the bool. LOCAL TEST (bundled python.exe of each install, KICAD_CONFIG_HOME pointed at scratchpad, no GUI): script <scratchpad>/verify-specctra/test_specctra.py builds a board (R1 from Resistor_SMD.pretty, outline, unlocked track (10,10)-(20,10), locked track (30,30)-(40,30), unlocked via at (20,10)), exports DSN, then imports a hand-written SES placing R1 at (70,80) back side rot 90 with a wire (10,10)-(30,10) and a via at (30,10). Results: 7.0.6 ExportSpecctraDSN(board,path)->True (2851-byte DSN); ImportSpecctraSES(board,path) raises TypeError (1-arg only). 8.0.1 / 9.0.5 / 10.0.0: ExportSpecctraDSN->True; ImportSpecctraSES->True; after import tracks = [VIA (30,10) unlocked, TRACK (10,10)-(30,10) unlocked (new), TRACK (30,30)-(40,30) locked (kept)]; old unlocked track and via gone; R1 pos (70,80), layer B_Cu (31 on 8.0, 2 on 9.0/10.0 new layer numbering), orientation -90 (SES 90 + 180 on back = 270 = -90).

**Correction:** Claim stands. Precision notes for the architecture: (1) On 7.0.x, only the 1-arg frame-based ImportSpecctraSES exists and it returns False headlessly (no board overload until 8.0.0). (2) ImportSpecctraSES(board, path) returns False for any IO_ERROR, and on 8.0/9.0 a SES placement reference that is not on the board throws AFTER unlocked tracks/vias were already deleted - reload the board on False; 10.0 skips unresolved references/layers/padstacks with a wxLogWarning instead. (3) "Tracks" deleted include PCB_TRACK, PCB_ARC and PCB_VIA (all live in BOARD::Tracks()); DRC markers are also deleted; zones, footprints and drawings are untouched. (4) Placement is applied only when the SES has a (placement ...) section and the place has a vertex; back-side parts get rotation+180 and are flipped.

Sources:
- https://gitlab.com/kicad/code/kicad/-/raw/7.0.6/pcbnew/python/scripting/pcbnew_scripting_helpers.cpp
- https://gitlab.com/kicad/code/kicad/-/raw/7.0.11/pcbnew/python/scripting/pcbnew_scripting_helpers.cpp
- https://gitlab.com/kicad/code/kicad/-/raw/8.0.0/pcbnew/python/scripting/pcbnew_scripting_helpers.cpp
- https://gitlab.com/kicad/code/kicad/-/raw/8.0.1/pcbnew/python/scripting/pcbnew_scripting_helpers.cpp
- https://gitlab.com/kicad/code/kicad/-/raw/10.0.0/pcbnew/python/scripting/pcbnew_scripting_helpers.cpp
- https://gitlab.com/kicad/code/kicad/-/raw/8.0/pcbnew/specctra_import_export/specctra_import.cpp
- https://gitlab.com/kicad/code/kicad/-/raw/9.0/pcbnew/specctra_import_export/specctra_import.cpp
- https://gitlab.com/kicad/code/kicad/-/raw/10.0/pcbnew/specctra_import_export/specctra_import.cpp
- https://gitlab.com/kicad/code/kicad/-/raw/7.0/pcbnew/specctra_import_export/specctra_import.cpp
- https://gitlab.com/kicad/code/kicad/-/raw/7.0/pcbnew/specctra_import_export/specctra_export.cpp
- https://gitlab.com/kicad/code/kicad/-/raw/10.0/pcbnew/specctra_import_export/specctra_export.cpp
- https://gitlab.com/kicad/code/kicad/-/raw/10.0/pcbnew/board.cpp
- Local: C:/Program Files/KiCad/{7.0,8.0,9.0,10.0}/bin/Lib/site-packages/pcbnew.py (SWIG signatures: 7.0 ImportSpecctraSES(aFullFilename) only; 8.0/9.0/10.0 have the (BOARD, wxString) overload)
- Local test script and outputs: <scratchpad>/verify-specctra/


## Claim 8 — CORRECTED

> Freerouting 2.4.1 requires Java 25 and, with automatic_neckdown=false and fanout.enabled=false, routes a 30-net 2-layer board to 0 KiCad DRC violations / 0 unconnected, and kicad-cli 10 `pcb drc --refill-zones --save-board` removes phantom clearance errors caused by stale fills.

### Empirical verifier — refuted=true

All three sub-claims were reproduced on this machine (KiCad 10.0.0 and 9.0.5, Freerouting 2.4.1 jar sha256 251101c3..., Temurin JRE 25.0.4.1). Every measurable outcome in the claim holds, but the causal attribution to automatic_neckdown is wrong, so the claim is only partially correct.

(1) Java 25 - CONFIRMED. The release jar's own classes are class-file major version 69 (= Java 25; 910 classes at v69, remaining 2079 at v52 are bundled libs); MANIFEST Created-By 25.0.4.1. Running `java -jar freerouting-2.4.1.jar` fails with UnsupportedClassVersionError ("class file version 69.0") on Java 8 (1.8.0_503), 11 (11.0.17) and 17 (17.0.12); it runs on Temurin 25.0.4.1. Release notes for 2.4.x state "Fully upgraded to Java 25". Note: the env note said "Java 17 on PATH" but `java` on PATH is actually Oracle JRE 1.8.0_503 (JAVA_HOME points to Temurin JRE 17); neither works. The Windows MSI bundles its own runtime, so Java 25 is only a hard requirement for the .jar.

(2) Routing to 0 DRC / 0 unconnected - RESULT CONFIRMED, ATTRIBUTION REFUTED. Built an independent 30-net 2-layer board with KiCad 10 SWIG (TQFP-32 + SOIC-16 + 2x8 header + 6R + 4C, 48 unrouted connections, DSN rule width 200um/clearance 200um) at .../scratchpad/freerouting-verify/b30/, plus re-ran the earlier "medium" 30-net board. Headless run: `java -jar freerouting-2.4.1.jar -de b30.dsn -do routed.ses --gui.enabled=false -mp 100 [flags]`, import via pcbnew.ImportSpecctraSES (KiCad 10), then `kicad-cli pcb drc --format json --severity-all --all-track-errors`.
  - defaults: 0 unrouted, but 27 (b30) / 26 (medium) `track_width` ERRORS: 0.15 mm segments vs board min width 0.20 mm. Tracks: 218 seg/21 vias.
  - --router.automatic_neckdown=false ONLY: identical 27 / 26 track_width errors (0.15 mm segments still present).
  - --router.fanout.enabled=false ONLY: 0 violations, 0 unconnected (181 seg/14 vias, all 0.2 mm).
  - both flags: 0 violations, 0 unconnected; 4 repeat runs identical (181 seg/14 vias). Freerouting's own report: "0 unrouted and 0 violations", score 999.99, ~2.5 s.
  The freerouting.log shows the 0.15 mm traces come from the fanout pre-pass, which is enabled by default in 2.4.1 (fr_result.json settings_snapshot fanout.enabled=true; log: "Fanout stage started ... 62 of 68 SMD pins needing fanout ... +35 extra vias"). automatic_neckdown=false is neither necessary nor sufficient; fanout.enabled=false is the flag that matters. Also, the track_width errors only exist because KiCad 10's BOARD() default min_track_width is 0.2 mm; the same script under KiCad 9.0.5 gives min_track_width 0.0, and there even the default Freerouting config yields 0 violations / 0 unconnected after a zone refill.

(3) --refill-zones --save-board - CONFIRMED on KiCad 10.0.0. Board variant with a filled GND pour on B.Cu (ZONE_FILLER, then routed with both flags, SES imported without refilling): plain `kicad-cli pcb drc` -> 44 errors (30 clearance + 14 hole_clearance, every one against "Zone 'GNDPOUR' [GND] on B.Cu", "zone clearance 0.25 mm; actual 0.0000 mm"); no "fill out of date" warning is emitted, so they are indistinguishable from real errors. `--refill-zones` -> 0 violations / 0 unconnected (file unchanged, sha256 identical). `--refill-zones --save-board` -> 0 violations, "Saved board", file sha256 changed; a subsequent plain DRC on the saved file -> 0 violations. `--save-board` without `--refill-zones` silently does nothing (44 errors, no save). kicad-cli 9.0.5 has NO --refill-zones/--save-board options; the same stale board gives 44 errors there and must be refilled via pcbnew.ZONE_FILLER in the bundled python (after which DRC = 0).

Artifacts: <scratchpad>/freerouting-verify/ (make_board30.py, run_fr.sh, run_fr9.sh, import_ses.py, fill_zone.py, runs/<tag>/{freerouting.log,fr_result.json,routed.ses,routed.kicad_pcb,drc*.json}). No GUI was launched; the pcbnew.exe/eeschema.exe processes visible in tasklist were not started by me and were left alone.

**Correction:** Freerouting 2.4.1's .jar requires Java 25 or newer (class-file 69; fails on 8/11/17; the MSI bundles its own runtime). With `--router.fanout.enabled=false` it routes a 30-net 2-layer board (two different boards tested, 4 repeats) to 0 KiCad 10 DRC violations / 0 unconnected; adding `--router.automatic_neckdown=false` is harmless but has no effect - neckdown=false alone still leaves 26-27 track_width errors, because the 0.15 mm segments come from the fanout pre-pass (enabled by default in 2.4.1), not from neckdown. Those track_width errors only arise against KiCad 10's new default min_track_width of 0.2 mm (KiCad 9 defaults to 0.0, where even default Freerouting settings pass DRC). kicad-cli 10.0.0 `pcb drc --refill-zones --save-board` does remove the phantom zone clearance/hole_clearance errors caused by stale fills (44 -> 0) and persists the refilled board; kicad-cli 9.0.5 lacks both flags, so on 9.x zones must be refilled via pcbnew.ZONE_FILLER instead.

Sources:
- https://github.com/freerouting/freerouting/releases/tag/v2.4.1
- https://github.com/freerouting/freerouting/releases/download/v2.4.1/freerouting-2.4.1.jar (sha256 251101c3eeac22d7e7dfcf6796603279e5d1000283eb82d8f093780f7afc6aa9)
- https://github.com/freerouting/freerouting/blob/master/docs/settings.md
- https://docs.kicad.org/10.0/en/cli/cli.html (kicad-cli pcb drc --refill-zones / --save-board; verified via local `kicad-cli pcb drc --help` on 10.0.0 and 9.0.5)
- Local test scripts and outputs: <scratchpad>/freerouting-verify/

### Documentary verifier — refuted=true

PART 1 - "Freerouting 2.4.1 requires Java 25": CONFIRMED. (a) GitHub release v2.4.1 (published 2026-09-03, tag commit ae3d377) says "Fully upgraded to Java 25, Gradle 9.7.1"; release v2.2.0 (2026-04-27) says "The project now requires and builds with Java 25". (b) build.gradle at tag v2.4.1: sourceCompatibility/targetCompatibility = JavaVersion.VERSION_25, toolchain languageVersion 25. (c) I downloaded freerouting-2.4.1.jar (64,076,787 bytes) into the scratchpad and read the class headers: all app/freerouting/*.class files are class-file major version 69 (= Java 25); MANIFEST.MF says Created-By: 25.0.4.1 (Eclipse Adoptium 25.0.4.1+1-LTS), Build-Date 2026-09-03. (d) README install steps say pick JRE "version 25". Side note for the architecture: the `java` on this machine's PATH is 1.8.0_503 (not the Java 17 the environment notes claim), so the JAR cannot run here without a separate Java 25 JRE (the Windows .msi bundles one).

PART 2 - "with automatic_neckdown=false and fanout.enabled=false, routes a 30-net 2-layer board to 0 KiCad DRC violations / 0 unconnected": NOT ESTABLISHED and partly contradicted by the project's own records. What IS confirmed: both settings exist in 2.4.1 source (RouterSettings.java: @SerializedName("automatic_neckdown"), FanoutSettings.java: @SerializedName("enabled") reached as router.fanout.enabled; docs/settings.md documents both; DefaultSettings.java defaults both to true, strict_drc to false, copper_to_edge_clearance_um to 500). What is NOT in any document: any statement that disabling neckdown/fanout yields DRC-clean output, or any claim of "0 KiCad DRC violations" on any board. No official KiCad or Freerouting doc, release note, or benchmark doc (docs/Benchmarks.md is empty at v2.4.1) makes this promise; it is a board-specific empirical outcome. Contrary evidence: (i) Issue #858 (opened 2026-08-28, closed 2026-09-17 as "resolved in master") reports that 2.3.x ships "a small number of real routed-wire-over-pad overlaps ... in the final .ses (~0.029 mm each) ... what originally broke our downstream DRC" on a 2-layer, 19-net KiCad 10.0.4 board; the maintainer's fix is commit b32877f9 "fix(drc): eliminate router clearance violations and add pass-aware strict DRC" dated 2026-09-11, and GitHub compare v2.4.1...b32877f9 reports status "ahead", ahead_by 85 - i.e. the fix is NOT in 2.4.1 and is slated for v2.5.0. (ii) v2.4.1 release "Known Limitations": KiCad DSN exports omit copper-to-edge clearance (#558); Freerouting substitutes its own default (500 um), which does not necessarily match the KiCad board's edge-clearance rule, so KiCad edge-clearance DRC results depend on the user setting --router.copper_to_edge_clearance_um. (iii) Issue #575 (closed, milestone 2.2) documents that Freerouting's internal DRC yields "false negatives" relative to KiCad's DRC, and strict_drc (opt-in, default false) exists precisely because routing can introduce clearance violations. So the "0 DRC / 0 unconnected" figure cannot be treated as a property of 2.4.1 + those two flags; at best it is a single-board observation that must be re-verified per board with kicad-cli.

PART 3 - "kicad-cli 10 pcb drc --refill-zones --save-board removes phantom clearance errors caused by stale fills": CONFIRMED with a wording nuance. docs.kicad.org/10.0/en/cli/cli.html and kicad/cli/command_pcb_drc.cpp at tag 10.0.0 define --refill-zones ("Refill zones before running DRC") and --save-board ("Save the board after DRC, must be used with --refill-zones"); local `kicad-cli.exe pcb drc --help` (10.0.0 install) prints the same. pcbnew/pcbnew_jobs_handler.cpp at 10.0.0 (lines ~2404-2490): if m_refillZones, ZONE_FILLER_TOOL::FillAllZones() runs BEFORE drcEngine->RunTests(); SaveBoard() runs only if m_refillZones && m_saveBoard. Why stale fills cause clearance errors: DRC copper-clearance tests build the zone R-tree from ZONE::GetEffectiveShape(), which returns the stored m_FilledPolysList (pcbnew/zone.cpp, drc_cache_generator.cpp, drc_test_provider_copper_clearance.cpp testItemAgainstZone), so a fill computed before the Freerouting tracks were imported collides with the new tracks and produces DRCE_CLEARANCE items; refilling recomputes the fill with clearance around the new tracks and those items disappear. Nuance: these are not "phantom" in the sense of a DRC bug - DRC is correctly evaluating the stored (stale) fill geometry; and refilling only removes fill-vs-track/pad conflicts, not genuine track-vs-track or track-vs-pad violations that Freerouting itself may have produced. These two flags are absent from command_pcb_drc.cpp at tag 9.0.0, so they are a KiCad 10 feature.

LOCAL CHECKS DONE (light, not a routing test): JAR class-version/manifest inspection and kicad-cli 10 help output. NOT tested locally: routing any board with Freerouting or running the DRC refill flow; no Java 25 JRE is on this machine.

**Correction:** Freerouting 2.4.1 does require Java 25 (class-file version 69; build.gradle toolchain 25; release notes since v2.2.0). The settings router.automatic_neckdown and router.fanout.enabled exist (both default true), but no official documentation or release note states that disabling them yields DRC-clean output; "0 KiCad DRC violations / 0 unconnected on a 30-net 2-layer board" is a single-board empirical observation, not a documented property. Freerouting's own tracker shows 2.3.x-2.4.x can emit genuinely router-introduced clearance violations (~0.029 mm wire-over-pad overlaps, issue #858), and the fix commit b32877f9 (2026-09-11, "eliminate router clearance violations") is 85 commits after the v2.4.1 tag and targeted at v2.5.0; additionally KiCad DSN export omits copper-to-edge clearance (#558), so Freerouting's 500 um default must be aligned with the board's rule via --router.copper_to_edge_clearance_um. The architecture must therefore re-run kicad-cli DRC on every routed board and cannot assume zero violations. The kicad-cli 10 part is correct: `pcb drc --refill-zones --save-board` refills all zones before RunTests() and then saves the board (save only happens when both flags are given; flags are new in 10.0, absent in 9.0). The clearance errors it removes are real DRC evaluations of stale stored fill polygons (ZONE::GetEffectiveShape returns m_FilledPolysList), not a DRC bug, and refilling only clears fill-related conflicts, not track/pad violations introduced by the router.

Sources:
- https://github.com/freerouting/freerouting/releases/tag/v2.4.1
- https://github.com/freerouting/freerouting/releases/tag/v2.2.0
- https://github.com/freerouting/freerouting/releases/tag/v2.3.0
- https://github.com/freerouting/freerouting/releases/download/v2.4.1/freerouting-2.4.1.jar
- https://raw.githubusercontent.com/freerouting/freerouting/v2.4.1/build.gradle
- https://raw.githubusercontent.com/freerouting/freerouting/v2.4.1/src/main/java/app/freerouting/settings/RouterSettings.java
- https://raw.githubusercontent.com/freerouting/freerouting/v2.4.1/src/main/java/app/freerouting/settings/FanoutSettings.java
- https://raw.githubusercontent.com/freerouting/freerouting/v2.4.1/src/main/java/app/freerouting/settings/sources/DefaultSettings.java
- https://raw.githubusercontent.com/freerouting/freerouting/v2.4.1/docs/settings.md
- https://github.com/freerouting/freerouting
- https://github.com/freerouting/freerouting/issues/858
- https://github.com/freerouting/freerouting/commit/b32877f9331b6293e393eb9977c595b9fc369c84
- https://api.github.com/repos/freerouting/freerouting/compare/v2.4.1...b32877f93
- https://github.com/freerouting/freerouting/issues/575
- https://github.com/freerouting/freerouting/issues/558
- https://github.com/freerouting/freerouting/issues/728
- https://docs.kicad.org/10.0/en/cli/cli.html
- https://gitlab.com/kicad/code/kicad/-/blob/10.0.0/kicad/cli/command_pcb_drc.cpp
- https://gitlab.com/kicad/code/kicad/-/blob/9.0.0/kicad/cli/command_pcb_drc.cpp
- https://gitlab.com/kicad/code/kicad/-/blob/10.0.0/pcbnew/pcbnew_jobs_handler.cpp
- https://gitlab.com/kicad/code/kicad/-/blob/10.0.0/pcbnew/zone.cpp
- https://gitlab.com/kicad/code/kicad/-/blob/10.0.0/pcbnew/drc/drc_cache_generator.cpp
- https://gitlab.com/kicad/code/kicad/-/blob/10.0.0/pcbnew/drc/drc_test_provider_copper_clearance.cpp
- https://forum.kicad.info/t/refill-zones-via-cli-or-custom-drc-rule-for-out-of-date-zones/52649


## Claim 9 — CORRECTED

> A file written in KiCad N's format loads on every newer kicad-cli and fails on every older one (exit 3/127), and a single unknown token makes the 7.0/8.0 parsers reject the file.

### Empirical verifier — refuted=true

All experiments in <scratchpad>/fmt-compat (demo7.0/8.0/9.0 = shipped ecc83 demos stamped 20221018/20240108/20241229 pcb and 20230121/20231120/20250114 sch; native10.0 = same demo run through kicad-cli 10 'pcb upgrade'/'sch upgrade' -> stamps 20260206/20260306; the shipped 10.0 demos still carry 9.0 stamps). Cross-version matrix via 'pcb export svg' / 'sch export netlist': fmt7->cli 7/8/9/10 rc=0; fmt8->cli7 rc=127(pcb)/3(sch), cli8/9/10 rc=0; fmt9->cli7,8 rc=127/3, cli9,10 rc=0; fmt10->cli7,8 rc=127 (pcb, "created with a more recent version... upgrade to version 10.0") /3 (sch), cli9 rc=3 ("Failed to load board"/"Failed to load schematic"), cli10 rc=0. Stamp-only probes (7.0 body, stamp edited): PCB cli7.0 rc=0 for stamps 20230101..20240109 (no gate); cli8.0 rc=0 at 20240108, 127 at 20240109+; cli9.0 rc=0 at 20241229, 3 at 20241230+; cli10.0 rc=0 at 20260206, 3 at 20260207+. SCH: cli7.0 rc=0 at 20230121, 3 at 20230122+; cli8.0/9.0/10.0 rc=0 even at 20250114/20260306/20260307 when no generator_version token; adding (generator_version "10.0") or "9.0" with stamp 20260306 -> rc=3 on 8.0 and 9.0; stamp 20250114 + generator_version "10.0" -> rc=0 on 9.0. Source confirms: 9.0 sch parser calls checkVersion() lambda (throws FUTURE_FORMAT_ERROR if m_requiredVersion > SEXPR_SCHEMATIC_FILE_VERSION) after T_generator_version processing; 8.0 pcb parser checks in parseBOARD_unchecked via checkVersion(). Unknown-token (zzz_unknown 1) injection into 7.0 PCB: top-level -> 7.0/8.0 "Unknown token" rc=127, 9/10 rc=3; setup -> "Unexpected" rc=127/3; footprint/pad/segment/zone -> "Expecting '...'" rc=127/3; layers -> "need a number" rc=127/3; BUT inside (general), inside (pcbplotparams), inside stackup (layer ...) -> rc=0 with SVG produced on 7.0, 8.0, 9.0 and 10.0. Real newer tokens: (generator_version "8.0") in 7-stamped pcb -> 7.0 rc=127 "Unknown token 'generator_version'", 8.0/9.0/10.0 rc=0; (embedded_fonts no) -> 7.0 and 8.0 rc=127, 9.0/10.0 rc=0. Unknown-token injection into 7.0 SCH at 8 locations -> rc=3 on all four versions; (generator_version "8.0") in sch -> 7.0 rc=3, 8/9/10 rc=0; (embedded_fonts no) in sch -> 7.0/8.0 rc=3, 9/10 rc=0. Minimal board/schematic upgraded by KiCad 10 (min10.kicad_pcb adds legacy_teardrops, tenting/covering/plugging/capping/filling, generator_version; min10.kicad_sch adds generator_version + embedded_fonts) fail on 7/8/9 as expected. Not tested: .kicad_sym/.kicad_mod/.kicad_pro, GUI apps, KiCad 5/6-era formats, kicad-cli 8.0.x/9.0.x point releases other than 8.0.1/9.0.5.

**Correction:** Partially true. What holds (tested on 7.0.6/8.0.1/9.0.5/10.0.0 with real files): (a) genuine files written by KiCad 7/8/9 load on every newer kicad-cli (16/16 PCB+SCH combos rc=0); (b) genuine files written by a newer KiCad fail on every older kicad-cli. Exit codes are NOT uniformly "3/127": kicad-cli 7.0/8.0 return 127 for PCB failures and 3 for schematic failures; 9.0/10.0 return 3 for both. What is wrong or over-stated: (1) The rejection is not driven by the version stamp in a uniform way. PCB 7.0 has NO upfront stamp gate: a 7.0-body board stamped 20240108/20240109 loads fine on 7.0.6; the "created with a more recent version" message (rc 127) appears only when a parse error hits AND the stamp is newer. PCB 8.0/9.0/10.0 do gate on the stamp alone (8.0.1 rejects stamp >20240108, 9.0.5 >20241229, 10.0.0 >20260206). Schematic 7.0 gates on stamp alone (>20230121), but schematic 8.0/9.0/10.0 only run the future-version check inside the (generator_version ...) branch: a schematic with a future stamp but no generator_version token loads on 8/9/10, while the same file plus (generator_version "x") is rejected. So "fails on every older one" is true only for files that carry the tokens a real KiCad writes; hand-edited/synthesized files can slip through. (2) "A single unknown token makes 7.0/8.0 reject the file" is true for the schematic parser in every location tested (top level, title_block, junction, wire, symbol, lib_symbols, property, sheet_instances) and for most PCB locations (top level, layers, setup, footprint, pad, segment, zone), but is FALSE inside PCB (general ...), (setup (pcbplotparams ...)) and stackup (layer ...) blocks, where all four parsers silently skip unknown tokens (source: parseGeneralSection default branch "Skip everything else"; parseBoardStackup default skipCurrent()). Also, a token unknown to 7.0 is not necessarily unknown to 8.0 (e.g. generator_version: rejected by 7.0, accepted by 8.0), so "7.0/8.0" cannot be treated as one parser.

Sources:
- https://gitlab.com/kicad/code/kicad/-/raw/9.0/eeschema/sch_io/kicad_sexpr/sch_io_kicad_sexpr_parser.cpp
- https://gitlab.com/kicad/code/kicad/-/raw/8.0/pcbnew/pcb_io/kicad_sexpr/pcb_io_kicad_sexpr_parser.cpp
- Local runs of C:/Program Files/KiCad/{7.0,8.0,9.0,10.0}/bin/kicad-cli.exe (7.0.6, 8.0.1, 9.0.5, 10.0.0) on files in <scratchpad>/fmt-compat

### Empirical verifier — refuted=true

LOCAL TESTS (scratchpad <scratchpad>/verify-format-compat, logs in out/). Boards created with each version's bundled python pcbnew.SaveBoard: stamps 7.0=20221018, 8.0=20240108, 9.0=20241229, 10.0=20260206. Schematics: ecc83 demo per install (7.0=20230121, 8.0=20231120, 9.0=20250114) plus 10.0 `kicad-cli sch upgrade --force` -> 20260306. Matrix via `pcb export svg` / `sch export netlist`: newer-or-equal CLI on any file = exit 0 (all 20 combos). Older CLI on newer file: pcb 7.0/8.0 -> Git Bash 127 / PowerShell $LASTEXITCODE=-1 with wx "Error: KiCad was unable to open this file because it was created with a more recent version..." (7.0's "Full error text" shows the real cause: "Unknown token 'generator_version'"); pcb 9.0 -> exit 3 "Failed to load board"; sch 7.0/8.0/9.0 -> exit 3 "Failed to load schematic (file)". Probe files: A (7.0 content, stamp 20260206): pcb 7.0 exit 0, 8.0 exit -1/127, 9.0 exit 3, 10.0 exit 0; sch A (7.0 content, stamp 20260306, no generator_version): 7.0 exit 3, 8.0 exit 0, 9.0 exit 0, 10.0 exit 0. B (10.0 content, stamp 20221018): 7.0 fails on 'generator_version', 8.0 fails on Unexpected 'tenting'. Unknown token (bogus_token 42): top-level -> reject on all 4 (7.0/8.0 "Unknown token 'bogus_token'"); inside (general) -> exit 0 on all 4; inside (pcbplotparams) -> exit 0 on all 4; inside (setup) -> reject all 4 ("Unexpected 'bogus_token'"); inside (segment) -> reject all 4 ("Expecting 'start, end, width, layer, net, tstamp, or locked'"); schematic top-level and inside (symbol) -> exit 3 on all 4. SOURCE (downloaded from gitlab branches 7.0/8.0/9.0/10.0): pcb_parser.cpp 7.0 parseBOARD() wraps parseBOARD_unchecked in try/catch(PARSE_ERROR) and only then throws FUTURE_FORMAT_ERROR if m_tooRecent; parseGeneralSection default branch comment "Skip everything but the board thickness." 8.0/9.0/10.0 pcb_io_kicad_sexpr_parser.cpp add lambda checkVersion() called at T_generator_version and T_general ("Do another version check here, for older files that do not include generator_version"). 8.0 pcbnew/pcb_plot_params.cpp: "default: skipCurrent(); // skip unknown or outdated plot parameter". 7.0 sch_sexpr_parser.cpp parseHeader(): "if( m_requiredVersion > aFileVersion ) throw FUTURE_FORMAT_ERROR". 8.0/9.0 sch_io_kicad_sexpr_parser.cpp: checkVersion() called at T_generator_version, and at T_uuid only "if( m_requiredVersion < 20231120 )". include/cli/exit_codes.h (7.0, 8.0): OK=0, ERR_ARGS=1, ERR_UNKNOWN=2, ERR_INVALID_INPUT_FILE=3, ERR_INVALID_OUTPUT_CONFLICT=4, ERR_RC_VIOLATIONS=5 (no 127). kicad/kicad_cli.cpp 8.0 OnRun(): catch(...) { Pgm().HandleException(...) } return -1. pcbnew_jobs_handler.cpp 8.0 calls LoadBoard(..., true) without try/catch in export jobs; 9.0 wraps it and reports "Failed to load board" -> ERR_INVALID_INPUT_FILE. DOCS: docs.kicad.org 10.0 Introduction: "KiCad is in general always backwards compatible with files created by older versions, but not forwards compatible: Once files are edited and saved by a new major version, these files will not be openable by the previous major version."

**Correction:** Only the first half is reliably true. Corrected statement: (1) Newer-loads-older holds: kicad-cli 7.0.6/8.0.1/9.0.5/10.0.0 each loaded every board (.kicad_pcb) and schematic (.kicad_sch) written by the same or an older major (16/16 board and 16/16 schematic combinations that should pass did, exit 0). This matches the official docs (backwards compatible, not forwards compatible). (2) Older-rejects-newer holds for files actually written by KiCad N+1 (all 12 such combinations failed), BUT the exit code is not "3/127": 9.0 and 10.0 return 3 (ERR_INVALID_INPUT_FILE) with only the generic text "Failed to load board"; 7.0/8.0 return 3 for schematics but -1 (raw Windows 0xFFFFFFFF; shown as 127 by Git Bash, -1 by PowerShell) for boards because LoadBoard's IO_ERROR is not caught in pcbnew_jobs_handler and falls through to wxApp::OnRun's catch(...) which returns -1. Also 7.0 kicad-cli has no `pcb drc`, so behavior is command-dependent. (3) The rejection is NOT purely driven by the version stamp, and the rule differs per parser/version: 7.0's board parser only converts a PARSE_ERROR into FUTURE_FORMAT_ERROR (m_tooRecent flag), so a 7.0-content board with (version 20260206) loads fine on 7.0 (exit 0); 8.0+ board parsers add an eager checkVersion() at generator_version/general so the stamp alone rejects. Conversely 7.0's schematic parser rejects on the stamp alone in parseHeader, while 8.0 and 9.0 schematic parsers only call checkVersion() on `generator_version` (or on `uuid` if version < 20231120), so a schematic with a future stamp but no generator_version token loads on 8.0/9.0 (exit 0, tested). Any tool that emits its own files must therefore not rely on the stamp for gating. (4) "A single unknown token makes 7.0/8.0 reject the file" is false as a general rule: unknown tokens inside (general ...) and (setup (pcbplotparams ...)) are silently skipped by every version 7.0-10.0 (exit 0, tested; source: parseGeneralSection default "Skip everything...", PCB_PLOT_PARAMS_PARSER default skipCurrent()). Unknown tokens at board top level, inside (setup ...), inside (segment ...), at schematic top level, or inside a schematic (symbol ...) do reject on all four versions (Unknown token / Unexpected / Expecting errors). In 9.0/10.0 the CLI hides the parser reason entirely, printing only "Failed to load board/schematic".

Sources:
- https://docs.kicad.org/10.0/en/introduction/introduction.html
- https://gitlab.com/kicad/code/kicad/-/raw/7.0/pcbnew/plugins/kicad/pcb_parser.cpp
- https://gitlab.com/kicad/code/kicad/-/raw/8.0/pcbnew/pcb_io/kicad_sexpr/pcb_io_kicad_sexpr_parser.cpp
- https://gitlab.com/kicad/code/kicad/-/raw/9.0/pcbnew/pcb_io/kicad_sexpr/pcb_io_kicad_sexpr_parser.cpp
- https://gitlab.com/kicad/code/kicad/-/raw/10.0/pcbnew/pcb_io/kicad_sexpr/pcb_io_kicad_sexpr_parser.cpp
- https://gitlab.com/kicad/code/kicad/-/raw/8.0/pcbnew/pcb_plot_params.cpp
- https://gitlab.com/kicad/code/kicad/-/raw/7.0/eeschema/sch_plugins/kicad/sch_sexpr_parser.cpp
- https://gitlab.com/kicad/code/kicad/-/raw/8.0/eeschema/sch_io/kicad_sexpr/sch_io_kicad_sexpr_parser.cpp
- https://gitlab.com/kicad/code/kicad/-/raw/9.0/eeschema/sch_io/kicad_sexpr/sch_io_kicad_sexpr_parser.cpp
- https://gitlab.com/kicad/code/kicad/-/raw/8.0/include/cli/exit_codes.h
- https://gitlab.com/kicad/code/kicad/-/raw/8.0/kicad/kicad_cli.cpp
- https://gitlab.com/kicad/code/kicad/-/raw/8.0/pcbnew/pcbnew_jobs_handler.cpp
- https://gitlab.com/kicad/code/kicad/-/raw/9.0/pcbnew/pcbnew_jobs_handler.cpp
- https://dev-docs.kicad.org/en/file-formats/sexpr-intro/
- https://www.kicad.org/blog/2026/03/Version-10.0.0-Released/


## Claim 10 — CORRECTED

> A KiCad 9/10 GUI launched with KICAD_CONFIG_HOME seeded with kicad_common.json (api.enable_server, prompt flags) and the three global lib tables starts without the Setup wizard and answers kicad-python 0.8.0 over ipc://%TEMP%\kicad\api.sock on both 9.0.5 and 10.0.0.

### Empirical verifier — refuted=true

All runs in <scratchpad>\verify-ipc-config-home (venv .venv with kicad-python==0.8.0 + pynng 0.9.0; tools\winlist.ps1 enumerates visible top-level windows per PID; tools\kipy_probe.py runs KiCad().get_version()/get_board()). Seeds: cfg10\10.0\{kicad_common.json (api.enable_server true, do_not_show_again.update_check_prompt/data_collection_prompt true, meta version 5), sym-lib-table & fp-lib-table (type "Table" -> stock template), design-block-lib-table (empty v7)}; cfg9\9.0\{same flags with meta version 3, stock expanded sym/fp tables, empty design-block table}. Boards: uniquely named copies board\ecc83_10.kicad_pcb / ecc83_9.kicad_pcb so responses could be attributed to my process.
RESULTS: [10.0 pcbnew.exe, cfg10] windows = only "ecc83_10 - PCB Editor"; probe: "CONNECTED version= 10.0.0 (10.0.0) board: ecc83_10.kicad_pcb footprints= 15". [9.0 pcbnew.exe, cfg9] windows = only "ecc83_9 - PCB Editor"; probe: "CONNECTED version= 9.0.5 (9.0.5) board: ecc83_9.kicad_pcb footprints= 15"; graceful close wrote pcbnew.json/kicad_common.json etc. into cfg9\9.0\ while %APPDATA%\kicad\9.0\kicad_common.json mtime was unchanged by my run. [Negative controls, 10.0 pcbnew.exe] cfg10neg (kicad_common only, no tables) -> window class #32770 title "KiCad Setup"; cfg10neg2 (tables present, kicad_common without do_not_show_again) -> "KiCad Setup"; cfg10root (all files at KICAD_CONFIG_HOME root, no 10.0 subfolder) -> "KiCad Setup" and KiCad created <root>\10.0\. [10.0 kicad.exe with project, cfg10, TEMP redirected to short dir %LOCALAPPDATA%\Temp\kv-ipc so the pipe was mine] KICAD_API_SOCKET=ipc://%LOCALAPPDATA%\Temp\kv-ipc\kicad\api.sock -> "OK version: 10.0.0 (10.0.0)"; get_board -> "no handler available for ... GetOpenDocuments" (no PCB frame open). [Contention] After my kicad.exe exited, the named pipe \\.\pipe\%LOCALAPPDATA%\Temp\kicad\api.sock persisted and a sibling agent's eeschema.exe (10.0, not started by me) answered "no handler available for request of type kiapi.common.commands.GetVersion"; later a sibling pcbnew.exe answered "KiCad is not ready to reply". [Path length] pynng listen on ipc:// path of 148 chars -> AddressInvalid; 126 chars OK, 146 FAIL. SOURCE (10.0 branch): common/startwizard/startwizard.cpp CheckAndRun builds wizard if any provider NeedsUserInput(); startwizard_provider_settings.cpp: !SettingsDirectoryValid() (kicad_common.json in versioned dir, settings_manager.cpp:637); startwizard_provider_libraries.cpp: !LIBRARY_MANAGER::GlobalTablesValid() over SYMBOL/FOOTPRINT/DESIGN_BLOCK (common/libraries/library_manager.cpp:507); startwizard_provider_privacy.cpp:81: !update_check_prompt || !data_collection_prompt. Called from kicad/kicad.cpp:266 and common/single_top.cpp:434 (all standalone apps). common/paths.cpp:645 CalculateUserSettingsPath appends GetMajorMinorVersion() after KICAD_CONFIG_HOME. common/api/api_server.cpp (identical in 9.0 and 10.0): Start() uses wxStandardPaths::GetTempDir()/kicad/api.sock, no env override; API_HANDLER_COMMON (GetVersion) registered in kicad.cpp:299 and pcb_edit_frame.cpp:543 only if Kiface().IsSingle(); sch_edit_frame.cpp:461 registers only API_HANDLER_SCH (9.0 line 415 same). Side note: %APPDATA%\kicad\10.0\kicad_common.json and 9.0\kicad_common.json were modified during this session (22:08-22:09) by a sibling agent's KiCad launches (working_dir points at scratchpad\freerouting-verify; enable_server still false) - not by my processes, which never touched %APPDATA%. No KiCad process I started is left running; kv-ipc temp dir removed.

**Correction:** The core mechanism holds, but the claim is only true with these precise conditions: (1) The seed files must be placed in <KICAD_CONFIG_HOME>\<major.minor>\ (e.g. ...\cfg10\10.0\kicad_common.json), NOT at the root of KICAD_CONFIG_HOME; KiCad appends the version subfolder (PATHS::CalculateUserSettingsPath), and files at the root produce the "KiCad Setup" wizard (tested). (2) kicad_common.json must contain BOTH do_not_show_again.update_check_prompt=true AND do_not_show_again.data_collection_prompt=true in addition to api.enable_server=true; omitting the flags with valid tables still shows the wizard on 10.0 (tested). (3) All three tables (sym-lib-table, fp-lib-table, design-block-lib-table) must exist and parse in that versioned folder; missing tables show the wizard on 10.0 (tested). (4) "GUI" must be kicad.exe or standalone pcbnew.exe. Standalone eeschema.exe (9.0 and 10.0) registers only API_HANDLER_SCH and no API_HANDLER_COMMON, so kipy.KiCad().get_version() fails with "no handler available for request of type kiapi.common.commands.GetVersion" (observed against a 10.0 eeschema.exe on this machine; confirmed in sch_edit_frame.cpp vs pcb_edit_frame.cpp which registers API_HANDLER_COMMON only when Kiface().IsSingle()). kicad.exe answers get_version but get_board only works once a PCB editor frame is open. (5) The socket path is not %TEMP%-literal but wxStandardPaths::GetTempDir()/kicad/api.sock (honours TEMP/TMP of the KiCad process; kipy default likewise uses tempfile.gettempdir()); it is a single fixed Windows named pipe with no KiCad-side override, so only ONE KiCad instance on the machine can serve it (first to bind wins; a second instance's server silently fails) - a sibling KiCad instance on this machine hijacked the default pipe during testing. Also, the nng ipc transport rejects pipe paths longer than ~128 characters ("Address invalid"), so a long redirected TEMP breaks both server and client. 9.0.5 and 10.0.0 pcbnew.exe both started wizard/dialog-free and answered kicad-python 0.8.0 (get_version and get_board) under these conditions.

Sources:
- https://gitlab.com/kicad/code/kicad/-/raw/10.0/common/startwizard/startwizard.cpp
- https://gitlab.com/kicad/code/kicad/-/raw/10.0/common/startwizard/startwizard_provider_settings.cpp
- https://gitlab.com/kicad/code/kicad/-/raw/10.0/common/startwizard/startwizard_provider_libraries.cpp
- https://gitlab.com/kicad/code/kicad/-/raw/10.0/common/startwizard/startwizard_provider_privacy.cpp
- https://gitlab.com/kicad/code/kicad/-/raw/10.0/common/settings/settings_manager.cpp
- https://gitlab.com/kicad/code/kicad/-/raw/10.0/common/paths.cpp
- https://gitlab.com/kicad/code/kicad/-/raw/10.0/common/libraries/library_manager.cpp
- https://gitlab.com/kicad/code/kicad/-/raw/10.0/common/single_top.cpp
- https://gitlab.com/kicad/code/kicad/-/raw/10.0/kicad/kicad.cpp
- https://gitlab.com/kicad/code/kicad/-/raw/9.0/kicad/kicad.cpp
- https://gitlab.com/kicad/code/kicad/-/raw/10.0/common/api/api_server.cpp
- https://gitlab.com/kicad/code/kicad/-/raw/9.0/common/api/api_server.cpp
- https://gitlab.com/kicad/code/kicad/-/raw/10.0/common/api/api_handler_common.cpp
- https://gitlab.com/kicad/code/kicad/-/raw/10.0/pcbnew/pcb_edit_frame.cpp
- https://gitlab.com/kicad/code/kicad/-/raw/10.0/eeschema/sch_edit_frame.cpp
- https://gitlab.com/kicad/code/kicad/-/raw/9.0/eeschema/sch_edit_frame.cpp
- <scratchpad>\verify-ipc-config-home\.venv\Lib\site-packages\kipy\kicad.py
- <scratchpad>\verify-ipc-config-home\tools\winlist.ps1
- <scratchpad>\verify-ipc-config-home\tools\kipy_probe.py
- <scratchpad>\verify-ipc-config-home\cfg10\10.0\kicad_common.json
- <scratchpad>\verify-ipc-config-home\cfg9\9.0\kicad_common.json

### Documentary verifier — refuted=true

Core mechanism is confirmed by source (tags 9.0.5 fda292bd and 10.0.0 0feeca2a checked out from gitlab.com/kicad/code/kicad) but the claim is imprecise in ways that will break a literal implementation:

1. CONFIG PATH: PATHS::CalculateUserSettingsPath(aIncludeVer=true default) does cfgpath.AssignDir(KICAD_CONFIG_HOME) then AppendDir(GetMajorMinorVersion()) (common/paths.cpp, both tags). So the seeded files must live in %KICAD_CONFIG_HOME%\9.0\ and %KICAD_CONFIG_HOME%\10.0\, NOT at the root of KICAD_CONFIG_HOME. Seeding the root => wizard (10) / migration dialog (9) appears.

2. THE "SETUP WIZARD" ONLY EXISTS IN 10.0.0. common/startwizard/startwizard.cpp (wxWizard titled "KiCad Setup") is invoked from kicad/kicad.cpp (manager) and common/single_top.cpp (standalone pcbnew/eeschema). It runs iff any provider NeedsUserInput(): SETTINGS -> !SettingsDirectoryValid() (= kicad_common.json missing in the versioned dir); LIBRARIES -> !LIBRARY_MANAGER::GlobalTablesValid(), which iterates SYMBOL, FOOTPRINT, DESIGN_BLOCK (sym-lib-table, fp-lib-table, design-block-lib-table) and requires each file to exist AND parse (IsTableValid -> LIBRARY_TABLE::IsOk), not merely exist; PRIVACY -> !do_not_show_again.update_check_prompt || !do_not_show_again.data_collection_prompt. So for 10.0.0 the exact prompt flags are those two keys (both true). 9.0.5 has no wizard; instead: SETTINGS_MANAGER::MigrateIfNeeded shows DIALOG_MIGRATE_SETTINGS when kicad_common.json is missing; PGM_BASE::sentryPrompt shows a wxMessageDialog unless do_not_show_again.data_collection_prompt; KICAD_MANAGER_FRAME shows DIALOG_UPDATE_CHECK_PROMPT unless do_not_show_again.update_check_prompt; eeschema/pcbnew IFACE::loadGlobalLibTable shows DIALOG_GLOBAL_SYM/FP_LIB_TABLE_CONFIG only if the file does not exist; a missing design-block-lib-table is silently auto-created (eeschema.cpp ~line 495). The same seeding therefore suppresses all 9.0.5 dialogs too.

3. api.enable_server: PARAM "api.enable_server" (default false) in common/settings/common_settings.cpp in both tags; KICAD_API_SERVER ctor returns early if false and otherwise Start()s (common/api/api_server.cpp is byte-identical between 9.0 and 10.0 branches). Server is created in kicad.cpp (kicad.exe) and single_top.cpp (standalone pcbnew.exe/eeschema.exe) under #ifdef KICAD_IPC_API; the installed 9.0.5 and 10.0.0 kicommon.dll both contain the strings "api.sock", "api.enable_server", "KICAD_API_SOCKET" (10.0 also "KiCad Setup"), confirming official Windows builds have the API compiled in. JSON_SETTINGS::LoadFromFile still applies parsed values even if meta.version is absent.

4. SOCKET PATH: Start() builds wxStandardPaths::GetTempDir() + "kicad" + "api.sock" and listens on fmt::format("ipc://{}", fullpath). On Windows GetTempDir = Win32 GetTempPath (locally %LOCALAPPDATA%\Temp\), i.e. ipc://%TEMP%\kicad\api.sock. kicad-python 0.8.0 (installed in scratchpad venv) kipy/kicad.py _default_socket_path(): on Windows returns f'ipc://{gettempdir()}\\kicad\\api.sock' unless KICAD_API_SOCKET is set; locally this evaluates to ipc://%LOCALAPPDATA%\Temp\kicad\api.sock, matching. nng ipc on Windows is a named pipe \\.\pipe\<path> (no file on disk). CAVEAT: the server does NOT read KICAD_API_SOCKET (that env var is only exported to plugins/clients), and the "api-<pid>.sock" fallback is gated on socket.Exists() which is a file test that is always false for a named pipe, and KINNG_REQUEST_SERVER::listenThread ignores the return value of nng_listener_start (libs/kinng/src/kinng.cpp); nng's win_ipclisten.c uses FILE_FLAG_FIRST_PIPE_INSTANCE and returns NNG_EADDRINUSE. Hence only ONE KiCad GUI process on the machine can serve the API at a time; running 9.0.5 and 10.0.0 (or kicad.exe plus a standalone pcbnew.exe) concurrently leaves the second one silently unreachable. "Both 9.0.5 and 10.0.0" holds only sequentially.

5. kicad-python 0.8.0: exists on PyPI (released 2026-08-30; requires pynng>=0.9,<0.10, protobuf>=5.29,<6, Python>=3.9; Windows wheels installed fine on Python 3.11). Its bundled KICAD_API_VERSION is "10.0.6-0-gcaf7377e9c" (built against KiCad 10.0.6 protos; 0.6.0 was the last release built against 9.0.x). README states the API "requires a suitable version of KiCad (9.0 or higher)"; KiCad.check_version() raises FutureVersionError only when KiCad is NEWER than the library, so 9.0.5 passes. Release notes gate newer calls ("requires KiCad 10", "requires KiCad 10.0.1"), and the dev-docs stability policy says new versions add messages/fields but do not change existing ones, so base commands (GetVersion, Ping, GetOpenDocuments, GetItems...) are expected to work on 9.0.5 while 10-only commands will error. No official compatibility matrix or test guarantee for 0.8.0 against 9.0.x exists; this part is plausible-by-policy, not proven.

Local checks were static only (source, installed DLL strings, pip package inspection, GetTempPath); no GUI was launched.

**Correction:** Corrected statement: KiCad 9.0.5 and 10.0.0 GUIs (kicad.exe, or standalone pcbnew.exe/eeschema.exe) start without any first-run dialog/wizard when %KICAD_CONFIG_HOME%\<9.0|10.0>\ (the major.minor subfolder is always appended, not the KICAD_CONFIG_HOME root) contains (a) kicad_common.json with "api": {"enable_server": true} and "do_not_show_again": {"update_check_prompt": true, "data_collection_prompt": true}, and (b) parseable sym-lib-table, fp-lib-table and design-block-lib-table (10.0.0 requires all three to exist and parse; 9.0.5 only gates dialogs on sym/fp existing and auto-creates the design-block table). The "KiCad Setup" wizard is a 10.0-only construct; 9.0.5 has equivalent separate dialogs suppressed by the same files. The API server then listens on ipc://<Win32 GetTempPath>kicad\api.sock (= ipc://%TEMP%\kicad\api.sock, a named pipe, no file on disk), which is exactly kicad-python 0.8.0's Windows default. Constraints: the server path is hardcoded (KICAD_API_SOCKET is honored only by clients), and on Windows a second concurrent KiCad GUI process silently fails to bind the same pipe, so 9.0.5 and 10.0.0 can be served only one at a time. kicad-python 0.8.0 is built against KiCad 10.0.6 protos; it is documented to support KiCad 9.0+ for base commands (check_version passes because 9.0.5 < 10.0.6), but 10.x-only commands will fail on 9.0.5 and there is no official test guarantee for 0.8.0 against 9.0.x.

Sources:
- https://gitlab.com/kicad/code/kicad/-/raw/10.0/common/api/api_server.cpp
- https://gitlab.com/kicad/code/kicad/-/raw/9.0/common/api/api_server.cpp
- https://gitlab.com/kicad/code/kicad/-/blob/10.0.0/common/startwizard/startwizard.cpp
- https://gitlab.com/kicad/code/kicad/-/blob/10.0.0/common/startwizard/startwizard_provider_settings.cpp
- https://gitlab.com/kicad/code/kicad/-/blob/10.0.0/common/startwizard/startwizard_provider_libraries.cpp
- https://gitlab.com/kicad/code/kicad/-/blob/10.0.0/common/startwizard/startwizard_provider_privacy.cpp
- https://gitlab.com/kicad/code/kicad/-/blob/10.0.0/common/libraries/library_manager.cpp
- https://gitlab.com/kicad/code/kicad/-/blob/10.0.0/common/paths.cpp
- https://gitlab.com/kicad/code/kicad/-/blob/10.0.0/kicad/kicad.cpp
- https://gitlab.com/kicad/code/kicad/-/blob/10.0.0/common/single_top.cpp
- https://gitlab.com/kicad/code/kicad/-/blob/10.0.0/common/settings/settings_manager.cpp
- https://gitlab.com/kicad/code/kicad/-/blob/10.0.0/common/settings/common_settings.cpp
- https://gitlab.com/kicad/code/kicad/-/blob/10.0.0/libs/kinng/src/kinng.cpp
- https://gitlab.com/kicad/code/kicad/-/blob/9.0.5/common/settings/settings_manager.cpp
- https://gitlab.com/kicad/code/kicad/-/blob/9.0.5/common/pgm_base.cpp
- https://gitlab.com/kicad/code/kicad/-/blob/9.0.5/kicad/kicad_manager_frame.cpp
- https://gitlab.com/kicad/code/kicad/-/blob/9.0.5/eeschema/eeschema.cpp
- https://gitlab.com/kicad/code/kicad/-/blob/9.0.5/pcbnew/pcbnew.cpp
- https://pypi.org/pypi/kicad-python/json
- https://gitlab.com/kicad/code/kicad-python/-/raw/0.8.0/README.md
- https://gitlab.com/kicad/code/kicad-python/-/commits/0.8.0
- https://dev-docs.kicad.org/en/apis-and-binding/ipc-api/index.html
- https://dev-docs.kicad.org/en/apis-and-binding/ipc-api/for-addon-developers/index.html
- https://nng.nanomsg.org/man/v1.5.2/nng_ipc.7.html
- https://raw.githubusercontent.com/nanomsg/nng/v1.9.0/src/platform/windows/win_ipclisten.c
- https://docs.kicad.org/10.0/en/kicad/kicad.html
- https://github.com/mixelpixx/Konnect/issues/532


## Claim 11 — SURVIVED

> An mcp 2.2 Python MCPServer negotiates protocol 2026-07-28, serves legacy 2025-06-18 clients, and returns PNG ImageContent and structuredContent over stdio and streamable HTTP.

### Empirical verifier — refuted=false

Installed mcp==2.2.0 (+ mcp-types==2.2.0) into a uv venv (Python 3.11) in the scratchpad and built a real MCPServer (<scratchpad>/mcp-verify/server.py) whose tool shells out to KiCad 10.0.0 `kicad-cli pcb render` on the bundled ecc83-pp demo board and returns CallToolResult(content=[ImageContent(image/png), TextContent], structured_content={...}).

Version registry (mcp_types/version.py): KNOWN_PROTOCOL_VERSIONS = 2024-11-05, 2025-03-26, 2025-06-18, 2025-11-25, 2026-07-28; HANDSHAKE_PROTOCOL_VERSIONS = the first four; MODERN_PROTOCOL_VERSIONS = ("2026-07-28",); LATEST_PROTOCOL_VERSION = 2026-07-28. `from mcp.server import MCPServer` works; `FastMCP` no longer exists in 2.2 (ImportError).

STDIO results:
- Raw JSON-RPC client sending initialize protocolVersion=2025-06-18 -> server answers protocolVersion "2025-06-18"; tools/call result keys = content, isError, structuredContent; image block mimeType=image/png, base64 decodes to valid PNG signature (10570 bytes); structuredContent = {'board':'ecc83-pp.kicad_pcb','width':320,'height':240,'png_bytes':10570,'kicad_version':'10.0.0'}.
- Raw initialize offering protocolVersion=2026-07-28 -> server counter-offers "2025-11-25" (NOT 2026-07-28); tool call still works.
- Raw modern envelope (server/discover with _meta io.modelcontextprotocol/protocolVersion=2026-07-28 + io.modelcontextprotocol/clientCapabilities={}) -> DiscoverResult supportedVersions ["2026-07-28"]; tools/call with the same _meta returns content/isError/resultType/structuredContent/_meta, valid PNG, correct structuredContent. Omitting the clientCapabilities key yields -32602 "params._meta is missing the required envelope key(s)". After modern use, a subsequent initialize on the same connection is rejected with -32022 "connection is serving the 2026-07-28 protocol".
- SDK `mcp.Client` over StdioServerParameters: mode=auto -> protocol_version 2026-07-28; mode=legacy -> 2025-11-25; mode="2026-07-28" -> 2026-07-28. All three: image/png verified PNG bytes, structured_content populated, result_type=complete.

STREAMABLE HTTP results (mcp.run("streamable-http", host=127.0.0.1, port=8765), uvicorn 0.53 / starlette 1.6):
- SDK Client("http://127.0.0.1:8765/mcp"): auto -> 2026-07-28, legacy -> 2025-11-25, pinned -> 2026-07-28; PNG + structured_content verified in all three.
- Raw httpx legacy client: POST initialize protocolVersion=2025-06-18 -> HTTP 200 text/event-stream, mcp-session-id issued, negotiated "2025-06-18"; notifications/initialized -> 202; tools/call -> 200 with content/isError/structuredContent, valid PNG (2463 bytes), structuredContent correct; DELETE session -> 200.

Also ran the same server with KICAD_CLI pointed at KiCad 9.0.5 kicad-cli: legacy stdio path returned valid PNG + structuredContent with kicad_version '9.0.5'.

Test scripts: raw_stdio_client.py, raw_modern_stdio.py, sdk_client.py, raw_http_legacy.py in the same scratchpad folder. Background HTTP server was terminated (PID 72240) after testing.

**Correction:** Claim stands, with one precision the architecture must respect: 2026-07-28 is never the outcome of the `initialize` handshake. It is a "modern" per-request-envelope revision reached only via the `server/discover` probe / `_meta` envelope (io.modelcontextprotocol/protocolVersion + io.modelcontextprotocol/clientCapabilities on every request). A client that sends `initialize` offering 2026-07-28 is counter-offered 2025-11-25. The SDK `Client` in the default mode='auto' does end up on 2026-07-28 (via discover), while mode='legacy' negotiates 2025-11-25 and a raw legacy client gets exactly the version it offers (2025-06-18 verified). Once a connection has served a modern envelope request, `initialize` on that connection is rejected with -32022. Also note: in mcp 2.2 the class is `mcp.server.MCPServer`; `FastMCP` is gone, and the client HTTP stack uses `httpx2` (plain `httpx` is not a dependency).

Sources:
- Local test folder: <scratchpad>/mcp-verify/ (server.py, raw_stdio_client.py, raw_modern_stdio.py, sdk_client.py, raw_http_legacy.py, http_server.log)
- Installed package source: .venv/Lib/site-packages/mcp_types/version.py (protocol version registry, mcp-types 2.2.0)
- Installed package source: .venv/Lib/site-packages/mcp/client/_probe.py (mode='auto' discover-then-fallback negotiation)
- Installed package source: .venv/Lib/site-packages/mcp/shared/inbound.py (envelope key validation, -32602 / -32022)
- Installed package source: .venv/Lib/site-packages/mcp/server/mcpserver/server.py (MCPServer.run transports: stdio, sse, streamable-http)
- PyPI: mcp 2.2.0 and mcp-types 2.2.0 resolved by uv on 2026-09-18
- KiCad 10.0.0 and 9.0.5 kicad-cli pcb render (C:/Program Files/KiCad/10.0/bin/kicad-cli.exe, C:/Program Files/KiCad/9.0/bin/kicad-cli.exe)

### Empirical verifier — refuted=false

DOCUMENTARY: The official "What's new in v2" page (py.sdk.modelcontextprotocol.io/whats-new/) states v2 "speaks the 2026-07-28 revision of MCP" and that "the same streamable_http_app() (and the same stdio server) answers a 2025-era client's initialize and a 2026-era client's requests with nothing to configure"; FastMCP was renamed MCPServer (import from mcp.server). The v2.0.0 GitHub release says the same (2026-07-28, "still serves every 2025-era client from the same MCPServer, over Streamable HTTP and stdio"). v2.2.0 release notes (released ~Sep 2026) only change defaults (idle HTTP session expiry 30 min / max 10,000 sessions, same-origin redirect policy, OAuth issuer checks) and do not alter any of this. 2026-07-28 is the current MCP spec revision per modelcontextprotocol.io/specification/2026-07-28 and the MCP blog.

LOCAL TEST (scratchpad <scratchpad>/verify-mcp22, uv venv, Python 3.11, mcp==2.2.0 + mcp-types 2.2.0): 
- mcp.types.version: KNOWN_PROTOCOL_VERSIONS = (2024-11-05, 2025-03-26, 2025-06-18, 2025-11-25, 2026-07-28); LATEST_PROTOCOL_VERSION = 2026-07-28; HANDSHAKE versions include 2025-06-18.
- server.py: MCPServer("verify") with a tool returning CallToolResult(content=[TextContent, ImageContent(mime_type="image/png", data=<base64 1x1 PNG>)], structured_content={...}); run via mcp.run("stdio") and mcp.run("streamable-http", host, port).
- v2 Client (mode="auto") over stdio and over http://127.0.0.1:8765/mcp: negotiated protocol_version = 2026-07-28; result content types [TextContent, ImageContent], mime image/png, PNG magic bytes verified, structured_content returned, is_error=False.
- Raw JSON-RPC initialize with protocolVersion "2025-06-18" over stdio and over Streamable HTTP (POST, SSE response, Mcp-Session-Id issued): server answered protocolVersion "2025-06-18"; tools/call wire result keys = ['content','isError','structuredContent'], image block mimeType image/png, PNG verified.
- Real legacy SDK client mcp==1.12.4 (its LATEST_PROTOCOL_VERSION is 2025-06-18) in a separate venv, using stdio_client and streamablehttp_client against the v2.2 server: negotiated 2025-06-18, image/png ImageContent and structuredContent received on both transports.
Minor nuances (not refuting): in v2 Python attribute names are snake_case (structured_content, mime_type, is_error) while the wire JSON stays camelCase (structuredContent, mimeType); the v2 Client's mode="legacy" offers 2025-11-25 by default, so testing 2025-06-18 specifically requires an older client or a raw handshake (which both succeeded). Note also v2.2 idle streamable-HTTP sessions expire after 30 min by default (client gets 404 and must re-initialize).

Sources:
- https://py.sdk.modelcontextprotocol.io/whats-new/
- https://github.com/modelcontextprotocol/python-sdk/releases/tag/v2.0.0
- https://github.com/modelcontextprotocol/python-sdk/releases
- https://newreleases.io/project/github/modelcontextprotocol/python-sdk/release/v2.2.0
- https://modelcontextprotocol.io/specification/2026-07-28
- https://blog.modelcontextprotocol.io/posts/2026-07-28/
- https://github.com/modelcontextprotocol/python-sdk


## Claim 12 — SURVIVED

> symbols.kicad.org/kicad-stable serves PDBs matching the installed 10.0.0 binaries, so cdb symbolicates WER minidumps of KiCad crashes to source lines without a source build.

### Empirical verifier — refuted=false

Tested end-to-end on this machine (Windows 11, cdb 10.0.22621.3233 from C:\Program Files (x86)\Windows Kits\10\Debuggers\x64, which was already installed). All artifacts are under <scratchpad>\verify-symbols-pdb\ (pdbid.py, cdb_*.txt scripts, cdb_*.log outputs, kicad10_mini.dmp, wer_eeschema.dmp).

1. PDB identity of installed binaries (parsed RSDS CodeView records with pdbid.py): KiCad 10.0.0 kicad.exe 8D4D877A814B46ACA4B697D7D250BC9C/5, pcbnew.exe 98E5AA937AF942869CC1829F63866FD3/5, eeschema.exe ECE61F73F98F4B21AE5494337CE31B28/5, kicad-cli.exe CBC90706EE2B4F5EBAF23E4DDE4328B4/5, _pcbnew.dll 64D0808226A44C06812F71472CD8A7B5/5, _eeschema.dll BCDE735CB4DA4973B0158155E57740C0/6, kicommon.dll 9AEDF265041941DDB93E9EF45FE5F238/6; all built by MSVC 14.44 on jenkins (build-win-10.0-stable). KiCad 9.0.1 ids also extracted.

2. Server probe with curl (HEAD): for every one of those GUID+age paths, https://symbols.kicad.org/kicad-stable/<name>.pdb/<GUID><age>/<name>.pd_ returns HTTP 200 application/octet-stream (compressed CAB form); the uncompressed .pdb URL returns 404. Sizes: kicad-cli.pd_ 3.4 MB, kicommon.pd_ 15.0 MB, _pcbnew.pd_ 54.5 MB, pcbnew.pd_ 1.4 MB, Last-Modified 25 Mar 2026. KiCad 9.0.1 _pcbnew.pdb/139DA84C3BDB4AF6A2184CAAD7F6270A2 also 200.

3. Live test: ran "kicad-cli pcb drc" (KiCad 10) under cdb with .sympath srv*<cache>*https://symbols.kicad.org/kicad-stable and !sym noisy; symsrv fetched _pcbnew.pd_, kicommon.pd_, kicad-cli.pd_ over HTTPS, expanded them, and dbghelp reported "private symbols & lines" for each. Deferred breakpoint _pcbnew!DRC_ENGINE::RunTests resolved and hit; wrote a WER-style "mini user dump" (.dump /m).

4. Re-opened that mini dump in a fresh cdb with an EMPTY cache (symcache2) and only the KiCad server in the sympath: "k" printed every KiCad frame with file@line, e.g. _pcbnew!DRC_ENGINE::RunTests [C:\jenkins\...\kicad\pcbnew\drc\drc_engine.cpp @ 882], _pcbnew!PCBNEW_JOBS_HANDLER::JobExportDrc+0xa60 [pcbnew_jobs_handler.cpp @ 2425], kicommon!JOB_DISPATCHER::RunJob+0x1d3 [common\jobs\job_dispatcher.cpp @ 58], kicad_cli!CLI::PCB_DRC_COMMAND::doPerform+0x542 [kicad\cli\command_pcb_drc.cpp @ 179]. The server also serves the custom wxWidgets PDBs (wxbase332u_vc_x64_custom.pdb 8B63F2A4B7364F3AABD192E104BE85781 -> init.cpp @ 563). lm shows "C (private pdb symbols)".

5. Real WER dump: %LOCALAPPDATA%\CrashDumps contained genuine WER-written dumps of KiCad 10 from today (eeschema.exe.*.dmp ~19 MB, kicad-cli.exe.*.dmp). Copied eeschema.exe.89232.dmp to the scratchpad and symbolicated it with cdb + KiCad server only: .ecxr -> Access violation c0000005 at _eeschema!API_HANDLER_EDITOR::checkForBusy+0xd [common\api\api_handler_editor.cpp @ 207] (null rcx deref), called from API_HANDLER_EDITOR::handleHitTest [api_handler_editor.cpp @ 325] <- kicommon!API_HANDLER::Handle [api_handler.cpp @ 55] <- kicommon!KICAD_API_SERVER::handleApiEvent [api_server.cpp @ 246] <- wx event dispatch. _eeschema.pdb BCDE735CB4DA4973B0158155E57740C06 loaded as private pdb symbols. No source build or checkout was used. (Side note for the architecture: this real crash is in the IPC API HitTest handler, i.e. the very interface a VS Code plugin would use.)

Operational qualifications found (do not refute the claim but must be in the design):
- WER LocalDumps is NOT enabled by default on Windows ("This feature is not enabled by default", HKLM\...\Windows Error Reporting\LocalDumps must exist; default DumpFolder %LOCALAPPDATA%\CrashDumps, DumpType 1 = mini dump). On this machine the key exists (created by other installers) with no global values, and WER still wrote dumps to %LOCALAPPDATA%\CrashDumps, so it is effectively on here; on a clean machine it is off.
- Microsoft: "Applications that do their own custom crash reporting are not supported by this feature." KiCad 10 bundles Sentry+Crashpad (crashpad_handler.exe, crashpad_wer.dll in bin), but common/app_monitor.cpp on branch 10.0 only calls sentry_init() when the marker file <UserCachePath>/sentry-opt-in exists. On this machine %LOCALAPPDATA%\kicad\10.0\sentry-opt-in is absent (9.0 has it), so KiCad 10 crashes fall through to WER here; an opted-in user's crashes would be captured by Crashpad instead (into %LOCALAPPDATA%\kicad\10.0\sentry, also minidumps, symbolicable the same way).
- The server only serves the compressed .pd_ form; symsrv/cdb handle that transparently, but any custom (non-symsrv) fetcher must request .pd_ and expand it.
- Only KiCad/wx/vcpkg PDBs are on the server; ntdll/KERNELBASE etc. need the Microsoft symbol server added to the sympath ("The system cannot find the file specified" for ntdll/KERNELBASE with KiCad server alone). Stack walking of KiCad frames worked regardless.
- The PDBs have no source-server (SRCSRV) stream; they give file@line with jenkins build paths (C:\jenkins\workspace\build-win-10.0-stable\.build\kicad\...). Viewing source text needs a plain git checkout of tag 10.0.0 mapped via .srcpath, not a build.

**Correction:** Claim stands as tested. Qualified form: symbols.kicad.org/kicad-stable serves exact GUID/age-matched, full private PDBs (with line info) for KiCad 10.0.0 and 9.0.1 binaries plus the bundled wxWidgets DLLs, in compressed .pd_ form only; cdb with sympath srv*<cache>*https://symbols.kicad.org/kicad-stable symbolicates both synthetic mini dumps and real WER LocalDumps of KiCad 10 crashes to source file@line with no source build (verified on a genuine eeschema.exe access violation). Prerequisites: WER LocalDumps must be enabled (HKLM LocalDumps key; off by default on Windows), KiCad's Sentry/Crashpad opt-in (sentry-opt-in marker file) must be absent or the dump must be taken from the Crashpad database instead, add the Microsoft symbol server for system modules, and a plain source checkout of tag 10.0.0 is needed only if you want to display source text.

Sources:
- https://symbols.kicad.org/kicad-stable/_pcbnew.pdb/64D0808226A44C06812F71472CD8A7B55/_pcbnew.pd_
- https://symbols.kicad.org/kicad-stable/kicad-cli.pdb/CBC90706EE2B4F5EBAF23E4DDE4328B45/kicad-cli.pd_
- https://symbols.kicad.org/kicad-stable/_pcbnew.pdb/139DA84C3BDB4AF6A2184CAAD7F6270A2/_pcbnew.pd_
- https://learn.microsoft.com/en-us/windows/win32/wer/collecting-user-mode-dumps
- https://gitlab.com/kicad/code/kicad/-/raw/10.0/common/app_monitor.cpp
- https://gitlab.com/kicad/code/kicad/-/raw/10.0/common/pgm_base.cpp
- <scratchpad>\verify-symbols-pdb\cdb_analyze2.log
- <scratchpad>\verify-symbols-pdb\cdb_wer.log
- <scratchpad>\verify-symbols-pdb\cdb_gen.log

### Empirical verifier — refuted=false

CONFIRMED by local testing against the exact installed build (KiCad 10.0.0.49194, C:/Program Files/KiCad/10.0/bin, PE timestamps 2026-03-26, RSDS PDB path C:\jenkins\workspace\build-win-10.0-stable\.out\x64-windows-Release-pdb\*.pdb).

1) Exact-match PDBs exist on the server. I parsed the CodeView RSDS GUID+age from the installed binaries and probed the symstore layout: kicad.exe -> 8D4D877A814B46ACA4B697D7D250BC9C5, pcbnew.exe -> 98E5AA937AF942869CC1829F63866FD35, _pcbnew.dll -> 64D0808226A44C06812F71472CD8A7B55, _eeschema.dll -> BCDE735CB4DA4973B0158155E57740C06, kicad-cli.exe -> CBC90706EE2B4F5EBAF23E4DDE4328B45, kicommon.dll -> 9AEDF265041941DDB93E9EF45FE5F2386. For every one, https://symbols.kicad.org/kicad-stable/<name>.pdb/<GUIDAGE>/<name>.pd_ returns HTTP 200 (MSCF cab-compressed; e.g. _pcbnew.pd_ = 54,527,416 bytes, kicommon.pd_ = 14,962,649). The uncompressed .pdb URL returns 404 (only the .pd_ form is stored), and pingme.txt returns 200, so symsrv/dbghelp handles it transparently. The server also stores the matching executable images (e.g. eeschema.exe\69C46108f4000\eeschema.exe was fetched by cdb).

2) They are FULL private PDBs, not stripped. symchk.exe /pf (require full source info) from Windows SDK Debuggers (C:\Program Files (x86)\Windows Kits\10\Debuggers\x64) PASSED for pcbnew.exe, _pcbnew.dll, kicommon.dll, _eeschema.dll, kicad-cli.exe, kicad.exe, kiapi.dll, kigal.dll with symbol path srv*<cache>*https://symbols.kicad.org/kicad-stable. Verbose output for pcbnew.exe: "DBGHELP: pcbnew - private symbols & lines", "Line nubmers: TRUE, Global syms: TRUE, Type Info: TRUE", PDB Matched TRUE. (First attempt failed with SYMSRV RESULT 0x57 only because I used a forward-slash cache path; a normal backslash path works.)

3) End-to-end on a REAL WER minidump: %LOCALAPPDATA%\CrashDumps contained eeschema.exe.89232.dmp (written 2026-09-18 22:08 by Windows Error Reporting LocalDumps; "User Mini Dump File: Only registers, stack and portions of memory are available"). `cdb -z <dmp> -y "srv*<cache>*https://symbols.kicad.org/kicad-stable;srv*<cache>*https://msdl.microsoft.com/download/symbols" -lines -c ".ecxr; kn 30; q"` resolved the crashing stack to file and line with zero local source or build: frame 00 `_eeschema!API_HANDLER_EDITOR::checkForBusy+0xd [C:\jenkins\workspace\build-win-10.0-stable\.build\kicad\common\api\api_handler_editor.cpp @ 207]` (Access violation c0000005, mov rax,[rcx] with rcx=0 - null deref), 01 `API_HANDLER_EDITOR::handleHitTest [api_handler_editor.cpp @ 325]`, 07 `kicommon!API_HANDLER::Handle [common\api\api_handler.cpp @ 55]`, 08 `kicommon!KICAD_API_SERVER::handleApiEvent [common\api\api_server.cpp @ 246]`, then wxWidgets frames also with file@line (wx PDBs are on the server too), 1c `eeschema!APP_SINGLE_TOP::OnRun [common\single_top.cpp @ 241]`. `lmvm` shows eeschema and _eeschema loaded as "(private pdb symbols)" from the cache. I also produced my own minidump of kicad-cli.exe under cdb (.dump /ma at a first-chance C++ exception) and cold-symbolicated it: `kicommon!NOTIFICATIONS_MANAGER::Load+0x1fd [common\notifications_manager.cpp @ 281]`, `kicad_cli!APP_KICAD_CLI::OnInit+0xcf [kicad\kicad_cli.cpp @ 632]`.

Documentary: https://symbols.kicad.org/ lists three channels (kicad-nightly, kicad-testing, kicad-stable) and states "Nightly and testing KiCad symbols are deleted over time to conserve disk space" (stable is retained; the kicad-stable index still lists 2024 entries). 10.0 branch source (common/app_monitor.cpp, SENTRY::sentryInit / isConfiguredOptedIn) and the KiCad blog (2022-04 Development Highlight) confirm the bundled Sentry/crashpad crash handler is strictly opt-in.

Operational preconditions (not refutations) for the architecture: (a) WER only writes local dumps when HKLM\...\Windows Error Reporting\LocalDumps exists (Microsoft "Collecting User-Mode Dumps"); on this machine it exists (created by other apps) so dumps land in %LOCALAPPDATA%\CrashDumps by default. (b) KiCad 10.0 ships crashpad_handler.exe/crashpad_wer.dll (Sentry); if the user opts in to data collection (marker file %LOCALAPPDATA%\KiCad\10.0\sentry-opt-in or policy), crashpad takes over the crash and writes its own minidumps under %LOCALAPPDATA%\KiCad\10.0\sentry\ instead of WER; those are still standard minidumps cdb can symbolicate the same way. This machine is opted in for 9.0 but NOT for 10.0. (c) PDBs are not source-indexed (srctool: "is not source indexed"); file@line refer to build-machine paths C:\jenkins\workspace\build-win-10.0-stable\.build\kicad\..., so viewing source text requires a git checkout of the 10.0.0 tag plus .srcpath (no build). (d) Add srv*https://msdl.microsoft.com/download/symbols for ntdll/kernelbase frames. (e) Fetching with curl requires expand.exe for the .pd_ cab; symsrv does this automatically.

Bonus finding relevant to the user's IPC-plugin plan: the real crash on this machine is a null-pointer AV in eeschema 10.0.0's IPC API server while handling a kiapi HitTest request (api_handler_editor.cpp:207 checkForBusy), i.e. KiCad 10.0.0 eeschema can be crashed by an IPC API client.

Scratch files: <scratchpad>\verify-symbols (rsds.py, symcache\, kicadcli.dmp, eeschema_wer.dmp copy, app_monitor.cpp, pgm_base_10.0.cpp).

Sources:
- https://symbols.kicad.org/
- https://symbols.kicad.org/kicad-stable/
- https://symbols.kicad.org/kicad-stable/_pcbnew.pdb/64D0808226A44C06812F71472CD8A7B55/_pcbnew.pd_
- https://symbols.kicad.org/kicad-stable/pcbnew.pdb/98E5AA937AF942869CC1829F63866FD35/pcbnew.pd_
- https://symbols.kicad.org/kicad-stable/kicommon.pdb/9AEDF265041941DDB93E9EF45FE5F2386/kicommon.pd_
- https://symbols.kicad.org/kicad-stable/_eeschema.pdb/BCDE735CB4DA4973B0158155E57740C06/_eeschema.pd_
- https://gitlab.com/kicad/code/kicad/-/raw/10.0/common/app_monitor.cpp
- https://gitlab.com/kicad/code/kicad/-/raw/10.0/common/pgm_base.cpp
- https://www.kicad.org/blog/2022/04/Development-Highlight-April-Update/
- https://learn.microsoft.com/en-us/windows/win32/wer/collecting-user-mode-dumps
- https://msdl.microsoft.com/download/symbols
