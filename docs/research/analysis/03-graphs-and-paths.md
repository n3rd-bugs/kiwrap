# Copper connectivity graphs and path queries (track: graph-connectivity)

Research for `kw_pcb_analyze` items 28 (`copper_path` / `net_topology`), 29 (`loop_area`), 30
(`nearest_via` / `pad_return`), 35 (`current_capacity` bottlenecks), 36 (`interfaces` /
`esd_entry`), and the raster half of item 27 (`gnd_reach`) in
[requests/kiwrap-feature-requests.md](../../../requests/kiwrap-feature-requests.md). The owner's
question (D23, "open": *research the best tooling, not just numpy*) is answered for graphs and
paths. Polygon booleans, offsets and clearance belong to the geometry tracks.

Date: 2026-09-24. Machine: Windows 11, Core Ultra 9 285K, 127 GB RAM, Python 3.11.8 in a scratch
venv, Rust 1.95. `[MEASURED]` means reproduced on this machine on the real demo boards. Every
timing is the median of 3 runs, each run in its own process.

---

## 1. Verdict

1. **The graph library is not the bottleneck, and kiwrap does not need one.** On the biggest demo
   copper graph (vme-wren: 12 layers, 1508 footprints, 48,130 nodes, 53,787 edges), every query
   the items need runs in **≤ 62 ms in pure Python** (`heapq` + lists). Examples: one pad → nearest
   same-net via for all 6,883 pads, GND pad paths with neck width and via count, a max-min width
   bottleneck path, 984 point-to-point paths for 492 decoupling loops, and connector pins → the
   first series part. networkx is the slowest option on every query. rustworkx is no faster than
   pure Python on this graph size, because its weight callbacks run in Python. The zero-dependency
   rule of `kiwrap.formats` holds for graphs. `[MEASURED]`
2. **The costs sit elsewhere:**
   - File parse: 11.9 s for vme-wren with `BoardFile.load`.
   - Geometric contact detection, i.e. which copper items touch and where: 4.3 s with shapely 2.1.
     The existing pure-Python `ratsnest_estimate` does the coarser group-only version in 13.7 s.
   - Raster geodesics through pours: 18.7 s in pure Python for one full plane at 0.1 mm.

   The geometry-track decisions and a raster accelerator matter; the graph library does not.
3. **Raster geodesic (gnd_reach, pour paths, necks) needs an implicit grid, not a graph library.**
   On a 3.4M-cell GND plane at 0.1 mm:

   | Method | Time | Memory |
   |---|---|---|
   | Rust implicit-grid Dijkstra | 0.63 s | 83 MB peak at 13.4M cells |
   | scikit-image `MCP_Geometric` | 2.1 s | 326 MB |
   | scipy csgraph | 4.4 s | 1.5 GB |
   | rustworkx | 11.8 s | 3.2 GB |
   | pure Python | 18.7 s | 244 MB |
   | networkx | about 60 s (extrapolated) | about 6 GB (extrapolated) |

   networkx was measured at 0.25 mm only: 10 s and 1.1 GB. If an accelerator is allowed, it
   belongs in Rust, next to the existing `helper/` crate. `[MEASURED]`
4. **Excluded:**
   - **igraph** (python-igraph 1.0.0 and the C core are GPL-2.0-or-later, so importing it breaks
     D8). Its speed is no better anyway.
   - **graph-tool**: LGPL-3.0, with no Windows wheel and no conda-forge win-64 build (Windows only
     through WSL or Docker), which rules it out under D18.
5. **What KiCad's own `CONNECTIVITY_DATA` can offer:**
   - **Stock IPC `GetConnectedItems` works as an oracle, not as a path engine.** It answers with
     the whole cluster as a flat set: 5,404 items for GND on vme-wren, 0.31 s per call, after 29 s
     to start and open the board. It has no edges, no contact points and no island identity.
   - **Fork patch (optional).** A G1-style fork patch that exports the `CN_ITEM` adjacency
     (`m_connected`) would give kiwrap KiCad's exact contact truth. Graph weights and zone
     geodesics would still be kiwrap's. It is an optional tier-3 upgrade, not a prerequisite.
6. **Bug found in kiwrap on the way.** `ratsnest_estimate` reports 1 unrouted airwire on
   jetson-agx-thor, GND D63.1–D10.1. KiCad 10.0.6 DRC reports 0 `unconnected_items`, and so does
   the contact graph built here. The cause: a 0.1 mm GND track from (166.25, 54.6) to
   (184.25, 54.6) runs *over* both pads, and neither pad touches a track end. The estimator only
   joins pads to track end discs (§3.3).

---

## 2. What the items need, as graph problems

