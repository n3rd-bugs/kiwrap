# MR draft: footprint rule areas exported in the wrong frame (commit `e8c61b3c09` of `mr/specctra-fp-zone-frame`; NOT opened, branch pushed to the private fork only, 2026-09-20)

Branch `mr/specctra-fp-zone-frame` on `n3rd-bugs/kicad-fork` (private; never pushed to `public` or
GitLab) = `base/6dd893f7d1` (nightly 10.99.0-4083) + one commit (docs/UPSTREAM.md §3.4: one logical
change per MR). It is independent of the two `specctra-jobs` MRs (it touches the exporter they call,
not the jobs) and can be opened first: it is a bug fix in code the editor's File > Export > Specctra
DSN menu already runs.

| Commit | Subject | Files |
|---|---|---|
| `e8c61b3c09` | Export a footprint's rule areas in the image frame | `pcbnew/specctra_import_export/specctra_export.cpp` (-11 / +5), `qa/tests/pcbnew/test_specctra_export.cpp` (+47, 1 case in the existing `SpecctraExport` suite) |

## The bug

Since the affine-transform change a footprint zone keeps its outline in the footprint's LIBRARY
frame (`ZONE::GetLibraryOutline()` is `m_Poly`; `ZONE::GetBoardOutline()` applies
`FOOTPRINT::GetTransform()`). `SPECCTRA_DB::makeIMAGE()` still treated `m_Poly` as board geometry:

```cpp
ZONE untransformedZone( *zone );
EDA_ANGLE angle = -aFootprint->GetOrientation();
angle.Normalize();
untransformedZone.Rotate( aFootprint->GetPosition(), angle );
...
point -= aFootprint->GetPosition();
```

`ZONE::Rotate` on a footprint zone rotates the library outline about the inverse-transformed
centre and the subtraction then removes the board position from library coordinates, so the image
keepout of any PLACED footprint lands `position` away from the part, rotated by `-orientation`
(blinker R1 at (37.3, 14.6) rotated -90: 41.6 mm off). An external router (Freerouting) never sees
the real keepout and routes tracks and vias through it; DRC reports the violations after the
session import. Observed on the kiwrap bench (`docs/UPSTREAM.md` G17, Phase 1c review finding 3):
the same board exported by KiCad 10.0.6's `ExportSpecctraDSN` (board-frame storage) is right.

## The fix

The library outline IS the image geometry (relative to the footprint origin, unrotated - exactly
what `IMAGE` coordinates are, and what the pads already use through `SetFPRelativePosition`), so
`makeIMAGE()` iterates `zone->GetLibraryOutline()` and drops the rotate / subtract. Back-side
footprints are flipped to the top by `FlipFOOTPRINTs()` before `makeIMAGE()`; `ZONE::Mirror` mirrors
the library outline about the footprint origin for them, so nothing else changes.

## Verification (Windows, RelWithDebInfo `msvc-win64-release`, VS 2022 17.14 / MSVC 14.44)

- `qa_pcbnew.exe --run_test='SpecctraExport*'`: 4/4 (the three pre-existing cases plus
  `SpecctraExportFootprintRuleAreaInImageFrame`: the test board's first footprint is placed at
  (50, 40) mm, rotated 90, and given a 2 x 3 mm track + via rule area whose library corners are
  (1, 2) .. (3, 5) mm; the DSN's image keepout holds `1000 -2000`, `3000 -2000`, `3000 -5000`,
  `1000 -5000` (micrometres, y up) and none of the position-shifted values). "No errors detected",
  77 assertions.
- `qa_api.exe` full run on the integration commit `0cdfa41cb6`: 108/110, the two known test-side
  failures (`ApiE2E/CreateDocument` path separator, `ApiWizards/QueryWizards` Windows mock gap);
  nothing in this commit touches the API.
- `clang-format` (LLVM 19.1.5, `_clang-format`) `--dry-run --Werror` clean on the changed ranges.
- Oracle test from kiwrap on the installed build (`C:/kiwrap-builds/6dd893f7d1-6`, T7
  `test_t7_library_frame_footprint_keepout_equals_swig_export_of_the_board_frame_file` and the
  codec side of it): a blinker board with a keepout inside a rotated footprint, saved by the
  nightly through `kicad-cli pcb upgrade` (stamp 20260901, zone in the library frame), exports
  the same image keepout as KiCad 10.0.6's SWIG `ExportSpecctraDSN` of the original board-frame
  file (`compare_dsn`: 0 differences at 5 um).
- T4 `--help` tree of the installed build = the -5 build's (no CLI change); T5 IPC handler
  snapshot `10.99.0+0cdfa41cb6.json` EQUAL to the stock nightly's.

## MR description (for GitLab)

Title: `Specctra export: write footprint rule areas in the image frame`

A footprint's rule areas are stored in the footprint's library frame since the affine-transform
change, but `SPECCTRA_DB::makeIMAGE()` still un-rotated the outline about the footprint position
and subtracted the position, the transform a board-frame outline needs. For any placed footprint
the image keepout therefore landed tens of millimetres away from the part, and an external
autorouter routed straight through the real keepout; DRC reported the violations after the session
import.

Use the library outline (`ZONE::GetLibraryOutline()`) as the image geometry, like the pads. Back
side footprints are flipped to the top before the images are built and `ZONE::Mirror` already
mirrors the library outline for them, so nothing else changes. The QA case places a rotated
footprint carrying a rule area and checks that the exported image keepout holds the library
corners and none of the shifted ones.
