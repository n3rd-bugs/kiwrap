# Ratsnest: MVP-first KiCad agent wrapper

Lens: MVP-FIRST / INCREMENTAL: ship a real 2-layer board end-to-end on installed KiCad 10 in four weeks using only proven, headless, file-based surfaces (kicad-cli, s-expression files, bundled SWIG python as an isolated subprocess, Freerouting), then harden and widen (KiCad 9, VS Code UI, live IPC session, KiCad 11). Boring, testable choices; KiCad is the judge; everything not needed to ship a board is explicitly deferred.

# Ratsnest — MVP-first architecture for an agent-driven KiCad wrapper

Lens: **MVP-first / incremental.** Ship a real 2-layer board end-to-end from an AI agent in four weeks on the KiCad 10 that is already installed, using only surfaces that were verified headless in the research digest. Then harden (KiCad 9), add the VS Code UI, add the optional live session, and finally swap backends for KiCad 11. Every choice below is the boring, testable one; everything that is not needed to ship a board is named in the "Deferred" list.

---

## 0. Decisions in one screen

| Question | Decision |
|---|---|
| First target | KiCad **10.0.x** headless (installed 10.0.0); KiCad 9.0.5 in Phase 2; 11 via backend swap in Phase 5; 7/8 best-effort, last. |
| Schematic | **File generation** by an in-house s-expression writer with per-version dialect tables; validated by `kicad-cli sch erc` + netlist + intent diff. No third-party schematic library. |
| Board creation / mutation | **SWIG pcbnew in an isolated subprocess** under each install's bundled `python.exe` (one BOARD per process, hard timeout, wx asserts nulled), behind a `BoardBackend` seam. |
| Placement | Agent-supplied coordinates + immediate overlap/off-board check + render. KRT `place_seed` as an optional seed in Phase 2. No autoplacer of our own. |
| Routing | **Freerouting 2.4.1** (managed private JRE 25) via SWIG DSN/SES round trip, then zone refill, then kicad-cli DRC. KRT as second engine in Phase 2. |
| ERC / DRC / exports / renders | **kicad-cli** JSON, SVG/PNG; PyMuPDF rasterisation. |
| Agent surface | One **Python MCP stdio server** (33 tools) + CLI; `.mcp.json`/`.vscode/mcp.json`/`.cursor/mcp.json`; `SKILL.md`. |
| VS Code extension | **Phase 3**, thin TypeScript shell: MCP auto-registration, preview, Problems panel, tasks. |
| IPC API (live editing) | **Phase 4**, optional live-session backend, PCB only, 9/10. |
| In-KiCad plugin ↔ VS Code | **Defer to Phase 4, thin launcher only** ("Connect to VS Code" hands socket+token to the MCP server). Rejected as transport/capability. |
| KiCad source build | **Reject for the product; defer as gated developer diagnostic.** Recipe documented in Phase 0; public PDBs + cdb + `api.log` used first; 10.99 nightly instead of building for 11 prototyping. |
| Success verdict | **KiCad is the judge**: every mutating tool returns kicad-cli ERC/DRC deltas; the agent never self-certifies. |

---

## 1. MVP scope

**Ships in Phase 1 (end of week 4):** an agent, given a natural-language spec, produces a 2-layer board on KiCad 10 with 0 ERC errors, an intent-verified netlist, 0 unconnected items, 0 must-fix DRC violations, and a JLCPCB fabrication bundle — with no human edits and no KiCad GUI open.

**Reference board (the MVP acceptance test):** "MCU breakout": ESP32-S3-WROOM-1 module, USB-C 2.0 receptacle (CC 5.1k, ESD array), MCP73831 LiPo charger, 3.3 V LDO, boot/reset buttons, I2C header, four mounting holes, about 50 × 30 mm, 2 layers, JLCPCB rules. Every phase re-runs it.

**Deferred (explicitly):** KiCad 7/8 writers; live IPC session; KiCad-side plugin; VS Code custom UI; LCSC import; KRT placement/routing; length matching/diff pairs beyond netclass width; panelisation; source build; KiCanvas; MCP Apps.

---

## 2. Components

