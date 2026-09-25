"""`sch erc` adapter: runs kicad-cli ERC with `--format json` and normalises the result.

Verified facts encoded here (docs/VERIFICATION.md):
- claim 1: on kicad-cli 8.0.1 / 9.0.5 / 10.0.0 (and still on 10.0.6 and the 10.99 nightly
  10.99.0-4083-g6dd893f7d1, re-verified 2026-09-25 against item anchors in demo schematics) ERC
  JSON item positions are exactly 100x too small (the JSON path uses pcbIUScale for schematic
  IUs), for `--units mm` and `--units in` alike, and the item `description` strings carry the same
  error ("length 0.2032 mm" for a 20.32 mm wire). DRC JSON is correct. The factor is the
  capability `cli.erc_json_pos_scale`; it is handed to `kiwrap.formats.reports.erc.parse_erc_json`,
  which multiplies positions AND the "N.NNNN mm" dimensions inside descriptions (never the "2.54mm"
  of a library name) so `Report` consumers only ever see millimetres.
- Upstream fixed it (work item #25582): master 407f6293 and its 10.0 cherry-pick 6d8e1fea (both
  2026-09-21, milestone 10.0.7) switch `ERC_REPORT::WriteJsonReport`'s UNITS_PROVIDER from
  pcbIUScale to schIUScale. The matrix records scale 1 for 10.0.7 and 11.0.0, but a 10.0 branch
  build between the 10.0.6 tag and the 10.0.7 bump still calls itself 10.0.6 and a nightly built
  after 407f6293 still calls itself 10.99.0, so their seeded record says 100. `run_erc` therefore
  checks the scale on every run (D27 follow-up / plan step S1 in docs/decisions/D24-D31.md):
  `check_erc_pos_scale` compares the unscaled item positions, times each known scale, with the
  anchor of the item their uuid names in the schematic (pin end, wire segment, label, sheet
  outline ...), falling back to the page bounds; when the record is contradicted the run uses the
  plausible scale and `Report.raw["_cli"]` carries `degraded: ["erc_pos_scale_corrected"]` plus
  the evidence, instead of emitting positions 100x off. `probe_erc_pos_scale` is the explicit
  fixture probe `scripts/ci/refresh_matrix.py` runs on every nightly bump (D16).
- matrix row "`sch erc --format json`": absent on 7.0.6 -> `Unsupported(oldest='8.0.0')`.
- `--exit-code-violations` -> exit 5 means "violations found", not failure (`--severity-all`
  makes warnings count too); the exit code is mapped into the `Report` and never raised.
- ERC type names differ across versions (8: `label_dangling`; 9: `global_label_dangling`;
  10: `label_dangling` + `unconnected_wire_endpoint`), so nothing here matches on them.
"""

from __future__ import annotations

import json
import math
import shutil
import statistics
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kiwrap.backends.cli.runner import CliResult, resolve_caps, run_kicad_cli
from kiwrap.capability import Capabilities, Unsupported
from kiwrap.formats.reports import Report
from kiwrap.formats.reports.erc import parse_erc_json, parse_erc_text
from kiwrap.formats.schematic import SchematicFile
from kiwrap.formats.sexpr import Node
from kiwrap.installation import KiCadInstallation
from kiwrap.sandbox import ConfigRoot

#: kicad-cli's first release with `sch erc` / `pcb drc` (matrix §2; 7.0.6 has neither).
OLDEST_KICAD_WITH_ERC = "8.0.0"
_SEVERITY_FLAGS: Mapping[str, str] = {
    "all": "--severity-all",
    "error": "--severity-error",
    "warning": "--severity-warning",
    "exclusion": "--severity-exclusions",
    "exclusions": "--severity-exclusions",
}

