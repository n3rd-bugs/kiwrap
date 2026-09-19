"""Unit tests for kiwrap.formats.sexpr (no KiCad needed)."""

from __future__ import annotations

import pytest

from kiwrap.formats.sexpr import Atom, Node, ParseError, build, dump, dumps, load, loads, quote, unquote
from kiwrap.formats.sexpr.cst import Style, format_number

# ----------------------------------------------------------------------------------------------
# quoting (rules verified against kicad-cli 10.0.0, see cst.py module docstring)
# ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("plain", '"plain"'),
        ("", '""'),
        ('say "hi"', '"say \\"hi\\""'),
        ("back\\slash", '"back\\\\slash"'),
        ("line1\nline2", '"line1\\nline2"'),
        ("cr\rlf\n", '"cr\\rlf\\n"'),
        ("tab\there", '"tab\there"'),  # KiCad writes tabs raw
        ("with (parens) and spaces", '"with (parens) and spaces"'),
        ("{slash}in-band", '"{slash}in-band"'),  # application-level escapes untouched
        ("ünïcødé ✓", '"ünïcødé ✓"'),
    ],
)
def test_quote(value, expected):
    assert quote(value) == expected
    assert unquote(expected) == value


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('"A\\tB"', "A\tB"),
        ('"A\\\\tB"', "A\\tB"),
        ('"bell\\a"', "bell\a"),
        ('"\\b\\f\\v"', "\b\f\v"),
        ('"\\x41\\x7a"', "Az"),
        ('"\\x4g"', "\x04g"),  # one hex digit then a literal g
        ('"\\101\\12"', "A\n"),
        ('"\\1234"', "S4"),  # at most three octal digits
        ('"\\{G\\/H\\ I\\z"', "\\{G\\/H\\ I\\z"),  # unknown escapes keep the backslash
        ('"trailing\\\\"', "trailing\\"),
        ("unquoted", "unquoted"),
        ('"', '"'),  # a lone quote is returned as-is
    ],
)
def test_unquote_kicad_lexer_rules(raw, expected):
    assert unquote(raw) == expected


def test_quote_unquote_round_trip_arbitrary_text():
    text = "".join(chr(c) for c in range(0, 128)) + "€"
    assert unquote(quote(text)) == text


# ----------------------------------------------------------------------------------------------
# numbers
# ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, "0"),
        (0.0, "0"),
        (-0.0, "0"),
        (90, "90"),
        (90.0, "90"),
        (1.27, "1.27"),
        (-0.898426, "-0.898426"),
        (172.085, "172.085"),
        (0.1 + 0.2, "0.3"),
        (123456.789012, "123456.789"),  # %.10g: ten significant digits
        (0.0001, "0.0001"),
        (0.00001, "0.00001"),  # no exponent for tiny values
        (1.5e-7, "0.00000015"),
        (12345678901.5, "12345678901.5"),  # no exponent for large values either
        (20260306, "20260306"),
    ],
)
def test_format_number(value, expected):
    assert format_number(value) == expected
    assert "e" not in format_number(value)


def test_format_number_rejects_bool_and_nan():
    with pytest.raises(TypeError):
        format_number(True)
    with pytest.raises(ValueError):
        format_number(float("nan"))


def test_atom_kinds():
    root = loads('(a 1 -2.5 .5 1e3 +7 sym "str" 1.2.3 - 00042)')
    kinds = [c.kind for c in root.children]
    assert kinds == [
        "symbol",
        "number",
        "number",
        "number",
        "number",
        "number",
        "symbol",
        "string",
        "symbol",
        "symbol",
        "number",
    ]
    assert root.children[1].as_int() == 1
    assert root.children[2].as_float() == -2.5
    assert root.children[7].value == "str"
    assert loads('(code "1")').atoms[0].as_int() == 1  # netlists quote numbers
    with pytest.raises(ValueError):
        root.children[6].as_float()
    with pytest.raises(ValueError):
        root.children[2].as_int()