| # | Component | Language | Responsibility | Reaches KiCad via |
|---|---|---|---|---|
| 1 | `kicad_agent` core library | Python 3.11 (stdlib, sexpdata, pymupdf, Pillow) | Install discovery + capability record; wrapper-owned `KICAD_CONFIG_HOME`; lossless s-expr parser/writer; schematic dialect writers (10, 9); library index; kicad-cli wrapper + normalisers; SWIG subprocess driver; Freerouting driver; ERC/DRC/netlist verification; git snapshots; renders. | kicad-cli subprocess; `<install>/bin/python.exe` subprocess; files; (Phase 4) kipy IPC. |
| 2 | `kicad-agent` CLI | Python | Same functions for humans/CI/tests, `--json`. | core |
| 3 | MCP server | Python, `mcp>=2.2` `MCPServer` | 33 tools over stdio (HTTP optional); images as `ImageContent`; `structuredContent`; per-project mutation lock. | core |
| 4 | SWIG driver scripts `pcbnew_jobs/*.py` | Python run by KiCad's bundled interpreter | The only code that imports `pcbnew`: build board from netlist, edit, zones, DSN export, SES import, geometry queries. JSON in/out; load-modify-save per job. | SWIG pcbnew (9/10; removed in 11 → IPC backend in Phase 5) |
| 5 | Freerouting runner | Java 25 external process, driven by Python | Autoroute DSN→SES; settings from board rules; result manifest. GPL kept at arm's length. | Only via DSN/SES files |
| 6 | VS Code extension (Phase 3) | TypeScript, engines `^1.101.0` | MCP auto-registration for Copilot; "Configure agents"; SVG preview custom editor; ERC/DRC → Problems; tasks; install picker; "Open in KiCad". No EDA logic. | MCP/CLI only |
| 7 | KiCad launcher plugin (Phase 4) | plugin.json `exec` runtime, PCM `runtime: ipc` | One toolbar action in pcbnew and eeschema: write `KICAD_API_SOCKET`/`KICAD_API_TOKEN` to `~/.kicad-agent/live/<pid>.json`, optionally `code <project>`, exit. | Launched by KiCad; hands token to MCP server |
| 8 | Live-session backend (Phase 4) | Python, `kicad-python` 0.8.0 pinned | Attach/spawn, apply file results as one undoable commit, refill, save; retries; safety rules. | IPC API (protobuf/nng named pipe) |
| 9 | Skills | Markdown (`SKILL.md`, `AGENTS.md`) | Workflow knowledge for Claude Code, Copilot, Cursor, Codex. | n/a |

---

## 3. Which KiCad surface backs which job, and why

| Job | Surface | Why (verified facts) |
|---|---|---|
| Schematic create/edit/annotate | **File generation** (own writer) | No schematic API in 7–10: KiCad 10's schematic IPC handler serves only `GetOpenDocuments`; `CreateItems`/`BeginCommit` crash eeschema 10.0.0. Stdlib generation gave 0 ERC and the correct netlist on 8/9/10. kiutils/kicad-sch-api/skidl/circuit-synth all failed fidelity or connectivity tests. |
| ERC, netlist, BOM | **kicad-cli** | Only headless ERC; `erc.v1` JSON stable 8→10; positions ×100 bug corrected by wrapper. |
| Board from netlist | **SWIG subprocess** | kicad-cli has no "update PCB from schematic"; IPC 9/10 has no `ImportNetlist` and library placement yields an empty footprint. SWIG `FootprintLoad` + `NETINFO_ITEM` + `pad.SetNet` verified (58/63 footprints placed on pic_programmer; parity DRC validates). |
| Placement, tracks, vias, zones, fill | **SWIG subprocess** | Verified headless on 8/9/10; byte-stable `SaveBoard` keeps git diffs minimal. |
| Routing | **Freerouting via SWIG DSN/SES** | Verified 0/0 on KiCad 10 with neckdown+fanout off; PNS router unreachable (RunAction only starts the interactive tool and then the API returns AS_BUSY); no Specctra in kicad-cli/IPC. |
| DRC | **kicad-cli** | `WriteDRCReport` asserts on 10, hangs on 9; IPC has no DRC command in any version incl. master. `--refill-zones --save-board` (10) removes phantom clearance errors after SES import. |
| Renders | **kicad-cli** | `pcb render` (9/10) ~2.5 s; SVG → PyMuPDF PNG < 0.3 s; schematic PDF/SVG → PNG legible. |
| Fabrication | **kicad-cli** (`jobset run` on 9/10 for profiles) | Full JLCPCB bundle in ~3 s; stable exit codes. |
| Live editing of an open board | **IPC (kipy)** — Phase 4 only | Works on 9/10 for PCB items, commits, refill, save; needs GUI, single pipe, AS_BUSY semantics. Not needed to ship a board. |
| Format normalisation | `kicad-cli sch|pcb|sym|fp upgrade --force` on copies (10) | Used as a drift canary in CI, never in place. |

Not used, on purpose: `RunAction` for interactive tools; SWIG action plugins; `ParseAndCreateItemsFromString` (stub in 10.0); IPC library placement; `kicad-cli sch upgrade` in place (it segfaulted and truncated a malformed file).

---

## 4. Verdict: in-KiCad plugin ↔ VS Code extension channel

**Decision: defer to Phase 4, adopt only as a thin launcher; reject as transport, agent surface, or capability source.**

What the channel buys over `kicad-cli` plus an external IPC client (facts from the digest):

1. KiCad launches an IPC plugin with the exact `KICAD_API_SOCKET` and `KICAD_API_TOKEN`. On Windows the API socket is a named pipe (`\\.\pipe\C:\Users\...\Temp\kicad\api.sock`) that cannot be probed as a file, all instances share one name, and a second instance silently loses. The hand-off solves discovery and identity.
2. A toolbar button in **both** pcbnew and eeschema on 9/10 (scope `schematic` works despite the docs) and PCM-managed install.
3. Nothing else. An IPC plugin is an external process with exactly the API surface the extension could open itself: no PNS router, no DRC/ERC, no exports, no netlist import, no library placement, no schematic access on 9/10.

Why not more:

