# Bake-off: the leading engines per capability on one fixed workload set (track: bakeoff)

Tracks 01-05 each measured their candidates on slightly different inputs: different boards,
layers, chord errors and meters. This track builds **one harness** with a **fixed workload set**
from the same two boards and re-runs the top 2-4 candidates of every capability head-to-head in
the same process model under the same meter. It then names one winner per capability.

Date: 2026-09-24. Machine: Windows 11, Core Ultra 9 285K (24 threads), 127 GB RAM. Python 3.11.8
scratch venv (uv), Rust 1.95 (MSVC), KiCad 10.0.6 for the DRC oracle, and the KiCad 10.99 transline
harness from track 04 for Z0. Nothing was installed into the kiwrap env or system Python. Every
kicad-cli run went through `kiwrap.sandbox` (`sandbox.ensure`), and `user_config_hash` was asserted
unchanged afterwards. Scratch: `%TEMP%\claude\c--Upwork-KiCad\48a8df66-...\scratchpad\tooling\bakeoff\`.
It holds `extract.py`, `common.py`, `cap_*.py`, `driver.py`, `run_drc*.py`, `kern5.py`, `bakegeo/`
and `results/*.jsonl`. No dependency was added to kiwrap, and this file is the only repo change.

---

## 1. Verdict

| # | Capability | Winner | Runner-up | Deciding numbers (video / jetson) |
|---|---|---|---|---|
| 1 | Stroke + union of one layer | **i_overlay 9.0** (Rust) | clipper2-rust 1.2 (Rust) | 46-118 ms against clipper2 90-323 ms and shapely 553-1,513 ms. Same union topology as GEOS on all 4 layers. For **inflate/offset**, use clipper2-rust: it is the only engine that reproduced KiCad's own inflated topology. |
| 2 | Different-net clearance with closest points | **rstar 0.13 edge index + exact capsule distance** (Rust, rayon) | pure Python (same algorithm) | 10 / 42 ms against shapely 174 / 1,169 ms and pure Python 147 / 1,510 ms. **All four engines match kicad-cli DRC pair for pair:** 782/782 (video, 0.25 mm) and 1,648/1,648 (jetson, 0.15 mm), max \|Δ actual\| ≤ 0.0001 mm outside zone-to-zone pairs. |
| 3 | Zone-fill islands | **clipper2-rust unfracture** (Rust) | pure Python unfracture | All vector engines agree exactly (4 / 29 islands, same area to 1e-4 mm²) in 32-75 ms. Pure Python: 0.45-0.50 s. clipper2 follows KiCad's hole convention. Raster: 4-7× slower, 313-564 MB, and 4 spurious holes. |
| 4 | Geodesic distance to the nearest via across a plane | **5×5 Borgefors chamfer in Rust** | the same kernel in numba, hand-inlined | 0.60 / 0.28 s at 50 µm and 1.18 s at 25 µm (29 Mpx); numba 0.70 / 0.33 / 1.31 s. +0.08…+0.26 % mean against order-2 FMM, where 3×3 chamfer or 8-neighbour Dijkstra are +4.8…+5.1 %. FMM takes 4.9-39 s. |
| 5 | Copper graph + pad-to-nearest-via for one net | **pure-Python graph and queries, contacts from the Rust kit** | contacts from shapely | Build 215 ms / 1.20 s (Rust contacts) against 212 ms / 1.43 s (shapely) and 0.99 / 10.7 s (pure). All 250 / 1,266 pads get identical answers. Queries take 0.4-9.8 ms with any library. |
| 6 | Z0 of 3 line types on JLC04161H-7628 | **Closed forms (tier 0)** for microstrip and edge-coupled pairs; **scikit-fem P1** for GCPW and fab-comparable numbers | pcb-toolkit 0.2.1 (oracle) | Closed forms take 2-9 µs and are within −0.64…−0.01 % of FEM P2. P1 takes 0.46-1.6 s, is within 0.15 % of P2, and reproduces JLC's calculator (50.07 / 49.86 / 102.9 Ω). KiCad transline is +1.4…+7.7 %. |

**What this settles between the tracks.** Track 01 proposed shapely as the primary engine with a Rust
accelerator. Track 05 proposed Rust as the engine. On one harness, **shapely wins no capability**:
- Union: the Rust engines are 3-16× faster, with the same answers.
- Clearance: rstar is 17-28× faster. Pure Python is as fast as shapely here: 0.85× the time on video
  and 1.3× on jetson.
- Islands and the graph build: shapely ties with the Rust kit.

The recommended stack has three parts:

1. **One MIT native component, `kiwrap-geom`**: a pyo3 abi3-py311 wheel of about 1 MB. It holds
   i_overlay, clipper2-rust, rstar, rayon and the chamfer kernel, and covers capabilities 1-5.
2. **The pure-Python kit as the zero-dependency fallback and reference.** It covers clearance,
   islands and the graph. Measured here it is interactive (≤ 1.5 s per layer on the largest board)
   and exact.
3. **scikit-fem only in the optional `kiwrap[fields]` extra.**

Without the native wheel, pure Python has no union or offset. Only that case needs shapely, as an
optional extra. numpy, numba and OpenCV are not needed at runtime once the chamfer kernel ships in Rust.

**One finding corrects the raster track (02).** Its "5×5 chamfer costs 13 s" came from a numba
kernel that calls an `inline="always"` helper. That kernel took 5.4-21 s here. Hand-inlined in
`kern5.py`, the identical algorithm runs in 0.33-1.31 s, within 10-16 % of Rust, with bit-identical
distances. The 5×5 Borgefors chamfer is therefore the interactive default, not only the "reported
number" pass.

---

## 2. Workloads and method

**Boards.** Both boards were extracted once with kiwrap's own `BoardFile` (`extract.py`, run from the
repo with `uv run`) into `data/<board>.json`.

| Board | Role | Copper layers | Items | BoardFile load |
|---|---|---|---|---|
| `video` (KiCad 10 demo) | mid-size 4-layer | 4 (F, In1, In2, B) | 16,026 | 0.98 s |
| `jetson-agx-thor-baseboard` | largest demo that loads | 10 | 57,909 | 16.9 s |

jetson has the most items of the demos (vme-wren has 56,769). KiCad's own SWIG `LoadBoard` hangs on
jetson (track 05), but kiwrap's parser and kicad-cli both load it.

**Item model** (identical for every engine):
- Tracks, and arcs cut into pieces at 0.05 µm chord error for distance work, are centre line plus width.
- Vias are flashed per layer, following `remove_unused_layers`, `keep_end_layers` and `zone_layer_connections`.
- Pads are kiwrap's `Pad.polygons()` rings, plus exact "fat primitives" for distance work: a circle is a
  point plus r, an oval a segment plus r, a roundrect the inner rectangle plus the corner r, and any
  straight-edged pad its edges.
- Zone fills are KiCad's fractured `filled_polygon` rings, as written in the file.
- Union work uses 0.005 mm polygonisation (KiCad `max_error`), with KiCad's segment-count rule
  (`common.segcount`).
- The clearance epsilon is KiCad's `m_DRCEpsilon` of 0.0005 mm.

**Meter** (`common.Meter`):
- One fresh Python process per measurement.
- Wall time is the median of 3 runs (pure Python and FMM: 1 run).
- The reported `ms` **includes marshalling** from the Python item dicts to the engine: building numpy
  or shapely geometry, or filling `array` buffers for Rust.
- Peak memory is the RSS rise above the pre-run baseline, sampled every 2 ms. It therefore includes
  native allocations in GEOS, Rust and numba.

**Rust candidates.** They run in the same process through one pyo3 0.29 abi3 module, `bakegeo`
(`bakegeo/src/lib.rs`: i_overlay 9.0.0, clipper2-rust 1.2.0, rstar 0.13.0, rayon 1.12, `lto=fat`).
The installed `.pyd` is 1.07 MB.

**Stability.** Timings were re-run:
- Union, geodesic and Z0: one driver pass.
- Clearance and islands: 2 passes.
- Graph: 3 passes.

Run-to-run spread of the repeated capabilities was at most 17 % (jetson graph build, 1,034-1,217 ms). The first clearance pass ran an
earlier revision of `cap_clearance.py` and is superseded (`bakeoff.run1.jsonl`).

**Oracles.**
- **kicad-cli 10.0.6 `pcb drc`** runs on a scratch copy with one custom rule
  `(rule kw_bench (layer F.Cu) (constraint clearance (min C)))`.
- KiCad caps each violation type at 499 in the report, so `run_drc_tiled.py` adds N×N
  non-restricting rule areas and runs the rule once per tile (`A.intersectsArea('kw_tile_k')`). The
  union of the runs is the full set, and no tile reached the cap.
- Pairs are matched by the two items' UUIDs, and KiCad's "actual" value is parsed from the description.
- For the geodesic: scikit-fmm order 2 on wide copper (EDT ≥ 4 px), plus an exact 30×30 mm plate
  with a wall (taut-string ground truth).
- For Z0: scikit-fem P2 on a graded mesh.

---

## 3. Results: one table

Times are wall ms, including marshalling. "Peak" is MB above baseline. Correctness is judged
against the oracle named in each block.

### 3.1 (1) Stroke + union of one layer's copper, then inflate the union by 0.1 mm

Workloads: video F.Cu (6,288 items), video In2.Cu (2,263), jetson F.Cu (12,690), jetson In2.Cu
(6,359, plane layer with fractured fills).

| Engine | video F | video In2 | jetson F | jetson In2 | Peak MB | Inflate 0.1 mm (engine ms) |
|---|---|---|---|---|---|---|
| **i_overlay 9.0** | **55** | **46** | **118** | **97** | 10-31 | **59 / 78 / 109 / 152** |
| clipper2-rust 1.2 | 198 | 90 | 269 | 323 | 17-33 | 452 / 306 / 627 / 1,224 |
| shapely 2.1.2 (GEOS 3.13.1) | 553 | 611 | 1,198 | 1,513 | 20-48 | 162 / 489 / 1,629 / 1,025 |

Correctness:

| Layer | Union shapes / holes | Area agreement | Inflated shapes / holes |
|---|---|---|---|
| video F | 1,352 / 0 for all engines, equal to KiCad's `ConvertBrdLayerToPolygonalContours` + union | engines within 0.003 % of each other | KiCad 1,256 / 2; **clipper2 1,256 / 2**; shapely 1,301 / 1; i_overlay 1,302 / 0 |
| video In2 | 1,536 / 1,094 for all engines and KiCad | within 1e-6 relative | KiCad 1,536 / 1,109; **clipper2 1,109**; shapely 1,109; i_overlay 1,113 |
| jetson F | 2,957 / 54 for all engines | within 0.001 % | shapely 1,338 / 917; clipper2 1,338 / 928; i_overlay 1,381 / 823 |
| jetson In2 | 2,701 shapes for all engines. Holes: shapely 1,178, i_overlay 1,178, clipper2 1,154, because Clipper2 (like KiCad's `SHAPE_POLY_SET`) merges holes that touch at a vertex | within 0.001 % | 417 shapes for all engines |

The KiCad reference was measured through the 10.0.6 SWIG driver, `results/kicad_video.txt`. That
driver hangs on jetson, so the jetson rows have no KiCad reference.

**Reading.** i_overlay is 10-16× faster than shapely for the union and gives the same union
topology. Where engines diverge, it is at exact-touch inflate cases. There clipper2-rust is the one
that reproduces KiCad's topology, even though its inflate is sometimes slower than GEOS.
Zero-length strokes were emitted as discs for every engine, because i_overlay drops them (track 05).

### 3.2 (2) Every different-net clearance violation on one layer, with closest points

Workloads: video F.Cu at 0.25 mm (9,039 primitives) and jetson F.Cu at 0.15 mm (33,502 primitives).
The task's 0.15 mm on video gives 0 violations for both KiCad and every engine; the same holds at
0.20 mm. 0.25 mm was therefore added for a non-trivial check on the mid-size board.

| Engine | video 0.25 ms | jetson 0.15 ms | Peak MB | Pairs matched against kicad-cli DRC | Max \|Δ actual\| |
|---|---|---|---|---|---|
| **rstar + rayon (24 thr)** | **10.3** | **41.5** | 2-11 | 782/782 · 1,648/1,648 | 0.00005 mm |
| rstar (1 thread) | 12.9 | 55.3 | 3-11 | same | 0.00005 mm |
| pure Python (grid index + capsule distance) | 147 | 1,510 | 18-26 | same | 0.00005 mm |
| shapely (STRtree, fills indexed as edges) | 174 | 1,169 | 12-25 | same | 0.0001 mm |
| *kicad-cli DRC, one run (report capped at 499)* | *2,900-3,100* | *17,000* | | *oracle* | |
| *kicad-cli DRC, tiled so the report is complete* | *44,000 (16 tiles)* | *277,000 (9 tiles)* | | *oracle* | |

**Correctness detail.**
- **video, 0.25 mm:** 782 KiCad violations. Every engine found all 782, with no extras.
- **jetson, 0.15 mm:** 1,648 KiCad violations. Every engine found all 1,648 and none were missed.
  Every engine also reported the same 3 extra pairs, all zone fill against a different-net zone fill.
- **Excluding zone-to-zone pairs:** 1,605 against 1,605, max |Δ| 0.0001 mm.
- **Zone-to-zone pairs:** KiCad reports an actual gap up to 0.037 mm larger than the fill-to-fill
  gap. KiCad evaluates zone pairs in its own zone-to-zone test, not on the stored fills. That
  semantics is an open item for `kw_pcb_analyze` (report fill gaps, labelled as such).
- **Closest points:** every engine returns both points for every pair (1,651 of 1,651).

Every candidate gives KiCad's answer once the representation is right (track 01's lesson). The
choice is speed. rstar is 17-28× faster than shapely. Pure Python is as fast as shapely on these
layers, so shapely adds nothing here.

### 3.3 (3) Zone-fill islands: unfracture, count islands and holes, mark connected islands

Workloads:
- video In2.Cu: 4 fill rings, 53,551 vertices, 198 same-net contacts.
- jetson In4.Cu: 29 fill rings, 87,571 vertices, 1,516 contacts.

| Engine | video ms | jetson ms | Peak MB | Islands | Holes (video / jetson) | Connected |
|---|---|---|---|---|---|---|
| i_overlay | **32** | **48** | 10-12 | 4 · 29 | 989 / 1,503 | 4 · 29 |
| clipper2-rust | 48 | 57 | 11-12 | 4 · 29 | 989 / 1,501 (KiCad convention) | 4 · 29 |
| shapely `buffer(0)` | 39 | 75 | 8-11 | 4 · 29 | 989 / 1,504 | 4 · 29 |
| pure Python | 503 | 449 | 12-18 | 4 · 29 | 989 / 1,504 | 4 · 29 |
| raster 25 µm (numba scanline + OpenCV labels) | 254 | 176 | **313-564** | 4 · 29 | 989 / **1,508** | 4 · 29 |

**Oracle.** KiCad writes one `filled_polygon` per island, so islands equal the ring count, and every
engine matches it. The vector engines agree on area to 1e-4 mm². The raster is +0.30 / +2.2 mm²
and finds 4 spurious holes on jetson.

**Limits of this test.** None of the demo boards keeps an `(island)` flag. The `island_removal_mode 1`
zones exist, but none of their islands is isolated. "Connected" was therefore all-true on both
layers: the test proves agreement, not detection of an orphan island.

**Choice.** All vector engines are exact and fast. clipper2-rust is picked for KiCad's hole
convention. Pure Python is an adequate fallback at 0.45-0.50 s.

### 3.4 (4) Geodesic distance through copper to the nearest via, across one plane

Mask: the GND fills on In1.Cu, plus GND pads, vias and track capsules, drawn with the exact
pixel-centre even-odd scanline (numba, 26-94 ms, not included below). Seeds: the GND vias flashed on
In1.Cu.

| Engine | video 50 µm (12.3 Mpx, 104 vias) | jetson 50 µm (7.2 Mpx, 1,405 vias) | jetson 25 µm (29.0 Mpx) | Peak MB | Mean error against FMM (wide copper) | p99 \|error\| |
|---|---|---|---|---|---|---|
| **Rust 5×5 Borgefors chamfer** | **603** | **278** | **1,179** | 35-138 | +0.14 / +0.08 / +0.26 % | 1.5-1.8 % |
| numba 5×5, hand-inlined (`kern5.py`) | 697 | 333 | 1,307 | 28-111 | identical to Rust | identical |
| numba 5×5 with an `inline="always"` helper (track 02's form) | 9,630 | 5,385 | 21,048 | 28-111 | identical | identical |
| numba 3×3 chamfer | 247 | 120 | 481 | 28-111 | +4.77 / +4.92 / +5.12 % | 8.0-8.1 % |
| Rust 8-neighbour Dijkstra | 746 | 1,043 | 7,809 | 65-297 | same metric as 3×3 | 8.0-8.1 % |
| pure-Python 8-neighbour Dijkstra | 33,214 | 33,542 | not run | 299-409 | same metric as 3×3 | 8.0-8.1 % |
| *scikit-fmm order 2 (oracle)* | *4,850* | *5,184* | *38,834* | *173-799* | *reference* | |

**Reach** (maximum distance from any copper pixel to its nearest via), in mm:

| Engine | video, 50 µm | jetson, 50 µm | jetson, 25 µm |
|---|---|---|---|
| Rust 5×5 chamfer | 71.58 | 12.64 | 12.66 |
| scikit-fmm order 2 | 71.45 | 12.56 | 12.58 |
| 3×3 chamfer | 74.82 | 13.29 | 13.30 |

**Exact plate test** (50 µm pitch; `accuracy_plate.py`):

| Engine | Mean error | Max error |
|---|---|---|
| 5×5 chamfer | +0.22 % | 4.7 % (0.39 mm, next to the wall corner) |
| 3×3 chamfer / 8-neighbour Dijkstra | +5.76 % | 11.6 % (3.0 mm) |
| FMM | −0.003 % | 0.05 mm |

Every method reached every copper pixel. FMM is the most accurate on wide copper, but it is
8-33× slower, uses up to 6× the memory, and fails through 1-2 px tracks (tracks 02 and 03), so it
stays an oracle.

### 3.5 (5) Copper graph of one net and pad-to-nearest-via through copper

Net GND, all layers. The model is track 03's: split points on track centre lines, pad and via
attachments, seg-seg joins, and visibility edges inside each zone-fill island plus Kruskal "estimate"
edges.

| Board | Items | Fills | Nodes | Edges | Estimate edges | Pads |
|---|---|---|---|---|---|---|
| video | 1,371 | 4 | 1,125 | 1,404 | 81 | 250 |
| jetson | 15,881 | 25 | 4,293 | 21,491 | 23 | 1,266 |

**Build**, in ms (peak MB in brackets). The phases are those of the last run.

| Contacts from | video | jetson | jetson phases |
|---|---|---|---|
| **Rust kit** (`bakegeo` rstar + clipper2) | **215** (23) | **1,199** (86) | contacts 420, covered-test 308, Python assembly 546 |
| shapely | 212 (18) | 1,425 (71) | contacts 657, covered 314, assembly 529 |
| pure Python | 990 (34) | 10,736 (105) | contacts 5,547, covered 4,442, assembly 712 |

**Query** (all pads to the nearest via through the copper graph), in ms:

| Library | video | jetson |
|---|---|---|
| rustworkx 0.18.1 | 0.4 | 4.6 |
| scipy 1.17.1 csgraph | 0.9 | 5.3 |
| pure-Python heapq | 0.7 | 9.8 |

**Correctness.** All 250 / 1,266 pads were reached. The Rust and pure-Python builds are identical:
the sum of pad-to-via lengths is 3,381.248 / 1,002.869 mm, and the maximum is 104.172 / 9.774 mm.
The shapely build differs by 1 node and 8 edges on jetson (sum +0.22 mm, +0.02 %) because of a
floating-point difference in the covered-segment test. All three query libraries return identical
numbers.

**Reading.** The query is noise at 5-10 ms, so no graph library is needed (track 03 confirmed).
Build time is contact detection. Rust and shapely tie on video, and Rust leads by 16 % on jetson.
About 45 % of the Rust build is still Python-side graph assembly, which would move into the crate in
`kiwrap-geom` (not measured).

### 3.6 (6) Z0 of three line types on JLCPCB JLC04161H-7628

**Stack.** L1 over 0.2104 mm of 7628 prepreg, εr 4.4, 1 oz copper. The widths are those JLC's own
calculator gives for 50 Ω, 50 Ω and 100 Ω.

**Geometries.**
- "rect": T 35 µm, rectangular, no mask; this is what closed forms model.
- "jlc": JLC's documented calculator model: T 1.6 mil, etch 0.7 mil, mask 1.2 / 0.6 mil with εr 3.8.

**"rect" geometry, against FEM P2:**

| Engine | Microstrip W 0.3493 | GCPW W 0.294 / S 0.200 | Edge-coupled W 0.2052 / S 0.2032 (Zdiff) | Time per evaluation | Peak MB |
|---|---|---|---|---|---|
| **closed forms (tier 0, stdlib)** | 51.77 (−0.01 %) | 56.31 (**+7.4 %**) | 108.89 (−0.64 %) | **2.2 / 3.6 / 9.4 µs** | ~0 |
| **scikit-fem P1 (tier 1)** | 51.72 (−0.10 %) | 52.35 (−0.11 %) | 109.42 (−0.15 %) | 459 / 599 / 1,112 ms | 124-160 |
| scikit-fem P2 (reference) | 51.77 | 52.41 | 109.59 | 16-47 s | 1,120-1,440 |
| pcb-toolkit 0.2.1 (Rust CLI, oracle) | 51.77 (−0.01 %) | 49.49 (−5.6 %) | 107.79 (−1.6 %) | 11-15 ms per process | |
| KiCad 10.99 transline (GPL process, oracle) | 52.51 (+1.4 %) | 56.46 (+7.7 %) | 118.03 (+7.7 %) | 11-14 ms per process | |

**"jlc" geometry, against JLC's calculator (50 / 50 / 100 Ω):**

| Engine | Microstrip | GCPW | Edge-coupled (Zdiff) | Time per evaluation | Peak MB |
|---|---|---|---|---|---|
| scikit-fem P1 | 50.07 | 49.86 | 102.92 | 0.66 / 1.19 / 1.57 s | 156-217 |
| scikit-fem P2 | 50.09 | 49.86 | 102.93 | 25-55 s | 1,390-1,850 |

The coupled pair's +2.9 % against JLC is within track 04's +1.2…+4.1 % band for published JLC
pairs.

**Reading.** Closed forms are exact enough for microstrip and coupled microstrip, at microseconds
per evaluation. GCPW needs the FEM: every closed form tried, and pcb-toolkit, is off by 5-8 %.
P1 costs 0.1-0.15 % of accuracy against P2 for 30-40× less time and about 9× less memory, so P1 is the
tier-1 setting. KiCad's transline stays an oracle and a D19 fix candidate: its thickness is counted
twice (track 04).

---

## 4. Packaging, licence, wheels and maintenance of the winners

| Winner | Version (date) | Licence (file read) | Windows wheels, Py 3.11-3.13 | Installed size | Python / Rust |
|---|---|---|---|---|---|
| i_overlay | 9.0.0 (2026-09-19) | MIT OR Apache-2.0 (`Cargo.toml`; the crate ships no LICENSE file) | via our abi3 wheel | inside `kiwrap-geom` | Rust; Python via pyo3 |
| clipper2-rust | 1.2.0 (2026-09-18) | BSL-1.0 (`LICENSE`; object-code exemption) | via our abi3 wheel | inside `kiwrap-geom` | Rust; Python via pyo3 |
| rstar | 0.13.0 (2026-05-24) | MIT OR Apache-2.0 (`Cargo.toml`) | via our abi3 wheel | inside `kiwrap-geom` | Rust |
| rayon / pyo3 | 1.12.0 / 0.29.2 | MIT OR Apache-2.0 (`LICENSE-MIT`, `LICENSE-APACHE`) | pyo3 abi3-py311: one cp311-abi3-win_amd64 artefact serves 3.11-3.14 | bakegeo `.pyd` 1.07 MB | |
| chamfer kernel | ours (~70 lines Rust, ~90 lines numba) | MIT | n/a | n/a | both |
| pure-Python kit | ours | MIT | stdlib | 0 | Python |
| scikit-fem (tier 1, extra) | 12.0.2 (2026-06-05) | BSD-3-Clause (`LICENSE`) | pure Python. Needs numpy 2.4.6 (last cp311) and scipy 1.17.1 (last cp311) | 2 + 27 + 92 MB | Python |
| closed forms (tier 0) | ours, re-derived from papers | MIT | stdlib | about 300 lines | Python |

Runners-up and oracles, checked in the same venv:

| Package | Licence | Installed size | Role |
|---|---|---|---|
| shapely 2.1.2 | BSD-3; GEOS LGPL-2.1 in a separate DLL (`LICENSE_GEOS`) | 3 MB + GEOS DLLs + numpy 27 MB | test oracle |
| numba 0.67.0 | BSD-2 (`LICENSE`) | 21 MB + llvmlite 116 MB | fallback if native is refused |
| opencv-python-headless 5.0.0.93 | Apache-2.0 | 113 MB | not needed once the chamfer is in Rust |
| scikit-fmm 2025.6.23 | BSD-3 | 1 MB; no cp314 wheel | oracle |
| rustworkx 0.18.1 | Apache-2.0 | 7 MB | not needed |
| pcb-toolkit 0.2.1 | MIT OR Apache-2.0 | small CLI | oracle, young crate |
| KiCad | GPL | n/a | oracle, as a separate process only (D8) |

Maintenance activity and the robustness history of each winner are in tracks 01-05. All three Rust
geometry crates had releases in 2026.

## 5. Robustness observed in the bake-off

1. **Stroke-then-inflate topology is engine-specific at exact touches.** Clearance must be decided by
   a distance compared with `C − 0.0005 mm`, never by inflating and testing overlap. Every engine that
   did so matched DRC exactly.
2. **Hole conventions differ.** Clipper2, like KiCad, merges holes that touch at a vertex (1,154 against
   1,178; 1,501 against 1,504). Report hole counts from clipper2-rust when they must match KiCad.
3. **Fractured fills.**
   - GEOS needs `make_valid(method="structure")` or `buffer(0)` first.
   - Both Rust engines unfracture natively.
   - The raster needs the even-odd pixel-centre scanline. With it, it still finds 4 extra holes on
     jetson at 25 µm.
4. **Zero-length tracks** must be emitted as discs before i_overlay sees them (track 05).
5. **numba performance is fragile.** The same 5×5 kernel is 14-16× slower when written with an
   `inline="always"` helper. If numba is ever used, kernels must be hand-inlined and have a timing
   test.
6. **The kicad-cli report caps each type at 499**, which silently truncated both single DRC runs. Any
   T3 DRC oracle in kiwrap must tile or detect the cap. `run_drc` already reports `capped`.

## 6. Recommendation

1. Build **`helper/kiwrap-geom`** as the second crate in the Cargo workspace: a pyo3 0.29 abi3-py311
   wheel built in CI, with `bakegeo/src/lib.rs` as the proven starting point. It contains:
   - i_overlay for stroke + union;
   - clipper2-rust for offset, inflate and unfracture (KiCad parity);
   - an rstar edge index with exact capsule and fat-primitive distance, containment and closest
     points (rayon);
   - the 5×5 Borgefors chamfer and the implicit-grid Dijkstra;
   - graph-contact detection, with the graph assembly moved into the crate as well.

   Gate it with the capability key `native.geom`.
2. Keep the **pure-Python kit** in `kiwrap.formats` (zero-dependency) as the reference and fallback
   for clearance, islands, unfracture and the graph. It is measured interactive on the largest demo:
   ≤ 1.5 s per layer for clearance and ≤ 0.5 s for islands. The graph build takes 10.7 s for jetson
   GND, so use `run_async` above a budget. The raster geodesic falls back to pure Dijkstra only below
   about 4 Mpx, tagged `degraded`.
3. Without `native.geom`, **union and offset** are the one gap. Either make shapely an optional
   extra (`kiwrap[analysis]`) with a teaching error, or refuse with the fix "install the kiwrap-geom
   wheel". shapely stays in tests as an oracle either way.
4. **Z0:** tier-0 closed forms in `kiwrap.formats`. scikit-fem P1 in `kiwrap[fields]`, cached per
   cross-section; tag GCPW closed forms `estimate`. Fix `addsublayer` stacking first (track 04).
5. **Oracles in conformance tests:**
   - tiled kicad-cli DRC (clearance parity, excluding zone-to-zone pairs until their semantics are
     modelled);
   - scikit-fmm on wide copper and the exact plate (geodesic);
   - KiCad SWIG `ConvertBrdLayerToPolygonalContours` on 10.x (union topology);
   - scikit-fem P2 (Z0).

   The owner decides the dependency questions: shapely as an extra or not, and whether `kiwrap[fields]`
   pins numpy < 2.5 and scipy < 1.18 or needs Python 3.12+.

## 7. Sources

Measurements: `scratchpad/tooling/bakeoff/results/bakeoff.jsonl`, `clearance_extra.jsonl` and
`geodesic_extra.jsonl`; DRC reports in `drc/*/drc.json`. The track reports in this directory,
01-polygon-engines, 02-raster-geodesic, 03-graphs-and-paths, 04-em-impedance-current and
05-kicad-native-and-rust, hold the web sources, release dates and maintenance data for each package.

Packages, with licences read from the installed dist-info or the cargo registry sources on 2026-09-24:

| Package | Source |
|---|---|
| i_overlay 9.0.0 | https://crates.io/crates/i_overlay · https://github.com/iShape-Rust/iOverlay |
| clipper2-rust 1.2.0 | https://crates.io/crates/clipper2-rust |
| rstar 0.13.0 | https://crates.io/crates/rstar · https://github.com/georust/rstar |
| rayon 1.12.0 | https://crates.io/crates/rayon |
| pyo3 0.29.2 | https://crates.io/crates/pyo3 |
| maturin 1.15.0 | https://pypi.org/project/maturin/ |
| shapely 2.1.2 | https://pypi.org/project/shapely/ (GEOS 3.13.1, `LICENSE_GEOS` LGPL-2.1) |
| numba 0.67.0 | https://pypi.org/project/numba/ |
| opencv-python-headless 5.0.0.93 | https://pypi.org/project/opencv-python-headless/ |
| scikit-fmm 2025.6.23 | https://pypi.org/project/scikit-fmm/ |
| rustworkx 0.18.1 | https://pypi.org/project/rustworkx/ |
| scipy 1.17.1 | https://pypi.org/project/scipy/ |
| numpy 2.4.6 | https://pypi.org/project/numpy/ |
| scikit-fem 12.0.2 | https://pypi.org/project/scikit-fem/ |
| pcb-toolkit 0.2.1 | https://crates.io/crates/pcb-toolkit |
| KiCad 10.0.6 | kicad-cli and the SWIG driver; 10.99 for the transline harness (track 04) |
| JLCPCB JLC04161H-7628 stack-up and impedance calculator | https://jlcpcb.com/impedance (values as collected in track 04) |