#: The capability key the scale lives in (docs/VERIFICATION.md claim 1).
POS_SCALE_KEY = "cli.erc_json_pos_scale"
#: Every value kicad-cli has shipped: 100 on 8.0.1-10.0.6 and nightlies before master 407f6293,
#: 1 from 10.0.7 (6d8e1fea) / master 407f6293 on.
KNOWN_ERC_POS_SCALES: tuple[float, ...] = (1.0, 100.0)
#: The degraded tag a run carries when the record was contradicted and the run used another scale.
DEGRADED_POS_SCALE_CORRECTED = "erc_pos_scale_corrected"
#: An item's scaled position must lie this close (mm) to the anchor its uuid names. A pin, label,
#: junction or symbol origin is hit exactly by the right scale; the wrong one misses by 99 % of
#: the coordinate, so only items within ~0.5 mm of the sheet origin are ambiguous (and skipped).
ANCHOR_TOL_MM = 0.5
#: At most this many ERC items are checked (the verdict needs a handful; big reports repeat items).
MAX_CHECKED_ITEMS = 64
#: Sheet files read while looking for anchors (stops as soon as every wanted uuid is found).
MAX_EVIDENCE_FILES = 64
#: Page fallback: an item may sit off the drawing sheet, but not this many page sizes away ...
PAGE_SLACK = 3.0
#: ... and a schematic whose EVERY item lies within this fraction of the page size from the origin
#: (A4: 3.7 mm, inside the drawing-sheet frame) is what a 100x-too-small report looks like.
PAGE_TINY_FRACTION = 0.0125

#: `(paper "<name>")` sizes in mm (landscape; KiCad's PAGE_INFO table). "User" carries its own.
PAPER_SIZES_MM: Mapping[str, tuple[float, float]] = {
    "A5": (210.0, 148.0),
    "A4": (297.0, 210.0),
    "A3": (420.0, 297.0),
    "A2": (594.0, 420.0),
    "A1": (841.0, 594.0),
    "A0": (1189.0, 841.0),
    "A": (279.4, 215.9),
    "B": (431.8, 279.4),
    "C": (558.8, 431.8),
    "D": (863.6, 558.8),
    "E": (1117.6, 863.6),
    "USLETTER": (279.4, 215.9),
    "USLEGAL": (355.6, 215.9),
    "USLEDGER": (431.8, 279.4),
}


def severity_flags(severity: str) -> list[str]:
    """Map "all" | "error" | "warning" | "exclusions" | "error,warning" to kicad-cli flags."""
    flags: list[str] = []
    for part in severity.replace("+", ",").split(","):
        key = part.strip().lower()
        if not key:
            continue
        if key not in _SEVERITY_FLAGS:
            raise ValueError(f"unknown severity {part!r}; expected one of {sorted(_SEVERITY_FLAGS)}")
        flag = _SEVERITY_FLAGS[key]
        if flag not in flags:
            flags.append(flag)
    return flags or ["--severity-all"]


def report_from_erc_json(doc: dict[str, Any], *, source: str, position_scale: float) -> Report:
    """Normalise an erc.v1 document (already parsed) into a `Report` with positions in mm.

    `position_scale` is `cli.erc_json_pos_scale` (100 on the affected releases, 1 once fixed) or
    the scale `check_erc_pos_scale` settled on. The quirk is undone by
    `kiwrap.formats.reports.erc.parse_erc_json`, which rescales the document once (so `Report.raw`
    is also correct) before parsing it; there is deliberately no second rescaler in the backend.
    """
    return parse_erc_json(json.dumps(doc), position_scale=position_scale, source=source)


# --- the scale plausibility check (S1, D27 follow-up) ------------------------------------------------


@dataclass(frozen=True)
class Anchor:
    """Where a schematic item lies, in mm: a point (both ends equal) or one segment of a wire,
    bus or sheet outline."""

    x0: float
    y0: float
    x1: float
    y1: float

    @classmethod
    def point(cls, x: float, y: float) -> Anchor:
        return cls(x, y, x, y)

    def distance(self, x: float, y: float) -> float:
        """Distance from (x, y) to the point / segment."""
        dx, dy = self.x1 - self.x0, self.y1 - self.y0
        length2 = dx * dx + dy * dy
        t = 0.0 if length2 == 0 else max(0.0, min(1.0, ((x - self.x0) * dx + (y - self.y0) * dy) / length2))
        return math.hypot(x - (self.x0 + t * dx), y - (self.y0 + t * dy))