- **SWIG action-plugin server inside pcbnew**: reaches `WriteDRCReport`/`ExportSpecctraDSN` in-process, but pcbnew-only, mutations outside `Run()` bypass undo/connectivity/refresh, thread issues are documented, and SWIG is removed in KiCad 11. Konnect labels its own such bridge "not the KiCad 11 path".
- **C++ handler plugin**: does not exist; handlers are registered by frames in C++. Code inside KiCad means a patched KiCad, which defeats "upgrade KiCad underneath".
- **Terminating the channel in the VS Code extension**: would strand Claude Code CLI, Codex and Copilot CLI. The long-lived side must be the MCP server.

Implementation (Phase 4, 2–3 days): `kicad-plugin/plugin.json` (`runtime.type: exec`, one action, scopes `["pcb","schematic"]`, show-button), entrypoint writes `{socket, token, pid, kicad_version, cwd}` to `~/.kicad-agent/live/<pid>.json` (never to stdout — KiCad drains the pipe only at exit), optionally runs `code <project>`, exits. PCM `metadata.json` with `runtime: "ipc"`, `kicad_version: "9.0"`. The MCP server's `live_session attach` reads the newest token file, Pings, validates the token, and checks `GetOpenDocuments`.

## 5. Verdict: KiCad source build

**Decision: reject for the product; defer as a gated developer diagnostic (Phase 4 at the earliest).**

