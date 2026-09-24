# Raster and distance-field tooling for pour and plane analyses (track: raster-geodesic)

Research for the raster half of `kw_pcb_analyze` in
[requests/kiwrap-feature-requests.md](../../../requests/kiwrap-feature-requests.md):
- item 27: `gnd_reach`, `pour_necks`, islands and slivers;
- the geodesic parts of items 28 (`copper_path`), 30 (`nearest_via` / neck width) and 35 (bottleneck width);
- the plane-void inputs of items 25 and 26;
- the raster overlays of item 39.

It answers the owner's request in D23 ("research the best tooling, not just numpy") for the raster
kit that [review §6 T5](../../../requests/kiwrap-feature-requests.review.md) sketched as pure-Python
`formats/geometry/raster.py`. Polygon booleans, offsets and closest-point clearance belong to the
geometry tracks. Graph queries are in [03-graphs-and-paths.md](03-graphs-and-paths.md), and the
native/Rust route is in [05-kicad-native-and-rust.md](05-kicad-native-and-rust.md).

**Test setup.**
- Date: 2026-09-24.
- Machine: Windows 11, Core Ultra 9 285K (24 threads), 127 GB RAM, RTX 5080 (driver 616.92).
- Software: Python 3.11 in scratch venvs, Rust 1.95.
- `[MEASURED]` means reproduced on this machine on real demo-board copper extracted with kiwrap's own
  `BoardFile` (KiCad 10.0.6 demos).

**Measurement caveats.**
- Timings are single runs, one process per method for the geodesic runs.
- Other research tracks were benchmarking on the same machine at the same time, so expect ±20 % noise.
  Treat anything within 2× as a tie.
- "peak+" is the peak increase in process RSS during the step, sampled every 2 ms.

---

## 1. Verdict

1. **At 25 µm, every analysis except an accurate geodesic takes under a second on a whole 12-layer-board
   plane (60 Mpx).** vme-wren In1.Cu GND measured with numpy + OpenCV + a small compiled loop `[MEASURED]`:

   | Step | Time |
   |---|---|
   | rasterise | 0.08 s |
   | islands | 0.04 s |
   | exact EDT | 0.08 s |
   | necks < 0.3 mm | 0.68 s |
   | geodesic reach to every via (3×3 chamfer) | 0.95 s |

   The whole pipeline, including a 1 %-accurate geodesic, takes 14.9 s at 25 µm, 3.2 s at 50 µm and
   0.7 s at 100 µm.
2. **The library choice matters less than three robustness traps:**
   - **The rasteriser.** OpenCV `fillPoly` and Pillow `ImageDraw.polygon` fill boundary pixels and draw
     the zero-width bridges of KiCad's *fractured* fills. They add 0.2–8.5 % copper, cut voids in two
     (vme: 3,020 voids with OpenCV against the true 2,998) and would short narrow gaps.
   - **The geodesic metric.** 8-neighbour Dijkstra, scikit-image `MCP_Geometric` and a 3×3 chamfer all
     overestimate by **+5.8 % on average and up to 10 %**. A 5×5 chamfer gives +1.3 % on average; with
     Borgefors weights it gives **+0.27 %**.
   - **Fast marching (scikit-fmm) is the most accurate on wide copper and breaks on thin copper.**
     - Wide copper: mean error 0.00 %, max 25 µm at 25 µm pitch.
     - Thin copper: +41 % on a 2-px diagonal strip, and it refuses a 1-px strip.
     - It is 130× slower than a chamfer sweep and has no Windows wheel for Python 3.14.
3. **Recommended stack.**
   - **Runtime: numpy + opencv-python-headless + one compiled sweep kernel.** Use numba in Python, or
     the same ~80 lines in the Rust helper. Rasterise with an exact even-odd, pixel-centre scanline:
     numba, pure Python (0.24 s at 60 Mpx) or skia-python / tiny-skia (0.03–0.23 s).
   - **Not needed at runtime:**
     - scipy: its EDT takes 3.6 s against OpenCV's 0.08 s;
     - scikit-image: skeletons take 12 s and `MCP` 30–96 s;
     - scikit-fmm;
     - GPUs;
     - pyvips;
     - the LGPL/GPL helpers (cc3d, edt, dijkstra3d).
   - **Where scikit-fmm and shapely belong:** in tests only, as independent oracles.
4. **The GPU (RTX 5080) is not worth it.**
   - cupy: EDT, labelling and opening take 20–70 ms, against 40–110 ms on the CPU with OpenCV. The
     ~0.1 s host↔device copy cancels the gain.
   - GPU geodesics (Jacobi relaxation, FastGeodis-style row sweeps) take 2.2–3.0 s, against 0.54–0.95 s
     for the CPU chamfer.
   - torch plus the CUDA wheels is 6.4 GB installed. `[MEASURED]`
5. **Zero dependency is possible but slow.** A pure-Python multi-source BFS takes 48–66 s at 60 Mpx and
   has a +28 % L1-metric error. The pure-Python scanline rasteriser is fine: 0.24 s at 60 Mpx, because
   row slices are assigned in C. Keep the pure-Python path only as a small-window fallback (≤ 4 Mpx,
   ~3 s) tagged `degraded: raster_fallback`.