| Item | Graph problem | Weight | Primitive |
|---|---|---|---|
| 28 `copper_path` / `net_topology` | pad→pad or pad→nearest via through tracks, arcs, vias, zone islands and copper graphics; min neck, layer sequence, via count, series parts on the way | length (mm) or resistance | single-pair Dijkstra with predecessors; multi-source Dijkstra; path post-processing |
| 29 `loop_area` | ordered pads (C35.2 → U8.2 … → C35.1): concatenate the real copper paths per consecutive pair, then shoelace area; compare to the ideal (pad-centre) polygon | length | k point-to-point paths |
| 30 `nearest_via` / `pad_return` | every pad → nearest same-net via, plus distance and neck width | length | **one** multi-source Dijkstra from all vias (nets are disjoint components, so one run answers every net) |
| 35 `current_capacity` | the bottleneck (max-min width) path between two pads, then mΩ and ΔT along it (IPC-2152) | width, then resistance | widest-path Dijkstra (or maximum spanning tree + tree path), then shortest path restricted to width ≥ bottleneck |
| 36 `interfaces` / `esd_entry` | each connector pin → first protection/series part through copper; then pin → part → far net | length | multi-source Dijkstra from all 2-pin-part pads; a tiny net/part graph for the series hop |
| 16 `near_items` vias | k nearest vias (Euclidean) | – | spatial index (geometry track), not a graph |
| 27 `gnd_reach`, `pour_necks` | geodesic distance through a pour to the nearest via, max reach per region, necks | cell cost | multi-source grid Dijkstra/BFS on a raster, plus erosion |

Everything except item 27 is a *small* graph: tens of thousands of nodes, sparse, degree ≈ 2.
Item 27 is a *grid*: millions of cells, where an explicit edge list is the wrong representation.

---

## 3. The copper graph model (what was built and validated)

Built by `build_graph.py` (scratch, §10) from kiwrap's own file model (`BoardFile.load` →
`extract.py`). Shapely 2.1.2 was used for contact detection only, so that the graph libraries
could be compared on identical input.

### 3.1 Nodes and edges

- **Nodes:**
  - one per pad (all its copper layers);
  - one per via (all layers it spans);
  - one per distinct *contact point* on a track centre line, keyed by (layer, x, y) at 0.1 µm.
    These are the track ends plus the split points where another item touches the track's
    interior.
- **Edges:**

  | Kind | Count on vme-wren | What it is |
  |---|---|---|
  | `track` | 31,628 | track/arc piece between consecutive contact points, with width and layer |
  | `attach` | 16,685 | pad or via centre → the closest point on a track that overlaps the pad/via copper |
  | `join` | 7,933 | two overlapping same-net tracks: the closest points of the two centre lines (T-junctions, overlaps, crossings) |
  | `padvia` | 936 | overlapping pads/vias |
  | `zone` | 17,318 | see §3.2 |
  | `zone_est` | 299 | see §3.2 |
  | `graphic` | 15 | through net-less copper graphics (item 9); these connect any net, as KiCad does |

- **Contact detection:**
  - Items: track capsules, pad polygons, via discs, zone fills and stroke-inflated graphics.
  - Candidate pairs: one STRtree per layer with `intersects`.
  - Only same-net pairs (or pairs involving a graphic) become edges. Different-net overlaps are
    counted as shorts: 1 on vme-wren, 12 on thor.

### 3.2 Zone islands

Each stored filled polygon is one island.

1. Attachment points are the pads, vias and track points that overlap it.
2. They are joined by the Delaunay edges whose straight segment is *covered* by the fill. That
   segment is the geodesic when the pair is mutually visible.
3. Delaunay edges that cross a hole are dropped. If that disconnects the island, Kruskal adds the
   shortest dropped edges back as `zone_est` (Euclidean, flagged as an estimate).

Section 5.3 measures how close this is to a true raster geodesic.

### 3.3 Validation

| Board | Components | Unrouted (Σ per-net pad components − 1) | kiwrap `ratsnest_estimate` | KiCad DRC `unconnected_items` |
|---|---|---|---|---|
| vme-wren | 1,860 | **0** | 0 (13.7 s) | – (routed demo) |
| jetson-agx-thor | 1,426 | **0** | **1** (GND D63.1–D10.1, 6.3 s) | **0** (kicad-cli 10.0.6, sandboxed copy) |
| kit-dev-coldfire | 287 | 0 | – | – |

- **The thor discrepancy is a kiwrap bug.** The file contains a 0.1 mm F.Cu GND track from
  (166.25, 54.6) to (184.25, 54.6), and both pads lie on its interior. `ratsnest_estimate`'s rule
  ("a pad is its polygon, a track end is a disc", module docstring) never tests a pad polygon
  against a track *body*. KiCad's `CN_ITEM` collision does, and so does the contact graph here.
  - **Fix:** in `formats/analysis/ratsnest.py`, add a segment-vs-pad-polygon test (distance from
    the pad polygon to the segment < half the width), the same test already used for zone fills.
  - This is outside my assigned file, so it is reported here rather than changed.
