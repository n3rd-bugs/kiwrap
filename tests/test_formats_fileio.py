"""`kiwrap.formats.fileio.atomic_write_text`: design files are replaced whole, never truncated in
place (a kill mid-write must leave the previous complete file)."""

from __future__ import annotations

from pathlib import Path

import pytest

from kiwrap.formats import fileio
from kiwrap.formats.sexpr import dump, loads


def test_atomic_write_replaces_whole_and_leaves_no_temp(tmp_path: Path):
    target = tmp_path / "a.kicad_sch"
    fileio.atomic_write_text(target, "(kicad_sch\r\n)\r\n", newline="")
    assert target.read_bytes() == b"(kicad_sch\r\n)\r\n"  # the caller's line endings, untouched
    fileio.atomic_write_text(target, "(kicad_sch (version 2))\n")
    assert target.read_text(encoding="utf-8") == "(kicad_sch (version 2))\n"
    assert [p.name for p in tmp_path.iterdir()] == ["a.kicad_sch"]


def test_failed_write_keeps_the_previous_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    target = tmp_path / "b.kicad_pcb"
    target.write_text("(kicad_pcb (version 1))\n", encoding="utf-8")

    class Boom(OSError):
        pass

    def failing_fsync(fd: int) -> None:
        raise Boom("disk gone")

    monkeypatch.setattr(fileio.os, "fsync", failing_fsync)
    with pytest.raises(Boom):
        fileio.atomic_write_text(target, "(kicad_pcb (version 2))\n")
    assert target.read_text(encoding="utf-8") == "(kicad_pcb (version 1))\n"
    assert not list(tmp_path.glob("*" + fileio.TMP_SUFFIX))


def test_sexpr_dump_goes_through_the_atomic_writer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    seen: list[Path] = []
    real = fileio.atomic_write_text

    def spy(path, text, **kw):
        seen.append(Path(path))
        return real(path, text, **kw)

    monkeypatch.setattr(fileio, "atomic_write_text", spy)
    node = loads("(kicad_sch\n\t(version 1)\n)\n")
    dump(node, tmp_path / "c.kicad_sch")
    assert seen == [tmp_path / "c.kicad_sch"]
    assert (tmp_path / "c.kicad_sch").read_text(encoding="utf-8") == "(kicad_sch\n\t(version 1)\n)\n"
