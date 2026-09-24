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
**Clarified (owner, 2026-09-24, with D29):** "default" means the first vendor profile kiwrap builds,
not the tool's default argument. `kw_fab_export` keeps `profile='generic'` when the argument is
omitted. Agents following the kiwrap-flow skill pass `profile='jlcpcb'`, which also runs the D29
fab-limits gate, so a board that doesn't target JLCPCB (e.g. jetson's 0.1 mm vias) is not blocked by
default.

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
and every patch is carried in the fork (`n3rd-bugs/kicad-fork` (private; the public fork `n3rd-bugs/kicad-fork` carries none of our refs)) until merged upstream.
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

## D19 — Everything stays private until an end-to-end solution exists

**Choice (owner, 2026-09-20):** no upstream MRs or issues, no public repos, no external posts
until the end-to-end flow works. Upstream contributions are prepared as drafts under
`docs/upstream/drafts/`; the KiCad tree with our patches lives in the **private**
`n3rd-bugs/kicad-fork` (GitHub forks of public repos cannot be made private, so the public fork
`n3rd-bugs/kicad-source-mirror` was stripped of every kiwrap ref and is now only a stale mirror).
**Consequence:** `origin` of `C:\src\kicad` is the private repo; `public` is the read-only mirror
remote; nothing is pushed to gitlab.com. **Amended (owner, 2026-09-24):** no AI-attribution trailer on any of our commits, the private
fork included. The 14 fork commits that carried `Assisted-by: Claude Code` were rewritten without
it (message-only, code trees identical; the new hashes are in `src/kiwrap/data/kiwrap_patches.json`)
and force-pushed to the private origin; the `public` remote's push URL is `DISABLED`. KiCad's
tool-generated-content policy §4.2 asks for `Assisted-by: <tool>` on a submitted contribution that
is substantially tool-generated; the owner decides that on the day of submission.

## D20 — Live view without hidden KiCad (resolves DISPLAY.md §12.8 decision 1)

