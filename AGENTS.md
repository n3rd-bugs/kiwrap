# kiwrap - always-on rules for agents

kiwrap is an MCP server (`kiwrap serve`, registered in `.mcp.json` as `kiwrap`) that drives the
KiCad binaries installed on this machine. KiCad is the judge: every result comes from kicad-cli
ERC/DRC/exports, never from the model's own geometry.

## The stage protocol (Phase 0)

```
kw_env -> kw_project(create) -> kw_sch_check -> kw_pcb_drc -> kw_fab_export
stages: created -> sch_written -> erc_clean -> synced -> placed -> routed -> drc_clean -> exported
```

- `kw_sch_check` ok = zero ERC **errors** (warnings are listed, never gate) -> stage `erc_clean`.
- `kw_pcb_drc` ok = zero **must_fix** violations (clearance, shorting_items, tracks_crossing,
  unconnected_items, hole_clearance, net_conflict, track_width, any other error) -> `drc_clean`.
  `review` (courtyards_overlap, solder_mask_bridge, lib_footprint_issues) and `cosmetic` (silk_*)
  never gate.
- Gates (server-enforced): `kw_pcb_sync` needs `erc_clean`; `kw_pcb_route_auto` needs `placed`;
  `kw_fab_export` needs `drc_clean`. Override only with `force={"reason": "..."}`; the reason is
  written into the snapshot commit message.
- After every mutating step: read the Receipt (`changed`, `validators`, `stage`,
  `next_suggested`, `degraded`), then look at a render before claiming success.

## Hard rules

- **Never edit `.kicad_pro` / `.kicad_sch` / `.kicad_pcb` / `.kicad_sym` / `.kicad_mod` by hand.**
  kiwrap re-hashes the inputs of every stage; a hand edit downgrades the stage and adds
  `external_edit` to `degraded`. Use the kw_* tools (Phase 1 adds `kw_sch_*` editing tools).
- **Positions and sizes are millimetres** everywhere (ERC JSON positions from kicad-cli are
  already corrected).
- The user's real KiCad configuration (`%APPDATA%\kicad`, `~/.config/kicad`) is never written;
  every KiCad process runs against a kiwrap-owned sandbox.
- Never launch a KiCad GUI from tests or tools.
- Errors come back as `isError` results with `{code, message, why, fix, candidates, example}`;
  `kw_help(topic="E_...")` explains every code. Do not retry blindly - apply `fix`.
- `kw_snapshot(action="restore", ref=<sha>)` is the universal undo; every mutating tool commits
  `[kiwrap] <tool> ...` automatically.

## Where things are

- Tool catalog + design: `docs/ARCHITECTURE.md` (§9 agent surface, §10 catalog, §11 receipts/gates).
- Verified KiCad facts: `docs/VERIFICATION.md`; the scripts behind them: `spikes/`.
- The flow skill: `.claude/skills/kiwrap-flow/SKILL.md`.
