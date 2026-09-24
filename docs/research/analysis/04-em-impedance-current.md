# Impedance, coupling, current capacity and return path (track: em-and-physics)

Research for the physics behind `kw_pcb_analyze` items 25 `return_path`, 33 `coupling`, 34
`diffpair` / `rf_line` and 35 `current_capacity` of
[requests/kiwrap-feature-requests.md](../../../requests/kiwrap-feature-requests.md). It answers the
owner's open question in D23 ("research the best tooling, not just numpy") for the
transmission-line, field-solver and thermal part. Geometry (parallel length, gaps, voids, necks) is
covered by the other analysis tracks. This track only needs a cross-section and a stackup.

Date: 2026-09-24. Machine: Windows 11, Core Ultra 9 285K (24 threads), 127 GB RAM, Python 3.11.8
and 3.13 scratch venvs (uv), Rust 1.95.0, MinGW g++ 16.1. KiCad 10.0.6 (stock) and the private fork
`C:\src\kicad` at `kiwrap/6dd893f7d1` (master of 2026-09-17).

How to read the tags:
- `[MEASURED]`: reproduced on this machine.
- `[SOURCE]`: read in the KiCad tree.
- `[DOCS]`: read on the web. The URL is in §11.

Every timing and every accuracy figure in this document was measured here. No blog numbers are
used. Scratch scripts and raw results are under
`%TEMP%\claude\c--Upwork-KiCad\48a8df66-…\scratchpad\tooling\em-and-physics\`:
- `closedform.py`: the zero-dependency prototype;
- `fem2d.py`: the scikit-fem field solver;
- `validate.py`, `sweep.py`, `realboards_z.py`, `fullboard.py`, `misc.py`, `extract_boards.py`;
- `oems_line.py` (openEMS), `kicad_tc/harness.cpp` (KiCad transline, GPL, built only in scratch),
  `fdsolve/` (Rust prototype);
- `workloads/*.json`, `validation.json`, `sweep.json`, `realboards_z.json`, `misc.json`.

Nothing was installed into the kiwrap env or system Python. The repo was only read, apart from
this file.

---

## 1. Verdict

1. **The physics splits cleanly into two layers, and neither needs numpy at the core.**
   - *Closed forms* cover the everyday cases. They are pure `math`, re-derived from the papers,
     and run in 2-9 µs per evaluation. They can live in `kiwrap.formats` under the zero-dependency
     rule. `[MEASURED]`
   - *A 2-D quasi-static field solver* covers everything the closed forms get wrong: GCPW,
     strongly offset stripline, solder mask, etch trapezoid, mixed dielectrics, and any
     multi-conductor coupling. It needs a sparse linear solver. The best fit is an **optional extra**
     (`kiwrap[fields]` = scikit-fem + scipy + numpy, all BSD-3), or later a Rust port in the MIT
     helper crate. `[MEASURED]`
2. **Our FEM reproduces JLCPCB's own impedance calculator.**
   - The JLC04161H-7628 50 Ω outer microstrip comes out at W = 0.3503 mm; JLC's calculator gives
     0.3493 mm, a 0.3 % difference. At JLC's width the FEM gives Z0 = 50.09 Ω.
   - The JLC 50 Ω CPWG (0.294/0.200 mm) comes out at 49.86 Ω.
   - Four published JLC differential pairs land within 1.2-4.1 %.
   - This works only with the geometry JLC documents: trace thickness 1.6 mil, etch 0.7 mil,
     solder mask 1.2/0.6 mil with εr 3.8.
   - No closed form, and not KiCad's calculator, gets within 3 % on those cases. `[MEASURED]`
3. **KiCad's own transline code is not a reference to copy, and not only for licence reasons**
   (it is GPL, see T9). Against the FEM, openEMS and the published closed forms, KiCad 10.99:
   - overestimates microstrip Z0 by **+0.9 to +9 %** once copper thickness is non-zero (it applies
     the thickness correction twice, §6.1);
   - overestimates edge-coupled microstrip Zdiff by **+6.6 to +11.3 %**;
   - returns **different Zdiff for mirror-image offset coupled striplines**: 57.7 vs 65.6 Ω, where
     the FEM gives 65.2 Ω for both.

   KiCad 10.0's *tuning profiles* use this code to compute widths, so kiwrap must not treat a
   profile's width as ground truth. These are D19 upstream-draft candidates. `[MEASURED]` `[SOURCE]`
4. **Every closed-form GCPW model disagrees with the FEM by +2.5 to +10 %** at 35 µm copper. That
   includes ours, scikit-rf, KiCad and pcb-toolkit. The Ghione-Naldi thickness correction is too
   weak for PCB gaps. `rf_line` must use the field solver for coplanar lines, or tag the result
   `estimate`. `[MEASURED]`
5. **openEMS (GPL-3, separate process) confirms the FEM at zero thickness.**
   - Microstrip: 53.98 Ω, against 54.22 Ω (FEM) and 54.29 Ω (HJ80).
   - GCPW: 57.35 Ω, against 56.69 Ω (FEM) and 57.20 Ω (Ghione-Naldi).
   - Each line takes 4.1-5.3 min on 16 threads. That makes it a certification oracle, not a runtime
     tool. `[MEASURED]`
6. **Current capacity: IPC-2152 has no official closed form.**
   - The usable published fit is Brooks & Adam 2015, Eq. 3-2: `ΔT = 215.3·I²·W^-1.15·Th^-1.0`
     (mils), for external traces.
   - It gives *narrower* traces than IPC-2221 below about 1.5 A and *wider* ones above. For
     example, 10 A external at ΔT 10 °C needs 15.2 mm against 7.2 mm.
   - Ship both: IPC-2221 for KiCad parity and the Brooks-Adam fit as `estimate` with its test
     conditions. Add copper resistance ρ(T) for the mΩ figure. `[MEASURED]` `[DOCS]`
7. **Two kiwrap data gaps change the numbers more than any model choice.**
   - **(a)** `kiwrap.formats.board` reads only the first of a dielectric's `addsublayer` entries.
     On jetson this turns a 0.14 mm prepreg into 0.07 mm, and In2's 0.125 mm traces come out at
     39.4 Ω instead of 51.6 Ω. This is a contract change in `formats/board.py` (§8.4).
   - **(b)** KiCad 10's `.kicad_pro` `tuning_profiles` store the user's **declared reference
     layers** per signal layer. These are better than inferring planes. `[MEASURED]` `[SOURCE]`
8. **numpy 2.5 and scipy 1.18 no longer ship cp311 wheels** (they ship cp312-cp315). The kiwrap
   floor is Python 3.11, so a `[fields]` extra must either pin `numpy<2.5, scipy<1.18` on 3.11
   (the wheels installed here were numpy 2.4.6 and scipy 1.17.1) or require 3.12+. `[DOCS]`
   `[MEASURED]`

---

## 2. What each item needs from physics

| Item | Physics input | Output | Model tier |
|---|---|---|---|
| 34 diffpair | Stackup (h, εr, t per layer), reference layers, W, S per segment | Zdiff, Zodd/Zeven, εeff (skew), per-segment Zdiff, deviation from target | Closed form KJ84 (outer, S ≥ 0.15 mm). FEM for inner, mask, tight gap |
| 34 rf_line | + coplanar gap to the GND copper, frequency, length | Z0 (microstrip/GCPW), εeff, electrical length (°), λ/20 flags | Closed form HJ80 for microstrip. **FEM for GCPW** |
| 33 coupling | Parallel length, min gap per layer, rise time | Kb (NEXT), Kf (FEXT), saturation length | Modal: Kb = (Lm/L + Cm/C)/4 from FEM, or k/2 closed form. FEXT from the even/odd delay difference |
| 35 current_capacity | Bottleneck width/thickness along the path, layer (ext/int), via barrels, ΔT | I_max(ΔT) or ΔT(I), R (mΩ) at T | IPC-2221 (KiCad parity) + Brooks-Adam IPC-2152 fit (estimate) + ρ(T) |
| 25 return_path | Reference plane per layer, plane voids crossed | Fraction of return current displaced; nearest return via for a reference change | Stackup and plane detection; J(x) ∝ 1/(1+(x/h)²); λ/20 at f_knee = 0.35/tr |

---

## 3. Validation stackup: JLC04161H-7628

JLCPCB's default 4-layer impedance stackup (1.6 mm, 1 oz outer, 0.5 oz inner). `[DOCS]`
- JLC's impedance page gives the Dk: 7628 prepreg **4.4**, core **4.6**, solder mask 3.8.
- The API template (`getImpedanceTemplateSettings`, captured in gsuberland/jlcpcb_autogenerated_stackups,
  MIT script / public-domain data, 2025-05-20) gives the thicknesses.
- The KiCad stackup that repo generates uses εr 4.43 for the Nan Ya NP-155F core (datasheet)
  instead of 4.6.

| Layer | Thickness | εr / note |
|---|---|---|
| F.Cu (L1) | 0.035 mm | outer 1 oz |
| Prepreg 7628×1 | **0.2104 mm** | 4.4 |
| In1.Cu (L2) | 0.0152 mm | |
| Core | **1.065 mm** | 4.6 (JLC page) / 4.43 (NP-155F datasheet) |
| In2.Cu (L3) | 0.0152 mm | |
| Prepreg 7628×1 | 0.2104 mm | 4.4 |
| B.Cu (L4) | 0.035 mm | |

The JLC calculator geometry comes from "User Guide to the JLCPCB Impedance Calculator", updated
2026-09-16. `[DOCS]`
- Outer copper T1 = **1.6 mil** after plating.
- Top width = base width − **0.7 mil**.
- Mask C1 = **1.2 mil** over the substrate, C2 = **0.6 mil** over the trace, C3 = 1.2 mil between
  traces, CEr = **3.8**.

This is Polar SI9000's "coated microstrip" parameter set. The page does not name the solver.
Decompiling the calculator JS shows `/jlcTools/impedance/calc` plus a websocket result channel, and
that T1 is picked from a width-dependent table (`getT1ValueByConfig`). We did not script the
private API.

**JLC calculator outputs used as the fab reference** (all outer layer, L1 over L2):
- **Microstrip 50 Ω: W = 0.3493 mm** (aboutbytes.com, 2024-03-29, "JLCPCB online impedance
  calculator").
- **CPWG 50 Ω: W = 0.294 mm, gap = 0.200 mm** (same source).
- JITX `jitxlib.jlcpcb.JLC04161H_7628` routing structures:
  - RS_50: W = 0.3244 mm;
  - DRS_100: W = 0.1722 mm, S = 0.15 mm;
  - DRS_90: W = 0.2332 mm, S = 0.15 mm.
  - The page does not say which calculator produced these.
- EEVblog thread, 2021-06-27, "JLC7628", layer not stated:
  - 50 Ω SE: 11.55 mil;
  - 100 Ω diff: 8.08 mil / 8 mil;
  - 90 Ω diff: 10.28 mil / 8 mil.

The two published 50 Ω widths differ by 7 % (0.3493 vs 0.3244). JLC's calculator has changed over
time, and JLC itself says its results are "for reference only" with ±10 % fab tolerance.

---

## 4. Tools exercised

| Tool | Version | How it was run |
|---|---|---|
| **kiwrap prototype** `closedform.py` | this work | stdlib `math` only. HJ80 microstrip + KJ82 dispersion; KJ84 coupled microstrip + HJ thickness split; Cohn/Wheeler-78 stripline + Cohn offset superposition; Ghione-Naldi CBCPW + Gupta thickness; modal crosstalk; IPC-2221; Brooks-Adam IPC-2152 fit; ρ(T) |
| **2-D FEM** `fem2d.py` | scikit-fem 12.0.2, scipy 1.17.1, numpy 2.4.6 | Laplace on a graded tensor-product triangle mesh aligned to every interface and edge; trapezoid and conformal mask staircased; Maxwell C-matrix from energy; Z, εeff from C and C_air; L = (C_air)⁻¹/c². "P1 fast": hmin 1 µm, growth 1.12. "P2 ref": hmin 1 µm, growth 1.1, quadratic elements |
| **scikit-rf** | 2.1.0 (2026-08-13) | `media.MLine` (HJ + KJ), `media.CPW(has_metal_backside=True)` |
| **KiCad transline** | 10.99 master `common/transline_calculations` (GPL-2+) | Compiled into a scratch-only `harness.exe` (MinGW, static, 0.8 MB) and called as a separate process. Never linked into or shipped with kiwrap |
| **openEMS** | v0.37.0-rc3 Windows MSVC build (2026-09-22) | Python bindings (cp313 wheels from the bundle) in a separate 3.13 venv. 12 mm line, two MSL ports, Z from the port V/I probes |
| **pcb-toolkit** (Rust) | 0.2.1 (2026-09-17), MIT OR Apache-2.0 | Built from source, CLI `--json` |
| **Rust FV prototype** `fdsolve/` | this work, std only | 5-point finite volume on the same graded grid, Jacobi-PCG |

---

## 5. Results

### 5.1 JLC04161H-7628 validation set `[MEASURED]`

The "rect" cases use a rectangular 35 µm trace with no mask, the textbook geometry. The "JLC geom"
cases use T1 1.6 mil, etch 0.7 mil and the C1/C2 mask with εr 3.8. Quasi-static values (1 MHz for
the tools that take a frequency).

**M1: microstrip W = 0.350 mm, rect**

| Tool | Z0 (Ω) | εeff | Time per evaluation |
|---|---|---|---|
| kiwrap closed form (HJ80) | **51.711** | 3.1669 | **2.3 µs** |
| scikit-rf 2.1 MLine | 51.711 | 3.1669 | 636 µs |
| KiCad 10.99 microstrip | 52.450 (+1.4 %) | 3.0782 | 20 ms (process spawn) |
| FEM P2 (reference) | **51.718** | 3.1710 | 17.2 s |
| FEM P1 (fast) | 51.666 (−0.1 %) | 3.1716 | 0.53 s |

**M2: 50 Ω width synthesis**

| Tool | W (mm) |
|---|---|
| kiwrap HJ80, rect | 0.3721 |
| FEM rect | 0.3716 |
| KiCad 10.99, rect | 0.3813 |
| KiCad 10.99 + its soldermask cover (C 0.6 mil, T 1.6 mil) | 0.3705 |
| **FEM, JLC geom** | **0.3503** |
| **JLC calculator (2024)** | **0.3493** |
| JITX RS_50 | 0.3244 |

**M3: JLC's published 50 Ω widths, analysed**

| W | FEM JLC geom | HJ80 rect | KiCad (T 1.6 mil + mask) |
|---|---|---|---|
| 0.3493 mm (JLC calc 2024) | **50.09 Ω** | 51.77 Ω | 51.63 Ω |
| 0.3244 mm (JITX) | 52.03 Ω | 53.86 Ω | 53.69 Ω |

**G1: GCPW W = 0.294, gap = 0.200 mm (JLC 50 Ω design)**

| Tool | Z0 (Ω) | εeff |
|---|---|---|
| kiwrap Ghione-Naldi + Gupta t | 56.31 | 2.692 |
| scikit-rf CPW (back metal) | 55.86 | 2.793 |
| KiCad 10.99 coplanar (back metal) | 56.46 | 2.658 |
| pcb-toolkit coplanar | 49.49 | 2.822 |
| FEM rect (side grounds 30 h wide, at 0 V) | **52.41** | 2.872 |
| **FEM JLC geom** | **49.86** | 3.170 |

**S1: offset stripline on In1**
- Geometry: W = 0.150 mm; L1 plane 0.2104 mm above the trace, In2 plane 1.065 mm below
  (h1/h2 = 0.20); T = 15.2 µm.

| Tool | εr 4.4 / 4.4 | εr 4.4 prepreg / 4.6 core |
|---|---|---|
| kiwrap Wheeler78 + Cohn offset | 68.63 | 68.09 |
| KiCad 10.99 stripline | 68.29 (same result for A = either side) | n/a (homogeneous only) |
| **FEM** | **64.14** | **63.64** |

**D: edge-coupled outer pairs, Zdiff (Ω)**

| W / S (mm), source | KJ84 + HJ t (kiwrap) | KJ84 t = 0 | KiCad 10.99 | pcb-toolkit | FEM rect | FEM JLC geom | JLC target |
|---|---|---|---|---|---|---|---|
| 0.1722 / 0.150 (JITX DRS_100) | 107.12 | 124.00 | 119.24 | 108.63 | **109.04** | **101.16** | 100 |
| 0.2332 / 0.150 (JITX DRS_90) | 95.89 | 108.89 | 105.54 | 95.02 | **97.33** | **91.07** | 90 |
| 0.2052 / 0.2032 (EEVblog 100R) | 108.88 | 121.73 | 118.02 | n/a | **109.58** | **102.92** | 100 |
| 0.2611 / 0.2032 (EEVblog 90R) | 98.61 | 108.83 | 106.01 | n/a | **99.11** | **93.68** | 90 |

KiCad with its mask correction gives 118.7, 103.2, 117.6 and 103.9 Ω, still 13-19 % above JLC.

**Agreement with the JLC calculator.** The FEM with JLC's documented geometry lands within:
- **0.3 %** in width (50 Ω microstrip);
- **0.3 %** in Z0 (CPWG);
- **+1.2 %, +1.2 %, +2.9 %, +4.1 %** for the four differential pairs.

That is inside JLC's ±10 % and consistent with a Polar-class 2-D solver on the other end. The
closed forms, which cannot model mask or trapezoid, are 3-7 % off. KiCad is 3-19 % off.

### 5.2 Error bands of the closed forms against the FEM `[MEASURED]`

Signed error in %, relative to FEM P1. εr 4.4 and T 35 µm unless stated. `sweep.py`.

**Microstrip, H = 0.2104 mm (7628)**

| W/H | 0.3 | 0.5 | 1 | 1.5 | 2 | 3 | 5 |
|---|---|---|---|---|---|---|---|
| FEM Z0 (Ω) | 99.97 | 86.29 | 66.32 | 54.59 | 46.60 | 36.25 | 25.30 |
| HJ80 (kiwrap = scikit-rf = pcb-toolkit) | +0.32 | +0.30 | +0.19 | +0.10 | +0.07 | +0.08 | +0.09 |
| KiCad 10.99 | +3.60 | +2.93 | +2.06 | +1.62 | +1.37 | +1.10 | +0.85 |

**Microstrip, H = 0.0764 mm (3 mil)**

| W/H | 0.3 | 0.5 | 1 | 1.5 | 2 | 3 | 5 |
|---|---|---|---|---|---|---|---|
| HJ80 | +0.68 | +0.36 | +0.10 | −0.02 | −0.07 | −0.09 | −0.08 |
| KiCad 10.99 | **+8.97** | +7.19 | +5.12 | +4.08 | +3.45 | +2.73 | +2.02 |

**GCPW, W = 0.3 mm, H = 0.2104 mm**

| Gap (mm) | 0.10 | 0.15 | 0.20 | 0.30 | 0.50 |
|---|---|---|---|---|---|
| FEM Z0 (Ω) | 45.70 | 49.61 | 51.84 | 54.03 | 55.40 |
| Ghione-Naldi (kiwrap) | +2.56 | +6.78 | +7.49 | +8.20 | +9.75 |
| scikit-rf | +3.94 | +6.20 | +6.64 | +7.28 | +8.90 |
| KiCad 10.99 | +2.05 | +6.70 | +7.65 | +8.53 | +10.05 |

**Offset stripline, W = 0.15 mm, h1 + h2 = 1.2 mm, T = 17 µm**

| h1:h2 | 1:1 | 1:2 | 1:3.3 | 1:5 |
|---|---|---|---|---|
| FEM Z0 (Ω) | 81.04 | 77.10 | 69.85 | 62.31 |
| Wheeler78 + Cohn offset (kiwrap) | +0.07 | +1.60 | +4.35 | +7.01 |
| KiCad 10.99 | +0.01 | +1.45 | +3.90 | +6.57 |

**Coupled microstrip, W = 0.2 mm, Zdiff**

| S (mm) | 0.10 | 0.15 | 0.20 | 0.30 | 0.50 |
|---|---|---|---|---|---|
| FEM Zdiff (Ω) | 92.68 | 103.11 | 110.17 | 118.97 | 127.15 |
| KJ84 + HJ thickness (kiwrap) | −4.63 | −1.47 | −0.54 | −0.01 | +0.17 |
| pcb-toolkit (IPC-2141A/Saturn-compatible) | +1.05 | n/a | −1.38 | n/a | +0.66 |
| KiCad 10.99 | **+11.32** | +9.08 | +7.99 | +7.02 | +6.59 |
| FEM Kb = (Kc + Kl)/4 | 0.1476 | 0.1133 | 0.0896 | 0.0594 | 0.0307 |
| Closed-form k/2 = (Ze−Zo)/(2(Ze+Zo)) | 0.1595 | 0.1170 | 0.0907 | 0.0588 | 0.0293 |

Zero-thickness anchors, checking that the three independent paths agree:
- KiCad = kiwrap HJ80 to 5 digits (54.293 Ω).
- KiCad = kiwrap KJ84 to 1e-5 (Zdiff 123.9952 vs 123.9957).
- The FEM is within 0.1-0.9 % of the exact Cohn stripline (61.64 vs 61.51 Ω), of HJ80 (54.29 vs
  54.22 Ω) and of Ghione-Naldi (57.20 vs 56.69 Ω).
- The differences with thickness are therefore model differences, not transcription or FEM errors.

### 5.3 openEMS full-wave cross-check (zero-thickness conductors) `[MEASURED]`

| Case | openEMS Z0 over 0.2-5.2 GHz | FEM (t ≈ 0) | Closed form (t = 0) | Run |
|---|---|---|---|---|
| Microstrip W 0.35 / H 0.2104, εr 4.4 | 54.00 → 53.96 Ω | 54.22 Ω | 54.29 Ω (HJ80) | 1.22 M cells, 40,000 steps at 196.9 MCells/s ≈ 4.1 min, 16 threads |
| GCPW 0.294 / 0.200 | 57.48 → 57.30 Ω | 56.69 Ω | 57.20 Ω (GN) | 1.39 M cells, 5.3 min |

An attempt with 35 µm PEC boxes for thick copper gave nonphysical, frequency-drifting Z (94.9 →
81.7 Ω). The MSL port's probes sit inside the thick metal. This was not debugged further, so openEMS
is used only at t = 0.

### 5.4 Real boards (KiCad demos) `[MEASURED]`

Workloads were extracted with `kiwrap.formats.board.BoardFile`:
- stackup;
- plane layers, meaning a zone net covering more than 35 % of the outline bbox. This found every
  GND and PWR plane on the five demos;
- per-(layer, width) track lengths;
- diff-pair segment pairs (`*_P/_N`, `+/-`) with edge gap and overlap.

The comparison covers each layer's two longest widths (≥ 20 mm) and the six longest pair
combinations. All tools get the same geometry. For closed forms the εr of multi-dielectric stacks is
thickness-weighted.

| Board (tracks) | Signal-layer Z0 range (FEM) | HJ80 / Cohn vs FEM | KiCad vs FEM | Pairs: FEM Zdiff | KJ84 vs FEM (outer) | KiCad pair vs FEM |
|---|---|---|---|---|---|---|
| jetson (19,835) ¹ | 35.8-71.2 Ω | µstrip +0.05…+0.22 %; stripline −0.04…+2.84 % | −0.5…+3.6 % | 66.2-93.9 Ω | +0.17 % | **−12.95 %** (In5/In7), +0.7 % (In2), +8.6 % (outer) |
| vme-wren (24,858) | 44.3-67.1 Ω | +0.2 %; +0.7…+1.0 % | −0.5…+1.9 % | 95.5-115.7 Ω | −0.33 % | −2.3…+0.9 % (inner), +7.3 % (outer) |
| royalblue54L (943) | 27.6-52.2 Ω | −0.02…+0.03 %; +1.65…+2.59 % | +2.0…+3.2 % | 100-119 Ω | +0.06…+0.42 % | +8.5…+11.3 % |
| video (7,932) | 90.7-95.0 Ω | +0.25 % | +1.5…+1.6 % | no pairs | | |
| coldfire (2,935) | 41.1-95.0 Ω | −0.05…+0.25 % | −0.07…+1.6 % | no pairs | | |

¹ The jetson rows use kiwrap's current stackup reader, which drops `addsublayer` (§8.4). With the
sublayers summed (0.07 + 0.07 mm), In2 0.125 mm is **51.57 Ω, not 39.41 Ω**, and In7 0.15 mm is
47.58 Ω, not 35.83 Ω. The tool-vs-tool comparison is unaffected because every tool saw the same
geometry.

**Throughput on the real boards:**
- Closed form: **5.2-9.0 µs per evaluation** including dispatch, so a whole board's segments take
  milliseconds.
- FEM P1: **0.17-0.90 s** per single line and **0.37-1.06 s** per pair.
- Distinct cross-sections per board: 7 (video) to 168 (jetson, 38 widths plus 130 pair
  geometries).
- **Every** jetson cross-section: 254 s serial, **17.6 s wall** on a 16-process pool.
- vme-wren (88 sections): 93 s serial, **7.4 s wall**.
- Board load, for comparison: `BoardFile.load` takes 17.7 s (jetson) and 10.7 s (vme-wren).

### 5.5 Dispersion (εeff(f), microstrip W 0.3721 / H 0.2104, T 35 µm) `[MEASURED]`

| f | kiwrap KJ82 | scikit-rf KJ | KiCad 10.99 |
|---|---|---|---|
| 0.1 GHz | 3.1875 | 3.1875 | 3.1012 |
| 1 GHz | 3.1883 | 3.1883 | 3.1020 |
| 10 GHz | 3.2112 | 3.2128 | 3.1266 |
| 20 GHz | 3.2477 | 3.2516 | 3.1657 |

Dispersion moves εeff by less than 1 % below 10 GHz on these substrates. Quasi-static Z0 is enough
for `rf_line` below that, with `f` used only for the electrical length. KiCad's εeff is 2.7 % low
at every frequency because of the thickness double-count.

### 5.6 Crosstalk (item 33) `[MEASURED]`

- Take the JLC 100 Ω outer pair geometry (0.1722/0.15) with each line as the other's aggressor.
- Closed form: k = 0.242, so the saturated NEXT is 12.1 %. The saturation length is Lsat =
  v·tr/2 = 42.6 mm at tr = 0.5 ns. FEXT for 50 mm is 2.6 %.
- FEM Kb = 0.1168 (rect), 3.5 % below k/2.
- Across the sweep in §5.2, k/2 is within +8 % (S = 0.1 mm) to −5 % (S = 0.5 mm) of the FEM's
  inductive+capacitive Kb. That is good enough for a review check. Use the FEM value when the
  field solver is available.

### 5.7 Current capacity (item 35), 1 oz = 35 µm, ΔT = 10 °C `[MEASURED]`

Width needed, in mm:

| I (A) | IPC-2221 ext | Brooks-Adam IPC-2152 fit ext | IPC-2221 int | Brooks-Adam int (1 oz row) | R at 30 °C, ext fit width (mΩ/10 mm) |
|---|---|---|---|---|---|
| 0.5 | 0.115 | 0.083 | 0.300 | 0.075 | 61.6 |
| 1 | 0.300 | 0.277 | 0.781 | 0.248 | 18.5 |
| 2 | 0.781 | 0.926 | 2.033 | 0.823 | 5.5 |
| 3 | 1.367 | 1.874 | 3.556 | 1.657 | 2.7 |
| 5 | 2.766 | 4.555 | 7.194 | 4.004 | 1.1 |
| 10 | 7.194 | 15.208 | 18.716 | 13.258 | 0.34 |

- IPC-2221: `I = k·ΔT^0.44·A^0.725`, k = 0.048 external and 0.024 internal. This is exactly what
  KiCad's `pcb_calculator` uses (`panel_track_width.cpp:401-422`, 10.0.6). `[SOURCE]`
- IPC-2152 has no closed form. Its data are charts, as the review said.
- Brooks & Adam (2015) fitted the IPC-2152 external 2 oz and 3 oz curves with one equation
  (Eq. 3-2, the constants of Table 3-1) and report a close overlay.
- Their internal fits differ by weight: the constant is 110-300 across weights, with exponents
  `I^1.9-2 · W^-1.10…-1.15 · Th^-1.52`. We use the 1 oz row, which is conservative for 0.5 oz.
- IPC-2152's baseline is the worst case: still air, no copper planes. Planes, thicker boards and
  short traces cool traces substantially. Brooks & Adam, SI Journal 2019: a plane drops 69 °C to
  54 °C or 45 °C.
- So the result is an **upper-bound estimate**. It was not checked against the paywalled IPC-2152
  charts themselves.

---

## 6. KiCad transline findings (D19 upstream-draft candidates) `[MEASURED]` `[SOURCE]`

1. **Microstrip thickness is counted twice.**
   - `MICROSTRIP::microstrip_Z0()` (10.0.6 and 10.99 alike) widens u by the HJ80 Δu_r, which is
     correct. It then *also* subtracts a Bahl-Garg-style thickness term `q_t = (2 ln2/π)·(t/h)/√u`
     from the filling factor, and uses that reduced εeff in Z0.
   - HJ80 uses εeff(u_r) for Z0.
   - Result: Z0 is +0.85 % (wide lines on 7628) up to +9 % (3 mil dielectric, W/H 0.3) above the
     FEM. At t = 0 KiCad equals HJ80 to 5 digits.
   - This code came from the Transcalc/Qucs heritage. KiCad 10's tuning profiles use it, so a
     50 Ω profile on 7628 outer gets 0.381 mm instead of about 0.372 mm (rect), a +2.5 % width.
2. **Coupled microstrip barely responds to thickness.** Zdiff is +6.6 to +11.3 % above the FEM.
   With thickness corrections split between the even and odd modes (HJ80: u_o = u_e + t/(g·εr)),
   the error drops to −4.6…+0.2 %.
3. **Offset coupled stripline is not symmetric under a plane swap.**
   - W 0.15, S 0.125, 0.07 and 0.20 mm to the planes, εr 4.2: Zdiff is 57.7 Ω with A = 0.07 and
     65.6 Ω with A = 0.20. The FEM gives 65.19 Ω both ways.
   - The tuning profile passes `STRIPLINE_A = TopDielectricLayerThickness`, so the answer depends
     on which side the thinner dielectric is.
4. **CBCPW** uses the same Ghione-Naldi + Gupta formulas as everyone else. It is +2…+10 % against
   the FEM, which is a general closed-form limitation, not a KiCad bug.
5. The pcb_calculator **track width** tool is IPC-2221 only.

These are worth one draft MR in `n3rd-bugs/kicad-fork` (D19). Fixes 1-3 are each about 10-40 lines
with the FEM tables above as test vectors. The recommendation is to draft them after the kiwrap
models land, not before.

---

## 7. Candidate matrix

Licences below were read from the LICENSE file (GitHub API `license` content or the file in the
package) unless marked "(detected)". Activity is the last push or release seen on 2026-09-24.

| Candidate | Licence | Capabilities for items 25/33/34/35 | Measured here | Windows wheels / install | Maintenance | Robustness | Py / Rust | Verdict |
|---|---|---|---|---|---|---|---|---|
| **Closed forms re-derived from papers** (HJ80, KJ82, KJ84, Cohn54, Wheeler78, Ghione-Naldi 83/84, Gupta 96, IPC-2221, Brooks-Adam 2015) | ours, MIT | microstrip, coupled microstrip, stripline (sym/offset), CBCPW, dispersion, crosstalk, IPC current, R(T) | 2.3-9 µs/eval. Errors vs FEM in §5.2 | stdlib `math` only, about 300 lines | n/a | Singular at g → 0 and u → 0 (guard ranges); offset stripline +7 % at 1:5; GCPW +10 % | Python (Rust trivial) | **Adopt** as `kiwrap.formats` tier 0, with range and `estimate` tags |
| **scikit-fem** + scipy + numpy (2-D FEM, `fem2d.py`) | BSD-3 (scikit-fem LICENSE "Copyright 2018-26 scikit-fem developers"); numpy BSD-3 AND 0BSD AND MIT AND Zlib AND CC0-1.0; scipy BSD-3 | Any 2-D cross-section: mask, trapezoid, mixed εr, coplanar grounds, N conductors → C, L matrices, Zodd/Zeven, Kb/Kf | P1 0.17-1.5 s; P2 13-41 s. Reproduces JLC calc to 0.3 %. Agrees with openEMS at t = 0 to 0.4-1.2 % | skfem pure Python (2 MB); numpy 48 MB; scipy 113 MB. **numpy ≥ 2.5 and scipy ≥ 1.18 have no cp311 wheels** | skfem 12.0.2 (2026-06-05), pushed 2026-09-23 | Dirichlet node sets on conductors, energy-based C (variational, monotone under refinement); box-size and refinement studies in §5.1 | Python | **Adopt as optional extra `kiwrap[fields]`**, cached per cross-section, `run_async` |
| **scikit-rf** 2.1.0 | BSD-3 (LICENSE.txt, Arsenovic 2010 / scikit-rf devs 2020) | MLine (HJ, Schneider, Wheeler, KJ/Kobayashi dispersion), CPW (± back metal), Coaxial. **No stripline, no coupled lines** | MLine = kiwrap HJ80 to 1e-9; 636 µs per construct | pure Python 5 MB, but needs **pandas 37 MB** + numpy + scipy | release 2026-08-13, active | Good; loss models (Djordjevic-Svensson) are the best part | Python | **Dev/test oracle only** (pytest extra), not a runtime dependency |
| **KiCad `transline_calculations`** | GPL-2+ (file headers) | microstrip, coupled µstrip, stripline, coupled stripline (offset), CPW/CBCPW, coax; 10.99 adds soldermask cover | §5 and §6: +1…+19 % errors with thickness | inside KiCad; no CLI | active (2026-05) | §6 bugs | C++ | **Do not port** (T9). Read `.kicad_pro` `tuning_profiles` as *data* (reference layers, widths, target Z) |
| **openEMS** + CSXCAD | openEMS GPL-3.0 (detected); CSXCAD LGPL-3.0 (detected) | Full-wave 3-D FDTD: any line, via, discontinuity, S-params | Z at t = 0 within 0.4-1.2 % of FEM; **4.1-5.3 min per line** (16 threads, ~180-230 MCells/s) | Windows zip 65 MB → 150 MB; Python wheels only cp313/cp314 | v0.37.0-rc3 2026-09-22, active | Port setup is fiddly (thick-metal probe failure, §5.3) | Python (separate proc) | **Certification oracle only** (conformance corpus), separate process, never imported |
| **pcb-toolkit** (Rust crate + CLI) | MIT OR Apache-2.0 (Cargo.toml; no LICENSE file in the repo root) | microstrip, stripline, embedded, CBCPW, 5 diff topologies (incl. offset), IPC-2221 current, vias, Saturn-style calculators | microstrip = HJ80; coupled µstrip −1.4…+1.1 %; offset pair +2.3 % and symmetric; CBCPW −5.6 % | cargo, no deps issue | created 2026-03, 74 downloads, 5 stars | Young; states a validation status per model | Rust | **Cross-check oracle.** Could be vendored into `helper/` later; not a dependency now |
| **Rust FV solver prototype** `fdsolve/` | ours, MIT | Same cross-sections as the FEM | Within 0.1-0.2 % of FEM P2; 0.69 s (µstrip) to 2.7 s (pair) with naive Jacobi-PCG; 0.2 MB exe | std only | n/a | Converges on all cases; slow preconditioner | Rust | **Future option** to drop numpy: needs a direct or AMG solver (e.g. `faer` 0.24, MIT). Speed with faer not measured |
| Pure-Python FD (no numpy) | ours | same | **13.9 ms per PCG iteration** on 28.5 k nodes → about 46 s per microstrip (measured iteration cost × Rust-measured iteration count) | none | | | Python | **Reject** for interactive use |
| **FasterCap** / FastHenry2 (FastFieldSolvers) | FasterCap LGPL-2.1 (LICENCE.txt); FastHenry2 no licence file in repo (detected none) | 2-D/3-D capacitance with dielectrics (FasterCap); 3-D R/L (FastHenry: loop inductance, current distribution) | not run (binaries behind a download form; building needs wxWidgets) | Windows installers after a form | last commit 2019 | Mature BEM | exe (separate proc) | **Skip.** Our FEM covers 2-D. FastHenry is a possible later oracle for item 29 loop inductance |
| **NGSolve / Netgen** | LGPL-2.1 (detected) | General FEM + mesher (true curved/trapezoid geometry) | not run | cp311-313 win_amd64 wheels, 15.6 + 8.0 MB download | 6.2.2607 2026-09-09 | Excellent | Python | **Not needed.** LGPL via Python import is dynamic linking (OK if shipped unmodified), but it brings nothing scikit-fem lacks for Manhattan PCB sections |
| **FEniCSx (dolfinx)** | LGPL-3.0 (detected) | General FEM | not run | **No PyPI package**; conda-forge win-64 0.11.0 only | active | Excellent | Python | **Reject** (conda-only on Windows) |
| **gmsh** | GPL-2+ with linking exception for some libs | Mesher | not run | py2.py3 win_amd64 wheels | 4.15.2 2026-03 | | Python API links GPL lib | **Reject in-process** (GPL). Not needed: tensor meshes suffice |
| **Triangle / meshpy** | `triangle` PyPI wrapper LGPL-3.0, but Shewchuk's Triangle forbids commercial use without a licence; meshpy MIT wrapper of Triangle/TetGen (AGPL) | Mesher | not run | wheels cp311-313 | | | Python | **Reject** (licence traps) |
| **Elmer FEM** | GPL-2 (LICENSE.md) | Multiphysics incl. electrostatics and heat (thermal item 35 in 3-D) | not run | Windows installers | pushed 2026-09-23 | Excellent | exe | **Skip.** Thermal FEM of traces is beyond a review check |
| **FEMM 4.2** (+ pyfemm) | Aladdin Free Public License (FEMM wiki) | 2-D electrostatics / heat, Windows GUI via ActiveX | not run | Windows installer | stale | Good | exe (COM) | **Reject** (non-OSI licence, GUI automation) |
| **atlc / atlc2** | atlc GPL; atlc2 freeware | 2-D FD from bitmaps | not run | old binaries | atlc 4.6.1 (2011); atlc2 unmaintained | bitmap-resolution limited | exe | **Reject** |
| **lineforge** | AGPL-3.0 (relicensed from GPL-3) | MCP transmission-line calculator + bitmap FD solver | not run | not on PyPI | created 2026-05, 1 star | self-reported ±2 % vs openEMS | Python | **Reject** (AGPL, immature) |
| **TNT-MMTL** (Mayo BEM) | GPL | 2-D BEM multilayer multiconductor, SPICE models | not run | Win32 binaries from 2004-2006 | forks only | Mature BEM | exe | **Skip** (stale; FEM covers it) |
| **EMerge**, **femwell**, **SignalIntegrity**, **Qucs-S qucstrans** | GPL-2+ (gmsh-derived), GPL-3, GPL-3+, GPL-2 (LICENSE files) | 3-D FEM / waveguide modes / SI toolkit / Transcalc | not run | | active | | Python / C++ | **Reject in-process** (GPL). No capability we lack |
| **Palace** (AWS), **MFEM** | Apache-2.0, BSD-3 | 3-D FEM EM | not run | Linux-first; no Windows Python wheels for PyMFEM on PyPI | active | | C++ | **Skip** (3-D, Windows unsupported) |
| **Sonnet Lite**, **Saturn PCB Toolkit**, **Polar SI9000** | proprietary | 2.5-D planar EM; calculators; industry 2-D BEM | not run | Windows GUI | | Industry reference | none | **Reference values only.** JLC's parameter set is SI9000-style |
| "pcb-tools", "transmission-line", "tline" (PyPI) | Apache-2.0 (Gerber tools 2019) / no such package / "Proprietary", Python == 3.12.8 only | none relevant | | | | | | **Reject** |

---

## 8. Recommendation for kiwrap

### 8.1 Tier 0: `kiwrap.formats.physics` (zero dependency, MIT)

Port `closedform.py` into `kiwrap.formats` (stdlib `math` only; the import rule holds). Each
function's docstring cites its paper, and every result carries its validity range:
- `microstrip`: HJ80 + KJ82 εeff dispersion. Z0 stays quasi-static.
- `coupled_microstrip`: KJ84 static + HJ thickness split.
- `stripline`: Wheeler 1978 thick strip; offset by Cohn superposition. Tag `estimate` when h1/h2 is
  outside [0.67, 1.5].
- `gcpw`: Ghione-Naldi + Gupta. **Always** tag `estimate` (§5.2).
- `crosstalk`: k/2 NEXT, saturation length, modal FEXT.
- `ipc2221_current`, `ipc2152_brooks_adam_dt`, `resistance_ohm(T)`.
- `return_current_fraction(d, h)`: `2/π·atan(d/h)`, so 50 % within ±h and 79.5 % within ±3 h.

Add unit tests with the tables in §5 as vectors. Include the zero-thickness anchors: HJ80 54.293 Ω,
KJ84 Zdiff 123.996 Ω, Cohn 61.644 Ω.

### 8.2 Tier 1: optional extra `kiwrap[fields]`

- scikit-fem (BSD-3), scipy, numpy. Pin `numpy<2.5, scipy<1.18` for Python 3.11, or make the
  extra 3.12+. This is a dependency addition the owner must approve, per CLAUDE.md.
- The solver is `fem2d.py` generalised:
  - input: a stackup slice (bands), conductor polygons (trapezoids), coplanar grounds, mask;
  - output: C and L matrices, then Z0, Zdiff/Zcomm, εeff per mode, and Kb/Kf.
- Default P1 fast settings: accuracy within 0.1-0.2 % of P2, about 0.2-1.5 s per section.
- Cache results by the section's hash, since whole boards have 7-168 distinct sections. Run
  through `run_async` with a process pool: jetson measured 17.6 s wall for all 168.
- `kw_pcb_analyze` picks the model per section:
  - closed form when in range and no `fab_comparable` flag;
  - FEM when the extra is installed and the section is GCPW, offset stripline outside the ratio,
    tight gap (S < 0.75 h), or `fab_comparable=true`. `fab_comparable` adds the fab's etch, plating
    and mask, for example the JLC profile values of §3.
  - otherwise closed form with `degraded: ['estimate']`.
- Later: port the solver into `helper/` (Rust, MIT) with a direct sparse solver (`faer`) if the
  numpy footprint is unwanted. The std-only prototype already gives the same numbers (§7).

### 8.3 Oracles (tests only, never shipped)

- openEMS as a separate process, for a conformance corpus of zero-thickness lines (4-5 min each).
- The KiCad transline harness, only to track §6 and KiCad-parity where kiwrap reports "what KiCad
  would say".
- pcb-toolkit and scikit-rf as dev-dependency cross-checks.

### 8.4 Data prerequisites (contract changes to report)

1. **`formats/board.py` `StackupLayer`: sum `addsublayer` thicknesses** and keep the per-sublayer
   εr and tan δ. `_parse_setup` reads `layer.get("thickness")`, which returns only the first
   sublayer. Found on jetson (3 dielectrics) and tiny_tapeout. The impact is Z0 39.4 vs 51.6 Ω
   (§5.4).
2. **Reference layers.**
   - Read `.kicad_pro` `tuning_profiles[].layer_entries[]`: `signal_layer`, `top_reference_layer`,
     `bottom_reference_layer`, `width`, `diff_pair_gap`, `delay`, plus `target_impedance` per
     profile (KiCad 10.0.6 `common/project/tuning_profiles.cpp`). `[SOURCE]`
   - Otherwise infer planes from zone fills. A threshold of more than 35 % of the outline bbox
     found every GND/PWR plane on the 5 demos. Tag `reference_inferred`.
3. **`stackup_defaulted`** when εr or thickness is missing (KiCad writes no stackup for old boards)
   or when the material is the generic "FR4" at 4.5.
4. Solder mask εr and thickness come from the stackup (`F.Mask` thickness 0.01-0.01524 mm on the
   demos, εr often missing). Default to 3.8 / 0.6 mil only for `fab_comparable`.

### 8.5 Per item

- **34 diffpair**
  - Per coupled segment: Zdiff (tier 0 for outer S ≥ 0.15 mm; else tier 1), deviation from the
    profile's `target_impedance`, and the uncoupled length at the segment's single-ended Z0.
  - Skew from the modal delay: `√εeff_odd/c` × length.
- **34 rf_line**
  - Microstrip: tier 0.
  - GCPW: tier 1, or tier 0 with `estimate`.
  - Electrical length `360·f·L·√εeff/c`, plus λ/20 at `f` or at `f_knee = 0.35/tr`.
- **33 coupling**
  - NEXT = Kb·min(1, L_par/L_sat); FEXT = (Td_even − Td_odd)/(2 tr), which is 0 for homogeneous
    stripline.
  - Kb from FEM when available, otherwise k/2 (within −5…+8 %).
- **35 current_capacity**
  - Bottleneck cross-section along the path from the geometry track, reported with IPC-2221 (KiCad
    parity) and the Brooks-Adam IPC-2152 fit tagged `estimate` with the test conditions (still air,
    no plane).
  - Resistance at T_amb + ΔT with α = 0.00393.
  - Via barrels are π·(d + p)·p conductors; flag them rather than claim a rating.
- **25 return_path**
  - Reference plane per segment from §8.4.
  - The fraction of return current displaced by a void at lateral distance d from the trace:
    1 − `2/π·atan(d/h)`.
  - On a layer change, the nearest return via compared against λ/20 at f_knee.
  - Plane-to-plane transitions between different nets: flag the absence of stitching capacitance.
    The geometry comes from the other tracks.

---

## 9. Licensing notes

- **GPL code (KiCad transline, openEMS, qucstrans, EMerge, femwell).** None is imported. The KiCad
  harness exists only in scratch, as a measuring instrument. The closed forms in `closedform.py`
  were written from the papers' equations (§10). KiCad's source was read only to *explain* its
  numbers (§6) and to list the papers its comments cite.
- **LGPL (NGSolve, dolfinx, CSXCAD, FasterCap).**
  - Importing an unmodified LGPL shared library through Python is dynamic linking. It is
    permissible for an MIT distribution if the user can replace the library and the licence text
    ships with it.
  - We recommend none of them in-process because scikit-fem (BSD) covers the need.
  - FasterCap would run as a separate executable in any case.
- **Triangle.** The PyPI `triangle` wrapper says LGPL-3.0, but Shewchuk's Triangle itself may not be
  used commercially without permission. Avoid it and meshpy.
- **AGPL (lineforge).** Separate process only per D8, and not recommended.

---

## 10. Papers the tier-0 models are re-derived from

- Hammerstad, Jensen, "Accurate Models for Microstrip Computer-Aided Design", IEEE MTT-S Digest
  1980, pp. 407-409.
- Kirschning, Jansen, "Accurate model for effective dielectric constant of microstrip with validity
  up to millimetre-wave frequencies", Electronics Letters 18(6), 1982, pp. 272-273.
- Kirschning, Jansen, "Accurate Wide-Range Design Equations for the Frequency-Dependent
  Characteristic of Parallel Coupled Microstrip Lines", IEEE Trans. MTT 32(1), 1984, pp. 83-90;
  errata MTT 33(3), 1985, p. 288.
- Cohn, "Characteristic Impedance of the Shielded-Strip Transmission Line", IRE Trans. MTT 2(2),
  1954, pp. 52-57.
- Cohn, "Shielded Coupled-Strip Transmission Line", IRE Trans. MTT 3(5), 1955, pp. 29-38.
- Wheeler, "Transmission-Line Properties of a Strip Line Between Parallel Planes", IEEE Trans. MTT
  26(11), 1978, pp. 866-876.
- Wadell, *Transmission Line Design Handbook*, Artech House 1991, §3.5-3.6.
- Ghione, Naldi, "Parameters of Coplanar Waveguides with Lower Common Planes", Electronics Letters
  19(18), 1983, pp. 734-735.
- Ghione, Naldi, "Analytical Formulas for Coplanar Lines in Hybrid and Monolithic MICs",
  Electronics Letters 20(4), 1984, pp. 179-181.
- Gupta, Garg, Bahl, Bhartia, *Microstrip Lines and Slotlines*, 2nd ed., Artech House 1996,
  eq. 7.98-7.100.
- Brooks, Adam, "Trace Currents and Temperatures Revisited", UltraCAD, April 2015 (edit
  2015-07-20), Eq. 3-2 and Table 3-1.
- Brooks, Adam, "PCB Trace Current/Temperature Relationships and their Dependencies", Signal
  Integrity Journal, 2019-10-08.
- IPC-2221 (the k·ΔT^0.44·A^0.725 form) and IPC-2152 (2009, chart-based).

---

## 11. Sources (read 2026-09-24)

**JLCPCB stackup and calculator**
- JLCPCB controlled impedance stackups and Dk: https://jlcpcb.com/impedance
- JLCPCB calculator user guide (T1, etch, mask; updated 2026-09-16):
  https://jlcpcb.com/help/article/User-Guide-to-the-JLCPCB-Impedance-Calculator
- JLCPCB calculator page and JS bundle (endpoints `/jlcTools/impedance/calc`,
  `getImpedanceTemplateSettings`): https://jlcpcb.com/pcb-impedance-calculator
- gsuberland/jlcpcb_autogenerated_stackups (commit 9c8b685, 2025-05-20; MIT script, public-domain
  data): https://github.com/gsuberland/jlcpcb_autogenerated_stackups

**JLC calculator outputs**
- aboutbytes, "EM simulation – RF design parameters" (2024-03-29):
  https://aboutbytes.com/em-simulation-rf-design-parameters/
- JITX `JLC04161H_7628`: https://docs.jitx.com/en/latest/jitxlib/jitxlib.jlcpcb.JLC04161H_7628.html
- EEVblog, "JLCPCB Impedance Calculator can't get 100R or 90R" (2021-06-27):
  https://www.eevblog.com/forum/manufacture/jlcpcb-impedance-calculator-cant-get-100r-or-90r/
- LibreCAL PR #8 (Saturn values on JLC04161H-7628): https://github.com/jankae/LibreCAL/pull/8

**Candidate projects**
- scikit-rf: https://github.com/scikit-rf/scikit-rf (LICENSE.txt), https://pypi.org/project/scikit-rf/
- scikit-fem: https://github.com/kinnala/scikit-fem (LICENSE), https://pypi.org/project/scikit-fem/
- numpy / scipy / pandas wheels: https://pypi.org/project/numpy/ , https://pypi.org/project/scipy/ ,
  https://pypi.org/project/pandas/
- openEMS: https://github.com/thliebig/openEMS , releases:
  https://github.com/thliebig/openEMS-Project/releases (v0.37.0-rc3, 2026-09-22; v0.0.36,
  2023-10-22); CSXCAD: https://github.com/thliebig/CSXCAD
- FasterCap: https://github.com/ediloren/FasterCap (LICENCE.txt);
  https://www.fastfieldsolvers.com/download.htm ; FastHenry2: https://github.com/ediloren/FastHenry2
- NGSolve: https://pypi.org/project/ngsolve/ , https://pypi.org/project/netgen-mesher/
- FEniCSx: https://github.com/FEniCS/dolfinx ; conda-forge https://anaconda.org/conda-forge/fenics-dolfinx
- gmsh: https://pypi.org/project/gmsh/ ; triangle: https://pypi.org/project/triangle/ ; meshpy:
  https://pypi.org/project/meshpy/
- Elmer: https://github.com/ElmerCSC/elmerfem (LICENSE.md)
- FEMM licence: https://www.femm.info/wiki/License
- atlc: https://atlc.sourceforge.net/ ; atlc2: http://www.hdtvprimer.com/KQ6QV/atlc2.html
- lineforge: https://github.com/RFingAdam/lineforge
- TNT-MMTL: https://mmtl.sourceforge.net/ , https://github.com/corecode/mmtl-tnt
- EMerge: https://github.com/FennisRobert/EMerge (LICENSE); femwell: https://github.com/HelgeGehring/femwell ;
  SignalIntegrity: https://github.com/Nubis-Communications/SignalIntegrity (LICENSE.txt);
  Qucs-S: https://github.com/ra3xdh/qucs_s
- Palace: https://github.com/awslabs/palace ; MFEM: https://github.com/mfem/mfem
- pcb-toolkit: https://github.com/akiselev/pcb-toolkit , https://crates.io/crates/pcb-toolkit ;
  faer: https://crates.io/crates/faer
- Saturn PCB Toolkit: https://saturnpcb.com/saturn-pcb-toolkit/

**Current capacity**
- Brooks & Adam 2015 PDF: https://www.mathscinotes.com/wp-content/uploads/2016/06/pcbtempr.pdf
- Brooks & Adam 2019: https://www.signalintegrityjournal.com/articles/1406-pcb-trace-currenttemperature-relationships-and-their-dependencies
- smps.us IPC-2152 interpolation (Jack Olson coefficients, for context):
  https://www.smps.us/pcb-calculator.html

**KiCad source** (fork tree `C:\src\kicad`)
- `common/transline_calculations/*` (10.0.6 and master 2026-05-26)
- `pcbnew/dialogs/panel_setup_tuning_profile_info.cpp`
- `common/project/tuning_profiles.cpp`
- `pcb_calculator/calculator_panels/panel_track_width.cpp`