6. **Pixel budget.**
   - **Neck counts** converge at 25 µm (596 → 48 → 43 → 43 → 43 from 100 µm down to 10 µm) for a 0.3 mm
     threshold. The rule is ≥ 12 px across the threshold width.
   - **Reach** converges at 100 µm (25.75 mm against 25.78 mm).
   - **Sizes.** 10 µm is 378 Mpx: 86 s and +4 GB peak. Budget ≤ 150 Mpx per job (≈ 1.8 GB peak), and
     window or tile above that.

---

## 2. What the items need, as raster problems

| Item | Raster operation | Minimum pitch that is safe |
|---|---|---|
| 27 `gnd_reach` | multi-source geodesic distance through one net's copper on one layer, max + location per island | 50–100 µm (converged, §5.5) |
| 27 `pour_necks` | morphological opening by a disc of radius w/2, necks = removed parts that join ≥ 2 opened parts | w/12 (25 µm for w = 0.3 mm) |
| 27 islands / slivers | connected components (copper), area / via membership; slivers = parts removed by the opening that touch no opened part | fills are already per-island polygons (vector is exact); raster only for composed copper |
| 28 / 30 geodesic pad → via length, neck width along the path | geodesic + nearest-source id; EDT sampled along the back-traced path | 25 µm (tracks are 4–8 px wide) |
| 35 bottleneck | max-min width path = widest-path on the EDT (bottleneck Dijkstra) | 25 µm |
| 25 / 26 plane voids, slots | label the complement (voids), merge antipads by closing | 25–50 µm |
| 39 overlays | rasterise + colour, contours back to vector (`cv2.findContours`) | render scale |

---

## 3. Workloads (real, extracted with kiwrap's file model)

Extracted by `extract.py` (§10) with `BoardFile.load(...)`: zone `filled_polygon`s (fractured single
rings, even-odd), through and blind vias spanning the layer, THT pads of the net, and every copper
item of F.Cu.

| Workload | Board | Content | Grid at 25 µm |
|---|---|---|---|
| `vme_GND_In1Cu` | vme-wren (12 layers, 1,508 fp) | GND fill: 1 ring, 114,288 vertices, 2,998 voids; 1,503 vias + 112 THT pads | 6,446 × 9,379 = **60.5 Mpx** |
| `vme_P1V8_In6Cu` | vme-wren | P1V8 fill: 155,641 vertices, 4,038 voids, 124 vias | 60.5 Mpx |
| `jetson_GND_In1Cu` | jetson-agx-thor-baseboard (10 layers) | GND fill: 72,879 vertices, 1,247 voids, 1,405 vias + 51 THT | 7,240 × 4,040 = **29.2 Mpx** |
| `coldfire_GND_In1Cu` | kit-dev-coldfire (4 layers) | 5 islands, 14,538 vertices, 44 vias + 33 THT | 23.4 Mpx |
| `vme_FCu_all` | vme-wren F.Cu | 10,812 track segments (arcs flattened), 6,141 pad polygons, 4,374 via discs, 67 zone rings | 60.5 Mpx |
| `jetson_FCu_all` | jetson F.Cu | 7,029 segments, 2,852 pads, 3,235 vias, 49 zone rings | 29.2 Mpx |

Load time with `BoardFile.load`: vme-wren 8.6 s, jetson 13.6 s, coldfire 0.5 s. Graph track 03
reports the same order.

---

## 4. Candidates

Licence is read from the installed wheel's `LICENSE` / `METADATA` (dist-info) or the crate's
`Cargo.toml`. Versions and dates come from the PyPI JSON API on 2026-09-24.

"Win wheels" gives the CPython tags that have `win_amd64` wheels **for the latest release**. Size is
the cp311 wheel download / measured installed size.