@dataclass(frozen=True)
class SchematicEvidence:
    """What the schematic says about where ERC items can be: `anchors` by item uuid and the
    largest drawing sheet (`page_mm`, (w, h)) among the files read."""

    anchors: Mapping[str, tuple[Anchor, ...]] = field(default_factory=dict)
    page_mm: tuple[float, float] | None = None
    files: int = 0


@dataclass(frozen=True)
class ErcScaleCheck:
    """The outcome of `check_erc_pos_scale` for one ERC report.

    `evidence` is "anchor" (item positions matched the items their uuids name), "page" (only the
    drawing-sheet bounds decided), "none" (nothing decisive: the record stands) or "no_items" (no
    positioned item to check). `votes` counts, per candidate scale, the items that matched an
    anchor under that scale and no other.
    """

    recorded: float
    scale: float
    evidence: str
    votes: tuple[tuple[float, int], ...] = ()
    checked: int = 0

    @property
    def corrected(self) -> bool:
        return self.scale != self.recorded

    def warning(self) -> str:
        return (
            f"{POS_SCALE_KEY} is {_fmt(self.recorded)} for this KiCad but its ERC JSON positions fit "
            f"{_fmt(self.scale)} ({self.evidence} evidence); this run used {_fmt(self.scale)}. "
            "Re-run scripts/ci/refresh_matrix.py for this install and fix kicad_version_matrix.json."
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "key": POS_SCALE_KEY,
            "recorded": self.recorded,
            "used": self.scale,
            "corrected": self.corrected,
            "evidence": self.evidence,
            "votes": {_fmt(s): n for s, n in self.votes},
            "checked": self.checked,
        }


def _fmt(scale: float) -> str:
    return f"{scale:g}"


def erc_item_points(unscaled: Report, limit: int = MAX_CHECKED_ITEMS) -> list[tuple[str, float, float]]:
    """(uuid, x_mm, y_mm) of the positioned items of a report parsed with `position_scale=1`
    (`coordinate_units` already converted to mm), de-duplicated, at most `limit`."""
    seen: set[tuple[str, float, float]] = set()
    out: list[tuple[str, float, float]] = []
    for violation in unscaled.violations:
        for item in violation.items:
            if item.pos is None:
                continue
            point = (item.uuid or "", float(item.pos.x_mm), float(item.pos.y_mm))
            if point in seen:
                continue
            seen.add(point)
            out.append(point)
            if len(out) >= limit:
                return out
    return out


def paper_size_mm(node: Node | None) -> tuple[float, float] | None:
    """(w, h) in mm of a `(paper "A4" [portrait])` / `(paper "User" W H)` node, None if unknown."""
    if node is None:
        return None
    atoms = node.atoms
    if not atoms:
        return None
    name = atoms[0].value.strip().upper()
    size: tuple[float, float] | None
    if name == "USER":
        try:
            size = (atoms[1].as_float(), atoms[2].as_float())
        except (IndexError, ValueError):
            return None
    else:
        size = PAPER_SIZES_MM.get(name)
    if size is None:
        return None
    if any(a.value == "portrait" for a in atoms[1:]):
        size = (size[1], size[0])
    return size


def _node_anchors(node: Node) -> list[Anchor]:
    """Generic anchors of one top-level schematic item: its `(at)`, the segments of its
    `(pts (xy)...)` and, for an item with a `(size)` (sheet, text box, bus entry), its outline."""
    out: list[Anchor] = []
    at = node.find("at")
    x = y = None
    if at is not None and len(at.atoms) >= 2:
        x, y = at.atoms[0].as_float(), at.atoms[1].as_float()
        out.append(Anchor.point(x, y))
    pts = node.find("pts")
    if pts is not None:
        xy = [(a.atoms[0].as_float(), a.atoms[1].as_float()) for a in pts.find_all("xy") if len(a.atoms) >= 2]
        out.extend(Anchor(*p0, *p1) for p0, p1 in zip(xy, xy[1:], strict=False))
        if len(xy) == 1:
            out.append(Anchor.point(*xy[0]))
    size = node.find("size")
    if x is not None and y is not None and size is not None and len(size.atoms) >= 2:
        w, h = size.atoms[0].as_float(), size.atoms[1].as_float()
        corners = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
        out.extend(Anchor(*a, *b) for a, b in zip(corners, corners[1:] + corners[:1], strict=False))
    return out


