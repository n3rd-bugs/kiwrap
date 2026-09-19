"""Unit tests for kiwrap.formats.epochs."""

from __future__ import annotations

import pytest

from kiwrap.formats.epochs import read_generator, read_stamp, set_stamp
from kiwrap.formats.sexpr import dumps, loads

SCH7 = '(kicad_sch (version 20230121) (generator eeschema)\n  (uuid 1234)\n  (paper "A4")\n)\n'
SCH10 = (
    '(kicad_sch\n\t(version 20260306)\n\t(generator "eeschema")\n\t(generator_version "10.0")\n'
    '\t(uuid "abcd")\n)\n'
)
MOD = '(footprint "R_0603"\n\t(version 20241229)\n\t(generator "pcbnew")\n\t(generator_version "9.0")\n\t(layer "F.Cu")\n)\n'


def test_read_stamp_and_generator():
    assert read_stamp(loads(SCH7)) == 20230121
    assert read_generator(loads(SCH7)) == ("eeschema", None)
    assert read_stamp(loads(SCH10)) == 20260306
    assert read_generator(loads(SCH10)) == ("eeschema", "10.0")
    assert read_generator(loads(MOD)) == ("pcbnew", "9.0")
    assert read_stamp(loads(MOD)) == 20241229


def test_read_stamp_errors():
    with pytest.raises(ValueError, match="no \\(version"):
        read_stamp(loads('(kicad_sch (paper "A4"))'))
    with pytest.raises(ValueError, match="not an integer"):
        read_stamp(loads('(kicad_sch (version "x"))'))
    assert read_generator(loads("(kicad_sch)")) == ("", None)


def test_set_stamp_preserves_bare_generator_and_inserts_generator_version():
    root = loads(SCH7)
    set_stamp(root, 20260306, "eeschema", "10.0")
    assert dumps(root) == (
        '(kicad_sch (version 20260306) (generator eeschema)\n  (generator_version "10.0")\n'
        '  (uuid 1234)\n  (paper "A4")\n)\n'
    )
    # back to a 7.0-loadable header: generator_version removed (7.0 rejects the token, claim 9)
    set_stamp(root, 20230121, "eeschema", None)
    assert dumps(root) == SCH7


def test_set_stamp_is_a_noop_when_nothing_changes():
    root = loads(SCH10)
    set_stamp(root, 20260306, "eeschema", "10.0")
    assert dumps(root) == SCH10


def test_set_stamp_changes_only_touched_atoms():
    root = loads(SCH10)
    set_stamp(root, 20250114, "kiwrap", "9.0")
    out = dumps(root)
    assert out == SCH10.replace("20260306", "20250114").replace('"eeschema"', '"kiwrap"').replace(
        '"10.0"', '"9.0"'
    )


def test_set_stamp_inserts_missing_header_after_leading_scalars():
    root = loads('(footprint "X"\n\t(layer "F.Cu")\n)\n')
    set_stamp(root, 20260206, "pcbnew", "10.0")
    assert dumps(root) == (
        '(footprint "X"\n\t(version 20260206)\n\t(generator "pcbnew")\n\t(generator_version "10.0")\n'
        '\t(layer "F.Cu")\n)\n'
    )
    bare = loads("(kicad_pcb)")
    set_stamp(bare, 20221018, "pcbnew", None)
    assert dumps(bare) == '(kicad_pcb\n\t(version 20221018)\n\t(generator "pcbnew")\n)'
