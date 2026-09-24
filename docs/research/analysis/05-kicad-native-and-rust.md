# KiCad-native computation and Rust inside kiwrap (track: kicad-native-and-rust)

Research for the geometry behind `kw_pcb_analyze` and the location/zone/graphics data items of
[requests/kiwrap-feature-requests.md](../../../requests/kiwrap-feature-requests.md) (items 8-9, 16,
18, 21-39), answering the owner's open question in D23 ("research the best tooling, not just
numpy") for two strategies that are *alternatives* to a Python geometry stack:

- **(A) Let KiCad compute it**: what KiCad's C++ already has, what stock 10.0 / 11 expose, and
  what a fork "analysis job" would cost.
- **(B) Rust inside kiwrap**: a pyo3/maturin extension (or a helper exe, like
  `helper/kiwrap-live`) over `i_overlay` / `clipper2-rust` / `rstar` / `rayon`, measured against
  shapely 2.1 on real boards.

Date: 2026-09-24. Machine: Windows 11, Core Ultra 9 285K (24 threads), 127 GB RAM. Python 3.11
scratch venv (uv), Rust 1.95.0 stable-msvc, KiCad 10.0.6 (stock) and the private fork
`C:\src\kicad` at `kiwrap/6dd893f7d1` (master of 2026-09-17 plus our G1/G6/specctra patches).
`[MEASURED]` = reproduced here on the demo boards; `[SOURCE]` = read in the KiCad tree;
`[DOCS]` = read on the web (URL and date in §9). Every timing is the best of 3 runs in one
process unless stated otherwise. Scratch scripts and raw results:
`%TEMP%\claude\...\scratchpad\tooling\kicad-native-and-rust\` (`extract.py`, `bench.py`,
`degen.py`, `robust.py`, `kicad_native_bench.py`, `run_kicad.py`, `helper_bench.py`, `kwgeo/`,
`kwgeo-cli/`, `results/`). Nothing was installed into the kiwrap env or system Python; every KiCad
process ran in a kiwrap sandbox root under that scratch dir and the user config hash was asserted
unchanged after each run.

---

## 1. Verdict

1. **KiCad already computes almost everything the items ask for, but stock KiCad exports almost
   none of it.** `[SOURCE]`
   - DRC clearance providers compute the *actual* gap, the violation position and, since
     `814f65934f` (2025-10-10, present on the `10.0` branch), the two **closest points** of every
     two-shape violation (`reportTwoShapeGeometry` → `SHAPE::NearestPoints` → `PCB_MARKER::SetPath`).
     The JSON report throws all three away and writes each item's `GetPosition()` instead
     (`common/rc_item.cpp:163-215`). That is item 18's bug, in KiCad.
   - `SHAPE_POLY_SET` is Clipper2 2.0.1 (BSL-1.0) under GPL glue: booleans, inflate, fracture /
     unfracture, `Collide(actual, location)`. `BOARD::ConvertBrdLayerToPolygonalContours` builds
     "all copper of a layer" in one call. `CONNECTIVITY_DATA` has islands and connected items; the
     zone filler has thermal spokes; `common/transline_calculations` has Z0 models (GPL).
2. **A general fork "analysis job" API is not worth building now.** A kicad-cli job that exposes
   union/clearance/islands/thermal/transline would be ~4-6k lines (calibrated on our ratsnest export
   patch, 1,471 lines incl. tests), stays private under D19, is unavailable to every stock user,
   and pays KiCad's board load on every call (12.5 s for vme-wren in the SWIG `LoadBoard`;
   `LoadBoard` hung for 5+ min on the jetson demo). KiCad's own union was also *slower* than the Rust
   engines on plane layers (6.2 s vs 0.06-0.16 s on vme-wren In6.Cu, §3.2). kiwrap needs its own
   engine for stock users regardless. `[MEASURED]`
3. **Two small KiCad-side things are worth it:**
   - *Stock, now:* the T3 temp-copy DRC probe (`.kicad_dru` rules on a copy) as the **oracle** for
     rule-resolved clearances and keepout checks (item 31 `disallow graphic`, item 24), plus the
     `(island yes)` flag that the file format already stores on kept zone islands.
   - *Fork draft (D19):* ~40-80 lines in `drc_report.cpp` / `rc_json_schema.h` that add
     `marker{pos, layer, path_start, path_end}` to each JSON violation. The data exists in 10.0.
     Capability key `cli.drc_json_marker`.
4. **Rust wins decisively over shapely on every real workload, and the prototype built first try.**
   A 443 KB abi3 wheel (one wheel for CPython 3.11+) with `i_overlay` 9.0, `clipper2-rust` 1.2,
   `rstar` 0.13 and `rayon`:
   - stroke + union of one layer: **12-53x faster** than shapely (vme-wren F.Cu 164 ms vs 2.79 s);
   - union per net (islands): **8-28x** (vme-wren F.Cu 39 ms vs 1.09 s);
   - different-net clearance pairs with closest points: **6-988x** (a plane layer: 119 ms vs
     117 s), because the Rust version indexes edges, not whole polygons;
   - union results agree with shapely on shape counts exactly and on area to 0.001 %. Where the
     inputs match KiCad's (video), `clipper2-rust` reproduces KiCad's own `SHAPE_POLY_SET` shape and
     hole counts after inflate. The engines differ only at exactly-touching offsets (§4.4).
     `[MEASURED]`
5. **Recommendation (§8):** one MIT native component, `helper/kiwrap-geom`, a pyo3 abi3 extension
   built with maturin, behind the pure-Python API that the review's T5 proposes and a capability
   key `native.geom` (pure Python stays the fallback). Engine: **`clipper2-rust` for offset/union**
   (identical semantics to KiCad's Clipper2 at exact touches), **`i_overlay` optionally for bulk
   speed** only after its zero-length-stroke gap is covered, **`rstar`** for every proximity query,
   `rayon` for per-net / per-layer parallelism. This is also the answer to the graph track's open
   question ([03-graphs-and-paths.md](03-graphs-and-paths.md) §8): its raster Dijkstra goes in the
   same crate, so there is one native artefact, not two.

---

## 2. Workloads

Extracted with kiwrap's own file model (`BoardFile.load`) into per-layer JSON: tracks and arc
tracks as segments with width (stroked by each engine, round caps), and graphic copper strokes the
same way. Pads (`Pad.polygons()`), via annuli (circles at 0.005 mm max error) and zone
`filled_polygon`s (as written, i.e. fractured) are rings. Every engine gets identical input.
`[MEASURED]`

| Board | Copper layers | Load (kiwrap file model) | Layer used | Segments | Rings | Ring vertices |
|---|---|---|---|---|---|---|
| video | 4 | 1.0 s | In1.Cu (planes) | 69 | 1,724 | 97,352 |
| vme-wren | 12 | 11.4 s | F.Cu (signal) | 10,812 | 10,582 | 137,398 |
| vme-wren | 12 | | In6.Cu (plane) | 0 | 4,906 | 256,255 |
| jetson-agx-thor-baseboard | 10 | 19.6 s | F.Cu (signal) | 7,029 | 6,136 | 98,241 |
| jetson-agx-thor-baseboard | 10 | | In4.Cu (split planes) | 305 | 3,375 | 139,092 |

Caveats, all affecting every engine equally:
- Through vias are put on every copper layer (KiCad may remove unconnected inner annuli), so the
  0.005 mm minimum gap on vme-wren In6.Cu is a workload artefact (fill-to-annulus), not a board
  defect.
- kiwrap's file model does not yet expose footprint copper graphics/text (item 9): KiCad's own
  layer conversion found 5,021 F.Cu shapes on vme-wren where the file-model workload has 4,997.

---

## 3. Strategy A: let KiCad compute it

### 3.1 What KiCad's C++ has, and who can reach it

`[SOURCE]` fork at base `6dd893f7d1`; "10.0" = branch `upstream/10.0` (2026-09-18); "11" = master.

| Capability | Where (KiCad tree) | Stock 10.0 surface | Stock 11 (master) surface | Items |
|---|---|---|---|---|
| Polygon booleans, inflate/deflate, fracture/unfracture | `libs/kimath/src/geometry/shape_poly_set.cpp` (`booleanOp` :753 via `Clipper2Lib::Clipper64`; `Inflate` :1125; `Unfracture` :1858). Clipper2 2.0.1, `thirdparty/clipper2` (BSL-1.0) | SWIG `pcbnew.SHAPE_POLY_SET` only (GPL process) | none (SWIG removed in 11) | 8, 9, 25-27, 31, 32 |
| All copper of one layer as polygons | `BOARD::ConvertBrdLayerToPolygonalContours` (`pcbnew/board.cpp:4247`): tracks, pads, footprint shapes+text, footprint zones, zones, board graphics, all `ERROR_INSIDE` | SWIG only | none | 9, 25-31 |
| Clearance with actual value and position | every DRC provider, e.g. `drc_test_provider_copper_clearance.cpp:268` `Collide(…, &actual, &pos)`; `SHAPE_POLY_SET::Collide` triangulates and keeps the min (`shape_poly_set.cpp:2468`) | `kicad-cli pcb drc`: `actual` only as text in `description` | same | 18, 31, 32 |
| Closest points of a violation | `DRC_TEST_PROVIDER::reportTwoShapeGeometry` (`drc_test_provider.cpp:103-117`) → `NearestPoints` → `PCB_MARKER::SetPath(ptA, ptB)`; commit `814f65934f` 2025-10-10, on the 10.0 branch | **computed but not exported**: `RC_ITEM::GetJsonViolation` writes `mainItem->GetPosition()` (`common/rc_item.cpp:193-215`); schema `include/rc_json_schema.h` has no marker | same | 18 |
| Rule resolution (netclass, custom rules, `physical_clearance`, `disallow`) | `DRC_ENGINE`, `drc_rule_parser.cpp` | `.kicad_dru` + `kicad-cli pcb drc` on a copy (review T3, verified 10.0.6) | same | 23, 24, 31, 32 |
| Neck width, slivers | `drc_test_provider_connection_width.cpp`, `drc_test_provider_sliver_checker.cpp` | DRC checks `connection_width`, `copper_sliver` (rule-driven) | same | 27 (`pour_necks`) |
| Creepage (graph over board edges, Dijkstra) | `drc_test_provider_creepage.cpp`, `drc_creepage_utils.h` | DRC `creepage` rule | same | none directly (not copper geodesic) |
| Via stitching generator + overlap check | `drc_test_provider_via_stitch.cpp` (`f360829c97`, 2026-08-17) | **no** (master only) | DRC | 27 |
| Connected items of an item | `CONNECTIVITY_DATA::GetConnectedItems` | IPC `GetConnectedItems` (10.0) | IPC | 14, 17, 28, 36 |
| Zone islands | `ZONE::IsIsland` / `m_insulatedIslands` (`zone.h:728-738`); `CONNECTIVITY_DATA::FillIsolatedIslandsMap` (:325), `GetZoneIslandConnections` (:701) | **file**: kept islands carry `(island yes)` on their `filled_polygon` (`pcb_io_kicad_sexpr.cpp:3688`); IPC zone `filled_polygons` (no island flag) | same | 8, 27 |
| Thermal spokes | `ZONE_FILLER::buildThermalSpokes` (`zone_filler.cpp:3610`); spoke counting in `drc_test_provider_zone_connections.cpp` | spokes are part of the saved fill; count only as DRC `starved_thermal` text | same | 12, 30 |
| Ratsnest | `CONNECTIVITY_DATA::RunOnUnconnectedEdges` | none | none (our G1 `GetRatsnest` / `pcb export ratsnest` are fork-only) | 28 |
| Transmission lines (microstrip, stripline, CPW, coupled) | `common/transline_calculations/*` (GPL, Transcalc/Qucs authors), used by `panel_setup_tuning_profile_info.cpp` | GUI only | GUI only | 34 |
| Layer plots | `kicad-cli pcb export svg` (10.0: layers, mirror, page modes, drill shape), `pcb export png` (10.99: + dpi, scale) | yes, **no bbox crop** | yes | 38 (crop = SVG `viewBox` / raster crop on our side) |

Facts that shape the choice:
- **SWIG is the only stock surface for geometry ops and it ends with 10.x.** "The current plan is
  to remove the SWIG bindings in KiCad 11.0" `[DOCS]`; the 10.99 nightly here ships no
  `bin/python.exe`. `[MEASURED]`
- **IPC 11 exposes data, not computation**: items, pad polygons (`GetPadShapeAsPolygon`), zone
  fills, connected items, refills and job exports; no booleans, no DRC run, no distance query.
  `[SOURCE]` `api/proto/board/*.proto`.
- **Licensing:** everything above except Clipper2 is GPL-3.0-or-later. Per D8 it may only run as a
  separate KiCad process (kicad-cli, the GPL swig-driver, the IPC server). Porting any of it into
  the MIT core is excluded; the review's T9 already says so for transline.

### 3.2 Measured: KiCad's own engine on the same job

`BOARD::ConvertBrdLayerToPolygonalContours` + `SHAPE_POLY_SET::Simplify()` (= Clipper2 union,
NonZero) + `Inflate(0.1 mm, ROUND_ALL_CORNERS, 5 µm)`, run by KiCad 10.0.6's own python in a
sandbox root (`kicad_native_bench.py`, measurement only; GPL process). The board's own
`m_MaxError` applies (video 10 µm, vme-wren 5 µm), so vertex counts differ a little from §4.
`[MEASURED]`

| Board / layer | LoadBoard | BuildConnectivity | Layer → polys | Union | Inflate 0.1 | Union shapes / holes | Inflated shapes / holes |
|---|---|---|---|---|---|---|---|
| video F.Cu | 0.56 s | 0.10 s | 18 ms | 80 ms | 109 ms | 1,352 / 0 | 1,256 / 2 |
| video In1.Cu | | | 3 ms | 102 ms | 147 ms | 1,535 / 1,334 | 1,535 / 1,362 |
| vme-wren F.Cu | 12.5 s | 0.40 s | 80 ms | 349 ms | 621 ms | 5,021 / 21 | 1,784 / 2,579 |
| vme-wren In6.Cu | | | 18 ms | **6,219 ms** | 667 ms | 4,740 / 4,308 | 7 / 87 |
| jetson (any) | **hung** in `LoadBoard` (faulthandler dump after 300 s; ~2 s CPU in 17 min) | | | | | | |

- KiCad agrees with the Rust `clipper2-rust` engine on topology wherever the inputs match: video
  F.Cu inflated 1,256 shapes / 2 holes in both, and video In1.Cu 1,535 / 1,362 in both (§4.3).
- The 6.2 s union on the plane layer is KiCad-specific overhead around the same Clipper2 core. The
  pure-Rust port did that union in 0.16 s and `i_overlay` in 0.06 s. Not profiled; candidates are
  `splitCollinearOutlines()` and the arc-preserving Z callback in `booleanOp`.
- The load dominates everything. A KiCad-side analysis only pays off when it runs in a process
  that already holds the board (a live GUI via IPC, or a persistent headless api-server, which is
  our G6 fork patch).

### 3.3 Stock kicad-cli as the oracle (DRC probe cost)

`kicad-cli pcb drc --format json --severity-all` in a sandbox root, 10.0.6. This is the per-call
price of the review's T3 "KiCad as the instrument" runner on big boards. `[MEASURED]`

| Board | DRC, fills as saved | DRC with `--refill-zones` | Violations (as saved / refilled) |
|---|---|---|---|
| video | 3.5 s | 6.5 s | 129 / 129 |
| jetson-agx-thor-baseboard | 38.2 s | 69.2 s | 578 / 222 |
| vme-wren | 85.2 s | 221.7 s | 610 / 446 |

- kicad-cli loaded the jetson board without trouble, although SWIG `LoadBoard` hung on it (§3.2).
- The JSON confirms item 18 on stock 10.0.6. Example: a vme-wren `connection_width` violation
  ("actual 0.0790 mm", Bottom Layer) reports only the two vias' centres as positions. The neck
  location KiCad computed for its marker is not in the file.
- One probe run therefore costs 3.5-222 s on these boards. The probe is right for
  **batch** verdicts: one run with many generated rules, then read all results. It is wrong for
  interactive or per-point queries such as `probe_via` at 200 candidate points, or `measure(a, b)`
  in an agent loop. Those need an in-process index (§4).

### 3.4 What an "analysis job" API would look like, and cost

Shape (fork, D17 tier 3, drafts only under D19):

```text
kicad-cli pcb analyze --request req.json --output res.json [--refill-zones] board.kicad_pcb
IPC:      RunBoardAnalysis{ header, request_json }  -> AnalysisResponse{ result_json }   (11, live/headless)

req.json = { "version": 1, "ops": [
  {"op": "copper",    "layer": "F.Cu", "nets": ["GND"], "inflate_mm": 0.1},      -> polygons with holes, islands
  {"op": "clearance", "a": "<uuid|ref.pad>", "b": "<uuid|net:VBUS>", "layer": "F.Cu"},
                                                   -> actual_mm, required_mm (rule-resolved), closest points
  {"op": "markers"},                               -> every DRC marker with pos, layer, path (ptA, ptB)
  {"op": "islands",   "zone": "<uuid>"},           -> per island: layer, area, connected pads/vias
  {"op": "thermals",  "pad": "U8.10"},             -> spokes achieved, spoke width, contact area
  {"op": "transline", "net": "RF_OUT"}             -> Z0 / Zdiff per segment from the stackup
]}
```

| Piece | Implementation inside KiCad | Size estimate |
|---|---|---|
| job plumbing (CLI command, job class, JSON schema, `pcbnew_jobs_handler` entry) | like `pcb export ratsnest` (`5d40bcb51c`: 18 files, 1,471 lines incl. 286-line unit test, 168-line schema) | ~800 |
| `copper` | `ConvertBrdLayerToPolygonalContours`, per-net filter, `Simplify`, `Inflate`, `Unfracture` | ~250 |
| `clearance` | resolve items, `DRC_ENGINE::EvalRules(CLEARANCE_CONSTRAINT)`, `GetEffectiveShape()->Collide(…,&actual,&pos)`, `NearestPoints` | ~400 |
| `markers` | run `DRC_ENGINE`, serialise `PCB_MARKER` pos/layer/path | ~150 (or the 40-80-line JSON patch to the existing report, §1.3) |
| `islands`, `thermals` | `FillIsolatedIslandsMap`, `GetZoneIslandConnections`; spoke count from the zone-connection DRC provider's logic | ~500 |
| `transline` | stackup → `TRANSLINE_CALCULATION_BASE` subclasses | ~400 |
| tests (qa/tests/cli, qa/tests/pcbnew) | same pattern as the ratsnest patch | ~1,500-2,500 |
| **Total** | | **~4-6k lines, 2-3 weeks, plus a rebase per KiCad master bump** |

Why not now:
- **Reach.** Stock users (10.0.x today, 11 later) get none of it until upstream merges it, and D19
  forbids submitting before the end-to-end flow works. kiwrap must ship its own engine for them
  anyway, so the fork job would be a second implementation of the same checks.
- **Latency.** Each kicad-cli call pays the board load (§3.2-3.3), 10-100x the Rust computation.
  Only an IPC variant inside a live session amortises that.
- **Semantics.** Where KiCad's answer matters (rule resolution, fills, spoke results), the stock
  DRC probe already gives it on a copy. Only the marker geometry is missing, and that patch is
  tiny.

What to do instead: draft the `marker` JSON patch now (next to G1 in docs/UPSTREAM.md). Keep
`analyze` as a later option, for when 11's headless api-server makes a persistent KiCad process
cheap.

---

## 4. Strategy B: Rust inside kiwrap

### 4.1 Candidates

Licence = what the repository's LICENSE files say (read via the GitHub API / crate sources on
2026-09-24; the GitHub "license" badge shows only Apache-2.0 for dual-licensed repos).

| Crate | Version (date) | Licence (files) | What it gives | Maintenance | Robustness model | Python / Rust |
|---|---|---|---|---|---|---|
| **i_overlay** | 9.0.0 (2026-09-19) | MIT OR Apache-2.0 (`LICENSE-MIT`, `LICENSE-APACHE`) | booleans with holes, fill rules, stroke (caps/joins) and outline offset, predicates, clip/slice, hierarchy; optional rayon | very active (100+ commits in 6 months), but one author (913 of ~940 commits); 4.1M recent downloads (it is `geo`'s boolean engine); **major-version churn**: v9 is 5 days old and `geo` 0.33 still pins 4.5 | integer core (i32/i64 snap-rounding); float API auto-scales | Rust; no official Python binding |
| **clipper2-rust** | 1.2.0 (2026-09-18) | BSL-1.0 (`LICENSE`) | full Clipper2 port: booleans, offsets (Round/Square/Miter, open-path end types), PolyTree, Minkowski, rect clip | young (first release 2026-02-07), one main author, 27 stars, 60k recent downloads; README: "created … using Claude", "457 tests… exact behavioral match with C++" | Clipper2's i64 integer core; `#![forbid(unsafe_code)]` | Rust |
| clipper2 (tirithen) | 0.6.0 (2026-05-06) | MIT OR Apache-2.0 wrapper; bundles C++ Clipper2 2.0.1 (BSL-1.0) | same as C++ Clipper2, i.e. KiCad's engine | moderate | same as KiCad | Rust (needs a C++ compiler); not benchmarked (its safe `inflate` lacks an arc tolerance; KiCad's own numbers in §3.2 stand in for C++ Clipper2) |
| geo | 0.33.1 (2026-04-20) | MIT OR Apache-2.0 | geometry types, `BooleanOps` (i_overlay 4.5), `Buffer`, distance, simplification | very active, georust org | via i_overlay 4.5 | Rust; no Python binding |
| **rstar** | 0.13.0 (2026-05-24) | MIT OR Apache-2.0 | R*-tree: bulk load, envelope and nearest-neighbour queries | active, georust org, 13.6M recent downloads | exact float comparisons only | Rust |
| petgraph | 0.8.3 (2025-09-30) | MIT OR Apache-2.0 | graphs, Dijkstra, components | active | n/a | Rust (graph track: not needed) |
| rayon | 1.x | MIT OR Apache-2.0 | data parallelism | active | n/a | Rust |
| **pyo3** | 0.29.2 (2026-08-05) | MIT OR Apache-2.0 | Rust ⇄ CPython bindings, abi3 | very active (16k stars) | n/a | both |
| **maturin** | 1.15.0 (2026-08-24) | MIT OR Apache-2.0 (`license-mit`, `license-apache`) | builds pyo3 wheels (abi3), works as a PEP 517 backend under uv | very active | n/a | build tool |
| shapely (reference) | 2.1.2 (2025-09-24), GEOS 3.13.1 bundled | BSD-3-Clause; GEOS is LGPL-2.1, shipped as separate DLLs (`shapely.libs/geos_c-….dll`, `geos-….dll`) | everything GEOS does | active | GEOS: float, "invalid input" semantics | Python; Windows wheels cp310-cp314 win_amd64 and win32 on PyPI |

### 4.2 The prototype

`kwgeo` (pyo3 0.29 `abi3-py311`, maturin 1.15, `i_overlay` 9.0 with `allow_multithreading`,
`clipper2-rust` 1.2, `rstar` 0.13, `rayon`; MIT). It exposes three functions that take **flat
`array.array` buffers**, so no numpy is needed on the Python side:
- `union_layer(engine, segs, coords, offs, nets, inflate_mm, max_err_mm)`: strokes the tracks
  (round caps, width groups), orients rings CCW, unions everything (NonZero), optionally inflates
  the union, and returns counts, area and timings.
- `union_per_net(engine, …, parallel)`: the same per net, returning islands per net, serial or
  with rayon.
- `clearance_pairs(…, limit_mm)`: builds an rstar tree over *edges* (each stroke segment keeps its
  half-width) and returns, for every different-net item pair closer than `limit_mm`, the minimum
  gap and the two closest points (parallel over edges).

Build: `maturin build --release` took 46 s cold (LTO fat, codegen-units 1) with no errors on the
first try. The result is a 443 KB wheel (`kwgeo-0.1.0-cp311-abi3-win_amd64.whl`) whose `.pyd` is
977 KB unstripped. The shapely + numpy reference install is ~49 MB (shapely 5.3 MB incl. GEOS DLLs,
numpy 44 MB). A helper-exe twin (`kwgeo-cli`, stdin/stdout binary protocol) is a 587 KB exe.
`[MEASURED]`

### 4.3 Measurements

**Stroke + union of one layer, then inflate the union by 0.1 mm** (max chord error 0.005 mm for
every engine; shapely uses `buffer(quad_segs)` computed from the same error): `[MEASURED]`

| Board / layer | i_overlay stroke+union / inflate | clipper2-rust stroke+union / inflate | shapely union / inflate | Union speed-up, i_overlay vs shapely |
|---|---|---|---|---|
| video In1.Cu | 28 / 114 ms | 79 / 331 ms | 421 / 266 ms | 15x |
| vme-wren F.Cu | 164 / 216 ms | 545 / 877 ms | 2,789 / 3,958 ms | 17x |
| vme-wren In6.Cu | 62 / 214 ms | 159 / 1,067 ms | 1,744 / 793 ms | 28x |
| jetson F.Cu | 92 / 161 ms | 293 / 566 ms | 1,083 / 2,101 ms | 12x |
| jetson In4.Cu | 44 / 145 ms | 167 / 1,035 ms | 2,318 / 1,759 ms | 53x |

Getting the data across costs 11-34 ms (building the `array.array`s from Python lists). Building
shapely's geometries costs 15-151 ms. The Rust side's buffer copy is 1-3 ms.

**Agreement** (shapes / holes; area):

| Board / layer | Union: i_overlay, clipper2-rust, shapely | Inflated: i_overlay, clipper2-rust, shapely (shapes) | Union area mm² (io / c2r / shp) |
|---|---|---|---|
| video In1.Cu | 1535/1334, 1535/1334, 1535/1334 | 1535/1369, 1535/1362, 1535 | 23359.00 / 23359.00 / 23359.00 |
| vme-wren F.Cu | 4997/5, 4997/5, 4997/5 | 2352/1959, 1769/2548, 1771 | 7678.51 / 7678.49 / 7678.58 |
| vme-wren In6.Cu | 4747/4307, 4747/4290, 4747/4308 | 14/95, 14/88, 14 | 34707.88 / 34707.88 / 34707.88 |
| jetson F.Cu | 2957/54, 2957/54, 2957/54 | 1383/824, 1339/932, 1339 | 5922.01 / 5922.08 / 5922.04 |
| jetson In4.Cu | 2792/1503, 2792/1501, 2792/1504 | 45/1490, 45/1476, 45 | 16815.38 / 16815.39 / 16815.38 |

Union results agree exactly on shape counts, and on area to 0.001 %. Hole counts differ by up to 17
on plane layers, from pinhole slivers between fractured fills. The inflate disagreement is explained
in §4.4.

**Union per net → islands** (the building block of items 8, 27, 28 and the ratsnest estimate):

| Board / layer | Nets | i_overlay serial / rayon | clipper2-rust serial / rayon | shapely serial / 24 threads | Islands (all engines) |
|---|---|---|---|---|---|
| video In1.Cu | 442 | 20 / 15 ms | 41 / 37 ms | 219 / 185 ms | 1,535 |
| vme-wren F.Cu | 1,767 | 134 / 39 ms | 100 / 45 ms | 1,090 / 401 ms | 4,997 |
| vme-wren In6.Cu | 1,058 | 47 / 34 ms | 89 / 68 ms | 928 / 642 ms | 4,747 |
| jetson F.Cu | 970 | 64 / 16 ms | 56 / 22 ms | 610 / 193 ms | 2,969 |
| jetson In4.Cu | 566 | 57 / 26 ms | 96 / 35 ms | 904 / 323 ms | 2,792 |

**Different-net clearance pairs < 0.2 mm with closest points** (items 18, 31, 32 building
block). Rust: rstar over edges plus exact segment-segment distance. Shapely: `STRtree.query(dwithin)`
+ `distance` + `shortest_line` over whole geometries:

| Board / layer | rstar: pairs / min gap / time | shapely: pairs / min gap / time | Speed-up |
|---|---|---|---|
| video In1.Cu | 0 / - / 28 ms | 0 / - / 2,646 ms | 95x |
| vme-wren F.Cu | 7,823 / 0.1005 / 69 ms | 7,819 / 0.1005 / 446 ms | 6x |
| vme-wren In6.Cu | 5,131 / 0.00506 / 119 ms | 5,131 / 0.00506 / 117,332 ms | 988x |
| jetson F.Cu | 6,870 / 0.1000 / 40 ms | 6,744 / 0.0 / 431 ms | 11x |
| jetson In4.Cu | 3,929 / 0.0455 / 108 ms | 3,932 / 0.0455 / 20,862 ms | 194x |

- The pair counts differ by < 2 % at the threshold: exact capsules against shapely's polygonised
  caps.
- On jetson F.Cu shapely's 0.0 is a netless graphic that *contains* a pad. The edge-only Rust
  query cannot see containment. A production version needs one point-in-polygon test per candidate
  pair, which is cheap with the same tree.
- shapely's cost explodes on plane layers because every candidate pair computes the distance to a
  100k-vertex fill. An edge index is the right structure whichever engine is chosen.

**Helper-exe transport** (`kwgeo-cli`, persistent process, binary frames over stdin): a 2.8 MB
(vme-wren F.Cu) or 4.1 MB (In6.Cu) request adds 10-20 ms of pipe time over the in-process call.
Spawning costs 11-21 ms (155 ms on the very first launch of a new exe, while it is scanned).
`[MEASURED]` These round trips ran concurrently with a KiCad benchmark, so compare their overhead,
not their absolute compute time.

### 4.4 Robustness

Hand-made degenerate inputs (`robust.py`): `[MEASURED]`

| Case | i_overlay 9.0 | clipper2-rust 1.2 | shapely 2.1.2 / GEOS 3.13.1 |
|---|---|---|---|
| identical squares | 1 shape, area 1 | same | same |
| shared edge | 1 shape, area 2 | same | same |
| vertex touch | 2 shapes | 2 shapes | 2 shapes |
| KiCad-fractured ring (hole joined by a zero-width slit) | **1 shape + 1 hole** (unfractured for free) | **1 shape + 1 hole** | 1 shape, **0 holes** (input is "invalid"; slit kept) |
| zero-length track, w = 0.2 | **nothing** (degenerate stroke dropped) | disc, area 0.0294 | disc, 0.0312 |
| zero-area ring + square | square only | square only | square only |
| squares 1e-9 mm apart | merged (integer snap) | merged | 2 shapes |

Real-board degeneracy (`degen.py`, jetson F.Cu): inflating the union by exactly half the dominant
clearance makes thousands of items **touch exactly**. The engines then disagree on which touches
merge:

| Inflate | i_overlay shapes/holes | clipper2-rust | shapely |
|---|---|---|---|
| 0.0999 mm | 1397 / 782 | 1397 / 779 | 1396 / 775 |
| **0.1000 mm** | **1383 / 824** | **1339 / 932** | **1339 / 923** |
| 0.1001 mm | 1335 / 1003 | 1335 / 940 | 1335 / 941 |

Consequences for kiwrap:
- Never decide "clearance violated" by *inflate-and-overlap* at the exact rule value. Use a distance
  with an explicit tolerance, as KiCad's DRC does. Inflation is for rendering and area metrics.
- Clipper2 (in KiCad and in the Rust port) and GEOS make the same merge decisions. i_overlay rounds
  differently at exact contact.
- i_overlay drops zero-length strokes. KiCad boards contain zero-length tracks, so emit a disc
  for them before calling it.
- Both integer engines "unfracture" KiCad's zone fills correctly. That is item 8's
  `holes (not fractured)` in one call; GEOS needs `make_valid` first.

### 4.5 Build and distribution cost

- **Toolchain:** Rust stable-msvc plus MSVC build tools. Both are already required by
  `helper/kiwrap-live`. No C/C++ is needed for `i_overlay`, `clipper2-rust`, `rstar`, `rayon` or
  `pyo3`: all pure Rust.
- **One wheel for all supported Pythons:** pyo3's `abi3-py311` produces a `cp311-abi3-win_amd64`
  wheel that loads on every GIL-enabled CPython ≥ 3.11 `[DOCS]` (maturin bindings guide). D18 means
  a single platform, so there is exactly one artefact.
- **How users get it.** kiwrap runs as `uv run --project <checkout>` (EXTENSION.md §2) and is not
  on PyPI (D19). A maturin workspace member would be *built from source on the user's machine*,
  which would need Rust; do not do that. Use the precedent that already exists for `kicad-python`:
  CI builds the wheel once into a wheelhouse, and `uv sync` installs from it. The alternative is
  to ship `kiwrap-geom.exe` in the VSIX next to `kiwrap-live.exe`
  (`scripts/build_extension.ps1`) and talk to it over stdio.
- **kiwrap already depends on compiled wheels**, so "pure Python" is only a property of
  `kiwrap.formats`, not of the distribution: `pydantic-core` (itself pyo3/maturin), `pynng`,
  `zstandard` and `protobuf` are in `uv.lock`.
- **pyo3 wheel vs helper exe:**

  | | pyo3 abi3 wheel | helper exe (stdio) |
  |---|---|---|
  | per-call overhead | ~0 (buffer copy 1-3 ms) | 10-20 ms per 3-4 MB request, plus 11-21 ms spawn unless persistent |
  | state across calls (rstar index of a board kept between `measure`/`probe_via` calls) | natural (`#[pyclass]`) | needs a session protocol |
  | ABI coupling | abi3 (none within 3.11+) | none |
  | crash isolation | a Rust panic becomes a Python exception (keep `panic = "unwind"` in this crate) | process boundary |
  | ships via | wheelhouse + `uv sync` | VSIX `extension/bin/` + a path handed to the server |

  Recommendation: **pyo3 wheel**. The chatty queries (probe_via at N points, measure a,b, nearest
  via) want a persistent in-process index, and the review's `kw_pcb_analyze` runs in the server
  process.
- **Import rule:** `kiwrap.formats` stays zero-dependency. The accelerator is imported only by
  `kiwrap.core` / a new `kiwrap.native` shim that selects the backend by capability key, so
  `lint-imports` does not change for `formats`.

### 4.6 Licensing

- `i_overlay`, `rstar`, `rayon`, `pyo3`, `geo`, `petgraph` and `maturin` are dual MIT/Apache-2.0.
  They are fine to link into an MIT distribution; keep their licence texts in the wheel's
  `THIRD-PARTY` file (`cargo about` or `cargo deny` can generate it).
- `clipper2-rust` and the C++ Clipper2 inside `clipper2` are Boost-1.0. BSL requires the notice
  in copies of the software "unless such copies or derivative works are solely in the form of
  machine-executable object code". A binary wheel is therefore unencumbered; include the notice
  anyway.
- `shapely` is BSD-3. GEOS is LGPL-2.1, shipped as separate, unmodified DLLs that shapely loads
  dynamically. That satisfies LGPL relinking, provided the LGPL text and the source offer
  travel with any bundled copy. It is acceptable under D8, but it is the only copyleft piece in
  the Python route; the Rust route has none.
- KiCad code (`SHAPE_POLY_SET`, DRC, connectivity, transline) is GPL-3.0-or-later. It may only be
  *run* (kicad-cli, the swig-driver process, IPC) and never ported into kiwrap-geom. Implement
  Clipper2-equivalent behaviour by *using* Clipper2 (BSL), not by translating KiCad's glue.

---

## 5. Which items each strategy serves

| Item | Needs | KiCad stock | KiCad fork | Rust engine |
|---|---|---|---|---|
| 8 zones with holes, islands, area | unfracture, per-net union, island flag | `(island yes)` in file; fills in file/IPC | `islands` op | yes: unfracture + area + contained vias (rstar) |
| 9 graphics inflated by stroke/2 | stroke union | SWIG 10 only | `copper` op | yes |
| 16 k nearest vias | kNN | none | none | rstar `nearest_neighbor_iter` |
| 18 violation location + closest points | marker path | computed, not exported | **JSON marker patch (40-80 lines)** | recompute from the pair + tolerance-checked `actual` |
| 21 by-design hints | net-tie / graphics bridging nets | DRC text | - | union per net + netless copper overlaps |
| 24 fab audit | rule-resolved checks | DRC probe with generated rules | - | - |
| 25-26 return path, plane slots | antipad union, segment-over-void clipping | none | - | yes (union, clip polyline by polygon) |
| 27 stitching, gnd_reach, necks, islands | raster geodesic, erosion, per-net union | `connection_width` / `copper_sliver` DRC | - | yes (+ graph track's raster Dijkstra in the same crate) |
| 28-30, 35-36 paths, loops, pad return, current | copper graph contacts | IPC `GetConnectedItems` (items only) | ratsnest (G1) | contact detection = union/edge index |
| 31 measure, region/antenna clearance, keepout audit | distance + closest points; region queries | `physical_clearance` / `disallow graphic` on a copy | `clearance` op | rstar edge index (ms) |
| 32 probe_via | disc-vs-copper queries at many points | DRC probe per candidate (slow) | - | rstar (µs per point) |
| 33-34 coupling, Z0 | parallel-run extraction; closed-form models | GPL transline, GUI only | `transline` op | parallel-run via rstar; Z0 models from papers (T9) in Python |
| 37 paste/mask coverage | layer booleans | plots | - | yes |
| 38 crop/px_per_mm | plot + crop | `pcb export svg/png`; no bbox | - | not needed (crop the SVG `viewBox`) |

---

## 6. Risks

- **i_overlay API churn:** 9.0 shipped 5 days ago and README examples still show `^8.1`. Pin the
  exact version in `helper/Cargo.lock` and wrap it in one module.
- **clipper2-rust maturity:** a 7-month-old AI-assisted port with one maintainer. Mitigation: its
  results matched KiCad's C++ Clipper2 topology on the measured layers, and the tirithen `clipper2`
  crate (the C++ original) is a drop-in fallback at the cost of a C++ build step.
- **Two engines are more surface to maintain.** Start with one (clipper2-rust); add i_overlay only
  where the 3-5x matters (stroke/union of whole boards in interactive loops).
- **The fixture gap:** the file model lacks footprint copper graphics and text (item 9), so
  file-tier copper differs from KiCad's (5,021 vs 4,997 F.Cu shapes on vme-wren). A parity test
  against the SWIG layer dump (KiCad 10 only) belongs in `conformance/`.
- **SWIG `LoadBoard` hang on jetson (10.0.6):** not investigated. Another reason not to put
  analysis on SWIG.

---

## 7. Cost summary

| Option | Build effort | Per-call latency (vme-wren) | Stock users | Licence risk |
|---|---|---|---|---|
| A1 fork `pcb analyze` job | ~4-6k lines, 2-3 weeks + rebases | 12.5 s load + compute | no (D19) | GPL, separate process: OK |
| A2 JSON marker patch | 40-80 lines + test, 1-2 days | none (inside the existing DRC run) | no until upstream | OK |
| A3 stock DRC probe on a copy (T3) | already planned (review phase 4) | 38-222 s per DRC run on the big boards (vme-wren 85 s without refill) | **yes** | OK |
| B pyo3 `kiwrap-geom` | ~2-3k lines Rust + bindings + CI wheel job, 1.5-2 weeks for the §5 primitives | 40-400 ms per layer op | **yes** | MIT/Apache/BSL only |
| Python + shapely (reference) | smaller | 0.4-117 s per layer op | yes | LGPL DLLs (dynamic) |

---

## 8. Recommendation

1. **Build `helper/kiwrap-geom`** (MIT, second member of the existing Cargo workspace): pyo3
   0.29 `abi3-py311` + maturin 1.15.
   - Primitives: stroke/union/offset/unfracture/area (clipper2-rust 1.2), an rstar edge index
     with gap + closest points + containment, per-net/per-layer rayon parallelism, polyline clipping.
     Add the graph track's implicit-grid raster Dijkstra/erosion so there is one native artefact.
   - Python API: exactly the T5 vector kit's (`kiwrap.native` facade). The pure-Python kit is the
     fallback, gated by capability key `native.geom`.
   - Ship a CI-built wheel through a wheelhouse, never a from-source build on user machines.
2. **Adopt i_overlay later, and only behind the same facade**, for bulk stroke/union where it is
   3-5x faster than clipper2-rust. Cover zero-length strokes first, and cross-check exact-touch
   cases against clipper2-rust in tests.
3. **Use KiCad as the oracle, not the engine:**
   - The T3 DRC probe (stock) supplies rule-resolved `required_mm` / `actual_mm` and keepout
     verdicts. kiwrap-geom supplies location, closest points and every free-form query at
     millisecond cost.
   - Read `(island yes)` from the file for item 8.
4. **Draft the DRC JSON `marker` patch** in `n3rd-bugs/kicad-fork` (D19 draft, record it next to
   G1 in docs/UPSTREAM.md, capability `cli.drc_json_marker`). Do **not** build the general
   `pcb analyze` job now. Reconsider it once KiCad 11's headless api-server keeps a board resident.
5. **Conformance:** add a parity test that compares kiwrap-geom's layer union with KiCad 10's
   `ConvertBrdLayerToPolygonalContours` + `Simplify`: shape counts equal, area within 0.1 %. Run it
   through the GPL swig-driver process on video/vme-wren, as `kicad_version("10.0")`.

---

## 9. Sources

Web (read 2026-09-24):
- KiCad SWIG deprecation, "remove the SWIG bindings in KiCad 11.0":
  https://dev-docs.kicad.org/en/apis-and-binding/pcbnew/
- iOverlay: https://github.com/iShape-Rust/iOverlay (LICENSE-MIT, LICENSE-APACHE; release 9.0.0
  2026-09-19); crate https://crates.io/crates/i_overlay (9.0.0; README read from the crate source)
- clipper2-rust: https://github.com/larsbrubaker/clipper2-rust (LICENSE = BSL-1.0; v1.2.0
  2026-09-18); https://crates.io/crates/clipper2-rust (created 2026-02-07)
- Clipper2 (C++): https://github.com/AngusJohnson/Clipper2 (BSL-1.0; release Clipper2_2.0.1
  2025-12-19); https://www.angusj.com/clipper2/Docs/Overview.htm (integer coordinates internally)
- clipper2 (Rust wrapper): https://github.com/tirithen/clipper2 (v0.6.0 2026-05-06)
- geo: https://github.com/georust/geo (LICENSE-MIT, LICENSE-APACHE; 0.33.1)
- rstar: https://github.com/georust/rstar (0.13.0, 2026-05-24)
- petgraph: https://github.com/petgraph/petgraph (0.8.3, 2025-09-30)
- pyo3: https://github.com/PyO3/pyo3 (v0.29.2 2026-08-05); maturin: https://github.com/PyO3/maturin
  (v1.15.0 2026-08-24); abi3 guide: https://www.maturin.rs/bindings.html
- shapely: https://github.com/shapely/shapely (BSD-3-Clause, 2.1.2 2025-09-24); Windows wheels
  from https://pypi.org/pypi/shapely/2.1.2/json (cp310-cp314, win32 + win_amd64); GEOS
  https://github.com/libgeos/geos (LGPL-2.1, 3.15.0 2026-09-01; shapely 2.1.2 bundles 3.13.1)
- kicad-python 0.8.0, MIT: https://pypi.org/pypi/kicad-python/json

KiCad source (`C:\src\kicad`, `kiwrap/6dd893f7d1`, and `upstream/10.0` @ `251d99700f`):
`libs/kimath/src/geometry/shape_poly_set.cpp`, `thirdparty/clipper2/.../clipper.version.h`
(2.0.1), `pcbnew/board.cpp:4247`, `pcbnew/drc/drc_test_provider.cpp:80-122`,
`pcbnew/drc/drc_test_provider_copper_clearance.cpp:263-471`, `common/rc_item.cpp:163-215`,
`include/rc_json_schema.h`, `pcbnew/drc/drc_report.cpp:160-210`, `pcbnew/pcb_marker.h:156-174`,
`pcbnew/connectivity/connectivity_data.cpp`, `pcbnew/zone.h:728-738`,
`pcbnew/zone_filler.cpp:3610`, `pcbnew/pcb_io/kicad_sexpr/pcb_io_kicad_sexpr.cpp:3678-3695`,
`common/transline_calculations/*`, `pcbnew/dialogs/panel_setup_tuning_profile_info.cpp`,
`api/proto/board/board_commands.proto`, `api/proto/board/board_types.proto`; commits
`814f65934f` (marker clearance paths), `f360829c97` (via stitching), `5d40bcb51c` and
`f2f299b687` (our ratsnest patches, size calibration).
