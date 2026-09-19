# Decisions log

Interactive decisions taken by the project owner on 2026-09-19 against the options in
[ARCHITECTURE.md](ARCHITECTURE.md) §15. Each entry records the choice, the alternatives that were
on the table, and what the choice changes in the plan. Later decisions append below; earlier ones
are never rewritten, only superseded.

## D1 — KiCad source build: gated trigger

**Choice:** write the build recipe (`docs/dev/kicad-source-build.md`) and fork
`KiCad/kicad-source-mirror` with `gh repo fork` in Phase 0; build only when a named trigger fires
(upstream MR, stepping through KiCad's netlist updater / DRC engine while reimplementing their
semantics, PNS as a batch engine).
**Alternatives:** bench in Phase 0; bench in Phase 3.
**Consequence:** no VS 2022 update or 40–80 GB spend now; crash diagnosis relies on
symbols.kicad.org PDBs + `cdb` and `EnableAPILogging=1` (both verified). The fork exists from
Phase 0 so the trigger costs a clone, not a decision.

## D2 — Connect plugin / live sessions on 9.x–10.0.x: PCB-only, sandboxed pcbnew

**Choice:** the Connect toolbar button in pcbnew hands over token/PID; the eeschema button only
opens VS Code; live sessions launch a sandboxed standalone `pcbnew.exe` with isolated
`KICAD_CONFIG_HOME` and a short `TEMP`. Schematics always go through the file path.
**Alternatives:** attach to the user's running KiCad; defer Connect entirely.
**Consequence:** zero exposure to #24966 (schematic-handler crash) and to the fixed-pipe collision;
`kw_live` refuses mutating RPCs while a schematic editor is open on < 10.0.7. Clarified during the
decision: this does **not** limit schematic authoring — only live-in-GUI schematic editing, which
KiCad has no API for before 11.

## D3 — Version floor: 10 first, 9 certified, 8 partial, 7 best-effort

**Alternatives:** 10 + 9 only; full 7–10.
**Consequence:** KiCad 7 dialect + SWIG DRC shims stay in Phase 4 as best-effort; 8 gets the
schematic and fabrication stages only (no routing certification).

## D4 — Installs: point releases + 10.99 nightly

**Choice:** install 10.0.6, latest 9.0.x and 8.0.x, and a 10.99 nightly side-by-side; re-run the
[spikes](../spikes/) so the capability record is seeded from current builds.
**Alternatives:** point releases only; keep what is installed.
**Consequence:** Phase 0 exit criteria require the T5 handler snapshot on the nightly and the
verification scripts green on the current point releases. Installers need a UAC prompt on this
machine.

## D5 — Reference board: ESP32-S3 dev board

**Choice:** ESP32-S3-WROOM-1 + USB-C 2.0 + MCP73831 LiPo charger + 3.3 V LDO + buttons + I²C
header, ~50×30 mm, 2-layer, JLCPCB parts. MVP acceptance = Claude Code builds it from the spec,
3 consecutive runs, no human edits, 0 ERC / empty intent diff / 0 unconnected / 0 must-fix DRC /
complete fab bundle.
**Alternatives:** a real project of the owner's; a simpler MCU breakout; both.

## D6 — Live editing: headless first, live in Phase 3

**Alternatives:** live editing in v1; headless only.
**Consequence:** the VS Code extension is not gated on the IPC session supervisor; kipy ≥ 0.9 /
vendored-proto work for 10.0.7 lands in Phase 3.

## D7 — Libraries: read the user's global tables, never write them

**Choice:** the sandbox config seeds from `<install>/share/kicad/template/*-lib-table`, merges
rows from `%APPDATA%\kicad\<major.minor>\{sym,fp}-lib-table` read-only, resolves env vars from the
user's `kicad_common.json`, and always creates the `.kicad_pro` before relying on project-local
tables. `%APPDATA%\kicad` is never written; CI asserts its hash is unchanged.
**Alternatives:** stock + project-local only; explicit allow-list.

## D8 — Licensing: MIT core, GPL SWIG driver, no AGPL in-process, no cloud routers