def _uuid_of(node: Node) -> str:
    return node.get("uuid") or node.get("tstamp") or ""


def _file_anchors(sch: SchematicFile, anchors: dict[str, list[Anchor]]) -> None:
    for node in sch.root.nodes:
        uid = _uuid_of(node)
        if uid:
            anchors.setdefault(uid, []).extend(_node_anchors(node))
        if node.head == "sheet":
            for sheet_pin in node.find_all("pin"):
                pin_uid = _uuid_of(sheet_pin)
                if pin_uid:
                    anchors.setdefault(pin_uid, []).extend(_node_anchors(sheet_pin))
    for symbol in sch.symbols:
        for pin in symbol.pins:
            if pin.uuid:
                anchors.setdefault(pin.uuid, []).append(Anchor.point(pin.position.x, pin.position.y))
                # an ERC item for a pin may also carry the symbol's uuid
                anchors.setdefault(symbol.uuid, []).append(Anchor.point(pin.position.x, pin.position.y))


def schematic_evidence(
    root: Path, want: Iterable[str] = (), *, max_files: int = MAX_EVIDENCE_FILES
) -> SchematicEvidence:
    """Anchors (by uuid) and the page size from `root` and its sheet files, read breadth-first
    and stopping as soon as every uuid in `want` has an anchor. A file that cannot be read or
    parsed is skipped: the check must never make ERC fail."""
    wanted = {u for u in want if u}
    anchors: dict[str, list[Anchor]] = {}
    page: tuple[float, float] | None = None
    queue: list[Path] = [Path(root)]
    seen: set[Path] = set()
    files = 0
    while queue and files < max_files:
        path = queue.pop(0)
        key = path.resolve() if path.exists() else path
        if key in seen:
            continue
        seen.add(key)
        try:
            sch = SchematicFile.load(path)
        except Exception:  # noqa: BLE001 - evidence is best-effort; unreadable sheets are skipped
            continue
        files += 1
        _file_anchors(sch, anchors)
        size = paper_size_mm(sch.root.find("paper"))
        if size is not None and (page is None or max(size) > max(page)):
            page = size
        for sheet in sch.sheets:
            if sheet.file:
                queue.append(path.parent / sheet.file)
        if wanted and wanted <= anchors.keys():
            break
    return SchematicEvidence({k: tuple(v) for k, v in anchors.items()}, page, files)


def _page_verdict(
    points: list[tuple[str, float, float]],
    candidates: list[float],
    recorded: float,
    page: tuple[float, float],
) -> float | None:
    """The scale the drawing-sheet bounds single out, or None when they do not decide."""
    side = max(page)
    reach = [max(abs(x), abs(y)) for _, x, y in points]
    if not reach or max(reach) == 0:
        return None

    def plausible(s: float) -> bool:
        far = max(r * s for r in reach)
        return far <= PAGE_SLACK * side and far > PAGE_TINY_FRACTION * side

    ok = [s for s in candidates if plausible(s)]
    if recorded in ok:
        return recorded
    return ok[0] if len(ok) == 1 else None