- Every stage of the flow was reproduced on installed 7–10 binaries. No shipped component runs code inside KiCad.
- The two KiCad-side crashes met so far were diagnosed without a build: the eeschema `BeginCommit` crash was symbolicated with `cdb` and `symbols.kicad.org/kicad-stable` PDBs to `API_HANDLER_EDITOR::checkForBusy` (null `m_frame`, upstream #24966) in about 30 minutes; `EnableAPILogging=1` in `kicad_advanced` gives full request/response transcripts in `logs/api.log`; WER minidumps land in `%LOCALAPPDATA%\CrashDumps`.
- Cost on this machine: roughly one working day for the first debug build (update VS 2022 17.9 → 17.14 so MSVC 14.44 matches CI and the kicad-gitlab NuGet vcpkg cache hits; vcpkg at a drive root at build.ps1's pinned commit; swigwin 4.3.1; 10 min–1 h dependencies with cache hits, else 2–5 h; 40–80 GB). Afterwards it is cheap to keep.
- Value: zero for the MVP, because a patched KiCad cannot be swapped for stock upgrades unless upstreamed.

Genuine triggers (each outside the MVP): contributing handlers upstream (RunDRC / marker retrieval do not exist even on master), driving the PNS router as a batch engine (PCBWorld-Engine style GPL fork), or stepping through an IPC handler with unoptimised locals. For KiCad 11 prototyping, install the 10.99 nightly instead. Phase 0 writes `docs/dev/kicad-source-build.md` so the build can start the day a trigger fires.

---

## 6. Version isolation design (7 / 8 / 9 / 10 / future)

1. **`KiCadInstallation`** = `{root, kicad-cli, python.exe + site-packages + DLL dir, share/kicad/{symbols,footprints,3dmodels,template,schemas}, semver (kicad-cli version --format plain), commit, python ABI tag}`. Discovery: registry Uninstall keys (`DisplayName 'KiCad X.Y'`; 7.0 lives under WOW6432Node) ∪ glob `C:/Program Files/KiCad/*/bin/kicad-cli.exe` ∪ PATH ∪ explicit setting.
2. **Capability record keyed on full semver**, seeded from the verified matrix and re-probed from `kicad-cli <cmd> --help` (ignore exit status; 7/8 exit 1): `has_erc_cli (>=8)`, `has_drc_cli (>=8)`, `has_render (>=9)`, `has_jobset (>=9)`, `has_upgrade (>=10)`, `drc_refill_flags (>=10)`, `has_ipc (>=9)`, `ipc_headless (>=11)`, `ipc_schematic (>=11)`, `swig_available (<=10)`, `gerbers_creates_dir (>=9)`, `writes_prl (>=10)`, `ships_wrl (<=9)`, `sch_epoch`, `pcb_epoch`, `sym_epoch`. Tools branch on flags, never on version numbers, and fail with a typed `unsupported_on_this_version`.
3. **Format epochs**: schematic serializers for `20260306` (10) and `20250114` (9) in MVP (`20231120`, `20230121` later); boards are always written by the target install's own SWIG so the epoch is inherently right; `.kicad_pro` meta 3. A project records its target install and is written in that install's dialect with lib symbols from that install's libraries. Rationale: newer KiCad reads older files; older KiCad hard-fails on newer files and even on a single unknown token.
4. **Tolerant reader**: one s-expr parser normalising bare `hide` / `(hide yes)`, `tstamp`/`uuid`, `fp_text`/`property`, numeric or named nets, both layer-ID tables into a small semantic model (nets by name, layers by canonical name, units in mm).
5. **Process environment**: every KiCad process (kicad-cli, python.exe, GUI) gets `KICAD_CONFIG_HOME=<state>/kicad-config` (KiCad appends `<major.minor>/`), pre-provisioned per version with `kicad_common.json` (`api.enable_server`, `do_not_show_again.*`, `system.first_run_shown`) plus copies of `share/kicad/template/{sym,fp}-lib-table` and an empty `design-block-lib-table`; `KICAD<N>_{SYMBOL,FOOTPRINT,3DMODEL}_DIR`; `KICAD_DOCUMENTS_HOME` (also sidesteps Controlled Folder Access). `%APPDATA%/kicad` is never touched and CI asserts it.
6. **SWIG as per-install subprocess only**: `<install>/bin/python.exe pcbnew_jobs/<job>.py --in job.json --out result.json`; `_shim.py` maps `IO_MGR`/`PCB_IO_MGR`, `EDA_UNITS_*`, VIATYPE ints, resolves layer names → ids at runtime via `pcbnew.<symbol>`, nulls `wxTheAssertHandler`, guards one BOARD per process, always `SetFileName`, `SaveBoard(aSkipSettings=True)`, `BuildConnectivity()` before `ZONE_FILLER.Fill`, outlines built via `Outline()`.
7. **kicad-cli flag tables** per subcommand generated from `--help` (kicadcliwrapper technique) → CLI drift is data (`gerbers -o` mkdir on 7/8, `--variant` on 10, `gerber` singular removed in 10, etc.).
8. **IPC**: kipy pinned to the newest release covering all installed majors (0.8.0 covers 9 and 10); RPCs probed at runtime (`no handler available` → capability false); per-command "Since" table from proto comments.
9. **Backend seams** that make 11 a swap: `SessionStart` (GUI now / `kicad-cli api-server --socket` later), `BoardBuilder` (SWIG now / IPC `ImportNetlist` + `PlaceFootprintFromLibrary` later), `Exporter` (kicad-cli now / `RunBoardJobExport*` later), `SchematicWriter` (file now, IPC optional later; file stays default), `Specctra` (SWIG now / own DSN writer + SES applier or Freerouting JSON later).
10. **CI matrix**: for every installed version: generate → ERC → build → place → route → DRC → export; negative tests (unknown token, future stamp); `sch upgrade` semantic-diff canary on 10.

---

## 7. Schematic strategy

- **Model**: sheets; symbol instances (`lib_id`, `at`, `rot`, `mirror`, `unit`, properties, instances path); wires as segments; junctions; no-connects; local/global/hierarchical labels; power symbols; sheet symbols with pins.
- **Writer rules (all verified)**: embed every used symbol verbatim from the target install's `.kicad_sym`, rename only the top-level name to `Lib:Name`, keep sub-unit names unprefixed, **flatten `extends`**; instances block path `/<ROOT_UUID>[/<SHEET_UUID>]` with reference/unit (instances win over the Reference property); `sheet_instances` only in root; pin endpoints from the verified transform (rot0 `(X+lx, Y−ly)`, rot90 `(X−ly, Y−lx)`, rot180 `(X−lx, Y+ly)`, rot270 `(X+ly, Y+lx)`; mirror x flips Y offset, mirror y flips X offset); wires split at every pin/PWR_FLAG endpoint and snapped to 1.27 mm; PWR_FLAG on every power-input net; Datasheet `""` on v10; dialect-10 tokens (`hide`/`show_name`/`do_not_autoplace` at property level, `body_style`, `in_pos_files`, `duplicate_pin_numbers_are_jumpers`, `(power global|local)`) and dialect-9 tokens as tables.
- **Connection style**: netlist-first. `sch_connect` places labels exactly on pin endpoints (labels connect anywhere along a segment; pins only at endpoints), short stubs, one hierarchical sheet per functional block, annotation per sheet (100s/200s).
- **Validation gate after every mutation**: `kicad-cli sch erc --format json --severity-all --exit-code-violations` (positions ×100), `sch export netlist` parsed with a real parser, and `sch_verify_intent` diffing nets against `intent.json` — because ERC cannot see a label on the wrong pin (the exact bug that broke kicad-sch-api).
- **Render**: `sch export svg --pages` → PyMuPDF PNG ≤1024 px, ERC markers overlaid.
- **Editing existing user files**: same lossless tree; round-trip test asserts token-multiset, ERC-set and netlist equality on the 7–10 demo corpus.
- Deferred: dialects 7/8; IPC schematic backend (11); editor-lock detection via `~*.lck` (Phase 2).

## 8. Board creation, placement, routing

**Setup** (`pcb_setup`): outline, stackup, rules (JLCPCB 2-layer preset: 0.127 mm track/space, via 0.3/0.6, edge clearance 0.3, 1.6 mm FR4), netclasses, `.kicad_dru`, via the SWIG driver of the target install.

**Netlist → PCB** (`pcb_sync_from_schematic`): `kicad-cli sch export netlist --format kicadsexpr` → parser → SWIG job: `NETINFO_ITEM` per net; `FootprintLoad` with nickname resolution through project `fp-lib-table` then the global template table (following nested `Table` entries on 10); `SetFPID(LIB_ID)`; `SetReference/SetValue/SetPath`; `pad.SetNet`; existing refs keep position; stale nets removed; `SaveBoard(skipSettings=True)`. Verified afterwards by `kicad-cli pcb drc --schematic-parity`.

**Placement (MVP)**: agent supplies `{ref, x, y, rot, side}` guided by the placement skill; `pcb_place` moves via SWIG and returns courtyard overlaps + off-board parts (kicad-cli DRC courtyards + outline containment) and a 2D render with ratsnest overlay. Phase 2: `pcb_place_suggest` = KRT `place_seed` from floorplan-intent JSON as a seed only. No home-grown autoplacer; KiCad's legacy autoplacer over `RunAction` not used.

**Routing (MVP)**: `pcb_autoroute` = lock tracks to keep → SWIG `ExportSpecctraDSN` → Freerouting 2.4.1 under managed JRE 25 (`--gui.enabled=false -de -do -mp 20 -mt 1 -da --router.automatic_neckdown=false --router.fanout.enabled=false --router.copper_to_edge_clearance_um=<rules> -inc <plane classes> --router.result_json --user_data_path/--logging.file.location under state dir`, scratch cwd) → SWIG `ImportSpecctraSES` (replaces unlocked tracks, applies placement) → refill (`kicad-cli pcb drc --refill-zones --save-board` on 10; SWIG `ZONE_FILLER` on 9) → DRC JSON. Returns routed/unrouted, vias, DRC summary, render. `pcb_route` for manual touch-ups; `pcb_zones` for GND pours. Phase 2: KRT as no-Java second engine; Freerouting REST API for progress/cancel. Out: PNS router, RunAction Autoroute, cloud routers.

## 9. Verification loop (non-negotiable agent protocol)

After every mutating step: (1) `project_snapshot`; (2) stage validator — `sch_erc` → `sch_verify_intent` → `pcb_drc(schematic_parity)`; (3) render the affected region and look; (4) fix from violation JSON (each item has uuid + pos); (5) re-validate. Never route with open ERC errors; never export with must-fix DRC types (`clearance`, `shorting_items`, `tracks_crossing`, `unconnected_items`, `hole_clearance`, `net_conflict`). `courtyards_overlap`, `silk_*`, `starved_thermal` are review-class.

---

## 10. Agent surface

- **One MCP stdio server** (`mcp>=2.2`, `MCPServer`), 33 tools, `ToolAnnotations`, `outputSchema`/`structuredContent` for reports, `ImageContent` PNG ≤1024 px for renders (fits Claude Code's 25k-token image rule), `resource_link` for SVGs/Gerber zips, `isError` with actionable text, `_meta['anthropic/maxResultSizeChars']` on report tools, deterministic `tools/list`, server `instructions` describing the flow and preconditions.
- **2026-07-28 rules**: no Roots/Sampling/Logging; explicit project paths on every call; live sessions are explicit handles; logging to stderr/file.
- **Registration**: `.mcp.json` (Claude Code CLI + extension, VS Code Agent Host), `.vscode/mcp.json` (Copilot Local harness, with `dev.debug: python`), `.cursor/mcp.json`, `~/.codex/config.toml` snippet (raise `tool_timeout_sec`; long routes via `async=true` + polling), Windsurf/Copilot CLI snippets. Phase 3 adds programmatic registration in VS Code.
- **Tool budget**: 33 < Cursor's 40 (tightest); enums (`view`, `targets`, `action`) instead of one tool per subcommand; `kicad_help(tool)` for deep docs.
- **Skills**: `.claude/skills/kicad-flow/SKILL.md` (spec-only frontmatter; read by Claude Code, Copilot, Cursor, Codex) with references for geometry, netlist-first schematic, ERC/DRC triage, placement, routing, JLCPCB presets, context budget, version awareness; `AGENTS.md`/`CLAUDE.md` always-on.

## 11. Tool catalog (33)

| Tool | Stage | Args → Returns | Backend |
|---|---|---|---|
| `kicad_env` | project | `refresh?` → installs, capabilities, freerouting/java, live | discovery + `--help` probe |
| `kicad_help` | project | `tool` → markdown | static |
| `project_create` | project | `dir, name, install_id, fab_profile` → paths, epochs, git_head | FILE + SWIG + git |
| `project_status` | project | `project` → files, dirty, last_erc/drc, unrouted, stage | FILE |
| `project_snapshot` | project | `project, message` → sha | git |
| `project_restore` | project | `project, sha` → restored_files | git |
| `design_intent_set` | requirements | `project, intent{blocks, rails, interfaces, constraints, nets}` → validation_errors | FILE |
| `lib_search_symbols` | parts | `query, lib?, min/max_pins?` → [lib_id, description, fp_filters, pin_count] | SQLite index |
| `lib_symbol_info` | parts | `lib_id, unit?, render?` → pins (symbol space), fp_filters, png | FILE + `sym export svg` |
| `lib_search_footprints` | parts | `query, pads?, smd/tht?` → [fpid, description, pads, bbox] | index |
| `lib_footprint_info` | parts | `fpid, render?` → pads, courtyard, bbox, model, png | SWIG + `fp export svg` |
| `sch_add_symbol` | schematic | `project, sheet, lib_id, ref?, value, footprint?, at, rotation, mirror, unit, fields, dnp` → uuid, ref, absolute pin endpoints | writer |
| `sch_add_power` | schematic | `project, sheet, kind(power/pwr_flag/no_connect), symbol?, attach_to` → uuid, net | writer |
| `sch_connect` | schematic | `project, sheet, net, endpoints, style(label/wire/global/hier)` → items, junctions | writer |
| `sch_add_sheet` | schematic | `project, parent, name, at, size, pins` → sheet_uuid, path | writer |
| `sch_annotate` | schematic | `project, scheme, footprint_map?, rules?` → mapping, unresolved | writer |
| `sch_get` | schematic | `project, sheet?, refs?, bbox?, include` → compact JSON | parser |
| `sch_render` | schematic | `project, sheet, region?, max_px, overlay` → PNG | `sch export svg` + PyMuPDF |
| `sch_erc` | schematic | `project, severity` → ok, counts, violations (pos ×100 fixed) | `kicad-cli sch erc` |
| `sch_netlist` | schematic | `project` → components, nets/nodes | `sch export netlist` |
| `sch_verify_intent` | schematic | `project` → missing/extra/wrong_pin/unassigned | netlist vs intent |
| `pcb_setup` | board | `project, outline, layers, rules, netclasses, dru_text` → applied | SWIG + FILE |
| `pcb_sync_from_schematic` | board | `project, remove_orphans` → added/updated/removed, parity_issues | netlist + SWIG + DRC parity |
| `pcb_get` | board | `project, refs?, bbox?, include` → compact JSON | SWIG read job |
| `pcb_place` | placement | `project, placements[], relative_to?, render` → overlaps, off_board, png | SWIG + DRC + render |
| `pcb_place_suggest` | placement | `project, intent` → proposals, png | KRT (Phase 2) |
| `pcb_render` | board | `project, view, layers?, region?, max_px, overlay` → PNG | `pcb export svg`/`pcb render` |
| `pcb_zones` | routing | `project, zones[], fill` → uuids, area | SWIG / `drc --refill-zones` |
| `pcb_autoroute` | routing | `project, engine, passes, timeout_s, ignore_netclasses, lock_refs, async` → routed, unrouted, vias, drc_summary, png | SWIG DSN/SES + Freerouting + DRC |
| `pcb_route` | routing | `project, ops[track/via/unroute]` → uuids, drc_delta | SWIG + DRC |
| `pcb_drc` | drc | `project, schematic_parity, refill_zones, severity` → ok, must_fix_count, classified violations, unconnected, parity | `kicad-cli pcb drc` |
| `fab_export` | fab | `project, targets[], profile, out_dir` → files (sha256), warnings | kicad-cli / jobset |
| `review_report` | review | `project` → markdown + images | composite |
| `live_session` | live (P4) | `action(attach/spawn/status/stop), project?` → session_id, version, open_docs, busy | IPC |
| `live_apply` | live (P4) | `session_id, mode(reload/delta), delta?` → commit_ok, items_changed | IPC commit |

(`parts_lcsc_import` is added in Phase 2 within the parts group; `kicad_help` keeps option depth out of `tools/list`.)

## 12. VS Code surface (Phase 3)

Thin TypeScript extension (Node LTS must be installed; none on PATH today):
- `contributes.mcpServerDefinitionProviders` + `vscode.lm.registerMcpServerDefinitionProvider` → `McpStdioServerDefinition(label, <venv>/python, ['-m','kicad_agent.mcp_server'], env)`; venv bootstrapped with `uv` on first `resolveMcpServerDefinition`; `version` bumped to refresh tools.
- "Configure agents" command writes `.mcp.json`, `.cursor/mcp.json`, prints Codex/Windsurf snippets.
- Read-only custom editor (`priority: option`) for `*.kicad_sch`/`*.kicad_pcb`: kicad-cli SVG in a webview, refreshed on save; "Open in KiCad" escape hatch. KiCanvas not used (alpha, 9/10 gaps).
- `DiagnosticCollection` mapping ERC/DRC violations (corrected positions) to file URIs; task provider for exports/jobsets; tree view of jobs; status-bar KiCad install picker writing the project's target install.
- Optional `languageModelTools` facade (`kicadRender`) with confirmation UI for Copilot users; TextMate grammar contributions for `.kicad_*`/`.kicad_dru`.
- Phase 4: status of live sessions and a "Launch sandboxed KiCad" command.

## 13. Repo layout

```
ratsnest/
  README.md  LICENSE(MIT)  AGENTS.md  CLAUDE.md  .mcp.json  .vscode/mcp.json  .cursor/mcp.json
  .claude/skills/kicad-flow/SKILL.md (+references/)   .github/skills -> .claude/skills
  pyproject.toml (uv; extras: mcp, dev)
  kicad_agent/
    installs/    discovery.py  capabilities.py  config_home.py  seed_matrix.json
    sexpr/       parser.py  writer.py  diff.py
    schematic/   model.py  writer.py  dialects/{v9,v10}.py  libsym.py  transform.py  connect.py  annotate.py
    library/     index.py  symbols.py  footprints.py  fp_lib_table.py
    cli/         kicad_cli.py  flags/<semver>.json  erc.py  drc.py  netlist.py  exports.py  render.py
    board/       backend.py  swig_backend.py  ipc_backend.py(P4)  builder.py  classify.py
    pcbnew_jobs/ _shim.py  build_board.py  edit_board.py  zones.py  dsn_export.py  ses_import.py  query.py
    routing/     freerouting.py  krt.py(P2)
    render/      raster.py  overlay.py
    project/     create.py  status.py  git.py  intent.py  verify.py
    tools/       project.py  parts.py  schematic.py  board.py  routing.py  fab.py  live.py
    mcp_server.py  cli_main.py
  vscode-extension/ (P3)   kicad-plugin/ (P4)
  tests/  corpus/  unit/  integration/  e2e/
  docs/   architecture.md  capability-matrix.md  dev/kicad-source-build.md  dev/debugging-kicad.md
  scripts/ bootstrap.ps1  ci.ps1
```

## 14. Roadmap

| Phase | Weeks | Deliverables | Exit criteria |
|---|---|---|---|
| 0 Foundation | 1 | Install discovery + capability record; config-home provisioning; kicad-cli runner + parsers (ERC ×100); lossless sexpr with corpus round-trip tests; bootstrap (uv, JRE 25, Freerouting jar); source-build and debugging recipes written | `kicad-agent env` lists 7.0.6/8.0.1/9.0.5/10.0.0 with correct flags; parsers green on 8/9/10; round trip 0 token diffs; `%APPDATA%/kicad` untouched |
| 1 MVP on KiCad 10 | 2–4 | Dialect-10 writer, library index, SWIG driver jobs, Freerouting pipeline, renders, fab profile, MCP server (31 tools), CLI, configs, SKILL.md, reference-board spec | Claude Code builds the reference board from the spec: 0 ERC, intent ok, 0 unconnected, 0 must-fix DRC, fab bundle — 3 consecutive runs, no human edits |
| 2 Harden + KiCad 9 | 5–6 | Dialect 9, 9 zone-fill path, idempotency/locks/snapshots/editor-lock, KRT router + place seed, LCSC import, CI matrix, negative tests, upgrade canary, Codex/Cursor/Copilot smoke tests | Reference board passes on 9.0.5 and 10.0.x; tool contract identical; CI green |
| 3 VS Code | 7–9 | Extension: MCP registration, Configure agents, SVG preview, Problems, tasks, install picker, VSIX | Fresh VS Code + VSIX: Copilot sees tools zero-config; Claude Code via `.mcp.json`; Problems panel positions correct |
| 4 Live + plugin + 11 probe | 10–12 | Live-session backend (kipy), launcher plugin (PCM), 10.99 nightly probe report; source build only if a blocker demands it | Agent applies results to an open board as one undo step; plugin hands token on 9/10; swap list for 11 documented |
| 5 KiCad 11 GA + legacy | post-GA | IPC BoardBuilder/Exporter/headless; own DSN/SES; 7/8 best-effort | Reference board on 11.0 with no SWIG process; catalog unchanged |

## 15. Risks and mitigations

| Risk | Mitigation |
|---|---|
| SWIG removed in 11; wrapper depends on it for 9/10 | `BoardBackend` seam, subprocess only, conformance tests; Phase 5 IPC backend + own DSN/SES |
| SWIG headless crashes (SetOutline, second BOARD, wx asserts, WriteDRCReport) | `_shim.py` rules, faulthandler, hard timeout/kill, load-modify-save per job, never WriteDRCReport |
| Writer emits tokens KiCad rejects or silently drops parts | Epoch tables, verbatim symbols + flattening, ERC+netlist+intent gate, upgrade canary, negative tests |
| Future point-release drift | Full-semver capability record, `--help` probing, real parsers, corpus CI on new installs |
| Freerouting: Java 25, GPL, rule-violating defaults, copper text, non-determinism, stale fills | Managed JRE, separate process, rule-derived flags (`-mt 1`, neckdown/fanout off, edge clearance), refill then DRC, lock kept tracks, KRT fallback |
| Poor agent placement | Immediate overlap/off-board + render feedback, placement skill, KRT seed, courtyard DRC gate |
| Context blow-up / tool caps | 33 tools, chunked reads, PNG ≤1024 px, structured summaries, resource links |
| Live IPC fragility (GUI, single pipe, AS_BUSY, #24966) | Optional, off the shipping path; token hand-off; Ping+token; retries; no writes with eeschema open on 10.x |
| kicad-python 0.8.0 wheel inconsistencies | Only `KiCad/board/project`; pinned; vendored `_pb2` if needed; runtime RPC probing |
| Windows traps (CFA, setup wizard, `.kicad_prl`, user settings) | `KICAD_DOCUMENTS_HOME`/`KICAD_CONFIG_HOME` always set, wizard preconditions provisioned, `.gitignore`, CI assertion |
| Fast-moving/stale third-party libs | Own sexpr layer; KRT pinned in its own venv behind the interface; DRC after it |
| Early source-build detour | Recipe in Phase 0, execution gated on a named blocker, PDBs/cdb/api.log first, nightly for 11 |

## 16. Key technical claims (each testable)

1. kicad-cli 10.0.0 `pcb drc --format json --severity-all` emits drc.v1 JSON with correct millimetre coordinates and uuids per violation item.
2. kicad-cli 8.0.1/9.0.5/10.0.0 `sch erc --format json` positions are exactly 100× too small; ×100 matches the text report.
3. A `.kicad_sch` at version 20260306 with lib_symbols copied verbatim from KiCad 10's libraries, instances blocks, wires split at pin endpoints on the 1.27 mm grid and PWR_FLAGs passes kicad-cli 10 ERC with zero violations and exports the intended netlist.
4. Symbol pin transform: rot0 `(X+lx, Y−ly)`, rot90 `(X−ly, Y−lx)`, rot180 `(X−lx, Y+ly)`, rot270 `(X+ly, Y+lx)`, mirror x flips the Y offset, mirror y flips the X offset — for all 12 orientations.
5. A derived symbol copied raw with `(extends)` yields zero pins in the netlist; flattening the parent body under the derived name restores them.
6. KiCad 10's bundled python.exe can headlessly create a BOARD, FootprintLoad from share/kicad/footprints, create nets, assign pad nets, add tracks/vias/zones, fill with ZONE_FILLER after BuildConnectivity, and SaveBoard a file that kicad-cli DRC reports 0/0.
7. A second `pcbnew.BOARD` in one process makes SaveBoard crash intermittently on 8/9/10; one BOARD per process is stable.
8. `ZONE.SetOutline` leaves ownership with the Python proxy (zero-area fills, intermittent AVs); `z.Outline()` built in place avoids it.
9. `pcbnew.WriteDRCReport` asserts on null `Pgm()` in KiCad 10 standalone python.
10. `ExportSpecctraDSN(board, path)` / `ImportSpecctraSES(board, path)` work headlessly on 8.0.1/9.0.5/10.0.0; SES import replaces all non-locked tracks/vias.
11. Freerouting 2.4.1 needs Java 25 and, with `automatic_neckdown=false` and `fanout.enabled=false`, routes a 30-net 2-layer board to 0 KiCad DRC violations / 0 unconnected.
12. kicad-cli 10 `pcb drc --refill-zones --save-board` eliminates phantom clearance violations caused by stale fills after SES import.
13. Neither kicad-cli 7–10 nor the 9/10 IPC API can import a netlist, load a library footprint by LIB_ID, export Specctra DSN, or run DRC/ERC.
14. KiCad 10.0.0's schematic IPC handler answers only GetOpenDocuments; HitTest/CreateItems/BeginCommit crash eeschema.exe.
15. The 9/10 IPC API needs a running GUI, one named pipe `%TEMP%\kicad\api.sock`, `api.enable_server=true` in `<KICAD_CONFIG_HOME>/<major.minor>/kicad_common.json`, and returns AS_BUSY during interactive tools.
16. `KICAD_CONFIG_HOME` redirects every KiCad 7–10 process to `<dir>/<major.minor>/`; a sandboxed KiCad 10 GUI starts without the setup wizard when kicad_common.json prompt flags and the three global lib tables exist.
17. Files written in version N's format load on every newer kicad-cli and fail on every older one; one unknown token breaks 7.0/8.0 parsers.
18. kicad-cli 10 `pcb render` takes ~2–3 s; `pcb export svg`/`sch export svg` rasterise via PyMuPDF in <0.3 s into legible PNGs.
19. kicad-cli 10 netlist export prints one token per line and adds `(groups)`/`(variants)`, so a real parser is required.
20. A Python `mcp` 2.2 server negotiates 2026-07-28 and also serves 2025-06-18 clients, and PNG ImageContent is consumed by Claude Code and VS Code Copilot.
21. Cursor forwards only the first 40 MCP tools; 33 are fully visible.
22. kicad-python 0.8.0 connects to and edits boards in 10.0.0 and 9.0.5, while `kipy.schematic`/`kipy.board_jobs` fail to import.
23. symbols.kicad.org/kicad-stable serves PDBs matching installed 10.0.0 binaries, so cdb can symbolicate KiCad minidumps without a source build.
24. Specctra, ZONE_FILLER and FootprintLoad exist only in SWIG on 9/10 and SWIG is removed in 11, so they need IPC or wrapper-owned replacements on 11.
25. `exec` IPC plugins with scope `schematic` appear as toolbar buttons in the KiCad 10 Schematic Editor and are launched with `KICAD_API_SOCKET`/`KICAD_API_TOKEN`.

## 17. Deferred list (with the phase that picks it up)

KiCad 9 (P2) · KRT routing/placement seed (P2) · LCSC import (P2) · editor-lock refusal (P2) · Freerouting REST/progress (P2) · VS Code extension (P3) · languageModelTools facade (P3) · live IPC session (P4) · launcher plugin + PCM (P4) · 10.99 nightly probe (P4) · source build (gated, P4+) · KiCad 11 IPC backends and own DSN/SES (P5) · KiCad 7/8 dialects and SWIG-DRC-on-7 (P5) · KiCanvas/MCP Apps (not planned) · PNS router batch driving (rejected).