- **Shapely pitfalls hit while building this** (for the geometry tracks):
  - **KiCad's fills are fractured.** 100 of 137 fills on vme-wren are invalid for GEOS: the
    self-touching ring has zero-width bridges.
  - **`make_valid` must use `method="structure"`.** The default `linework` method returns a
    `GeometryCollection`, and a prepared `GeometryCollection` is not indexed. One zone-vs-vias query
    then takes **22 s** instead of 0.09 s. The structure method takes 0.13 s per 114k-vertex fill and
    recovers 2,975 holes.
  - **STRtree query direction matters.** The big polygons must be the *input* side of
    `STRtree.query`, because only the input side is prepared. Otherwise every small item scans the
    fill linearly: 340 s instead of 1.2 s on vme-wren.

---

## 4. Workloads and setup

Workloads were extracted with kiwrap's `BoardFile` (read-only) from the KiCad 10.0 demos:

| Board | Copper layers | Tracks | Arcs | Vias | Pads (copper) | Zone fill polygons (vertices) | Parse | Graph nodes / edges | Contact graph build (shapely) |
|---|---|---|---|---|---|---|---|---|---|
| vme-wren | 12 | 24,858 | 1,792 | 4,374 | 6,883 | 137 (860,108) | 11.9 s | 48,130 / 53,787 | 4.26 s (make_valid 1.52, STRtree 1.19, rest 1.5) |
| jetson-agx-thor-baseboard | 10 | 19,835 | 2,316 | 3,235 | 4,808 | 120 (474,830) | 18.9 s | 39,156 / 47,431 | 2.70 s |
| kit-dev-coldfire-xilinx_5213 | 4 | 2,935 | 0 | 253 | 825 | 21 (48,606) | 0.5 s | 4,696 / 5,186 | 0.22 s |

Edges were deduplicated (the minimum length is kept) and ε = 1e-9 was added to each length. The
same arrays went to every library.

**Query set** (`plan.py`, identical for every library):
- **Q0:** connected components, then unrouted count per net.
- **Q1:** all pads → nearest same-net via (one multi-source run from all 4,374 vias).
- **Q1b:** 1,498 GND pads → nearest of 1,503 GND vias, with full path reconstruction, via count and
  neck width.
- **Q2:** bottleneck path between the two P3V3 pads that are farthest apart (233 mm apart).
- **Q2b:** shortest path among width ≥ bottleneck.
- **Q3:** 492 decoupling loops (C with one pad on GND, nearest multi-pin IC power pin and that IC's
  nearest GND pin): 984 point-to-point paths, then real and ideal areas.
- **Q4:** 162 pins of VME connector P1 → nearest pad of any of 1,241 two-pin parts.

Every library returned identical answers:

| Query | Result on vme-wren |
|---|---|
| Q0 | 1,860 components, 0 unrouted |
| Q1 | 5,099 pads reached, Σ 17,903.796 mm, max 146.858 mm |
| Q1b | max 118.994 mm, min neck 0.115 mm |
| Q2 | bottleneck 0.2 mm; widest-shortest path 356.782 mm with 58 vias |
| Q3 | Σ real loop area 7,078 mm² against 1,406 mm² ideal (±1.2 mm² between libraries from equal-length ties) |
| Q4 | 40 of 162 pins reach a part, Σ 1,394.937 mm |

MST-based bottleneck *paths* differ between libraries (MST ties); the bottleneck *value* does
not. That is why Q2b (shortest path at the bottleneck width) is the product semantics.

---

## 5. Measurements `[MEASURED]`

### 5.1 Item graph, vme-wren (ms, median of 3)

| Library | Version | Import | Build | Q0 | Q1 all pads | Q1b GND + paths | Q2 bottleneck | Q2b | Q3 984 p2p | Q4 | Graph MB |
|---|---|---|---|---|---|---|---|---|---|---|---|
| **pure Python** (`heapq`, lists) | 3.11.8 | 0 | 32.8 | 17.3 | 62.1 | 14.4 | 3.9 ¹ | 2.3 | 52.3 | 35.7 | 17.4 |
| scipy.sparse.csgraph | 1.17.1 | 450 | 2.7 | 1.7 | **12.0** | 8.7 | 0.8 | 2.0 | 384.2 ² | **2.8** | 3.0 |
| rustworkx | 0.18.1 | 6 | 12.0 | 8.3 | 60.4 | 13.0 | 2.8 | 1.5 | 58.3 | 41.5 | 16.3 |
| networkx | 3.6.1 | 460–830 | 70.4 | 21.7 | 102.6 | 44.8 | 17.3 | 9.0 | 51.7 | 71.9 | 38.1 |
| networkit | 11.2.2 | 960 | 7.6 | 3.3 | 15.7 | 8.7 | 8.7 | 5.0 | 827.1 ³ | 7.5 | 6.0 |
| igraph (GPL) | 1.0.0 | 110 | 10.1 | 4.0 | 38.1 | 19.5 | 1.7 | 6.9 | 891.3 ³ | 25.5 | 7.3 |
| **petgraph** (Rust, release, LTO) | 0.8.3 | – | 0.53 | 0.95 | 6.3 ⁴ / 3.5 ⁵ | 0.72 | 8.1 | – | 8.0 | 4.3 | 2.1 |

