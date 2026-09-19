"""Library tables: parsing, variable expansion, nested `Table` rows, hidden/disabled, precedence.
No KiCad needed (a stand-in install points `share` at the fixture directory)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from kiwrap.formats.libs import (
    LibResolver,
    NotFound,
    UnsupportedLibrary,
    expand_uri,
    library_env,
    parse_table,
)
from kiwrap.formats.libs.tables import load_tables

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "libs"


@dataclass
class StubInstall:
    root: Path = FIXTURES
    share: Path = FIXTURES
    version: str = "10.0.6"

    @property
    def major(self) -> int:
        return 10


def env_for(project: Path | None = None, **extra: str) -> dict[str, str]:
    env = library_env(StubInstall(), {"KIWRAP_TEST_LIBS": str(FIXTURES)}, project)
    env.update(extra)
    return env


# ---- expansion -----------------------------------------------------------------------------------


def test_expand_uri_forms_and_unresolved():
    env = {"KICAD10_SYMBOL_DIR": "C:/s", "KIPRJMOD": "C:/p", "X": "v"}
    assert expand_uri("${KICAD10_SYMBOL_DIR}/a.kicad_sym", env, major=10) == ("C:/s/a.kicad_sym", [])
    assert expand_uri("$(KIPRJMOD)/lib", env, major=10) == ("C:/p/lib", [])
    assert expand_uri("$X/y", env, major=10) == ("v/y", [])
    assert expand_uri("%X%/y", env, major=10) == ("v/y", [])
    text, missing = expand_uri("${NOPE}/y", env, major=10)
    assert text == "${NOPE}/y" and missing == ["NOPE"]
    assert expand_uri("~/x", env, major=10)[0] == str(Path.home()) + "/x"


def test_versioned_variable_fallback_maps_other_majors_and_legacy_names():
    """KiCad 8+ maps an unmatched KICAD<n>_* (and KICAD_SYMBOL_DIR / KISYS3DMOD) onto the running
    version's variable (common.cpp KIwxExpandEnvVars)."""
    env = {
        "KICAD10_SYMBOL_DIR": "S",
        "KICAD10_FOOTPRINT_DIR": "F",
        "KICAD10_3DMODEL_DIR": "M",
        "KICAD10_3RD_PARTY": "T",
    }
    assert expand_uri("${KICAD9_SYMBOL_DIR}/x", env, major=10) == ("S/x", [])
    assert expand_uri("${KICAD8_FOOTPRINT_DIR}/x", env, major=10) == ("F/x", [])
    assert expand_uri("${KISYS3DMOD}/x", env, major=10) == ("M/x", [])
    assert expand_uri("${KICAD_SYMBOL_DIR}/x", env, major=10) == ("S/x", [])
    assert expand_uri("${KICAD7_3RD_PARTY}/x", env, major=10) == ("T/x", [])
    # an explicitly defined older variable wins over the remap
    env["KICAD9_SYMBOL_DIR"] = "S9"
    assert expand_uri("${KICAD9_SYMBOL_DIR}/x", env, major=10) == ("S9/x", [])


def test_library_env_fills_install_defaults_without_clobbering_overrides(tmp_path: Path):
    env = library_env(StubInstall(), {"KICAD10_SYMBOL_DIR": "user"}, tmp_path, extra={"MYVAR": "1"})
    assert env["KICAD10_SYMBOL_DIR"] == "user"
    assert env["KICAD10_FOOTPRINT_DIR"] == str(FIXTURES / "footprints")
    assert env["KIPRJMOD"] == str(tmp_path) and env["MYVAR"] == "1"


# ---- parsing ---------------------------------------------------------------------------------------


def test_parse_table_rows_flags_and_bare_values():
    rows = parse_table(
        "(sym_lib_table (version 7)\n"
        '  (lib (name "A") (type KiCad) (uri "${X}/a.kicad_sym") (options "") (descr "d") (hidden))\n'
        '  (lib (name "B") (type "KiCad") (uri x/b.kicad_sym) (disabled yes))\n'
        '  (lib (name "C") (type "KiCad") (uri "c") (hidden no))\n'
        ")"
    )
    assert [(r.name, r.type, r.uri, r.hidden, r.disabled) for r in rows] == [
        ("A", "KiCad", "${X}/a.kicad_sym", True, False),
        ("B", "KiCad", "x/b.kicad_sym", False, True),
        ("C", "KiCad", "c", False, False),
    ]
    with pytest.raises(ValueError):
        parse_table("(kicad_sch (version 1))")
    with pytest.raises(ValueError):
        parse_table("(sym_lib_table (version 7")


# ---- resolution ----------------------------------------------------------------------------------


