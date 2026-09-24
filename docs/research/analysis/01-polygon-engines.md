# Polygon engines for PCB copper (track: polygon-engines)

Research for the vector geometry behind `kw_pcb_analyze` and the zone/graphics/location items of
[requests/kiwrap-feature-requests.md](../../../requests/kiwrap-feature-requests.md) (items 8-9, 16, 18,
21, 24-37), answering the owner's request in D23 / review decision 8: "open this requirement, research
and find the best tooling, not just numpy".

The operations in scope:
- offset/buffer: stroke inflation with round caps and joins, clearance sizing;
- boolean union, difference and intersection with holes;
- distance and closest points between polygons;
- point-in-polygon, area, simplification;
- a spatial index.

Raster analyses, graph connectivity and geodesic distance belong to sibling tracks. The Rust-extension
packaging question is covered in [05-kicad-native-and-rust.md](05-kicad-native-and-rust.md), and this
document is consistent with its numbers.

**Setup.** Date 2026-09-24. Windows 11, Core Ultra 9 285K (24 threads), 127 GB RAM. Python 3.11.8 in a
scratch uv venv; rustc 1.95.0 (msvc); KiCad 10.0.6.
- Every KiCad process ran through `kiwrap.sandbox` (`find("10.0")` + `sandbox.ensure()` + `run_drc`) on
  copies in the scratch dir.
- Nothing was installed into the kiwrap env or system Python.

**Tags.** `[MEASURED]` means reproduced here on the demo boards. `[SOURCE]` means read in a source tree or
a licence file on disk. `[DOCS]` means read on the web (URLs in §9).