# ----------------------------------------------------------------------------------------------
# parsing and lossless round trip
# ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "(a)",
        "(a)\n",
        "()",
        "( )",
        "(a ())",
        "(a\n\t(b 1)\n)\n",
        "(a\r\n\t(b 1)\r\n)\r\n",
        "  (a  b   c )  \n\n",
        '(a "s with (parens) and \\"quotes\\"" (b "" c))',
        '(a "multi\nline\n\tstring")',
        '(a "tab\there")',
        "(a 1)(b 2)",  # implicit document without whitespace
        '(version 1)\n\n# comment\n(rule "x"\n\t(constraint clearance (min 0.2mm))\n)\n',
        "# leading comment\n(a)\n# trailing comment\n",
        "(a\n\t# comment inside\n\t(b)\n)",
        '(a b"c")',  # quote glued to a bare token
        "(a (b (c (d (e (f))))))",
        "(a é 中 \U0001f600)",
    ],
)
def test_round_trip_exact(text):
    assert dumps(loads(text)) == text


def test_empty_list_and_nested_parens_in_strings():
    root = loads('(a () (b "x)y(z" ()))')
    assert root.head == "a"
    empty = root.children[1]
    assert isinstance(empty, Node) and empty.children == [] and empty.head == ""
    b = root.find("b")
    assert b is not None and b.values() == ["x)y(z"]
    assert dumps(root) == '(a () (b "x)y(z" ()))'


def test_implicit_document_and_comments():
    text = '(version 1)\n# note\n(rule "r"\n\t(constraint clearance (min 0.2mm))\n)\n'
    doc = loads(text)
    assert doc.implicit and doc.head == ""
    assert [c.kind if isinstance(c, Atom) else c.head for c in doc.children] == ["version", "comment", "rule"]
    assert doc.find("rule").find("constraint").find("min").values() == ["0.2mm"]
    assert dumps(doc) == text


def test_hash_after_token_is_a_bare_token_not_a_comment():
    # verified: kicad-cli 10 rejects a trailing "# comment" after a token, so it is not a comment
    root = loads("(a 1 #x)")
    assert [c.raw for c in root.children] == ["a", "1", "#x"]
    assert root.children[2].kind == "symbol"


@pytest.mark.parametrize(
    ("text", "line", "col", "message"),
    [
        ("(a", 1, 3, "unterminated list"),
        ("(a))", 1, 4, "unbalanced"),
        ('(a "oops)', 1, 4, "unterminated string"),
        ("(a)\n(b)\nc", 3, 1, "atom outside"),
        ("", 1, 1, "empty"),
        ("   \n ", 1, 1, "empty"),
        ("(a\n  (b\n    (c 1)\n  )\n", 5, 1, "unterminated list"),
    ],
)
def test_parse_errors_carry_line_and_column(text, line, col, message):
    with pytest.raises(ParseError) as info:
        loads(text)
    assert info.value.line == line and info.value.col == col
    assert message in info.value.message


def test_load_dump_preserve_crlf(tmp_path):
    text = "(kicad_pcb\r\n\t(version 20260206)\r\n)\r\n"
    path = tmp_path / "a.kicad_pcb"
    path.write_bytes(text.encode())
    root = load(path)
    out = tmp_path / "b.kicad_pcb"
    dump(root, out)
    assert out.read_bytes() == text.encode()


# ----------------------------------------------------------------------------------------------
# navigation
# ----------------------------------------------------------------------------------------------

SCH = """(kicad_sch
\t(version 20260306)
\t(generator "eeschema")
\t(generator_version "10.0")
\t(uuid "5f80c590-fe58-403a-81ae-8d41b5328502")
\t(paper "A4")
\t(symbol
\t\t(lib_id "Device:R")
\t\t(at 63.5 50.8 0)
\t\t(property "Reference" "R1"
\t\t\t(at 66.04 49.53 0)
\t\t)
\t\t(property "Value" "1k"
\t\t\t(at 66.04 52.07 0)
\t\t)
\t)
)
"""


def test_find_get_values():
    root = loads(SCH)
    assert root.head == "kicad_sch"
    assert root.get("version") == "20260306"
    assert root.get("generator") == "eeschema"
    assert root.get("missing") is None and root.get("missing", "d") == "d"
    sym = root.find("symbol")
    assert sym is not None
    assert sym.find("at").values() == ["63.5", "50.8", "0"]
    props = sym.find_all("property")
    assert [p.values()[:2] for p in props] == [["Reference", "R1"], ["Value", "1k"]]
    assert len(list(root.walk())) == 13
    assert [n.head for n in root.nodes][:3] == ["version", "generator", "generator_version"]
    assert sym.index(props[1]) == 4  # children[0] is the head atom