**Choice (owner, 2026-09-20):** the product's live experience is the capture-only view — the
real KiCad window stays on the desktop (launched sandboxed by kiwrap with its API enabled), each
editor streams into its VS Code tab, an agent's edits appear live, and the user interacts with
KiCad natively (the tab's *Raise KiCad* button or the window itself). The hidden/cloaked mode
and the input injectors are **not** part of the product: `mr/display-input` stays parked in the
fork (not merged into the integration branch), the helper's `events`/`mirror`/`hybrid` modes
remain experimental settings off by default, and no user-facing text promises "KiCad inside
VS Code" beyond the live view.
**Why:** the owner does not want the complexity of running KiCad hidden as long as users see the
updates live.
**Consequence:** stock KiCad 10.0.x/11 is the only requirement for the live view; the
display-input bench build (6dd893f7d1-7) is unregistered; DISPLAY.md decisions 5, 11, 13, 14
are closed as "not pursued"; the e2e keeps the stock view-only scenario as the product path.

## D21 — A project runs on the KiCad matching its file format; upgrading needs the user's consent

**Choice (owner, 2026-09-24, from the feature-request review, requests/kiwrap-feature-requests.review.md
item 1):** for a project kiwrap did not create, kiwrap picks an installed KiCad whose file-format epoch
matches the project's files (the newest point release of that epoch). If none is installed, kiwrap
must not silently write a newer format: reads may run (in memory / on copies), but any operation that
would save the design files in a newer format stops and the **user is asked** whether the schematic
and board may be upgraded (MCP elicitation where the client supports it, otherwise a teaching error
the agent relays, and a modal in the VS Code extension); only then does `kw_project(action='upgrade',
confirm=true)` convert them. The prompt also names the alternative: install the matching KiCad.
**Why:** the review reproduced four paths that re-stamped a KiCad 10.0 project as 10.99 (unopenable in
10.0) on default arguments. D16 stays "develop against the newest KiCad" but never writes a newer
format into a user's files without consent.

## D22 — `kw_pcb_drc` does not save by default

**Choice (owner, 2026-09-24):** `save_board` defaults to false: zones are refilled in memory for the
check and the board on disk is untouched. Fabrication exports refill (or warn about stale fills)
themselves.

## D23 — Read-only mode: a server flag plus per-call `persist=false`

**Choice (owner, 2026-09-24, as recommended):** `kiwrap serve --readonly` (and `KIWRAP_READONLY=1`)
makes every call non-persisting; individual calls can pass `persist=false`. A non-persisting call
writes nothing into the project: no snapshot, no `.kiwrap/` state, no saved refill, no reports or
exports inside the project (outputs go to a temp or caller-given directory), and `.kicad_prl`
restored. Mutating tools refuse with a teaching error in read-only mode.

Resolved 2026-09-24 by research ([ANALYSIS-TOOLING.md](ANALYSIS-TOOLING.md) §5, §6, accepted by the
owner): Claude Code has no tool-count limit, so kiwrap keeps coherent tools and does not wrap them
(guideline about 30-45 tools); the analysis stack is a Rust pyo3 wheel plus a pure-Python fallback
and an optional FEM extra. The delivery details are D24-D31.

## D24 — Format upgrades go only through `kw_project_upgrade` (amends D21)

**Choice (resolved on evidence, 2026-09-24, at the owner's request; full record in [decisions/D24-D31.md](decisions/D24-D31.md)):** files
move to a newer KiCad format only through `kw_project_upgrade(project, from_kicad, to_kicad, files)`.
- **Publishing.** The tool is published with `_meta["anthropic/requiresUserInteraction"]: true` and
  destructive annotations, and has no `dry_run`, `confirm` or `persist`. Claude Code 2.1.280/281
  checks that flag before bypass mode and allow rules, so the call always asks.
- **What it does.** The server re-derives the arguments and refuses any mismatch. It then snapshots,
  converts exactly `files` with kicad-cli in the sandbox, runs ERC/DRC before and after, and pins the
  target.
- **Receipt.** It names the client permission flow that approved the call, not a human click: an SDK
  host's `canUseTool` can also approve.
- **`kw_project`.** It loses `upgrade`/`confirm`/`dry_run`; a stale call gets a teaching `E_BAD_ARGS`.
  It gains a read-only `upgrade_preview`, and `status` returns `upgrade_args`.
- **Other routes.** The extension modal calls the same tool. Elicitation stays an optional shortcut
  outside the VS Code panel, which declines it.
- **D15 check.** The end-to-end check with a real login is a manual checklist script.

It is the first change after Phase 0+1.

## D25 — Receipt shape: `pos` is KiCad's marker location, one cursor grammar, compact receipts

**Choice (resolved on evidence, 2026-09-24; review decision 5; [decisions/D24-D31.md](decisions/D24-D31.md)):**
- **Location.** There is no `anchor_pos` and no alias. A violation's `pos` is where KiCad marks it.
  Its sources, in order: kicad-cli's JSON marker (`pos_source: "kicad"`), then kiwrap's computed
  location (`"computed"`), otherwise null. It is never copied from an item. ERC keeps items[0]
  (`pos_source: "item"`). `Violation.pos` becomes `first_item_pos`, and `path_mm` carries the marker
  path or the computed closest points.
- **Paging.** All paged tools share one cursor grammar, `<lane><off>(.<lane><off>)*@<ver8>`. ver8
  covers the inputs and the arguments that decide order, and a stale cursor gives `E_STALE`. The DRC
  report lane is keyed on the stored result id plus the filters, and the stored schema becomes
  `kiwrap.drc_result.v2`.
- **Size.** A successful result's text is the summary line plus compact JSON; Claude Code shows the
  model `JSON.stringify(structuredContent)`. Budgets are measured on compact JSON: 28,000 chars per
  page and 48,000 per receipt. Claude Code token-counts results over 50,000 chars against a
  25,000-token cap, and compact DRC JSON measured 1.86 chars per token. The DRC run and ERC stop
  inlining 500 violations; full lists go to `out_path`.
- **Pre-1.0 policy (D19).** There are no aliases. Breaking changes land in one commit across server,
  extension, skill, kw_help and tests, recorded in a CHANGELOG "Unreleased - breaking" list.

## D26 — Sandbox KiCad runs in English; live GUIs get their own root

**Choice (resolved on evidence, 2026-09-24; review decision 7; [decisions/D24-D31.md](decisions/D24-D31.md)):**
- **The pin.** Every seed writes `system.language = "English"` (`sandbox.HEADLESS_LANGUAGE`), and no
  locale environment variable is set. Measured on 8.0.9 to 10.99: German gives
  "Freiraum-Verstoß … 0,6000 mm", and only the file pin fixes both the words and the decimal comma.
- **Live GUIs.** They use a separate `-gui` root, selected by `enable_api_server=True`, which is also
  English for now. Copying the user's language into it waits until two things stop depending on
  translated text: netlist-import parsing and the window-title classification.
- **Seed restore.** The seed is restored before and after each run.
- **stdout.** No design data or report content is read from kicad-cli stdout or stderr. The
  sandboxed `version` and `--help` probes are exempt.
- **Guard.** A `,` decimal in a DRC/ERC description raises the backend `CliLocaleError`, which core
  maps to `E_SANDBOX_LANGUAGE`.

## D27 — Upstream drafts: the DRC marker `path` on top of !2780; transline fixes issue-first

**Choice (resolved on evidence, 2026-09-24; ANALYSIS-TOOLING §9 decision 8; [decisions/D24-D31.md](decisions/D24-D31.md)):**
- **DRC marker.** The fork carries upstream MR !2780 (flat per-violation `pos`/`layer`) unchanged,
  plus our follow-up adding an optional per-violation `path {start, end}`. There is no drc.v1 bump
  and no ERC change. kiwrap's keys are `cli.drc_json_has_marker_pos` and
  `cli.drc_json_has_marker_path`, and `cli.drc_json_marker` is retired. The parser decides per
  violation, from whether the field is present.
- **Issue-first.** Every draft starts as an issue, because KiCad auto-closes MRs from new
  contributors that have no assigned issue. The transline order:
  - fix 1: the microstrip thickness double count, a bug fix and the first submission;
  - fix 3′: the coupled-stripline label and the stale Z_DIFF at T=0;
  - fix 2: coupled-microstrip thickness, a model change, so an issue draft only.
- **No connectivity export for now.** It is a deferred card with a written trigger.
- **Branches.** All on `base/6dd893f7d1`.
- **Submission.** The owner rewrites the submitted prose (KiCad tool-generated-content policy §4.3).
- **Push guard.** The fork's `public` remote push URL is now `DISABLED` (D19).

The fork's commits carry no AI trailer (D19 as amended 2026-09-24); the `Assisted-by:` trailer
KiCad's policy §4.2 asks for on submission is the owner's call on the day of submission.

## D28 — Stackup sublayers: summed thickness, KiCad's thickness-weighted mean, sublayers kept

**Choice (resolved on evidence, 2026-09-24; ANALYSIS-TOOLING §9 decision 9a; [decisions/D24-D31.md](decisions/D24-D31.md)):** there is one
stackup model, `formats.reports.stackup`, which `formats/board.py` re-exports.
- **Combining sublayers.** For a dielectric slot:
  - thickness is the sum of its sublayers, in integer nm;
  - εr and tan δ are the thickness-weighted arithmetic mean, as KiCad 10.0's tuning-profile
    calculator computes them;
  - material, colour, type and lock come from sublayer 0;
  - `sublayers` lists each physical sublayer when there is more than one.
- **Defaults.** Every parser (board, JSON, CSV, IPC) mirrors KiCad's load defaults and records the
  fields it defaulted (`stackup_defaulted`).
- **Tools.** `kw_pcb_get kinds=['stackup']` reads the file first. `kw_pcb_setup` and the DSN export
  are unchanged. Impedance rules for mixed sublayers belong to `kw_pcb_analyze`.
- **Bug fix.** The change fixes the existing stackup CSV label mismatch.

## D29 — Fab limits: JLCPCB-only curated data and a vendor-floor gate on export

**Choice (resolved on evidence, 2026-09-24; review decision 9; [decisions/D24-D31.md](decisions/D24-D31.md)):**
- **Data.** v1 ships JLCPCB limits only, in `data/fab_limits.json`: seven sets (2-layer at 1, 2,
  2.5, 3.5 and 4.5 oz; multilayer 4-32 at 1 and 2 oz). Each source records its URL, read date and a
  verbatim quote. A manual stdlib refresh script checks for drift.
- **Gate.** With `profile='jlcpcb'`, `kw_fab_export` runs a `fab_limits` gate. It blocks
  (`E_FAB_LIMIT`) only when:
  - the board breaks the vendor's lowest stated floor for its layer class;
  - it breaks a vendor-wide limit (layer count, outline);
  - or the stored DRC holds copper clearance or short errors that are not yet measured.

  Everything else warns.
- **Force.** `force={'reason': ..., 'gates': [...]}` overrides only the named gates. A bare force
  still overrides only `drc_clean`, and receipts add `forced_gates`.

## D30 — `native.geom` lives in `kiwrap.analysis`; import contracts; a content-keyed board-model cache

**Choice (resolved on evidence, 2026-09-24; ANALYSIS-TOOLING §9 decisions 5, 9b, 9c; [decisions/D24-D31.md](decisions/D24-D31.md)):**
- **Environment record.** `kiwrap.analysis.environment.probe_environment()` reports environment
  capabilities, and `kw_env` shows them as `data.environment`. `capability.py` stays keyed on KiCad
  installs.
- **Single import site.** The only `import kiwrap_geom` is in `analysis/engines/native.py`, which
  also holds `GEOM_VERSION` and `GEOM_FINGERPRINT`. The possible reasons are `not_installed`,
  `import_failed`, `version_mismatch`, `fingerprint_mismatch`, `built_with_panic_abort`, `disabled`
  (`KIWRAP_GEOM=pure`), `restart_required` and `panicked`.
- **Without the wheel.** Union, difference and offset refuse with a teaching error whose fix is
  `uv sync --all-extras` (a plain `uv sync` removes extras). Clearance and islands fall back to pure
  Python, tagged degraded.
- **Shared primitives.** Pairwise-distance primitives are written once, in `formats.geometry`.
- **Enforcement.** Six import-linter contracts, ruff TID253 and a subprocess leak test keep optional
  packages inside `kiwrap.analysis`.
- **Board-model cache.** It is `core.state.ModelCache`, keyed on content: the sha256 of the board and
  of the `.kicad_pro`. NTFS stamps kept identical values across two same-size writes in 171 of 200
  trials. It has an LRU byte budget and an idle TTL, and releases a model in slices: a jetson model
  holds about 2.1 GB, and a plain drop paused 6-8 s.

## D31 — kiwrap-geom ships as a committed abi3 wheel pinned by `uv.lock`

**Choice (resolved on evidence, 2026-09-24; ANALYSIS-TOOLING §9 decisions 1 and 9d; [decisions/D24-D31.md](decisions/D24-D31.md)):**
- **The wheel.** `helper/kiwrap-geom/wheel/kiwrap_geom-<v>-cp311-abi3-win_amd64.whl` is tracked in
  git. It is installed through a default dependency group, `native`, with a uv path source.
- **Pin and delivery.** `uv.lock` is the only version and hash pin: no PyPI (D19), no bootstrap
  download, no VSIX copy. A plain `uv run --project <checkout>` installs it on a fresh clone, offline.
- **Cargo.** `helper/Cargo.toml` adds the crate with `default-members = ["kiwrap-live"]` and
  `[profile.pyext] inherits = "release", panic = "unwind"`. Cargo rejects a per-package `panic`
  setting. With `abort`, a panic killed Python with 0xC0000409; with `unwind` it raises
  `PanicException` and the process survives.
- **Build and check.** `scripts/build_geom_wheel.py` is the only writer of the wheel and its pins.
  Its `--check` mode is a blocking gate in pytest and in the ubuntu scripts-lint job.
- **Python.** Only a GIL cp311-abi3 wheel ships; the checkout venv runs CPython 3.14.4.
- **Timing.** It is wired in only when `kiwrap.analysis` first imports it; until then the crate is
  developed with `cargo test -p kiwrap-geom`.