| Candidate | Version (date) | Licence | Win wheels | Size | Import from MIT core? | Role |
|---|---|---|---|---|---|---|
| **numpy** | 2.5.3 (2026-09-06); **2.4.6 is the last with cp311** | BSD-3 (+0BSD/MIT/Zlib/CC0 parts) | cp312–315 (2.5); cp311–314 (2.4.6) | ~44 MB inst. | yes | arrays |
| **opencv-python-headless** | 5.0.0.93 (2026-07-02) | Apache-2.0 (OpenCV). The wheel also ships `opencv_videoio_ffmpeg*.dll` (LGPL, a separate dynamically loaded DLL; unused here) | abi3 cp37 → every CPython 3.x | 44 MB wheel / **113 MB** inst. (30 MB of it the ffmpeg DLL). contrib: 54 / 138 MB | yes (LGPL part only dynamically loaded; ship the 3rd-party notice) | islands, EDT, opening, contours, thinning (contrib) |
| **numba** (+ llvmlite) | 0.67.0 (2026-08-11) | BSD-2 | cp310–314 (+ win_arm64) | 2.8 MB wheel; 14 MB + **116 MB llvmlite** inst. | yes | compiled sweep / Dijkstra / scanline kernels |
| skia-python | 144.0.post2 (2026-03-19) | BSD-3 | cp38–314 (+ arm64) | 11 MB wheel / 25 MB inst. | yes | exact even-odd fills, round-cap strokes |
| scipy (ndimage, csgraph) | 1.18.1; **1.17.1 last cp311** | BSD-3 | cp312–315 (1.18) | ~110 MB inst. | yes | EDT, label, grid Dijkstra (all slower than OpenCV / numba here) |
| scikit-image | 0.26.0 (2025-12-20) | BSD-3 | cp311–314 | 12 MB wheel / 22 MB inst. | yes | skeletonize, medial_axis, `MCP_Geometric`, isotropic ops |
| scikit-fmm | 2025.6.23 (2025-06-23) | BSD-3 | **cp310–313 only (no cp314)** | 0.1 MB / 0.2 MB | yes | fast marching (eikonal), order 1/2 |
| rasterio (GDAL) | 1.5.1; **1.4.4 last cp311** | BSD-3 (GDAL MIT/X) | cp312–315 (1.5) | 68 MB inst. | yes | exact pixel-centre burn-in |
| Pillow | 12.3.0 (2026-07-01) | MIT-CMU | cp310–315 | 7 MB / 15 MB | yes | `ImageDraw` |
| matplotlib | 3.11.2 | PSF-style (matplotlib licence) | yes | 23 MB inst. | yes | `Path.contains_points` |
| pyvips + pyvips-binary | 3.2.0 / 8.18.6 (2026-08-26) | wrapper MIT. The bundled libvips DLL is LGPL (upstream LGPL-2.1+; the binary wheel metadata says LGPL-3.0-or-later) | abi3 | 8 MB / 18 MB DLL | only with the DLL dynamically linked and replaceable | `labelregions`, `fill_nearest` |
| connected-components-3d (cc3d) | 4.1.0 (2026-08-26) | **LGPL-3.0-or-later** | cp39–315 | 0.8 MB | same LGPL conditions | labels |
| edt (seung-lab) | 3.1.2 (2026-07-02) | **LGPL-3.0-or-later** | cp38–314 | 0.2 MB | same | multithreaded EDT |
| dijkstra3d | 1.15.2 (2025-11-06) | **GPL-3.0-or-later** | cp38–314 | 0.3 MB | **no** (separate process only, D8) | grid Dijkstra |
| cupy-cuda12x (+`[ctk]`) | 14.2.0 (2026-08-20) | MIT | cp310–314 | 99 MB wheel; 146 MB + 2.3 GB `nvidia-*` CUDA wheels | yes | GPU EDT/label |
| torch cu128 | 2.11.0+cu128 | BSD-3 | yes | **4.1 GB** inst. | yes | GPU relaxation |
| FastGeodis | 1.0.5 (**sdist only**, 2024-02-09) | BSD-3 | none (build needs torch + nvcc/MSVC; built CPU-only here) | – | yes | raster-scan geodesic |
| GeodisTK | 0.1.7 (2020-08-25) | MIT | none | – | – | stale |
| Rust **tiny-skia** | 0.12.0 | BSD-3 | crate | – | helper crate | raster (even-odd Mask) |
| Rust **imageproc** (+ image) | 0.27.0 / 0.25.10 | MIT / MIT-or-Apache-2.0 | crate | exe 0.5 MB total | helper crate | labels, exact EDT, morphology |

---

## 5. Measurements `[MEASURED]`

### 5.1 Rasterising a real plane fill (fractured single ring, even-odd)

Error is against the exact vector area of the ring (shoelace). The islands (8-connected copper) and
voids (4-connected complement) counts are topological checks.

| Rasteriser | vme GND 60.5 Mpx | jetson GND 29.2 Mpx | area err (vme) | voids (vme, true 2,998) | notes |
|---|---|---|---|---|---|
| **numba scanline** (pixel centres, even-odd) | **0.07 s** / +116 MB | 0.04 s | +0.0047 % | 2,998 | reference; JIT 0.6 s cached, 3–6 s first ever |
| **pure-Python scanline** (bytearray row slices) | **0.24 s** | – | +0.004 % | – | zero dependency |
| **skia-python** (no AA, `kEvenOdd`) | 0.23 s / +128 MB | 0.12 s | +0.0037 % | 2,998 | 2,394 px (0.004 %) differ from exact centre sampling |
| **tiny-skia** (Rust `Mask::fill_path`) | 0.03 s | 0.01 s | +0.004 % | – | |
| rasterio `features.rasterize` | 2.50 s / +187 MB | 0.75 s | +0.0047 % | 2,998 | 4 px from the numba result |
| OpenCV `fillPoly` (shift=8) | 0.08 s | 0.04 s | **+0.29 %** | **3,020** | boundary-inclusive; draws fracture bridges |
| Pillow `ImageDraw.polygon` | 3.26 s | 0.85 s | **+0.21 %** | **3,006** | boundary-inclusive |
| scikit-image `draw.polygon` | 24.5 s at **1.5 Mpx** (coldfire, 100 µm) | – | +0.07 % | – | O(pixels × vertices); impractical |
| matplotlib `contains_points` | ~58 s at 1.5 Mpx (extrapolated from 23 rows) | – | – | – | impractical |

At coldfire 100 µm, OpenCV adds 27 false voids and Pillow merges two islands (4 against the true 5
islands). On pour workloads, `fillPoly` and `ImageDraw` are **wrong, not just imprecise**.

A whole signal layer (F.Cu, union semantics) is rasterised as follows:
- tracks: round-capped strokes;
- arcs: flattened;
- pads: polygons ORed;
- vias: discs;
- zones: even-odd.

The reference is shapely `buffer` + `union_all` → rasterio burn.