jetson-agx-thor (39k nodes) shows the same ranking:

| Library | Build (ms) | Q1 (ms) | Q3 (ms) | Q4 (ms) |
|---|---|---|---|---|
| pure Python | 21.6 | 42.3 | 24.2 | 26.2 |
| scipy | 2.0 | 8.5 | 134.6 | 2.2 |
| rustworkx | 11.1 | 50.5 | 31.0 | 26.5 |
| networkx | 55.5 | 73.9 | 29.5 | 72.5 |
| petgraph | 0.48 | 5.8 | 3.9 | 2.3 |

¹ The pure-Python Q2 is a widest-path Dijkstra (max-min) straight from s to t. The libraries use a
maximum spanning tree and then the tree path, which is the reusable form when many pairs are asked.

² scipy's `dijkstra` has no target/early exit, so each point-to-point query is a full
single-source run over the net's component. It is fastest for batch multi-source work and slowest
for many point-to-point queries.

³ The per-call overhead of `get_shortest_path` / `BidirectionalDijkstra` dominates.

⁴ petgraph's own `dijkstra` (single start, super-source, `HashMap` result, no predecessors).

⁵ A 30-line multi-source Dijkstra with predecessors over petgraph's adjacency. petgraph has neither
multi-source nor predecessor output; `astar` returns paths.

**Reading:**
- The whole analysis pass (Q0–Q4) costs about 0.2 s in pure Python against 12 s parse + 4 s
  contact detection. A compiled library saves ≤ 0.2 s per call, below the noise of one
  `kw_pcb_analyze`.
- networkx costs 0.5–0.8 s just to import. That alone exceeds every query here.

### 5.2 Raster geodesic through one net's copper (vme-wren GND)

Rasterised with Pillow `ImageDraw`. Fractured fills scan-convert correctly, with holes empty. The
metric is 8-neighbour octile; fmm is the Euclidean eikonal. Sources are GND via cells.

**In1.Cu, full GND plane at 0.1 mm: 2,345 × 1,612 grid, 3,381,167 copper cells, 1,503 sources, 13.4M edges.**

| Method | Build | Query | Total | Peak / graph memory | Reach (mm) |
|---|---|---|---|---|---|
| **Rust implicit grid Dijkstra** (BinaryHeap, f32) | – | **0.63 s** | 0.63 s | ~80 MB (0.05 mm run below) | 29.35 |
| Rust petgraph explicit grid | 0.22 s | 3.02 s | 3.24 s | +310 MB graph | 29.35 |
| scikit-image `MCP_Geometric` | – | 2.10 s | 2.13 s | 326 MB peak | 29.35 |
| scikit-fmm `distance` (Euclidean) | – | 3.20 s | 3.21 s | 204 MB peak | 28.07 (octile over-estimates by up to 8 %) |
| scipy csgraph `dijkstra(min_only)` | 0.63 s edge list + 1.46 s CSR | 2.29 s | 4.39 s | 860 MB graph / 1.54 GB peak | 29.35 |
| rustworkx | 0.58 s + 3.41 s | 7.80 s | 11.8 s | 1.30 GB graph / 3.25 GB peak | 29.35 |
| pure Python `heapq` on a flat list | – | 18.7 s | 18.97 s | 244 MB peak | 29.35 |
| networkx | not run at 0.1 mm | | | | |

The same plane at **0.25 mm** (550k cells) and at **0.05 mm** (13.4M cells):

| Method | 0.25 mm | 0.05 mm |
|---|---|---|
| skimage | 0.23 s | 7.74 s / 1.2 GB |
| fmm | 0.38 s | 19.2 s / 705 MB |
| scipy | 1.09 s | 18.7 s / 6.0 GB peak |
| rustworkx | 1.56 s | – |
| pure Python | 2.87 s | – |
| networkx | **10.1 s / 1.1 GB** | – |
| **Rust implicit** | – | **3.85 s / 83 MB peak** |

Reach at 0.05 mm is 29.30 mm, stable against 0.1 mm.

**F.Cu, GND tracks + pads + pours at 0.1 mm (199k copper cells, 1,174 SMD pad targets):**

