# Judge reports

Tally (sum of three judges' 0–10 scores per proposal, names as the judges wrote them): `{"Ratsnest (MVP-first)": 8.2, "Keel (stability and upgradability)": 6.4, "KiForge (agent-ergonomics first)": 8.5, "Keel \u2014 a version-isolated KiCad wrapper for AI agents (stability & upgradability lens)": 8.5, "KiForge \u2014 agent-first KiCad workbench (MCP server + VS Code extension + thin KiCad connect plugin) (agent-ergonomics lens)": 7.5, "Ratsnest: MVP-first KiCad agent wrapper (MVP-first / incremental lens)": 6.5, "KiForge \u2014 agent-first KiCad workbench (agent-ergonomics lens)": 8.5, "Ratsnest \u2014 MVP-first KiCad agent wrapper": 7, "Keel \u2014 version-isolated KiCad wrapper (stability & upgradability lens)": 6.5}`


## Judge 1

Winner: **KiForge (agent-ergonomics first)**

All three proposals reach the same verdicts the digest supports (file-based schematics because KiCad 9/10 have no schematic API and eeschema 10.0.0 crashes on writes; kicad-cli as the only DRC/ERC executor; Freerouting over DSN/SES because PNS is not scriptable; thin exec plugin for socket/token hand-off only; source build deferred as a diagnostic with PDBs/cdb/api.log first), so the ranking turns on which one avoids unverified capabilities and manages Windows risk. KiForge and Ratsnest both build the board stage on the only headless path that was verified on installed 7-10 binaries (SWIG subprocess under each install's python.exe) and keep every other surface to what was reproduced locally. KiForge edges Ratsnest because it turns the digest's documented agent failure modes (self-judged success, wrong-pin labels, tool caps, Codex timeouts, context blow-up) into server-enforced contract features (gates, receipts, teaching errors, async jobs, chunked reads) at low technical risk, exercises kicad-cli on 8/9/10 from week 1, and ships a per-version job table that matches the digest cell by cell; Ratsnest is marginally lower-risk in scope and gets a board sooner, but lacks a job/poll tool for its async routing, relies on skills rather than tool contracts for the loop, and has a tool-count inconsistency. Keel scores lowest despite the best drift/certification design because its core board stage depends on an unverified in-house .kicad_pcb emitter across four drifting epochs (netcodes removed in 10, layer renumbering and 4-word layerselection in 9, KiCad 7/8 rejecting any unknown token) with SWIG demoted to a test oracle, adds an unverified DSN writer and a hand-rolled IPC client in place of the verified kipy path, uses 1600 px images that risk Claude Code's image budget, and takes about 12 weeks to a first routed board. The winner should absorb Keel's planner/degraded-reason reporting, T1-T9 contract tests, certification and emit policy, and Ratsnest's MVP-first milestone discipline and Phase-0 source-build recipe.

### Ratsnest (MVP-first) — 8.2/10

Strengths:
- Every runtime surface it depends on is VERIFIED LOCALLY in the digest: kicad-cli ERC/DRC/exports/render, in-house schematic s-expr writer (0 ERC on 8/9/10), SWIG subprocess board build (FootprintLoad/NETINFO_ITEM/pad.SetNet, ZONE_FILLER after BuildConnectivity, byte-stable SaveBoard), Freerouting 2.4.1 via SWIG DSN/SES with neckdown/fanout off, `pcb drc --refill-zones --save-board` to kill phantom clearance errors.
- Smallest blast radius: KiCad 10 headless only for the first 4 weeks, with a concrete reference board as the acceptance test rerun every phase and an explicit deferred list. This surfaces Windows-specific traps earliest.
- Correct, precisely argued verdicts on the plugin channel (thin exec launcher only, token hand-off to the MCP server, never terminating in VS Code) and the source build (rejected for product, gated diagnostic, PDBs/cdb/api.log first, 10.99 nightly for 11 prototyping).
- SWIG hardening rules match the digest exactly (one BOARD per process, Outline() in place, wxTheAssertHandler nulled, SetFileName, SaveBoard(aSkipSettings=True), never WriteDRCReport on 10).
- Version isolation is data-driven (full-semver capability record, --help-derived flag tables, epoch serializers only for dialects it actually ships) and the 11 seams are named.
- Correct handling of the Windows IPC pitfalls: sandboxed KICAD_CONFIG_HOME with wizard preconditions, single pipe, AS_BUSY retry, no writes while eeschema is open on 10.x (#24966).

Weaknesses:
- Agent-side failure modes that the digest documents (self-judged success, wrong-pin labels, tool caps, Codex 60 s timeouts) are handled by skills rather than enforced server-side; there are no receipts/gates in the tool contract.
- `pcb_autoroute` has an `async` argument but the catalog has no job-status/poll tool, so long routes cannot be resumed under Codex's default 60 s tool timeout.
- Phase 1 exit criterion (3 consecutive unattended spec-to-fab runs by week 4, including writer, library index, SWIG driver, Freerouting pipeline and a 31-tool MCP server) is a schedule risk, though not a technical one.
- KiCad 9 is deferred to Phase 2 and 7/8 to Phase 5, so cross-version drift bugs in the writer are found late.
- Board creation remains SWIG-only until Phase 5 with no interim SWIG-free twin (acceptable, since SWIG is the only verified headless path on 7-10, but it leaves the 11 transition entirely to a later phase).

Factual problems vs. research:
- Internal inconsistency: states 33 tools but the catalog lists 35 rows plus parts_lcsc_import in Phase 2 (36); still under Cursor's 40-tool cap.
- 'WriteDRCReport asserts on 10, hangs on 9' omits the digest's finding that it works on 7/8/9 once wxTheAssertHandler is nulled via ctypes (hangs only without the shim).
- Claim 20 presents VS Code Copilot consuming PNG ImageContent as established; the digest rates that as medium/unverified (verified only for Claude Code).
### Keel (stability and upgradability) — 6.4/10

Strengths:
- Best-thought-out drift strategy: tolerant lens / strict emitter per epoch, oldest-epoch-by-default emit policy, T1-T9 contract tests (especially T3 negative gates and T9 upgrade/pcbnew.py canaries), certification of installs, and a planner that reports provider_used/degraded reasons to the agent.
- CST-first lossless editing with unknown-token preservation, matching the digest's evidence that dataclass libraries corrupt files.
- Correct verdicts on the plugin channel (thin hand-off, reject SWIG server and C++ patch) and on the source build (diagnostic bench, triggers named, nightly install for 11).
- Proper capability negotiation keyed on full semver + commit, sandboxed per-install processes, quirk probing (ERC x100, gerbers outdir, .prl, .wrl).
- SWIG worker rules and per-major shims (EDA_UNITS, VIATYPE ints, IO_MGR vs PCB_IO_MGR, 7's one-arg ImportSpecctraSES) are correct.

Weaknesses:
- Core board stage depends on an UNVERIFIED capability: `board.build` lists File(epoch certified) as the only provider and demotes SWIG FootprintLoad to a test oracle. The digest verified schematic file emission and SWIG board construction, never an in-house from-scratch .kicad_pcb emitter, and the board format drifts hard across 4 epochs (netcode table removed in 10, layer renumbering + 4-word layerselection in 9, footprint child reordering, KiCad 7/8 hard-fail on a single unknown token). This is the largest risk in any of the three proposals and its payoff is small because SWIG works on 7-10 and IPC ImportNetlist/PlaceFootprintFromLibrary arrive headless in 11.
- Own DSN writer is unverified (only the SES reader was prototyped); Freerouting depends on DSN rule/keepout fidelity.
- Discards the verified kipy path (board/editor/project worked on 9.0.5 and 10.0.0) in favour of vendored protos plus a hand-rolled nng client; more work for the same surface.
- Longest path to a routed board: Phase 0 (3 wk) has no schematic or board tools, end-to-end only at week ~12, live sessions at week ~18.
- Read policy that refuses mutations on 'uncertified' files/installs adds friction for exactly the point releases (10.0.1+) users are likely to have.
- Plugin uses runtime `python`, which invokes KiCad's venv+pip provisioning (verified from source only, never observed live); `exec` avoids that machinery.

Factual problems vs. research:
- 'Both KiCad-side crashes encountered ... were fully diagnosed with WER minidumps + cdb + PDBs': only the eeschema BeginCommit crash was cdb-symbolicated; the WriteDRCReport Pgm() assert on 10 was diagnosed from the assert text/faulthandler, not a minidump.
- Images '<= 1600 px' conflicts with the digest guidance for Claude Code (25k-token cap includes images, no file spill; keep renders <= ~1024 px / <100 KB).
- Board creation 'file path' is presented as verified by `pcb drc --schematic-parity`; the digest verified parity only on SWIG-built boards, not on any in-house-emitted board.
### KiForge (agent-ergonomics first) — 8.5/10

Strengths:
- Same fully verified backend split as Ratsnest (file CST for schematics, SWIG subprocess for board build/sync on 7-10, Freerouting+KRT via DSN/SES, kicad-cli as sole ERC/DRC authority, kicad-cli renders + PyMuPDF), with a per-version job table that matches the digest cell by cell (E_UNSUPPORTED ERC on 7, SWIG DRC only on 7, pcbnew standalone for live on 10.x because of #24966, KRT 9/10 only).
- Designs directly against the digest's documented agent failure modes: tools return absolute pin endpoints (kicad-sch-api Y-sign bug), server-enforced stage gates decided by kicad-cli, receipts with KiCad-computed deltas, errors that name the fallback and oldest KiCad with the capability, chunked reads, <=1024 px images, 36 tools under Cursor's 40, async jobs with `kf_job` for Codex's 60 s timeout.
- Thin `KiForge Connect` exec plugin with PCM packaging, loopback hand-off to forge-core (not the VS Code extension), and explicit reasons why it adds discovery/UX but no capability; source build deferred to an upstream track with triggers and a written recipe.
- Version isolation is data-only (full-semver capability map, epoch serializers, CST unknown-token preservation, sandboxed config/documents/cache homes, .wrl->.step rewrite for 10), plus the 11 seams named.
- Evals graded only by KiCad outputs and a 4-version conformance corpus give a repeatable measure of agent success rather than anecdote.
- Phase 0 already exercises kicad-cli on 8/9/10 with corrected coordinates, so Windows traps are hit in weeks 1-2.

Weaknesses:
- End-to-end board arrives at week ~10 (vs Ratsnest's week 4 on KiCad 10 only); the ergonomics layer (receipts, gates, evals, kf_pcb_fix, prompts) is extra engineering before the first fab bundle.
- `kf_pcb_fix` (rip-up/re-route, courtyard nudge) and the eval runners are unproven components; optional but on the roadmap.
- Board creation stays SWIG-only until Phase 4 with no interim SWIG-free twin (same as Ratsnest).
- Some tool signatures (kf_sch_edit ops, kf_intent) are broad; risk of the catalog growing past 40 without discipline.

Factual problems vs. research:
- `connect.exe` as the plugin entrypoint implies a compiled/packaged binary per platform; the digest only shows the exec runtime launching an entrypoint (a .bat/.py launcher suffices and avoids packaging).
- forge-ipc proposes kipy 0.8.0 wrappers plus `_pb2` regenerated per KiCad tag; kipy's wrappers import its own bundled `_pb2`, so mixing the two can break imports (the digest recommends either pinned kipy or raw vendored protos, not both).
- Placement row lists 'KRT seed' under the 7.0/8.0 columns while noting KRT supports only 9/10 formats; the seed is unavailable there.

Ideas to graft from the others:
- From Ratsnest: MVP-first sequencing inside KiForge's phases: land the KiCad-10-only spec-to-fab path (dialect-10 writer, SWIG driver, Freerouting, kicad-cli DRC) as a week-4 milestone before broadening to 8/9 and before kf_pcb_fix/evals; keep the reference 'MCU breakout' board (ESP32-S3 + USB-C + MCP73831 + LDO) as the acceptance test rerun at every phase.
- From Ratsnest: an explicit 'Deferred (with the phase that picks it up)' list and the rule that nothing not needed to ship a board enters Phase 1.
- From Ratsnest: write docs/dev/kicad-source-build.md and docs/dev/debugging-kicad.md in Phase 0 (VS 17.14 update, vcpkg at drive root at build.ps1's pinned commit, kicad-gitlab NuGet cache, swigwin 4.3.1, msvc-win64-debug preset) so the build can start the day a named blocker fires.
- From Keel: the planner concept - each operation declares an ordered provider list with capability predicates and every result carries provider_used and degraded[] reasons, so the agent learns 'ERC unavailable on KiCad 7' from the receipt rather than a skill.
- From Keel: the contract-test suite as named tests T1-T9, in particular T3 negative gates (emit epoch > V is rejected by V; injected unknown token rejected by 7/8), T5 IPC handler snapshot per semver, T6 quirk probes (ERC x100, gerbers outdir, .prl, wizard), and T9 drift canaries (sch/pcb upgrade on a copy + semantic diff, pcbnew.py def/class diff, nightly 11 run); plus a `certify` command that marks an install supported only when they pass.
- From Keel: emit policy 'oldest epoch the user needs, never an uncertified epoch' pinned per project, and a `project_upgrade` tool (kicad-cli 10 sch/pcb/sym/fp upgrade on a copy, semantic diff, ERC/DRC before/after) instead of in-place upgrades.
- From Keel: parse '// Since: x.y.z' comments from vendored api/proto into a per-command Since table for the IPC backend, and vendor the protos per supported KiCad tag (keep kipy only for the verified KiCad/board/project wrappers, or drop it, but do not mix wrappers with regenerated _pb2).
- From Keel: Keel's SES reader / DSN writer as an OPTIONAL SWIG-free twin certified against SWIG's output in Phase 4 (not as the primary path), so the 11 transition for routing does not wait on IPC.
- From Keel: 'nothing outside the drivers package knows a KiCad version number' as a lint rule, and the upgrade budget (<= 1 day per point release, <= 1 week per major) as a stated maintenance target.

## Judge 2

Winner: **Keel — a version-isolated KiCad wrapper for AI agents (stability & upgradability lens)**

All three proposals reach the same, digest-consistent verdicts on the two questions the user raised: the in-KiCad plugin is worth shipping only as a thin socket/token hand-off launcher (PCM-packaged, terminating in the MCP server, never a transport or capability source), and a KiCad source build is not a product dependency but a gated diagnostic, with symbols.kicad.org PDBs, cdb minidump analysis and EnableAPILogging=1 as the first-line tools and a 10.99 nightly install for KiCad-11 prototyping. None contradicts the digest on these points. The proposals differ on the criterion I was asked to weigh most: maintainability across KiCad upgrades and the SWIG->IPC transition. Keel is the only design whose shipped board layer does not depend on SWIG: board build/sync is file-based from footprint definitions (the digest's own recommended workaround), DSN/SES gets a wrapper-owned codec with SWIG as oracle, the IPC provider generates protobufs from vendored KiCad tags instead of pinning the inconsistent kipy wheel, a planner selects providers by capability predicates and reports degradation, and a nine-part certification suite plus explicit upgrade budgets turn drift into failing tests. Its main risks are execution cost (board emitter fidelity across four epochs) and a long runway before the first board, and it has a few overstatements (kicad-cli 7 'no upgrade' ignores sym/fp upgrade; the WriteDRCReport failure was not cdb-diagnosed; principle 7 is not met for zone fill on 7-9 and DRC on 7). KiForge has the best agent contract and equally clean version-isolation data structures, but keeps SWIG as the primary board path on 7-10 with the SWIG-free path only on 11 and pins kipy, so it migrates the entire board hot path in one phase. Ratsnest is the most pragmatic and has excellent SWIG hygiene and exit criteria, but it is the most tightly coupled: all board operations on 9/10 go through SWIG with no board emitter, IPC/own-DSN are deferred to after KiCad 11 GA, kipy is pinned, and only two schematic dialects exist initially, so it scores lowest on the maintainability lens despite being the fastest to a board. Keel wins; the grafts import KiForge's receipts, teaching errors, server-side gates, evals and exec-runtime plugin, and Ratsnest's early vertical-slice milestone, deferred list, image cap and SWIG shim checklist, so that Keel's stability does not come at the cost of agent ergonomics or a first board arriving three months in.

### Keel — a version-isolated KiCad wrapper for AI agents (stability & upgradability lens) — 8.5/10

Strengths:
- Weakest coupling to any KiCad surface: SWIG is optional from day one (file-based board IR + footprint lens for board build/sync, own DSN/SES codec as the SWIG-free routing path, SWIG kept only as a test oracle and for zone fill on 7-9 / DRC on 7). This is the only proposal whose shipped board layer does not have to be rewritten when KiCad 11 removes SWIG.
- IPC provider generates protobufs from vendored api/proto per tag with a parsed '// Since:' table and no kipy dependency — exactly the digest's recommendation given the broken 0.8.0 wheel and the additive-only wire policy.
- Planner with ordered provider lists and predicates (e.g. zones.fill: [Ipc, Cli(--refill-zones), Swig]) plus 'provider_used'/'degraded' in results: version drift becomes data and the agent is told about gaps.
- Two-tier CST + IR with tolerant lenses and strict per-epoch emitters, emit policy defaulting to the oldest epoch the user needs, and refusal to emit uncertified epochs — directly matches the one-directional compatibility facts (older parsers reject a single unknown token).
- Certification suite T1-T9 (round-trip, epoch certification, negative gates, CLI/IPC snapshots, quirk probes, SWIG worker, E2E golden, drift canaries) with an explicit upgrade budget (1 day per point release, 1 week per major) and 'keel certify' — the strongest long-term maintainability story of the three.
- Session abstraction covers GUI IPC (9/10) and headless api-server (11) with one interface; 11-nightly enters the matrix in Phase 3 rather than after GA.
- Plugin verdict is sound and precise (hand-off only, terminating in the MCP daemon, PCM-packaged, rejects SWIG-server and C++ patches with correct reasons); source-build verdict is sound and responsive to the user's direction (bounded one-day diagnostic bench in Phase 3, symbol-server/api.log toolkit first, explicit triggers, nightly install instead of building for 11).

Weaknesses:
- Highest execution risk: hand-written board emitters for four pcb/mod epochs (layer renumbering, 2- vs 4-word layerselection, numeric vs named nets, footprint child order/indentation changes) must reach KiCad-load fidelity before any board ships; the roadmap puts the first headless fab bundle at week ~12 versus week 4 (Ratsnest) / week 10 (KiForge).
- Principle 7 ('every optional surface has a non-optional twin') is not honoured by its own table: zone fill on 7-9 and DRC on 7 have SWIG as the sole provider, and DSN export depends on SWIG until the codec is certified (Phase 2b).
- Image cap of 1600 px is above the digest's ~1024 px / <100 KB guidance for Claude Code's 25k-token limit on image results.
- Connect plugin uses runtime 'python' — KiCad then provisions a per-plugin venv via the user's configured interpreter path; 'exec' (as KiForge/Ratsnest choose) is simpler and avoids that dependency.
- Agent-ergonomics contract (receipts, structured teaching errors, server-enforced gates, idempotency/if_match) is thinner than KiForge's; 'live' and 'project_history' pack many actions into one tool, which is fine for the cap but under-specified.
- Longest total schedule (about 18 weeks) with three foundational weeks before any schematic tool exists.

Factual problems vs. research:
- Claim 3 says kicad-cli 7.0.6 has no 'upgrade'; the digest shows 'sym upgrade' and 'fp upgrade' exist in all four versions (only sch/pcb upgrade are 10-only).
- Section 11.2 states both KiCad-side crashes (eeschema BeginCommit and WriteDRCReport Pgm() assert on 10) were 'fully diagnosed with WER minidumps + cdb + PDBs'; the digest diagnosed only the eeschema crash that way — the WriteDRCReport failure was identified from the wx assert message and faulthandler output, not a symbolicated minidump.
- Describes kipy 0.8.0 as 'internally broken' as the reason to drop it; the digest says the wheel is internally inconsistent but kipy.KiCad/board/project are usable against 9.0.5 and 10.0.0 (a reasonable design choice, but the justification overstates the digest).
### KiForge — agent-first KiCad workbench (MCP server + VS Code extension + thin KiCad connect plugin) (agent-ergonomics lens) — 7.5/10

Strengths:
- Best agent-facing contract: uniform receipts (changed/validators before-after/snapshot/stage/next_suggested), structured errors that teach with candidates and examples, E_UNSUPPORTED naming the oldest KiCad with the capability plus the fallback used, server-enforced stage gates with logged 'force' reasons, keyed upserts, if_match hashes, cursor/detail chunking, async jobs for Codex's 60 s timeout.
- Version isolation is well designed: KiCadInstallation record, capability map keyed on full semver seeded from the verified matrix and re-probed, per-epoch serializers for sch/sym/pcb/mod/pro, tools that never branch on version, and named KiCad-11 seams (session.start, document.open, export, schematic.write, netlist.sync).
- Explicit per-version job table (7/8/9/10/11) that matches the digest on every row, including the pcbnew-standalone-on-10.x rule for live sessions because of #24966 and the 7.0 fallback SES parser.
- Plugin verdict sound and detailed (exec runtime, PCM, loopback POST with 0600 token file, terminates in forge-core so terminal agents keep the live session; rejects SWIG-server and C++ patch with correct reasons). Source-build verdict sound (defer, symbol server + EnableAPILogging first, concrete triggers, upstream-MR track for RunDRC/GetDrcMarkers/ERC scale/#24966).
- KiCad-graded eval suite with tool-call and token budgets, runnable from Claude Code/Copilot CLI/Codex — the only proposal that measures agent success objectively.
- Conformance CI (serialize per epoch, compare that version's ERC/DRC/netlist, negative tests) and a concrete week-1 plan.

Weaknesses:
- Board creation, netlist sync, placement, zones and DSN/SES are SWIG-driver-primary on 7-10; the SWIG-free path exists only on 11 via IPC headless. Coupling is explicit and seam-protected, but the whole board hot path still migrates in Phase 4 rather than being backend-independent now.
- forge-ipc depends on kipy 0.8.0 wrappers while also regenerating _pb2 per tag; kipy's wrappers import their own generated modules, so this mix needs care and still pins a wheel the digest found inconsistent.
- Has per-epoch .kicad_pcb serializers in forge-ir yet does not use them for board build (SWIG instead), leaving that emitter uncertified until it is suddenly needed.
- No own DSN writer scheduled ('can be added'); Freerouting round trip on 7-10 remains SWIG-only.
- Live sessions and Connect land in Phase 3 (weeks 11-14) against a 10.x IPC that crashes on first write when eeschema is open; mitigated by pcbnew-standalone but adds surface before the headless path is hardened.
- Catalog is 36 of Cursor's 40-tool budget, leaving little room for growth.

Factual problems vs. research:
- Claim 3 says 9.0.x/10.0.x IPC 'registers no schematic commands beyond GetOpenDocuments'; the digest's binary enumeration found eeschema 10.0 registers 7 handlers (GetOpenDocuments plus the generic editor set BeginCommit/EndCommit/CreateItems/UpdateItems/DeleteItems/HitTest) — they are registered but crash or are stubs. (The digest itself is inconsistent here, so this is a nuance rather than a hard error.)
- Section 4 lists the KRT placement seed in the KiCad 7.0 row while KRT supports only 9/10 file formats (self-flagged in the same cell, but the row is misleading).
### Ratsnest: MVP-first KiCad agent wrapper (MVP-first / incremental lens) — 6.5/10

Strengths:
- Fastest path to a KiCad-judged board (week 4) using only surfaces verified headless; reference-board acceptance test with '3 consecutive runs, no human edits' is the best exit criterion of the three.
- Correct plugin verdict (thin launcher writing socket/token to a file, never stdout; rejects SWIG-server and C++ handler; long-lived side is the MCP server) and correct source-build verdict (reject for product, gated diagnostic, PDBs/cdb/api.log first, nightly for 11, recipe written in Phase 0).
- Version isolation essentials are present: install discovery incl. WOW6432Node, capability record on full semver with --help probing, tolerant reader, process environment with wrapper-owned KICAD_CONFIG_HOME/DOCUMENTS_HOME, CI assertion that %APPDATA%/kicad is untouched, named backend seams for 11 (SessionStart, BoardBuilder, Exporter, SchematicWriter, Specctra).
- SWIG shim rules are the most complete and directly derived from the verified pitfalls (one BOARD per process, Outline() in place, BuildConnectivity before Fill, SetFileName, aSkipSettings, wx assert null, VIATYPE ints, IO_MGR/PCB_IO_MGR).
- Explicit deferred list with owning phase; 33 tools with headroom under Cursor's cap; images <=1024 px matching the Claude Code limit.

Weaknesses:
- Tightest coupling to a deprecated surface: every board operation on 9/10 (setup, sync, place, route, zones, DSN/SES) runs through SWIG, and boards are 'always written by the target install's own SWIG', so no board emitter exists; the IPC BoardBuilder and own DSN/SES are Phase 5 'post-GA' of KiCad 11 (expected ~early 2027), i.e. the shipped product is on a clock with no incremental migration.
- kipy 0.8.0 pinned as the IPC client rather than vendored protos; combined with SWIG-primary this means two KiCad-version-specific binary dependencies on the hot path.
- Schematic dialects only 10 and 9 initially and KiCad 9 support only in Phase 2; the 'oldest epoch the user needs' policy is stated but cannot be honoured for 7/8 until Phase 5.
- Contract-test story is lighter (corpus round-trip, negative tests, upgrade canary) with no IPC-handler or pcbnew.py symbol snapshots and no certification gate for new point releases.
- Agent contract lacks structured receipts/teaching errors/server-side gates; the verification loop is a protocol the agent must follow rather than one the server enforces.
- Internal inconsistency: Phase 1 says '31 tools', catalog lists 33 (+2 live).

Factual problems vs. research:
- Claim 25 presents exec IPC plugins being 'launched with KICAD_API_SOCKET/KICAD_API_TOKEN' as a verified fact; the digest verified the toolbar button but could not trigger the launch live (env-var hand-off is source-verified only).
- Minor: states the eeschema crash was symbolicated 'in about 30 minutes'; the digest reports roughly half an hour of setup, which is consistent, but the WER minidump path claim ('land in %LOCALAPPDATA%\CrashDumps') is asserted generally while the digest observed it only on this machine.

Ideas to graft from the others:
- From KiForge: the receipt contract on every mutating tool (changed / validators before-after / snapshot / stage / next_suggested / optional image) and structured teaching errors (code, why, fix, candidates, example), including E_UNSUPPORTED that names the oldest KiCad with the capability and the fallback the planner used.
- From KiForge: server-enforced stage gates (sync requires ERC clean; auto-route requires no unplaced/off-board footprints; fab export requires must_fix == 0) with a logged 'force: {reason}' override, plus keyed idempotent upserts and if_match content hashes on mutations.
- From KiForge: a KiCad-graded eval suite (ERC=0, intent diff empty, unrouted=0, must_fix=0, fab bundle present) with tool-call and token budgets, runnable from Claude Code -p, Copilot CLI and Codex, published as an artifact; and kf_pcb_fix-style deterministic fixers (rip-up violating nets + re-route, courtyard nudge, cleanup, refill).
- From KiForge: use runtime 'exec' (not 'python') for the Connect plugin so KiCad does not provision a venv from the user's interpreter path; keep the loopback POST + 0600 token-file pattern; add the pcbnew-standalone-on-10.x rule for live sessions because of #24966.
- From Ratsnest: an early vertical-slice milestone — ship the reference board (ESP32-S3 / USB-C / MCP73831 / LDO, 2-layer, JLCPCB rules) end-to-end by week 4-5 using the SWIG provider as the temporary board builder (the planner already supports provider ordering) while the file-based board emitter is being certified; exit criterion '3 consecutive runs, no human edits, no GUI open'.
- From Ratsnest: the explicit deferred list with owning phase, the CI assertion that %APPDATA%/kicad is byte-unchanged after every test run, the 1024 px / <100 KB image cap for Claude Code, and the complete SWIG shim rule list (one BOARD per process, Outline() in place, BuildConnectivity before Fill, SetFileName, aSkipSettings=True, VIATYPE ints, IO_MGR/PCB_IO_MGR) as the keel-swig-worker checklist.
- From both: lock-then-snapshot semantics around SES import (lock kept tracks before DSN export, git snapshot before import), a kicad_help/kf_help tool to keep option depth out of tools/list, and a datasheet-reading tool (PDF fetch + PyMuPDF text) in the parts group.

## Judge 3

Winner: **KiForge — agent-first KiCad workbench (agent-ergonomics lens)**

The primary criterion is whether an LLM agent actually finishes a board with the tool surface. All three proposals converge on the same verified backends (file-based schematic writer, kicad-cli as the ERC/DRC/export authority, SWIG-or-file board construction, Freerouting via DSN/SES, IPC only for optional live sessions) and all three reach the same, correct verdicts on the in-KiCad plugin channel (thin socket/token hand-off only; reject SWIG-server and C++ patches; terminate in the MCP server, not the extension) and on the source build (defer; PDBs + cdb + api.log first; 10.99 nightly for 11). So the differentiator is the agent-facing contract.

KiForge is the only proposal that designs the loop before the backends: receipts with validator deltas and next_suggested, structured teaching errors with candidates, server-enforced stage gates with logged force reasons, keyed upserts with if_match, cursor-chunked reads, async jobs with kf_job, a combined kf_sch_check (ERC + netlist + intent diff in one call), kf_sch_edit/kf_pcb_route ops for removal, kf_pcb_fix for deterministic repair, and KiCad-graded evals with call/token budgets. Its catalog (36) is neither too coarse nor too fine, and I found no claims that contradict the digest (only a misleading table cell placing KRT under 7/8 with a caveat). Its main cost is scope and a week-10 first board.

Ratsnest has the most de-risked backends and a crisp acceptance board, but its feedback loop lives in skills rather than the server, it lacks an edit/remove primitive for fixing ERC errors, has an async route flag with no job tool, and its catalog count is internally inconsistent (33 vs 35/36 vs 31). It scores 7.0: an agent would probably still finish a board because every step is verified, but with more wasted turns and a higher chance of self-certified success.

Keel is the strongest five-year architecture (CST+IR+epochs, CapabilityRecord, planner, T1-T9 certification, SWIG-free twins) and its catalog is sound, but its agent ergonomics are principle-level only, it exposes no edit/remove or fix tool, and its board build has a single unverified File provider with SWIG relegated to an oracle — the largest completion risk of the three, arriving at week 12. One key claim (SWIG 'importable only under each install's python') and one diagnostic statement overstate the digest. It scores 6.5.

Winner: KiForge, with Keel's planner/certification/emit-policy/SWIG-free-twin ideas and Ratsnest's KiCad-10-first ordering and concrete acceptance board grafted in.

### KiForge — agent-first KiCad workbench (agent-ergonomics lens) — 8.5/10

Strengths:
- Feedback loop is designed as a first-class contract, not a skill: every mutating tool returns a receipt with before/after validator deltas, snapshot sha, stage and next_suggested, plus an image only when geometry changed. This is exactly what an LLM needs to self-correct without re-querying.
- Errors that teach (code, why, fix, candidates, example) and E_UNSUPPORTED naming the oldest KiCad with the capability and the fallback used — converts the most common agent dead-ends (wrong lib_id, missing capability on 7) into one-step recoveries.
- Server-enforced stage gates (sync requires sch_check ok; route_auto requires no unplaced parts; fab_export requires must_fix==0, force with logged reason) — removes the 'model-judged success' failure mode observed across prior art.
- Catalog granularity is right: 36 tools (< Cursor's 40), kf_sch_check merges ERC + netlist + intent diff into one call, kf_lib_search/kf_lib_info collapse symbol/footprint search, kf_sch_edit ops[] and kf_pcb_route ops[] give removal/touch-up primitives that Ratsnest and Keel lack, kf_pcb_fix gives a deterministic repair loop, kf_job covers Codex's 60 s tool timeout.
- Idempotent keyed upserts with if_match hashes, per-project locks, auto git commits, cursor-based chunked reads and region_mm renders — all directly addressing context-budget and concurrency failures.
- Backend choices match the verified digest exactly (SWIG subprocess for board build/sync/DSN/SES on 7-10, kicad-cli as sole DRC/ERC authority, Freerouting REST with rule-derived flags and -mt 1, KRT for placement seed, pcbnew standalone sandboxed on 10.x for live sessions).
- Plugin and source-build verdicts are precise and consistent with the digest: thin exec plugin terminating in forge-core (not the extension), rejects SWIG-server and C++ patch, build deferred with concrete triggers and a machine-specific recipe.
- KiCad-graded evals with tool-call and token budgets, run through claude -p with isolated CLAUDE_CONFIG_DIR — the only proposal that measures agent completion rather than asserting it.

Weaknesses:
- Scope is the largest of the three (11 skills, evals, prompts, datasheet tool, LM-tool facades); first headless spec-to-gerbers milestone lands at week 10, later than Ratsnest's week 4 target.
- Phase 1 exit criteria target 8.0.1/9.0.5/10.0.0 simultaneously for the schematic engine; a KiCad-10-first ordering would reduce risk of the epoch-serializer work stalling the e2e path.
- Capability record still lists comment-style version thresholds (has_render (>=9) etc.) — fine as seed data but must be probed, as the text says elsewhere.
- kf_live has status/attach/launch/highlight/select/undo/run_action but no explicit push/pull (apply file-based results to the open board as one commit / pull GUI edits back), which both other proposals expose.
- The version-by-job table puts 'KRT seed' under the 7.0 and 8.0 columns with a parenthetical caveat; KRT only reads 9/10 formats, so the row is misleading at a glance.

Factual problems vs. research:
- Section 4 table, Placement/Routing rows: KRT is listed under the 7.0 and 8.0 columns (with a '9/10 formats only' caveat). Per the digest KRT supports KiCad 9 and 10 file formats only, so it is not a valid 7/8 backend; the caveat saves it from being false but the table cell is misleading.
- No hard contradictions with the research digest found; claims 1-26 match verified findings.
### Ratsnest — MVP-first KiCad agent wrapper — 7/10

Strengths:
- Every backend choice is the one the digest verified end-to-end on installed binaries: stdlib schematic writer, SWIG subprocess for board build/netlist sync/DSN/SES, Freerouting 2.4.1 on JRE 25 with neckdown/fanout off and -mt 1, kicad-cli drc --refill-zones --save-board, kicad-cli renders + PyMuPDF. Highest technical-de-risk of the three.
- Concrete reference board (ESP32-S3 + USB-C + MCP73831 + LDO, 2-layer JLCPCB) re-run as the acceptance test at every phase, with a strict pass criterion (0 ERC, intent ok, 0 unconnected, 0 must-fix, fab bundle, 3 consecutive runs, no human edits).
- Correct, tight verdicts on the plugin (thin launcher writing socket/token to a file, not stdout; rejects SWIG-server, C++ patch and extension-terminus) and the source build (gated diagnostic, PDBs + cdb + api.log first, 10.99 nightly for 11).
- Explicit deferred list with the phase that picks each item up — makes the scope honest.
- sch_add_symbol returns absolute pin endpoints; sch_verify_intent (netlist vs intent) is on the gate path; must-fix vs review DRC classification matches the digest.
- Version isolation design (install record, full-semver capability flags, epoch tables, tolerant reader, config-home provisioning, SWIG shim rules) is complete and consistent with the digest.

Weaknesses:
- The verify-render-fix loop is described as an 'agent protocol' in section 9 and in skills; gates are not server-enforced and there is no receipt schema, so an agent can still skip validation or self-certify — the failure mode prior art warns about most.
- No general edit/remove tool for schematic items: sch_add_symbol upserts by ref, but there is no way to delete a dangling wire/label or change a property other than re-adding; ERC fix loops (wire_dangling, unconnected_wire_endpoint) have no direct primitive except project_restore.
- pcb_autoroute has an async flag but the catalog has no job/status tool to poll it; Codex's 60 s default tool timeout and Claude Code idle timeouts are not addressed.
- No structured 'errors that teach' or near-miss candidates on library lookups; the agent gets isError text only.
- Tool count is internally inconsistent (says 33, table lists 35 plus parts_lcsc_import in P2 = 36; Phase 1 says 31), which suggests the catalog was not finalised.
- Placement feedback in MVP is agent-supplied coordinates only; KRT seed deferred to Phase 2, so the hardest step for an LLM (placement) has the least assistance in the milestone that matters.
- Four-week MVP including dialect-10 writer, library index, SWIG driver, Freerouting pipeline, renders, MCP server and skills is optimistic.

Factual problems vs. research:
- Tool catalog count: section 0/10/11 state 33 tools and claim 21 says '33 are fully visible', but the table enumerates 35 (kicad_env ... live_apply) and Phase 1 text says 31. Internal inconsistency rather than a digest contradiction; still under Cursor's 40.
- Phase 1 claims 'MCP server (31 tools)' while the catalog shows 35 for the same phase minus two Phase-4 live tools = 33. Same inconsistency.
- No contradictions with the research digest found in the 25 key claims.
### Keel — version-isolated KiCad wrapper (stability & upgradability lens) — 6.5/10

Strengths:
- Best long-term architecture: CST (lossless, byte-identical round trip) + semantic IR + per-epoch strict emitters and tolerant lenses; 'nothing outside keel-drivers knows a version number'; emit policy defaults to the oldest KiCad the user wants to open the project with, pinned in keel.toml.
- CapabilityRecord keyed on semver+commit with probed quirks (ERC x100, gerbers outdir, .prl side file, wrl availability, wizard conditions) and a planner with ordered providers/predicates; every result carries provider_used and degraded reasons so the agent can reason about gaps.
- Certification suite T1-T9 (round-trip, epoch certification, negative gates, CLI/IPC/pcbnew.py snapshots, quirk probes, e2e golden, upgrade canaries) turns drift into failing tests; upgrade budget (1 day/point, 1 week/major) is explicit.
- Only proposal that plans a SWIG-free routing path (Keel DSN writer / SES reader with SWIG as oracle) and a SWIG-free board build before KiCad 11 removes SWIG; vendors protos from KiCad tags instead of the broken kipy 0.8.0 wheel.
- Catalog is well-shaped (36 tools) and includes job_status, project_upgrade with dry-run semantic diff, sch_assign_footprints, fab_bom, and a live tool with push/pull/selection.
- Plugin and source-build verdicts are correct and specific (hand-off plugin with loopback + 0600 token file; bench deferred to Phase 3 with triggers and recipe).

Weaknesses:
- Feedback loop is a principle ('KiCad is the judge'), not a contract: no receipt schema, no server-side gates, no structured teaching errors, no explicit idempotency/if_match semantics — the agent-facing ergonomics are the thinnest of the three despite a comparable catalog.
- board.build planner has only a File provider: the from-scratch .kicad_pcb emitter (footprints embedded from .kicad_mod, numeric net table <10, name-only nets on 10, layer tables, layerselection widths) is unverified in the digest, and if it fails T2 there is no runtime fallback (SWIG is 'oracle only'). This is the single biggest risk to an agent actually finishing a board in Phase 2.
- No edit/remove tool for schematic items (IR ops RemoveItem/SetProperty exist in the engine but are not exposed); no pcb_fix; fix loops rely on re-adding or restoring snapshots.
- First end-to-end board arrives at week 12 (Phase 0 three weeks of harness with six tools, schematic four weeks, board five weeks) — the lens optimises a five-year horizon at the cost of near-term completion evidence.
- Emitters for all four schematic epochs (7/8/9/10) are Phase 1 deliverables, front-loading rarely needed 7/8 dialect work before any board exists.

Factual problems vs. research:
- Key claim 17 states SWIG pcbnew is 'importable only under each install's python'. The digest verified that a system/venv CPython of the same minor (3.11.x) imports KiCad 10's pcbnew when sys.path includes its site-packages and os.add_dll_directory('<install>/bin') is called; only cross-minor (3.12, 3.9 vs 3.11) fails. The subprocess-per-install policy is still the right recommendation, but the claim as written contradicts the digest.
- Section 11.2 says both KiCad-side crashes (eeschema BeginCommit and WriteDRCReport Pgm() assert on 10) were 'fully diagnosed with WER minidumps + cdb + PDBs'. Per the digest only the eeschema crash was symbolicated from a minidump; the WriteDRCReport failure was diagnosed from the wx assert message and faulthandler output. Minor overstatement.

Ideas to graft from the others:
- From Ratsnest: adopt the concrete MVP acceptance board (ESP32-S3-WROOM-1 + USB-C 2.0 + MCP73831 + 3.3 V LDO + buttons + I2C header, ~50x30 mm, 2-layer JLCPCB) as eval task 2, and require '3 consecutive agent runs, no human edits' as the Phase 2 exit criterion.
- From Ratsnest: re-order KiForge's Phase 1/2 to reach spec-to-gerbers on the installed KiCad 10 first (dialect 20260306 only), then certify 9.0.5, then 8; keep the 7/8 schematic dialects and SWIG-DRC-on-7 in a late 'best-effort' phase. This shortens time-to-first-board without changing the tool contract.
- From Ratsnest: the explicit 'Deferred list with pick-up phase' section and the 'boards are always written by the target install's own SWIG so the pcb epoch is inherently right' rule (KiForge already uses the SWIG driver; state the epoch guarantee explicitly and add the sch upgrade-on-a-copy drift canary to CI).
- From Keel: add provider_used and degraded[] to every KiForge receipt, backed by a small planner with ordered provider lists and predicates per operation (e.g. zones.fill: [Ipc(live), Cli(--refill-zones), Swig(ZONE_FILLER)]) so E_UNSUPPORTED and silent fallbacks become visible to the agent.
- From Keel: the certification suite T1-T9 (CST round-trip with ERC/netlist equality, epoch certification per install, negative gates for future stamps/unknown tokens, --help tree and IPC handler snapshots, quirk probes, upgrade canaries) and the 'keel certify' notion that an install is supported only when the suite passes; plus the upgrade budget (1 day per point release, 1 week per major).
- From Keel: emit-policy pinning in the project file (target epoch = oldest KiCad the user wants to open the project with; never emit an uncertified epoch; read-only tools allowed on uncertified installs) and a project_upgrade tool with dry_run semantic diff and ERC/DRC before/after.
- From Keel: plan the SWIG-free twins before KiCad 11 — a wrapper-owned DSN writer / SES reader certified against SWIG as oracle, and a file-based netlist-to-board emitter as a fallback provider (keep SWIG as the primary runtime path on 7-10, but do not let 11 be the first time the file path is exercised).
- From Keel: generate _pb2 from vendored api/proto of each supported KiCad tag with a Since table parsed from '// Since:' comments, rather than depending on the kipy 0.8.0 wheel; and expose live push/pull/selection actions in kf_live (apply file results to the open board as one commit, pull GUI edits back).
- From Keel: the split between tolerant lenses (accept bare hide/(hide yes), tstamp/uuid, fp_text/property, numeric/named nets, both layer tables) and strict per-epoch emitters, and the rule that the core format package has zero runtime dependencies and the SWIG worker is copied into KiCad's interpreter, never imported by the server.