# ----------------------------------------------------------------------------------------------
# editing: minimal diffs
# ----------------------------------------------------------------------------------------------


def test_atom_edit_changes_only_its_line():
    root = loads(SCH)
    ref = root.find("symbol").find_all("property")[0].atoms[1]
    ref.value = "R2"
    out = dumps(root)
    diff = [(a, b) for a, b in zip(SCH.split("\n"), out.split("\n"), strict=True) if a != b]
    assert diff == [('\t\t(property "Reference" "R1"', '\t\t(property "Reference" "R2"')]


def test_number_edit_keeps_kicad_formatting():
    root = loads(SCH)
    at = root.find("symbol").find("at")
    at.atoms[0].value = 64.77
    at.atoms[2].value = 90
    assert dumps(at) == "(at 64.77 50.8 90)"
    at.atoms[1].value = 50.800000  # still no trailing zeros
    assert dumps(at) == "(at 64.77 50.8 90)"


def test_set_touches_only_changed_atoms():
    root = loads(SCH)
    root.set("version", 20260306)  # unchanged value
    assert dumps(root) == SCH
    root.set("generator_version", "11.0")
    assert '(generator_version "11.0")' in dumps(root)
    at = root.find("symbol").find("at")
    root.find("symbol").set("at", 1, 2)
    assert dumps(at) == "(at 1 2)"
    root.find("symbol").set("at", 1, 2, 90, 5)
    assert dumps(at) == "(at 1 2 90 5)"
    new = root.set("embedded_fonts", False)
    assert dumps(new) == "(embedded_fonts no)" and dumps(root).endswith("\t(embedded_fonts no)\n)\n")


def test_remove_and_replace():
    root = loads(SCH)
    sym = root.find("symbol")
    value_prop = sym.find_all("property")[1]
    sym.remove(value_prop)
    assert dumps(root) == SCH.replace('\t\t(property "Value" "1k"\n\t\t\t(at 66.04 52.07 0)\n\t\t)\n', "")
    sym.replace(sym.find("at"), build("at", 10, 20, 180))
    assert "\t\t(at 10 20 180)\n" in dumps(root)
    # replacing an atom with a string keeps the slot
    sym.find("lib_id").replace(sym.find("lib_id").atoms[0], "Device:C")
    assert '(lib_id "Device:C")' in dumps(root)


def test_remove_last_sublist_closes_inline():
    root = loads("(a\n\t(b 1)\n)\n")
    root.remove(root.find("b"))
    assert dumps(root) == "(a)\n"


# ----------------------------------------------------------------------------------------------
# build(): KiCad Prettify formatting
# ----------------------------------------------------------------------------------------------


def test_build_scalar_list_inline():
    assert dumps(build("at", 1, 2, 90)) == "(at 1 2 90)"
    assert dumps(build("hide", True)) == "(hide yes)"
    assert dumps(build("layer", "F.Cu")) == '(layer "F.Cu")'
    assert dumps(build("type", Atom.symbol("default"))) == "(type default)"
    assert dumps(build("empty")) == "(empty)"


def test_build_compound_one_child_per_line_tab_indent():
    node = build(
        "property",
        "Reference",
        "R1",
        build("at", 66.04, 49.53, 0),
        build("effects", build("font", build("size", 1.27, 1.27)), build("justify", Atom.symbol("left"))),
    )
    assert dumps(node) == (
        '(property "Reference" "R1"\n'
        "\t(at 66.04 49.53 0)\n"
        "\t(effects\n"
        "\t\t(font\n"
        "\t\t\t(size 1.27 1.27)\n"
        "\t\t)\n"
        "\t\t(justify left)\n"
        "\t)\n"
        ")"
    )