| Method | Time |
|---|---|
| Rust implicit | 0.020 s |
| petgraph explicit | 0.08 s |
| scipy | 0.83 s |
| rustworkx | 0.50 s |
| skimage | 0.32 s |
| pure Python | 0.84 s |
| networkx | 3.0 s / 389 MB |

**scikit-fmm is not robust on thin copper.** It reached only 1,152 of 1,174 pads, with a reach of
10.9 mm against 24.5 mm. The first-order fast-marching stencil does not propagate through tracks
that are 1–2 cells wide. Use it for planes only, never for mixed track/pour masks.

Rasterisation itself (Pillow) took 0.88 s (In1, 0.1 mm), 2.0 s (0.05 mm) and 0.14 s (F.Cu). The
review's earlier pure-Python figure (coldfire In1, 1.7 s at 0.1 mm) is consistent with these.

### 5.3 How good is the item graph's zone model against a raster geodesic?

The test: F.Cu GND, each SMD pad → nearest GND via, with the item graph restricted to F.Cu edges
(so like-for-like) against the 0.1 mm octile raster. Results for the 1,173 pads both reach:

| Measure | Value |
|---|---|
| raster / item distance ratio, p5 / p50 / p95 | 0.907 / 1.000 / 1.053 (max 1.16) |
| raster − item difference, p50 / p95 | 0.000 / +0.05 mm |
| largest raster − item differences | +0.16 mm (C2.2: 1.27 against 1.42 mm, the octile overshoot) and −2.25 mm |

The −2.25 mm case is the raster cutting across wide copper while the item graph follows track
centre lines and attachment points.

The Delaunay-visibility zone model is good enough for `nearest_via`, `copper_path` and
`loop_area`, with outer layers within ±10 % at p5–p95. Plane reach, pockets and necks (item 27)
need the raster.

### 5.4 KiCad's own connectivity over IPC (10.99.0 headless, sandboxed copy of vme-wren)

| Step | Time | Result |
|---|---|---|
| `kicad-cli api-server` start + open board | 28.95 s | |
| `net_connectivity("P3V3")` (GetItemsByNet + GetConnectedItems per cluster) | 2.46 s | 1 cluster, 964 anchors |
| `net_connectivity("GND")` | 2.69 s | 1 cluster, 3,001 anchors |
| one `GetConnectedItems(GND pad, all copper types)` | 0.31 s | 5,404 items (Pad, Track, Via, Zone) as a flat set |
| 50 × `GetConnectedItems(pad, [pads, vias])` | 3.90 s | 78 ms per call |

The user's KiCad config hash was unchanged.

---

## 6. Candidates