| Method | vme F.Cu 60.5 Mpx | jetson F.Cu 29.2 Mpx | xor vs vector union |
|---|---|---|---|
| shapely buffer + `union_all` (vector) | 3.94 s | 2.12 s | – (area 7,691.0 mm²) |
| rasterio burn of that union | 0.30 s | 0.20 s | reference |
| **skia-python** (strokes grouped by width, round caps) | **0.17 s** | 0.09 s | 0.19 % / 0.16 % |
| **numba** capsules + per-pad scanline | 0.42 s | 0.21 s | 0.17 % / 0.06 % |
| OpenCV `line(thickness)` + `circle` + `fillPoly` | 0.23 s | 0.14 s | **8.5 % / 4.5 %** (integer thickness, inclusive edges) |
| Pillow `line` + `ellipse` caps | 0.31 s | 0.20 s | **7.8 % / 4.4 %** |

### 5.2 Islands, EDT, opening, skeleton (vme GND 60.5 Mpx / jetson 29.2 Mpx)

| Operation | vme | jetson | Notes |
|---|---|---|---|
| **OpenCV `connectedComponentsWithStats`** 8-conn | **0.05 s** / +296 MB | 0.03 s | 24 threads; stats give area/bbox per island |
| scipy `ndimage.label` | 0.20 s | 0.13 s | |
| cc3d (LGPL) | 0.13 s | 0.06 s | |
| scikit-image `measure.label` | 0.43 s | 0.42 s | |
| pyvips `labelregions` (LGPL) | 0.22 s | 0.11 s | labels both phases |
| cupy `cupyx.scipy.ndimage.label` | 0.021 s (+0.1 s upload) | 0.037 s | |
| imageproc `connected_components` (Rust) | 1.08 s | 0.56 s | |
| **OpenCV `distanceTransform(DIST_L2, DIST_MASK_PRECISE)`** f32 | **0.08–0.11 s** / +236 MB | 0.04 s | exact: equals scipy to 1.5e-5 px |
| edt (LGPL, 24 threads) | 0.45 s | 0.29 s | identical to OpenCV |
| scipy `distance_transform_edt` f64 | 3.64 s / **+1.9 GB** | 2.09 s | |
| OpenCV `DIST_L2` 5×5 mask (approx) | 0.25 s | 0.16 s | up to 9.4 px wrong; do not use |
| pyvips `fill_nearest(distance)` | 63.3 s | 32.1 s | 0.34 px off |
| cupy EDT | 0.02–0.07 s | 0.066 s | + transfers |
| imageproc exact EDT (Rust, f64) | 0.90 s | 0.39 s | |
| **Opening by r = 0.15 mm via two EDT thresholds + labels + neck classification** | **0.79 s** / +1.1 GB | 0.46 s | |
| OpenCV `morphologyEx(OPEN, ellipse 13×13)` | 0.03 s | 0.02 s | digital disc: 58–137k px differ from the Euclidean opening |
| scipy `binary_opening` 13×13 | – | 1.60 s | same as OpenCV |
| scikit-image `isotropic_opening` | 6.77 s / +2.0 GB | 3.89 s | |
| OpenCV contrib `thinning` Zhang-Suen / Guo-Hall | – | 13.2 s / 19.4 s | |
| scikit-image `skeletonize` / `medial_axis` | – | 12.4 s / 22.3 s (+1.6 GB) | |

**Neck and sliver finding.** The EDT-threshold opening uses erode at `edt > r + ½px` and dilate at
`dist_to_eroded ≤ r`. Then:
- thin = copper − opened;
- a thin component that touches ≥ 2 opened components is a **neck**;
- one that touches 1 is a pocket or peninsula;
- one that touches 0 is a sliver.

On the plane fills:
- jetson GND: **39 necks**, the narrowest ≈ 0.13 mm at (147.6, 122.7) mm, and 27,253 pockets, of
  which only **44 are > 0.05 mm²**. The rest are the rounded-off corners that any opening removes, so
  report pockets with an area/depth threshold.
- vme GND: **43 necks**, the narrowest ≈ 0.175 mm at (150.7, 248.4).
- vme P1V8: 74 necks at 25 µm.

**Skeletons are not needed.** Opening + labels answers "where is the pour narrower than w" 15–25×
faster than any thinning. Width along a path is the EDT sampled on the path.

### 5.3 Geodesic reach (`gnd_reach`) at 25 µm

Every copper pixel gets the distance through the pour to the nearest GND via or THT pad.

| Method | vme GND 60.5 Mpx | jetson GND 29.2 Mpx | Metric | Reach max (vme / jetson) |
|---|---|---|---|---|
| **numba iterated 3×3 chamfer sweeps** (6 fwd+bwd passes) | **1.15 s** / +231 MB (0.95 s in pipeline) | **0.54 s** / +112 MB | octile (=d8) | 27.21 / 13.30 mm |
| numba iterated 5×5 chamfer sweeps (generic loop) | 13.1–14.0 s | 5.45 s | 16-neighbour | 25.78 / 12.80 |
| numba multi-source Dijkstra 8-nbr (heapq, + nearest-via id) | 32.9 s / +921 MB | 15.1 s | octile | 27.21 / 13.30 |
| numba Dijkstra 16-nbr | 56.2 s | 22.6 s | 16-neighbour | 25.78 / 12.80 |
| scikit-fmm order 1 | 77.4 s / +1.7 GB | 34.7 s | eikonal | 25.69 / 12.61 |
| scikit-fmm order 2 | 124.9 s / +1.7 GB | 35.3 s | eikonal | **25.57 / 12.58** |
| scikit-image `MCP_Geometric` | 96.3 s / **+4.4 GB** | 30.0 s / +2.1 GB | octile | 27.21 / 13.30 |
| scipy `csgraph.dijkstra(min_only)` on an explicit grid graph | 48.1 s / **+12 GB** | 29.3 s / +6.0 GB | octile | 27.21 / 13.30 |
| pure-Python BFS, 4-nbr | 48.1 s (66.2 s vias only) / +1.1 GB | 17.2 s | L1 (+28 %) | 34.85 / 14.50 |
| cupy Jacobi relaxation (RTX 5080) | 2.48 s (1,024 it) | 3.00 s | octile | = CPU d8 (1e-2 px) |
| cupy directional row sweeps (FastGeodis scheme) | 2.37 s (4 it) | 2.24 s | octile | = CPU d8 |
| torch Jacobi (8 shifted mins) | 59.7 s | 6.4 s | octile | = CPU d8 |
| FastGeodis 1.0.5 (CPU; no CUDA build possible without nvcc) | – | 4.6 s (2 it) | raster-scan | – |
| Rust naive port of the 3×3 sweep | 2.69 s | 1.28 s | octile | 29.28 (vias only) |