def check_erc_pos_scale(
    points: list[tuple[str, float, float]],
    recorded: float,
    evidence: SchematicEvidence,
    *,
    candidates: Iterable[float] = KNOWN_ERC_POS_SCALES,
) -> ErcScaleCheck:
    """Decide which scale the ERC positions `points` (unscaled mm, from `erc_item_points`) need.

    1. Anchors: for every item whose uuid names a schematic item, each candidate scale either puts
       it within ANCHOR_TOL_MM of that item or not; an item that only one candidate fits is a vote
       for it. The candidate with the most votes wins; a tie keeps `recorded`.
    2. No vote at all: the drawing-sheet bounds. A scale is implausible when it throws an item more
       than PAGE_SLACK page sizes away, or squeezes EVERY item into the first PAGE_TINY_FRACTION of
       the page (the shape of a 100x-too-small report); when exactly one candidate survives and the
       record does not, that candidate wins.
    3. Otherwise the record stands (evidence "none").
    Pure: no I/O. Encodes docs/VERIFICATION.md claim 1 and the upstream fix 407f6293 / 6d8e1fea.
    """
    recorded = float(recorded)
    cands = sorted({recorded, *(float(c) for c in candidates)})
    if not points:
        return ErcScaleCheck(recorded, recorded, "no_items")
    votes = dict.fromkeys(cands, 0)
    checked = 0
    for uid, x, y in points:
        anchors = evidence.anchors.get(uid) if uid else None
        if not anchors:
            continue
        checked += 1
        fits = [s for s in cands if min(a.distance(x * s, y * s) for a in anchors) <= ANCHOR_TOL_MM]
        if len(fits) == 1:
            votes[fits[0]] += 1
    tally = tuple((s, votes[s]) for s in cands)
    if any(votes.values()):
        best = max(cands, key=lambda s: (votes[s], s == recorded))
        scale = best if votes[best] > votes[recorded] else recorded
        return ErcScaleCheck(recorded, scale, "anchor", tally, checked)
    if evidence.page_mm is not None:
        verdict = _page_verdict(points, cands, recorded, evidence.page_mm)
        if verdict is not None:
            return ErcScaleCheck(recorded, verdict, "page", tally, checked)
    return ErcScaleCheck(recorded, recorded, "none", tally, checked)


def plausible_erc_pos_scale(
    doc: dict[str, Any],
    recorded: float,
    schematic: Path | None,
    *,
    evidence_for: Callable[[Path, set[str]], SchematicEvidence] = schematic_evidence,
) -> ErcScaleCheck:
    """`check_erc_pos_scale` for an erc.v1 document produced from `schematic` (the root sheet).

    The schematic is only read when the report has positioned items, and only as far as needed
    (`schematic_evidence` stops once every item uuid is anchored)."""
    points = erc_item_points(report_from_erc_json(doc, source="", position_scale=1.0))
    if not points:
        return ErcScaleCheck(float(recorded), float(recorded), "no_items")
    evidence = SchematicEvidence()
    if schematic is not None:
        try:
            evidence = evidence_for(Path(schematic), {u for u, _, _ in points})
        except Exception:  # noqa: BLE001 - the check must never make ERC fail
            evidence = SchematicEvidence()
    return check_erc_pos_scale(points, recorded, evidence)


# --- the fixture probe (scripts/ci/refresh_matrix.py, D16) --------------------------------------------


@dataclass(frozen=True)
class ErcScaleProbe:
    """`probe_erc_pos_scale`'s measurement: text-report position / JSON position (the report is in
    real units, claim 1), its median over every paired coordinate, and the known scale it snaps to
    (the rounded ratio when it matches none)."""

    scale: float
    ratio: float
    pairs: int
    fixture: str


def _snap(ratio: float) -> float:
    for known in KNOWN_ERC_POS_SCALES:
        if abs(ratio - known) <= 0.01 * known:
            return known
    return round(ratio, 4)