**Choice:** `kiwrap-core`/`formats`/`vscode`/`connect` MIT; `kiwrap-swig-driver` GPL-3.0-or-later
as a separate program (it links KiCad's GPL bindings); Freerouting (GPL), easyeda2kicad and KiBot
(AGPL) run only as separate processes downloaded at bootstrap; PyMuPDF replaced by pypdfium2/resvg.
Cloud routers excluded.
**Alternatives:** same + optional cloud routers; everything GPL; proprietary.

## D9 — Fabrication: JLCPCB default profile

**Alternatives:** generic first; another vendor first.
**Consequence:** JLC capability rules preset, Gerber naming, CPL/BOM with `LCSC Part #`, JLC
rotation corrections, and the LCSC parts search/import path are Phase 1 deliverables.

## D10 — Schematic scope: hierarchy + multi-unit in v1, buses Phase 2

**Alternatives:** everything in v1; flat only for MVP.
**Consequence:** Phase 1 opens with the 3-sheet / repeated-instance / multi-unit spike (§7); the
writer's bus support is a Phase 2 item.

## D11 — Toolchain: install Node now; bootstrap downloads pinned engines

**Choice:** Node LTS installed in Phase 0; `bootstrap.ps1`/`bootstrap.sh` download a private
JRE 25, the Freerouting 2.4.1 jar, the jlcparts DB and EasyEDA data with pinned versions and
hashes; Freerouting analytics/telemetry/update checks forced off.
**Alternatives:** manual downloads (air-gapped); defer Node to Phase 3.

## D12 — Platforms: Windows + Linux from Phase 0 (override of the Windows-first default)

**Choice:** both certified from the start; macOS later.
**Consequence:** a `kiwrap.platform` seam from day one; a Phase 0 Linux spike on a GitHub-hosted
runner (distro/Flatpak discovery, system-python SWIG ABI, unix-socket IPC under Xvfb) — none of
this has been verified yet, everything to date ran on Windows; CI matrix gains a Linux job; JRE
packaging per platform.

## D13 — Agent clients: Claude Code first-class only (override of the multi-client default)

**Choice:** Claude Code CLI + VS Code extension is the certified client (configs, skills, image
handling, eval runner). Copilot, Cursor, Windsurf and Codex get generated MCP configs and a smoke
test, nothing more.
**Consequence:** eval matrix has one runner; the VS Code extension's
`registerMcpServerDefinitionProvider` path (Copilot) is best-effort.

## D14 — CI: GitHub Actions + this machine as a self-hosted runner

**Choice:** headless tests (kicad-cli, SWIG, formats, Linux) on GitHub-hosted Windows/Linux
runners with cached KiCad installers; GUI/IPC certification on a self-hosted runner on the owner's
PC, scheduled when idle.
**Alternatives:** hosted only; local only.

## D15 — Eval budget: manual only (override of the ~$50/month default)

**Choice:** no scheduled agent evals; `kiwrap eval` is run by hand when the owner chooses.
**Consequence:** certification (T1–T9) stays automated; "3 consecutive green runs" for a release
is a manual gate.

## D16 — KiCad 11 first (supersedes the priority order in D3)

**Choice (owner, 2026-09-19):** the newest KiCad is the primary target: the 10.99 nightly now
(pinned build, re-probed with `scripts/ci/refresh_matrix.py` on every bump), KiCad 11.0 at GA.
KiCad 10.0.x stays co-primary because it is what stable users have. 9 is certified later; 8 and 7
drop to best-effort/deferred.
**Verified on the installed nightly (10.99.0-4083-g6dd893f7d1):** `kicad-cli api-server
[--socket] <project|pcb|sch>` runs headless and answers `GetVersion`; documents are opened with the
new `OpenDocument` command family, which kicad-python 0.8.0 (10.0.6 protos) cannot send; SWIG
`pcbnew` and the bundled Python are gone; new CLI commands `sch diff --format json`, `sch import`,
`pcb export png|bom|stackup --format json`, `gerber`, `mergetool`.
**Consequence:** the IPC headless backend becomes the primary backend for schematic AND board
from Phase 1, with a client generated from the nightly's own `api/proto`; the file engine remains
the lossless editing/bridging layer and the only schematic path on ≤ 10; the SWIG driver becomes a
legacy backend for ≤ 10 and leaves the critical path; the capability matrix keeps a verified entry
per nightly build.

## D17 — Data-first feedback; expand kicad-cli/IPC upstream; source build planned

**Choice (owner, 2026-09-19):** the agent's feedback loop is structured data, not images. Renders
stay as secondary tools (VS Code previews, human review). Where KiCad exposes no structured
surface, kiwrap extends KiCad's CLI/IPC upstream, which requires the source build — so D1's
"gated trigger" becomes "planned, Phase 1".
**Tiers:** (1) no KiCad change needed — everything stored in the files via the lossless CST
(footprint/pad/courtyard geometry, tracks, vias, zone fills, outline; symbol pin endpoints, wires,
labels, junctions, sheets → overlaps, off-board, bounding boxes, pin connectivity, dangling ends)
plus kicad-cli JSON/structured exports (DRC/ERC with coordinates, netlist, IPC-2581, IPC-D-356,
stats, stackup, `sch diff`); (2) KiCad 11 IPC headless — `GetItems`, bounding boxes, hit-test,
nets, netlist + hierarchy, `ImportNetlist`, `PlaceFootprintFromLibrary`/`PlaceSymbolFromLibrary`,
job exports; (3) upstream candidates — ratsnest/airwire export with endpoints (`kicad-cli pcb
export ratsnest --format json` or an IPC `GetRatsnest`), `RunDRC`/`RunERC` over IPC (absent even
on master), per-net length/trace statistics, and a full structured dump (`pcb/sch export json`).
**Consequence:** the ARCHITECTURE "non-negotiable" that no shipped component may require a
patched KiCad is relaxed to: stock KiCad always works; a patched or nightly build unlocks more,
and every patch is carried in the fork (`n3rd-bugs/kicad-source-mirror`) until merged upstream.
The T5 handler snapshot of the nightly is the authoritative gap list for tier 3.

## D18 — Windows only, VS Code only (supersedes D12; narrows D13)

**Choice (owner, 2026-09-20):** the product targets Windows hosts and VS Code as the only user
surface. Claude Code running inside VS Code is the agent client; Linux/macOS, other IDEs (Cursor,
Windsurf) and terminal-only clients are out of scope until further notice.
**Consequence:** the Linux discovery/sandbox code stays behind the `kiwrap.platform` seam but is
unmaintained and untested (the `kicad-linux` CI job is removed; the cheap Ubuntu lint/unit jobs
remain as a Python-portability canary only); WSL/Xpra/Broadway display routes are informational,
the display-forwarding recommendation is Windows-native; generated MCP configs for other clients
are dropped from the deliverables; the VS Code extension is the one front end (its own webview
hosts the forwarded KiCad display and the previews).