| Candidate | Licence (read from the file) | Import into MIT core? | Windows wheels (Py 3.11 / 3.12 / 3.13) | Install size | Maintenance | Robustness on degenerate input (probed) | Python / Rust |
|---|---|---|---|---|---|---|---|
| **pure Python** (`heapq`, union-find) | kiwrap MIT | yes | n/a | 0 | ours | we validate the weights ourselves | Python |
| **networkx** 3.6.1 | BSD-3-Clause (`LICENSE.txt`: "NetworkX is distributed with the 3-clause BSD license") | yes | pure `py3-none-any`; **3.7 (2026-09-21) requires Python ≥ 3.12**, so a 3.11 floor pins 3.6.x | 8.0 MB | very active (17.3k★, pushed 2026-09-23) | parallel edge in `Graph` silently keeps the last; NaN weight propagates silently; negative weight raises `ValueError("Contradictory paths… negative weights?")` | Python |
| **rustworkx** 0.18.1 (2026-07-29) | Apache-2.0 (`LICENSE`) | yes | one `cp310-abi3-win_amd64` wheel (2.3 MB) covers 3.10–3.14 | 6.3 MB | active (Qiskit, pushed 2026-09-23) | rejects negative and NaN weights (`ValueError`); `PyGraph` is a multigraph by default (parallel edges kept) | Python + `rustworkx-core` crate (Apache-2.0, rust 1.85) |
| **scipy.sparse.csgraph** 1.17.1 | BSD-3 (`LICENSE.txt`; the wheel bundles OpenBLAS/LAPACK BSD and the libgfortran GPL-3.0 **with the GCC runtime exception**, which is fine to redistribute) | yes | cp311/312/313/314 win_amd64 (36.7 MB); **1.18 and numpy 2.5 require ≥ 3.12** | 89 MB + numpy 23 MB | very active | **hangs** (> 20 s, killed) on one negative undirected edge; **duplicate COO entries are summed** (1 + 2 → 3); a NaN weight is silently skipped (answer 6.0 via another path); explicit 0 weights are kept as edges | Python |
| **networkit** 11.2.2 (2026-09-14) | MIT (`License.txt`) | yes | cp310–cp314 win_amd64 (30.1 MB) | 47 MB | active (878★, pushed 2026-09-21) | not probed in depth; single attribute weight (a second graph is needed for width) | Python (C++ core, OpenMP) |
| **igraph** (python-igraph) 1.0.0 (2025-10-23) | **GPL-2.0** (`LICENSE`: "GNU GENERAL PUBLIC LICENSE Version 2"); C core GPL-2.0 | **no** (D8): only as a separate process | `cp39-abi3-win_amd64` (3.2 MB) | 7.1 MB | python-igraph last push 2026-05-14, C core active | rejects NaN (`InternalError`); negative → falls back to Bellman-Ford and reports a negative cycle | Python / C |
| **graph-tool** | **LGPL-3.0** | a dynamically linked LGPL library could be shipped, but… | **none**: the PyPI entry is a stub with no files; the install guide says Windows needs WSL or Docker | – | active | – | Python / C++ (Boost) |
| **petgraph** 0.8.3 (2025-09-30) | MIT OR Apache-2.0 (`LICENSE-MIT`, `Cargo.toml`) | yes (Rust) | n/a (crate) | +0.2 MB in a release exe | active (4.0k★, pushed 2026-09-20) | typed weights; `dijkstra` gives no predecessors and no multi-source (write your own, 30 lines) | Rust |
| **pathfinding** 4.16.0 | Apache-2.0/MIT (`cargo info`) | yes (Rust) | n/a | small | active (pushed 2026-09-23) | implicit-graph Dijkstra/BFS/A* over closures, a good fit for grids | Rust |
| **scikit-image** `graph.MCP_Geometric` 0.26.0 (2025-12-20) | BSD-3-Clause (`LICENSE.txt`, "Files: *") | yes | cp311–cp314 win_amd64 (11.9 MB); requires scipy, numpy, pillow, imageio, tifffile, lazy-loader | 22 MB + deps (~135 MB total) | very active | inf/negative costs are impassable (a clean obstacle model) | Python |
| **scikit-fmm** 2025.6.23 | BSD-3 (`LICENSE.txt`) | yes | cp310–cp313 win_amd64 (0.1 MB) | 0.2 MB (+numpy) | low (pushed 2026-07-01) | **fails through 1–2-cell passages** (§5.2) | Python / C++ |
| **KiCad IPC `GetConnectedItems`** | KiCad GPL, as a separate process (fine) | n/a (IPC) | stock 10.0+ GUI, 10.99 headless | – | – | exact KiCad connectivity, but clusters only | protobuf |

---

## 7. KiCad's `CONNECTIVITY_DATA`: what it could provide

Read from the private fork checkout `C:\src\kicad` at `0cdfa41cb6`, `pcbnew/connectivity/`:

- **The graph exists inside KiCad.** `CN_ITEM` holds `m_connected`, the list of items it touches
  (`ConnectedItems()`, `connectivity_items.h:227`). Zone islands are `CN_ZONE_LAYER` items, one per
  filled outline (`connectivity_items.h:289`). This is the *exact* contact truth kiwrap
  re-derives: `SHAPE_SEGMENT::Collide` with clearance 0, jumper pad groups, net ties, and so on.
- **The stock API does not expose it.**
  - `CONNECTIVITY_DATA::GetConnectedItems` and `GetNetItems` return flat item sets. The IPC
    command is a thin wrapper and returns a cluster (§5.4).
  - KiCad's only path search is `FROM_TO_CACHE::cacheFromToPaths` → `uniquePathBetweenNodes`
    (`from_to_cache.cpp`). It is a BFS that copies whole paths into a deque (exponential on meshes)
    and is called with `EXCLUDE_ZONES`. It exists for the DRC `fromTo` rule, not for geodesics, and
    is not reusable.
- **A G1-style fork patch** (docs/UPSTREAM.md §G1: CLI job plus IPC command, capability keys) would
  be about 150 lines:
  - `kicad-cli pcb export connectivity --format json` and IPC `GetCopperGraph(nets[])`, answering
    per net with the nodes (KIID, type, layer set, anchor) and the edges (pairs of adjacent
    `CN_ITEM`s with the shared layer), island ids for `CN_ZONE_LAYER`, and optionally the
    collision point.
  - Capability keys: `cli.has_connectivity_export` and `ipc.has_get_copper_graph`.
  - **What it buys:** KiCad-exact adjacency, with no false positives or negatives like the thor
    bug in §3.3.
  - **What it does not buy:** edge lengths along tracks, split points, or geodesics inside zones.
    kiwrap still weights the graph from geometry.
  - **Verdict:** a worthwhile tier-3 oracle (it also validates the file-tier builder in tests),
    not a prerequisite. Per D19 it stays a draft in the fork.
