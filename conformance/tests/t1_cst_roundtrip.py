"""T1 — CST round trip (docs/ARCHITECTURE.md §6 certification, kiwrap.formats.sexpr contract).

For every hand-written corpus file and every text-format file of every installed KiCad's demo
projects: `dumps(loads(text)) == text` byte for byte (CRLF files included). Then two edit
properties on the same files:

* change one atom through the API and dump: the unified diff touches exactly that one line;
* build a sibling with `build()`, insert it, dump, re-parse: the new node is found, the tree still
  parses, and removing it again restores the original bytes.

Installed KiCads are found by globbing `C:/Program Files/KiCad/*/share/kicad` (and the usual
Linux share roots) directly, on purpose — this test must not depend on `kiwrap.installation`.
Run with `-s` (or `-rP`) to see the per-version file counts; they are also attached as
`record_property("files", …)`.

Set KIWRAP_T1_PROJECTS=named to restrict the demo walk to the seven projects named in
`conformance/corpus/demos.DEMO_PROJECTS` (default: every project in `demos/`).
"""

from __future__ import annotations

import importlib.util
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from kiwrap.formats.sexpr import Atom, Node, build, dumps, loads

HERE = Path(__file__).resolve().parent
CORPUS = HERE.parent / "corpus"


def _load_demos_module():
    """Import conformance/corpus/demos.py whether or not `conformance` is importable as a package."""
    try:
        from conformance.corpus import demos  # type: ignore[import-not-found]

        return demos
    except ImportError:
        pass
    spec = importlib.util.spec_from_file_location("kiwrap_corpus_demos", CORPUS / "demos.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


demos = _load_demos_module()

SHARE_GLOBS = (
    "C:/Program Files/KiCad/*/share/kicad",
    "/usr/share/kicad",
    "/usr/local/share/kicad",
    "/var/lib/flatpak/app/org.kicad.KiCad/current/active/files/share/kicad",
)


def _installed_share_dirs() -> dict[str, Path]:
    """`{label: share_dir}` for every KiCad share directory found on this machine."""
    found: dict[str, Path] = {}
    for pattern in SHARE_GLOBS:
        root = Path(pattern.split("*")[0])
        if "*" in pattern:
            if root.is_dir():
                for cand in sorted(root.iterdir()):
                    share = cand / "share" / "kicad"
                    if (share / "demos").is_dir():
                        found[cand.name] = share
        elif (root / "demos").is_dir():
            found[pattern] = root
    return found


@dataclass(frozen=True)
class Sample:
    label: str
    path: Path

    def __str__(self) -> str:
        return f"{self.label}:{self.path.name}"


def _read(path: Path) -> str:
    with path.open(encoding="utf-8", newline="") as fh:
        return fh.read()


def corpus_samples() -> list[Sample]:
    return [
        Sample(p.relative_to(CORPUS).parts[0], p)
        for p in sorted(CORPUS.rglob("*"))
        if p.is_file() and demos.is_text_format(p)
    ]


def demo_samples(label: str, share: Path) -> list[Sample]:
    all_projects = os.environ.get("KIWRAP_T1_PROJECTS", "all") != "named"
    return [Sample(label, f.path) for f in demos.collect_demo_files(share, all_projects=all_projects)]


def _version_key(label: str) -> tuple:
    parts = label.split(".")
    return tuple(int(x) if x.isdigit() else 9999 for x in parts), label


SHARES = _installed_share_dirs()
SHARE_LABELS = sorted(SHARES, key=_version_key)
CORPUS_SAMPLES = corpus_samples()


# ----------------------------------------------------------------------------------------------
# round trip
# ----------------------------------------------------------------------------------------------


def _roundtrip_failures(samples: list[Sample]) -> list[str]:
    failures: list[str] = []
    for s in samples:
        text = _read(s.path)
        try:
            out = dumps(loads(text))
        except Exception as exc:  # noqa: BLE001 - any failure is a T1 failure, report it
            failures.append(f"{s.path}: {type(exc).__name__}: {exc}")
            continue
        if out != text:
            first = next(
                (i for i, (a, b) in enumerate(zip(out, text, strict=False)) if a != b),
                min(len(out), len(text)),
            )
            failures.append(
                f"{s.path}: differs at offset {first}: {out[first : first + 40]!r} != {text[first : first + 40]!r}"
            )
    return failures


def test_corpus_roundtrip(record_property):
    assert CORPUS_SAMPLES, "conformance/corpus is empty"
    failures = _roundtrip_failures(CORPUS_SAMPLES)
    by_version: dict[str, int] = {}
    for s in CORPUS_SAMPLES:
        by_version[s.label] = by_version.get(s.label, 0) + 1
    record_property("files", by_version)
    print(f"\nT1 corpus: {len(CORPUS_SAMPLES)} files {by_version}, {len(failures)} failures")
    assert not failures, "\n".join(failures)


@pytest.mark.kicad  # needs an installed KiCad's share/kicad/demos (the corpus test above does not)
@pytest.mark.parametrize("label", SHARE_LABELS or ["<none>"])
def test_demo_roundtrip(label, record_property):
    if label not in SHARES:
        pytest.skip("no KiCad share/kicad/demos directory found")
    samples = demo_samples(label, SHARES[label])
    assert samples, f"no demo files under {SHARES[label]}"
    skipped = [
        f.path
        for f in demos.collect_demo_files(SHARES[label], all_projects=True, include_malformed=True)
        if demos.is_known_malformed(SHARES[label] / "demos", f.path)
    ]
    failures = _roundtrip_failures(samples)
    kinds: dict[str, int] = {}
    for s in samples:
        k = s.path.name if s.path.name in demos.TABLE_NAMES else s.path.suffix
        kinds[k] = kinds.get(k, 0) + 1
    size = sum(s.path.stat().st_size for s in samples)
    record_property("files", len(samples))
    record_property("known_malformed_skipped", len(skipped))
    print(
        f"\nT1 demos {label}: {len(samples)} files ({size / 1e6:.1f} MB) {kinds}, "
        f"{len(failures)} failures, {len(skipped)} known-malformed skipped"
    )
    assert not failures, f"{len(failures)}/{len(samples)} failed:\n" + "\n".join(failures[:20])


# ----------------------------------------------------------------------------------------------
# edit properties
# ----------------------------------------------------------------------------------------------


def _first_editable_atom(root: Node) -> Atom | None:
    """A scalar argument somewhere in the tree whose line we can change: prefer a uuid/tstamp."""
    for head in ("uuid", "tstamp", "version", "generator"):
        for node in root.walk():
            if node.head == head and node.atoms:
                return node.atoms[0]
    for node in root.walk():
        for a in node.atoms:
            if a.kind in ("string", "number"):
                return a
    return None


def _changed_lines(before: str, after: str) -> list[int]:
    a, b = before.split("\n"), after.split("\n")
    if len(a) != len(b):
        return [-1]
    return [i for i, (x, y) in enumerate(zip(a, b, strict=True)) if x != y]


def _edit_property_samples() -> list[Sample]:
    """Corpus files plus the first few demo files of every install (keeps the run short)."""
    out = list(CORPUS_SAMPLES)
    for label in SHARE_LABELS:
        out.extend(demo_samples(label, SHARES[label])[:8])
    return out


@pytest.mark.parametrize("sample", _edit_property_samples(), ids=str)
def test_single_atom_edit_touches_one_line(sample):
    text = _read(sample.path)
    root = loads(text)
    atom = _first_editable_atom(root)
    if atom is None:
        pytest.skip("no scalar to edit")
    original_raw = atom.raw
    if atom.kind == "number":
        atom.value = atom.as_float() + 1 if "." in atom.raw else atom.as_int() + 1
    elif atom.kind == "string":
        atom.value = atom.value + "-edited"
    else:
        atom.value = atom.value + "_edited"
    assert atom.raw != original_raw
    after = dumps(root)
    changed = _changed_lines(text, after)
    assert len(changed) == 1 and changed != [-1], f"edit touched lines {changed}"
    # and it is genuinely the atom's line
    assert atom.raw in after.split("\n")[changed[0]]
    # the change is reversible byte for byte
    atom.raw = original_raw
    assert dumps(root) == text


@pytest.mark.parametrize("sample", _edit_property_samples(), ids=str)
def test_build_sibling_insert_reparse(sample):
    text = _read(sample.path)
    root = loads(text)
    target = root if not root.implicit else next((c for c in root.children if isinstance(c, Node)), root)
    sibling = build("kiwrap_t1_marker", 42, 'text with (parens) and "quotes"', build("at", 1.5, -2, 90))
    target.append(sibling)
    edited = dumps(root)
    reparsed = loads(edited)
    marker = next((n for n in reparsed.walk() if n.head == "kiwrap_t1_marker"), None)
    assert marker is not None
    assert marker.values() == ["42", 'text with (parens) and "quotes"']
    assert marker.find("at") is not None and marker.find("at").values() == ["1.5", "-2", "90"]
    # everything else survived: removing the sibling restores the original bytes
    target.remove(sibling)
    assert dumps(root) == text
    # and the re-parsed tree has exactly the two new lists (marker + its (at …))
    assert sum(1 for _ in reparsed.walk()) == sum(1 for _ in root.walk()) + 2
