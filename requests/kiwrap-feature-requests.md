# kiwrap: feature requests from the PR Repeater V2.0 review (2026-09-23)

**Context.** These come from a read-only review of an existing KiCad 10.0 4-layer board: a schematic review, then an EMC-focused layout review. Twelve agents in total used kiwrap through a stdio MCP client against a scratch copy of the project.
- kw_* calls used: `kw_pcb_drc` (with refill + parity), `kw_pcb_get` (footprints, pads, tracks, vias, zones, nets, outline, rules; ipc11 on 10.99) and `kw_pcb_render` (3D).
- Everything else had to be built by hand: SVG crops, S-expression parsing of zone fills and graphics, Gerber parsing, raster path searches.

Priority tags:
- **P0**: safety / data loss.
- **P1**: blocked the review or forced re-parsing the .kicad_pcb.
- **P2**: new analysis that would replace custom scripts.
- **P3**: convenience.

---

## P0: safety and read-only use

1. **`kw_pcb_drc` upgrades the board file format when saving the refill.**
   - Observed: defaults (`refill_zones` null -> refill, `save_board` -> yes) on a 10.0 board (`version 20260206`) via the 10.99 install rewrote it as `20260901`. KiCad 10.0 then refuses to open it ("upgrade KiCad to version 10.99 or later"). On a real project this locks the user out of their own KiCad.
   - Ask: never save in a newer epoch than the file's own unless `target_kicad` / an explicit flag says so. Auto-select the install matching the file epoch, or refill in memory and don't save by default.
2. **Read-only mode.** Every call made a git "pre-call snapshot", a `.kiwrap/` directory and `<name>.drc.json` inside the project.
   - Ask: `readonly=true` per call, or `KIWRAP_READONLY=1` / `serve --readonly`: no snapshot, no save, outputs to a temp directory.
3. **`kw_pcb_drc_report(filter by bbox, ref, type, class)`**: re-read the last DRC result without refilling or saving, so verifiers don't have to re-run a mutating DRC.
4. **`kw_fab_export` for reviewers.**
   - Ask: `out_dir` outside the project, a `layers` filter (e.g. only F.Paste/F.Mask), `dry_run`.
   - For `target='pos'`, `dry_run` should return excluded refs with the reason (attr unspecified / dnp / tht / exclude_from_pos) and a diff against the schematic BOM. That would have caught 30 "unspecified"-type SMD parts plus 4 PCB-only DNP parts in one call.

## P1: transport and paging

5. **One-shot CLI**: `kiwrap call <tool> '<json>' [--out file]` and `kiwrap tools [--schema name]`. Today the CLI only has `serve | env | certify | version`, so a session without `.mcp.json` needs a hand-written JSON-RPC client.
6. **`kw_pcb_get` paging.**
   - The page is shortened by a char budget (~40 kB) even with `limit=2000`: 180 tracks / 133 pads / 169 vias per page.
   - Ask: `out_path` or `format='ndjson'` to write the complete result to a file in one call.
   - Bug: a saved first page said "continue with cursor=180" while `data.next_cursor` was null. Make them consistent.
7. **`kw_pcb_render` output file disappears.** The PNG under `%LOCALAPPDATA%/kiwrap/tmp/render/...` was gone once the server exited. Ask: `out_path`, or keep files until an explicit cleanup.

## P1: missing data in `kw_pcb_get`

8. **Zones** (only bbox / filled / priority today). Add with `detail='geometry'`:
   - filled polygons per layer, with holes (not fractured), island ids, area_mm2, and the vias/pads inside each island
   - fill settings: clearance, min_thickness, thermal_gap, spoke width, connect_pads mode, island_removal, fill mode
   - keepout rule flags
   - `teardrop: true`: the "Net-(J1-Shield) zone" was really a teardrop
   - `include_footprint_zones=true`: footprint-owned zones with parent ref. Examples here: U12's three antenna keep-outs, CR2's keep-out, and U8's 17 zones on F.Paste. Those need a `plotted:false` flag, because they were the root cause of U8 having no stencil aperture.
9. **Graphics** (`kinds=['graphics']`):
   - Board `gr_poly` / `gr_rect` / `gr_line` / `gr_arc` / `gr_circle` on copper, with net, layer, fill, `stroke_width_mm`, and an effective outline inflated by stroke/2. This board draws VBUS/VSYS/VBAT+/5V+ as 19 such shapes. The 0.127 mm half-stroke turned a 0.171 mm gap into a 0.044 mm VBUS-GND near-short, and none of it is visible through kiwrap.
   - Footprint copper graphics (`fp_poly` / `fp_rect` on *.Cu) in board coordinates, with owning ref and net / `<no net>`. The PCB inverted-F antenna radiators are invisible otherwise.