**Scratch.** `%TEMP%\claude\c--Upwork-KiCad\48a8df66-…\scratchpad\tooling\polygon-engines\`.

| Script | What it does |
|---|---|
| `extract.py` | Workload extraction with kiwrap's `BoardFile` |
| `common.py`, `eng_*.py` | One module per engine family |
| `bench.py`, `driver.py` | One process per measurement |
| `compare_drc.py`, `run_drc.py` | KiCad parity |
| `robust.py` | Degenerate cases |
| `klayout_worker.py`, `klayout_ipc.py` | KLayout as a separate process |
| `rust/` | Rust benchmark crate |
| `results.jsonl`, `rust_*.jsonl`, `tables.md`, `robust.json` | Raw results |

**Caveat.** The machine was shared with other research tracks during the runs, so absolute times carry
some noise. Rankings and orders of magnitude were stable across reruns.

---

## 1. Verdict

1. **shapely 2.1 (GEOS 3.13) is the only off-the-shelf Python package that covers the whole list.**
   It has round-cap buffers, booleans with holes, `distance` + `shortest_line`, `STRtree` with a
   `dwithin` predicate, prepared predicates, `make_valid` / `buffer(0)`, `simplify` and `set_precision`.
   - With the right representation its clearance answer matches kicad-cli DRC pair for pair:
     **143/143** violations on vme-wren B.Cu, and **400/402** on jetson B.Cu, where the 2 misses are an
     extraction gap. See §4.3.
   - The right representation: tracks and vias as centre-lines plus a radius, not polygonised strokes;
     arcs at ≤ 0.05 µm chord error; KiCad's 0.0005 mm DRC epsilon.
   - It is fast enough for an MCP call. A whole-board different-net clearance check takes **2.2 s
     sequential and 0.9-1.1 s on 4-12 threads** for 57k items on vme-wren or jetson. Loading those boards
     with `BoardFile.load` takes 12-20 s. `[MEASURED]`
2. **Rust engines are 3-20x faster than shapely on the same inputs (up to 50x on single two-net
   queries), with the same shape counts and
   areas to 1e-9 relative.** The engines are `i_overlay` 9.0, `geo` 0.33 (which uses `i_overlay`),
   `clipper2-rust` 1.2 and `rstar` 0.13.
   - Stroke + union of all vme-wren F.Cu copper: 126-331 ms vs 2.3 s.
   - Whole-board clearance: 0.37-0.68 s vs 2.2 s.
   - Min distance between two nets: 0.1-10 ms vs 6-40 ms.

   This is the accelerator path. Packaging (a pyo3 abi3 wheel `kiwrap-geom`) is in track 05; the
   numbers here confirm it independently. `[MEASURED]`
3. **KLayout (`klayout.db`) is EDA-grade but ruled out.**
   - `klayout` on PyPI is **GPL-3.0-or-later** (dist-info `LICENSE`, repo `LICENSE`). Under D8 it may
     only run as a separate process.
   - On PCB data its flat-mode DRC is *slower* than shapely: a plane layer takes 22 s and dense diff
     pairs 26 s, against 0.2 s for shapely.
   - Its edge pairs carry only the first shape's properties, so a violation has to be mapped back to
     items afterwards.

   Its `space_check` / `separation_check` with `DifferentPropertiesConstraint` are exactly the right
   primitives, but they buy nothing over shapely plus an edge index. `[MEASURED]` `[SOURCE]`
4. **Clipper 6.4.2 based engines (`pyclipper`, `gdstk`) are disqualified on KiCad planes.** Fractured
   plane fills make them pathological:
   - 115-144 s to union one GND plane layer;
   - 346-362 s to unfracture it;
   - 380 s on jetson In2.Cu;
   - a 20-minute timeout on vme-wren In6.Cu.

   Clipper2 (`pyclipr`, `manifold3d`, `clipper2-rust`) does not have this problem. `[MEASURED]`
5. **Of the remaining Python wheels, none replaces shapely.**
   - `pyclipr` (Clipper2, BSL-1.0) and `manifold3d` (Apache-2.0) have no distance or closest-point
     function. `manifold3d` is the fastest Python union/unfracture, but its master has already dropped
     the `FillRule` argument and Clipper2 for an in-house boolean engine.
   - `skia-pathops` is superlinear: 1,000 capsules take 7.6 s.
   - The CGAL Python bindings (GPL/LGPL + commercial) ship no 2-D boolean, offset or Minkowski module.
   - `pyclipper2` has no Windows wheel and its sdist build fails to import.
   - `pygeoops` is a thin layer over shapely that pulls in geopandas, pandas and pyproj.

   `[MEASURED]`
6. **The review's pure-Python kit (T5) is viable for queries, not for merged copper.**
   - What works: exact capsule distances with closest points, a grid index, exact unfracture by
     bridge-edge cancellation, and banded point-in-polygon. It matches KiCad as well as shapely does
     (143/143, 400/402). Timings: whole-board clearance 7.6-11.3 s, a two-net min distance 7-600 ms,
     plane unfracture 2.4-4.3 s.
   - What it lacks: union, offset and erosion. Those are what items 8 (islands of merged copper),
     25-27 (antipad clusters, necks, slivers) and 32 need. Writing a robust sweep-line boolean in pure
     Python is the part not worth building. `[MEASURED]`
7. **Recommendation (§8):**
   - Keep `kiwrap.formats.geometry` pure. Add the exact-primitive query kit there as the fallback and
     reference.
   - Implement `kw_pcb_analyze`'s vector checks in `kiwrap.core` on **shapely 2.1** behind one
     `GeometryEngine` interface. This adds one runtime dependency (shapely + numpy, about 50 MB
     installed), with the LGPL GEOS DLL loaded dynamically.
   - Leave the slot for track 05's Rust `kiwrap-geom` wheel as the accelerator for bulk union, offset
     and erosion.
   - Never use Clipper 6.4.2, never import KLayout.
   - Model arcs at fine tolerance and use the KiCad DRC epsilon. Most engine "disagreements" with KiCad
     traced back to modelling, not to the engines.

---

## 2. Candidates

Licence = what the LICENSE file shipped in the wheel or crate says (`[SOURCE]`, read from `dist-info` or
`~/.cargo/registry`), cross-checked with the GitHub licence API. Wheels = PyPI files of the latest
release, checked 2026-09-24. Maintenance = GitHub `pushed_at` / latest release on 2026-09-24.

| Candidate | Version (date) | Licence (file) | Win x64 wheels | Installed size | Offset/stroke | Boolean + holes | Distance + closest pts | Spatial index | From |
|---|---|---|---|---|---|---|---|---|---|
| **shapely** | 2.1.2 (2025-09-24); repo pushed 2026-09-23 | BSD-3-Clause (`LICENSE.txt`); bundled GEOS 3.13.1 **LGPL-2.1** (`LICENSE_GEOS`, separate `geos_c-*.dll`) | cp310-cp314 | 5.8 MB + numpy 46 MB | yes (`buffer`, round/flat/square caps, `quad_segs`) | yes (OverlayNG; `grid_size` snap rounding) | yes (`distance`, `shortest_line`, `dwithin`, `hausdorff_distance`) | `STRtree` (query predicates incl. `dwithin`, `query_nearest`) | Python |
| **klayout** (`klayout.db`) | 0.30.12 (2026-08-26) | **GPL-3.0-or-later** (`LICENSE`) | cp310-cp314 (13.9 MB) | 51 MB | `sized` (octagon modes, not round), `minkowski_sum` | yes (`merged`, `&`, `-`, property constraints) | DRC edge pairs (`space_check`, `separation_check`, `width_check`) = closest edge portions | internal | Python (separate process only, D8) |
| **pyclipr** (Clipper2 2.0.1) | 0.1.8 (2026-02-01) | BSL-1.0 | cp39-cp314 | 0.5 MB + numpy | yes (`ClipperOffset`, round ends; `arcTolerance` is in *scaled* units) | yes (PolyTree; NonZero/EvenOdd) | **no** | no | Python |
| **pyclipper** (Clipper 6.4.2) | 1.4.0 (2025-12-01) | MIT | cp310-cp314 | 0.2 MB, no deps | yes | yes, pathological on fractured planes | no | no | Python |
| **gdstk** (bundles Clipper 6.4.2) | 1.0.1 (2026-07-16) | BSL-1.0 | cp39-cp314 | 1.2 MB + numpy | yes (`offset`, `FlexPath` round ends) | yes, same pathology; output is GDS-style (holes cut open, new vertices) | no (`inside` only) | no | Python |
| **manifold3d** `CrossSection` (Clipper2 inside at v3.5.3) | 3.5.3 (2026-09-17) | Apache-2.0 | cp39-cp314 | 2.2 MB + numpy | polygon offset only (no open-path stroke) | yes (`decompose` = islands) | no | no | Python |
| **skia-pathops** | 0.9.2 (2026-02-16) | BSD-3-Clause | cp310-abi3 | 3.7 MB | `Path.stroke` (exact conics) | yes, superlinear | no | no | Python |
| **cgal** (SWIG bindings) | 6.0.1.post2024… (2024-10-24) | GPL-3 / LGPL per package + commercial (`LICENSE.GPL`, `LICENSE.COMMERCIAL`) | cp38-cp312 only (25 MB) | 87 MB | not bound | **not bound** (no `Polygon_2`, `Boolean_set_operations_2`, `Minkowski_sum_2` modules) | AABB tree only (3-D) | yes (3-D) | Python |
| pyclipper2 | 0.0.8 (2025-11-24) | not stated in dist-info | **none** (sdist; built here, `DLL load failed`) | - | - | - | - | - | - |
| pygeoops | 0.6.0 (2025-11-02) | BSD-3-Clause | py3 (pure) | 0.3 MB + geopandas/pandas/pyproj (~70 MB) | via shapely | via shapely | via shapely | via shapely | Python |
| **i_overlay** | 9.0.0 (2026-09-19) | MIT OR Apache-2.0 | crate | - | yes (`StrokeOffset`/`OutlineOffset`, round caps) | yes (float/int APIs, holes, fill rules; optional rayon) | no (predicates only) | no | Rust |
| **geo** | 0.33.1 (2026-04-20) | MIT OR Apache-2.0 | crate | - | `Buffer` (via i_overlay) | `BooleanOps` / `unary_union` (i_overlay 4.5) | yes (`Euclidean` distance, `ClosestPoint`) | via `rstar` | Rust |
| **clipper2-rust** (pure-Rust port) | 1.2.0 (2026-09-18) | BSL-1.0 | crate | - | yes (`inflate_paths_d`) | yes | no | no | Rust |
| **rstar** | 0.13.0 (2026-05-24) | MIT OR Apache-2.0 | crate | - | - | - | nearest-neighbour | R*-tree | Rust |
| cavalier_contours | 0.9.0 (2026-08-20) | MIT OR Apache-2.0 | crate | - | **arc-native** polyline offsets | polyline booleans with arcs | - | - | Rust (noted, not benchmarked) |
| kiwrap pure Python (baseline) | repo HEAD | MIT | - | 0 | no | no | yes (written here, ~250 lines: capsule/segment distance with closest points, grid index, banded PIP, unfracture) | grid | Python |

Maintenance notes `[DOCS]`:
- GEOS 3.15.0 was released 2026-09-01; shapely 2.1.2 still bundles 3.13.1.
- Clipper2 2.0.1 (2025-12-19) is also what KiCad's `SHAPE_POLY_SET` uses (track 05 §3).
- `manifold` master (2026-06 to 09) replaced Clipper2 with its own `boolean2` engine and dropped
  `FillRule` (#1754, #1760, #1836), so its 2-D API is in flux.
- CGAL bindings: last release 2024-10.
- `i_overlay` 9.0 is 5 days old and `geo` 0.33 still pins `i_overlay` 4.5.

---

## 3. Workloads and method

Extracted with kiwrap's own file model (`BoardFile.load`, 11.5 s for vme-wren, 20 s for jetson) into
per-layer JSON. `[MEASURED]`

**Items.**
- Tracks are segments with width.
- Arc tracks are polylines: `ArcTrack.points()` at 0.005 mm for timing, and at 0.00005 mm for the KiCad
  parity runs (see §4.3).
- Vias are circles on every copper layer they are *flashed* on. This honours
  `remove_unused_layers` / `keep_end_layers` / `zone_layer_connections` and track endpoints: 4,370 of
  vme-wren's 4,374 vias remove unused layers.
- Pads are `Pad.polygons()` on each expanded copper layer.
- Zone fills are each `filled_polygon`, as written, i.e. **fractured** into one ring with zero-width
  bridges.

**Coordinates** are mm (nm-exact from the file).

| Board | Copper layers | Items (all layers) | Fill polygons (vertices) | Heaviest layer used |
|---|---|---|---|---|
| vme-wren | 12 | 56,769 (24,858 segs, 1,792 arcs, 17,269 via flashes, 12,713 pad polys, 137 fills) | 137 (860k) | F.Cu 21,391 items; In6.Cu 2 fills with 164k vertices |
| jetson-agx-thor-baseboard | 10 | 57,909 (19,835 segs, 2,316 arcs, 29,831 via flashes, 5,807 pad polys, 120 fills) | 120 (475k) | In2.Cu 6,359 items incl. 1,192 arcs (dense diff pairs) |

**Operations.**
- **(a) union**: stroke + union of one net on one layer, or of the whole layer.
- **inflate**: offset every item by 0.0625 mm.
- **(b) clearance**: all different-net item pairs with gap < C (0.12-0.125 mm), including overlaps. Net
  rule: same named net is skipped; no-net pads of different footprints are compared (as in KiCad's
  `testPadAgainstItem`).
- **(c) mindist**: minimum distance + closest points between two nets.
- **(d) islands**: unfracture every fill on a layer and assign same-net vias/pads to islands.
- **(e) roundtrip**: unfracture fidelity against the file's `filled_polygon`.

**Measurement.**
- Each measurement runs in its own process.
- Time is the median of 3 runs. Engines that take minutes (Clipper 6.4.2, `pure`, `pyclipr` clearance)
  got 1 run.
- Peak memory is the RSS above the pre-op RSS, sampled every 2 ms in a separate run.
- Rust memory could not be separated from the JSON load, so the whole-process peak working set is
  reported instead (28-101 MB).
- Stroking is done natively where the engine has it: shapely `buffer`, Clipper offset of open paths,
  gdstk `FlexPath`. Otherwise the same numpy capsules are used, at KiCad's `GetArcToSegmentCount` with
  0.005 mm error, rounded up to a multiple of 8.

---

## 4. Results

### 4.1 (a) Stroke + union

Times in ms. Shapely and KLayout are shown as union time + separate geometry build. Every engine
produced **identical polygon counts** on every workload.

| Engine | vme F.Cu GND (4,847 items) | jetson F.Cu GND (2,695) | vme In1.Cu GND (1,616 + 114k-vertex plane) | vme F.Cu all (21,391) | jetson In2.Cu all (6,359) | vme In6.Cu all (666 + 164k-vertex planes) |
|---|---:|---:|---:|---:|---:|---:|
| shapely `union_all` | 303 (+33) | 159 (+19) | 684 (+157) | 2,297 (+165) | 1,249 (+135) | 1,098 (+242) |
| shapely `grid_size=1e-6` (snap rounding) | 1,178 | 647 | 4,660 | 8,461 | 5,770 | 7,284 |
| klayout `merged` (flat) | 73 (+129) | 38 (+60) | 108 (+136) | 422 (+540) | 285 (+295) | 135 (+145) |
| pyclipr (Clipper2) | 40 | 27 | 183 | 295 | 1,135 | 2,773 |
| manifold3d | 69 | 30 | 67 | 471 | 310 | 79 |
| gdstk (Clipper 6.4.2) | 34 | 19 | **143,872** | 309 | **384,221** | **timeout 1,200 s** |
| pyclipper (Clipper 6.4.2) | 83 | 35 | **114,825** | 622 | **380,999** | **timeout 1,200 s** |
| Rust i_overlay | 31 | 16 | 49 | 199 | 126 | 70 |
| Rust i_overlay (rayon) | 23 | 12 | 33 | 126 | 65 | 56 |
| Rust geo `unary_union` | 27 | 15 | 40 | 131 | 86 | 60 |
| Rust clipper2-rust | 35 | 18 | 74 | 331 | 315 | 95 |

Results agree across engines. `[MEASURED]`
- Examples: vme F.Cu all gives 4,997 polygons with 5 holes everywhere. The area is 7,685.2451 mm² on
  engines fed identical capsules; 7,676-7,687 on engines that stroke natively at their own arc
  tolerance.
- Hole counts differ only by convention: 1,178 vs 1,154 on jetson In2.Cu, 2,980 vs 2,971 on vme
  In1.Cu. Clipper2-family engines merge holes that touch at a single vertex (§5).
- Peak memory above baseline stays small: shapely 6-52 MB, KLayout ≤ 8 MB, Clipper2 ≤ 22 MB,
  pyclipper up to 88 MB.

Stroke-only (`inflate` by 0.0625 mm, all 21,391 vme F.Cu items, per item):

| Engine | Time |
|---|---|
| shapely vectorised `buffer` | 146 ms (756k vertices) |
| pyclipr per item | 140 ms |
| klayout `sized` | 449 ms (octagonal corners, not round) |
| gdstk | 1,754 ms (369 MB peak) |

### 4.2 Where the Python time goes

- **shapely union** scales with the number of *overlapping* strokes. On a whole signal layer, 2.3 s
  is spread over 5k islands.
- **Snap rounding** (`grid_size=1e-6`) makes every union **4-7x slower**. It is not needed for PCB
  data: §5 shows float unions already merge nm gaps correctly when the inputs come from nm-exact
  files.
- **KLayout.** Building `db.Polygon`s from Python costs as much as the merge itself: 540 ms for 21k
  polygons via `Polygon.from_s`, 2.7x faster than via `db.Point` objects.
- **KLayout caching trap.** `Region.merged()` is cached on the Region, so timing the same Region twice
  shows about 1 ms. Every KLayout time here uses a fresh Region.

### 4.3 (b) Different-net clearance, and correctness against kicad-cli DRC

Times in ms for one layer. The pair counts in this table use the 0.005 mm arc workload; the parity
runs below use fine arcs.

| Engine | vme B.Cu (9,551 items) | vme F.Cu (21,391) | jetson F.Cu (12,690) | jetson In2.Cu (6,359, diff pairs) | vme In1.Cu (plane, 114k vertices) |
|---|---:|---:|---:|---:|---:|
| **shapely, exact capsules + edge-indexed fills** (`clearance_indexed`) | 75 (+34 build) | 356 (+95) | 475 (+55) | 211 (+74) | 198 (+142) |
| shapely, exact capsules, fills as whole polygons (naive STRtree) | 94 | 394 | 479 | 7,961 | 2,562 |
| shapely, polygonised strokes | 137 | 318 | 276 | 12,229 | 9,049 |
| klayout `space_check` + `and_` (DifferentPropertiesConstraint) | 958 | 845 | 839 | 26,530 | 22,341 |
| pyclipr: inflate C/2 + bbox sweep + pairwise intersection | 364 | 1,213 | 749 | 91,928 | 5,869 |
| pure Python (grid + capsule distance + banded PIP) | 344 | 1,277 | 999 | 889 | 1,102 |
| Rust rstar + `geo` segment distance (no containment pass) | 23 | 60 | 52 | 55 | 108 |

`[MEASURED]`

**Large polygons must be decomposed.**
- GEOS computes `distance(point, plane)` in O(n) with no index. `STRtree.query(predicate="dwithin")`
  prepares only the *query* geometry.
- So a plane layer costs 2.6-8 s naively and 0.2 s once each fill's boundary edges go into the same
  STRtree. Fills are then tested for containment via prepared `intersects` with the fill as the query
  side.
- KLayout's flat `space_check` pays the same price (22 s) with no user-side fix short of tiling.

**Whole board** (all layers, C = 0.125 mm). `[MEASURED]`

| Engine | vme-wren (12 layers, 56,769 items) | jetson (10 layers, 57,909 items) |
|---|---|---|
| shapely sequential | 2.19 s | 2.20 s |
| shapely, 4 threads | 1.14 s | 0.96 s |
| shapely, 12 threads | 1.11 s | 0.92 s |
| shapely geometry build | 1.2 s | 0.65 s |
| Rust | 0.68 s | 0.37 s |
| pure Python | 11.3 s | 7.6 s |

Shapely 2 releases the GIL in its vectorised calls, so threading helps up to the Python glue.

**KiCad parity.** Method:
- On a scratch copy, append one rule to the `.kicad_dru`: `(rule "kw_bench" (layer "B.Cu") (constraint
  clearance (min C)))`. On jetson the file holds only that rule, because its own `hs_zone_clearance`
  rule saturates the 499 clearance cap.
- Run sandboxed `kicad-cli pcb drc --all-track-errors`. It took 60 s for vme-wren and 16-28 s for
  jetson.
- Compare unordered **item-uuid pairs** with each engine.

| Engine | vme-wren B.Cu, C = 0.125 (KiCad: 143) | jetson B.Cu, C = 0.12 (KiCad: 402) |
|---|---|---|
| shapely exact capsules (`clearance_indexed`) | **143 / 143, 0 extra**; max \|actual − KiCad\| 0.0001 mm | **400 / 402**, 4 extra |
| pure Python exact capsules | **143 / 143, 0 extra** | **400 / 402**, 4 extra |
| Rust rstar exact capsules (count only) | 143 | 404 (= shapely's 400 + 4) |
| shapely polygonised strokes | 143 / 143 (actual off by ≤ 0.0043 mm) | 389 / 402; the 13 misses are 0.1118-0.118 mm, i.e. within polygonisation error of 0.12 |
| pyclipr inflate-and-intersect | 139 / 143; misses at 0.1242-0.1244 mm | 366 / 402; misses at 0.114-0.119 mm |
| klayout (net properties, edge pairs mapped back to items) | 137 / 143 | 347 / 402; 10 of 55 misses are attribution losses (net-level merge assigns the edge to another item of the same net) |

The remaining differences were each traced. `[MEASURED]` `[SOURCE]`
- **Arcs.** At 0.005 mm chord error, 128 extra pairs appeared on vme B.Cu (0.1200 vs 0.125 between
  concentric DDR clock arcs). At 0.00005 mm they vanish. KiCad measures `SHAPE_ARC` exactly, and no
  polygon engine here has arcs, so extraction must tessellate finely or the check must add the chord
  error to its tolerance.
- **Epsilon.** KiCad calls `Collide(clearance - epsilon)` with `ADVANCED_CFG::m_DRCEpsilon = 0.0005 mm`
  (`common/advanced_config.cpp:252`). With a 1 nm tolerance, 39 extra pairs at 0.12499x mm appeared.
- **jetson, 2 misses.** J7's shield pad "SH" vs +5V tracks at 0.1171 mm. kiwrap's pad polygon is
  larger than 0.12 away, which points to a per-layer padstack that `Pad.polygons()` does not model
  (item 12 territory).
- **jetson, 4 extras.** Fill-to-fill gaps of 0.1035-0.1055 mm between power zones (+3V3 vs
  /CSI/CSIB_3V3, VCC vs +VCC_FMC, …). KiCad's `testZonesToZones` does compare fills with the same rule
  (`drc_test_provider_copper_clearance.cpp` ~1240-1400), so these are most likely net-tie exclusions
  (`IsNetTieExclusion`), which the workload does not model.
- **Via flashing.** Without honouring `remove_unused_layers`, inner layers get phantom annuli. Track 05
  reports a 0.005 mm "gap" artefact from this on vme-wren In6.Cu.

### 4.4 (c) Minimum distance + closest points between two nets

Times in ms. The distance is the same for every engine in the table unless noted.

| Engine | vme F.Cu GND vs P3V3 (d = 0.125499) | jetson F.Cu GND vs +3V3 (0.1) | vme In5.Cu P3V3 vs GND planes (0.125499) | jetson In2.Cu GND vs CSI0_D1- (0.111027) |
|---|---:|---:|---:|---:|
| shapely exact (STRtree `query_nearest` + `dwithin` refine + `shortest_line`) | 41 | 20 | 37 | 5.7 |
| shapely `STRtree.query_nearest` on polygons | 181 | 90 | 21 | 40 |
| shapely union both nets + `distance` + `shortest_line` | 413 | 179 | 530 | 103 |
| klayout `separation_check`, doubling d | 133 | 57 | 1,737 | 17 (**0.118941**: polygonisation of arcs and vias, not an engine error) |
| pure Python (grid, doubling radius) | 89 | 52 | 602 | 7.4 |
| Rust rstar | 3.3 | 1.3 | 9.5 | 0.1 |

Every engine returns closest points. KLayout returns them as an edge pair, which then has to be reduced
to a point pair. Clipper2, gdstk, manifold and skia have no distance at all. `[MEASURED]`

### 4.5 (d) Islands: unfracture fills + assign same-net vias/pads

Times in ms.

| Engine | vme F.Cu (67 fills) | vme In1.Cu (1 fill, 114k vertices) | vme In6.Cu (2 fills, 164k) | jetson In4.Cu (29 fills, 88k) | jetson In2.Cu (2 fills, 64k) |
|---|---:|---:|---:|---:|---:|
| shapely (`buffer(0)` + STRtree `within`) | 29 | 126 | 126 | 96 | 69 |
| klayout (`merged` + `interacting`) | 193 | 326 | 334 | 290 | 230 |
| pure Python (edge cancellation + banded PIP) | 46 | 2,516 | 4,315 | 390 | 550 |

Island counts are identical everywhere: 67 / 1 / 2 / 29 / 2. So are pad/via assignments between shapely
and pure Python, except where a pad's reference point lies on a boundary (537 vs 501 on vme F.Cu, using a
centroid vs the first vertex). `[MEASURED]`

### 4.6 (e) Polygon-with-holes round-trip fidelity against KiCad's `filled_polygon`

Checks:
- **area error**: summed area against the shoelace of the fractured rings;
- **non-file vertices**: output vertices not present in the file;
- **holes**: the hole count.

| Engine | vme In1.Cu (114k vertices): ms, holes | vme In6.Cu (164k): ms, holes | jetson In4.Cu (88k): ms, holes | Area error (all layers) | Non-file vertices |
|---|---|---|---|---|---|
| shapely `buffer(0)` | 81, 2,975 | 111, 4,308 | 40, 1,504 | ≤ 1.1e-6 mm² | 0 |
| shapely `make_valid(method="structure")` | 140, 2,975 | 187, 4,308 | 64, 1,504 | ≤ 1.1e-6 mm² | 0 |
| klayout `merged` | 156, 2,975 | 219, 4,308 | 107, 1,504 | ≤ 1.1e-6 mm² | 0 |
| pure Python edge cancellation | 2,423, 2,975 | 4,199, 4,308 | 310, 1,504 | ≤ 1.1e-6 mm² | 0 |
| manifold3d | 50, 2,963 | 65, 4,290 | 27, 1,501 | ≤ 1e-6 mm² | 0 |
| pyclipr | 740, 2,963 | 2,186, 4,290 | 139, 1,501 | ≤ 1.1e-6 mm² | 0 |
| pyclipper | 346,070, 2,963 | not run (timeouts) | 8,095, 1,501 | ≤ 1.1e-6 mm² | 0 |
| gdstk | 362,207, n/a | not run | 8,102, n/a | ≤ 4e-7 mm² | **404** (GDS cut lines) |
| Rust geo / i_overlay / clipper2-rust | 32 / 36 / 53; 2,975 / 2,974 / 2,963 | 47 / 53 / 80 | 24 / 24 / 49 | 7e-6 to 9e-5 mm² (integer snapping) | n/a |

The unfracture checks agree across engines. `[MEASURED]`
- Every engine except gdstk reproduces KiCad's fill **exactly**: the same area to 1e-9 relative, and
  every output vertex is a file vertex.
- The hole-count split is a representation choice:
  - shapely, KLayout and pure Python: 2,975, 4,308, 1,504.
  - Clipper2 family: 2,963, 4,290, 1,501.
  - The difference is holes that touch each other or the shell at a single vertex. OGC keeps them as
    separate rings; Clipper2 merges them.
  - KiCad's `SHAPE_POLY_SET` is Clipper2, so the Clipper2 count is "KiCad's" count. Areas are
    identical either way.
- `shapely.make_valid` default (`method="linework"`) is 10x slower (1.26 s on In1) and split one plane
  into 2 parts in a spot test. Use `buffer(0)` or `method="structure"`.

### 4.7 KLayout as a separate process (D8)

A persistent worker (`klayout_worker.py`) takes JSON lines of `(net, "x,y;…")` polygons and returns edge
pairs. `[MEASURED]`
- Spawning and importing `klayout.db` costs 117-175 ms.
- The payload is 3.7-7 MB per layer and takes 210-470 ms to serialise.
- A round trip takes 955 ms (vme F.Cu), 997 ms (vme B.Cu) and **34.8 s** (jetson In2.Cu, 317k edge
  pairs).

The process boundary is affordable. KLayout's flat engine on arc-heavy PCB polygons is what makes it
lose.

---

## 5. Numerical robustness

**Synthetic cases** (`robust.py`, NonZero union; expected polys/holes/area). `[MEASURED]`

| Case | shapely `make_valid`+union | shapely `buffer(0)`+union | shapely grid 1 nm | klayout | pyclipr | pyclipper | manifold | gdstk | pure (unfracture only) |
|---|---|---|---|---|---|---|---|---|---|
| Shared edge (2 unit squares) → 1/0/2 | ok | ok | ok | ok | ok | ok | ok | ok | n/a |
| KiCad keyhole (fractured 10x10, 2x2 hole) → 1/1/96 | ok | ok | ok | ok | ok | ok | ok | ok (holes n/a) | ok |
| Two holes touching at a point → area 92 | 2 holes | 2 holes | 2 holes | 2 holes | 2 holes | 2 holes | 2 holes | ok | 2 holes |
| Hole touching shell at a vertex → area 97 | 1 hole | 1 hole | 1 hole | 1 hole | **0 holes** (notch) | 0 holes | 0 holes | ok | 1 hole |
| Squares touching at a corner → area 2 | 2 polys | 2 polys | 2 polys | **1 poly** | 2 | 2 | 2 | 2 | n/a |
| Gap of 1e-10 mm → area 2 | **2 polys, 1e-10 sliver** | same | 1 poly | 1 poly | 1 poly | 1 poly | 1 poly | 1 poly | n/a |
| Overlap of 1e-10 mm | ok | ok | ok | ok | ok | ok | ok | ok | n/a |
| Partial collinear overlap → 1.5 | ok | ok | ok | ok | ok | ok | ok | ok | n/a |
| 1,000 identical capsules → one capsule | ok | ok | ok | ok | ok | ok | ok | ok | n/a |
| Bow-tie → area 2 | ok (2 polys) | **area 1.0: a lobe dropped** | ok | ok | ok | ok | ok | ok | 0 (not a KiCad ring) |
| 1 nm² square at (1000, 1000) mm | ok | ok | ok | ok | ok | ok | ok (my shoelace cancels) | ok | shoelace cancels |

**Observations on real data.**
- **Fractured fills** (coincident, opposite bridge edges) are the degenerate case that matters on PCBs.
  All Clipper2, GEOS, KLayout, i_overlay and geo handle them exactly and fast. Clipper 6.4.2 handles
  them correctly but in O(n²)-like time.
- **Float engines** (GEOS, i_overlay float API) keep sub-nm gaps as slivers. Integer engines (Clipper,
  KLayout) close them. KiCad data is nm-quantised, so this never showed on the boards: identical
  polygon counts everywhere. For inputs from computation (offsets), use `shapely.set_precision(g, 1e-6)`
  or `grid_size` on the specific operation, at a cost of 4-7x.
- **`buffer(0)`** is the fast unfracture for KiCad rings. It must not be used on self-intersecting
  input (the bow-tie), which KiCad never writes.
- **`pyclipr` pitfalls:**
  - `arcTolerance` is in *scaled* integer units. Passing 0.005 with `scaleFactor=1e6` made one
    capsule into 7,258 vertices and a layer union took 21 s.
  - `PolyPath.area` already includes descendants, so summing node areas double-subtracts holes.
- **KLayout pitfalls:**
  - `Region.merged()` caches its result.
  - Edge pairs carry only the first shape's properties.
  - Default `ignore_angle` (90) skips corner cases; pass 180 for Euclidean clearance.
  - `sized` has no true round mode.
- **Shoelace on absolute board coordinates** (hundreds of mm) is exact enough for PCB features, but
  cancels for sub-µm features at 1 m. Translate to a local origin in any area/orientation code.

---

## 6. Licensing (D8)

| Component | Licence | In-process import from the MIT core? |
|---|---|---|
| shapely | BSD-3-Clause | yes |
| GEOS | LGPL-2.1, shipped as a separate DLL (`shapely.libs/geos_c-*.dll`) and loaded dynamically; the user can replace it | yes. kiwrap does not vendor it; `uv` installs the upstream wheel. Keep it as a pip dependency, never statically linked or copied into a kiwrap wheel. |
| numpy | BSD-3-Clause | yes |
| pyclipr, clipper2-rust, gdstk | BSL-1.0 | yes |
| pyclipper | MIT | yes |
| manifold3d | Apache-2.0 | yes |
| skia-pathops | BSD-3-Clause | yes |
| i_overlay, geo, rstar, cavalier_contours | MIT OR Apache-2.0 | yes; the Rust helper crate stays MIT |
| **klayout** | **GPL-3.0-or-later** | **no.** Separate process only, like the SWIG driver. Measured workable (§4.7) but not worth it. |
| **CGAL** 2-D boolean/offset packages | **GPL-3.0-or-later** (or commercial); kernel LGPL | **no**, and the bindings do not expose them anyway |
| KiCad's own geometry (`SHAPE_POLY_SET`, `SHAPE::Collide`, transline) | GPL-3.0-or-later except the vendored Clipper2 (BSL) | only via the kicad-cli oracle (review T3) or a GPL process. Reimplement from papers, never port (review T9). |

---

## 7. What each feature request needs from the engine

| Item(s) | Operation | Engine call (shapely) | Measured cost class |
|---|---|---|---|
| 8 zones with holes, islands, area, vias per island | unfracture, area, point-in-polygon | `buffer(0)` + `STRtree.query(predicate="within")` | ≤ 130 ms per plane layer |
| 9, 21 graphic copper with stroke / netless bridges | stroke inflation, union, intersects | `buffer(w/2, cap_style=round)`, `union_all`, `intersects` | ms |
| 16 k nearest vias | kNN | `STRtree.query_nearest(all_matches, return_distance)` | ms |
| 18, 31 locations, closest points, actual/required | distance + closest points | exact capsules + `shortest_line` | 6-40 ms for a net pair; 0.1-0.5 s for a layer |
| 24 fab audit, 32 probe via | clearance at other values, legality of a disc | same as (b) with a different C; `dwithin(disc, layer, C)` | 0.1-0.5 s per layer |
| 25, 26 plane voids, antipad clusters | union of inflated antipads, difference from the plane | `union_all(buffer(holes, clearance))`, `difference` | 0.1-1 s |
| 27 pour necks, slivers | erosion/dilation (opening) | `buffer(-w/2).buffer(w/2)` vs original; `difference` = necks | per plane about 1 s in shapely, tens of ms in Rust |
| 29 loop area | polygon area of the path loop | `Polygon(path).area` | trivial |
| 33, 34 coupling, gaps | parallel run / min gap between two nets | exact capsules pairwise within `dwithin(C_max)` | same as (b) |
| 36 copper distance pin to protection part | min distance through copper | geodesic: raster/graph tracks; Euclidean part here | - |
| 37 paste coverage, mask webs | area of intersection; width check | `intersection(...).area`; negative buffer | ms |

---

## 8. Recommendation

1. **Representation first. This is what made every engine agree with KiCad.**
   - Tracks, arcs and vias become (centre-line, radius) primitives. Pads become polygons. Fills become
     unfractured polygons with holes.
   - Arcs are tessellated at ≤ 0.05 µm for distance work.
   - Vias are flashed per `remove_unused_layers` / `keep_end_layers` / `zone_layer_connections`.
   - Clearance compares against `C − 0.0005 mm` (KiCad DRC epsilon).
   - Encode these in `kiwrap.formats.geometry` with docstrings citing this document. Record them as
     VERIFICATION claims when promoted: 143/143 and 400/402 against 10.0.6.
2. **Pure layer, zero-dependency, in `kiwrap.formats.geometry`.** This is the reference and the
   fallback, and keeps the `formats` import rule.
   - Contents: `unfracture()` by bridge-edge cancellation (exact, 0.3-4.3 s per plane layer), segment
     and capsule distance with closest points, a uniform grid index, and banded point-in-polygon.
   - It already matches KiCad as well as shapely does for queries.
   - It has no union or offset. Do not write them in Python.
3. **Engine layer in `kiwrap.core` (not `formats`): shapely 2.1 behind a small `GeometryEngine`
   protocol.**
   - Protocol operations: `stroke`, `union`, `difference`, `offset`, `unfracture`,
     `pairs_within(C)`, `nearest`, `islands`.
   - This is the one new runtime dependency (shapely + numpy, about 50 MB). It gives every operation
     in §7 today, with measured latencies of 0.03-2.3 s per layer. Those sit well inside an MCP call,
     and are dwarfed by the 12-20 s board load, which the review's T8 cache addresses.
   - Rules for the shapely implementation:
     - index plane boundaries as edges;
     - use `buffer(0)` for unfracture;
     - no `grid_size` by default;
     - thread per layer.
4. **Accelerator slot: track 05's `kiwrap-geom` (Rust, MIT).** Same protocol, with `i_overlay` for
   union/stroke, `rstar` for proximity, and `clipper2-rust` where KiCad-identical hole topology matters.
   - Use it when bulk union/offset or erosion over whole boards becomes the bottleneck. It is 10-20x
     faster for union and 3-6x faster for whole-board clearance.
   - The engines agree to 1e-9 relative on area and exactly on counts, so switching changes speed,
     not answers. Gate it with capability key `native.geom`.
5. **Rules.**
   - Never use Clipper 6.4.2 (`pyclipper`, `gdstk`) on fill data.
   - Never import `klayout` (GPL).
   - Do not adopt `manifold3d` while its 2-D API is being rewritten.
   - Keep CGAL, skia-pathops, pyclipper2 and pygeoops out.
6. **Owner decision needed (replaces review decision 8's "stay pure or numpy later"):** accept shapely
   (+ numpy) as a core dependency of the MCP server package, or ship it as an optional extra
   (`kiwrap[analysis]`) with `kw_pcb_analyze` returning a teaching error when absent. The
   recommendation is **core dependency**: the extension installs kiwrap with `uv sync`, and the
   wheels exist for cp310-cp314 on Windows.

---

## 9. Sources

**On-disk licence files** `[SOURCE]`, in `venv\Lib\site-packages\*.dist-info`:
- shapely `licenses/LICENSE.txt`, `LICENSE_GEOS`, `LICENSE_win32`;
- klayout `LICENSE` (GPL v3, 29 June 2007);
- pyclipr, gdstk `licenses/LICENSE` (BSL-1.0);
- pyclipper `licenses/LICENSE` (MIT);
- manifold3d `licenses/LICENSE` (Apache-2.0);
- skia_pathops `licenses/LICENSE` (BSD-3);
- cgal `LICENSE.GPL`, `LICENSE.COMMERCIAL`;
- rtree `LICENSE.txt` (MIT).

In `~/.cargo/registry/src/*`:
- `clipper2-rust-1.2.0/LICENSE` (BSL-1.0);
- `i_overlay-9.0.0` and `geo-0.33.1` `Cargo.toml` `license = "MIT OR Apache-2.0"`;
- iOverlay repo `LICENSE-MIT` / `LICENSE-APACHE`.

**KiCad source** `[SOURCE]`, private clone `C:\src\kicad`:
- `pcbnew/drc/drc_test_provider_copper_clearance.cpp` (net rules ~185-300, pad rules ~760-830, zone-zone
  ~1240-1400);
- `common/advanced_config.cpp:252` (`m_DRCEpsilon = 0.0005`).

**PyPI JSON API** (versions, upload dates, wheel tags, sizes), read 2026-09-24:
- https://pypi.org/pypi/shapely/json
- https://pypi.org/pypi/klayout/json
- https://pypi.org/pypi/pyclipr/json
- https://pypi.org/pypi/pyclipper/json
- https://pypi.org/pypi/pyclipper2/json
- https://pypi.org/pypi/gdstk/json
- https://pypi.org/pypi/manifold3d/json
- https://pypi.org/pypi/skia-pathops/json
- https://pypi.org/pypi/cgal/json
- https://pypi.org/pypi/pygeoops/json
- https://pypi.org/pypi/numpy/json

**crates.io API**, read 2026-09-24:
- https://crates.io/api/v1/crates/i_overlay
- https://crates.io/api/v1/crates/geo
- https://crates.io/api/v1/crates/clipper2-rust
- https://crates.io/api/v1/crates/clipper2
- https://crates.io/api/v1/crates/rstar
- https://crates.io/api/v1/crates/cavalier_contours
- https://crates.io/api/v1/crates/geo-booleanop
- https://crates.io/api/v1/crates/geo-clipper

**GitHub repos** (licence, `pushed_at`, releases via `gh api`), read 2026-09-24:
- https://github.com/shapely/shapely
- https://github.com/libgeos/geos
- https://github.com/KLayout/klayout (LICENSE: GPL v3)
- https://github.com/drlukeparry/pyclipr
- https://github.com/fonttools/pyclipper (README: "Clipper library (ver. 6.4.2)")
- https://github.com/AngusJohnson/Clipper2
- https://github.com/heitzmann/gdstk (`external/clipper/clipper.hpp`: `CLIPPER_VERSION "6.4.2"`)
- https://github.com/elalish/manifold (v3.5.3 `src/cross_section/cross_section.cpp` includes
  `clipper2/clipper.h`; master commits #1754, #1760, #1836 replace it)
- https://github.com/fonttools/skia-pathops
- https://github.com/CGAL/cgal-swig-bindings
- https://github.com/theroggy/pygeoops
- https://github.com/iShape-Rust/iOverlay (README: buffering, `allow_multithreading`)
- https://github.com/georust/geo
- https://github.com/larsbrubaker/clipper2-rust
- https://github.com/jbuckmccready/cavalier_contours
- https://github.com/georust/rstar

**Documentation:** KLayout `Region` (merged semantics, `space_check` / `separation_check`
`PropertyConstraint`, added 0.28.4), https://www.klayout.de/doc-qt5/code/class_Region.html, read
2026-09-24.
