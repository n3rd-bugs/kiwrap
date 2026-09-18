import uuid, sys
LX, LY = 2.54, 5.08   # pin connection point in library coords (asymmetric)
combos = []
i = 0
for rot in (0, 90, 180, 270):
    for mir in (None, 'x', 'y'):
        i += 1
        X = 50.8 + (i % 4) * 30.48
        Y = 50.8 + (i // 4) * 30.48
        combos.append((i, rot, mir, X, Y))
sheet_uuid = "11111111-1111-1111-1111-111111111111"
out = []
out.append(f'''(kicad_sch
  (version 20250610)
  (generator "eeschema")
  (generator_version "10.0")
  (uuid "{sheet_uuid}")
  (paper "A4")
  (lib_symbols
    (symbol "T:PINT"
      (pin_names (offset 1.016))
      (exclude_from_sim no) (in_bom yes) (on_board yes)
      (property "Reference" "U" (at 0 -3 0) (effects (font (size 1.27 1.27))))
      (property "Value" "PINT" (at 0 3 0) (effects (font (size 1.27 1.27))))
      (property "Footprint" "" (at 0 0 0) (hide yes) (effects (font (size 1.27 1.27))))
      (property "Datasheet" "" (at 0 0 0) (hide yes) (effects (font (size 1.27 1.27))))
      (symbol "PINT_0_1"
        (rectangle (start -1.27 -1.27) (end 1.27 1.27) (stroke (width 0.254) (type default)) (fill (type none)))
      )
      (symbol "PINT_1_1"
        (pin input line (at {LX} {LY} 180) (length 2.54)
          (name "IN" (effects (font (size 1.27 1.27))))
          (number "1" (effects (font (size 1.27 1.27))))
        )
      )
    )
  )
''')
for (i, rot, mir, X, Y) in combos:
    u = uuid.uuid4()
    mirs = f"(mirror {mir})" if mir else ""
    out.append(f'''  (symbol
    (lib_id "T:PINT")
    (at {X} {Y} {rot})
    {mirs}
    (unit 1)
    (exclude_from_sim no) (in_bom yes) (on_board yes) (dnp no)
    (uuid "{u}")
    (property "Reference" "U{i}" (at {X} {Y-3} 0) (effects (font (size 1.27 1.27))))
    (property "Value" "PINT" (at {X} {Y+3} 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "" (at {X} {Y} 0) (hide yes) (effects (font (size 1.27 1.27))))
    (property "Datasheet" "" (at {X} {Y} 0) (hide yes) (effects (font (size 1.27 1.27))))
    (pin "1" (uuid "{uuid.uuid4()}"))
    (instances (project "pinorient" (path "/{sheet_uuid}" (reference "U{i}") (unit 1))))
  )
''')
out.append('  (sheet_instances (path "/" (page "1")))\n)\n')
open("pinorient.kicad_sch", "w").write("".join(out))
open("combos.txt","w").write("\n".join(f"U{i} rot={rot} mirror={mir} X={X} Y={Y}" for (i,rot,mir,X,Y) in combos))
print("\n".join(f"U{i} rot={rot} mirror={mir} X={X} Y={Y}" for (i,rot,mir,X,Y) in combos))