10. **Outline.** `outline.segments` and `closed` came back null. Ask: real Edge.Cuts primitives, internal cutouts (the ESP32 antenna cutout rect) and holes (`gr_circle`).
11. **Stackup** (`kinds=['stackup']`): layer order, dielectric thickness, εr, loss tangent, copper thickness, and plane net per layer. Impedance, λ/20 and return-path checks all need it.
12. **Pads.**
    - Geometry: absolute `rotation_deg`, board-frame polygon/bbox (`size_mm` is footprint-local and ambiguous), roundrect ratio / chamfer / custom primitives resolved.
    - Margins: `solder_mask_margin`, `solder_paste_margin` (effective) + ratio, `has_paste` / `has_mask`.
    - Zone connection: override plus achieved result per layer (mode, spokes achieved, spoke width, fill contact area).
    - Holes: `drill_shape` / `drill_size_mm [x,y]` / slot angle. Oval NPTH and slots currently come back as a scalar.
    - `pinfunction`.
13. **Footprints.**
    - Fabrication flags: `exclude_from_pos_files`, `board_only`, `net_tie_pad_groups`, `duplicate_pad_numbers_are_jumpers`.
    - Geometry: courtyard polygon + `has_courtyard`, and the F.Fab body bbox (to catch courtyard smaller than body).
    - Schematic parity fields: `sch_dnp`, `sch_in_bom`, `sch_value`, `sch_footprint`.
    - Optional `mating_direction` for connectors (from 3D model offset or a field), so "cable exits into the antenna wing" can be checked.
    - `properties` came back `{}` with `detail=full`.
14. **Nets.** `code`, `pad_count` and `netclass` were null. Ask: pad list with component value/MPN, e.g. `Net-(U8-USB_DET)` = R60 10k -> VSYS.
15. **Rules.** Board setup values: `pad_to_mask_clearance`, `solder_mask_min_width`, `allow_soldermask_bridges`, plus the `rule_severities`, including which checks are set to *ignore*. Here `missing_courtyard` and `footprint_type_mismatch` were ignored, hiding 23 missing courtyards and 30 wrongly typed parts.
16. **Vias.** A `near={x_mm, y_mm}` / `near_items=[pads]` query with net filter and `k`: the k nearest vias with distances.
17. **Schematic context.** `kw_sch_get` or `kw_pcb_get` `pin_context(pin='U8.13')`: connected components with values and far-end nets (R60 10k -> VSYS). The charge-current selection (ISET1 vs ISET2) couldn't be resolved from kiwrap data.

## P1: `kw_pcb_drc` output quality

18. **Location.** `pos` should be the violation location, not the polygon's first vertex. Add `closest_points_mm [[x,y],[x,y]]`, numeric `actual_mm` / `required_mm`, and the layer.
    - Seen: VBUS-GND 0.044 mm reported at (152.02, 98.30), but the gap is at (164.0, 102.4).
    - Seen: starved thermal for U8 pad 10 reported at (126.27, 34.24), which is off the board.
19. **`report_all_track_errors=true`.** One colliding track produced only one violation, and which one varied between runs. kiwrap showed SW vs C35 pad at 0.120 mm, while kicad-cli showed the worse SW vs VBUS polygon at 0.113 mm.
20. **Bucket per violation.** Put the kiwrap bucket (must_fix / review / cosmetic) on each violation object. Today it can only be inferred from the summary counts.
21. **By-design hints.**
    - Netless footprint copper bridging two pads of different nets (inverted-F antenna, net tie): mark `by_design_candidate` and suggest `net_tie_pad_groups`.
    - Duplicate-number mechanical pads (4x shield tab "25"): report as "3 of 4 shell tabs not tied", not three airwires with "route a track".