Cross-check with graph track 03 §5.2: with vias only as sources, the Rust sweep gives 29.28 mm.
Track 03 measured 29.35 mm at 0.1 mm. With THT pads as extra sources and seed snapping inside the
via pad, the reach is 27.21 mm (octile) or 25.57 mm (FMM2). The difference is the metric, not a
disagreement.

**Chamfer sweeps converge in 5–6 iterations on real planes.** Voids are small antipads, so paths
rarely turn more than a few times, which makes them 30× faster than Dijkstra with the same answer. The
5×5 loop is written generically and is 10× slower than the unrolled 3×3 loop, so there is headroom.
One adversarial case needs a lot of sweeps: a serpentine pour (coldfire at 25 µm took 3.8 s at
23 Mpx). Cap the iterations and fall back to Dijkstra for that case.

### 5.4 Metric accuracy against an exact answer

The test plate is 30 × 30 mm with a 2 mm wall void from the left edge to x = 20 mm and a via at
(5, 5). The exact distance is the taut string around the wall corners. Pixels within 1 mm of the via
are excluded.

| Method | 25 µm: mean / max rel. err | max abs | 50 µm: mean / max | max abs |
|---|---|---|---|---|
| d8 = 3×3 chamfer = `MCP_Geometric` = csgraph | **+5.82 % / 9.98 %** | 3.02 mm | +5.76 % / 11.6 % | 3.01 mm |
| 3×3 chamfer, Borgefors weights (0.955, 1.369) | +1.77 % / 5.80 % | 1.36 mm | +1.71 % / 7.44 % | 1.36 mm |
| d16 = 5×5 chamfer (1, √2, √5) | +1.26 % / 4.27 % | 0.73 mm | +1.20 % / 5.84 % | 0.72 mm |
| **5×5 chamfer, Borgefors weights (0.9866, 1.4035, 2.2166)** | **+0.27 % / 3.13 %** | 0.40 mm | +0.22 % / 4.68 % | 0.39 mm |
| scikit-fmm order 1 | +0.24 % / 4.58 % | 0.11 mm | +0.42 % / 8.02 % | 0.20 mm |
| **scikit-fmm order 2** | **−0.00 % / 2.34 %** | **0.025 mm** | −0.00 % / 4.65 % | 0.05 mm |
| pure-Python BFS 4-nbr | +28.05 % / 43.9 % | 16.1 mm | +28.0 % / 46.3 % | 16.1 mm |

**Robustness on thin copper.** The test strip runs diagonally, 1–4 px wide (track 03 found the same on
F.Cu tracks):

| Method | 1 px | 2 px | 3 px | 4 px |
|---|---|---|---|---|
| scikit-fmm | error: "no zero level set" | +41 % | +14 % | +4.6 % |
| chamfer / Dijkstra | exact | exact | exact | exact |

FMM's 4-neighbour upwind stencil cannot follow diagonal features narrower than ~5 px. That rules it
out for mixed track/pour masks and makes it a plane-only oracle.

### 5.5 Pixel budget: the recommended CPU pipeline at five pitches (vme-wren In1.Cu GND)

The pipeline is the numba scanline, OpenCV islands, OpenCV EDT, EDT-opening necks (w = 0.3 mm) and the
3×3 and 5×5 chamfer geodesics. "peak+" is the largest step.

| Pitch | Grid | Mpx | raster | islands | EDT | necks | geo 3×3 | geo 5×5 | FMM2 | Total (no FMM) | Peak+ | Necks | Reach 5×5 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 100 µm | 1,612 × 2,345 | 3.8 | 0.03 | 0.01 | 0.01 | 0.06 | 0.05 | 0.55 | 2.6 s | **0.72 s** | 48 MB | 596 ✗ | 25.75 |
| 50 µm | 3,223 × 4,690 | 15.1 | 0.04 | 0.01 | 0.03 | 0.18 | 0.25 | 2.66 | 18.7 s | **3.2 s** | 165 MB | 48 | 25.78 (FMM2 25.60) |
| 25 µm | 6,446 × 9,379 | 60.5 | 0.08 | 0.04 | 0.08 | 0.68 | 0.95 | 13.1 | 125 s | **14.9 s** | 673 MB | **43** | 25.78 |
| 15 µm | 10,742 × 15,632 | 168 | 0.16 | 0.10 | 0.30 | 1.84 | 3.05 | 32.7 | – | 38.1 s | 1.8 GB | 43 | 25.78 |
| 10 µm | 16,113 × 23,447 | 378 | 0.32 | 0.24 | 0.99 | 4.23 | 6.71 | 73.5 | – | 86.0 s | **4.0 GB** | 43 | 25.79 |