def test_load_tables_global_nested_hidden_disabled_and_errors():
    env = env_for(FIXTURES)
    problems: list[str] = []
    tables = load_tables(StubInstall(), global_dir=FIXTURES, project_dir=None, env=env, problems=problems)
    by = {r.name: r for r in tables.sym}
    assert list(by) == ["Test", "Hidden", "Off", "Old", "Gone", "Unset", "Inner"]
    assert (
        by["Test"].usable and by["Test"].path == FIXTURES / "Test.kicad_sym" and by["Test"].scope == "global"
    )
    assert by["Hidden"].hidden and by["Hidden"].usable
    assert by["Off"].disabled and not by["Off"].usable
    assert not by["Old"].is_kicad and "Legacy" in (by["Old"].error or "")
    assert by["Gone"].path is not None and not by["Gone"].exists and by["Gone"].error == "path does not exist"
    assert by["Unset"].path is None and "NO_SUCH_VAR_KIWRAP" in (by["Unset"].error or "")
    # nested table: URI relative to the parent table's directory; its rows inline; the cycle row is skipped
    assert by["Inner"].usable and by["Inner"].path == FIXTURES / "Test.kicad_sym"
    assert by["Inner"].source == FIXTURES / "nested" / "sym-lib-table"
    assert "NestedOff" not in by and "Nested" not in by and "Deeper" not in by
    assert set(tables.sources) == {
        FIXTURES / "sym-lib-table",
        FIXTURES / "nested" / "sym-lib-table",
        FIXTURES / "fp-lib-table",
    }
    fp = {r.name: r for r in tables.fp}
    assert fp["Test"].usable and fp["Test"].path == FIXTURES / "Test.pretty"
    # KICAD9_FOOTPRINT_DIR remapped onto the install's KICAD10_FOOTPRINT_DIR (a fixture path: not on disk)
    assert fp["Legacy9"].path == FIXTURES / "footprints" / "Resistor_SMD.pretty" and not fp["Legacy9"].exists
    assert fp["Tilde"].path == Path.home() / "no-such-dir.pretty"
    assert problems == []


def test_project_rows_shadow_global_rows_and_text_variables_expand():
    project = FIXTURES / "project"
    resolver = LibResolver(StubInstall(), FIXTURES, project, env=env_for(project))
    assert resolver.env()["KIPRJMOD"] == str(project) and resolver.env()["MYLIBS"] == ".."
    tables = resolver.library_tables()
    by = {r.name: r for r in tables.sym}
    assert list(by)[:2] == ["Test", "Hidden"]  # order of first appearance is kept
    assert by["Hidden"].scope == "project" and not by["Hidden"].hidden  # the project row won
    assert by["Proj"].scope == "project" and by["Proj"].usable
    assert by["TextVar"].usable and by["TextVar"].path == FIXTURES / "Test.kicad_sym"
    assert {r.name for r in tables.fp if r.scope == "project"} == {"ProjFp"}
    dict_form = resolver.tables()
    assert set(dict_form) == {"sym", "fp"} and dict_form["sym"][0]["name"] == "Test"
    assert dict_form["sym"][0]["path"] == str(FIXTURES / "Test.kicad_sym")


def test_config_root_like_object_and_template_fallback(tmp_path: Path):
    class Root:
        versioned = FIXTURES

        def env(self, base=None):
            return {"KICAD10_SYMBOL_DIR": "from-sandbox"}

    resolver = LibResolver(StubInstall(), Root(), None)
    assert resolver.env()["KICAD10_SYMBOL_DIR"] == "from-sandbox"
    assert resolver.env()["KICAD10_FOOTPRINT_DIR"] == str(FIXTURES / "footprints")
    assert [r.name for r in resolver.rows("sym")][:1] == ["Test"]
    # no global dir and no template dir -> empty tables, no crash
    empty = LibResolver(StubInstall(root=tmp_path, share=tmp_path), None, None, env={})
    assert empty.library_tables().sym == () and empty.library_tables().fp == ()


def test_resolver_row_errors_are_teaching_errors():
    resolver = LibResolver(StubInstall(), FIXTURES, None, env=env_for(FIXTURES))
    with pytest.raises(NotFound) as e:
        resolver.symbol("Nope:X")
    assert e.value.code == "E_LIB_NOT_FOUND" and "sym-lib-table" in e.value.message
    with pytest.raises(NotFound) as e2:
        resolver.symbol("Tes:X")
    assert "Test" in e2.value.candidates
    with pytest.raises(UnsupportedLibrary) as e3:
        resolver.symbol("Off:Base")
    assert "disabled" in e3.value.message
    with pytest.raises(UnsupportedLibrary) as e4:
        resolver.symbol("Old:Base")
    assert "Legacy" in e4.value.message and e4.value.code == "E_LIB_UNSUPPORTED"
    with pytest.raises(NotFound) as e5:
        resolver.symbol("Gone:Base")
    assert "does not exist" in e5.value.message
    with pytest.raises(NotFound) as e6:
        resolver.symbol("Unset:Base")
    assert "NO_SUCH_VAR_KIWRAP" in e6.value.why
    with pytest.raises(NotFound):
        resolver.symbol("no-colon")
    with pytest.raises(NotFound):
        resolver.footprint("Test:Missing")


def test_tables_cache_invalidates_when_a_table_file_changes(tmp_path: Path):
    (tmp_path / "sym-lib-table").write_text(
        '(sym_lib_table (version 7) (lib (name "A") (type "KiCad") (uri "${KIPRJMOD}/Test.kicad_sym") (options "") (descr "")))\n',
        encoding="utf-8",
    )
    (tmp_path / "fp-lib-table").write_text("(fp_lib_table (version 7))\n", encoding="utf-8")
    resolver = LibResolver(StubInstall(), tmp_path, None, env=env_for(FIXTURES))
    assert [r.name for r in resolver.rows("sym")] == ["A"]
    digest1 = resolver.tables_digest()
    import os
    import time

    (tmp_path / "sym-lib-table").write_text(
        '(sym_lib_table (version 7) (lib (name "B") (type "KiCad") (uri "${KIPRJMOD}/Test.kicad_sym") (options "") (descr "")))\n',
        encoding="utf-8",
    )
    future = time.time() + 5
    os.utime(tmp_path / "sym-lib-table", (future, future))
    assert [r.name for r in resolver.rows("sym")] == ["B"]
    assert resolver.tables_digest() != digest1
