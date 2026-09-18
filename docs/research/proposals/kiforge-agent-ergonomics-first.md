# KiForge — agent-first KiCad workbench (MCP server + VS Code extension + thin KiCad connect plugin)

Lens: AGENT-ERGONOMICS FIRST: design the tool catalog and the verify-render-fix feedback loop for an LLM agent first (compact catalog under Cursor's 40-tool cap, idempotent upserts, structured receipts plus small images, errors that teach, chunked reads, stage gates decided by KiCad's own ERC/DRC, skills and evals), then fit KiCad 7/8/9/10/11 surfaces behind a capability-keyed backend.

# KiForge — Architecture Proposal (Agent-Ergonomics-First Lens)

**Date:** 2026-09-18 · **Status:** decision document · **Scope:** VS Code plugin/interface that lets an AI agent do end-to-end electronics development on KiCad 7/8/9/10 (and 11 when it ships) as a wrapper, so KiCad can be upgraded underneath without rewriting the tools.

---

## 0. Decisions in one screen

| Question | Decision |
|---|---|
| Agent contract | One Python MCP server (**forge-core**), 36 stage-named tools `kf_*`, identical for Claude Code, Copilot agent mode, Cursor, Windsurf, Codex. |
| Schematic creation | File-level: in-house lossless s-expression CST + semantic IR, one serializer per KiCad format epoch. IPC schematic API only on KiCad 11. |
| Board creation / sync / placement / zones | **forge-swig-driver** run under each install's bundled `python.exe` (7–10); IPC `ImportNetlist`/`CreateItems` on 11; IPC live session (9/10) when the user has the board open. |
| Routing | Freerouting 2.4.1 (separate process, managed JRE 25, REST API) via DSN/SES; KiCadRoutingTools (KRT) as no-Java router and as the intent-driven placement seeder. KiCad's PNS router is not driven programmatically. |
| ERC / DRC | **kicad-cli only** (8+). KiCad is the judge; the agent never is. SWIG `WriteDRCReport` only on 7. |
| Exports | kicad-cli (7–10), `RunBoardJobExport*` over IPC on 11. |
| Visual feedback | kicad-cli `pcb render` (9+), `pcb/sch export svg|pdf` + PyMuPDF raster (7+), ≤1024 px, ERC/DRC/ratsnest overlays. |
| In-KiCad plugin ↔ VS Code | **Adopt, thin only**: "KiForge Connect", an IPC `exec` plugin (PCM-packaged) that hands `KICAD_API_SOCKET`/`KICAD_API_TOKEN` to forge-core. Reject SWIG-action-plugin servers and C++ patches. |
| KiCad source build | **Defer to Phase 4** as a developer-only upstream track. Debug the installed release with `symbols.kicad.org` PDBs and `EnableAPILogging=1` first. Recipe written in Phase 0, build not started. |
| Version isolation | `KiCadInstallation` record + capability map keyed on full semver + format-epoch serializers + per-operation backend routing. Tools never branch on a version number. |
| VS Code | Thin TypeScript shell: registers the MCP server for Copilot, writes configs for the other agents, previews, Problems panel, Live-KiCad supervisor, three `languageModelTools` facades. |

---

## 1. Why the lens matters: what an LLM agent actually needs to succeed

The research digest shows the failure modes are not in KiCad's raw capability (everything from spec to gerbers was reproduced on installed binaries). They are in how agents use it:

1. **Geometry is where agents die.** kicad-sch-api put labels on the wrong pins with a Y-sign bug; ERC passed and only the netlist exposed it. The agent must never compute pin positions: tools return absolute endpoints, and the check tool diffs the real netlist against declared intent.
2. **Silent corruption.** kiutils and kicad-sch-api drop tokens; corruption shows up later as 72 ERC errors. The write path must be a lossless CST with round-trip tests judged by kicad-cli.
3. **Self-judged success.** Prior art converged on "the verdict comes from KiCad". Every mutating tool returns a KiCad-computed delta; stage gates are enforced server-side.
4. **Context budget.** Konnect's 226 tools cost 23K tokens per `tools/list`; Cursor truncates at 40 tools; Claude Code caps output at 25K tokens including images. Catalog: 36 tools, chunked reads, ≤1024 px images.
5. **Version traps.** Files written in a newer epoch are refused by older KiCad; layer IDs renumbered in 9; nets by name in 10; ERC JSON positions 100× too small; SWIG drift. All of this belongs in data (capability map, epoch tables), never in tool signatures.
6. **Long jobs.** Codex kills tools after 60 s; routing can take minutes. Async jobs + `kf_job`.

Therefore the design order is: **tool catalog → feedback loop → gates/skills/evals → backends**.

---

## 2. The feedback loop (designed first)

```
  intent ──► schematic ──► kf_sch_check ──► kf_pcb_sync ──► place ──► route ──► kf_pcb_drc ──► kf_fab_export
     ▲            │  (ERC + netlist + intent diff)  │          │         │       (DRC json, classified)   │
     │            ▼                                 ▼          ▼         ▼                                ▼
     └──── receipt {changed, validator delta, image, next} ◄── every mutating tool ◄──── snapshot (git) ──┘
```

**Receipt (returned by every mutating tool):**
```json
{
  "changed": {"added": ["R3"], "updated": [], "removed": []},
  "validators": {"erc": {"before": 2, "after": 0}, "intent_diff": {"missing": 0, "extra": 0}},
  "snapshot": "a1b2c3d",
  "stage": "erc_clean",
  "next_suggested": "kf_pcb_sync",
  "image": "<ImageContent, only when the tool changed geometry>"
}
```