def probe_erc_pos_scale(
    install: KiCadInstallation,
    config: ConfigRoot,
    fixture: Path,
    work_dir: Path,
    *,
    caps: Capabilities | None = None,
    timeout_s: float = 300,
) -> ErcScaleProbe | None:
    """Measure `cli.erc_json_pos_scale` by RUNNING kicad-cli: `sch erc --format json` and
    `--format report` on a copy of `fixture` (in `work_dir`, never the original) and the ratio of
    the positions of the same items. None when the fixture yields no positioned pair."""
    caps = resolve_caps(install, caps)
    if not caps.has("cli.has_erc"):
        raise Unsupported("cli.has_erc", install, oldest_kicad_with_it=OLDEST_KICAD_WITH_ERC, fallback="none")
    work_dir.mkdir(parents=True, exist_ok=True)
    sch = work_dir / Path(fixture).name
    shutil.copy2(fixture, sch)
    out_json, out_rpt = work_dir / "probe.erc.json", work_dir / "probe.erc.rpt"
    base = ["sch", "erc", "--units", "mm", "--severity-all"]
    for fmt, out in (("json", out_json), ("report", out_rpt)):
        run_kicad_cli(
            install,
            [*base, "--format", fmt, "-o", str(out), str(sch)],
            config=config,
            caps=caps,
            timeout_s=timeout_s,
        )
    unscaled = parse_erc_json(out_json.read_text(encoding="utf-8"), position_scale=1.0)
    text = parse_erc_text(out_rpt.read_text(encoding="utf-8", errors="replace"))
    ratios: list[float] = []
    for jv, tv in zip(unscaled.violations, text.violations, strict=False):
        for ji, ti in zip(jv.items, tv.items, strict=False):
            if ji.pos is None or ti.pos is None:
                continue
            for j, t in ((ji.pos.x_mm, ti.pos.x_mm), (ji.pos.y_mm, ti.pos.y_mm)):
                if abs(j) > 1e-6 and abs(t) > 1e-6:
                    ratios.append(t / j)
    if not ratios:
        return None
    ratio = statistics.median(ratios)
    return ErcScaleProbe(_snap(ratio), ratio, len(ratios), str(fixture))


# --- the adapter ---------------------------------------------------------------------------------------


def run_erc(
    install: KiCadInstallation,
    config: ConfigRoot,
    schematic: Path,
    *,
    severity: str = "all",
    units: str = "mm",
    caps: Capabilities | None = None,
    out_json: Path | None = None,
    timeout_s: float = 300,
    define_vars: Mapping[str, str] | None = None,
    verify_scale: bool = True,
) -> Report:
    """Run `kicad-cli sch erc --format json --exit-code-violations` and return a `Report`.

    Positions in the returned report are millimetres regardless of `units` (the JSON is produced in
    `units`, then converted). Exit 5 (violations) is normal and shows up as `report.errors /
    report.warnings`; a schematic that cannot be loaded raises `CliLoadError`.

    `report.raw["_cli"]["pos_scale"]` records the scale check (`ErcScaleCheck.to_json`); when the
    capability record was contradicted (`verify_scale`, on by default) the run used the plausible
    scale and `report.raw["_cli"]` also carries `degraded: ["erc_pos_scale_corrected"]` and a
    `warnings` line, which the tools copy into the receipt.
    """
    caps = resolve_caps(install, caps)
    if not caps.has("cli.has_erc"):
        raise Unsupported(
            "cli.has_erc",
            install,
            oldest_kicad_with_it=OLDEST_KICAD_WITH_ERC,
            fallback="none (KiCad 7 has no headless ERC; open the schematic in eeschema)",
        )
    schematic = Path(schematic)
    out = Path(out_json) if out_json else schematic.with_name(schematic.stem + ".erc.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    args: list[str] = ["sch", "erc", "--format", "json", "--units", units, *severity_flags(severity)]
    args += ["--exit-code-violations", "-o", str(out)]
    for k, v in (define_vars or {}).items():
        args += ["-D", f"{k}={v}"]
    args.append(str(schematic))
    res: CliResult = run_kicad_cli(install, args, config=config, caps=caps, timeout_s=timeout_s)
    text = out.read_text(encoding="utf-8")
    doc = json.loads(text)
    recorded = float(caps.get(POS_SCALE_KEY, 1) or 1)
    check = (
        plausible_erc_pos_scale(doc, recorded, schematic)
        if verify_scale
        else ErcScaleCheck(recorded, recorded, "none")
    )
    report = report_from_erc_json(doc, source=str(schematic), position_scale=check.scale)
    cli: dict[str, Any] = {
        "returncode": res.returncode,
        "exit_code_violations": res.violations,
        "path": str(out),
        "pos_scale": check.to_json(),
    }
    if check.corrected:
        cli["degraded"] = [DEGRADED_POS_SCALE_CORRECTED]
        cli["warnings"] = [check.warning()]
    report.raw["_cli"] = cli
    return report
