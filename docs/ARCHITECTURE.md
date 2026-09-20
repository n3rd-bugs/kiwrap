# KiWrap — Architecture

Status: decision document, 2026-09-18 (working name *kiwrap*; the synthesis was produced under the name "KiForge", which collides with existing KiCad projects). Final synthesis of the three reviewed proposals ([proposals](research/proposals/)): the agent-ergonomics-first design as the winning base, with Keel's planner / certification / emit policy / SWIG-free twins and Ratsnest's MVP-first sequencing grafted in. Every factual statement below was checked against the [research digests](research/); items marked **[verified]** were reproduced on this machine (KiCad 7.0.6, 8.0.1, 9.0.5, 10.0.0), items marked **[source/docs]** were confirmed from KiCad source or official docs only. After synthesis, the 12 load-bearing claims were adversarially re-verified ([VERIFICATION.md](VERIFICATION.md)); the corrections are applied in the body below and summarised in §16, and the completeness review's gaps are in §17.

---

## 1. Vision and scope

**Goal.** A VS Code-hosted interface that lets an AI agent (Claude Code, Copilot agent mode, Cursor, Windsurf, Codex, Copilot CLI) do end-to-end electronics development on KiCad: natural-language spec → schematic → ERC → PCB → placement → routing → DRC → fabrication outputs, implemented as a **wrapper over installed KiCad binaries** so KiCad can be upgraded underneath (7 → 8 → 9 → 10 → 11+) without rewriting the tools.

**Shape.** One MIT Python MCP server (**kiwrap-core**, 36 stage-named `kw_*` tools) is the only agent contract. A thin TypeScript VS Code extension registers it, previews files and shows diagnostics. A thin PCM-packaged KiCad plugin (**KiWrap Connect**) hands a running KiCad's socket/token to kiwrap-core. Everything the tools do is backed by four exchangeable surfaces: files (lossless s-expression engine), `kicad-cli`, a SWIG driver run under each install's own Python (7–10), and the IPC API (9/10 live, 11 headless).

**In scope (v1):** 2–4 layer boards on KiCad 10 first, then 9 certified and 8 partial (schematic + fab); **Windows and Linux certified from Phase 0**; JLCPCB fabrication profile; Freerouting/KRT autorouting; intent-driven placement; hierarchical and multi-unit schematics; live editing of an open *board* on 9/10 (Phase 3, sandboxed pcbnew); KiCad 11 readiness. Claude Code (CLI + VS Code) is the first-class agent client; others get generated configs and best-effort support.

**Out of scope:** driving KiCad's push-and-shove router as a batch engine, shipping a patched KiCad, cloud routers, KiCad 7 beyond best-effort, a from-scratch autoplacer, bus wires (Phase 2), macOS (later), scheduled paid evals (manual only).

**Non-negotiables:** KiCad is the judge (every mutating tool returns kicad-cli ERC/DRC deltas); geometry is computed by tools, never by the model; the user's `%APPDATA%/kicad` is never modified; no shipped component runs code inside KiCad.

---

## 2. Verified KiCad capability matrix

Legend: ✔ verified locally · ◐ verified from source/docs only · ✘ absent · — n/a.