**Errors that teach (`isError: true`, structured):**
```json
{
  "code": "E_SYMBOL_NOT_FOUND",
  "message": "Device:LED_Small not in KiCad 10.0.0 libraries",
  "why": "lib ids are exact; the index has 5 near matches",
  "fix": "call kf_lib_search {kind:'symbol', query:'LED'} and use one of the returned ids",
  "candidates": ["Device:LED", "Device:LED_Dual_2pin", "Device:LED_RGB", "..."],
  "example": {"tool": "kf_sch_add_symbol", "args": {"lib_id": "Device:LED", "ref": "D1", "...": "..."}}
}
```
Every error code has a `kf_help` page. Unsupported capability errors (`E_UNSUPPORTED`) name the oldest KiCad that has it and the fallback that was used or is available.

**Gates (server-enforced, `force: {reason}` to override, reason is logged into the snapshot message):**
- `kf_pcb_sync` requires last `kf_sch_check.ok == true`.
- `kf_pcb_route_auto` requires zero unplaced/off-board footprints.
- `kf_fab_export` requires `kf_pcb_drc.must_fix_count == 0` (must-fix = clearance, shorting_items, tracks_crossing, unconnected_items, hole_clearance, net_conflict, hole_to_hole; review = courtyards_overlap, silk_*, solder_mask_bridge, lib_footprint_issues; cosmetic = the rest).

**Idempotency:** all `add`/`place`/`assign`/`annotate`/`sync` tools are keyed upserts (same `ref` updates instead of duplicating); reads accept `cursor`/`detail`; mutations accept `if_match` (content hash) and are serialized per project by a lock; each mutation auto-commits to git with the tool name and args digest as the message; `kf_snapshot restore` is the universal undo.

**Chunking:** `kf_sch_get`/`kf_pcb_get` filter by sheet, bbox, refs, nets; `detail: summary|normal|full`; `max_items` + `next_cursor`. Renders accept `region_mm`. Large artifacts (SVG, gerber zips, full netlists) are returned as `resource_link`, never inline.

**Async:** `kf_pcb_route_auto`, `kf_fab_export`, 3D renders return `{job_id}` when the estimate exceeds 20 s or `async: true`; `kf_job wait` polls with streamed progress (Freerouting SSE, kicad-cli stderr).

---

## 3. Components

| # | Component | Language | Responsibility | Reaches KiCad via |
|---|---|---|---|---|
| 1 | **forge-core** (`kiforge`) | Python 3.11, `mcp>=2.2` `MCPServer` | MCP catalog, state machine, locks, git snapshots, receipts/errors, job queue, capability map, prompts | delegates to backends |
| 2 | **forge-cst / forge-ir** (`kiforge.formats`) | Python (stdlib tokenizer) | Lossless CST for .kicad_sch/.kicad_sym/.kicad_pcb/.kicad_mod/.kicad_pro/.kicad_dru; semantic IR; per-epoch serializers; lib_symbols embedding + `extends` flattening; verified pin transform; SQLite FTS index of symbols/footprints | files only |
| 3 | **forge-cli** (`kiforge.backends.cli`) | Python | Install discovery, `--help` probing → capability map, ERC/DRC/netlist/BOM/export/render/upgrade adapters, normalisation (exit codes, mkdir, .prl, ERC ×100, stats units), PyMuPDF raster | `kicad-cli.exe` subprocess with sandboxed `KICAD_CONFIG_HOME` |
| 4 | **forge-swig-driver** (`kiforge_swigdrv`) | Python, stdlib only, runs under `<KiCad>/bin/python.exe` (3.9 on 7.0, 3.11 on 8/9/10) | Headless board build/mutate, netlist→board sync, ZONE_FILLER, DSN/SES, PLOT_CONTROLLER, connectivity; WriteDRCReport on 7 only | `import pcbnew` of that exact install; one request per process |
| 5 | **forge-ipc** (`kiforge.backends.ipc`) | Python, kipy 0.8.0 board/editor/project + regenerated `_pb2` per KiCad tag | Live session (9/10 PCB), primary headless backend on 11; commits = undo steps; busy/not-ready retry; allowlisted `run_action` | protobuf/nng named pipe (GUI) or `kicad-cli api-server` (11) |
| 6 | **forge-router** (`kiforge.backends.router`) | Python + Java (Freerouting jar) + KRT (Python/Rust .pyd) | Autorouting, placement seed/optimize, JRE manager, DSN post-processing, lock/snapshot semantics | DSN/SES files via driver 4 (or IPC on 11); KRT edits .kicad_pcb directly (9/10) |
| 7 | **forge-vscode** | TypeScript (Node 24 host) | MCP registration for Copilot, agent config writer, previews, diagnostics, install picker, Live-KiCad panel, LM-tool facades, tasks | never directly; launches sandboxed KiCad GUI for forge-core |
| 8 | **KiForge Connect** | `plugin.json` (runtime `exec`) + tiny Python launcher, PCM package | Toolbar button in PCB + Schematic editors; hands socket/token/project to forge-core | launched by KiCad's `API_PLUGIN_MANAGER` |
| 9 | **forge-skills / forge-evals** | Markdown (Agent Skills spec) + pytest | Workflow and domain skills; KiCad-graded eval suite; 4-version conformance corpus | through forge-core |

Process topology on a developer machine: VS Code → (spawns) forge-core (stdio MCP) → (spawns per call) `kicad-cli.exe`, `python.exe kiforge_swigdrv`, `java -jar freerouting.jar` (REST), KRT venv python; → (optional) pcbnew.exe/kicad.exe launched with sandboxed config and reached over the IPC pipe. Terminal agents (Claude Code CLI, Codex, Copilot CLI) spawn forge-core themselves from `.mcp.json`/config; nothing depends on the VS Code window being open.