vme P1V8 In6.Cu at 25 µm: 18.9 s total (5×5 geodesic 15.5 s), 74 necks, reach 73.2 mm (124 vias).

**Where it becomes impractical.**
- Everything except the 5×5 geodesic stays under 1 s up to 60 Mpx and under 13 s at 378 Mpx.
- The 5×5 geodesic passes 10 s at about 50 Mpx.
- Memory passes 2 GB at about 170 Mpx.

**Budget rules.**
- Necks and islands at 25 µm, or w/12 in general.
- Reach at 50 µm.
- Refuse or tile above 150 Mpx, which keeps a job under ~2 GB on a 16 GB laptop.
- Window by `bbox_mm` whenever the caller gives one.

### 5.6 Vector comparison (shapely 2.1.2 / GEOS), jetson GND

| Operation | Vector | Raster 25 µm |
|---|---|---|
| unfracture (`make_valid`) | 1.13 s (area identical, 1,246 holes) | not needed |
| opening by 0.15 mm (`buffer(-r).buffer(+r)`) | 0.63 s | ~0.15 s (2 EDTs) |
| thin = P − opened | 2.14 s | 0.02 s |
| classify necks (STRtree + Python loop over 5,320 parts) | 14.1 s | 0.3 s (label + adjacency) |
| opened components | 51 | 44 |
| necks (≥ 2 opened parts touched) | 116 (72 of them are tangential pinches ≤ 0.02 mm² at exactly w) | 39 |
| islands of a zone fill | exact and free: KiCad stores one `filled_polygon` per island | 0.03 s |
| geodesic through the pour | no library; needs a visibility graph over ~73k hole vertices | 0.5–1 s |

Both methods agree on the opened-set topology to within features whose width is within ±1 px of the
threshold. A report should rank necks by width and label a ±1 px band as `borderline`, not rely on a
count. The vector opening is a good oracle for tests. It is 20× slower at classification and has no
geodesic.

---

## 6. Robustness notes (what an implementation must do)

1. **Rasterise by pixel-centre sampling with the even-odd rule** for zone fills. KiCad fills are
   fractured: holes are joined to the outline by zero-width bridges. Never use a boundary-inclusive
   filler (`cv2.fillPoly`, `ImageDraw.polygon`, thick `cv2.line`), because each bridge becomes a
   1-px copper line across a void (§5.1).
2. **Use union (OR) semantics for composed copper.** Pads overlap tracks and each other, and a
   single even-odd pass would cancel overlaps. The prototype ORs each pad, and skia strokes are
   unions. Skia `kWinding` over mixed-orientation pads can cancel overlaps too, so draw pads
   separately or normalise orientation.
3. **Resolution must resolve the clearance.** A void narrower than one pitch can disappear, which
   merges nets. Use pitch ≤ min(clearance)/3 for connectivity checks and ≤ w/12 for neck thresholds.
   Tag the result `degraded: raster_resolution` with the pitch.
4. **Diagonal moves through corner contacts.** 8-connected labels and 3×3 chamfers step through two
   pixels that touch only at a corner. On the three real planes, 4- and 8-connected island counts were
   identical (1/1/1). Still, forbid a diagonal step unless one straddled orthogonal cell is copper (the
   5×5 prototype does this for knight moves). Label copper 4-connected and voids 8-connected when it
   matters.
5. **Seeds.** A via is on the layer when its span covers it. Seed the via's pad disc, not only the
   centre pixel. "Island has a via" should be a vector test (via pad ∩ island polygon): at coldfire the
   pixel test flipped between 0 and 1 no-via islands with pitch.
6. **Metric bias.** Report which metric was used. Default to the 5×5 Borgefors chamfer (+0.3 %).
   State the error bound in the receipt: ±3 % far from vias, pitch-limited near them.
7. **Numerics bug found while prototyping.** A float64 heap key compared against a float32 distance
   array silently drops nodes: `d > dist[r, c]` is true after rounding, so propagation stops. Keep the
   heap key and the distance array in the same dtype.
8. **FMM** only on plane masks with features ≥ 5 px, and never on track copper (§5.4).

---

## 7. Where the tooling fits in kiwrap

- **`kiwrap.formats` stays zero-dependency.** The raster kit that uses numpy/OpenCV/numba cannot live
  there (CLAUDE.md rule, review T5). Put it in a sibling package (for example
  `kiwrap.analysis.raster`) behind an optional extra `kiwrap[analysis]` = `numpy`,
  `opencv-python-headless>=4.10,<6`, `numba`. Its inputs are the `formats` model (rings, segments,
  discs) and its outputs are plain dataclasses, so the pure-Python fallback can share the API.
- **The pure-Python fallback**: bytearray scanline (fast) plus windowed BFS (slow), capped at ~4 Mpx and
  tagged `degraded`. This keeps `kw_pcb_analyze` working when the extra is not installed.