| Surface / capability | 7.0.6 | 8.0.1 | 9.0.5 | 10.0.0 | 11 (master, not installed) |
|---|---|---|---|---|---|
| **kicad-cli** `pcb export` gerbers/drill/pos/step/svg/pdf | ✔ | ✔ | ✔ | ✔ | ◐ |
| `pcb drc --format json` (drc.v1, correct mm positions) | ✘ | ✔ | ✔ | ✔ (+`--refill-zones --save-board`) | ◐ |
| `sch erc --format json` (positions **×100 too small**) | ✘ | ✔ (×100) | ✔ (×100) | ✔ (×100) | ◐ |
| `--exit-code-violations` → exit 5 | ✘ | ✔ | ✔ | ✔ | ◐ |
| `pcb render` (3D PNG) | ✘ | ✘ | ✔ | ✔ (~2.5 s) | ◐ |
| `jobset run` | ✘ | ✘ | ✔ | ✔ | ◐ |
| `sch upgrade` / `pcb upgrade` | ✘ | ✘ | ✘ | ✔ (in place; segfaulted on malformed input) | ◐ |
| `sym upgrade` / `fp upgrade` | ✔ | ✔ | ✔ | ✔ | ◐ |
| `pcb export stats --format json` | ✘ | ✘ | ✘ | ✔ (unit strings, mojibake) | ◐ |
| `pcb export gerbers -o` creates the directory | ✘ | ✘ | ✔ | ✔ | ◐ |
| Writes `.kicad_prl` on read-only ops | ✘ | ✘ | ✘ | ✔ | ◐ |
| `api-server` (headless IPC) | ✘ | ✘ | ✘ | ✘ | ◐ (11 only) |
| **Formats written** sch / pcb+mod / sym | 20230121 / 20221018 / 20220914 ✔ | 20231120 / 20240108 / 20231120 ✔ | 20250114 / 20241229 / 20241209 ✔ | 20260306 / 20260206 / 20251024 ✔ | 20260830 / 20260901 ◐ |
| Older reader rejects a file genuinely written by a newer KiCad | ✔ (sch exit 3; pcb exit −1, shown as 127 in Git Bash) | ✔ (same) | ✔ (exit 3) | ✔ (exit 3) | — |
| Version stamp alone rejects a future file | pcb ✘ (only after a parse error); sch ✔ | pcb ✔; sch only inside the `generator_version` branch | same as 8 | same as 8 | — |
| Unknown token rejected | ✔ except inside `(general)`, `(pcbplotparams)` and stackup `(layer)` (silently skipped) | same | same | same | — |
| Layer IDs renumbered, 4-word layerselection | — | — | ✔ | ✔ | ◐ |
| Nets by name only (no netcode table) | ✘ | ✘ | ✘ | ✔ | ◐ |
| Ships `.wrl` 3D models | ✔ | ✔ | ✔ | ✘ (STEP only) | ◐ |
| **SWIG pcbnew** present (bundled Python) | ✔ (3.9.16) | ✔ (3.11.5) | ✔ (3.11.5, deprecated warning) | ✔ (3.11.5, deprecated) | ✘ removed ◐ |
| Headless board build, FootprintLoad, nets, tracks, ZONE_FILLER, SaveBoard | ✔ | ✔ | ✔ | ✔ | — |
| `ExportSpecctraDSN(board, path)` | ✔ | ✔ | ✔ | ✔ | — |
| `ImportSpecctraSES(board, path)` | ✘ (1-arg GUI form only) | ✔ | ✔ | ✔ | — |
| `WriteDRCReport` headless | ✔ with wx assert shim | ✔ with shim | ⚠ shim required (hangs on a wx assert dialog otherwise) | ✘ (`Pgm()` assert) | — |
| Netlist reader | ✘ | ✘ | ✘ | ✘ | — |
| **IPC API** server (GUI required) | ✘ | ✘ | ✔ | ✔ | ◐ (+headless) |
| PCB handlers: items CRUD, commits, nets, layers, refill, save/save_as/to-string, RunAction | — | — | ✔ (~50 command types) | ✔ (~70; +groups, variants, GetItemsById…) | ◐ (~100; +ImportNetlist, PlaceFootprintFromLibrary, design rules, RunBoardJobExport*) |
| Schematic handlers | — | — | same layout as 10; BeginCommit crashed 9.0.1 locally ✔ | GetOpenDocuments + 6 generic editor handlers registered, but only GetOpenDocuments works; **all six** (BeginCommit/EndCommit/CreateItems/UpdateItems/DeleteItems/HitTest) **crash the hosting process** ✔ (#24966; with pcbnew also open the crash is intermittent even for board-targeted writes; fixed for 10.0.7, unreleased as of 2026-09-18) | ◐ full (items, selection, netlist, hierarchy, PlaceSymbolFromLibrary, jobs) |
| DRC/ERC, plot/export, netlist import, footprint from library over IPC | — | — | ✘ | ✘ (LIB_ID-only footprint → 0 pads ✔) | ◐ exports/import yes; **no RunDRC/RunERC even on master** |
| `run_action` of interactive tools | — | — | ✔ starts tool then AS_BUSY | ✔ same | ◐ |
| `plugin.json` (api.v1, runtime python/exec, scopes incl. schematic) | ✘ | ✘ | ✔ schema | ✔ button shown in pcbnew **and** eeschema; exec launch + env hand-off ◐ | ◐ |
| kicad-python 0.8.0 connects and edits boards | — | — | ✔ | ✔ | — (kipy.schematic/board_jobs imports broken ✔) |
| **Config** `KICAD_CONFIG_HOME` → `<dir>/<major.minor>/` | ✔ | ✔ | ✔ | ✔ | ◐ |
| Setup wizard blocks GUI/API until prompts + 3 lib tables exist | — | — | ✘ (no wizard; data-collection prompt) | ✔ | ◐ |
| **External** Freerouting 2.4.1 DSN/SES round trip → DRC 0/0 | ✔ (fallback SES parser) | ✔ | ✔ | ✔ | — |
| KiCadRoutingTools 0.22.1 route + place_seed | ✘ (format) | ✘ (format) | ✔ | ✔ | ◐ |
| easyeda2kicad LCSC import + sym/fp upgrade | — | — | — | ✔ | — |
| **Debug** symbols.kicad.org PDBs + cdb; `EnableAPILogging=1` api.log | — | — | ◐ | ✔ | — |

Machine facts that matter: PATH `java` is 1.8 (Java 17 exists under Adoptium, but Freerouting 2.4.1 needs Java 25 → managed JRE); no Node/npm on PATH; VS 2022 17.9 (MSVC 14.39) present, CI builds use 14.44; `cdb` from the Windows 10 SDK debuggers is installed and symbolicates KiCad crashes against symbols.kicad.org **[verified]**. All verification ran on the installed point releases (10.0.0, 9.0.5, 8.0.1, 7.0.6); 10.0.6 is current and the 10.0 branch already differs (SES import error handling, #24966 handling) — see §17.

---

## 3. Architecture

### 3.1 Components

| # | Component | Language | Responsibility | Reaches KiCad via |
|---|---|---|---|---|
| 1 | **kiwrap-core** | Python 3.11, `mcp>=2.2` `MCPServer` | 36-tool MCP catalog, stage machine + gates, per-project lock, git snapshots, receipts/errors, job queue, capability record + planner, CLI | backends only |
| 2 | **kiwrap-formats** | Python, stdlib, zero deps | Lossless CST, semantic IR, tolerant lenses + strict per-epoch serializers, lib_symbols embedding + `extends` flattening, verified pin transform, FTS library index, report/netlist parsers | files |
| 3 | **kiwrap-cli backend** | Python | Install discovery, `--help`-derived flag map, ERC/DRC/netlist/BOM/export/render/upgrade adapters, quirk normalisation, SVG/PDF→PNG raster via pypdfium2 or resvg (never PyMuPDF — AGPL) | `kicad-cli.exe` subprocess, sandboxed env |
| 4 | **kiwrap-swig-driver** | Python (stdlib), run by `<install>/bin/python.exe`; **GPL-3.0-or-later, separate program** (links KiCad's GPL bindings) | Closed enum of JSON-argument operations: board mutate, **kiwrap's own netlist updater** (SWIG has no netlist reader), zones/fill, DSN/SES, plot, queries; WriteDRCReport on 7 only; shims v7/v8plus | `import pcbnew` of that install, one job per process |
| 5 | **kiwrap-ipc backend** | Python; kipy 0.8.0 for 9.0.x and 10.0.0–10.0.6; kipy ≥ 0.9 or vendored-proto client for ≥ 10.0.7 (commit `document` field) and for 11 | Live session supervisor, commit-per-mutation, busy/not-ready retry, Since table, allowlisted run_action; headless api-server on 11 | protobuf/nng named pipe or api-server socket |
| 6 | **kiwrap-router** | Python + Java 25 + KRT venv | Freerouting REST/CLI, managed JRE, rule-derived settings, lock-then-snapshot, KRT router/seeder | DSN/SES files; KRT edits `.kicad_pcb` |
| 7 | **kiwrap-vscode** | TypeScript, VS Code ≥1.101 | MCP registration, Configure agents, previews, Problems, install picker, Live-KiCad panel, tasks, grammars, 3 LM-tool facades | never directly |
| 8 | **KiWrap Connect** | `plugin.json` exec + `.bat`/`.py` launcher, PCM `runtime: ipc` | Toolbar button in the PCB editor (the Schematic-editor button only opens VS Code before 10.0.7); POSTs `{token, pid, project, editor}` to kiwrap-core, which verifies who owns the pipe; exits | launched by KiCad |
| 9 | **kiwrap-conformance / evals / skills** | pytest + corpus; Markdown | T1–T9 certification, `kiwrap certify`, KiCad-graded evals, Agent Skills | all surfaces via kiwrap-core |

### 3.2 Diagram

```
 Claude Code | Copilot agent mode | Cursor | Windsurf | Codex | Copilot CLI
        \            |              |          |         |        /
         +-----------+----- MCP 2026-07-28 (stdio / streamable HTTP) -----+
                                     |
  kiwrap-vscode (TS) ---spawns---> kiwrap-core (Python)  36 kw_* tools
  preview / Problems /            | planner: capability record -> ordered providers
  register / launch GUI           | gates, locks, snapshots, receipts, jobs
                                  |
      +------------+--------------+---------------+----------------+
      |            |              |               |                |
  kiwrap-formats  kiwrap-cli    kiwrap-swig-driver  kiwrap-ipc      kiwrap-router
  CST + IR +     kicad-cli    <ver>/bin/         kipy (9/10)    Freerouting 2.4.1
  epoch emitters <ver>/bin    python.exe         proto11 (11)   (JRE 25, REST)
      |            |              |               |                KRT (9/10)
   .kicad_*     ERC/DRC/       pcbnew SWIG     pcbnew.exe /        |
   files        exports/       (7-10 only)     kicad.exe GUI    DSN/SES
                render                         (9/10) or         files
                                               kicad-cli api-server (11)
                                                      ^
                                        KiWrap Connect (socket+token hand-off)
```

### 3.3 Data flow (headless, the default)

1. `kw_intent set` records blocks, rails, interfaces, constraints and intended nets.
2. `kw_sch_*` write the schematic through kiwrap-formats in the project's pinned epoch; `kw_sch_check` runs `kicad-cli sch erc`, exports the netlist and diffs it against intent.
3. `kw_pcb_setup` writes the initial board through the file emitter (kiwrap-formats); `kw_pcb_sync` runs kiwrap's own netlist updater (no KiCad surface imports a netlist headlessly on 7–10) using SWIG `FootprintLoad` + pad-net assignment; `kicad-cli pcb drc --schematic-parity` is the oracle that confirms the sync.
4. `kw_pcb_place[_auto]` moves footprints; each batch returns overlaps, off-board parts and a render.
5. `kw_pcb_route_auto` locks kept tracks, exports DSN, runs Freerouting, imports SES, refills zones, runs DRC; `kw_pcb_fix` iterates.
6. `kw_pcb_drc` gates `kw_fab_export`, which produces the fabrication bundle via kicad-cli.
7. Every mutating step commits a snapshot to the project's private store (`<project>/.kiwrap/snapshots.git`, a bare git dir with the project as work tree, tracking only kiwrap-owned files: `*.kicad_*` minus `.kicad_prl`, `*-lib-table`, `.kiwrap/state.json`) and returns a receipt. The user's own git repository, if any, is never touched.

The live path (`kw_live`) is the same tool contract with the IPC provider bound: results are pushed into the open board as one undoable commit.

---

## 4. KiCad integration strategy

| Job | 7.0 | 8.0 | 9.0 | 10.0 | 11 | Why |
|---|---|---|---|---|---|---|
| Schematic create/edit/annotate/assign | FILE (epoch 20230121, best-effort) | FILE (20231120) | FILE (20250114) | FILE (20260306) | FILE default; IPC schematic provider for live sessions | No schematic API through 10; eeschema 10.0.0 crashes on writes; every third-party writer failed fidelity/connectivity tests |
| ERC | E_UNSUPPORTED | kicad-cli | kicad-cli | kicad-cli | kicad-cli or job | Only ERC executor; no ERC RPC in any version |
| Board build / netlist sync | file emitter + kiwrap netlist updater on SWIG | same | same | same | IPC ImportNetlist + PlaceFootprintFromLibrary | Nothing on 7–10 imports a netlist headlessly (SWIG exposes no `NETLIST_READER`/`BOARD_NETLIST_UPDATER`; `FootprintLoad` takes a library *path*, so the fp-lib-table nickname is resolved by kiwrap). The initial board is emitted as a file (Phase 1); the updater is kiwrap code; `kicad-cli pcb drc --schematic-parity` is the oracle |
| Placement | SWIG | SWIG | SWIG / IPC live; KRT seed | same | IPC; KRT | Placement is a file/IPC mutation; KRT gives intent-driven seeds (9/10 formats only) |
| Routing | Freerouting via SWIG DSN + fallback SES parser | Freerouting via SWIG DSN/SES; KRT ✘ | Freerouting; KRT; IPC track push live | same | Freerouting/KRT; tracks via IPC | PNS not scriptable; DSN export exists only in SWIG/GUI on 7–10 |
| Zone fill | SWIG ZONE_FILLER | SWIG | SWIG / IPC RefillZones | `drc --refill-zones --save-board` / SWIG / IPC | IPC | Stale fills produce phantom clearance errors |
| DRC | SWIG WriteDRCReport + assert shim | kicad-cli | kicad-cli | kicad-cli | kicad-cli | WriteDRCReport asserts on 10; kicad-cli is the authority wherever it exists |
| Exports / fab | kicad-cli export (+ SWIG PLOT_CONTROLLER) | kicad-cli | kicad-cli + jobset | kicad-cli + stats | kicad-cli or RunBoardJobExport* | Full JLCPCB bundle in ~3 s on 10 |
| Render | svg/pdf → pypdfium2/resvg | same | `pcb render` + svg | same | same | Headless vision loop; no AGPL rasteriser in-process |
| Live in-editor edits | — | — | IPC (PCB) | IPC (PCB; pcbnew standalone because of #24966) | IPC (both editors) | Optional; undoable commits while the human watches |

Deliberately unused: `run_action` for interactive tools (router, move); SWIG action-plugin servers; `ParseAndCreateItemsFromString` (stub in 10.0); IPC footprint placement by LIB_ID (empty footprint); in-place `sch upgrade`; KiCad's legacy autoplacer except as an optional live seed.

---

## 5. In-KiCad plugin ↔ VS Code channel, and the source-build decision

### 5.1 Plugin channel — ADOPT, thin form only

An IPC plugin is an external process that KiCad launches with `KICAD_API_SOCKET` and `KICAD_API_TOKEN` set; it has **exactly** the API surface any client already has. It therefore cannot be a capability source: no schematic API on 9/10, no DRC/ERC/plot/netlist-import RPCs, no PNS router, no event push (selection must be polled; cross-probe arrives in 11).

What it uniquely buys:
1. **Instance discovery — weaker than it first looked.** The API socket is one fixed named pipe (`\\.\pipe\<GetTempPath>\kicad\api.sock`): no file appears, the *server* ignores `KICAD_API_SOCKET` (only clients honour it), and a second GUI instance silently fails to bind **[verified]**. KiCad hands the plugin that same fixed path, so a Connect click in the losing instance would hand over a pipe owned by a *different* KiCad. Session identity therefore comes from a probe (GetOpenDocuments + board filename + `GetNamedPipeServerProcessId` cross-checked against the PID the plugin reported), and real isolation comes from launching the sandboxed KiCad with its own **short** `TEMP`/`TMP` (nng rejects ipc paths longer than ~128 characters; VS Code globalStorage paths are too long). What Connect still uniquely buys is the token and the PID of the instance the user clicked in.
2. **A button in both editors — PCB-only in effect before 10.0.7.** Scope `schematic` renders a toolbar button in eeschema 9/10 despite the docs saying PCB-only **[verified on 10.0.0]**, but standalone eeschema registers no `API_HANDLER_COMMON` (Ping/GetVersion fail, so kipy cannot even connect) and all six editor RPCs crash it; hosted in `kicad.exe`, a schematic-dispatched request kills the whole process **[verified]**. On 9.x and 10.0.0–10.0.6 the eeschema button only opens VS Code, and kiwrap-core refuses every mutating RPC while `GetOpenDocuments(DOCTYPE_SCHEMATIC)` is non-empty unless the KiCad semver is ≥ 10.0.7.
3. **PCM-managed install/update** per KiCad major.

Design: `plugin.json` (api.v1, `runtime.type: exec`, one action scoped `["pcb","schematic"]`, `show-button: true`) whose entrypoint is a `.bat`/`.py` launcher (no compiled binary). It POSTs `{socket, token, pid, project, editor}` to kiwrap-core's loopback endpoint (127.0.0.1, random port, bearer token in a lock file protected by an explicit DACL — a 0600 mode bit means nothing on NTFS), optionally runs `code <project>`, logs to a file (KiCad drains child stdout only at exit), and exits within a second. It **terminates in kiwrap-core**, never in the VS Code extension, so terminal agents keep the live session. PCM `metadata.json` declares `runtime: "ipc"`, `kicad_version: "9.0"`. The exec launch and env hand-off are source-verified (`api_plugin_manager.cpp`) and used by third-party plugins; the toolbar button was observed live, the click was not (synthetic input was ignored). Phase 3 verifies the launch end-to-end.

Rejected:
- **SWIG action plugin hosting a server inside pcbnew.** pcbnew-only; board access from a non-main thread is unsafe; mutations outside `Run()` bypass undo, connectivity and redraw; documented freeze history; SWIG removed in 11. Konnect labels its own such bridge "not the KiCad 11 path".
- **C++ handler plugin.** Does not exist; handlers are registered by frames in C++. Code inside KiCad means a patched KiCad, a per-version fork that defeats "upgrade KiCad underneath".
- **Terminating the channel in the extension.** Would strand Claude Code CLI, Codex, Cursor and Copilot CLI.

### 5.2 Source build — DEFER as a gated developer diagnostic

Every stage of the flow ran on installed 7–10 binaries. KiCad-side debugging without a build is already proven: the eeschema `BeginCommit` crash was symbolicated with `cdb` and `symbols.kicad.org/kicad-stable` PDBs to `API_HANDLER_EDITOR::checkForBusy` (null `m_frame`, upstream #24966) **[verified]**; the `WriteDRCReport` failure on 10 was diagnosed from the wx assert text and `faulthandler` **[verified]**; `EnableAPILogging=1` in `kicad_advanced` writes every request/response to `logs/api.log` **[verified]**. KiCad 11 features (schematic API, headless `api-server`, export jobs) are prototyped on the **10.99 nightly installer**, not a build.

**Genuinely required only for:** (a) contributing handlers upstream (RunDRC / GetDrcMarkers do not exist even on master; ERC JSON ×100; #24966), (b) driving PNS as a batch engine (PCBWorld-Engine-style GPL fork — out of scope), (c) stepping with unoptimised locals when api.log + minidump cannot explain a blocker, (d) **stepping through KiCad's own netlist updater / DRC engine while we reimplement their semantics** (§4, §17) — the one case where a debug build is the honest tool. **Not required for:** anything on installed 7–10, writing IPC/PCM plugins, crash diagnosis, regenerating protos.

**Middle grounds before a build:** KiCad's `testing` channel ships 10.0-branch builds (the #24966 and `document`-field commits land in 10.0.7); a source *checkout* without a build supplies `.srcpath` for cdb; the 10.99 nightly installer covers KiCad 11 prototyping. When the build is wanted, the KiCad source lives in a fork of `KiCad/kicad-source-mirror` created with `gh repo fork`, cloned outside this repo.

**Recipe (written in Phase 0 as `docs/dev/kicad-source-build.md`, executed only when a trigger fires):** update VS 2022 17.9 → 17.14.x so vcpkg binary-cache hashes match CI's MSVC 14.44; vcpkg at a drive root at kicad-win-builder `build.ps1`'s pinned commit; `kicad-gitlab` NuGet source; swigwin 4.3.1; `msvc-win64-debug` preset; `KICAD_RUN_FROM_BUILD_DIR=1`. Budget: ~1 working day to the first debug build (10 min–5 h dependencies depending on cache hits), 40–80 GB; minutes incremental afterwards. Policy: nothing shipped may require a patched KiCad; conformance runs against stock installers only.

---

## 6. Version isolation and upgrade strategy

**Layer 1 — `KiCadInstallation`:** `{root, kicad-cli, python.exe + ABI tag + DLL dir, share/{symbols,footprints,3dmodels,template,schemas}, semver (kicad-cli version --format plain), commit}`. Discovery on Windows: registry Uninstall keys (`DisplayName 'KiCad X.Y'`; 7 under WOW6432Node) ∪ `C:\Program Files\KiCad\*\bin\kicad-cli.exe` ∪ PATH ∪ explicit setting. Discovery on Linux (**decided in, not yet verified** — Phase 0 spike): `kicad-cli` on PATH, distro packages (`/usr/bin/kicad-cli`; SWIG `pcbnew` lives in the system `python3` site-packages rather than a bundled interpreter, so the driver runs under `/usr/bin/python3` with the matching ABI), Flatpak `org.kicad.KiCad` (`flatpak run --command=kicad-cli org.kicad.KiCad`), Snap and AppImage; the IPC socket is a unix socket under `$XDG_RUNTIME_DIR`/`/tmp/kicad/api.sock`; config root `$XDG_CONFIG_HOME/kicad/<major.minor>`; GUI tests run under Xvfb. A platform seam (`kiwrap.platform`) isolates paths, process management and socket transport.

**Layer 2 — capability record keyed on FULL semver**, seeded from the verified matrix, re-probed on first use, cached per root+semver: `cli.subcommands`, `cli.flags[subcommand]` (parsed from `--help`, exit status ignored on 7/8), `has_erc_cli/has_drc_cli (≥8)`, `has_render/has_jobset (≥9)`, `has_sch_pcb_upgrade/drc_refill_flags (≥10)`, `sym_fp_upgrade (all)`, `has_ipc (≥9)`, `ipc_headless/ipc_schematic (≥11)`, `ipc_since[command]`, `swig_available (7–10)` + shim id, `gerbers_creates_dir (≥9)`, `writes_prl (≥10)`, `ships_wrl (≤9)`, `erc_json_pos_scale (100 on 8.0.1–10.0.0)`, `ipc_commit_needs_document (≥10.0.7)`, `default_min_track_width (0.2 mm on 10; 0 on 9)`, `sch_stamp_gate_needs_generator_version (8–10)`, epochs written. Tools consult flags only; a missing capability returns `E_UNSUPPORTED {oldest_kicad_with_it, fallback}`.

**Layer 3 — format epochs.** Tolerant lenses accept bare `hide`/`(hide yes)`, `tstamp`/`uuid`, `fp_text`/`property`, numeric or named nets, both layer-ID tables, 2- or 4-word `layerselection`, `true/false` or `yes/no`. Strict serializers emit exactly one epoch's token set and stamp (sch 20230121/20231120/20250114/20260306; pcb/mod 20221018/20240108/20241229/20260206; sym 20220914/20231120/20241209/20251024; pro meta 1/3). **Emit policy:** a project pins `target_kicad` (default: the oldest KiCad the user wants to open it with); never emit an uncertified epoch; boards written by SWIG on 7–10 carry that install's epoch inherently; `kw_project upgrade` runs kicad-cli 10 `sch/pcb/sym/fp upgrade` on a copy, semantic-diffs and re-runs ERC/DRC before replacing. Unknown tokens survive edits through the CST.

**Layer 4 — planner.** Each operation declares ordered providers with predicates, e.g. `zones.fill: [Ipc(live_bound), Cli(flag('pcb drc','--refill-zones')), Swig(ZONE_FILLER)]`, `drc.run: [Cli(has_drc), Swig(WriteDRCReport_ok)]`. Receipts carry `provider_used` and `degraded[]` so the agent learns "ERC unavailable on KiCad 7" from the result, not from a skill.

**Process and environment isolation.** SWIG runs only in a throw-away subprocess of its own install's `python.exe` (a same-minor system Python can import it with `os.add_dll_directory`, useful for developers, but the shipped policy is subprocess-per-install to avoid ABI drift: 3.9 on 7, 3.11 on 8–10, `IO_MGR`→`PCB_IO_MGR` in 8, layer renumbering in 9, `VIATYPE_*` gone in 10). Every KiCad process runs with `KICAD_CONFIG_HOME=<storage>/kicad-config` (KiCad appends `<major.minor>/`), seeded per version **inside the `<major.minor>/` subfolder** (files at the root of `KICAD_CONFIG_HOME` are ignored and the Setup wizard appears) with `kicad_common.json` (`api.enable_server`, **both** `do_not_show_again.update_check_prompt` and `.data_collection_prompt`, `system.first_run_shown`) plus all three lib tables (10.0 shows the wizard if any is missing or unparsable; 9.0.5 auto-creates the design-block table). Seed algorithm: start from `<install>/share/kicad/template/*-lib-table`; merge read-only rows from the user's `%APPDATA%\kicad\<major.minor>` tables when present (policy question in §15) so ERC/DRC/FootprintLoad see the user's custom libraries; resolve `${KICAD*_*_DIR}` and user variables from the user's `kicad_common.json` `environment.vars`; always create the `.kicad_pro` before relying on project-local tables (they are ignored without it). Each config root is pre-warmed once with a trivial kicad-cli command (8.0.1/9.0.5 segfaulted on the first ERC against a brand-new root), kicad-cli runs are serialised per config root (kicad-cli rewrites `kicad_common.json` on exit), and the seed is restored after each run; `KICAD_DOCUMENTS_HOME` and `KICAD_CACHE_HOME` under storage (also side-steps Controlled Folder Access). Output directories are always created; `.kicad_prl` is excluded from snapshots; `.wrl` references are rewritten to `.step` on 10+. `%APPDATA%\kicad` is never touched and CI asserts its hash is unchanged.

**IPC coupling.** kipy 0.8.0 (built against 10.0.6 protos) for 9.0.x and 10.0.0–10.0.6, using only `KiCad`/`board`/`project` (verified against 9.0.5 and 10.0.0; `kipy.schematic`/`board_jobs` are broken in that wheel). KiCad 10.0.7 (commit f3105844) adds a `document` field to BeginCommit/EndCommit and answers `AS_BAD_REQUEST` to document-less commits when more than one editor is open, which 0.8.0 cannot send: the 10.0 backend moves to kipy ≥ 0.9 or a vendored-proto client (same mechanism as for 11) behind a T5 probe for that field. For 11 a separate thin client is generated from the tag's vendored `api/proto` with a `Since` table parsed from `// Since:` comments; regenerated `_pb2` are never mixed with kipy wrappers. Commands are probed at runtime (`no handler available` → capability false).

**KiCad 11 seams (abstract now, swap later):** `session.start` (GUI now / `kicad-cli api-server --socket`), `document.open` (CLI argument / `OpenDocument`), `netlist.sync` (SWIG / `ImportNetlist`), `schematic.write` (file / IPC), `export` (kicad-cli / `RunBoardJobExport*`), `specctra` (SWIG / own codec).

**Certification (`kiwrap certify`).** An install is *supported* only when T1–T9 pass: T1 CST round-trip (token multiset, ERC set, netlist equal); T2 epoch certification on every install ≥ epoch; T3 negative gates (future stamp / unknown token rejected as KiCad rejects them); T4 `--help` tree snapshot; T5 IPC handler snapshot; T6 quirk probes (ERC ×100, gerbers outdir, `.prl`, help exit code, `.wrl`, wizard); T7 SWIG worker (symbols, shims, 20 consecutive saves, DSN/SES) — as built: `t7_ipc_writes.py` (headless IPC writes) and `t7_specctra_codec.py` (the codec vs KiCad's own DSN export / SES import, Freerouting round trips judged by kicad-cli DRC); T8 E2E golden flow — as built: `t8_board_writer.py` / `t8_sch_writer.py` (the file writers vs KiCad's re-save, parity DRC, pos export); T9 drift canaries (`sch/pcb upgrade` on a copy + semantic diff, `pcbnew.py` def/class diff, nightly 11 run). Budget: ≤1 day per point release, ≤1 week per major. Lint rule: nothing outside `kiwrap.backends` mentions a KiCad version number.

**SWIG-free twins.** The file-based board emitter (nets + `.kicad_mod` definitions resolved through project + global `fp-lib-table`, following nested `Table` entries on 10) and the DSN writer / SES reader are built in Phase 4 and certified against SWIG as oracle on 9/10, so KiCad 11 is not the first time they run.

---

## 7. Schematic strategy

File-based on 7–10 and by default on 11; the IPC schematic provider backs the same `kw_sch_*` tools only when an 11 live session is bound.

**Writer rules (all verified with kicad-cli):**
- Embed every used symbol in `lib_symbols` by copying the target install's `.kicad_sym` block and rewriting the top-level name to `<Nickname>:<Name>` (`Device:R`, `power:GND`) — KiCad links placed symbols to `lib_symbols` by the full lib_id string, and a literal verbatim copy parses but leaves every pin unconnected (3 errors + garbage netlist in the test); keep sub-units (`R_1_1`) unprefixed; **flatten `extends` chains exactly as KiCad does** (parent body + sub-units renamed to the child name, child properties overriding; an unflattened derived symbol is pinless and reports `lib_symbol_mismatch`; a missing entry silently drops the component from the netlist).
- Pin positions from the verified transform: rot0 `(X+lx, Y−ly)`, rot90 `(X−ly, Y−lx)`, rot180 `(X−lx, Y+ly)`, rot270 `(X+ly, Y+lx)`; mirror x negates the Y offset, mirror y negates the X offset. `kw_sch_add_symbol` returns absolute pin endpoints so the agent never computes geometry.
- Pins connect only at wire **endpoints**; labels connect anywhere on a segment → wires split at every pin and PWR_FLAG, all coordinates snapped to 1.27 mm, PWR_FLAG on every power-input net, NC markers on unused pins.
- References written to both the `Reference` property and the `instances` block (`/ROOT_UUID[/SHEET_UUID]`; the instances block wins; a symbol without one yields a wrong netlist plus a spurious `wire_dangling`); `sheet_instances` only in the root; one hierarchical sheet per functional block with sheet pins + hierarchical labels.
- **Not yet verified:** hierarchical sheets, repeated sheet instances, multi-unit symbols, rotated/mirrored symbols combined with hierarchy, and buses — every ERC/netlist test so far was flat, single-unit, rotation 0. Phase 1 opens with a spike that generates a 3-sheet schematic with one sheet instantiated twice and a multi-unit op-amp and certifies it with ERC + netlist + `sch upgrade` re-save diff. Buses are out of v1 scope unless the user says otherwise (§15).
- Dialect tables: v7 bare `hide`/unquoted; v8 `(hide yes)` in effects + `exclude_from_sim`; v9 `(pin_numbers (hide yes))` + `embedded_fonts`; v10 property-level `hide/show_name/do_not_autoplace`, `body_style`, `in_pos_files`, `duplicate_pin_numbers_are_jumpers`, `(power global|local)`, Datasheet `""`.

**Validation (`kw_sch_check`, after every write):** `kicad-cli sch erc --format json --severity-all --exit-code-violations` (positions ×100 in both the coordinates and the `description` strings; `--severity-all` makes kicad-cli exit 5 on warnings, so `ok` means **zero errors** and warnings are reported, never gated), `sch export netlist` parsed with a real s-expression parser (10 prints one token per line and adds `(groups)`/`(variants)`), and the **intent diff** — the check that catches "label on the wrong pin", which ERC cannot see. ERC type names are looked up per version (`label_dangling` on 8 vs `global_label_dangling` + `unconnected_wire_endpoint` on 10). Library resolution is part of the contract: with an unresolved sym/fp-lib-table every symbol reports `lib_symbol_issues`/`footprint_link_issues` warnings (11 in the test). Renders: `sch export svg --pages`/pdf → PNG ≤1024 px via pypdfium2/resvg with ERC markers.

**Existing user files** are edited via CST patches (unknown tokens preserved); refused while a `~*.lck` shows KiCad holds the file unless a live session is bound. `kicad-cli sch upgrade --force` runs only on copies. MVP order: dialect 20260306 (10) → 20250114 (9) → 20231120 (8) → 20230121 (7, best-effort).

---

## 8. Placement and routing strategy

**Placement loop.** Agent declares intent (edge connectors + side, zones by ref glob, keepouts, decap tethers ≤2 mm, locks) → `kw_pcb_place_auto` (KRT `place_seed` → `place_optimize`, ~1 s; 9/10 formats only, `E_UNSUPPORTED` on 7/8) or explicit coordinates via `kw_pcb_place` (SWIG, or IPC in one commit when live) → receipt with grade, courtyard overlaps, off-board parts and a top-2D render with ratsnest → iterate. No home-grown autoplacer; KiCad's legacy autoplacer (`run_action autoplaceOffboard`) only as an optional live seed.

**Routing loop (`kw_pcb_route_auto`, async job) — as built in Phase 1c (2026-09-20, `kiwrap.core.tools.pcb_route_auto`, `kiwrap.backends.router`, `kiwrap.formats.specctra`, `kiwrap.backends.cli.specctra`):** snapshot (the mutation prologue, gate `placed`) → **clean the dangling stubs** of the nets to route (unlocked track ends on no pad / item / zone of their net; Freerouting must connect every leftover fragment and gives up on nets a clean start routes) → **lock the copper to keep** (SES import deletes **all unlocked** tracks/vias — claim 7; `nets=[…]` locks the other nets' copper for the run, `lock_refs` the nets of those footprints; the flags kiwrap set are removed again afterwards) → **DSN export through the planner operation `routing.autoroute`, providers `cli` → `codec` → `swig`**: the patched `kicad-cli pcb export specctra` / `pcb import-ses` (fork patch `specctra-jobs`, keys `cli.has_specctra_export` / `cli.has_specctra_import`, `provider_used: cli:<v>+specctra-jobs`), kiwrap's own zero-dependency codec (`DsnWriter` / `SesReader` on the lossless CST — certified against KiCad's exporter/importer: 0 DSN differences at 5 µm vs SWIG `ExportSpecctraDSN` on ecc83 / blinker / kit-dev-coldfire 4L / One-Air-Max 4L / StickHub / interf_u / complex_hierarchy / pic_programmer and vs the patched CLI on 11, SES apply == KiCad's importer within 1e-3 mm, T7 `conformance/tests/t7_specctra_codec.py`; the only provider on stock KiCad 11, ranked ahead of SWIG on 10.x), then `pcbnew.ExportSpecctraDSN` / `ImportSpecctraSES` in the bundled python where a driver exists (none is shipped; the live tests bind a test-only oracle) → **the router's DSN hook** (`dsn_post.restrict_nets`: the nets not to route are REMOVED from the DSN's `(network …)` and their wiring made `(type fix)`, one path for every provider — Freerouting 2.4.1 headless ignores `-inc` / `router.ignore_net_classes` entirely, Phase 1c finding 9, verified on the reference board and the blinker: only the requested nets appear in the SES, the DRC after the apply is 0/0) → **Freerouting 2.4.1 under the managed private JRE 25 as a CLI process** (not its REST API: `java -jar … -de DSN -do SES --gui.enabled=false -l en -mp <passes> -mt 1 --router.optimizer.max_threads=1 --router.result_json`, per-run `--user_data_path` seeded with the telemetry-off profile — `usage_and_diagnostic_data.disable_analytics=true`, `allow_telemetry=false`, update checks off — log-line progress into the job, cancel via process-tree kill, `RouterTimeout` on the budget) with flags derived from the board rules (`rules_from_board`): **`--router.fanout.enabled=false` unless the board has BGAs** (the fanout pre-pass — not neckdown — produced the 0.15 mm segments that fail KiCad 10's 0.2 mm default `min_track_width`; claim 8), **`--router.copper_to_edge_clearance_um` always set from `min_copper_edge_clearance`** (KiCad's DSN omits it and Freerouting substitutes 500 µm), `-inc` for plane-only netclasses (kept for a Freerouting that honours it; 2.4.1 headless does not — the `nets=` subset is enforced by the DSN hook), the project's netclass minimums → **SES import with the same codec** (drill from the `Via[…]_<size>:<drill>_um` padstack name, placement ignored and counted) → restore the lock flags → **mandatory zone refill + classified kicad-cli DRC** (`pcb drc --refill-zones --save-board` on 10 and 11 — one path, no IPC `RefillZones`; `zones_stale` reported where a KiCad cannot) → receipt: `rules`, `dsn` summary, `router` (passes, history per pass, score, stop reason, Freerouting's own unrouted list), `routed` counts, `unrouted` = **KiCad's unconnected count, never the router's score** (Freerouting keeps counting pads a plane connects), `drc`, `provider_used`, `degraded[]`, `next_suggested: kw_pcb_fix` when `must_fix > 0` or a connection is open (a re-run keeps and only optimises the existing unlocked copper — same plateau; `from_scratch=true` deletes it first). A failed run after a board write (stub clean-up, kw_pcb_fix's rip-up) re-hashes the board at `placed` so the retry is not an `external_edit`, and its error names the copper that is gone plus the snapshot holding it; `kw_job cancel` ends the job `cancelled` (E_CANCELLED in the snapshot message). The per-project lock is kept alive by a heartbeat while the call runs, so a 6 h router job is never broken by a second mutating call; the JVM is bound to the server's lifetime (Job Object / PDEATHSIG) and the job pool is cancelled at server exit, so no router outlives a killed server; a run whose session was saved when the budget ran out is kept. **Pour order is load-bearing:** the B.Cu GND plane goes in *before* routing (the router works against it, `-inc` its class), the F.Cu pour *after* (a both-sides pour first makes the router think every F.Cu GND pad is connected; no pour first turns GND into track connections the router stalls on). DRC after routing is the judge, not a formality: Freerouting 2.3–2.4.1 can emit genuine router-introduced overlaps (#858, fixed for 2.5.0) and stops at a score plateau on partially routed boards, so "0/0" is an observed outcome per board (ecc83 stripped, blinker, blinker 4L, the ESP32-S3 reference board on 10.0.6 and 10.99), never an assumption; the designed closure for a plateau leftover is `kw_pcb_fix`. Known traps handled: Freerouting ignores copper-layer text; stale fills produce phantom clearance errors; `-drc` mode writes to cwd; KiCad master's DSN writes unlocked copper as `(type protect)` and copper zones as `(wire (polygon …) (type protect) (window …))` (10.0: `(type route)` / `(plane …)`) — `compare_dsn` normalises both, the codec keeps the 10.0 form because Freerouting routes both identically (byte-identical SES for the codec DSN and KiCad's DSN of the same board).

**Second engine.** KRT `route.py` (no Java; more vias; writes DRC constraints into `.kicad_pro`, so KiCad DRC always follows) — not wired in Phase 1c. **Touch-ups:** `kw_pcb_route` offers PCBWorld-style primitives (track polyline / via / unroute) with DRC deltas of touched nets. **Repair (`kw_pcb_fix`, built):** DRC → the nets named in must_fix violations (`[NET]` tags, unconnected_items included) → rip up their unlocked copper → clean zero-length / net-less leftovers → re-route only those nets through `routing.fix` (same provider chain, the other nets locked and in the ignore class) → refill + DRC, until `must_fix == 0` or `max_iterations`; violations naming no net are `unfixable`. Courtyard nudging is not implemented.

**PNS router: out of scope.** Not in SWIG or IPC; `run_action('pcbnew.InteractiveRouter.Autoroute')` starts the interactive tool from a selection and then every request answers `AS_BUSY` until a human finishes; only a patched fork (PCBWorld-Engine, GPL) exposes it.

---

## 9. Agent surface (MCP + VS Code)

*Refreshed 2026-09-20 (Phase 2): the Phase-0 plan text is replaced by what exists; the extension's
architecture is in [EXTENSION.md](EXTENSION.md), the measured display work in [DISPLAY.md](DISPLAY.md).*

**Protocol.** `mcp` 2.2 `MCPServer`, stdio by default (`uv run kiwrap serve`); `--http` streamable with `stateless_http=True`, bound to 127.0.0.1 behind a bearer token (`--token-file` / `KIWRAP_HTTP_TOKEN`) and an Origin check on top of the SDK's DNS-rebinding defence **[implemented, `kiwrap.core.cli_main`]** — kept for tooling, unused by the extension. Stateless: the project path is the handle; live GUI sessions are explicit handles from `kw_live`. Deterministic `tools/list`; every result is a `Receipt` (`kiwrap.core.state`) with `provider` and degradations named; failures are `KiwrapError`s with a stable code and a `fix`; positions in millimetres; images ≤ 1024 px; jobs over ~20 s return a `job_id` (`kw_job`).

**Catalog.** 33 tools (§10; `kw_<stage>_<verb>`, enums instead of one tool per subcommand, `kw_help` for depth): environment/project (`kw_env`, `kw_project`, `kw_snapshot`, `kw_job`, `kw_help`, `kw_intent`), libraries (`kw_lib_search`, `kw_lib_info`), schematic authoring and reads, board authoring and reads, checks (`kw_sch_check`, `kw_pcb_check`, `kw_pcb_drc`), fabrication (`kw_fab_bom`, `kw_fab_export`), renders (`kw_sch_render`, `kw_pcb_render`) and, new in Phase 2, **`kw_live`** (`launch | attach | status | screenshot | detach | raise`): a sandboxed GUI KiCad on the project with its API enabled (`kiwrap.sandbox.ensure(..., enable_api_server=True)`, no window activation, short `TEMP`, PID tracked, killed on detach/server exit), one per project; while it lives the `ipc11` provider binds to its socket instead of a headless `api-server`, so an agent's writes land in the window the user sees. `screenshot` is "what the user sees" (≤ 1024 px PNG through the display helper when one is attached, else the render fallback) — secondary to structured data (D17).

**Registration.** Claude Code inside VS Code is the only certified client (D13, D18). `<repo>/.mcp.json` carries `uv run --project … kiwrap serve`; the VS Code extension's **Configure Claude Code** command writes the same entry (plus `KIWRAP_HOME` when a storage home is configured) into any workspace, a managed block into `CLAUDE.md`/`AGENTS.md`, and a copy of the `kiwrap-flow` skill into `.claude/skills/`. Generated configs for Cursor/Windsurf/Codex/Copilot were dropped (D18).

**VS Code extension** (`extension/`, MIT, TypeScript, esbuild bundle, `engines.vscode ^1.101.0`; built into a private `.vsix` by `scripts/build_extension.ps1`, never published — D19). It is an MCP client of its **own** `kiwrap serve` instance (stdio, `@modelcontextprotocol/sdk`); the agent's server is a separate process, and the two share the storage home, the sandbox roots and the per-project mutation lock `<project>/.kiwrap/lock` (EXTENSION.md §6). Surfaces: **Live KiCad** custom editors for `*.kicad_pcb` / `*.kicad_sch` (`priority: option`, "Open With… → KiCad (live)") and a panel for the 3D viewer, fed by the Rust helper `helper/kiwrap-live.exe` (MIT; `Windows.Graphics.Capture` per window, surfaces keyed on HWND + owner chain with dialogs composited, `SetWinEventHook` tracking, loopback WebSocket with a per-launch token, JPEG/WebP frames, 0 bytes idle) — the **live view** (D20): a stock KiCad launched sandboxed and never activated streams into the tab, an agent's edit appears there within a second, and the person edits by hand in the real window (*Raise KiCad*: button, double-click, command); nothing is injected and VS Code's keyboard behaviour is untouched. The injecting modes measured in DISPLAY.md §12 (hybrid mirror, cloaked `mr/display-input`) stay in the code behind the experimental, off-by-default `kiwrap.live.inputMode`, which alone arms the generated keybinding shield (EXTENSION.md §5, §7); the same custom editor shows the `kw_*_render` preview when no session exists; a `DiagnosticCollection` fed by `kw_sch_check` / `kw_pcb_drc` (positions already corrected by the server); a **Select KiCad** picker over `kw_env`; a status bar item / project view (install, stage, session). Not built: Copilot `languageModelTools` facades, KiCanvas, TextMate grammars (deferred, D18 narrows the audience to Claude Code).

**Skills and prompts.** `.claude/skills/kiwrap-flow/SKILL.md` (the stage-gated flow, the data-first feedback protocol, how to read receipts and teaching errors) plus `AGENTS.md` / `CLAUDE.md` as the always-on summary. The further skills listed in the Phase-0 plan (`erc-triage`, `drc-triage`, `placement`, `routing`, `reference-circuits`, …) are folded into `kiwrap-flow` and `kw_help` for now. **Evals** are graded only by KiCad outputs (ERC = 0, intent diff empty, unrouted = 0, must_fix = 0, fab bundle present) and run manually (D15); the certification suite T1–T9 and the end-to-end tests (`tests/test_e2e_reference_board.py`) run without any agent.

---

## 10. Tool catalog (36)

| Tool | Stage | Args (abridged) | Returns | Backend |
|---|---|---|---|---|
| `kw_env` | setup | `refresh?` | installs, capabilities, quirks, engines, live | cli discovery + `--help`, ipc ping |
| `kw_project` | setup | `action: create/status/upgrade, dir, name, target_kicad?, to_kicad?, dry_run?` | paths, epochs, stage, next, semantic diff | formats + git; kicad-cli upgrade (10) on copy |
| `kw_snapshot` | setup | `project, action: create/restore/diff/list` | sha / files / semantic diff | git + IR |
| `kw_job` | setup | `job_id, action?, wait_s?` | state, progress, result | job queue |
| `kw_help` | setup | `topic` | markdown | docs + flag map |
| `kw_intent` | requirements | `project, action: set/get/render, intent?` | intent, validation errors, png | core |
| `kw_lib_search` | parts | `kind, query, filters?, limit?` | matches + near-misses | FTS index |
| `kw_lib_info` | parts | `id, unit?, image?` | pins/pads geometry, bbox, preview | formats / SWIG / cli svg |
| `kw_parts_search` | parts | `query, basic_only?, package?` | LCSC rows, price, stock | jlcparts DB + EasyEDA |
| `kw_parts_import` | parts | `project, lcsc` | lib_id, fpid, model | easyeda2kicad (subprocess) + cli upgrade; parts land in `${KIPRJMOD}/libs` + project tables |
| `kw_lib_create` | parts | `project, kind: symbol/footprint, spec{pins/pads…}` | lib_id/fpid, preview | symbol from pin table; footprint from parametric (IPC-7351-style) generators or KiCad's footprint wizards via the SWIG driver |
| `kw_datasheet` | parts | `source, pages?/query?` | text / matches (data-only framing, size-capped) | HTTP + pypdfium2 |
| `kw_sch_add_symbol` | schematic | `project, sheet, lib_id, ref, value, at, rotation?, mirror?, fields?` | uuid, absolute pins, receipt | formats (7–10) / IPC (11) |
| `kw_sch_connect` | schematic | `project, sheet, net, endpoints[], style?` | items, junctions, receipt | formats / IPC (11) |
| `kw_sch_add_item` | schematic | `project, sheet, item{type,…}` | uuid, receipt | formats |
| `kw_sch_edit` | schematic | `project, sheet, ops[], if_match?` | applied/skipped, receipt | formats CST patch |
| `kw_sch_annotate` | schematic | `project, scheme?, start_per_sheet?` | mapping, receipt | formats |
| `kw_sch_assign_footprints` | schematic | `project, map?/rules?` | assigned, unresolved + hints | formats + index |
| `kw_sch_get` | schematic | `project, sheet?, refs?, bbox?, include?, detail?, cursor?` | chunked JSON | formats / cli netlist / IPC (11) |
| `kw_sch_render` | schematic | `project, sheet?, region_mm?, overlay?` | image | cli svg/pdf + PyMuPDF |
| `kw_sch_check` | schematic | `project, severity?` | ok, ERC (×100 fixed), intent diff | cli erc + netlist |
| `kw_pcb_setup` | board | `project, outline?, layers?, rules?, netclasses?, preset?` | applied rules | formats + SWIG |
| `kw_pcb_sync` | board | `project, remove_orphans?, force?` | added/updated/removed, parity | SWIG / IPC (11) + cli drc parity |
| `kw_pcb_get` | board | `project, refs?, bbox?, include?, detail?, cursor?` | chunked board JSON | SWIG / IPC |
| `kw_pcb_place` | placement | `project, placements[], relative_to?` | overlaps, off_board, image | SWIG / IPC |
| `kw_pcb_place_auto` | placement | `project, intent{…}, optimize?` | placements, grade, image | KRT (9/10) |
| `kw_pcb_render` | board | `project, view?, layers?, region_mm?, overlay?` | image | cli render / svg |
| `kw_pcb_route_auto` | routing | `project, nets?, passes?, timeout_s?, lock_refs?, clean_dangling?, engine?, provider? auto\|cli\|codec\|swig, run_async?, force?` | job_id, or routed counts, unrouted (KiCad's), router stats, DRC classified, provider_used | router (Freerouting, managed JRE) + planner `routing.autoroute`: patched cli → codec → swig; kicad-cli refill + DRC |
| `kw_pcb_route` | routing | `project, ops[]` | uuids, drc_delta | file / IPC |
| `kw_pcb_zones` | routing | `project, zones?, fill?, stitch_vias?` | uuids, filled area | SWIG / cli / IPC |
| `kw_pcb_drc` | drc | `project, schematic_parity?, refill_zones?` | ok, classified violations | cli drc (SWIG on 7) |
| `kw_pcb_fix` | drc | `project, actions[]?, max_iterations?, passes?, timeout_s?, provider?, run_async?` | must_fix before/after, iterations[], nets_touched, unfixable[] | planner `routing.fix` (same chain) + kicad-cli refill + DRC |
| `kw_fab_export` | fab | `project, targets?, profile?, force?, async?` | files, warnings, unsupported | cli / jobset / IPC (11) |
| `kw_fab_bom` | fab | `project, fields?, group_by?` | rows, cost, stats | cli bom/stats + parts DB |
| `kw_report` | review | `project, out?` | markdown + images | composite |
| `kw_live` | live | `project, action: status/attach/launch/detach/push/pull/highlight/select/undo/run_action` | session handle | IPC + supervisor |

---

## 11. Feedback loops and guardrails

**Receipt on every mutating tool:**
```json
{"changed": {"added": ["R3"], "updated": [], "removed": []},
 "validators": {"erc": {"before": 2, "after": 0}, "intent_diff": {"missing": 0, "extra": 0}},
 "provider_used": "swig:10.0.0", "degraded": [],
 "snapshot": "a1b2c3d", "stage": "erc_clean", "next_suggested": "kw_pcb_sync",
 "image": "<ImageContent only when geometry changed>"}
```

**Teaching errors** (`isError: true`): `{code, message, why, fix, candidates[], example}`; `E_UNSUPPORTED` names the oldest KiCad with the capability and the fallback used; every code has a `kw_help` page.

**Stage gates (server-enforced; `force: {reason}` overrides and is logged into the snapshot message):** `kw_pcb_sync` requires last `kw_sch_check.ok`; `kw_pcb_route_auto` requires stage `placed` (zero copper off-board, zero courtyard overlaps); `kw_fab_export` requires `must_fix_count == 0`. `ok` = zero *errors*; warnings are listed and never gate. Must-fix = clearance, shorting_items, tracks_crossing, unconnected_items, hole_clearance, net_conflict, track_width; review = courtyards_overlap, solder_mask_bridge, lib_footprint_issues; cosmetic = silk_*. The must-fix/review/cosmetic tables live in the capability record per version. `kw_pcb_setup` also writes `.kicad_dru` custom rules and netclass→net assignments in `.kicad_pro`, and the corpus includes a 4-layer board (all routing tests so far were 2-layer).

**Idempotency and safety:** keyed upserts (same `ref` updates), `if_match` content hashes, **content hashes stored with every stage result — each tool re-hashes its inputs and downgrades the stage with `degraded: ['external_edit']` when an agent edited a `.kicad_*` file directly** (the skill says never to, and `kw_sch_edit` exposes a raw CST patch so it never has to), per-project mutation lock, a snapshot per mutation in the private `.kiwrap/snapshots.git` store (plus a pre-mutation snapshot when the design files differ from the last one, i.e. after an external edit), `kw_snapshot restore` as universal undo, editor-lock refusal (`~*.lck`), never edit on disk a board seen live, IPC commits = one undo step each, `%APPDATA%/kicad` untouched (CI assertion).

**Context budget:** chunked reads (`detail`, `cursor`, bbox/refs filters), ≤1024 px renders with region crops, `resource_link` for large artifacts, async jobs.

**Security model:** stdio by default; HTTP only on 127.0.0.1 with a bearer token and origin policy; SWIG-driver operations are a closed enum with JSON arguments — no Python or shell text from the agent is ever executed; Freerouting's REST server is bound to 127.0.0.1 on a random port; datasheet/parts text fetched from the web is returned in data-only framing with size caps (prompt-injection surface); `fp-lib-table`/`sym-lib-table` URIs and `.kicad_pro` environment variables inside a project are validated to resolve under the project, the KiCad install or the storage roots before any kicad-cli/SWIG process sees them.

**Agent protocol (skill):** after every mutating step — snapshot → stage validator → render and look → fix from violation JSON (uuid + pos) → re-validate; never route with open ERC errors; never export with must-fix DRC; never hand-route full nets; never guess library ids (search first); never claim success without the validator JSON and an image.

---

## 12. Repo layout

```
kiwrap/  (MIT)
  README.md  AGENTS.md  CLAUDE.md  .mcp.json  .vscode/mcp.json  .cursor/mcp.json
  .claude/skills/<12 skills>/SKILL.md
  pyproject.toml (uv workspace)
  packages/
    kiwrap-core/        src/kiwrap/{server.py, cli_main.py, tools/, state/(stages, gates, locks, snapshots, receipts, errors, jobs),
                       planner.py, capability.py, installation.py, sandbox.py, data/kicad_version_matrix.json}
    kiwrap-formats/     src/kiwrap/formats/{sexpr/(cst, writer, patch), ir/, epochs/, geometry/, libs/, reports/, interchange/(dsn_writer, ses_reader)}
    kiwrap-backends/    src/kiwrap/backends/{cli/, swig/, ipc/(session, supervisor, kipy_client, since_table, proto11/), router/(freerouting, jre, krt, dsn_post)}
    kiwrap-swig-driver/ kiwrap_swigdrv/{main.py, shim_v7.py, shim_v8plus.py, wx_noassert.py, ops/, ses_fallback.py}
    kiwrap-vscode/      package.json, src/(extension, mcpProvider, configureAgents, preview/, diagnostics, tasks, liveSession, installPicker, lmTools)
    kiwrap-connect/   plugin.json, connect.bat, connect.py, icons/, pcm/metadata.json
  conformance/         corpus/{7.0,8.0,9.0,10.0,11-nightly}/, tests/t1..t9, snapshots/{cli-help,ipc-handlers,pcbnew-api}/, certify.py
  evals/               tasks/(01_led_blinker, 02_mcu_breakout), graders/, runners/(claude_code, copilot_cli, codex), report.py
  tests/               unit/ integration/ e2e/
  docs/                ARCHITECTURE.md, capability-matrix.md (generated), tool-reference.md (generated), agent-configs.md,
                       dev/kicad-source-build.md, dev/debugging-kicad-with-symbols.md
  scripts/             bootstrap.ps1, seed_config_root.py, build_vsix.ps1, ci.ps1
```
Rules: `kiwrap-formats` has zero runtime dependencies; `kiwrap-swig-driver` is copied into KiCad's interpreter, never imported by kiwrap-core, and is **licensed GPL-3.0-or-later as an independent program** because it links KiCad's GPL `pcbnew` bindings; nothing outside `kiwrap-backends` knows a KiCad version number; GPL/AGPL engines (Freerouting, KiBot, **easyeda2kicad — AGPL**) run as separate processes only and are downloaded at bootstrap, never redistributed; **no AGPL library is imported in-process** (PyMuPDF is AGPL/commercial → pypdfium2 or resvg); no code copied from AGPL projects (Konnect, nevenfo). Whether the wrapper itself is MIT is an open question (§15).

---

## 13. Roadmap

| Phase | Weeks | Deliverables | Exit criteria |
|---|---|---|---|
| **0 Foundations** | 1–2 | Install discovery + capability record; seeded config roots; kiwrap-cli adapter with normalisations; CST + lenses + T1; MCP skeleton (`kw_env`, `kw_project status`, `kw_snapshot`, `kw_job`, `kw_help`, `kw_sch_check` ERC part, `kw_pcb_drc`, renders, `kw_fab_export`); receipts/errors; agent configs; skill v0; `bootstrap.ps1` (uv, JRE 25, Freerouting); source-build + symbol-debugging docs (build not started); CI design (cached installer downloads with hashes, self-hosted Windows runner for GUI/IPC tests, normalised Gerber/drill headers for goldens, WER `LocalDumps` registry key or `procdump -ma` fallback, nightly eval job with a cost cap); client-capability probe doc (image handling and tool caps per agent client, with sources); side-by-side install of the current point releases and the 10.99 nightly, then re-run the spike scripts (subject to §15) | `kw_env` lists 7.0.6/8.0.1/9.0.5/10.0.0 with correct flags; ERC/DRC/render on the corpus for 8/9/10 with corrected coordinates and ≤100 KB images; 7 returns `E_UNSUPPORTED` with hints; T1 0 token loss; Claude Code and Copilot list the tools; `%APPDATA%/kicad` hash unchanged |
| **1 MVP on KiCad 10** | 3–6 | Schematic IR + dialect 20260306, FTS index, `kw_intent`, `kw_lib_*`, all `kw_sch_*`; file-based initial-board emitter (pulled forward from Phase 4 — it is needed for the first board anyway); netlist-updater spec and implementation (ref matching, footprint swap preserving position/side/rotation, pad↔pin map from the netlist `(node …)` entries, net rename/merge, orphan removal, DNP/`exclude_from_board`, fp-lib-table nickname resolution incl. env vars, `${KIPRJMOD}` and nested `Table` rows on 10) on the SWIG driver with `kicad-cli pcb drc --schematic-parity` as oracle; hierarchy/multi-unit schematic spike; SWIG driver + shims (checklist in §14); `kw_pcb_setup/sync/get/place/zones/route`; kiwrap-router (Freerouting REST, rule-derived flags, lock-then-snapshot, refill); `kw_pcb_route_auto` async; JLCPCB profile, `kw_fab_bom`, `kw_report` v0; gates; reference board spec (ESP32-S3-WROOM-1 + USB-C 2.0 + MCP73831 + 3.3 V LDO + buttons + I²C header, ~50×30 mm, 2-layer) as eval task 2 | Claude Code builds the reference board from the spec on KiCad 10.0.x: 0 ERC, empty intent diff, 0 unconnected, 0 must-fix DRC, complete fab bundle — **3 consecutive runs, no human edits, no GUI open**, within budget |
| **2 Harden + certify 9/8** | 7–9 | Dialects 20250114 and 20231120; SWIG fill path on 7–9; `kw_project upgrade`; T2–T9 + `kiwrap certify` + CI matrix; KRT router #2 and `kw_pcb_place_auto`; `kw_pcb_fix`; parts tools; idempotency, locks, editor-lock; eval harness on `claude -p`/Copilot CLI/Codex; Cursor/Codex smoke tests | Reference board passes on 9.0.5 and 10.0.x with an identical contract; 8.0.1 passes schematic + fab stages; certify marks 8/9/10 supported; evals green; a synthetic point release is absorbed by data changes only |
| **3 VS Code + live + Connect** | 10–13 | VSIX (MCP provider, Configure agents, previews, Problems, picker, tasks, grammars, LM facades; Node LTS installed); kiwrap-ipc live sessions with kipy 0.8.0 (pcbnew standalone on 10.x), `kw_live`; KiWrap Connect PCM package; Live-KiCad panel; 10.99 nightly in the matrix (T5, schematic API probe) | VSIX gives Copilot and Claude Code identical tools zero-config; agent edits an open pcbnew 10 board through `kw_live` with one undo per call; Connect binds within 5 s in both editors on 9.0.5/10.0.x; nightly drift report produced |
| **4 KiCad 11 + SWIG-free twins + upstream** | 14+ | File board emitter and DSN/SES codec certified against SWIG (T2/T7 twins); IPC-11 backends behind existing seams (api-server, OpenDocument, ImportNetlist, PlaceFootprintFromLibrary, RunBoardJobExport*, schematic live provider) with vendored-proto client + Since table; gated source-build bench only on a named trigger; upstream MRs where justified; 7 declared best-effort; optional MCP Apps viewer | Reference board passes on the 11 nightly with no SWIG in the call path; same signatures pass on 9/10 with the twins; ≥1 upstream MR if the build was triggered; a new point release is absorbed within one day |
| **5 Sustained certification** | ongoing | Release-day playbook; quarterly pin review (kipy, Freerouting/JRE, KRT, mcp SDK); frozen tool contract | Point releases with no code change; majors within one week; no tool signature changes for a KiCad upgrade |

**Deferred list (owning phase):** KiCad 9/8 dialects (P2) · KRT (P2) · LCSC/datasheet (P2) · `kw_pcb_fix` (P2) · editor-lock (P2) · VS Code extension (P3) · live IPC + Connect (P3) · 10.99 probe (P3) · SWIG-free twins, KiCad 11 backends (P4) · source build (gated, P4) · KiCad 7 dialect + SWIG DRC (P4, best-effort) · KiCanvas/MCP Apps (optional) · PNS batch driving, cloud routers (rejected).

---

## 14. Risks

| Risk | Mitigation |
|---|---|
| SWIG removed in 11 while sync/DSN/fill on 7–10 depend on it | Provider isolation; file emitter + DSN/SES codec certified against SWIG in Phase 4 before 11 GA; IPC-11 backends behind the same seams; T7 twins |
| Headless SWIG crashes/hangs (SetOutline ownership, null DRC engine, wx assert dialogs, WriteDRCReport on 10, #24643 fill crashes on real boards) | Driver checklist **from the verification results**: boards come from `pcbnew.NewBoard(path)`/`LoadBoard` (never `BOARD()`/`CreateEmptyBoard()` — `ZONE_FILLER.Fill` access-violates on 7/8/9 because `m_DRCEngine` is null); keep the `SHAPE_POLY_SET` referenced when calling `SetOutline` (ownership transfer → use-after-free on 9/10, empty outline on 8); `SetFPID(LIB_ID(nick, name))` after `FootprintLoad` (else `lib_footprint_issues` on 8.0); validate Edge.Cuts before `ExportSpecctraDSN` (a malformed outline blocks on a modal `wxLogWarning` even headlessly); `PCB_VIA.GetWidth(layer)` on 9/10 (the no-arg form pops an assert dialog); `BuildConnectivity` before `Fill`, and fill is mandatory before DRC (kicad-cli does not refill); `SaveBoard(aSkipSettings=True)`; `IO_MGR`/`PCB_IO_MGR` shim; `wx.DisableAsserts()` if anything (`wxTheAssertHandler` is not exposed and did not prevent crashes); multiple boards per process are fine; faulthandler; a watchdog kills the job on timeout **or on any visible top-level window**; reload the board after any `False` from `ImportSpecctraSES` (it has already deleted the unlocked copper); WriteDRCReport only on 7; zone fill treated as best-effort with `drc --refill-zones --save-board` as the 10.x fallback |
| Serializer emits tokens older KiCad rejects, or silently drops parts | Strict epoch serializers, verbatim embedding + `extends` flattening, T2/T3/T9, ERC + netlist + intent gate, pinned `target_kicad` |
| IPC instability on 9/10 (GUI-only, one fixed pipe per machine, AS_BUSY, #24966 until 10.0.7, `document` field from 10.0.7) | Live sessions optional; sandboxed standalone `pcbnew.exe` with its own short TEMP; refuse mutating RPCs while a schematic editor is open on < 10.0.7; pipe-owner PID probe before every session; retries; crash supervisor tracking the PID (a WER auto-restart is a different process); kipy ≥ 0.9 / vendored protos for 10.0.7+ gated by T5 |
| Freerouting: Java 25, GPL, fanout pre-pass below KiCad 10's 0.2 mm default track width, 500 µm edge clearance substituted, telemetry on by default, router-introduced overlaps (#858 until 2.5.0), SES wipes unlocked copper, copper text, non-determinism | Managed JRE; separate process; fanout off unless BGA; edge clearance from the board rule; analytics/telemetry/update-check off in the managed profile; REST on 127.0.0.1 random port; `-mt 1`; lock-then-snapshot; mandatory refill + kicad-cli DRC; upgrade trigger at 2.5.0; KRT fallback |
| Agent self-judgement, hand-routing, guessed ids, context blow-up, client caps/timeouts | Gates, KiCad-computed receipts, teaching errors with candidates, 35 tools, chunked reads, small images, async jobs, skills, KiCad-graded evals |
| kicad-python 0.8.0 wheel inconsistencies / proto drift | Pin 0.8.0, board/editor/project only; runtime probing; separate vendored-proto client for 11, never mixed; T5 snapshots |
| Windows traps (Setup wizard, Controlled Folder Access, non-ASCII temp paths, `.kicad_prl`, GUIs rewriting settings) | Seeded config/documents/cache homes; three lib tables + prompt flags; Connect hand-off; gitignore `.prl`; CI asserts user config untouched |
| Fast-moving or stale third parties (KRT, KiCanvas, jlcparts, easyeda2kicad) | Pinned versions in dedicated venvs behind interfaces; DRC after KRT; kicad-cli SVG as guaranteed preview |
| Scope creep into a KiCad fork / premature source build | Policy: nothing may require a patched KiCad; build gated on named triggers; PDBs + cdb + api.log first; nightly for 11 |
| Linux certified from day one although every experiment so far ran on Windows; VS 2022 17.9 ≠ CI's MSVC 14.44 | Phase 0 Linux spike (distro/Flatpak discovery, system-python SWIG ABI, unix-socket IPC under Xvfb) on a GitHub-hosted runner before any Linux claim enters the capability record; Node LTS installed in Phase 0; update VS only if the build trigger fires |

---

## 15. Decisions (resolved 2026-09-19)

Asked interactively and answered by the user; the log with consequences is in [DECISIONS.md](DECISIONS.md). Recommended defaults were taken except where marked **(user override)**.

| # | Decision | Choice |
|---|---|---|
| 1 | Source build | ~~Recipe + fork in Phase 0; build only on a named trigger~~ **superseded by D17: planned in Phase 1 to extend kicad-cli/IPC upstream (data-first feedback)** |
| 2 | Connect / live sessions on 9.x–10.0.x | PCB-only; sandboxed standalone `pcbnew.exe`; eeschema button only opens VS Code; schematics always via files |
| 3 | Version floor | ~~10 first, 9 certified, 8 schematic + fab, 7 best-effort~~ **superseded by D16: KiCad 11 (nightly) first, 10.0.x co-primary, 9 later, 8/7 best-effort** |
| 4 | Installs | Install 10.0.6, latest 9.0.x/8.0.x and a 10.99 nightly side-by-side; re-run spikes |
| 5 | Reference board | ESP32-S3-WROOM-1 + USB-C 2.0 + MCP73831 + 3.3 V LDO + buttons + I²C header, 2-layer, JLCPCB parts |
| 6 | Live editing | Headless first; live IPC sessions + Connect in Phase 3 |
| 7 | Libraries | Sandbox seeds from stock templates and merges the user's global tables **read-only**; `%APPDATA%\kicad` is never written |
| 8 | Licensing | MIT core; `kiwrap-swig-driver` GPL-3.0-or-later as a separate program; GPL/AGPL engines as separate processes; no PyMuPDF; no cloud routers |
| 9 | Fabrication | JLCPCB default profile (rules, naming, `LCSC Part #`, rotation corrections) |
| 10 | Schematic scope | Hierarchy + multi-unit in v1; buses Phase 2 |
| 11 | Toolchain | Install Node LTS now; bootstrap downloads pinned JRE 25, Freerouting, jlcparts DB, EasyEDA data; telemetry off |
| 12 | Platforms | ~~Windows + Linux certified from Phase 0~~ **superseded by D18: Windows only** |
| 13 | Agent clients | **Claude Code inside VS Code only** (D18: VS Code is the only surface; other IDEs/CLIs out of scope) |
| 14 | CI | GitHub Actions for headless tests (Windows + Linux runners); this machine as a self-hosted runner for GUI/IPC certification |
| 15 | Eval budget | **Manual only (user override)** — no scheduled agent evals |

---

## 16. Verification results and corrections applied

Each load-bearing claim was attacked by two independent verifiers (one empirical on this machine, one documentary against KiCad source/docs). "Corrected" means the mechanism held but the sentence was wrong or incomplete; the corrected statement is what the body above now says. Full evidence: [VERIFICATION.md](VERIFICATION.md).

| # | Claim (as synthesised) | Result | What changed |
|---|---|---|---|
| 1 | On KiCad 8.0.1, 9.0.5 and 10.0.0, `kicad-cli sch erc --format json` reports item positions exactly 100 times smaller than the text report, while `kicad-cli pcb drc --format json` positions are correct in millimetres. | SURVIVED | Confirmed on 8.0.1/9.0.5/10.0.0 and in source: ERC JSON uses schIUScale, text report uses the units provider. Positions and `description` strings are rescaled by the cli adapter. |
| 2 | A .kicad_sch at version 20260306 with lib_symbols copied verbatim from KiCad 10's libraries (extends flattened), wires split at pin endpoints on the 1.27 mm grid and PWR_FLAGs on power-input nets passes kicad-cli 10 ERC with zero violations and exports the intended netlist. | CORRECTED | lib_symbols are not verbatim: top-level name must be `<Nickname>:<Name>`; `extends` flattened as KiCad does; `instances` block mandatory; libraries must resolve in the kicad-cli environment (else 11 warnings, exit 5). Only flat/single-unit/rotation-0 schematics verified (§7 spike). |
| 3 | Symbol pin sheet positions follow rot0 (X+lx, Y-ly), rot90 (X-ly, Y-lx), rot180 (X-lx, Y+ly), rot270 (X+ly, Y+lx), with mirror x negating the Y offset and mirror y negating the X offset, for all 12 orientation combinations. | SURVIVED | Confirmed for all 12 orientation combinations on 9.0.5 and 10.0.0 by two methods. |
| 4 | KiCad 10.0.0's schematic IPC handler answers only GetOpenDocuments; BeginCommit, CreateItems and HitTest crash eeschema.exe with a null m_frame in API_HANDLER_EDITOR::checkForBusy (upstream #24966). | CORRECTED | Under-inclusive: all six editor RPCs crash; the crash kills `kicad.exe` when hosted; intermittent even for board writes when both editors are open; also on 9.0.1; fixed for 10.0.7 (unreleased). |
| 5 | Neither kicad-cli 7-10 nor the 9/10 IPC API can run DRC/ERC, import a netlist, place a library footprint by LIB_ID or export Specctra DSN; on 7-10 only SWIG pcbnew does the latter three headlessly. | CORRECTED | The sentence wrongly lumped DRC/ERC in: kicad-cli 8/9/10 run DRC and ERC headlessly (7 does not). True part: no surface on 7–10 imports a netlist headlessly; SWIG has no netlist reader → kiwrap writes its own updater (§4, §13). |
| 6 | Each installed KiCad's bundled python.exe can headlessly build a board (FootprintLoad, NETINFO_ITEM, pad.SetNet, tracks/vias/zones, ZONE_FILLER after BuildConnectivity, SaveBoard) that kicad-cli DRC reports as 0 violations / 0 unconnected, provided one BOARD per process, outlines built via Outline() in place and wxTheAssertHandler nulled. | CORRECTED | Preconditions rewritten: `NewBoard`/`LoadBoard` (null DRC engine on 7/8/9 otherwise), keep `SHAPE_POLY_SET` alive, `SetFPID` after load, fill mandatory; `wxTheAssertHandler` not exposed; one-board-per-process unnecessary; 7.0 has no kicad-cli DRC (§14). |
| 7 | ExportSpecctraDSN(board, path) works headlessly on 7.0.6-10.0.0 and ImportSpecctraSES(board, path) on 8.0.1-10.0.0; SES import deletes all unlocked tracks/vias and applies SES placement. | SURVIVED | Confirmed; SES import deletes unlocked copper *before* it can fail, applies placement to locked footprints too → reload on `False`. |
| 8 | Freerouting 2.4.1 requires Java 25 and, with automatic_neckdown=false and fanout.enabled=false, routes a 30-net 2-layer board to 0 KiCad DRC violations / 0 unconnected, and kicad-cli 10 `pcb drc --refill-zones --save-board` removes phantom clearance errors caused by stale fills. | CORRECTED | `fanout.enabled=false` is the operative flag, neckdown irrelevant; edge clearance must be set; router can introduce overlaps (#858) → DRC always; 9.0.5 lacks `--refill-zones` (§8). |
| 9 | A file written in KiCad N's format loads on every newer kicad-cli and fails on every older one (exit 3/127), and a single unknown token makes the 7.0/8.0 parsers reject the file. | CORRECTED | Newer-loads-older holds. Rejection is not uniformly stamp-driven (pcb 7 only on parse error; sch 8–10 only inside `generator_version`); unknown tokens are skipped inside `(general)`/`(pcbplotparams)`/stackup; exit codes vary (§2, T3 in §6). |
| 10 | A KiCad 9/10 GUI launched with KICAD_CONFIG_HOME seeded with kicad_common.json (api.enable_server, prompt flags) and the three global lib tables starts without the Setup wizard and answers kicad-python 0.8.0 over ipc://%TEMP%\kicad\api.sock on both 9.0.5 and 10.0.0. | CORRECTED | Seed files go in `<KICAD_CONFIG_HOME>/<major.minor>/`; both prompt flags + all three tables required on 10; standalone eeschema has no common handler; one fixed pipe per machine, server ignores `KICAD_API_SOCKET`, nng path limit ~128 chars (§5.1, §6). |
| 11 | An mcp 2.2 Python MCPServer negotiates protocol 2026-07-28, serves legacy 2025-06-18 clients, and returns PNG ImageContent and structuredContent over stdio and streamable HTTP. | SURVIVED | Confirmed end-to-end with a real server shelling out to `kicad-cli pcb render`; 2026-07-28 is reached via the per-request envelope, not `initialize`. |
| 12 | symbols.kicad.org/kicad-stable serves PDBs matching the installed 10.0.0 binaries, so cdb symbolicates WER minidumps of KiCad crashes to source lines without a source build. | SURVIVED | Confirmed: exact GUID/age-matched private PDBs for 10.0.0 and 9.0.1; cdb symbolicates real WER dumps. |

Score: 5 of 12 sentences survived verbatim; every refuted sentence's underlying mechanism held, and the corrections are now in the body. The design's direction did not change; its details did.

---

## 17. Known gaps from the completeness review

Ordered by severity; each names the owning section/phase. These are inputs to Phase 0/1 planning, not reasons to change direction.

- **[blocker] Netlist-to-board sync (`kf_pcb_sync`) is attributed to the SWIG driver, but verification shows SWIG 7-10 exposes NO netlist reader/updater (no NETLIST_READER / BOARD_NETLIST_UPDATER), and FootprintLoad takes a filesystem path, not a LIB_ID. The design never specifies the hand-written updater: incremental ref matching, footprint swap preserving position/side/rotation, pad<->pin mapping from the netlist `(node ...)` entries, net rename/merge, orphan removal, DNP/exclude_from_board, and fp-lib-table nickname resolution (env vars, `${KIPRJMOD}`, nested `Table` rows on 10, user's custom libraries).**
  - Why it matters: This is the single largest piece of KiCad logic the wrapper must reimplement from scratch on every supported version, it is on the MVP critical path (Phase 1), and its semantics (what `--schematic-parity` will later judge) are exactly the KiCad-internal behaviour the project claims not to replicate. Under-scoping it will blow the Phase 1 budget.
  - Action: Add a `netlist-updater` spec section: match rules, field/attribute propagation, pad mapping by pad number string, behaviour on footprint change, how `kicad-cli pcb drc --schematic-parity` is used as the oracle in T8. Build the SWIG-free file emitter for the *initial* board (Phase 1, not Phase 4) since only pad.SetNet/FootprintLoad are actually needed from SWIG. Write the fp-lib-table resolver (global template + user's global table read-only + project table) as a forge-formats module with its own tests.
- **[blocker] Licensing conflicts with the stated MIT wrapper: PyMuPDF (used in-process for every schematic/board raster and datasheet reads) is AGPL-3.0-or-commercial (confirmed from installed dist-info: 'Dual Licensed - GNU AFFERO GPL 3.0 or Artifex Commercial License'); easyeda2kicad 1.0.1 is AGPL-3.0 (confirmed) but is not in the 'separate process only' list; forge-swig-driver `import pcbnew` links against GPL-3.0 KiCad code and is shipped inside an MIT repo without its own licence; the Freerouting jar (GPL-3.0) is to be downloaded but bundling/redistribution policy is not stated.**
  - Why it matters: An AGPL library imported into the MCP server process makes the whole server AGPL (or requires a commercial Artifex licence); this contradicts open question 7 and the repo rule. The SWIG driver is the piece most likely to be judged a derivative work of KiCad.
  - Action: Replace PyMuPDF with pypdfium2 (BSD/Apache) or resvg/cairosvg for SVG->PNG, or use `kicad-cli pcb render` PNG on 9/10 and PDF->PNG via pdfium. Run easyeda2kicad only as a subprocess CLI (add to the separate-process list). License `packages/forge-swig-driver` GPL-3.0-or-later as an independent program invoked over a JSON stdin/stdout boundary. Download Freerouting/JRE at bootstrap with pinned hashes rather than shipping them in the VSIX. Add a licence audit step to CI (pip-licenses).
- **[major] The Connect plugin's headline benefit ('instance discovery on Windows: KiCad hands the exact socket/token') does not hold under the verified pipe semantics: the server always binds the fixed pipe `\\.\pipe\<GetTempPath>\kicad\api.sock`, ignores KICAD_API_SOCKET, and a second instance silently fails to bind. KiCad still exports that same fixed path to its plugin, so a Connect click in the losing instance hands forge-core a socket owned by a *different* KiCad. The only real isolation is giving the sandboxed KiCad its own TEMP, and nng rejects ipc paths longer than ~128 chars (VS Code globalStorage paths exceed this).**
  - Why it matters: Live sessions can silently target the wrong board (the user's own KiCad, or a sibling agent's), and a naive storage-dir TEMP will make the API unreachable.
  - Action: Make session identity come from a probe (GetOpenDocuments + board filename + PID cross-check against the process that owns the pipe via `GetNamedPipeServerProcessId`), not from the handed-off path. Launch sandboxed KiCad with TEMP/TMP set to a short per-session directory (e.g. `%LOCALAPPDATA%\kf\<id>`), assert the resulting ipc path length. Have Connect send `{pid, project, editor, temp_dir}` and have forge-core refuse to bind when the pipe's server PID != reported PID. Reword section 5.1 accordingly.
- **[major] Connect's second benefit ('a button in both editors') is overstated for 9/10: standalone eeschema registers no API_HANDLER_COMMON (GetVersion/Ping fail, so kipy's KiCad() constructor cannot even connect), all six editor RPCs crash it, and when hosted in kicad.exe a schematic-dispatched request or an intermittent board request kills the whole kicad.exe process. The design also does not include a guard that refuses IPC writes when a schematic editor is open on < 10.0.7.**
  - Why it matters: A user who clicks Connect in their normal kicad.exe session (schematic + PCB open) can lose unsaved work when the agent's first BeginCommit is dispatched to the schematic handler.
  - Action: On 9.x/10.0.0-10.0.6: Connect in eeschema only opens VS Code (no socket hand-off); forge-core checks GetOpenDocuments(DOCTYPE_SCHEMATIC) and refuses all mutating RPCs with a teaching error if a schematic is open, unless the KiCad semver >= 10.0.7. Track KiCad by PID (WER auto-restarts a crashed KiCad with the same command line, so image-name liveness checks lie).
- **[major] kipy 0.8.0 is pinned for 9/10 'never mixed', but the committed 10.0.7 fix (f3105844) adds a `document` field to BeginCommit/EndCommit and returns AS_BAD_REQUEST for document-less commits when more than one editor is open. kipy 0.8.0 (built against 10.0.6 protos) cannot send it.**
  - Why it matters: The first point release that fixes the crash the design works around will break the pinned client in the common kicad.exe-hosted case.
  - Action: Plan for kipy >= 0.9 (or a vendored-proto client for the 10.0 branch, same mechanism as for 11) in Phase 3; add a T5 probe for the BeginCommit `document` field; install the KiCad 'testing' 10.0-branch build (symbols.kicad.org has a kicad-testing channel, implying such builds exist) to test before 10.0.7 ships.
- **[major] All verification ran on KiCad 10.0.0, but 10.0.6 is current and behaviour already differs across 10.0.x (SES import throw-vs-skip changed on the 10.0 branch; #24966 handling changed in 10.0.6; ERC JSON x100 confirmed still present in 10.0.5/10.0.6 only via a third-party report). Likewise 9.0.5 vs 9.0.x latest and 8.0.1 vs 8.0.x latest were not compared.**
  - Why it matters: The capability record is keyed on full semver, but the seed data comes from a single, stale point release per major; users will mostly have the latest point release.
  - Action: Upgrade the local 10.0 and 9.0 installs (or install side-by-side) to the latest point releases before Phase 0 exits; re-run the verification scripts already in the scratchpad; make 'latest point release of each supported major' a CI requirement.
- **[major] Sandboxed KICAD_CONFIG_HOME: the design 'copies' sym-lib-table/fp-lib-table but does not say from where, and the ERC verification shows the schematic gets 11 lib_symbol_issues/footprint_link_issues warnings (exit 5 with --severity-all) whenever the table set does not resolve the referenced libraries. If tables are copied from `%APPDATA%\kicad\<ver>` they do not exist for versions the user never ran; if they are the stock templates, the user's custom libraries are invisible to ERC/DRC/FootprintLoad for their existing projects. Also project-local tables are ignored unless a `.kicad_pro` exists.**
  - Why it matters: Either brownfield projects fail ERC/DRC with library warnings (breaking gates) or the sandbox silently diverges from what the user sees in their GUI.
  - Action: Define the seed algorithm: start from `<install>/share/kicad/template/*-lib-table`, merge (read-only) rows from the user's `%APPDATA%\kicad\<major.minor>` tables when present, resolve env vars from the user's kicad_common.json `environment.vars`, always create the `.kicad_pro` before project-local tables, and surface unresolved nicknames in `kf_env`. Add a T6 probe for 'library resolution in the sandbox'.
- **[major] Hierarchical/multi-sheet schematics are promised for v1 ('one hierarchical sheet per functional block', sheet pins, hierarchical labels, `/ROOT/SHEET_UUID` instances) but every ERC/netlist verification was flat, single-unit, rotation-0. Multi-unit symbols (op-amps, resistor networks), repeated sheet instances, per-sheet annotation, cross-sheet net naming (`/Sheet/Net`), and `sheet_instances` semantics are unverified. Buses/bus entries are absent from the writer spec entirely.**
  - Why it matters: The reference board (ESP32-S3 + USB-C + charger + LDO) is exactly the kind of design that needs several sheets and multi-unit parts; if the instances/sheet_instances model is wrong the netlist is silently wrong (the verification showed a missing instances block corrupts nets without an ERC error).
  - Action: Add a Phase 1 spike: generate a 3-sheet schematic with one sheet instantiated twice and a multi-unit symbol, verify with kicad-cli 10 ERC + netlist + `sch upgrade` re-save diff; add to T1/T8 corpus. Decide whether buses are v1 or out of scope and state it.
- **[major] The SWIG driver checklist encodes refuted preconditions and omits verified ones: 'one BOARD per process' is unnecessary; 'wxTheAssertHandler nulled' is not exposed by pcbnew and did not prevent the crash (use `wx.DisableAsserts()` or nothing); the actually required rules are: create boards via `pcbnew.NewBoard(path)`/`LoadBoard` (ZONE_FILLER access-violates on `BOARD()`/`CreateEmptyBoard()` on 7/8/9 because m_DRCEngine is null), keep the SHAPE_POLY_SET alive when using SetOutline, call `SetFPID` after FootprintLoad (otherwise lib_footprint_issues on 8.0), validate Edge.Cuts before ExportSpecctraDSN (malformed outline blocks on a modal wxLogWarning even headlessly), and `PCB_VIA.GetWidth(layer)` on 9/10 (no-arg form pops an assert dialog).**
  - Why it matters: The risk table's mitigation is wrong in detail; a driver built from it will hang on modal dialogs or crash on 7-9.
  - Action: Rewrite the driver checklist from the verification results; run every SWIG job under a watchdog that kills the process on a visible top-level window (wx dialog) as well as on timeout; reload the board on any False return from ImportSpecctraSES (partial mutation).
- **[major] Routing recipe attributes DRC-clean output to `automatic_neckdown=false`; verification shows `fanout.enabled=false` is the operative flag and neckdown is irrelevant. Freerouting 2.4.1 can also emit genuine router-introduced overlaps (#858, fixed only after v2.4.1 for 2.5.0) and substitutes its own 500 um copper-to-edge clearance. The Freerouting profile written on this machine has `allow_telemetry: true` / `disable_analytics: false`, and the design does not disable telemetry or the update check.**
  - Why it matters: Wrong flag attribution propagates into the skill and the router config; unhandled telemetry sends board metadata off-machine from a tool that advertises a sandbox.
  - Action: Fix section 8: fanout off unless BGA, neckdown left default, always set `--router.copper_to_edge_clearance_um` from the board rule; set `usage_and_diagnostic_data.disable_analytics=true`, `allow_telemetry=false`, disable update checks in the managed profile; note the 2.5.0 upgrade trigger.
- **[minor] T3 'negative gates' assume KiCad rejects on the version stamp and on any unknown token. Verified: PCB 7.0 has no stamp gate; schematic 8/9/10 only check the stamp inside the `generator_version` branch; unknown tokens inside `(general)`, `(pcbplotparams)` and stackup `(layer)` are silently skipped by all versions; exit codes are -1/127 for boards on 7/8 vs 3 elsewhere.**
  - Why it matters: An emitter that omits `generator_version` (or a test that expects rejection) will produce false confidence; a file that 'loads' on an older KiCad may be silently mis-parsed instead of rejected.
  - Action: Emit `generator`/`generator_version` exactly as KiCad N does; base T3 on real KiCad-written files rather than stamp edits; treat exit codes {3, -1, 127, 0xFFFFFFFF} as load failure; add per-location unknown-token expectations to the CST tests.
- **[minor] Concurrency of kicad-cli against a shared sandbox config root: kicad-cli rewrites `kicad_common.json` on exit (observed; a stray run re-serialised the user's real config before isolation) and 8.0.1/9.0.5 segfaulted on the very first ERC run against a brand-new config home. The design's per-project lock does not prevent two projects (or two agents) running kicad-cli concurrently against the same `<storage>/kicad-config/<ver>`.**
  - Why it matters: Config corruption and flaky first runs will show up as intermittent ERC/DRC failures in gates and CI.
  - Action: Pre-warm each config root once (run a trivial kicad-cli command, retry on exit 139), then either serialise kicad-cli per config root or give each forge-core process its own copy; make the config root read-only-ish by restoring the seed after each run.
- **[major] Security model is incomplete: `--http` streamable mode has no authentication story (any local process or a DNS-rebinding page could call `kf_*`), the Connect loopback endpoint's '0600 lock file' is meaningless on NTFS (needs an explicit DACL), Freerouting's REST server on :37864 is unauthenticated, `kf_datasheet`/jlcparts pull untrusted web content straight into agent context (prompt-injection surface), and project files themselves (`fp-lib-table` URIs, `.kicad_pro` env vars, `plugin.json` in a project) can point kicad-cli/SWIG at arbitrary paths. The claim 'no shipped component runs code inside KiCad' is fine, but nothing says the agent can never pass raw Python or shell to the SWIG driver.**
  - Why it matters: An MCP server that spawns interpreters and Java on behalf of an autonomous agent is a privilege-escalation path on the developer machine.
  - Action: stdio by default, `--http` bound to 127.0.0.1 with a bearer token and mcp 2.2's origin policy; bind Freerouting to 127.0.0.1 on a random port; state explicitly that SWIG ops are a closed enum with JSON args (no eval); wrap fetched datasheet/part text in data-only framing and size caps; validate lib-table URIs against an allowlist (install share, project dir, user lib dirs).
- **[major] Out-of-band edits: any agent (Claude Code, Cursor) can edit `.kicad_sch`/`.kicad_pcb` text directly with its own file tools, bypassing `kf_*`, gates, snapshots and stage state. The design mentions `if_match` hashes on `kf_sch_edit` but the stage machine has no invalidation on file change.**
  - Why it matters: Agents will hand-edit s-expressions when a tool is missing; stale stage state then lets `kf_fab_export` run on a board whose DRC was computed for a different file.
  - Action: Store content hashes per stage result; every tool re-hashes inputs and downgrades the stage (with a receipt `degraded: ['external_edit']`) when they differ; add a skill rule 'never edit KiCad files directly' plus a `kf_sch_edit` raw-CST-patch escape hatch so agents do not need to.
- **[major] Testing/CI mechanics are unspecified: where CI obtains four KiCad installers (~1.5 GB each) and Java 25, that GUI-required IPC tests need an interactive desktop session (untested on GitHub-hosted Windows runners), that WER LocalDumps is off by default (crash detection via minidumps needs an HKLM registry key), that export goldens contain version/date headers, and that the 'three consecutive Claude Code runs' eval needs API credentials and budget in CI.**
  - Why it matters: The certification programme (T1-T9, `kiforge certify`) is the project's core promise of 'upgrade KiCad underneath' and cannot be delivered without a reproducible matrix.
  - Action: Add a CI design: cached installer downloads with hashes, self-hosted Windows runner (or a documented manual certify step) for GUI/IPC tests, normalised gerber/drill headers before diffing, `procdump -ma` as a fallback to WER, and a nightly eval job with a cost cap.
- **[major] KiCad 11 path is entirely documentary (no nightly installed): `kicad-cli api-server`, ImportNetlist, PlaceFootprintFromLibrary, RunBoardJobExport*, schematic items over IPC, and the removal of SWIG are all '◐'. KiCad majors ship around Jan/Feb, so 11.0 GA likely lands around the roadmap's Phase 4 start; the SWIG-free twins (board emitter, DSN writer, SES reader) are scheduled after the MVP that depends on SWIG.**
  - Why it matters: If 11.0 GA arrives before Phase 4 completes, the product has no board-build path on the newest KiCad, which is exactly the upgrade scenario the user asked to be immune to.
  - Action: Install the 10.99 nightly in Phase 0 (it also contains the #24966 fix) and run the T5 handler snapshot immediately; pull the file-based board emitter into Phase 1 (it is needed anyway for the initial board) and keep only pad-net assignment/fill/DSN on SWIG.
- **[minor] Library management tooling is thin: no tool creates or edits footprints/symbols (`kf_fp_create`/`kf_sym_create` from parametric generators), no policy for where imported LCSC parts live (`${KIPRJMOD}/libs` + project tables + `.kicad_pro`), 3D model paths for JLCPCB parts, JLCPCB rotation/offset corrections for the pos file (well-known mismatch KiCad vs JLC orientation), and the `LCSC Part #` BOM field are absent from the fab profile.**
  - Why it matters: Real boards always need at least one custom footprint or an LCSC part; without these the agent will hand-write `.kicad_mod` s-expressions with guessed geometry, which the design forbids.
  - Action: Add `kf_lib_create` (symbol from pin table, footprint from IPC-7351-style parametric generators or KiCad's own footprint wizards run via the SWIG driver), specify the project library layout, and add rotation-correction data to the JLCPCB profile.
- **[minor] ERC/DRC gate definitions are imprecise: kicad-cli counts warnings when `--severity-all`/default severity includes warnings (exit 5), ERC type names differ per version (`label_dangling` vs `global_label_dangling` + `unconnected_wire_endpoint`), ERC JSON `description` strings are also x100, and custom design rules (`.kicad_dru`), netclass-to-net assignment in `.kicad_pro`, and 4-layer stackups (all tests were 2-layer) are not covered by `kf_pcb_setup`.**
  - Why it matters: Gate logic that hardcodes type names or treats warnings as errors will block or pass incorrectly across versions; 4-layer boards are in scope but untested.
  - Action: Define `ok` as zero *errors* with warnings reported; keep the must-fix/review/cosmetic tables per version in the capability record; add `.kicad_dru` and netclass writing to `kf_pcb_setup`; add a 4-layer board to the corpus.
- **[minor] The design defers the source build the user explicitly expected. The argument (crashes symbolicated via PDBs, api.log, nightly for 11) is well supported, but the document does not present the user with the concrete middle grounds: KiCad 'testing' 10.0-branch builds (fixed IPC handler before 10.0.7 ships), a source checkout without building for `.srcpath`, and the fact that a debug build is the only way to step through the netlist updater / DRC engine when reimplementing their semantics.**
  - Why it matters: This is a direct user direction; the user must consciously accept the deferral or fund the build.
  - Action: Keep the decision but add the middle grounds to section 5.2 and make Q5 a yes/no with the cost stated (VS 17.14 update, 40-80 GB, ~1 day).
- **[minor] Agent-client assumptions not verified: Claude Code's 25k-token image accounting, Copilot/Codex image handling, Cursor's 40-tool cap, Codex 60 s default timeout, and skill discovery from `.claude/skills` by non-Claude clients. Also, skills living in the kiforge repo do not reach a user's own project; 'Configure agents' covers MCP configs but not skill/AGENTS.md installation.**
  - Why it matters: The eval matrix and the 35-tool budget rest on these numbers; skills are the mechanism that keeps the agent from hand-routing or guessing ids.
  - Action: Add a Phase 0 client-capability probe document with sources/dates; have `kf_project create` and 'Configure agents' install the skills and AGENTS.md/CLAUDE.md snippet into the user's project.

**Assumptions still unverified** (candidates for Phase 0 spikes):

- Freerouting 2.4.1's local REST API (sessions/jobs/settings/input/start/output/drc with Freerouting-Profile-ID and Freerouting-Environment-Host headers) was exercised in earlier research (scratchpad/autorouting/api_client_test.py, api_server.log) but was not part of the formal verification; SSE progress and cancel are unverified.
- KiCadRoutingTools 0.22.1 is MIT (confirmed from scratchpad/autorouting/krt/LICENSE) but its output quality, determinism, and the claim that it writes DRC constraints into .kicad_pro were not re-verified.
- PCM package metadata `runtime: "ipc"` and PCM installation/update of an `exec`-runtime plugin on 9.0.5 and 10.0.x were verified from source/docs only; the Connect button click and env hand-off were never observed end to end (synthetic input was ignored).
- KiCad 11 (master/10.99 nightly): headless `kicad-cli api-server`, ImportNetlist, PlaceFootprintFromLibrary, RunBoardJobExport*, schematic item CRUD, and SWIG removal are all documentary; no nightly is installed.
- KiCad 'testing' builds of the 10.0 branch (pre-10.0.7 with the #24966 fix) exist as installers; inferred from the kicad-testing symbol channel, not checked on downloads.kicad.org.
- GitHub-hosted Windows runners provide an interactive desktop sufficient for wx GUI + IPC tests; not tested.
- Claude Code image token accounting, Copilot/Codex image support, Cursor 40-tool cap and Codex 60 s tool timeout are taken from the design, not verified here.
- KiCad's own position on GPL applicability to scripts that `import pcbnew` (SWIG) versus IPC clients (kicad-python is MIT) was not researched; treated as a licensing risk rather than a settled fact.
- easyeda2kicad output for LCSC parts passing `sym upgrade`/`fp upgrade` and ERC/DRC was verified only on KiCad 10 for one or few parts; behaviour on 8/9 formats is untested.
- The vcpkg/VS 17.14 source-build recipe (binary cache hash matching MSVC 14.44, swigwin 4.3.1, ~1 day) is unverified since no build was attempted.
- Multi-unit symbols, rotated/mirrored placements combined with hierarchical sheets, and bus wiring were never run through ERC/netlist; only the pin transform (12 orientations, single-pin symbol) and flat rotation-0 schematics were.
- kicad-cli `pcb drc --refill-zones --save-board` preserves all board content it does not touch (it re-serialises the file with KiCad's own writer); assumed lossless because it is KiCad, but not diffed.

---

## 18. Direction change 2026-09-19: KiCad 11 first, data-first feedback

Decisions D16 and D17 ([DECISIONS.md](DECISIONS.md)) change the priority order and the feedback
model set out in §1–§11. This section records what changed and where the detail now lives; the
earlier sections are left as written and read as "Phase 0 as planned".

**Targets.** The primary target is the newest KiCad: the pinned 10.99 nightly (10.99.0-4083-g6dd893f7d1
today, re-probed by `scripts/ci/refresh_matrix.py` on every bump) and KiCad 11.0 at GA; 10.0.x is
co-primary; 9 is certified later; 8 and 7 are best-effort. The §2 matrix column "11 (master, not
installed)" is now a verified column: `kicad-cli api-server` runs headless, `sch diff`, `sch import`,
`pcb export png|bom|stackup --format json`, `gerber`, `mergetool` exist, SWIG and the bundled Python
are gone. The SWIG driver (§3, §14) leaves the critical path and becomes a legacy backend for ≤ 10.

**Backends by version (replaces the §4 priority order).**

| KiCad | Schematic read/write | Board read/write | Checks and exports |
|---|---|---|---|
| 11 / nightly (`ipc.headless`) | headless IPC (`kiwrap.backends.ipc.sch_writes`: symbols, wires, labels, power, edit, delete, annotate, footprints — verified 2026-09-20, ERC 0 / netlist exact on the api-server's files) with the file engine as the lossless bridge and for what the nightly cannot do headless (hierarchical sheet creation: `SaveDocument` fails after `CreateItems(SheetSymbol)`) | headless IPC (`pcb_writes`: `ImportNetlist` sync, place, zones + `RefillZones`, tracks, vias) and the file engine (`board.setup` — .kicad_pro / .kicad_dru have no API) | kicad-cli (job exports over headless IPC verified — G6 closed) |
| 10.0.x (co-primary) | file engine only (`kiwrap.formats.schematic_writer`; no schematic API; #24966 until 10.0.7) | file engine (`kiwrap.formats.board_writer` + `netlist_sync`) + SWIG driver (legacy) + GUI IPC live sessions (PCB only, D2) | kicad-cli |
| 9 / 8 / 7 | file engine | file engine + SWIG driver | kicad-cli where it exists (9 has no zone refill: pours stay unfilled, `degraded: refill_unavailable`) |

The end-to-end proof is `tests/test_e2e_authoring.py`: one MCP stdio conversation per install
(kw_project → kw_lib_search → kw_sch_add_symbol × 11 over two sheets → kw_sch_connect /
kw_sch_add_item → kw_sch_check → kw_sch_annotate → kw_sch_assign_footprints → kw_pcb_setup →
kw_pcb_sync → kw_pcb_place → kw_pcb_route → kw_pcb_zones → kw_pcb_drc → kw_fab_export) reaching
ERC 0 / parity 0 / DRC must-fix 0 / `exported` on 10.99 (ipc11 writes) and 10.0.6 (file writes),
and the same schematic/board numbers on 9.0.9 with a forced export (no refill).

**Data-first feedback (replaces "render and look" in §11).** The agent's loop is structured data:
`kw_pcb_get`, `kw_sch_get`, `kw_pcb_check`, `kw_sch_stats`, `kw_sch_diff` (typed `outputSchema`,
filters, paging) fed by a planner (`kiwrap.core.planner`) whose provider chains are `ipc11 → file →
cli` with capability predicates, fall-through and `provider_used` / `degraded[]` receipts. Renders
(`kw_sch_render`, `kw_pcb_render`) are secondary tools for humans and VS Code previews. The skill
rule becomes: after every mutating step — snapshot → structured check (`kw_*_check` / `kw_*_get`)
→ fix from uuid + position → re-validate; render only when a human asks.

**Three tiers of signal ([UPSTREAM.md](UPSTREAM.md) §1 is the definitive table).** Tier 1, no
KiCad change: everything in the files through the lossless CST and its semantic models
(`kiwrap.formats.board` / `schematic` / `analysis`: geometry, courtyard overlaps matching DRC,
off-board, pin connectivity and a netlist that agrees with kicad-cli on 12779/12787 demo nets) plus
kicad-cli structured outputs (DRC/ERC JSON, netlist, IPC-2581, IPC-D-356, stats, stackup,
position, `sch diff`, PNG). Tier 2, KiCad 11 headless IPC: items, bounding boxes, hit test, nets,
connectivity, netlist + hierarchy, design rules, `ImportNetlist`, `Place*FromLibrary`, commits and
saves. Tier 3, gaps needing a KiCad change: ratsnest/airwires with endpoints (no CLI or IPC
surface anywhere; kiwrap ships an estimate), DRC/ERC over IPC, per-net statistics, job dispatch and
library queries on the headless server, schematic `GetBoundingBox`, plus the bugs ERC JSON ×100
(#25582), DRC per-type caps (#25434) and the nightly `UpdateItems` hang. UPSTREAM.md §2 has one
card per gap with workaround, upstream status, proposed proto/CLI change, effort and priority.

**The non-negotiable, relaxed.** "No shipped component may require a patched KiCad" becomes:
stock KiCad always works and is the only thing certified as *supported*; a patched or nightly
build unlocks more; every patch lives in `n3rd-bugs/kicad-fork` (private; the public fork `n3rd-bugs/kicad-fork` carries none of our refs) (branches `mr/<issue>-<slug>`,
integration branch `kiwrap/<kicad-sha>`, tags `kiwrap-build/<sha>-<n>`) until merged upstream, and
is detected by presence probes that flip ordinary capability keys (`cli.has_ratsnest_export`,
`ipc.has_get_ratsnest`, …) plus an informational `kiwrap.patches` list; tools never branch on the
latter. The source build moves from D1's "gated trigger" to planned Phase 1 work; the bench's
acceptance test is that T5 on the self-built nightly commit reproduces
`conformance/snapshots/ipc-handlers/10.99.0.json` (UPSTREAM.md §3–§4).

**Capability keys added or planned by this change.** Existing: `ipc.headless`, `ipc.schematic`,
`ipc.commit_needs_document`, `ipc.headless_libraries_need_load`, `ipc.headless_jobs`,
`ipc.schematic_update_shared_lib_symbol_hangs`, `cli.has_stats_json`, `cli.has_stackup_json`,
`cli.has_sch_diff`, `cli.has_pcb_png`. Planned with the fork: `cli.has_ratsnest_export`,
`ipc.has_get_ratsnest`, `ipc.has_drc_job`, `ipc.has_erc_job`, `ipc.has_net_statistics`,
`ipc.headless_library_queries`, `ipc.schematic_bounding_box`, `ipc.schematic_pins_are_items`,
`ipc.import_netlist_structured`, `cli.drc_json_has_marker_pos`, `cli.drc_unlimited_violations`,
`kiwrap.patches`, `kiwrap.patched_build`. `cli.erc_json_pos_scale` stays forever and flips to 1 on
fixed builds.

**Client.** kicad-python from git main (0.9.0.dev0, pinned commit, MIT) is the KiCad 11 client;
kiwrap launches `kicad-cli api-server` itself (sandbox env, `Ping` readiness as in KiCad's own
`qa/tests/api/api_e2e_utils.h`, tree kill) because `kipy.server.KiCadServer` takes no `env`. The
Windows git-install packaging bug (bare proto imports; root cause protoc-wheel-0's `os.execv`
launcher) is covered by a `.pth` site hook that is documented as temporary; the upstream fix is a
five-line change to `tools/generate_protos.py` (UPSTREAM.md G14). kipy 0.8.0 stays the client for
GUI sessions on 9/10 and is never installed in the same environment.

**What §17 items this closes.** "KiCad 11 path is entirely documentary" — closed: the nightly is
installed, T5 snapshotted, the headless backend, readers, queries and a write path are verified
live. "kipy 0.8.0 cannot send the `document` field" — handled by the git-main client for 11 and
the `ipc.commit_needs_document` key. Still open from §17: the ≤ 10 netlist updater (tier 1 on
those versions), library management tooling (tier 1 lib-table reader is a kiwrap TODO in
UPSTREAM.md G11), and the Linux/CI mechanics.

---

## 19. Phase 1c 2026-09-20: autorouting, the Specctra codec, fabrication profile, reference board

Five components landed on the Phase 1b tree and were integrated the same day; §8 was rewritten
to the as-built loop above. What exists now, with the evidence:

**Routing tools.** `kw_pcb_route_auto` and `kw_pcb_fix` (`kiwrap.core.tools.pcb_route_auto`,
32 tools in the catalog) over `kiwrap.backends.router` (`discover_engines` with a fallback from
the session's storage home to the platform default where `scripts/bootstrap.ps1` installs the
engines, `rules_from_board`, `dsn_post`, `Router`) and the planner operations `routing.autoroute`
/ `routing.fix` with providers `cli` (patched kicad-cli, presence keys) → `codec` → `swig`
(predicate `swig.available + swig.ses_import_headless`; no driver shipped). Verified live:
`tests/test_router_live.py` (ecc83 stripped / 4-layer / async / 10.99 through the codec),
`tests/test_route_auto_providers_live.py` (the patched CLI on the bench build
`C:/kiwrap-builds/6dd893f7d1-5` = `provider_used cli:10.99.0+specctra-jobs`, 2 and 4 layers,
`kw_pcb_fix` through the same chain; the codec's DSN == the patched CLI's DSN on the same build;
the stock nightly and 10.0.6 through the codec with `cli` / `swig` skipped by their predicates),
every run ending in kicad-cli DRC 0 must_fix / 0 unconnected after the refill.

**The codec** (`kiwrap.formats.specctra`, zero dependencies): `DsnWriter` reproduces
`specctra_export.cpp` (µm / y-flip / %.6g, KiCad's padstack naming for every pad shape incl. the
roundrect `CornerListToPolygon` and custom-pad hulls, back-side flip, content-hashed image
dedupe with `::N`, `REF**` / `@N` uniquifying, netclasses with the `NET_SETTINGS` schema
migrations replayed, planes with windows, cutout / rule-area keepouts, fix / route wiring),
`SesReader.apply` has `FromSESSION`'s semantics on a `BoardWriter` (delete unlocked, keep locked,
placement counted not applied by default, drill from the padstack name, blind / micro / buried
vias, unknown layer / padstack skipped and counted), `parse_dsn` / `compare_dsn` is the
semantic, tolerance-based diff that certifies any DSN producer against KiCad's own. T7
(`conformance/tests/t7_specctra_codec.py`) is the certification: 0 differences at 5 µm on eight
boards against SWIG 10.0.6, byte-identical Freerouting sessions for the codec DSN and KiCad's,
applied boards equal to `ImportSpecctraSES` within 1e-3 mm, DRC 0/0 on 10.0.6 and 10.99.
Phase 1c review fixes (2026-09-20, all re-certified: 0 differences on EVERY 10.0.6 demo incl.
the 10-layer jetson baseboard, the 12-layer vme-wren, the 8-layer RoyalBlue54L and the 8.99-written
multichannel_mixer-unrouted): copper layers are the LEADING block of typed `(layers …)` rows the
way `parseLayers` reads them (`User.N` rows typed `signal` are not copper — `BoardFile`, the
writer and the fab export agree); a back-side footprint's rule areas mirror their layers through
the stack like `ZONE::Flip`; a board saved by the nightly (stamp ≥ 20260616) holds footprint zones
in the LIBRARY frame and the codec writes that outline as the image geometry (KiCad master's own
exporter had the same bug — fixed on the fork, `mr/specctra-fp-zone-frame`); a via without
`(drill …)` takes its NET's netclass drill (`PCB_VIA::GetDrillValue`) and `SesReader.apply`
materialises the drill KiCad saves (the net's class drill when the padstack drill is the Default
class's); class ids are KiCad 9+/10's EFFECTIVE netclass names (`<name>,Default` for a class that
leaves a parameter to Default, `A,B` for a net in two classes); every millimetre goes through
KiCad's integer nanometres (`um`) and placements print ten significant digits; `roundrect` pads
with `(chamfer …)` are KiCad's CHAMFERED_RECT with the `doChamfer` polygon and id; zone-outline
arcs are polygonised with `SHAPE_ARC::ConvertToPolyline` at the PCB default accuracy. Remaining
approximation documented in the module: KiCad 9+ per-layer padstacks as uniform stacks.

**The fork patch `specctra-jobs`** (`mr/specctra-jobs` = base + `pcb export specctra` +
`pcb import-ses`, tag `kiwrap-build/6dd893f7d1-5`, drafts
`docs/upstream/drafts/specctra-jobs-{export,import}-mr.md`, UPSTREAM.md G17): KiCad's own
`ExportBoardToSpecctraFile` and `ImportSpecctraSession` as headless jobs on KiCad 11, which has
no SWIG. The import has the editor's semantics (locked copper kept, SES placement applied to
locked footprints too, unresolved items counted, exit 3 with the board untouched on a bad
session). `kiwrap.backends.cli.specctra.export_dsn[_run]` / `import_ses[_run]` adapt it
(`Unsupported(fallback='codec')` on stock installs); `tests/test_cli_specctra.py` proves it
equal to the SWIG oracle on ecc83 / coldfire 4L / blinker.

**Fabrication profile, BOM, intent** (`kiwrap.core.fab`, `kw_fab_bom`, `kw_intent`,
`kw_fab_export(profile="jlcpcb")`, `kw_pcb_setup(preset=…)`): D9's JLCPCB deliverables — Protel
gerber naming, PTH/NPTH Excellon + X2 map, CPL with the community rotation table (every
correction reported), BOM grouped by value + footprint + LCSC (+ DNP) from kicad-cli's
`sch export bom` or the netlist, a zip bundle, and the intent diff (`missing` / `wrong_pin` /
`extra` / `unmatched`) surfaced by `kw_sch_check`. Found on the way and fixed: kiwrap's board
template enabled only F.Cu / B.Cu / Edge.Cuts, so kicad-cli plotted no mask / paste / silk for
any kiwrap-created board (now KiCad's standard technical layer set).

**Reference board (D5)** — `evals/reference_board` (data-driven plan, grader, `claude -p` runner)
and `tests/test_e2e_reference_board.py`: the ESP32-S3 board (29 stock-library parts, 3 child
sheets, 2-layer 50 × 35 mm, JLCPCB preset) from `kw_intent` to the JLCPCB bundle over MCP
stdio, graded by KiCad only. Results are recorded in the test (`<project>/.kiwrap/e2e-out.json`:
router passes and per-pass history, routing time, unrouted before / after, DRC before / after
`kw_pcb_fix`, `provider_used` per stage) and summarised in README.md.

**Bugs found by the integration, fixed in the file tier:** (1) kiwrap's board template wrote a
`(net 0 "")` row into the KiCad-10 dialect (nets are by name only there): `BoardFile.nets` saw
a one-entry code table and hid every net, the DSN carried an empty `(network)` and Freerouting
routed nothing until a kicad-cli `--save-board` had rewritten the file — the template no longer
writes the row and `_NetTable.nets()` lists name-only nets beside a code table; (2) new
`.kicad_pro` files said `net_settings.meta.version 0`, and KiCad's `migrateSchema2to3` wipes
`netclass_patterns` on load (only `Default` reached the DSN / DRC): `BoardWriter` now writes
schema 3 whenever it writes the project. Still open (UPSTREAM.md / the integrator's report):
footprint rule areas placed by the file writer on ≤ 10 dialects keep the library's local
coordinates where KiCad expects board coordinates (harmless for the reference board — the
antenna keep-out is off-board either way — wrong in general), and KiCad master's own DSN export
double-subtracts the footprint position for such zones when it loads a ≤ 10 board.

**Engines.** `scripts/bootstrap.ps1` → `%LOCALAPPDATA%/kiwrap/engines/{jre, freerouting}` +
the telemetry-off profile (`scripts/engines.json` pins Temurin JRE 25.0.4.1 and Freerouting
2.4.1); `kw_env` reports them through `discover_engines`; a session storage home without them
falls back to `KIWRAP_ENGINES_HOME`, then the platform default; `KIWRAP_JAVA` /
`KIWRAP_FREEROUTING_JAR` override the executables.
