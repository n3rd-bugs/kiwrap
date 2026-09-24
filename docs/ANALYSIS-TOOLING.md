# Analysis tooling for `kw_pcb_analyze`: the recommended stack

Date: 2026-09-24. Status: **accepted by the owner on 2026-09-24.** The remaining delivery details were resolved on evidence as [DECISIONS.md](DECISIONS.md) D24-D31 (full records in [decisions/D24-D31.md](decisions/D24-D31.md)); where they differ from this document, they win. Nothing described here is implemented yet, and no dependency has been added to kiwrap.

This document pulls together seven research tracks. Every number below comes from one of them. Each was measured on this machine (Windows 11, Core Ultra 9 285K, 24 threads, 127 GB RAM, RTX 5080), on copper extracted from the KiCad 10 demo boards with kiwrap's own `BoardFile`. Licences were read from the LICENSE files.

| # | Track | Question it answers |
|---|---|---|
| 01 | [Polygon engines](research/analysis/01-polygon-engines.md) | 16 vector engines (Python and Rust); parity with kicad-cli DRC |
| 02 | [Raster and geodesic](research/analysis/02-raster-geodesic.md) | Rasterisers, EDT, necks, geodesic reach, GPU, pixel budget |
| 03 | [Graphs and paths](research/analysis/03-graphs-and-paths.md) | Copper connectivity graph, path queries, KiCad `CONNECTIVITY_DATA` |
| 04 | [EM, impedance, current](research/analysis/04-em-impedance-current.md) | Z0 / Zdiff / coupling / current capacity; closed forms against a 2-D FEM |
| 05 | [KiCad-native and Rust](research/analysis/05-kicad-native-and-rust.md) | What KiCad computes, and what a fork job or a pyo3 wheel would cost |
| 06 | [Client tool limits and elicitation](research/analysis/06-client-tool-limits-and-elicitation.md) | Tool-count caps in Claude Code; MCP elicitation for D21 |
| 07 | [Bake-off](research/analysis/07-bakeoff.md) | The top candidates head to head on one harness; one winner per capability |

Where the tracks disagree, the bake-off (07) decides. It ran the leading candidates from 01-05 in one process model with one meter, and its timings include marshalling the data into each engine. §8 lists the places where it changed an earlier track's conclusion.

---

## 1. The question

The owner asked (2026-09-24, answering review decision 8): *"open this requirement, research and find the best tooling, not just numpy"*. Items 8-9, 16, 18 and 21-39 of [requests/kiwrap-feature-requests.md](../requests/kiwrap-feature-requests.md) need real computational geometry:
- polygon offset and booleans with holes, and stroke inflation;
- clearance with closest points;
- zone-fill islands;
- geodesic distance through copper;
- path and loop areas;
- raster analyses (necks, reach);
- copper connectivity graphs;
- impedance, coupling and current capacity.

The review ([requests/kiwrap-feature-requests.review.md](../requests/kiwrap-feature-requests.review.md) §6 T5) proposed doing all of it in zero-dependency pure Python in `kiwrap.formats.geometry`.

The same answers left one more question open: does any client cap the number of tools, and should kiwrap wrap its tools if so? D21's consent prompt raised another: does MCP elicitation reach a human in the product surface? Both are answered here (§5, §6).

## 2. Answer in one paragraph

Ship **one MIT native component, `kiwrap-geom`**: a pyo3 abi3 wheel of about 0.5-1 MB that serves every CPython from 3.11 up. It is built from pure-Rust crates: `clipper2-rust` (BSL-1.0), `i_overlay`, `rstar` and `rayon` (MIT/Apache).
- Keep the **zero-dependency pure-Python kit** in `kiwrap.formats` as the reference implementation and the fallback. It is measured interactive for clearance, islands and graph queries.
- Put the only heavy Python dependency, **scikit-fem + scipy + numpy, in an optional `kiwrap[fields]` extra** for impedance cases that closed forms get wrong.
- Use **KiCad as the oracle, never as the engine.**
- **shapely, numpy, OpenCV, numba and every graph library stay out of the runtime.** No measured capability needs them once the native wheel exists: shapely won no capability in the bake-off.
- **Do not wrap the tool catalog.** Claude Code sets no tool cap and defers MCP tools by default.
- **Do not make elicitation the D21 consent gate.** The VS Code panel auto-declines elicitation. Use a dedicated upgrade tool with `_meta["anthropic/requiresUserInteraction"]` instead.

## 3. Recommended stack per layer

### 3.1 Summary