- **Python versions.**
  - kiwrap's dev venv is CPython 3.14.4. OpenCV (abi3), numba 0.67 and numpy 2.5 have cp314 Windows
    wheels; scikit-fmm does not.
  - `requires-python >= 3.11` limits numpy to 2.4.6 and scipy to 1.17.1, the last releases with cp311
    wheels. That is fine for this stack but must be pinned if 3.11 stays supported.
- **Rust alternative.** The same kernels in the helper crate (tiny-skia + imageproc + a hand-written
  sweep) make one 0.5 MB exe with no Python dependencies. A naive port measured:
  - raster: 0.03 s;
  - labels: 1.08 s;
  - EDT: 0.90 s;
  - necks: 1.95 s;
  - 3×3 sweep: 2.69 s.

  That is 2–20× slower per step than OpenCV + numba before any tuning, but still interactive. It
  suits the "one native artefact" route in 05-kicad-native-and-rust.md. imageproc's EDT and labels
  are single-threaded. OpenCV's use 24 threads.
- **Licensing (D8).** Every recommended runtime dependency is BSD/Apache/MIT:
  - numpy BSD-3;
  - OpenCV Apache-2.0 (LGPL ffmpeg DLL bundled but unused, dynamically loaded);
  - numba/llvmlite BSD-2;
  - skia-python BSD-3;
  - tiny-skia BSD-3;
  - imageproc MIT.

  The LGPL-3 helpers (cc3d, edt) and LGPL libvips would be acceptable only as unmodified,
  dynamically loaded wheels, and they bring nothing OpenCV does not. dijkstra3d is GPL-3, so it can
  only run as a separate process, and it is not needed.
- **Install cost of the recommended extra.** About 44 MB numpy + 113 MB OpenCV + 130 MB numba/llvmlite
  ≈ **290 MB** installed. Dropping numba in favour of the Rust helper saves 130 MB. Swapping the
  numba scanline for skia-python costs 25 MB but still needs the compiled sweep.

---

## 8. Recommendation

1. **Adopt numpy + opencv-python-headless + numba** as the optional `kiwrap[analysis]` raster engine.
   Mention the new dependencies in the change that adds them (CLAUDE.md). Use:
   - `cv2.connectedComponentsWithStats` for islands and voids;
   - `cv2.distanceTransform(DIST_L2, DIST_MASK_PRECISE)` for widths;
   - EDT-threshold opening + labels for `pour_necks`, pockets and slivers;
   - numba chamfer sweeps for `gnd_reach` / geodesic lengths: 3×3 for a fast first pass, 5×5 with
     Borgefors weights for the reported number;
   - `cv2.findContours` to turn flagged regions into overlay polygons (item 39).
2. **Rasterise with an exact pixel-centre even-odd scanline.** Use the numba kernel, or the pure-Python
   bytearray one, which is already fast enough. Use skia-python only if round-capped track strokes are
   needed faster than the numba capsule kernel (0.17 s against 0.42 s on vme F.Cu). Never use
   `cv2.fillPoly` / Pillow on fills.
3. **Default pitches**: 25 µm for necks/islands/slivers (or w/12), 50 µm for reach. Cap at 150 Mpx per
   job (tile/window above), with `run_async` for anything over ~5 s.
4. **Tests only:** scikit-fmm order 2 and shapely as oracles in `tests/`, plus the synthetic
   wall-plate exact-geodesic case (§5.4) as a unit test for the metric bound.
5. **Do not adopt:**
   - GPU (cupy/torch/FastGeodis): slower end to end, 2.4–6.4 GB of installs;
   - scikit-image: its geodesic, skeleton and opening are 10–100× slower than the chosen path;
   - scipy for EDT;
   - pyvips;
   - LGPL/GPL helpers;
   - rasterio / matplotlib / Pillow for rasterising.
6. **If the owner prefers zero Python dependencies (review §8 decision 8)**, implement the same five
   kernels (scanline, labels, exact EDT (Felzenszwalb), opening, chamfer sweep) in the Rust helper
   crate with tiny-skia + imageproc or hand-written loops, and keep the pure-Python fallback. The
   numbers above show both routes are interactive. The choice is about packaging, not capability.

### Open questions for the owner

- An optional Python extra (≈ 290 MB, fastest, easiest to maintain) or the Rust helper (0.5 MB,
  zero Python dependencies, one more native build)? This is decision 8 of the review, now with numbers.
- The accuracy contract for `gnd_reach`: is ±3 % (5×5 chamfer) acceptable, or should the receipt
  compute FMM2 on a window around the worst point (≈ 1–2 s for a 20 × 20 mm window at 25 µm)?

---

## 9. Sources

Package facts (versions, dates, wheel tags, sizes) come from the PyPI JSON API
(`https://pypi.org/pypi/<name>/json`, read 2026-09-24). Licences were read from the installed wheels'
dist-info `LICENSE*` / `METADATA` in the scratch venvs, and from the crates' `Cargo.toml` in the cargo
registry.