---

## 4. KiCad integration strategy — which surface backs which job, and why

| Job | 7.0 | 8.0 | 9.0 | 10.0 | 11 (master) | Why |
|---|---|---|---|---|---|---|
| Schematic create/edit/annotate/assign | FILE (CST/IR, epoch 20230121) | FILE (20231120) | FILE (20250114) | FILE (20260306) | IPC schematic API (PlaceSymbolFromLibrary, CreateItems, GetSchematicNetlist) behind the same tools | 9/10 IPC has zero schematic commands and crashes eeschema on writes; SWIG has no eeschema; every third-party writer failed fidelity/connectivity tests |
| ERC | E_UNSUPPORTED (export-only) | kicad-cli `sch erc --format json` | same | same (+ `--exit-code-violations`) | same, or RunSchematicJob | No ERC/DRC RPC exists in any IPC version incl. master; kicad-cli JSON is stable (`erc.v1`) |
| Board creation + netlist sync | SWIG driver | SWIG driver | SWIG driver | SWIG driver | IPC `ImportNetlist` / `PlaceFootprintFromLibrary` (headless api-server) | Only SWIG can load footprints and create nets headlessly on 7–10; IPC 9/10 creates empty footprints from LIB_IDs and has no netlist import |
| Placement | SWIG driver; KRT seed (9/10 formats only) | same | SWIG or IPC live; KRT seed | same | IPC (headless or live); KRT | Placement is a file/IPC mutation; KRT gives intent-driven seeding with grades |
| Routing | Freerouting via SWIG DSN + fallback SES parser | Freerouting via SWIG DSN/SES; KRT | same (+ IPC live track creation) | same | Freerouting/KRT; tracks via IPC | PNS is not scriptable (run_action Autoroute waits for mouse and blocks the API); DSN export exists only in SWIG/GUI |
| Zone fill | SWIG ZONE_FILLER | same | same / IPC refill | `drc --refill-zones --save-board` or SWIG / IPC | IPC RefillZones | Stale fills produce phantom clearance errors; fill must precede DRC |
| DRC | SWIG WriteDRCReport (asserts nulled) | kicad-cli `pcb drc --format json --schematic-parity` | same | same (+ refill flags) | same | WriteDRCReport is broken in 10 standalone; kicad-cli is the authority everywhere it exists |
| Exports / fab | kicad-cli export + SWIG PLOT_CONTROLLER | kicad-cli | kicad-cli + jobset | kicad-cli + stats/upgrade | kicad-cli or `RunBoardJobExport*` | Complete and fast (full JLCPCB bundle in ~3 s on 10) |
| Render / vision | svg/pdf → PyMuPDF | same | `pcb render` + svg | same | same (+ `pcb export png`) | Fully headless, ~0.5–2.5 s |
| Live in-editor edits | — | — | IPC (PCB) | IPC (PCB; pcbnew standalone on 10.x because of #24966) | IPC (both editors) | Optional; undoable commits while the human watches |

---

## 5. Verdicts

### 5.1 In-KiCad plugin ↔ VS Code extension channel — **ADOPT, thin form only**

**What we ship:** `KiForge Connect`, a KiCad IPC-API plugin:
```json
{ "identifier": "com.kiforge.connect", "name": "KiForge Connect",
  "runtime": {"type": "exec"},
  "actions": [{ "identifier": "connect", "name": "Connect to VS Code / agent",
                "entrypoint": "connect.exe", "show-button": true,
                "scopes": ["pcb", "schematic"] }] }
```
Packaged for PCM (`metadata.json` `runtime: "ipc"`, `kicad_version: "9.0"`). When clicked, KiCad launches it with `KICAD_API_SOCKET` and `KICAD_API_TOKEN` (and cwd = plugin dir); it POSTs `{socket, token, project, editor}` to forge-core's loopback endpoint (`127.0.0.1:17930`, bearer token from a 0600 lock file — the same pattern Claude Code's own `ide` server uses), optionally runs `code <project>`, logs to a file (KiCad drains child stdout only at exit), and exits within a second.

**What it buys that a plain external client cannot:** (1) reliable instance discovery/identity on Windows — the API socket is a named pipe (no file appears, PID-suffix fallback never triggers, all instances collide on one name, a squatter can pre-bind it); (2) a discoverable button in both PCB **and** Schematic editors (verified on 10.0.0, code present in 9.0); (3) PCM-managed install/update per KiCad major; (4) the human's open-project context for "fix this selected footprint" requests through `kf_live highlight/select`.

**What it does not buy, which is why it is not the core:** an IPC plugin has exactly the API surface of any external client — no schematic API on 9/10, no DRC/ERC/plot RPCs, no PNS router, no event push. It terminates in **forge-core**, not in the VS Code extension, so Claude Code CLI, Codex, Cursor and Copilot all get the live session.

**Rejected alternatives:**
- *SWIG action-plugin hosting a server inside pcbnew*: pcbnew-only, board access from a non-main thread is unsafe, mutations outside `Run()` bypass undo/connectivity/refresh, well-known freeze history, removed in KiCad 11.
- *C++ handler "plugin"*: does not exist as a concept; the only way is to patch and rebuild KiCad, producing a per-version fork that defeats "upgrade KiCad underneath". If a handler is missing (RunDRC), we upstream it (Phase 4), we do not ship a fork.

### 5.2 KiCad source build — **DEFER to Phase 4 (developer-only upstream track)**

