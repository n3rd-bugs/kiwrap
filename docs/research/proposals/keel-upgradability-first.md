# Keel — a version-isolated KiCad wrapper for AI agents (stability & upgradability lens)

Lens: STABILITY & UPGRADABILITY FIRST: survive KiCad 7 -> 10 -> 11+ and the SWIG -> IPC transition with a canonical IR, a driver/adapter layer, capability negotiation, per-version contract tests and an explicit format-drift strategy; optimise for 5-year maintainability.

# Keel — a version-isolated KiCad wrapper for AI agents

**Lens: stability and upgradability first.** The wrapper must survive KiCad 7 -> 10 -> 11+ and the SWIG -> IPC transition, and be maintainable for five years by a small team. Every decision below is made to keep KiCad an *exchangeable* backend.

---

## 1. Decisions at a glance

| Question | Decision |
|---|---|
| Where does design state live? | In **Keel IR**, a canonical semantic model on top of a lossless s-expression CST. KiCad files are *serialisations* of the IR, one emitter per format epoch. |
| Schematic creation (7-10) | **File-based** IR emitter + kicad-cli ERC/netlist validation. No API exists (10.0's handler crashes on writes). IPC schematic provider added for **11+ live sessions only**. |
| Board creation / netlist sync | **File-based** (board IR from netlist + footprint libs), verified by `kicad-cli pcb drc --schematic-parity`. IPC `ImportNetlist` on 11+ live only. |
| Placement | IR ops (file) or IPC `UpdateItems` in a commit (live); KiCadRoutingTools seeder for intent-driven placement. |
| Routing | External engines over Specctra DSN/SES: Freerouting 2.4.1 (default), KRT (second). SWIG export first, **Keel's own DSN/SES codec** as the SWIG-free path. PNS router: rejected. |
| DRC / ERC | kicad-cli JSON (8+). 7: SWIG DRC with assert shim, ERC declared gap. |
| Exports / fab | kicad-cli via a help-derived flag map; jobsets on 9+. |
| Live editing | IPC (9/10 PCB GUI, 11 both editors + headless), commits = undo steps. |
| In-KiCad plugin <-> VS Code | **Adopt thin hand-off plugin only** (Phase 4). Reject SWIG-server and C++ plugins. |
| KiCad source build | **Defer** to a one-day diagnostic bench in Phase 3; never a runtime dependency. |
| Agent surface | One Python MCP server (36 tools) consumed by Claude Code, Copilot, Cursor, Windsurf, Codex; VS Code extension hosts/previews/registers. |
| Version isolation | Installation abstraction + capability record keyed on full semver + epoch registry + sandboxed per-install processes + certification suite. |

---

## 2. Design principles

1. **KiCad is data, not code.** Keel's own interpreter never links a KiCad DLL. SWIG runs only in a throw-away subprocess of the install's own `python.exe`; IPC is protobuf over a socket; everything else is files and `kicad-cli`.
2. **Capabilities, never version numbers.** No tool tests `major >= 9`. It asks the capability record for `drc.cli`, `zones.fill.cli`, `ipc.headless`, and the planner picks a provider.
3. **Emit exactly what the target reads.** Older KiCad rejects newer stamps *and* unknown tokens; newer KiCad reads everything older. Therefore: one emitter per epoch, and default to the oldest epoch the user needs.
4. **Lossless by construction.** User files are edited through CST patches that preserve unknown tokens and formatting. Dataclass libraries that drop tokens (kiutils, kicad-sch-api) are banned from the write path.
5. **KiCad is the judge.** Every mutating tool returns KiCad-produced verdicts (ERC/DRC JSON, netlist) and a render; the model never declares success on its own.
6. **Drift becomes a failing test, not a support ticket.** Canaries snapshot `--help` trees, IPC handler sets, `pcbnew.py` symbols and `sch/pcb upgrade` diffs for every install.
7. **Every optional surface has a non-optional twin.** SWIG (dies in 11) is paired with a file path; GUI IPC (9/10) is paired with headless IPC (11) behind one session abstraction.

---

## 3. Component architecture

```
 Agents: Claude Code | Copilot agent mode | Cursor | Windsurf | Codex | Copilot CLI
                          |  MCP 2026-07-28 (stdio / streamable HTTP)
                    +-----v-----------------------------------------------+
   VS Code ext ---->|  keel-mcp (Python)  36 tools, jobs, locks, images   |
   (TypeScript)     +-----+-----------------------------------------------+
   preview / diag /       |
   register / spawn  +----v------------------------------------------------+
                     |  keel-drivers: Installation, CapabilityRecord,      |
                     |  Sandbox, Planner, Providers                        |
                     |  FILE | CLI | SWIG | IPC | ROUTER                   |
                     +--+------+-------+--------+--------+-----------------+
                        |      |       |        |        |
                 keel-core  kicad-cli  keel-swig-  nng/protobuf  Freerouting (JRE 25)
                 CST+IR+    <ver>/bin  worker in   -> GUI 9/10   KiCadRoutingTools
                 epochs               <ver>/bin/   -> api-server
                                      python.exe      11+
                                                      ^
                                     keel-kicad-plugin (hand-off: socket+token)
```

| Component | Language | Responsibility | Reaches KiCad via |
|---|---|---|---|
| **keel-core** | Python 3.11, stdlib only | Lossless CST, semantic IR (project/schematic/symbol/board/footprint/rules), epoch registry (lens + emitter per epoch), IR-op -> CST-patch compiler, verified geometry, report/netlist parsers, DSN writer / SES reader | Files only |
| **keel-drivers** | Python 3.11 | Install discovery/identification, CapabilityRecord (seeded + probed), sandbox env, providers (File/Cli/Swig/Ipc/Router), planner | kicad-cli subprocess; bundled python subprocess; nng socket |
| **keel-swig-worker** | Python 3.9/3.11-compatible, stdlib | One-board-per-process JSON-lines RPC worker with per-major shims, assert shim, ownership rules | `import pcbnew` under `<install>/bin/python.exe` |
| **keel-mcp** | Python (mcp >= 2.2) | Tool catalog, job manager, per-project locks, editor-lock refusal, images, prompts | keel-drivers only |
| **keel-vscode** | TypeScript | MCP registration, agent-config writer, custom editor previews (kicad-cli SVG), Problems diagnostics, tasks, install picker, live-session supervisor, 3 LM-tool facades | Spawns keel-mcp; spawns sandboxed kicad.exe for live sessions |
| **keel-kicad-plugin** | plugin.json + ~40-line Python | 'Connect to Keel' button in pcbnew and eeschema (9/10/11); posts socket+token to Keel and exits | Launched by KiCad's plugin manager |
| **keel-conformance** | pytest + corpus | T1-T9 contract tests and drift canaries; `keel certify` | All surfaces of every install |
| **keel-runtime** | Python manager; external engines | Pinned Freerouting + private JRE 25, KRT venv, easyeda2kicad venv, PyMuPDF | None (consume DSN / .kicad_pcb) |
| **keel-skills** | Markdown (Agent Skills) | Workflow, geometry, triage, placement, routing, version-awareness | n/a |

---

## 4. KiCad integration strategy per job

| Job | Primary surface (versions) | Optional/live surface | Why |
|---|---|---|---|
| Schematic create/edit/annotate/assign | FILE: IR -> epoch emitter; CST patches for user files (7-10, default on 11) | IPC schematic handler (11+ live) | No schematic API through 10; 10.0.0 crashes on CreateItems/BeginCommit; file path is headless and identical on all versions |
| ERC | `kicad-cli sch erc --format json --exit-code-violations` (8+), x100 quirk corrected | — | Only ERC executor that exists; 7 declared gap |
| Netlist / BOM | `kicad-cli sch export netlist/bom` (7+/8+), real s-expr parser | — | 10 reformatted netlists one-token-per-line; regex parsing breaks |
| Board creation / sync from netlist | FILE: board IR from netlist + `.kicad_mod` definitions resolved via project+global fp-lib-table; verified by `pcb drc --schematic-parity` | IPC `ImportNetlist` (11+ live); SWIG FootprintLoad as test oracle | Neither kicad-cli nor IPC 9/10 nor SWIG has netlist import; file path survives SWIG removal |
| Placement | FILE IR ops; KRT `place_seed/optimize` | IPC `UpdateItems` in commit (9+ live); RunAction autoplacer opt-in seed | Headless first; live for humans watching |
| Routing | Freerouting over DSN/SES (SWIG export 7-10 -> Keel codec); KRT file-based (9/10) | IPC CreateItems to push routed copper into an open board | PNS not scriptable (SWIG/IPC lack it; RunAction blocks) |
| Zone fill | `kicad-cli pcb drc --refill-zones --save-board` (10+); SWIG ZONE_FILLER (7-9) | IPC RefillZones (9+ live) | Stale fills cause phantom DRC errors |
| DRC | `kicad-cli pcb drc --format json --schematic-parity` (8+) | SWIG WriteDRCReport + assert shim (7 only) | WriteDRCReport is broken headless on 10, hangs on 9 |
| Exports / fab | `kicad-cli pcb/sch export *`, `jobset run` (9+) via help-derived flag map | IPC RunBoardJobExport* (11+ live) | Flags drift per version; the map makes drift data |
| Renders | `pcb render` (9+); `pcb/sch export svg/pdf` + PyMuPDF (7+) | — | Vision loop without GUI |
| Live editing | IPC GUI (9/10 PCB) ; IPC headless `kicad-cli api-server` (11) | hand-off plugin | Same session abstraction for both |
| LCSC parts | easyeda2kicad + `kicad-cli sym/fp upgrade` | — | Verified 4 s import |

---

## 5. Canonical IR and document model

### 5.1 Two tiers

* **Tier 0 — CST** (`keel_core.sexpr`): a concrete syntax tree of atoms/lists with original quoting, whitespace and comments retained. Round-trip of an unmodified file is byte-identical; a patched file changes only the patched subtree. Unknown tokens are opaque nodes and survive every edit. This is what makes reading a *newer* file with an *older* Keel safe.
* **Tier 1 — IR** (`keel_core.ir`): semantic dataclasses. Units are integer nanometres (board) and integer 1/10000 mm (schematic) with helpers; booleans are booleans; nets are referenced **by name**; layers **by canonical name** (`F.Cu`, `B.Cu`, `Edge.Cuts`); ids are UUIDs (tstamp lifted to uuid); properties are ordered maps; hidden/visibility flags are fields, never tokens.

Key IR types: `Project` (meta version, sheets list, net settings, rules, variants), `Schematic` (`SchSymbol` with lib_id/at/rot/mirror/unit/body_style/properties/pins/instances paths, `Wire`, `Junction`, `Label{local,global,hier,directive}`, `NoConnect`, `SheetSymbol` + `SheetPin`, embedded `LibSymbol` set), `LibSymbol` (units, body styles, pins in Y-up symbol space, `extends` resolved), `Board` (`Footprint` with pads/shapes/models/properties, `Track`, `Via`, `Zone` with outline + fills, `Shape`, `Text`, `Group`, `Net`, `NetClass`, `Stackup`, `Rules`, layer table), `FootprintDef`, `Rules` (.kicad_dru text kept opaque).

### 5.2 Lenses and emitters

For each **epoch** the registry holds `(stamp, lens, emitter, certified_installs)`:

| File | Epochs (stamp) |
|---|---|
| .kicad_sch | 20230121 (7), 20231120 (8), 20250114 (9), 20260306 (10); 20260830 (11-dev, uncertified) |
| .kicad_pcb / .kicad_mod | 20221018 (7), 20240108 (8), 20241229 (9), 20260206 (10); 20260901 (11-dev) |
| .kicad_sym | 20220914, 20231120, 20241209, 20251024 |
| .kicad_pro meta | 1 (7/8), 3 (9/10), 4 (11-dev) |

A **lens** is tolerant: it accepts bare `hide` and `(hide yes)`, `tstamp` and `uuid`, `fp_text reference` and `(property "Reference")`, `(net 2 "GND")` and `(net "GND")`, both layer-ID tables, `true/false` and `yes/no`, `(power)` and `(power global)`. An **emitter** is strict: it writes exactly one dialect (token set, ordering, indentation) and the exact stamp, validated by T2 against every install that must read it.

### 5.3 Editing model

* New documents: IR -> emitter.
* Existing documents: `IR.apply(ops)` produces a list of **IR ops** (`SetProperty`, `AddItem`, `RemoveItem`, `MoveItem`, `SetNet`, ...) that the patch compiler turns into CST edits at the epoch of the file, so a KiCad-10 file stays a KiCad-10 file and nothing else moves.
* Report parsers normalise ERC/DRC/netlist/stats into IR-level types (`Violation{type, severity, items[{pos_mm, uuid}]}`), applying probed quirks (ERC x100, stats unit strings).

---

## 6. Driver/adapter layer and capability negotiation

### 6.1 KiCadInstallation

`{root, kicad_cli, python_exe, python_tag (cp39|cp311), share{symbols, footprints, 3dmodels, template, schemas}, semver (from 'kicad-cli version --format plain'), commit (--format commit)}`. Discovery: HKLM Uninstall keys (DisplayName 'KiCad X.Y', InstallLocation; 7 lives under WOW6432Node), glob `C:/Program Files/KiCad/*/bin/kicad-cli.exe`, PATH, explicit settings; Linux/macOS by path/glob. Cached per root.

### 6.2 CapabilityRecord (keyed on full semver + commit)

```
cli:   subcommands (help tree), flags per subcommand, has_erc, has_drc, has_render, has_jobset,
       has_upgrade, exit_codes_stable, help_exit_nonzero
files: epochs_written {sch, pcb, sym, pro_meta}, reads_epochs <=
ipc:   present, headless, socket_default, handlers{name: handled|unhandled|crash_risk}, since_table
swig:  present, python_tag, symbols{ExportSpecctraDSN, ImportSpecctraSES_2arg, ZONE_FILLER, WriteDRCReport_ok, PCB_IO_MGR|IO_MGR, VIATYPE_consts}
quirks (probed): erc_json_pos_div100, gerbers_creates_outdir, writes_prl_side_file, ships_wrl, first_run_wizard, netlist_pinfunction_name_number, eeschema_write_crash
engines: freerouting{jar, java_ok}, krt{ok}, easyeda2kicad{ok}
```

Seeded from `matrix/kicad_version_matrix.json` (the verified 7.0.6/8.0.1/9.0.5/10.0.0 data) and **re-probed** on first use of an install: help trees parsed with exit status ignored; IPC handlers probed by sending each known command and classifying `no handler available` (only against pcbnew/kicad.exe — never editor messages to eeschema on 10.x); SWIG symbols probed in the worker; quirks probed with cheap experiments (an ERC on a shipped demo compares JSON vs text positions).

### 6.3 Planner

Each operation declares an ordered provider list with predicates, e.g.

```
zones.fill:  [Ipc(live_bound), Cli(has_drc and flag('pcb drc','--refill-zones')), Swig(ZONE_FILLER)]
board.build: [File(epoch certified for target)]; verify: [Cli(has_drc)]
drc.run:     [Cli(has_drc), Swig(WriteDRCReport_ok)]  # 10: Swig predicate false
route.dsn:   [File(keel_dsn_certified), Swig(ExportSpecctraDSN)]
```

The result carries `provider_used` and `degraded: [reasons]`, surfaced to the agent so it can reason about gaps ("ERC unavailable on KiCad 7").

### 6.4 IPC provider

Protobufs are generated from **vendored** `api/proto` of the newest supported tag (wire policy is additive-only; 10.0.6-built protos worked against 9.0.5 and 10.0.0), with a `Since` table parsed from `// Since: x.y.z` comments. kipy is not a dependency (0.8.0 wheel is internally broken). Connection: socket from hand-off > `KICAD_API_SOCKET` env > default `ipc://<TEMP>/kicad/api.sock`; empty token first, adopt the returned one; Ping until `AS_NOT_READY` clears; `AS_BUSY` retried with backoff (especially after `RefillZones` and `RunAction`); every mutating tool call is one `BeginCommit`/`EndCommit`. Session abstraction: `start()` spawns `kicad.exe` (9/10) or `kicad-cli api-server --socket` (11+); `open_document()` is a command-line argument (9/10) or `OpenDocument` (11+).

### 6.5 SWIG worker

Spawned as `<install>/bin/python.exe -u -X faulthandler worker.py` with `KICAD_CONFIG_HOME`, `KICAD<N>_FOOTPRINT_DIR/SYMBOL_DIR/3DMODEL_DIR`; nulls `wxTheAssertHandler` via ctypes right after `import pcbnew`; one BOARD per process; `board.SetFileName` always; `SaveBoard(..., aSkipSettings=True)`; outlines via `Outline()` in place or `AppendCorner`; `BuildConnectivity()` before `ZONE_FILLER.Fill`; layers via `board.GetLayerID("B.Cu")`; shims: `EDA_UNITS_MILLIMETRES` (7) vs `EDA_UNITS_MM` (8+), `VIATYPE_*` missing on 10 (ints), `IO_MGR` (7) vs `PCB_IO_MGR` (8+), 7's one-argument `ImportSpecctraSES` replaced by Keel's SES reader. Hard timeout and kill by the parent.

---

## 7. Version isolation and format-drift strategy

1. **Emit policy.** `target_epoch = install.epoch if certified else newest certified epoch <= install`. Projects pin their target in `keel.toml` (default: the oldest KiCad the user wants to open the project with). Never emit an uncertified epoch.
2. **Read policy.** Files newer than the newest known epoch are parsed via CST plus the newest lens with warnings; read-only tools work; mutating tools refuse unless `allow_uncertified=true`. Uncertified installs still get kicad-cli-based tools (ERC/DRC/exports) because those depend only on the help-derived flag map.
3. **Sandbox per install.** `KICAD_CONFIG_HOME=<storage>/kicad-config` (KiCad appends `<major.minor>/`), provisioned with `kicad_common.json` (`api.enable_server`, `do_not_show_again.*`, `system.first_run_shown`), `sym-lib-table`/`fp-lib-table` copied from `share/kicad/template`, an empty `design_block_lib_table` on 9/10; `KICAD_DOCUMENTS_HOME` and `KICAD_CACHE_HOME` under storage (also dodges Controlled Folder Access); output dirs always created; `.kicad_prl` side files ignored. The user's `%APPDATA%/kicad` is never touched.
4. **Certification** (`keel certify`): an install is *supported* only when T1-T9 pass. Point releases that pass unchanged are auto-supported. Failing canaries create a triage task with the diff.
5. **Deprecation ladder.** SWIG is optional from day one (file twins for board build, DSN/SES, zone fill). GUI IPC and headless IPC share one session abstraction. When 11 removes SWIG, a provider disappears; no tool changes.
6. **Upgrade budget.** <= 1 day per point release; <= 1 week per major (new epoch entry, lens/emitter deltas, matrix update, certification).

### Contract tests (run per installed KiCad)

| Test | What it proves |
|---|---|
| T1 CST round-trip | Token-multiset equality; kicad-cli ERC/DRC violation sets and netlists equal before/after on the corpus |
| T2 Epoch certification | Reference designs emitted at epoch E load with 0 ERC/DRC and expected netlist on every install >= E; boards re-saved by SWIG (7-10) are semantically identical |
| T3 Negative gates | Emission at epoch > V is rejected by V (exit 3/127); an injected unknown token is rejected by 7/8 — proves the gate the emit policy relies on |
| T4 CLI snapshot | `--help` trees per semver stored; diff on a new install flags added/removed subcommands/flags |
| T5 IPC snapshot | Probed handler set per semver; diff flags new/removed commands and crash-risk ones |
| T6 Quirk probes | ERC x100, gerbers outdir, .kicad_prl side file, help exit code, .wrl availability, first-run wizard conditions |
| T7 SWIG worker | Symbol presence, shims, ownership patterns (fill area > 0, 20 consecutive saves without crash), DSN/SES |
| T8 E2E golden flow | Intent -> schematic -> board -> route -> DRC -> fab bundle; semantic hashes compared |
| T9 Drift canaries | `sch/pcb upgrade` on a copy + semantic diff; `pcbnew.py` def/class diff; nightly 11 run |

---

## 8. Schematic strategy

Schematics are generated and edited **on disk** for every currently released KiCad, because no other path exists: SWIG has no eeschema module; KiCad 10.0.0's IPC schematic handler answers only `GetOpenDocuments` and crashes on `HitTest`/`CreateItems`/`BeginCommit`; kipy's `Schematic` class is marked KiCad 11.

Emitter rules (all verified with kicad-cli): embed every used symbol in `lib_symbols` by copying the installed library's block verbatim, prefix only the top-level name (`Device:R`), keep sub-units unprefixed, flatten `extends` chains; place pins with the verified transform; split wires at every pin and PWR_FLAG; snap to 1.27 mm; put PWR_FLAG on power-input nets; write references in both the `Reference` property and the `instances` block with `/ROOT_UUID[/SHEET_UUID]` paths; `sheet_instances` only in the root; per-epoch dialect (v7 bare `hide`/unquoted, v8 `(hide yes)` in effects + `exclude_from_sim`, v9 `(pin_numbers (hide yes))` + `embedded_fonts`, v10 property-level `hide`/`show_name`/`do_not_autoplace`, `body_style`, `in_pos_files`, `(power global)`, Datasheet `""`).

Validation after every write: `sch erc --format json --exit-code-violations` (positions x100), `sch export netlist`, and **`sch_verify_intent`** — the netlist-vs-intent diff that catches "label on the wrong pin", which ERC cannot. Renders come from `sch export svg/pdf` + PyMuPDF with ERC markers overlaid.

On KiCad 11 the IPC schematic provider (`PlaceSymbolFromLibrary`, `CreateItems`, `GetSchematicNetlist`, `SaveDocument`) backs the same `sch_*` tools *when a live session is bound*; the emitter remains the default and the CI path so results are reproducible without a GUI.

---

## 9. Board creation, placement and routing

**Board creation / sync.** `pcb_sync_from_schematic` parses the kicadsexpr netlist, resolves each footprint through project + global `fp-lib-table` (following nested `Table` entries on 10), reads the `.kicad_mod` with the footprint lens, embeds it in the board IR with the correct `LIB_ID` nickname, creates nets (numeric table for <10, name-only for 10), assigns pads, keeps existing positions, removes orphans, emits the target epoch, then runs `pcb drc --schematic-parity`. SWIG `FootprintLoad` is the test oracle, not the runtime path.

**Placement.** Intent JSON (edge connectors, zones by ref glob, keepouts, decap tethers, locks) -> KRT `place_seed` -> `place_optimize`; or explicit coordinates as IR ops / IPC `UpdateItems`. Each batch returns overlaps, off-board items and a top-2D render with ratsnest.

**Routing.** `pcb_route_auto` snapshots the project, locks nets the agent wants kept (SES import wipes unlocked copper and applies SES placement), exports DSN (SWIG on 7-10 until Keel's codec is certified, then Keel's codec everywhere), runs Freerouting 2.4.1 through its REST API (private JRE 25, settings derived from board rules: neck-down off or `neck_width_um >= min track`, fanout off unless BGAs, `copper_to_edge_clearance_um`, `-inc` plane nets, `-mt 1`), imports the SES, refills zones, runs DRC and classifies violations. KRT is the no-Java engine on 9/10. `pcb_route_edit` handles touch-ups with per-net DRC deltas. The PNS router is out of scope: not in SWIG, not in IPC, `RunAction` only starts the interactive tool and then answers `AS_BUSY`; driving it needs a patched fork (PCBWorld-Engine), which the lens forbids.

---

## 10. DRC, ERC and exports

* **DRC:** `kicad-cli pcb drc --format json --severity-all --schematic-parity [--refill-zones --save-board on 10+] --exit-code-violations` (8+). Classification: must_fix = clearance, shorting_items, tracks_crossing, unconnected_items, track_width, hole_clearance; review = courtyards_overlap, solder_mask_bridge, lib_footprint_issues; cosmetic = silk_*. On 7: SWIG `WriteDRCReport` with the assert shim and `KICAD7_FOOTPRINT_DIR` set.
* **ERC:** kicad-cli (8+) with the x100 correction from the probed quirk; on 7 the tool returns `isError` with "ERC unavailable on KiCad 7.0.x; upgrade the install or run ERC in the GUI".
* **Exports:** the CLI flag map is generated from `--help` of the selected install, so `--variant` (10), `--mode-single` (9), removed `pcb export gerber` (10), `hpgl` stub, `--exclude-dnp` (8) are data. Profiles (`jlcpcb`, `generic`) map to flags; unsupported targets on a version are reported, not silently skipped. `.wrl` model references are rewritten to `.step` when exporting on 10.

---

## 11. Verdicts: in-KiCad plugin channel and source build

### 11.1 In-KiCad plugin <-> VS Code extension — ADOPT thin, REJECT thick

**What a plugin buys over an external IPC client + kicad-cli:** (1) KiCad launches it with `KICAD_API_SOCKET`/`KICAD_API_TOKEN`, solving Windows named-pipe discovery and the fixed-`api.sock` collision; (2) a discoverable "Connect to Keel" button in both the PCB editor and the Schematic editor on 9/10/11 (scope `schematic` buttons are rendered by eeschema 10 despite the docs); (3) PCM-managed install/update. **What it does not buy:** any API — an IPC plugin is an external process with exactly the protobuf surface the extension already has; no schematic editing before 11; no DRC/ERC; no PNS router; no event push.

**Adopted form (Phase 4, ~2 days):** `plugin.json` (api.v1, runtime `python` without requirements, one action scoped `['pcb','schematic']`) whose script POSTs `{socket, token, pid, document}` to the Keel daemon's loopback endpoint (random port + 0600 token lock file, the pattern Claude Code's own `ide` server uses), optionally launches `code <project>`, logs to a file (KiCad drains plugin stdout only at exit) and exits. Long-lived work stays in `keel-mcp`, so terminal agents keep every capability.