- numpy: https://pypi.org/project/numpy/ (2.5.3; 2.4.6 is the last with cp311 Windows wheels)
- scipy: https://pypi.org/project/scipy/ (1.18.1; 1.17.1 is the last with cp311)
- OpenCV: https://pypi.org/project/opencv-python-headless/ (5.0.0.93, Apache-2.0; `LICENSE-3RD-PARTY.txt` lists the LGPL ffmpeg DLL), https://docs.opencv.org/4.x/d7/d1b/group__imgproc__misc.html (distanceTransform / connectedComponents reference; the fetch returned 403 here. The exactness of `DIST_MASK_PRECISE` was verified numerically instead: equal to scipy's exact EDT within 1.5e-5 px, §5.2)
- numba: https://pypi.org/project/numba/ (0.67.0, BSD-2), https://numba.readthedocs.io/en/stable/developer/caching.html. The cache locator order in `numba/core/caching.py` 0.67.0 is `UserProvidedCacheLocator` (`NUMBA_CACHE_DIR`), then `InTreeCacheLocator`, then `UserWideCacheLocator`, so a read-only site-packages falls back to a per-user cache. Measured cost with a cold cache: 1.0–1.3 s of compile per kernel; about 0.6 s to load once cached.
- scikit-image: https://pypi.org/project/scikit-image/ (0.26.0, BSD-3)
- scikit-fmm: https://pypi.org/project/scikit-fmm/ (2025.6.23, BSD-3; wheels cp310–313), https://github.com/scikit-fmm/scikit-fmm
- skia-python: https://pypi.org/project/skia-python/ (144.0.post2, BSD-3)
- rasterio: https://pypi.org/project/rasterio/ (1.5.1; 1.4.4 is the last with cp311)
- Pillow: https://pypi.org/project/pillow/ (12.3.0, MIT-CMU)
- shapely: https://pypi.org/project/shapely/ (2.1.2, BSD-3; GEOS LGPL-2.1 bundled as a DLL)
- pyvips / pyvips-binary: https://pypi.org/project/pyvips-binary/ (8.18.6, LGPL-3.0-or-later metadata), libvips licence https://github.com/libvips/libvips/blob/master/LICENSE (LGPL-2.1)
- cc3d: https://pypi.org/project/connected-components-3d/ (4.1.0, LGPL-3.0-or-later)
- edt: https://pypi.org/project/edt/ (3.1.2, LGPL-3.0-or-later)
- dijkstra3d: https://pypi.org/project/dijkstra3d/ (1.15.2, GPL-3.0-or-later)
- cupy: https://pypi.org/project/cupy-cuda12x/ (14.2.0, MIT; `[ctk]` pulls the CUDA 12.9 runtime wheels)
- torch: https://download.pytorch.org/whl/cu128 (2.11.0+cu128, BSD-3)
- FastGeodis: https://pypi.org/project/FastGeodis/ (1.0.5, sdist only), https://github.com/masadcv/FastGeodis (BSD-3; raster-scan / Toivanen / FMM)
- GeodisTK: https://pypi.org/project/GeodisTK/ (0.1.7, 2020, no wheels)
- tiny-skia: https://crates.io/crates/tiny-skia (0.12.0, BSD-3); imageproc: https://crates.io/crates/imageproc (0.27.0, MIT); image: https://crates.io/crates/image (0.25.10, MIT/Apache-2.0)
- Chamfer masks: G. Borgefors, "Distance transformations in digital images", CVGIP 34 (1986) 344–371. The 3×3 weights used (0.95509, 1.36930) are the commonly quoted optimum. The 5×5 weights used (0.98665, 1.40359, 2.21651) are near-optimal real weights. §5.4 measures their bias directly, so the report does not rely on the attribution.
- Exact EDT algorithm in OpenCV `DIST_MASK_PRECISE`: P. Felzenszwalb, D. Huttenlocher, "Distance Transforms of Sampled Functions", Theory of Computing 8 (2012)
- Fast marching: J. A. Sethian, "A fast marching level set method for monotonically advancing fronts", PNAS 93 (1996)

---

## 10. Reproduce

The scripts live in the session scratchpad
`C:/Users/usama/AppData/Local/Temp/claude/c--Upwork-KiCad/48a8df66-7213-479f-ab2e-a63b89135016/scratchpad/tooling/raster-geodesic/`.
Copy them to `spikes/raster-geodesic/` if they should be kept.

| Script | What it does |
|---|---|
| `extract.py` | runs under the kiwrap env: `uv run python extract.py data/`. Dumps the workloads of §3 to JSON. |
| `common.py` | grid, rasterisers (numba / skia / cv2 / Pillow / rasterio / skimage / matplotlib), RSS meter, seed snapping |
| `kern.py` | numba kernels: Dijkstra 8/16, 3×3 and 5×5 chamfer sweeps, pure-Python BFS |
| `bench_raster.py <work> <pitch>` | §5.1 fills |
| `bench_layer.py <work> <pitch>` | §5.1 whole layer |
| `bench_analysis.py <work> <pitch> islands,edt,necks,skel` | §5.2 |
| `geo_one.py <work> <pitch> <d8,d16,chamfer,chamfer16,fmm1,fmm2,mcp,csgraph,pybfs>` | §5.3 |
| `accuracy.py <pitch>` | §5.4 exact-geodesic plate |
| `pipeline.py <work> <pitches> [fmm]` | §5.5 |
| `vector_necks.py`, `neck_xcheck.py` | §5.6 |
| `gpu_bench.py`, `fastgeodis_bench.py` | GPU; run with `gpuvenv` |
| `rustbench/` | `cargo build --release`: tiny-skia + imageproc + sweep |
| `py_zero.py` | stdlib-only scanline + BFS |

Venvs: `uv venv --python 3.11 venv` with the §4 packages, and `gpuvenv` with `cupy-cuda12x[ctk]` and
torch cu128.