- **Today's stock use:**
  - `GetConnectedItems` as a *checker*. kiwrap's per-net component count must equal the IPC
    cluster count when a session is open.
  - `kicad-cli pcb drc` `unconnected_items` as the offline oracle (below the 499-item cap).

---

## 8. Recommendation

1. **Ship the graph layer as zero-dependency pure Python** in `kiwrap.formats.analysis`, for
   example `copper_graph.py`. The `formats` import rule holds: no third-party package, no kiwrap
   import outside `formats`.
   - **Size:** about 300 lines. It holds the node/edge model of §3 (kinds, width, layer,
     resistance), union-find components, and a multi-source Dijkstra with predecessors and a source
     label. It also has an early-exit point-to-point Dijkstra (bidirectional optional), a widest-path
     Dijkstra with the "shortest at bottleneck width" second pass, and path post-processing (length,
     neck, via count, layer sequence, shoelace area).
   - **Cost:** ≤ 62 ms per query on the biggest demo.
   - **Input validation:** the weights must be checked at build time (finite, ≥ 0, parallel edges
     resolved). Those are exactly the cases where scipy, networkx and igraph misbehave (§6).
2. **Contact detection is the real work.** Take it from the geometry track's engine and follow §3.
   - Contact points are split points on track centre lines, not just groups.
   - Pads on a track *interior* connect.
   - Fills are islands, with Delaunay visibility inside and flagged `zone_est` edges across holes.
   - Graphics conduct.
   - The same builder should replace `ratsnest_estimate`'s grouping, which fixes the thor
     false-positive. One builder serves ratsnest, 28, 29, 30, 35 and 36.
3. **The raster kit (item 27) should stay pure Python behind a cell budget, with a Rust accelerator
   as the optional fast path.**
   - Without the accelerator, a full plane at 0.1 mm takes 18.7 s: acceptable only as
     `run_async`, with windowing by bbox and 0.25 mm defaults (2.9 s).
   - With it, 0.63 s at 0.1 mm and 3.9 s at 0.05 mm, at under 100 MB.
   - The accelerator is a 60-line implicit-grid Dijkstra/BFS (plus chamfer distance and erosion
     later), built with `pathfinding` or hand-rolled, MIT/Apache. It lives in the `helper/`
     workspace as a second crate. Its form is either a `kiwrap-geom` subcommand exchanging `.npy`/raw
     masks over files (like `kiwrap-live`), or a PyO3/maturin extension. Choose the form together
     with the geometry tracks' polygon-engine decision, so there is one native component, not two.
   - Capability-gate it (`native.raster`) with the pure-Python kit as the fallback, the same
     pattern as the IPC keys.
4. **Do not adopt the graph libraries.**
   - **networkx:** slowest, 0.5–0.8 s import, 38 MB, and its 3.7 release already drops Python 3.11.
   - **rustworkx:** no gain at this size. Reconsider only if kiwrap ever builds million-node
     explicit graphs, which the raster kit avoids.
   - **scipy / scikit-image:** the only candidates with a real win (raster 2–4 s). They pull in
     about 135 MB, their newest releases also require Python ≥ 3.12, and scipy has the
     negative-weight hang.
   - **igraph** and **graph-tool:** excluded (§1.4).
5. **Tier 3 (optional, D19 draft):** the `GetCopperGraph` / `pcb export connectivity` fork patch
   (§7), as an oracle and a future provider. Record it next to G1 in docs/UPSTREAM.md.
6. **Report now:** the `ratsnest_estimate` pad-on-track-interior bug (§3.3), with thor D63.1–D10.1
   as the regression case.

### Open questions for the owner

- **Accelerator form.** If the geometry tracks also recommend a native engine, is the preferred
  form a PyO3 wheel (one more build artefact per Python minor unless abi3) or a helper executable
  called over files (no ABI coupling, about 30 ms process overhead)? The measurements here favour
  either; a single decision should cover both tracks.
- **Default raster resolution for `gnd_reach`.** Reach changed by 0.05 mm (29.35 against 29.30 mm)
  between 0.1 and 0.05 mm on vme-wren In1, so **0.1 mm** is proposed with a cell budget of
  about 4M cells per call.

---

## 9. Sources

- networkx: PyPI JSON `https://pypi.org/pypi/networkx/json` (3.7 uploaded 2026-09-21,
  `requires_python >=3.12`; 3.6.1 2025-12-08, `>=3.11`, `py3-none-any`); `LICENSE.txt` in the
  wheel; https://github.com/networkx/networkx (read 2026-09-24).
- rustworkx: `https://pypi.org/pypi/rustworkx/json` (0.18.1, 2026-07-30 upload,
  `cp310-abi3-win_amd64`); `LICENSE` (Apache-2.0); https://github.com/Qiskit/rustworkx; crate
  `rustworkx-core` 0.18.1 (`cargo info`: Apache-2.0).