Reasons: every capability in scope ran on installed binaries; the KiCad-side crash we hit (eeschema BeginCommit, #24966) was fully symbolicated with `cdb` and the public symbol server in half an hour; `EnableAPILogging=1` in `kicad_advanced` gives a complete request/response transcript; prototyping 11 features is faster on a 10.99 nightly install than on a build. A build genuinely unlocks only: contributing handlers/protos upstream, driving PNS as a batch engine (PCBWorld-style GPL fork — out of scope), or shipping a patched KiCad (rejected).

**Triggers that promote it earlier:** a kicad-cli/IPC bug blocking a Phase-2 exit criterion that cannot be worked around from Python; the decision to upstream `RunDRC`/`GetDrcMarkers`.

**Effort when triggered (this machine):** update VS 2022 17.9 → 17.14.x so vcpkg binary-cache hashes match CI's MSVC 14.44; vcpkg at a drive root at kicad-win-builder's pinned commit; `kicad-gitlab` NuGet source; swigwin 4.3.1; `msvc-win64-debug` preset; `KICAD_RUN_FROM_BUILD_DIR=1`. ~1 working day to the first debug build, 40–80 GB, minutes incremental. Phase 0 writes `docs/dev/kicad-source-build.md` so it is ready but idle.

---

## 6. Version isolation design

**Layer 1 — `KiCadInstallation`:** `{root, cli, python, python_abi ('cp39'|'cp311'), site_packages, dll_dir, share:{symbols,footprints,3dmodels,template,schemas}, semver, commit}`. Discovery: registry Uninstall keys (`DisplayName 'KiCad X.Y'`; 7 under WOW6432Node) ∪ `C:\Program Files\KiCad\*\bin\kicad-cli.exe` ∪ PATH ∪ explicit setting; exact version from `kicad-cli version --format plain`.

**Layer 2 — capability map keyed on FULL semver** (seeded from the verified matrix, re-probed at first use, cached per root+semver):
```
cli.subcommands, cli.flags[subcommand]   ← parsed from `--help` (ignore exit status on 7/8)
has_erc_cli (>=8)  has_drc_cli (>=8)  has_render (>=9)  has_jobset (>=9)  has_upgrade (>=10)
has_ipc (>=9)  ipc_headless (>=11)  ipc_schematic (>=11)  ipc_since[command]  run_action_names[]
swig_available (7–10)  swig_python_abi  swig_shim ('v7'|'v8plus')
gerbers_creates_dir (>=9)  writes_prl (>=10)  ships_wrl (<=9)  erc_json_pos_scale (100 on 8.0.1–10.0.0)
sch_epoch, pcb_epoch, sym_epoch, pro_meta
```
Tools consult flags only. A missing capability returns `E_UNSUPPORTED {oldest_kicad_with_it, fallback}`.

**Layer 3 — format epochs:** the IR is semantic (nets by name, layers by canonical name, UUIDs, booleans, nm). The CST preserves unknown tokens on edit. One serializer per epoch emits exactly that epoch's token set and stamp (sch 20230121/20231120/20250114/20260306; pcb/mod 20221018/20240108/20241229/20260206; sym 20220914/20231120/20241209/20251024; pro meta 1/3), including `tstamp`↔`uuid`, bare `hide`↔`(hide yes)`, `fp_text`↔`property`, numeric net table (<10) vs name-only nets (10), old/new layer-ID tables and 2- vs 4-word `layerselection`. A project declares `target_kicad`; `kf_project upgrade` uses kicad-cli 10 `sch/pcb/sym/fp upgrade` on a copy and semantic-diffs before replacing.

**Process/env isolation:** SWIG runs only in a subprocess of its own install's `python.exe`. kicad-cli and GUIs run with `KICAD_CONFIG_HOME=<storage>/kicad-config` (KiCad appends `<major.minor>/`), pre-seeded per version with `kicad_common.json` (`api.enable_server`, `do_not_show_again.*`, `system.first_run_shown`) and copied `sym/fp-lib-table` plus an empty `design-block-lib-table` on 9/10; `KICAD_DOCUMENTS_HOME` and `KICAD_CACHE_HOME` likewise (also side-steps Controlled Folder Access). `%APPDATA%\kicad` is never touched (CI asserts hash unchanged). Lib tables use `${KICAD<N>_*}` of the target major (KiCad's legacy fallback resolves older N on newer installs); `.wrl` refs are rewritten to `.step` for 10+.

**KiCad 11 seams (abstract now, swap later):** `session.start` (GUI now / `kicad-cli api-server --socket` later), `document.open` (CLI arg now / `OpenDocument` later), `export` (kicad-cli now / `RunBoardJobExport*` later), `schematic.write` (file now / IPC later), `netlist.sync` (SWIG now / `ImportNetlist` later). kipy `_pb2` regenerated from each supported tag's `api/proto`.

**Conformance CI:** for each installed version: read → IR → serialize(epoch) → that version's kicad-cli ERC/DRC/netlist must equal the original; negative tests (unknown token, future stamp) must fail the way KiCad fails.

---

## 7. Schematic strategy (detail)

