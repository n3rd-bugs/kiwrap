# kiwrap/data — seed data keyed on KiCad version

This directory and `kiwrap/backends/` are the only places in `src/` allowed to mention a KiCad
version number (CLAUDE.md). Everything else branches on capability keys.

## `kicad_version_matrix.json`

Seed for `kiwrap.capability.capabilities_for()`. Layout:

```json
{
  "schema": 1,
  "versions": {
    "10.0.0": {"inherits": null,     "note": "...", "values": {"<key>": <json scalar or list>, ...}},
    "10.0.6": {"inherits": "10.0.0", "note": "...", "values": {"<changed key>": ...}},
    ...
  },
  "provenance": {"10.0.0": {"<key>": "verified" | "docs", ...}, ...}
}
```

- `versions.<semver>.values` is flat; keys are the dotted names documented in
  `kiwrap/capability.py` (`cli.*`, `swig.*`, `ipc.*`, `formats.*`, `libs.*`, `rules.*`, `setup.*`).
  `inherits` chains are resolved by `kiwrap.capability.load_matrix()`; a child only lists what
  differs from its base.
- `provenance.<semver>.<key>` is `verified` when the value was produced on this machine against
  the installed release (spikes under `spikes/`, docs/VERIFICATION.md) and `docs` when it comes
  from KiCad source/docs/issue trackers only. Every key of a release that was never run here
  (10.0.6, 10.0.7, 11.0.0) is `docs`, including inherited ones.
- Seeding rule (`kiwrap.capability.seed_for`): same major, highest known version `<=` the
  install's; if the install is older than every known release of its major, the lowest known one;
  otherwise the nearest known version from another major. The record carries `seed.version`,
  `seed.exact` and `seed.note`, and runtime probes (`--help` tree, `_pcbnew` module, IPC schema
  file) override the seed's `cli.*`, `swig.available` and `ipc.available`.

### Where the values come from