- scipy: `https://pypi.org/pypi/scipy/json` (1.18.1 2026-08-21 `>=3.12`; 1.17.1 2026-02-23
  `>=3.11`); `LICENSE.txt` in the wheel (BSD-3 plus bundled OpenBLAS, LAPACK and GCC runtime with the
  GCC-exception-3.1); csgraph docs https://docs.scipy.org/doc/scipy/reference/sparse.csgraph.html.
- numpy: `https://pypi.org/pypi/numpy/json` (2.5.3 `>=3.12`; 2.4.6 `>=3.11`).
- python-igraph: `https://pypi.org/pypi/igraph/json` (1.0.0, 2025-10-23, `cp39-abi3`); `LICENSE`
  (GPL v2) in the wheel and https://github.com/igraph/python-igraph; C core
  https://github.com/igraph/igraph (GPL-2.0).
- graph-tool: https://graph-tool.skewed.de/installation.html (Linux/macOS; Windows via WSL/Docker;
  LGPLv3); PyPI `graph-tool` has no files.
- networkit: `https://pypi.org/pypi/networkit/json` (11.2.2, 2026-09-14, cp310–cp314 win_amd64);
  `License.txt` (MIT); https://github.com/networkit/networkit.
- petgraph 0.8.3: https://github.com/petgraph/petgraph (release 2025-09-30); `LICENSE-MIT` /
  `Cargo.toml` `license = "MIT OR Apache-2.0"` in `~/.cargo/registry`.
- pathfinding 4.16.0: https://github.com/evenfurther/pathfinding; `cargo info` (Apache-2.0/MIT,
  rust 1.88).
- scikit-image 0.26.0: `https://pypi.org/pypi/scikit-image/json`; `LICENSE.txt` (BSD-3-Clause);
  `skimage.graph.MCP_Geometric` docs https://scikit-image.org/docs/stable/api/skimage.graph.html.
- scikit-fmm 2025.6.23: `https://pypi.org/pypi/scikit-fmm/json`; `LICENSE.txt` (BSD-3);
  https://github.com/scikit-fmm/scikit-fmm.
- shapely 2.1.2 (contact detection only): `LICENSE.txt` (BSD-3), `LICENSE_GEOS` (LGPL-2.1, GEOS
  dynamically linked in `shapely.libs`).
- KiCad connectivity: `pcbnew/connectivity/connectivity_data.h`, `connectivity_items.h`,
  `from_to_cache.cpp` at `C:\src\kicad` commit `0cdfa41cb6` (private fork; upstream equivalents at
  https://gitlab.com/kicad/code/kicad/-/tree/master/pcbnew/connectivity).
- kiwrap: `src/kiwrap/formats/analysis/ratsnest.py` (docstring and cost), `src/kiwrap/backends/ipc/queries.py`
  (GetConnectedItems cluster walk, G1 `GetRatsnest`), docs/UPSTREAM.md §G1,
  requests/kiwrap-feature-requests.review.md §6 T5 (the earlier raster BFS figure).

## 10. Reproduce

All scripts live in the session scratchpad
`C:\Users\usama\AppData\Local\Temp\claude\c--Upwork-KiCad\48a8df66-7213-479f-ab2e-a63b89135016\scratchpad\tooling\graph-connectivity\`.
They should be moved to `spikes/analysis-graphs/` if the owner wants them kept.

| Script | What it does |
|---|---|
| `extract.py` | kiwrap env: `uv run python extract.py <board> <out.json>`; writes the workload JSON |
| `build_graph.py` | scratch venv: `build_graph.py <workload.json> <graph.npz>`; builds the contact graph, writes `.stats.json` |
| `plan.py` | picks the query set |
| `bench_lib.py <lib> <npz> <plan> <out>` | one library per process |
| `agg.py` | medians over the runs |
| `raster_bench.py <workload> <net> <layer> <res> <method> <out>` | raster methods; the `rasterize` method is run first |
| `rust/` | `cargo build --release`, then `graphbench item vme` and `graphbench grid raster_vme-wren_GND_1_0.1 x` (input from `export_bin.py`) |
| `robust.py <case>` | degenerate-input probes, one case per process |
| `ratsnest_ref.py`, `ratsnest_time.py`, `ratsnest_net.py` | the kiwrap estimator reference runs |
| `ipc_probe.py` | the headless IPC measurement (sandboxed copy, 10.99) |
| `drc/thor.drc.json` | kicad-cli 10.0.6 DRC of a sandboxed copy of thor (`KICAD_CONFIG_HOME` pointed into the scratch) |

Results are in `results/` (item graph) and `rres/` (raster).