def test_build_inserted_at_depth_uses_tree_indent_and_newline():
    root = loads(SCH)
    root.find("symbol").append(build("uuid", "abc"))
    assert '\t\t)\n\t\t(uuid "abc")\n\t)\n' in dumps(root)
    root.find("symbol").append(
        build("instances", build("project", "p", build("path", "/x", build("unit", 1))))
    )
    assert (
        '\t\t(instances\n\t\t\t(project "p"\n\t\t\t\t(path "/x"\n\t\t\t\t\t(unit 1)\n\t\t\t\t)\n\t\t\t)\n\t\t)\n\t)\n'
        in dumps(root)
    )
    legacy = loads('(kicad_sch (version 20230121) (generator eeschema)\n  (paper "A4")\n)\n')
    legacy.append(build("junction", build("at", 1, 2)))
    assert dumps(legacy).endswith('  (paper "A4")\n  (junction\n    (at 1 2)\n  )\n)\n')
    crlf = loads("(kicad_pcb\r\n\t(version 1)\r\n)\r\n")
    crlf.append(build("net", 0, ""))
    assert dumps(crlf) == '(kicad_pcb\r\n\t(version 1)\r\n\t(net 0 "")\r\n)\r\n'


def test_build_xy_packing_until_column_99():
    pts = build("pts", *[build("xy", 100 + i * 1.234567, 50) for i in range(10)])
    lines = dumps(pts).split("\n")
    assert lines[0] == "(pts" and lines[-1] == ")"
    for line in lines[1:-1]:
        assert line.startswith("\t(xy ")
        # a new (xy is only appended while the column before it is < 99
        col = 0
        for chunk in line.split(") (")[:-1]:
            col += len(chunk) + 1
            assert col < 99
        assert len(line) >= 99 or line is lines[-2]


def test_build_long_atom_list_wraps_at_column_72():
    members = build("members", *[f"{i:08d}-0000-0000-0000-000000000000" for i in range(4)])
    assert dumps(members) == (
        '(members "00000000-0000-0000-0000-000000000000" "00000001-0000-0000-0000-000000000000"\n'
        '\t"00000002-0000-0000-0000-000000000000" "00000003-0000-0000-0000-000000000000"\n'
        ")"
    )


def test_build_rejects_unsupported_values():
    with pytest.raises(TypeError):
        build("a", object())
    with pytest.raises(ValueError):
        Atom.symbol("has space")
    with pytest.raises(ValueError):
        Atom.number("abc")


def test_incremental_append_of_xy_matches_build():
    pts = build("pts")
    for i in range(10):
        pts.append(build("xy", 100 + i * 1.234567, 50))
    assert dumps(pts) == dumps(build("pts", *[build("xy", 100 + i * 1.234567, 50) for i in range(10)]))


def test_reformat_moved_subtree():
    root = loads(SCH)
    deep = root.find("symbol").find_all("property")[0]
    root.append(deep)  # moved: gets a lead for its new depth but keeps its inner whitespace
    assert '\t(property "Reference" "R1"\n\t\t\t(at 66.04 49.53 0)\n\t\t)\n)\n' in dumps(root)
    deep.reformat()
    assert dumps(root).endswith('\t(property "Reference" "R1"\n\t\t(at 66.04 49.53 0)\n\t)\n)\n')


def test_copy_is_detached_and_equal_text():
    root = loads(SCH)
    sym = root.find("symbol")
    cp = sym.copy()
    assert cp.parent is None and dumps(cp) == dumps(sym)
    cp.find("at").atoms[0].value = 1
    assert dumps(sym) != dumps(cp)


def test_style_detection():
    from kiwrap.formats.sexpr.cst import _style_of

    assert (_style_of(loads(SCH)).indent, _style_of(loads(SCH)).newline) == ("\t", "\n")
    st = _style_of(loads("(a\r\n  (b)\r\n)\r\n"))
    assert (st.indent, st.newline) == ("  ", "\r\n")
    assert isinstance(Style(), Style)


def test_one_mb_board_parses_under_two_seconds():
    """Synthetic 1 MB board: the acceptance bar is < 2 s (real demo boards are ~0.1 s/MB)."""
    import time

    seg = '\t(segment\n\t\t(start 1.5 2.5)\n\t\t(end 3.5 4.5)\n\t\t(width 0.2)\n\t\t(layer "F.Cu")\n\t\t(net 1)\n\t\t(uuid "00000000-0000-0000-0000-000000000000")\n\t)\n'
    text = "(kicad_pcb\n\t(version 20260206)\n" + seg * (1_000_000 // len(seg) + 1) + ")\n"
    assert len(text) >= 1_000_000
    t0 = time.perf_counter()
    root = loads(text)
    elapsed = time.perf_counter() - t0
    assert dumps(root) == text
    assert elapsed < 2.0, f"parse took {elapsed:.2f}s"