| Layer | Choice | Licence | Where it lives | Deciding measurement (source) |
|---|---|---|---|---|
| Vector: offset, inflate, unfracture, union | **clipper2-rust 1.2** in `kiwrap-geom` | BSL-1.0 (object-code exemption) | native wheel | Union of one layer: 90-323 ms. Inflate: 306-1,224 ms. The only engine that reproduced KiCad's own inflated shape and hole counts (video F.Cu 1,256/2; In2 1,536/1,109). Merges vertex-touching holes as KiCad's `SHAPE_POLY_SET` does (07 §3.1) |
| Vector: bulk stroke + union (later, optional) | **i_overlay 9.0** behind the same facade | MIT OR Apache-2.0 | native wheel | 46-118 ms per layer against shapely's 553-1,513 ms, with the same union topology. It drops zero-length strokes and differs at exact touches, so it must be fed discs and cross-checked in tests (05 §4.4, 07 §3.1) |
| Vector: proximity (clearance, closest points, kNN, containment) | **rstar 0.13 edge index + exact capsule distance, rayon** | MIT OR Apache-2.0 | native wheel | 10 / 42 ms (video / jetson F.Cu), against shapely 174 / 1,169 ms and pure Python 147 / 1,510 ms. **782/782 and 1,648/1,648 pairs equal kicad-cli DRC**, max \|Δ\| 0.00005 mm (07 §3.2) |
| Vector: reference and fallback | **pure-Python kit** in `kiwrap.formats.geometry`: unfracture, capsule distance with closest points, grid index, banded point-in-polygon | MIT (ours) | `kiwrap.formats` (zero-dependency) | Clearance ≤ 1.5 s per layer on the largest board. Islands 0.45-0.50 s. Same answers as rstar (07). No union or offset: do not write those in Python (01 §8) |
| Raster: scanline, chamfer geodesic, grid Dijkstra, EDT, necks | **5×5 Borgefors chamfer + implicit-grid Dijkstra in `kiwrap-geom`**; exact pixel-centre even-odd scanline | MIT (ours) | native wheel; pure scanline in `formats` | Chamfer: 0.28-1.18 s up to 29 Mpx, mean error +0.08…+0.26 % against order-2 FMM (07 §3.4). Scanline: pure Python 0.24 s at 60.5 Mpx, exact (02 §5.1). Rust EDT 0.90 s and necks 1.95 s at 60.5 Mpx, untuned (02 §7) |
| Raster: fallback | pure-Python scanline + windowed Dijkstra/BFS, ≤ 4 Mpx, tagged `degraded` | MIT | `kiwrap.formats` | BFS/Dijkstra takes 33 s at 7-12 Mpx (07), 18.7 s at 3.4 M cells (03), and ~3 s at 3.8 Mpx (02). Only small windows are usable |
| Graph (items 28-30, 35, 36) | **pure Python** (heapq + union-find), `kiwrap.formats.analysis.copper_graph` | MIT | `kiwrap.formats` | Every query ≤ 62 ms on vme-wren (48k nodes). networkx is slowest everywhere; rustworkx and scipy gain nothing at this size (03 §1). The cost is contact detection: 1.2 s on jetson GND with Rust contacts, 10.7 s pure (07 §3.5) |
| EM tier 0: Z0, Zdiff, crosstalk, current | **closed forms re-derived from the papers** (HJ80, KJ82, KJ84, Wheeler78/Cohn, Ghione-Naldi, IPC-2221, Brooks-Adam 2015) | MIT (ours) | `kiwrap.formats.physics` | 2-9 µs per evaluation. Within 0.64 % of FEM P2 for microstrip and edge-coupled pairs. GCPW is +7.4 %, so always tag it `estimate` (07 §3.6, 04 §5.2) |
| EM tier 1: 2-D quasi-static FEM | **scikit-fem 12.0.2 P1 + scipy + numpy** | BSD-3-Clause | optional extra `kiwrap[fields]`, in `kiwrap.analysis.fields` | 0.46-1.6 s per cross-section, within 0.15 % of P2. Reproduces JLC's calculator: 50.07 / 49.86 / 102.9 Ω. All 168 jetson sections in 17.6 s on 16 processes (04, 07) |
| KiCad-native | **oracle only**: T3 DRC probe on a copy (tiled past the 499 cap), the file's `(island yes)` flag, IPC `GetConnectedItems`, and SWIG `ConvertBrdLayerToPolygonalContours` on 10.x for conformance | GPL, separate process only (D8) | `backends/cli`, `conformance/` | A DRC run takes 3.5-222 s (05 §3.3). A fork `pcb analyze` job would be ~4-6k lines and would pay the board load on every call (05 §3.4) |
| Rust / pyo3 or not | **yes: one pyo3 0.29 abi3-py311 wheel built with maturin 1.15**, not a helper exe | MIT OR Apache-2.0 | `helper/kiwrap-geom` (second crate in the Cargo workspace) | 443 KB wheel, built first try in 46 s. In-process buffer copy 1-3 ms, against 10-20 ms per 3-4 MB request plus 11-21 ms spawn for an exe (05 §4.2, §4.5). A persistent index across calls needs in-process state |

### 3.2 Vector geometry

**Representation decides correctness more than the choice of engine** (01 §8, 07 §2). Every engine matched KiCad once the input was modelled as follows:
- Tracks, arcs and vias are centre line plus radius. Arcs are cut at ≤ 0.05 µm chord error for distance work.
- Pads are kiwrap's `Pad.polygons()` rings, plus exact "fat primitives" for distance.
- Vias are flashed per layer following `remove_unused_layers`, `keep_end_layers` and `zone_layer_connections`.
- Fills are unfractured polygons with holes.
- Clearance is compared against `C − 0.0005 mm`, KiCad's `m_DRCEpsilon`.

Polygon-only engines, which polygonise strokes, miss violations that sit within the polygonisation error of the threshold:
- shapely with polygonised strokes: 389/402 on jetson B.Cu;
- pyclipr: 366/402;
- KLayout: 347/402 (01 §4.3).