**Rejected:** SWIG action plugin hosting a socket server inside pcbnew (pcbnew-only, mutations outside `Run()` bypass undo/connectivity/refresh, freeze history, removed in 11); C++ "handler plugin" (does not exist; only a patched fork runs code inside KiCad, the opposite of "upgrade KiCad underneath"); making the VS Code extension the terminus of the channel (Cursor/Codex/CLI agents would lose it).

### 11.2 KiCad source build — DEFER (Phase 3 diagnostic bench, one day, never a dependency)

Everything Keel needs on 7-10 was reproduced on installed binaries. Both KiCad-side crashes encountered (eeschema `BeginCommit` null `m_frame` = upstream #24966; `WriteDRCReport` `Pgm()` assert on 10) were fully diagnosed with WER minidumps + cdb + PDBs from symbols.kicad.org in under an hour. The next API surface Keel wants (schematic RPCs, headless `api-server`, export jobs, `ImportNetlist`) already ships in KiCad 11 nightlies, so **installing kicad-nightly** is the cheap way to develop against it. Any handler we would add (RunDRC/ERC over API, marker retrieval — absent even on master) only helps users when upstreamed, so it must target master anyway.

**Triggers that flip to "build now":** an IPC handler misbehaves and `api.log` + minidump cannot explain it; we commit to an upstream MR; we need unoptimised locals stepping through specctra export or PNS.

**Recipe when triggered** (machine: VS2022 17.9/MSVC 14.39, CMake 3.31, Ninja, cdb, 24 threads, 127 GB RAM, 715 GB free): update VS 2022 to 17.14.x so vcpkg binary-cache hashes match CI's MSVC 14.44; clone vcpkg at kicad-win-builder `build.ps1`'s pinned commit to a drive root; add the `kicad-gitlab` NuGet source; install swigwin 4.3.1; `msvc-win64-debug` preset; `KICAD_RUN_FROM_BUILD_DIR=1`. Budget: 1-2 h setup, 10 min-5 h dependencies, 30-60 min build, 40-80 GB disk.

**Debug toolkit until then:** `EnableAPILogging=1` in `kicad_advanced` (full request/response transcript), WER minidumps, `cdb -z dump -y srv*cache*https://symbols.kicad.org/kicad-stable`, source view from the matching tag, and the failing conformance test as the reproducer.

---

## 12. Agent surface and VS Code

* **keel-mcp**: mcp >= 2.2 `MCPServer`, stdio default, `--http` streamable (stateless). 2026-07-28 rules: no Roots/Sampling/Logging dependence; project path, `job_id`, `session_id` are explicit args; deterministic tool order; `isError` with actionable text; `_meta['anthropic/maxResultSizeChars']` on report tools; images <= 1600 px; `structuredContent` + `outputSchema` for reports; `resource_link` for large artifacts. Long jobs return `job_id` immediately (Codex 60 s default tool timeout, Claude Code idle timeout).
* **Registration**: repo `.mcp.json` (Claude Code + VS Code Agent Host), `.vscode/mcp.json` (Copilot local harness; `dev.watch`/`dev.debug` for Python breakpoints), `.cursor/mcp.json`, snippets for Codex `config.toml`, Windsurf, Copilot CLI. The extension registers the same server via `mcpServerDefinitionProviders` and writes the other files on command.
* **Skills**: `.claude/skills/keel-flow/SKILL.md` (spec-only frontmatter; read by Claude Code and Copilot), plus `AGENTS.md`/`CLAUDE.md`.
* **VS Code UI**: read-only custom editors for `.kicad_sch`/`.kicad_pcb` (kicad-cli SVG in a webview, "Open in KiCad"), Problems diagnostics from ERC/DRC, Keel tree view (installs, stage, jobs, live session), task provider, install picker, live-session supervisor, three `languageModelTools` facades with confirmation UI (Copilot only).
* **Tool budget**: 36 tools (Cursor forwards 40; Windsurf 100; VS Code 128) using action/format enums and `keel_help` for depth.

---

## 13. Tool catalog (36)

| Tool | Stage | Args (abridged) | Returns | Backend |
|---|---|---|---|---|
| keel_env | project | install?, refresh? | installs, capabilities, quirks, engines, live | drivers probe |
| project_create | project | name, dir, target_kicad, template? | paths, target_epoch, git_head | FILE |
| project_status | project | project | files, stage, last_erc/drc, unrouted, locks | FILE+git |
| project_history | project | action snapshot/restore/diff/list, sha? | sha / restored / semantic diff | git+IR |
| project_upgrade | project | to_kicad, dry_run? | semantic_diff, erc/drc before/after | kicad-cli upgrade / re-emit |
| design_intent_set | requirements | intent{blocks, interfaces, rails, constraints, nets} | validation_errors | FILE |
| lib_search_symbols | parts | query, filters | [{lib_id, pins, fp_filters,...}] | FILE index |
| lib_symbol_info | parts | lib_id, unit?, render? | pins (symbol space), bbox, png? | FILE + sym export svg |
| lib_search_footprints | parts | query, pads?, mount? | [{fpid, pads, bbox,...}] | FILE index |
| lib_footprint_info | parts | fpid, render? | pads, courtyard, png? | FILE + fp export svg |
| parts_import_lcsc | parts | lcsc_id | lib_id, fpid, price, stock | easyeda2kicad + upgrade |
| sch_add_symbol | schematic | sheet, lib_id, ref?, value, at, rot, mirror, unit, fields | uuid, ref, pins at sheet | FILE / IPC(11) |
| sch_add_item | schematic | kind power/pwr_flag/no_connect/text/labels/wire | uuid | FILE |
| sch_connect | schematic | net, endpoints, style | items_created, junctions | FILE |
| sch_add_sheet | schematic | parent, name, file, at, size, pins | sheet_uuid, path | FILE |
| sch_annotate | schematic | scheme, start_per_sheet | mapping | FILE |
| sch_assign_footprints | schematic | map / rules | assigned, unresolved | FILE |
| sch_get | schematic | sheet?, refs?, bbox?, include | JSON slice | FILE lens / IPC(11) |
| sch_render | schematic | sheet, region?, overlay | image | kicad-cli + PyMuPDF |
| sch_erc | schematic | severity | typed violations (pos fixed) | kicad-cli (8+) |
| sch_netlist | schematic | format | components, nets | kicad-cli + parser |
| sch_verify_intent | schematic | — | missing/extra/wrong_pin | keel-core |
| pcb_setup | board | outline, layers, rules, netclasses, preset | applied | FILE / IPC(11) |
| pcb_sync_from_schematic | board | remove_orphans, keep_positions | added/updated/removed, parity | FILE + drc parity / IPC(11) |
| pcb_get | board | refs?, bbox?, include | JSON slice, unconnected | FILE / IPC live |
| pcb_place | placement | mode absolute/relative_to/seed, placements, intent | new_state, overlaps, grade | FILE / IPC / KRT |
| pcb_render | board | view, layers, region, overlay | image | kicad-cli |
| pcb_route_auto | routing | engine, nets, passes, timeout, async | job_id -> routed/unrouted/drc | DSN/SES + Freerouting / KRT |
| pcb_route_edit | routing | ops track/via/unroute | added, drc_delta | FILE / IPC |
| pcb_zones | routing | zones, fill | zone_uuids, filled_area | FILE + cli/SWIG/IPC fill |
| pcb_drc | drc | parity, refill, severity | classified violations | kicad-cli (8+) / SWIG (7) |
| fab_export | fab | targets, profile/jobset, out_dir | files, unsupported | kicad-cli / IPC jobs (11) |
| fab_bom | fab | fields, group_by, with_lcsc | csv, rows, stats | kicad-cli + EasyEDA |
| live | live | action attach/start/stop/status/pull/push/selection | session, open_docs, result | IPC + supervisor + hand-off |
| job_status | jobs | job_id, cancel? | state, progress, result | job manager |
| keel_help | help | topic | markdown | docs + flag map |

---

## 14. Repo layout

```
keel/
  README.md  LICENSE(MIT)  AGENTS.md  CLAUDE.md  .mcp.json  .vscode/mcp.json  .cursor/mcp.json
  .claude/skills/keel-flow/SKILL.md
  pyproject.toml (uv workspace)
  packages/
    keel-core/        sexpr/(cst, writer, patch)  ir/  epochs/(registry, sch_*, pcb_*, sym_*, pro_meta)
                      geometry/  libs/(symtable, fptable, index, flatten)  reports/  interchange/(dsn_writer, ses_reader)
    keel-drivers/     installation.py capability.py sandbox.py planner.py quirks.py
                      providers/(file, cli, cli_helpmap, swig, ipc, router_freerouting, router_krt)
                      matrix/kicad_version_matrix.json  proto/{9.0,10.0,master}/  proto_gen/
    keel-swig-worker/ worker.py shims.py wx_noassert.py ops/(board, footprints, nets, zones, specctra, plot)
    keel-mcp/         server.py tools/(project, parts, schematic, board, routing, drc, fab, live, jobs, help) jobs.py locks.py images.py prompts.py
    keel-runtime/     freerouting.py jre.py krt.py easyeda.py health.py
  plugin/keel-kicad-plugin/   plugin.json connect_keel.py icons/ pcm/metadata.json
  vscode/keel-vscode/         package.json src/(extension, mcpProvider, configureAgents, preview/, diagnostics, tasks, liveSession, lmTools)
  conformance/       corpus/{7.0,8.0,9.0,10.0,11-nightly}/  tests/t1..t9  snapshots/{cli-help,ipc-handlers,pcbnew-api}/  certify.py
  skills/            keel-flow kicad-geometry erc-triage placement routing part-selection version-awareness
  docs/              architecture ir epochs capability-record drift-playbook source-build-bench agent-clients
  .github/workflows/ conformance-windows.yml lint.yml package.yml
```

Rules: `keel-core` has zero runtime dependencies; `keel-swig-worker` is copied into the KiCad interpreter, never imported by Keel; nothing outside `keel-drivers` knows a KiCad version number.

---

## 15. Roadmap

| Phase | Weeks | Deliverables | Exit criteria |
|---|---|---|---|
| **0 Foundation & harness** | 3 | Install discovery + CapabilityRecord; CST + patches; CliProvider + flag map; report parsers; sandbox; corpus + T1/T4/T6; MCP skeleton (keel_env, sch_erc, pcb_drc, renders, fab_export, project_history, keel_help) | T1 lossless on all corpora for 7/8/9/10; capability maps generated with no hardcoded version tests; the six tools run unchanged on all four installs; Claude Code and Copilot connect |
| **1 Schematic** | 4 | Schematic/symbol IR, lenses, emitters 7/8/9/10; lib index; all sch_* tools, intent tools, LCSC import; T2/T3/T9(sch); skills | Reference projects emitted per epoch load with 0 ERC and expected netlist on every install >= epoch; `sch upgrade` canary clean; an agent builds an ERC-clean ESP32-S3/USB-C/charger schematic with tools only |
| **2 Board, routing, fab** | 5 | Board/footprint IR + emitters; sync, setup, get, place (+KRT seed), zones, route_edit; SWIG worker; DSN/SES via SWIG + Freerouting REST + KRT; **Keel DSN/SES codec (2b)**; fab_export/fab_bom; project_upgrade; T7/T8 | Headless intent -> fab bundle on 8/9/10 (7 with declared gaps); Keel codec reaches 0 unconnected / 0 must-fix without SWIG; emitted boards SWIG-round-trip semantically identical |
| **3 VS Code, agents, bench** | 3 | Extension (MCP provider, configure-agents, previews, diagnostics, tasks, picker, LM facades); client smoke tests; kicad-nightly in matrix + T5; **one-day source-build bench** + symbol-server playbook; packaging | Same flow from Copilot, Claude Code, Cursor with no per-client code; nightly drift report against 11; bench completes or blocker documented; `.vsix` installs |
| **4 Live sessions & 11** | 3 | IpcProvider (generated protos, Since table, retries, commits); `live` tool (GUI 9/10, api-server 11); hand-off plugin + PCM; 11-only providers | Agent pushes placement/routing into open pcbnew as single undo steps on 9.0.5/10.0.0 and pulls edits back; identical calls pass headless on 11-nightly; plugin binds without guessing the pipe |
| **5 Sustained certification** | ongoing | Release-day playbook; quarterly pin review; upstream MRs when justified | Point releases supported with no code change; majors within one week; no tool signature changes for a KiCad upgrade |

---

## 16. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Board emitter fidelity drifts from KiCad's own output | Emitters derived from per-version reference saves; T2 loads every emission with that version and SWIG-round-trips it; T3 proves the gate; pinned target epoch |
| KiCad 11 lands (new stamps, meta 4, SWIG gone) before the codec/headless provider is certified | Nightly runs from Phase 3; Keel DSN/SES is a Phase 2b deliverable with SWIG as oracle; planner degrades explicitly; emit policy falls back to newest certified epoch |
| SWIG worker crashes/hangs | Throw-away subprocess, timeout, faulthandler, assert shim, one board per process, ownership rules, T7; SWIG is never sole provider |
| IPC instability on 9/10 (pipe collision, AS_BUSY, eeschema write crash, no headless) | Live sessions optional; Keel owns the process; retries; never write to eeschema on 10.x; hand-off for socket; single-session lock |
| External engine drift (Java 17 -> 25 in 18 months, KRT churn, broken kipy wheel) | Pin and download engines + JRE; health checks; two routers; generate protos from vendored KiCad sources |
| Semantic drift without a stamp change | T9 canaries + probed quirks; CST keeps unknown tokens |
| Agent misuse (hand-routing, guessing names, self-judged success, context blow-up) | Skills; tools return pin endpoints and chunked reads; KiCad verdicts on every mutation; 1600 px images; 36 tools |
| Windows traps (Controlled Folder Access, non-ASCII temp, .kicad_prl, outdir on 7/8) | Sandbox env, mkdir, gitignore, hand-off discovery, CREATE_NO_WINDOW, T6 |
| Licence contamination | Keel MIT; GPL/AGPL engines as separate processes; no AGPL code copied |
| Bench becomes a fork | Policy: nothing may require a patched KiCad; stock installers only in conformance |

---

## 17. Key technical claims (each testable)

1. kicad-cli 8.0.1/9.0.5/10.0.0 `pcb drc --format json` emit drc.v1 reports with correct per-item millimetre positions and uuids.
2. kicad-cli 8.0.1/9.0.5/10.0.0 `sch erc --format json` positions are 100x too small versus the text report.
3. kicad-cli 7.0.6 has no `pcb drc`, `sch erc`, `pcb render`, `jobset` or `upgrade`.
4. A file with a newer (version) stamp than the reader is rejected (exit 3/127); newer KiCad reads all older stamps.
5. A single unknown token makes 7.0.6/8.0.1 fail to load a file.
6. Stamps written: sch 20230121/20231120/20250114/20260306; pcb 20221018/20240108/20241229/20260206.
7. KiCad 10 boards reference nets by name only (no netcode table).
8. KiCad 9 renumbered layer IDs and widened `layerselection` to four words.
9. Missing lib_symbols entries silently drop components; raw `extends` symbols have no pins.
10. The 12-orientation pin transform (rot0 (X+lx, Y-ly) ... mirror rules) matches KiCad connectivity.
11. Pins connect only at wire endpoints on the 1.27 mm grid; labels connect anywhere on a segment.
12. eeschema 10.0.0 crashes on HitTest/CreateItems/BeginCommit over IPC (null m_frame, upstream #24966).
13. IPC 9/10 needs a running GUI, a fixed `ipc://<TEMP>/kicad/api.sock`, and `api.enable_server=true` in `<KICAD_CONFIG_HOME>/<major.minor>/kicad_common.json`.
14. Protobufs from 10.0.6 protos work against 9.0.5 and 10.0.0; missing commands fail with `no handler available`.
15. No IPC DRC/ERC, netlist import, library footprint placement or plotting on 9/10; those are master/11 features and even master lacks RunDRC.
16. RunAction executes any TOOL_ACTION by name; names are unstable; interactive tools leave the API AS_BUSY.
17. SWIG pcbnew exists on 7-10 (py3.9 on 7, py3.11 on 8-10), deprecated since 9, removal planned for 11, importable only under each install's python.
18. Standalone WriteDRCReport works on 7/8/9 with the wx assert shim and asserts in Pgm() on 10.
19. ZONE.SetOutline ownership bug yields 0 mm2 fills and random SaveBoard crashes unless the outline is built in place or disowned.
20. A second BOARD in one SWIG process makes SaveBoard crash intermittently on 8/9/10.
21. ExportSpecctraDSN(board, path) works headless on 7-10; ImportSpecctraSES(board, path) on 8-10 only.
22. SES import deletes unlocked copper and applies SES placement.
23. Freerouting 2.4.1 needs Java 25 and its defaults violate KiCad track_width unless neck-down and fanout are disabled.
24. KiCadRoutingTools reads/writes KiCad 9/10 boards directly, routes and seeds placement without KiCad, and writes DRC constraints into the .kicad_pro.
25. `kicad-cli 10 pcb drc --refill-zones --save-board` refills headlessly; stale fills otherwise yield phantom clearance errors.
26. `pcb render` (9+) and `sch export pdf/svg` + PyMuPDF produce legible PNGs headlessly.
27. A fresh KICAD_CONFIG_HOME gets JSON configs but no lib tables; ERC then reports extra lib issues until tables are copied from share/kicad/template.
28. KiCad 10 GUI with a fresh config shows the Setup wizard (API AS_NOT_READY) unless privacy flags and all three lib tables exist.
29. `${KICAD<N>_SYMBOL_DIR}`/`_FOOTPRINT_DIR` resolve for any N on 8+ via the legacy fallback.
30. KiCad 10 ships no .wrl models.
31. IPC plugin actions scoped `schematic` appear in eeschema 10's toolbar; KiCad launches plugins with KICAD_API_SOCKET/KICAD_API_TOKEN.
32. symbols.kicad.org/kicad-stable serves exact PDBs for installed 10.0.0 binaries; cdb symbolicates WER minidumps without a build.
33. Windows installs are discoverable via Uninstall registry keys + Program Files glob; `kicad-cli version --format plain` gives the semver.
34. mcp 2.2 serves 2026-07-28 and legacy clients from one server over stdio and streamable HTTP, with PNG ImageContent.
35. VS Code 1.101+ `registerMcpServerDefinitionProvider` reaches Copilot only; other agents need their own config files.
36. Cursor forwards only the first 40 MCP tools.