- **Embedding:** copy the `(symbol "R" …)` block verbatim from the target install's `.kicad_sym`, rename only the top-level name to `Device:R`, keep sub-units unprefixed, flatten `extends` chains into the derived name (raw derived symbols silently lose all pins). Pin lookup scans sub-symbols with unit 0 or the instance unit.
- **Geometry owned by the tool:** `kf_sch_add_symbol` returns absolute pin endpoints using the verified transform; all coordinates snapped to 1.27 mm; property angles relative to symbol rotation.
- **Connectivity rules enforced:** pins connect only at wire endpoints → wires are split at every pin and PWR_FLAG; labels may sit mid-segment; junctions only where three or more segments meet; PWR_FLAG auto-inserted on power-input nets without a driver; NC markers via `kf_sch_add_item`.
- **Annotation:** written to both the `Reference` property and the `instances` block (`/ROOT_UUID[/SHEET_UUID]`); convergent.
- **Hierarchy:** one sheet per functional block; sheet pins + hierarchical labels; only root carries `sheet_instances`.
- **Validation:** `kf_sch_check` = ERC JSON (positions ×100) + netlist export parsed with a real s-expr parser (10 reformatted netlists) + intent diff. `kicad-cli sch upgrade --force` only on a copy (it segfaults and truncates on malformed input).
- **Editing user files:** CST edits; refused while a `~*.lck` shows KiCad holds the file, unless a live session is bound; never edit on disk a board seen live.

## 8. Placement and routing strategy (detail)

**Placement loop:** agent writes intent (edge connectors + side, zones with ref globs, keepouts, decap tethers ≤2 mm, locks) → `kf_pcb_place_auto` (KRT `place_seed` → `place_optimize`, ~1 s) → receipt with grade + before/after render → agent adjusts intent or explicit coordinates via `kf_pcb_place` (courtyard overlap and outline containment checked immediately) → repeat. KiCad's legacy autoplacer (`run_action autoplaceOffboard`) is only an optional seed in a live session.

**Routing loop:** `kf_pcb_route_auto` → lock tracks to keep → SWIG `ExportSpecctraDSN` → DSN post-process (edge clearance rule) → Freerouting REST job with flags from board rules (`automatic_neckdown=false` or `neck_width_um ≥ min track`, `fanout.enabled=false` unless BGA, `copper_to_edge_clearance_um`, `-inc` plane classes, `-mt 1`, `result_json`) → SES import (SWIG; fallback parser on 7) → mandatory zone refill → kicad-cli DRC → receipt + render. KRT `route.py` is engine #2 (no Java). `kf_pcb_route` provides PCBWorld-style primitives for touch-ups with DRC of touched nets. `kf_pcb_fix` rips up violating nets and re-routes, nudges courtyards, cleans tracks, refills.

---

## 9. Agent surface