**Never decide clearance by inflating and testing overlap.** At an offset of exactly half a gap, the engines disagree on which touches merge (05 §4.4, 07 §5). Clearance is always a distance with a tolerance. Inflation is only for rendering and area metrics.

**Why clipper2-rust first and i_overlay second.** Track 05 recommends starting with one engine, and the bake-off supports that:
- clipper2-rust has KiCad's topology at exact touches and handles zero-length strokes.
- i_overlay is 2-4× faster on stroke and union, but:
  - it drops zero-length strokes;
  - its major version churns (9.0 was 5 days old when measured, and `geo` 0.33 still pins 4.5);
  - it has a single main author.

Add i_overlay only where bulk union becomes a measured bottleneck.

clipper2-rust has its own risk: it is a young, AI-assisted port with one maintainer. Two mitigations: it matched KiCad's C++ Clipper2 topology on the measured layers, and the `clipper2` crate, which wraps the C++ original, is a drop-in fallback that needs a C++ build step (05 §6).

**Rejected, with the measured reason:**

| Candidate | Reason |
|---|---|
| shapely 2.1.2 | Wins no capability. It is 3-16× slower on union and 17-28× on clearance than Rust. It is also the only copyleft piece in the Python route: GEOS is LGPL-2.1, shipped as separate DLLs. It stays as a test oracle (07 §1) |
| Clipper 6.4.2 (pyclipper, gdstk) | Pathological on fractured planes: 115-384 s per union, and 20-minute timeouts (01 §1) |
| KLayout | GPL-3.0-or-later. Also slower than an edge index on PCB data: 22-27 s on plane layers (01 §1) |
| manifold3d | Its 2-D API is being rewritten upstream (01 §1) |
| skia-pathops | Superlinear (01 §1) |
| CGAL Python bindings | No 2-D boolean module bound; GPL/commercial (01 §1) |
| pyclipper2 | No Windows wheel (01 §1) |
| pygeoops | Pulls in geopandas (01 §1) |

### 3.3 Raster and distance fields

Three correctness traps matter more than the choice of library (02 §1, §6):

1. **Rasterise fills with an exact pixel-centre even-odd scanline.** OpenCV `fillPoly` and Pillow draw the zero-width fracture bridges of KiCad's fills. They add 0.2-8.5 % copper and create false voids (3,020 against the true 2,998 on vme-wren).
2. **Use the 5×5 Borgefors chamfer, not 8-neighbour Dijkstra or a 3×3 chamfer.** The latter carry a +4.8…+5.8 % bias, up to 11.6 % on the exact plate (02 §5.4, 07 §3.4).
3. **Use fast marching (scikit-fmm) only as an oracle.** It is exact on wide copper, but it fails through 1-2 px tracks (+41 % on a 2-px diagonal) and is 8-33× slower.

Pixel budget (02 §1):
- Neck counts converge at 25 µm (≥ 12 px across the threshold width).
- Reach converges by 50-100 µm.
- Cap each job at 150 Mpx, window by bbox, and use `run_async` above about 5 s.

**The kernels move into `kiwrap-geom`; numpy, OpenCV and numba stay out.** Track 02 recommended a ~290 MB `numpy + opencv-python-headless + numba` extra. The bake-off then found that track 02's "5×5 chamfer costs 13 s" was a numba artefact (an `inline="always"` helper). Hand-inlined, the kernel runs within 10-16 % of Rust, with bit-identical output (07 §1).

With the chamfer in Rust, the remaining OpenCV advantage is EDT and neck speed:

| Step, at 60.5 Mpx | OpenCV | Untuned Rust (single-threaded imageproc) |
|---|---|---|
| EDT | 0.08 s | 0.90 s |
| Necks | 0.68 s | 1.95 s |

Both are interactive (02 §7). Zone-fill islands are exact as vector data. The raster was 4-7× slower there, needed 313-564 MB and found 4 false holes (07 §3.3).

**GPU: rejected** (02 §1). cupy's EDT and labels save 20-70 ms, and the host-device copies cancel that. GPU geodesics took 2.2-3.0 s against 0.54-0.95 s on the CPU. torch with CUDA is 6.4 GB installed.

### 3.4 Graphs

- **No graph library is needed** (03 §1).
- **The real work is contact detection**, following the model in 03 §3:
  - split points on track centre lines;
  - pads on a track *interior* connect;
  - zone islands get Delaunay visibility edges, plus flagged `zone_est` edges across holes;
  - net-less copper graphics conduct.
- **Contact detection should use the native kit.** The Rust kit builds the jetson GND graph in 1.2 s, against 10.7 s in pure Python (07 §3.5).
- **The same builder should replace `ratsnest_estimate`'s grouping**, which fixes a bug found by track 03 (§9).
- **Excluded libraries:**
  - igraph is GPL-2.0, so it cannot be imported under D8.
  - graph-tool has no Windows build.
  - networkx 3.7 and scipy 1.18 already require Python ≥ 3.12.

### 3.5 EM, impedance and current