22. **Padstack "no paste" warnings.** Cross-check against the plotted paste. Here they were real (U8's paste lives in non-plotted F.Paste zones), and a reviewer wrongly dismissed them because footprint paste polygons exist.
23. **`ignored_checks`** in the DRC result.
24. **Fab-capability audit** (`profile='jlcpcb-4L'`), reported separately from project rules: copper-copper, hole-to-copper, mask dam, annular ring, slot, copper-edge. A 0.044 mm gap is below the fab limit, while 0.114 mm is only a rule violation.

## P2: new read-only analysis checks (`kw_pcb_check` / a new `kw_pcb_analyze`)

Every item below was written by hand at least once during this review (raster BFS/Dijkstra, shapely, Gerber parsing).

25. **`return_path`**
    - Per outer-layer segment: adjacent reference plane and length over voids, ignoring the track's own via antipads, with a threshold.
    - Per signal via that changes reference: nearest return (GND) via distance.
    - A plane-void list with crossing nets.
26. **`plane_slots`**: merged antipad clusters with dimensions, and the traces crossing them.
27. **Stitching family:**
    - `stitching` / `edge_stitching`: via-fence gaps along each plane/board edge with start/end and legal fill-in positions.
    - `gnd_reach`: geodesic distance through the pour to the nearest via, with max and location per region.
    - `pour_necks`: narrow necks and pockets via erosion.
    - islands/slivers below a width or area threshold.
28. **`copper_path` / `route_path` / `net_topology`**: pad-to-pad (or pad-to-nearest-via) geodesic length through tracks, fills, graphics and vias, with minimum neck width, layer sequence, via count and the series parts along the path. Answers "is the TVS or series R at the connector end?".
29. **`loop_area` / `current_loop`**: ordered pads (e.g. `C35.2, U8.2, U8.5, C35.1`) or a switcher template (`ic, cin, cout, inductor`). Returns the real loop area using the actual return path, the ideal area, and per-segment layer and length.
30. **`nearest_via` / `pad_return` / `exposed_pad`**:
    - Per pad: achieved zone connection, geodesic distance to the nearest same-net via, and neck width.
    - Per EP: via-in-pad count, fill contact, and other-net copper on the opposite layer that would block via-in-pad. Here a B.Cu VBUS polygon sits under the U8 EP.
31. **`measure(a, b)` / `clearance_pairs` / `region_clearance` / `antenna_clearance` / `keepout_audit`**:
    - Minimum edge-to-edge distance between any items (including graphics with stroke, holes, courtyards), with closest points.
    - Every item inside a region or within a radius of antenna footprints.
    - Graphic copper and pads inside rule areas: a VSYS polygon covers 2.96 mm² of `ind_keepout_1` without any DRC report.
32. **`probe_via` / `via_spots`** (what-if, read-only): legality of a via at given points, or legal positions in a bbox sorted by distance to a target, against every layer including graphic copper and keepouts. Several hand-picked "free" stitching spots turned out to be 0.10-0.14 mm from the stroked VBUS polygon.
33. **`coupling`**: aggressor/victim net pairs with parallel length and minimum gap per layer, optional NEXT from stackup and rise time.
34. **`diffpair` and `rf_line`**:
    - Diff pair: coupled vs uncoupled length, gap stats, Zdiff per segment from the stackup, skew, via count and return vias.
    - RF line: width, reference height, measured coplanar gap, Z0 (microstrip / GCPW), electrical length in degrees, discontinuities, via-fence pitch.
35. **`current_capacity`**: bottleneck along a pad-to-pad path including graphics, zones and via count, with ΔT and mΩ per IPC-2152.
36. **`interfaces` / `esd_entry`**: for connectors and switches, each pin's net, the protection parts on it with copper distance from the pin, route length to the first series/clamp part, unreferenced length, neighbour nets, and clamp-GND-to-via distance.
37. **Assembly checks:**
    - `assembly_outputs`: simulate CPL/BOM (smd_only, exclude_dnp) and return dropped / phantom refs. Here U9/U10 antennas have an empty value.
    - `paste_coverage`: per footprint/pad, aperture area from the plotted paste vs pad area. Flags 0% (U8) and 100% EP (U2).
    - `mask_web`: webs below the minimum and gang openings (U2 0.098 mm).
    - `field_parity`: structured diff of PCB vs schematic fields. The F1 LCSC field is a 1.5 A fuse on the PCB but 2 A in the schematic.

## P2: `kw_pcb_render`

38. **Region crop and layers:** `bbox_mm` and `px_per_mm` (up to ~60; the 1024 px cap makes 0.2 mm features unreadable), any layer list including In1/In2, F/B.Paste and F/B.Mask, and `mirror_bottom`.
39. **Overlays:** `color_by_net` / `highlight_nets` / `highlight_refs`, thermal spokes and antipads, and annotations (points, segments, labels, measured distances) so a check result can be shown on the image.