- **Protocol:** `mcp` 2.2 `MCPServer`; stdio default, `--http` streamable (stateless_http=True); negotiates 2026-07-28 and legacy initialize. Stateless: project path is the handle; live sessions are explicit handles from `kf_live`; no Roots/Sampling/Logging.
- **Catalog hygiene:** 36 tools (< Cursor's 40), deterministic order, names `kf_<stage>_<verb>`, descriptions ≤ 60 words stating stage/precondition/cost/returns, `ToolAnnotations` (read_only, destructive, idempotent), `outputSchema` + `structuredContent` everywhere, `_meta['anthropic/maxResultSizeChars']` on report tools, images ≤1024 px / ≤120 KB.
- **Registration:** VS Code extension → `contributes.mcpServerDefinitionProviders` + `vscode.lm.registerMcpServerDefinitionProvider` (`McpStdioServerDefinition` pointing at the uv-managed venv); "KiForge: Configure agents" writes `<repo>/.mcp.json` (Claude Code + VS Code Agent Host harnesses), `.vscode/mcp.json` (with `dev.watch`/`dev.debug: python`), `.cursor/mcp.json`, `~/.codex/config.toml` (`tool_timeout_sec = 600`, `startup_timeout_sec = 60`), `~/.codeium/windsurf/mcp_config.json`, `~/.copilot/mcp-config.json`; docs give `claude mcp add`/`codex mcp add` one-liners; `enableAllProjectMcpServers` noted for teams.
- **Copilot extras:** `#kicadRender`, `#kicadCheck`, `#kicadStatus` `languageModelTools` with confirmation UI, calling forge-core over loopback.
- **Skills:** `.claude/skills/kiforge-flow/SKILL.md` (spec-only frontmatter; read by Claude Code, Copilot VS Code/CLI, Codex, Cursor, Windsurf) — the loop, gates, and "look before you claim"; plus `kicad-geometry`, `netlist-first-schematic`, `erc-triage`, `drc-triage`, `part-selection`, `reference-circuits` (ESP32-S3 minimal, USB-C 2.0, MCP73831, LDO, I²C pull-ups), `placement`, `routing`, `visual-verification`, `context-budget`. `AGENTS.md`/`CLAUDE.md` carry the always-on summary.
- **Prompts:** `new-board-from-spec`, `fix-drc`, `review-before-fab`.
- **Evals:** `evals/` tasks graded only by KiCad outputs (ERC=0, intent diff empty, unrouted=0, must_fix=0, fab bundle present) with tool-call and token budgets; runners for Claude Code (`claude -p`, isolated `CLAUDE_CONFIG_DIR`), Copilot CLI and Codex; results published as a markdown artifact.

---

## 10. Tool catalog (36)

| Tool | Stage | Args (abridged) | Returns | Backend |
|---|---|---|---|---|
| `kf_env` | setup | `refresh?` | installs + capabilities, live/router status | cli discovery, ipc ping |
| `kf_project_create` | setup | `dir, name, target_kicad?, layers?, template?` | paths, epochs, snapshot | cst + git |
| `kf_project_status` | setup | `project, detail?` | stage, blockers, last checks, next | core |
| `kf_snapshot` | setup | `project, action: create/restore/diff/list` | sha / files / semantic diff | git + ir |
| `kf_intent` | requirements | `project, action: set/get/render, intent?` | intent, validation errors, png | core |
| `kf_lib_search` | parts | `kind, query, filters?, limit?` | matches + near-misses | FTS index |
| `kf_lib_info` | parts | `id, unit?, image?` | pins/pads geometry, bbox, preview | cst / swig / cli svg |
| `kf_parts_search` | parts | `query, basic_only?, package?` | LCSC rows with price/stock | parts DB |
| `kf_parts_import` | parts | `project, lcsc` | lib_id, fpid, model | easyeda2kicad + cli upgrade |
| `kf_datasheet` | parts | `source, pages?/query?` | text/matches | HTTP + PyMuPDF |
| `kf_sch_add_symbol` | schematic | `project, sheet, lib_id, ref, value, at, rotation?, mirror?, fields?` | uuid, absolute pins, receipt | cst (7–10) / ipc (11) |
| `kf_sch_connect` | schematic | `project, sheet, net, endpoints[], style?` | items, junctions, receipt | cst / ipc (11) |
| `kf_sch_add_item` | schematic | `project, sheet, item{type,...}` | uuid, receipt | cst / ipc (11) |
| `kf_sch_edit` | schematic | `project, sheet, ops[], if_match?` | applied/skipped, receipt | cst |
| `kf_sch_annotate` | schematic | `project, scheme?, start_per_sheet?` | mapping, receipt | cst |
| `kf_sch_assign_footprints` | schematic | `project, map?/rules?` | assigned, unresolved+hints | cst + index |
| `kf_sch_get` | schematic | `project, sheet?, refs?, bbox?, include?, detail?, cursor?` | chunked JSON | cst / cli netlist / ipc (11) |
| `kf_sch_render` | schematic | `project, sheet?, region_mm?, overlay?` | image(s) | cli svg/pdf + PyMuPDF |
| `kf_sch_check` | schematic | `project, severity?` | ok, ERC (×100 fixed), intent diff | cli erc + netlist |
| `kf_pcb_setup` | board | `project, outline?, layers?, rules?, netclasses?, preset?` | applied rules | cst + swig |
| `kf_pcb_sync` | board | `project, remove_orphans?, force?` | added/updated/removed, parity | swig / ipc(11) + cli drc |
| `kf_pcb_get` | board | `project, refs?, bbox?, include?, detail?, cursor?` | chunked board JSON | swig / ipc |
| `kf_pcb_place` | placement | `project, placements[], relative_to?` | overlaps, off_board, image | swig / ipc |
| `kf_pcb_place_auto` | placement | `project, intent{...}, optimize?` | placements, grade, image | KRT |
| `kf_pcb_render` | board | `project, view?, layers?, region_mm?, overlay?` | image | cli render/svg |
| `kf_pcb_route_auto` | routing | `project, engine?, nets?, passes?, timeout_s?, async?` | routed/unrouted, DRC summary or job_id | router + swig + cli |
| `kf_pcb_route` | routing | `project, ops[]` | uuids, drc_delta | swig / ipc |
| `kf_pcb_zones` | routing | `project, zones?, fill?, stitch_vias?` | uuids, filled area | swig / cli / ipc |
| `kf_pcb_drc` | drc | `project, schematic_parity?, refill_zones?` | ok, classified violations | cli drc (swig on 7) |
| `kf_pcb_fix` | drc | `project, actions[], max_iterations?` | before/after | router + swig + cli |
| `kf_fab_export` | fab | `project, targets?, profile?, force?` | files, warnings | cli / jobset / ipc(11) |
| `kf_fab_bom` | fab | `project, fields?, group_by?` | rows, cost, stats | cli bom/stats + parts DB |
| `kf_report` | review | `project, out?` | markdown + images | composite |
| `kf_live` | live | `project, action: status/attach/launch/detach/highlight/select/undo/run_action` | session handle | ipc + supervisor |
| `kf_job` | setup | `job_id, action?, wait_s?` | state, progress, result | core |
| `kf_help` | setup | `topic` | markdown | docs bundle |

---

## 11. VS Code surface

1. **Zero-config agents:** MCP provider registration (Copilot Local harness; forwarded to the Agent Host), "Configure agents" command for the others, status bar item showing server health and bound KiCad session.
2. **Previews:** read-only custom editors for `*.kicad_sch`/`*.kicad_pcb` (kicad-cli SVG raster in a webview, layer toggles; KiCanvas optional behind a setting; "Open in KiCad" escape hatch).
3. **Problems panel:** `DiagnosticCollection` fed by `kf_sch_check`/`kf_pcb_drc` (positions corrected), click-to-locate in the preview.
4. **Live KiCad panel:** launch sandboxed `pcbnew.exe`/`kicad.exe` (config root seeded), show session state, detect crashes (#24966) and offer relaunch, warn when a GUI holds a file the agent wants to edit.
5. **Install picker:** all installed majors side by side; sets `target_kicad` for the project.
6. **Tasks + tree:** jobset/export tasks with problem matchers; project tree (sheets, boards, last results, jobs).
7. **Grammars:** TextMate for `.kicad_*` and `.kicad_dru`.
8. **Copilot facades:** 3 `languageModelTools` with confirmation messages.

Prerequisite: install Node LTS (`winget install OpenJS.NodeJS.LTS`) before Phase 3; `engines.vscode ^1.101.0`; `@vscode/vsce` 4.

---

## 12. Repository layout

```
kiforge/
  README.md  AGENTS.md  CLAUDE.md  .mcp.json  .vscode/mcp.json  .cursor/mcp.json
  .claude/skills/<11 skills>/SKILL.md
  packages/
    forge-core/         src/kiforge/{server.py, tools/, state/, formats/, backends/, data/}
    forge-swig-driver/  kiforge_swigdrv/{main.py, shim_v7.py, shim_v8plus.py, ops/, wx_noassert.py, ses_fallback.py}
    forge-vscode/       package.json, src/, media/
    kiforge-connect/    plugin.json, connect.py, icons/, pcm/metadata.json
  evals/                tasks/, graders/, runners/, report.py
  tests/                corpus/{7.0,8.0,9.0,10.0}, test_cst_roundtrip.py, test_epoch_serializers.py,
                        test_cli_capability.py, test_swig_driver_matrix.py, test_ipc_live.py (opt-in), test_router.py
  docs/                 architecture.md, tool-reference.md (generated), agent-configs.md, compatibility.md (generated),
                        dev/kicad-source-build.md, dev/debugging-kicad-with-symbols.md
  scripts/              bootstrap.ps1, seed_config_root.py, build_vsix.ps1
```
Licensing: KiForge is MIT. Freerouting (GPL) runs as a separate process; KRT (MIT) is vendored by pin; Konnect/nevenfo/KiBot (AGPL) are references only; no code copied from them.

---

## 13. Roadmap

| Phase | Weeks | Deliverables | Exit criteria |
|---|---|---|---|
| **0 Foundations** | 1–2 | Install discovery + capability map; config-root seeding; forge-cli adapter with normalisations; MCP skeleton (`kf_env`, `kf_project_*`, `kf_snapshot`, `kf_sch_check`, `kf_pcb_drc`, renders, `kf_help`, `kf_job`); receipts/errors; agent configs; `kiforge-flow` skill v0; source-build and symbol-debugging docs | Claude Code and Copilot list the tools; ERC/DRC/render run on the demo corpus for 8/9/10 with corrected coordinates and ≤120 KB images; 7 returns `E_UNSUPPORTED` with hints; user `%APPDATA%\kicad` untouched in tests |
| **1 Schematic** | 3–6 | CST/IR + sch/sym epoch serializers; FTS index; `kf_sch_*` incl. transform, wire splitting, PWR_FLAG, instances; `kf_intent`; parts tools; eval tasks 1–2 | 0 token loss on the 4-version corpus with identical kicad-cli ERC/netlist; agent builds tasks 1 and 2 from spec with ERC=0 and empty intent diff on 8.0.1/9.0.5/10.0.0 in ≤40 calls; upgrade drift canary green |
| **2 Board** | 7–10 | SWIG driver + shims + conformance suite; `kf_pcb_*`, `kf_fab_*`; forge-router (Freerouting REST + JRE 25, KRT); gates; drc/placement/routing skills | Task 2 spec→gerbers headless on 9/10 (and 8) with 0 unrouted, 0 must-fix, parity clean; driver matrix passes on all four installs; no hang beyond watchdog; route job streams and cancels |
| **3 VS Code + live + Connect** | 11–14 | Extension VSIX; forge-ipc live sessions (pcbnew standalone on 10.x); KiForge Connect PCM; eval harness in CI; prompts, full skills, generated compatibility docs | VSIX gives Copilot and Claude Code identical tools with zero config; agent edits an open pcbnew 10 board through `kf_live` with one undo per call; Connect button binds within 5 s in both editors; CI evals green on 9/10 |
| **4 KiCad 11 + upstream** | 15+ | Nightly install in the matrix; IPC schematic + headless api-server backends; SWIG-free path; source build for upstream MRs (RunDRC/GetDrcMarkers, ERC JSON scale, #24966); optional MCP Apps viewer; 7 declared best-effort | Task 2 passes on the 11 nightly with no SWIG in the call path; ≥1 upstream MR opened; a new KiCad point release needs only data changes |

---

## 14. Risks and mitigations

| Risk | Mitigation |
|---|---|
| SWIG removed in 11 while sync/DSN/fill depend on it | SWIG isolated behind flags; Phase 4 IPC-headless equivalents; fallback SES parser exists, DSN writer can be added |
| Headless SWIG crashes/hangs on Windows | Assert shim, one BOARD per process, safe zone outline API, BuildConnectivity before Fill, watchdog, no SWIG DRC except on 7, CI matrix |
| IPC writes crash eeschema on 10.0.x; GUI-only; single pipe; AS_BUSY | pcbnew standalone sandboxed on 10.x; never write to eeschema before 11; retry/backoff; foreign-instance detection; Connect hand-off; crash supervisor; headless path never needs a GUI |
| Format drift breaks older KiCad | Exact per-epoch serializers, negative tests, upgrade canary, semver-keyed capabilities, CST preserves unknown tokens |
| Freerouting: Java 25, GPL, rule-violating defaults, SES wipes tracks | Managed JRE, separate process, rule-derived flags, `-mt 1`, lock-before-export, snapshot-before-import, refill + kicad-cli DRC authority, KRT fallback |
| Context/tool-cap/timeouts across agents | 36 tools, `kf_help`, chunked reads, small images, resource links, async jobs, context-budget skill |
| Agent self-judgement, guessing lib ids, hand-routing | Server-side gates, KiCad-computed receipts, near-miss search results, skills, KiCad-graded evals |
| Windows traps (wizard, CFA, pipe names, `.prl`) | Seeded config/documents/cache homes, Connect hand-off, `.gitignore` `.prl`, tests assert user config untouched |
| KiCad 7 gaps | Declared best-effort; export + SWIG DRC + fallback SES + SVG renders; upgrade path offered |
| kipy 0.8.0 wheel inconsistencies | Use board/editor/project only; regenerate `_pb2` per tag; runtime probing |
| Fast-moving third parties (KRT, KiCanvas, jlcparts) | Pinned versions behind interfaces; kicad-cli SVG is the guaranteed preview |
| No Node on the dev box | Install Node LTS before Phase 3; Phases 0–2 need none |

---

## 15. Key technical claims the design depends on (each testable)

1. kicad-cli 8.0.1/9.0.5/10.0.0 `pcb drc --format json` emit drc.v1 reports with per-violation uuid and correct mm positions; `--exit-code-violations` returns 5 on violations.
2. kicad-cli 8.0.1/9.0.5/10.0.0 `sch erc --format json` item positions are exactly 100× too small; the text report is correct.
3. KiCad 9.0.x/10.0.x IPC registers no schematic commands beyond GetOpenDocuments; BeginCommit/CreateItems/HitTest crash eeschema 10.0.0.
4. IPC in 9/10 requires a running GUI, has no DRC/ERC/plot/netlist-import/footprint-from-library commands, and uses one fixed named pipe per machine.
5. A GUI launched with a seeded `KICAD_CONFIG_HOME` (api.enable_server, privacy flags, three lib tables) starts without the Setup wizard and answers IPC within 15 s.
6. kicad-python 0.8.0 edits boards on both 9.0.5 and 10.0.0; unsupported commands fail with 'no handler available'.
7. Bundled SWIG pcbnew on 7.0.6/8.0.1/9.0.5/10.0.0 headlessly creates boards, loads footprints, creates nets, adds tracks/vias/zones, fills and saves files that pass kicad-cli DRC.
8. `WriteDRCReport` is unusable in 10.0.0 standalone python but works on 7/8/9 once wxTheAssertHandler is nulled.
9. A second BOARD per SWIG process makes SaveBoard crash intermittently on 8/9/10; `SetOutline` without disowning yields zero-area fills and access violations.
10. `ExportSpecctraDSN(board, path)` works headlessly on 7–10; `ImportSpecctraSES(board, path)` on 8–10; SES import deletes non-locked tracks and applies placement.
11. Freerouting 2.4.1 needs Java 25, routes a 30-net board in seconds, and passes KiCad DRC only with neckdown and fanout disabled.
12. Freerouting 2.4.1's REST API requires Freerouting-Profile-ID and Freerouting-Environment-Host headers even with auth disabled.
13. KiCadRoutingTools v0.22.x routes and seeds placement on KiCad 9/10 files without a KiCad process and writes DRC constraints into `.kicad_pro`.
14. A `.kicad_sch` in epoch N loads on KiCad N and newer only; an unknown token or future stamp is fatal on older parsers.
15. KiCad 10 requires every used symbol embedded in lib_symbols; raw `extends` symbols load with no pins.
16. Pins connect only at wire endpoints on the 1.27 mm grid; labels connect mid-wire; the pin transform is rot0 (X+lx,Y−ly), rot90 (X−ly,Y−lx), rot180 (X−lx,Y+ly), rot270 (X+ly,Y+lx), mirror x flips the Y offset, mirror y flips the X offset.
17. kicad-cli 7.0.6 lacks drc/erc/render/jobset; 8 adds drc/erc/bom; 9 adds render/jobset; 10 adds upgrade and `--refill-zones --save-board`.
18. kicad-cli 10 `pcb render` takes ~2.5 s for a 63-part board; SVG exports rasterise legibly with PyMuPDF in <0.3 s.
19. kicad-cli 10 writes a `.kicad_prl` on read-only operations; kicad-cli 7/8 do not create gerber output directories.
20. kiutils 1.4.8 and kicad-sch-api 0.5.6 corrupt or lose tokens on 8/9/10 round-trips; a generic lossless s-expression round-trip preserves ERC and netlist exactly.
21. MCP Python SDK 2.2 negotiates 2026-07-28, serves legacy clients, and returns ImageContent/structuredContent consumed by Claude Code, Copilot and Cursor.
22. VS Code 1.101+ lets an extension register an MCP server via `vscode.lm.registerMcpServerDefinitionProvider`; Claude Code reads project `.mcp.json`.
23. Cursor sends only the first 40 MCP tools; Claude Code caps tool output at 25k tokens including images; Codex defaults to a 60 s tool timeout.
24. A KiCad 10 IPC plugin action scoped [pcb, schematic] with runtime `exec` shows a toolbar button in both editors and is launched with `KICAD_API_SOCKET`/`KICAD_API_TOKEN`.
25. symbols.kicad.org/kicad-stable serves PDBs matching the installed 10.0.0 binaries, so crash minidumps symbolicate to source lines without a source build.
26. KiCad master (11) adds a full schematic IPC API, ImportNetlist, PlaceFootprintFromLibrary, export jobs and `kicad-cli api-server`, and removes SWIG pcbnew.

---

## 16. What to do first (this week)

1. Create `kiforge/` (MIT), `uv` venv with `mcp>=2.2`, `pymupdf`, `sexpdata`-style tokenizer written in-house.
2. Implement install discovery + capability probe and seed it from the verified matrix; add the config-root seeder for 7/8/9/10.
3. Ship `kf_env`, `kf_sch_check`, `kf_pcb_drc`, `kf_sch_render`, `kf_pcb_render` with receipts and errors, plus `.mcp.json`/`.vscode/mcp.json`, and run them from Claude Code against the demo corpus.
4. Write `docs/dev/kicad-source-build.md` and `docs/dev/debugging-kicad-with-symbols.md`; do not start the build.
5. Start the CST round-trip test on the 4-version corpus — it is the single test that separates a safe schematic engine from every library that failed.