The model picks the tier per cross-section (04 §8.2):
- **Tier 0 (stdlib)** when the section is in range.
- **Tier 1 FEM** when the extra is installed and the section is:
  - GCPW;
  - offset stripline outside h1/h2 ∈ [0.67, 1.5];
  - a tight gap (S < 0.75 h);
  - or `fab_comparable=true` (the fab's etch, plating and mask).
- **Otherwise, tier 0 tagged `estimate`.**

Current capacity:
- IPC-2221 gives KiCad parity.
- The Brooks-Adam 2015 IPC-2152 fit is tagged `estimate`. It is an upper bound: planes and board thickness cool traces.
- ρ(T) gives the mΩ figure (04 §1).

**Do not port KiCad's transline code.** It is GPL (review T9), and it is also inaccurate:
- microstrip is +0.9…+9 % high, because copper thickness is counted twice;
- coupled microstrip Zdiff is +6.6…+11.3 % high;
- offset coupled stripline is asymmetric (04 §6).

KiCad 10's tuning profiles use this code, so kiwrap must not treat a profile's width as ground truth.

**Oracles only, never shipped** (04 §8.3):
- openEMS (GPL, a separate process, 4-5 min per line);
- the KiCad transline harness;
- scikit-rf;
- pcb-toolkit.

### 3.6 KiCad-native jobs

- **Stock KiCad computes most of what the items need but exports almost none of it** (05 §1).
  - The DRC providers compute the actual gap and the two closest points (since `814f65934f`, on the 10.0 branch). `RC_ITEM::GetJsonViolation` throws them away. That is item 18's bug, inside KiCad.
  - SWIG is the only stock surface for geometry operations, and it is removed in KiCad 11.
  - IPC 11 exposes data, not computation.
- **Use KiCad as the oracle** for:
  - rule-resolved `required_mm` / `actual_mm` and keepout verdicts: the T3 probe on a copy, as a batch;
  - island flags: the file's `(island yes)`;
  - connectivity: IPC `GetConnectedItems`, 0.31 s per call after a 29 s open (03 §5.4);
  - union topology: SWIG on 10.x, in `conformance/`.
- **A single kicad-cli DRC report truncates at 499 violations per type.** Any oracle run must tile the board or detect the cap. `run_drc` already reports `capped` (07 §5).
- **Do not build the general fork `pcb analyze` job now.** It is ~4-6k lines plus rebases, stock users never get it (D19), and every call pays the board load: 12.5 s for vme-wren in SWIG `LoadBoard`, which hung on jetson (05 §3.4).
  - Draft only the 40-80-line DRC JSON `marker{pos, layer, path_start, path_end}` patch, with capability key `cli.drc_json_marker`.
  - Revisit the general job once KiCad 11's headless api-server keeps a board resident.

### 3.7 Rust / pyo3: yes, as one wheel

- **Toolchain.** The toolchain `helper/kiwrap-live` already needs (Rust stable-msvc and the MSVC build tools) is enough. All the crates are pure Rust, with no C/C++ (05 §4.5).
- **One artefact.** An `abi3-py311` wheel loads on every GIL-enabled CPython ≥ 3.11. That covers kiwrap's 3.11 floor and the 3.14.4 dev venv, so the numpy/scipy cp311 drift does not apply to it (02 §7, 05 §4.5).
- **In-process, not a helper exe.**
  - Chatty queries need a persistent index: `probe_via` at N points, `measure(a, b)`, nearest via. A `#[pyclass]` can hold an rstar tree between calls.
  - An exe would add 10-20 ms per request plus 11-21 ms per spawn, and would need a session protocol (05 §4.5).
- **Packaging risks.**
  - **Unwind profile.** The workspace's `[profile.release]` in `helper/Cargo.toml` sets `panic = "abort"`. Track 05 says to keep `panic = "unwind"` in this crate, so that a Rust panic becomes a Python exception instead of killing the MCP server. Cargo profiles are set at workspace level, so kiwrap-geom needs its own profile, for example `[profile.pyext] inherits = "release", panic = "unwind"`, built with `maturin build --profile pyext`.
  - **Pinning.** Pin the crates in `helper/Cargo.lock`, and wrap i_overlay in one module because of its API churn (05 §6).
  - **Licence notices.** Ship a `THIRD-PARTY` file generated with `cargo about` or `cargo deny`. BSL-1.0 exempts object code, but include the notice anyway (05 §4.6).

## 4. What changes in kiwrap's architecture

1. **`kiwrap.formats` stays zero-dependency.** It gains the pure layer:
   - `formats/geometry/`: unfracture by bridge-edge cancellation, segment/capsule/fat-primitive distance with closest points, the grid index (generalising `ratsnest._FillIndex`), banded point-in-polygon, Sutherland-Hodgman, the exact pixel-centre scanline, and a windowed Dijkstra/BFS for ≤ 4 Mpx;
   - `formats/analysis/copper_graph.py`;
   - `formats/physics/`: the tier-0 closed forms.

   Docstrings cite the research docs, and the facts become VERIFICATION claims when promoted. Examples: 143/143 and 400/402 against kicad-cli 10.0.6 DRC (01), and 782/782 and 1,648/1,648 (07).
2. **A new package, `kiwrap.analysis`.** It is the only place in kiwrap allowed to import optional third-party compute packages, and it imports them lazily. It imports only `kiwrap.formats`. It holds:
   - the `GeometryEngine` facade, the protocol from 01 §8: `stroke`, `union`, `difference`, `offset`, `unfracture`, `pairs_within(C)`, `nearest`, `islands`, plus the raster kernels;
   - backend selection: `native` (`kiwrap_geom`) first, else `pure` (`kiwrap.formats`), else a teaching error for union/offset (see §9 decision 2);
   - `analysis/fields.py` for the scikit-fem tier.

   `kw_pcb_analyze` in `kiwrap.core` calls `kiwrap.analysis` and never imports a numeric package itself.
3. **Import-linter contracts** (in `pyproject.toml`):
   - Add `kiwrap.analysis` to the layers contract directly above `kiwrap.formats`.
   - Add `kiwrap.analysis` to the forbidden list of the "formats has zero dependencies" contract.
   - A new forbidden contract: `kiwrap.analysis` must not import `kiwrap.core`, `kiwrap.backends`, `kiwrap.sandbox`, `kiwrap.installation`, `kiwrap.capability` or `kiwrap.platform`.
   - A new forbidden contract with `include_external_packages = true`: every kiwrap package except `kiwrap.analysis` must not import `kiwrap_geom`, `shapely`, `numpy`, `scipy` or `skfem`. This keeps the relaxed rule in one package.
4. **`pyproject.toml` optional extras.** The runtime `dependencies` do not change.
   - `native = ["kiwrap-geom==<pinned>"]`: the wheel, from the wheelhouse.
   - `fields = ["scikit-fem>=12,<13", numpy and scipy with markers]`. On Python < 3.12 the markers pin `numpy<2.5` and `scipy<1.18`, the last releases with cp311 wheels (04 §1).
   - A separate dependency group `oracle` for tests: shapely, scikit-fmm, scikit-fem. scikit-fmm has no cp314 wheel, so that group needs a marker or a ≤ 3.13 venv (02 §7).
5. **Wheel shipping**, following the `kicad-python` wheelhouse precedent (05 §4.5):
   - CI builds `kiwrap_geom-<v>-cp311-abi3-win_amd64.whl` once. User machines never build from source, so Rust is not required there.
   - kiwrap runs as `uv run --project <checkout>` (EXTENSION.md §2). The wheel therefore has to reach that checkout, either by `bootstrap.ps1` from a pinned, hashed entry in `scripts/engines.json` (D11 pattern) or carried in the VSIX next to `kiwrap-live.exe`.
   - The exact uv mechanism (a `find-links` directory or a path source) has to be verified in the change that adds it; no track tested it.
6. **Capability key `native.geom`.** It is an environment capability: `kiwrap_geom` imports and its version matches. It is not a KiCad-version fact, so it does not belong in `data/kicad_version_matrix.json`.
   - `kw_env` reports it.
   - Each analysis receipt carries `engine: native|pure` and `degraded` tags: T5's `fills_stale`, `stackup_defaulted`, `raster_resolution`, `estimate`, plus `raster_fallback`, `reference_inferred` and the geodesic `metric` (02 §6, 04 §8.4).
   - Where this key lives is a contract question for `capability.py`, reported in §9.
7. **The per-process board-model cache (review T8) matters most for latency.** Loading the board takes longer than any analysis:
   - `BoardFile.load` takes 11.4-19.6 s on vme-wren and jetson (05 §2, 07 §2);
   - the analyses take 10 ms to 1.5 s per layer.

   Key the native index on the same (path, mtime, size) as the cache.
8. **Data prerequisites** (contract changes, found by the tracks):
   - `formats/board.py` `StackupLayer` must sum `addsublayer` dielectrics. Today jetson In2 comes out at 39.4 Ω instead of 51.6 Ω (04 §8.4).
   - Read `.kicad_pro` `tuning_profiles` for the declared reference layers and target impedance.
   - Expose footprint copper graphics and text (item 9). The file model has 4,997 F.Cu shapes on vme-wren, where KiCad has 5,021 (05 §2).
   - Zone-to-zone clearance: KiCad measures it in its own zone test, up to 0.037 mm off the fill-to-fill gap. Report fill gaps labelled as such until this is modelled (07 §3.2).
9. **Conformance oracles** (07 §6):
   - tiled kicad-cli DRC for clearance parity;
   - SWIG `ConvertBrdLayerToPolygonalContours` + `Simplify` on 10.x for union topology (shape counts equal, area within 0.1 %);
   - scikit-fmm and the exact plate for the geodesic metric bound;
   - scikit-fem P2 tables for Z0;
   - a timing test for every compiled kernel. numba showed that a harmless-looking rewrite can cost 14-16× (07 §5).
10. **Catalog:** `kw_pcb_analyze` (review T6), plus a dedicated `kw_project_upgrade` (§6). The ~36 figure in ARCHITECTURE §10 becomes a guideline (§5).

## 5. Tool count: is there a limit, and should kiwrap wrap its tools?

**No limit in the product client** (06 §1, measured on Claude Code 2.1.280, the build bundled in the VS Code extension):
- The docs say "Claude Code doesn't impose a fixed per-server tool cap; the practical limit is your context window budget". No cap appears in the binary.
- A 150-tool stdio server delivered all 150 tools in one `tools/list`.
- MCP tools are **deferred by default**. Only names and server instructions sit in context, and `ToolSearch` loads schemas on demand. The API allows 10,000 deferred tools.
- This session held about 237 MCP tools from 11 servers. `ToolSearch` still ranked the wanted `kw_*` tool 1st or 2nd in 3 probes.
- The MCP spec has no count limit.
- The known caps (VS Code Copilot 128, Cursor 40) belong to clients that D18 puts out of scope.

**What does cost something:**
- **Context, when tool search is off.** kiwrap's 33 tools are 65,064 model-visible chars, about 26k tokens per turn. Tool search is forced off by a non-first-party `ANTHROPIC_BASE_URL`, `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS`, Foundry on Azure, and pre-4.5 Agent Platform models.
- **Description length.** Descriptions and server instructions are cut silently at 2,048 chars.
- **Too many tools loaded at once.** Vendor evidence says selection degrades past 30-50 tools loaded together. No local accuracy eval was run (no credentials).

**Recommendation: keep coherent tools; do not wrap.**
- A `kw_call(tool, args)` dispatcher would lose:
  - API-side schema validation ("incorrect parameters" is the vendor's top failure mode);
  - tool-search indexing;
  - per-tool permission rules, which D23 relies on;
  - per-tool `_meta` and annotations;
  - hook matchers.
- Keep a `kw_<stage>_<verb>` catalog of about 30-45 tools, and treat 36 as a guideline.
- Use the review's T6 folding rule (same argument shape → fold into an existing tool) as the test for adding a tool, not a number.
- Worth doing now:
  - keep each description ≤ ~1,500 chars, with the domain nouns (KiCad, schematic, board, PCB) first;
  - strip pydantic `title` keys and collapse nullable `anyOf` (−15.4 % of schema chars);
  - do not set `alwaysLoad`.
- If a capped client ever returns, add a `serve --profile compact` subset, never a dispatcher.

## 6. MCP elicitation for D21's upgrade consent

**Findings** (06 §2):
- The Claude Code CLI shows form elicitation (since 2.1.76).
- **The VS Code chat panel does not show it (2.1.280).**
  - `extension.js` registers no `onElicitation`, so the SDK answers every elicitation with `{"action":"decline"}`.
  - This comes from static analysis; it could not be run end to end without credentials.
- **The server cannot detect the panel.** The client still declares `elicitation: {}`. The only observable difference is the undocumented env var `CLAUDE_CODE_ENTRYPOINT=claude-vscode`.
- **In `mcp` 2.2.0, `ctx.elicit()` raises `NoBackChannelError` on 2026-07-28 connections.** The `Resolve` + `Elicit` resolver API works on both protocol eras.

**Answer: elicitation cannot be the D21 gate in the product surface.** Use four layers (06 §2.5):
1. **Refuse and teach.** Any write that would change the file-format epoch refuses with `E_FORMAT_UPGRADE_REQUIRED`. The error names the project's epoch and KiCad version, the target, the files affected, and both ways forward: install the matching KiCad, or upgrade.
2. **A dedicated `kw_project_upgrade(project, from_kicad, to_kicad, files[])`** with `_meta["anthropic/requiresUserInteraction"]: true`.
   - Claude Code prompts the human on every call, in every permission mode, with no "don't ask again".
   - The server re-derives the arguments and refuses on any mismatch, so the prompt shows the truth.
   - Retire `kw_project(action='upgrade', confirm=true)` as the agent's route: `confirm=true` is the model attesting consent to itself.
   - `_meta` is per tool, so the upgrade needs its own tool.
   - The key is in the 2.1.280 binary. The panel prompt itself is not run-verified.
3. **Elicitation as an enhancement only.**
   - Use the resolver API, not `ctx.elicit()`.
   - Ask only when the client declares the capability and `CLAUDE_CODE_ENTRYPOINT != claude-vscode`.
   - `decline`, `cancel` or an error mean "no answer" and fall back to layer 1, never a stored refusal.
4. **The extension's modal** (`extension/src/consent.ts`, in progress) gates only the extension's own server's calls. The agent's server is a separate process (EXTENSION.md §2-3).

Phase 0 should add an end-to-end test with a real login:
- the panel shows the `kw_project_upgrade` permission prompt with from/to;
- on deny, the files stay byte-identical;
- on approve, the files are upgraded and a snapshot exists;
- an elicitation sent under the panel still comes back `decline`.

The D21 text in DECISIONS.md ("MCP elicitation where the client supports it … then `kw_project(action='upgrade', confirm=true)`") should be amended accordingly (§9 decision 7).

## 7. Request items 8-39 on the stack

Legend:
- **N** = `kiwrap-geom` (native).
- **P** = pure `kiwrap.formats` kit.
- **G** = pure copper graph.
- **R** = raster kernels (native, or pure ≤ 4 Mpx).
- **E0 / E1** = physics tier 0 / tier 1.
- **K** = KiCad oracle (T3 DRC probe on a copy, file flags, IPC).
- **D** = data or DRC plumbing only; no analysis stack.

"Fallback without N" says what still works when the native wheel is missing.

| Item | What it needs | Stack | Fallback without N |
|---|---|---|---|
| 8 zones with holes, islands, area, vias per island | unfracture, area, containment; island = one `filled_polygon`; `(island yes)` | N (clipper2 unfracture, KiCad hole convention; rstar containment), K (flag) | P: unfracture 0.45-0.50 s per layer (07), up to 2.4-4.3 s on the largest planes (01) |
| 9 graphics with stroke, footprint copper | stroke inflation (effective outline); distance work uses centre line + radius | N (stroke/offset); P for distance. **Data first:** footprint copper graphics are missing from the file model (05 §2) | Distance: yes (P). Inflated outline: no (§9 decision 2) |
| 10 outline, 11 stackup, 12 pads, 13 footprints, 14 nets, 15 rules | data parity (review T4) | D. For 11 the `addsublayer` fix feeds 34 | n/a |
| 16 k nearest vias | kNN | N (rstar `nearest_neighbor_iter`) | P grid index |
| 17 schematic pin context | netlist traversal | D | n/a |
| 18 violation location, closest points, actual/required | recompute the pair's distance and closest points; rule-resolved required value | N (edge index), K (T3 probe for `required_mm`), fork draft `cli.drc_json_marker` | P (same algorithm; same answers as N, 07) |
| 19 all track errors, 20 bucket per violation, 23 ignored checks | DRC plumbing | D | n/a |
| 21 by-design hints (netless copper bridging nets) | per-net union + netless copper overlaps; the graph's `graphic` edges | N, G | P contact detection (slower) |
| 22 padstack "no paste" cross-check | pad ∩ plotted paste (incl. footprint zones on F.Paste) | N boolean; data: footprint zones | P Sutherland-Hodgman for convex pads only |
| 24 fab-capability audit | the same clearance engine at fab values; rule-resolved checks | N, K (T3 probe with generated rules, tiled past the 499 cap) | P for clearances |
| 25 return_path | segment-over-void clipping, plane detection from fills, nearest return via; displaced-current fraction | N (clip, kNN), E0 (`return_current_fraction`), stackup + `tuning_profiles` data | P (segment clipping, grid index) |
| 26 plane_slots | union of inflated antipads, traces crossing them | N (offset + union, clip) | **no** vector union (§9 decision 2); raster R can label voids |
| 27 stitching / edge_stitching | via positions along plane and board edges; legal fill-ins = 32 | N (kNN, distance along boundary), 32 | P |
| 27 gnd_reach | geodesic through the pour to the nearest via | R: 5×5 Borgefors chamfer, 0.28-1.18 s up to 29 Mpx; pitch 50 µm | pure Dijkstra ≤ 4 Mpx windows, `degraded`, 3×3 metric bias |
| 27 pour_necks, islands, slivers | EDT-threshold opening + labels; islands exact from vector | R (EDT, labels, opening), N/P for islands | pure raster ≤ 4 Mpx |
| 28 copper_path / net_topology | contact graph + Dijkstra with predecessors; neck width, layer sequence, via count, series parts | G, contacts from N | G with P contacts: 10.7 s on jetson GND, so `run_async` |
| 29 loop_area | k point-to-point paths + shoelace area | G | same as 28 |
| 30 nearest_via / pad_return / exposed_pad | one multi-source Dijkstra from all vias; other-net copper under an EP | G, N (region query) | G + P |
| 31 measure / clearance_pairs / region / antenna / keepout | edge index + exact distances; region queries; keepout verdicts | N, K (`disallow graphic`, `physical_clearance` on a copy) | P (clearance ≤ 1.5 s per layer) |
| 32 probe_via / via_spots | disc-vs-copper at many points; a persistent index | N (rstar, µs per point, `#[pyclass]` index) | P grid index (slower per point) |
| 33 coupling | parallel run + min gap per layer; NEXT/FEXT | N (edge index), E0 (k/2, modal FEXT), E1 (Kb/Kf from C and L) | P + E0 |
| 34 diffpair / rf_line | Zdiff / Z0 per segment, measured coplanar gap, skew, electrical length | E0 (microstrip, coupled microstrip), E1 (GCPW, offset stripline, `fab_comparable`), N for gaps; data: `addsublayer`, `tuning_profiles` | E0 with `estimate` when E1 is absent |
| 35 current_capacity | widest-path (bottleneck) on the graph, then mΩ and ΔT | G, E0 (IPC-2221 + Brooks-Adam `estimate` + ρ(T)) | G + P |
| 36 interfaces / esd_entry | multi-source Dijkstra from 2-pin-part pads; neighbour nets | G, N (neighbours) | G + P |
| 37 assembly_outputs, field_parity | BOM/CPL simulation, field diff | D | n/a |
| 37 paste_coverage, mask_web | aperture ∩ pad area; web width, gang openings | N (boolean, offset; distance between openings) | P: widths by distance; areas only for convex shapes |
| 38 render crop, layers | crop the SVG `viewBox` / raster | D (05 §5: no geometry needed) | n/a |
| 39 overlays | check results emitted as vector geometry (points, segments, polygons) | output of the checks above. Raster-derived regions need a contour tracer: track 02 used `cv2.findContours`; the native equivalent is **not yet measured** | pure contour tracing (not measured) |

## 8. Where tracks disagreed, and what decided

| Topic | Tracks | Decision and evidence |
|---|---|---|
| Primary vector engine | 01: shapely in core, Rust as accelerator. 05: Rust as the engine | **Rust.** On one harness shapely wins no capability. Pure Python is as fast as shapely for clearance: 147 / 1,510 ms against 174 / 1,169 ms (07 §3.2) |
| Raster runtime | 02: numpy + OpenCV + numba extra (~290 MB). 03: Rust implicit grid | **Rust in the same wheel.** The 5×5 "13 s" was a numba inline-helper artefact. The Rust chamfer takes 0.28-1.18 s (07 §1) |
| Geodesic metric | 02: 3×3 first pass, 5×5 for the reported number | **5×5 Borgefors as the default.** At 0.28-1.18 s it is fast enough to be the only pass. 3×3 carries a +4.8…+5.1 % bias (07 §3.4) |
| Geodesic pitch | 02: 50 µm for reach. 03: 0.1 mm with a 4M-cell budget | **50 µm, capped at 150 Mpx**, with the native chamfer. The ≤ 4M-cell budget applies to the pure fallback only |
| Native form | 03: exe or pyo3, open. 05: pyo3 | **pyo3 abi3 wheel**, one artefact for geometry and raster (05 §4.5) |
| Union engine inside the wheel | 07: i_overlay wins union speed. 05: start with clipper2-rust | **clipper2-rust first** for KiCad parity and zero-length strokes; i_overlay later behind the facade |

## 9. Decisions for the owner

1. **Adopt `helper/kiwrap-geom`** as the primary analysis engine: a pyo3 abi3 wheel, MIT, pure-Rust crates, capability `native.geom`. *Recommended.*
   - Cost: ~2-3k lines of Rust plus bindings and a CI wheel job, 1.5-2 weeks for the primitives (05 §7).
   - Choose the delivery route to the checkout: `bootstrap.ps1` + `scripts/engines.json`, or the VSIX.
2. **Union and offset without the native wheel.**
   - (a) Refuse with a teaching error ("install the kiwrap-geom wheel"). *Recommended.* On the one supported platform (D18) the wheel always ships.
   - (b) Add shapely as an optional extra (`kiwrap[analysis]`), about 50 MB with LGPL GEOS DLLs.

   Track 01 recommended shapely as a core dependency. The bake-off superseded that.
3. **Approve `kiwrap[fields]`** (scikit-fem 12 + scipy + numpy, BSD-3, about 120-160 MB installed) as an optional extra for GCPW, offset stripline, tight gaps and `fab_comparable`.
   - Either pin `numpy<2.5, scipy<1.18` on Python 3.11 (*recommended*: it keeps the 3.11 floor), or make the extra 3.12+.
   - A later Rust port with `faer` could remove numpy. Its prototype matches the FEM, but its speed with faer is unmeasured (04 §7).
4. **Accuracy and pitch contract for `gnd_reach`.**
   - Accept the 5×5 chamfer at 50 µm: mean +0.08…+0.26 %, p99 ≤ 1.8 %, plate max 4.7 % next to a wall corner. *Recommended.*
   - Or add an FMM window around the worst point, at about 1-2 s (02, open question).
5. **Replace review decision 8** ("stay pure-Python zero-dep, or numpy later") with decisions 1-3. `kiwrap.formats` stays zero-dependency. The relaxation applies only to `kiwrap.analysis` and is enforced by import-linter (§4.3).
6. **Catalog.**
   - Accept that the catalog grows past 36 when a capability needs its own schema: `kw_pcb_analyze` and `kw_project_upgrade` now. *Recommended.*
   - Amend ARCHITECTURE §10's "36" to a guideline of about 30-45.
7. **Amend D21.**
   - The agent's consent gate becomes `kw_project_upgrade` with `requiresUserInteraction`.
   - Elicitation becomes an optional enhancement outside the panel.
   - `confirm=true` is retired.
   - Add the Phase 0 end-to-end test with a real login.
8. **Upstream drafts under D19.**
   - (a) The DRC JSON `marker` patch, 40-80 lines, `cli.drc_json_marker`. *Draft now.*
   - (b) Three KiCad transline fixes, with the FEM tables as test vectors:
     - thickness double-count;
     - coupled thickness handling;
     - offset-stripline asymmetry.
   - (c) An optional `GetCopperGraph` / `pcb export connectivity` patch.
   - No general `pcb analyze` job.
9. **Contract changes to approve:**
   - `formats/board.py` `StackupLayer` sums `addsublayer`;
   - where the environment capability `native.geom` lives (`capability.py` is keyed on KiCad installs);
   - the import-linter additions in §4.3;
   - a dedicated cargo profile with `panic = "unwind"` for kiwrap-geom.
10. **Zone-to-zone clearance semantics.** Report fill-to-fill gaps labelled as such until KiCad's zone-to-zone evaluation is modelled. *Recommended.*

**Findings that need action regardless of these decisions:**
- **`ratsnest_estimate` bug.** It reports a false airwire when a track passes over a pad without ending on it. Regression case: jetson-agx-thor GND D63.1-D10.1; KiCad DRC reports 0 (03 §3.3).
- **The 499-per-type cap in kicad-cli DRC reports.** It silently truncates single runs, so every T3 probe must tile or check `capped` (07 §5).
- **KiCad 10.0.6 SWIG `LoadBoard` hangs on jetson** (> 5 min; kicad-cli loads it). Another reason not to put analysis on SWIG (05 §6).