| Keys | Source |
|---|---|
| `cli.subcommands`, `cli.flags.<subcommand>`, `cli.has_*`, `cli.drc_refill_flags`, `cli.help_exit_code` | Live `kicad-cli … --help` tree probes of 7.0.6 / 8.0.1 / 9.0.5 / 10.0.0 on 2026-09-19 (`kiwrap.capability.probe_help_tree` + `flatten_help_tree`), cross-checked against `spikes/versioning-strategy/cli_flag_drift.txt` (100 of 100 availability/drift rows agree; the spike's only disagreement, `pcb export` "missing" on 10.0, is a spike artifact — the command exists). The conformance test `conformance/tests/t4_cli_help_snapshot.py` re-snapshots these live. |
| `cli.erc_json_pos_scale` | docs/VERIFICATION.md claim 1: 100 on 8.0.1–10.0.6 and on nightlies built before master 407f6293 (verified 2026-09-25 on 10.0.6 and 10.99.0-4083-g6dd893f7d1); 1 from 10.0.7 (10.0 branch fix 6d8e1fea, KiCad #25582, milestone 10.0.7) and 11.0.0 (407f6293), both recorded from the upstream diff (`docs`) until a build is measured. `scripts/ci/refresh_matrix.py` re-probes it on every install (D16 `NIGHTLY_REPROBE_KEYS`), and `kiwrap.backends.cli.erc.run_erc` checks the recorded value against the schematic's own anchors on every run (`degraded: erc_pos_scale_corrected` when it had to use the other scale). |
| `cli.has_ratsnest_export`, `ipc.has_get_ratsnest` | The kiwrap fork patch `ratsnest-export` (docs/UPSTREAM.md G1, `kiwrap_patches.json`): false for every release and the stock nightly (verified 2026-09-19 on 10.99.0-4083: no `pcb export ratsnest`, `GetRatsnest` unhandled). The `--help` tree probe flips the first on a build that carries the patch; the second is derived from it for a headless build and confirmed by the IPC presence probe (`backends.ipc.presence`, run by the first headless session that has a board open; recorded in `kiwrap.ipc_probed`). |
| `cli.gerbers_creates_outdir`, `cli.writes_prl_on_read`, `cli.help_exit_code` | docs/ARCHITECTURE.md §2, `spikes/versioning-strategy/kicad_version_matrix.json`; help exit code re-verified live. |
| `cli.sch_stamp_gate_needs_generator_version` | docs/VERIFICATION.md claim 9. |
| `swig.*` | docs/VERIFICATION.md claims 6 and 7, docs/ARCHITECTURE.md §2 and §14; removal in 11 from dev-docs (docs/research/02-swig-pcbnew.md). |
| `ipc.*` | docs/VERIFICATION.md claims 4 and 10, docs/research/01-ipc-api.md; 10.0.7 `document` field and #24966 fix from claim 4; 11 headless/schematic from dev-docs. |
| `formats.*` | docs/research/07-versioning-strategy.md (first finding) — re-saved boards/libraries per bundled python and `kicad-cli 10 sch upgrade`; `.kicad_pro` meta 1/1/3/3 re-verified against every `share/kicad/{template,demos}/*.kicad_pro` shipped by each install (the 10.0 installer's own templates still carry 9.0-era sch/pcb stamps, so `tests/test_capability.py` only checks shipped stamps are `<=` the matrix for 10 and `==` the maximum for 7/8/9). Master stamps from `sch_file_versions.h` / `pcb_io_kicad_sexpr.h`. |
| `formats.pcb_layer_ids_v2` | docs/research/07-versioning-strategy.md ("8->9: layer IDs renumbered", B.Cu 31 -> 2 with the 4-word `layerselection`); the `kw_project create` board templates for both dialects were verified loading (`pcb drc` / `pcb export svg`) on 7.0.6, 8.0.1, 9.0.5 and 10.0.0. |
| `formats.generator_version_token` | docs/VERIFICATION.md claim 9: `(generator_version "X.Y")` is an "Unknown token" for the 7.0 reader and written/accepted from 8.0 on (the same readers as `cli.sch_stamp_gate_needs_generator_version`). |
| `libs.ships_wrl` | docs/ARCHITECTURE.md §2 ("Ships .wrl 3D models"). |
| `rules.default_min_track_width_mm` | 10: docs/VERIFICATION.md claim 8 (Freerouting neck-downs violated the 0.20 mm board minimum); 9 and older: docs/ARCHITECTURE.md §6. |
| `rules.drc_must_fix`, `rules.drc_review`, `rules.drc_cosmetic_prefix` | docs/ARCHITECTURE.md §11 (must-fix = clearance, shorting_items, tracks_crossing, unconnected_items, hole_clearance, net_conflict, track_width; review = courtyards_overlap, solder_mask_bridge, lib_footprint_issues; cosmetic = `silk_*`). Identical on every version so far; `kw_pcb_drc` reads them from the record. |
| `setup.*` | docs/VERIFICATION.md claim 10 (10.0 wizard needs both prompt flags and all three lib tables; 9.0 gates dialogs on the same flags, no wizard). 7/8 values are documentary. |

### Regenerating

The `cli.*` values are probe output; do not hand-edit them. To refresh after installing a new
release: run `python -m kiwrap.installation --refresh` (which probes every install and caches the
trees under `<storage>/capabilities/`), copy the `cli.*` keys of the new version's cached record
into a new `versions.<semver>` entry with `inherits` pointing at the nearest older release, and
mark the provenance of anything that was not observed live as `docs`.

## `kiwrap_patches.json`

The fork's patch manifest (docs/UPSTREAM.md §3.2–3.3), read by `kiwrap.capability.load_patch_manifest()`:

- `base`: the upstream sha the pinned nightly and the `kiwrap/<sha>` integration branch are on.
- `patches[]`: `id`, `gap` (G-card), `branch`, `issue`, `mr`, `status`, `keys` (capability keys
  that must ALL probe true for `kiwrap.patches` to list the id) and `commands` (IPC commands whose
  T5 outcome may differ from the stock snapshot; `scripts/dev/t5_bench_snapshot.py` tolerates them).
- `builds[]`: the `kiwrap-build/<base sha>-<n>` tags (lightweight: annotated tags break
  `git describe` and every CMake reconfigure) that were built, T5-snapshotted and installed, each
  with `commit` = the BUILT commit (the integration branch's sha; the base's for a pristine build),
  `base`, `branch`, `preset`, `patches` and `snapshot` = `<version>+<commit>[.debug].json`;
  `kiwrap.patched_build` is true for an install whose `commit` (from `<root>/kiwrap-install.json`)
  matches one.

Its digest is part of the capability cache key next to the matrix's, so editing it re-probes every
install on next use.
