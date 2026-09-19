"""`kicad-cli pcb export ipcd356` parser -> `TestPoints` (IPC-D-356A netlist: every pad and via
with its net, position, size, drill and access side).

Record layout as KiCad writes it (pcbnew/exporters/export_d356.cpp `IPC356D_WRITER`; verified on
9.0.9, 10.0.6 and the 10.99 nightly, scratchpad structured/out/*/board.d356; the subcommand does
not exist on 7.0/8.0):

    P  JOB   <board name>                      (nightly only)
    P  CODE  00
    P  UNITS CUST 0                            0 = inches, coordinates in 0.0001 in ("decimils")
    P  arrayDim   N
    P  VER   IPC-D-356A                        (nightly only)
    P  NNAMEM0000 unconnected-(P5-Pad1)        net-name alias table (nightly only): names longer
                                               than 14 chars get an alias used in the records
    317GND              C1    -2    D0394PA00X+055750Y-037281X0787Y0000R270 S0
    327M0001            JP1   -2          A02X+034085Y+022000X0118Y0118R000 S1
    317/CLOCK-RB6       VIA        MD0236PA00X+050250Y+017000X0630Y0000R000 S3
    367N/C              P101       MD1693UA00X+006000Y+007000X1693Y0000R000 S0
    999

    cols 1-3   record type: 317 through-hole pad, 327 SMD pad, 367 non-plated mechanical hole,
               307 blind/buried via (followed by an `027` continuation, not emitted by KiCad so
               far); 999 end of file
    cols 4-17  net name, 14 chars. 10.0 and older UPPER-CASE and truncate to the LAST 14 chars
               ("unconnected-(P5-Pad1)" -> "CTED-(P5-PAD1)"); the nightly emits an alias (M0000)
               declared in a `P  NNAME` line instead and keeps case. Unconnected pads: "N/C".
    cols 21-26 reference ("VIA" for vias), col 27 "-" when a pin follows, cols 28-31 pin number,
               col 32 "M" = mid-net point (vias, and pads without a pin number)
    cols 33-38 `D<dddd><P|U>` hole diameter in units and Plated/Unplated, or blanks
    cols 39-41 `A<nn>` access: 00 both sides, 01 top only, 02 bottom only, n = inner layer index
    cols 42-56 `X<+xxxxxx>Y<+yyyyyy>` location in units, relative to the AUX origin, Y UP
               (`origin.y - pad.y`); X is `pad.x - origin.x`
    cols 57-66 `X<wwww>Y<hhhh>` pad size (Y 0000 for round pads)
    cols 67-70 `R<rrr>` rotation (negated pad orientation, normalised 0-359)
    cols 72-74 ` S<n>` soldermask: 0 none, 1 top covered, 2 bottom covered, 3 both (the nightly
               separates it from the rotation with a space; 10.0 writes it flush: "R270S0")

Conventions: `x_mm`/`y_mm` are converted into KiCad's board frame (mm, y down) given
`aux_origin_mm` (the board's `(aux_axis_origin x y)`, default 0,0); `x_raw`/`y_raw` keep the
file's own y-up, origin-relative values in mm.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

__all__ = ["TestPoint", "TestPoints", "parse_ipcd356"]

_UNITS_MM: dict[str, float] = {"0": 0.00254, "1": 0.001, "2": 0.0000254}  # CUST 0 in/1e4, 1 mm/1e3, 2 in/1e6
_RECORD_RE = re.compile(
    r"^(?P<type>3[0-9]7)"
    r"(?P<net>.{14})"
    r".{3}"
    r"(?P<ref>.{6})"
    r"(?P<dash>.)"
    r"(?P<pin>.{4})"
    r"(?P<mid>.)"
    r"(?:D(?P<drill>\d{4})(?P<plating>[PU])|\s{6})"
    r"A(?P<access>\d{2})"
    r"X(?P<x>[-+]?\d{6})Y(?P<y>[-+]?\d{6})"
    r"(?:X(?P<w>\d{4})Y(?P<h>\d{4})R(?P<rot>-?\d{2,3}))?"  # `R%03d` of a negative angle prints "-90"
    r"\s*(?:S(?P<mask>\d))?"
    r"(?:\s*L(?P<start>\d{2})L(?P<end>\d{2}))?"
)
_NNAME_RE = re.compile(r"^P\s+NNAME(?P<alias>\S{1,5})\s+(?P<name>.+?)\s*$")
_UNITS_RE = re.compile(r"^P\s+UNITS\s+(?P<sys>\w+)\s+(?P<code>\d)")
_JOB_RE = re.compile(r"^P\s+JOB\s+(?P<job>.+?)\s*$")


@dataclass(frozen=True)
class TestPoint:
    record: str  # "317" | "327" | "367" | "307"
    net: str  # resolved net name ("N/C" when unconnected); as written when no alias matched
    ref: str  # refdes or "VIA"
    pin: str  # "" for vias
    midpoint: bool
    x_mm: float  # KiCad board frame
    y_mm: float
    x_raw_mm: float  # file frame: aux-origin relative, y up
    y_raw_mm: float
    width_mm: float | None
    height_mm: float | None
    rotation_deg: float
    drill_mm: float | None
    plated: bool | None  # None when no hole
    access: int  # 0 both sides, 1 top, 2 bottom, n inner
    soldermask: int  # 0 none, 1 top, 2 bottom, 3 both
    start_layer: int | None = None  # 307 records only
    end_layer: int | None = None

    @property
    def kind(self) -> str:
        if self.ref == "VIA":
            return "via"
        return {"317": "pad_th", "327": "pad_smd", "367": "hole_npth", "307": "via_bb"}.get(
            self.record, "unknown"
        )

    @property
    def is_via(self) -> bool:
        return self.ref == "VIA"


@dataclass(frozen=True)
class TestPoints:
    source: str
    job: str
    units_code: str  # "0" inches/1e4
    aliases: dict[str, str]  # alias -> full net name (nightly)
    points: tuple[TestPoint, ...]
    aux_origin_mm: tuple[float, float] = (0.0, 0.0)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def nets(self) -> dict[str, tuple[TestPoint, ...]]:
        out: dict[str, list[TestPoint]] = {}
        for p in self.points:
            out.setdefault(p.net, []).append(p)
        return {k: tuple(v) for k, v in out.items()}

    @property
    def pads(self) -> tuple[TestPoint, ...]:
        return tuple(p for p in self.points if not p.is_via and p.record != "367")

    @property
    def vias(self) -> tuple[TestPoint, ...]:
        return tuple(p for p in self.points if p.is_via)

    def of(self, ref: str) -> tuple[TestPoint, ...]:
        return tuple(p for p in self.points if p.ref == ref)


def _f(s: str | None) -> float | None:
    return None if s is None else float(s)


def parse_ipcd356(
    text: str, *, source: str = "", aux_origin_mm: tuple[float, float] = (0.0, 0.0)
) -> TestPoints:
    """Parse an IPC-D-356(A) file written by KiCad. `aux_origin_mm` is the board's
    `(aux_axis_origin)` so positions can be mapped back into the board frame."""
    aliases: dict[str, str] = {}
    points: list[TestPoint] = []
    job = ""
    units_code = "0"
    unit_mm = _UNITS_MM["0"]
    header: list[str] = []
    ox, oy = aux_origin_mm
    for raw_line in text.splitlines():
        line = raw_line.rstrip("\r\n")
        if not line.strip():
            continue
        if line.startswith("P"):
            header.append(line.strip())
            if m := _NNAME_RE.match(line):
                aliases[m.group("alias")] = m.group("name")
            elif m := _UNITS_RE.match(line):
                units_code = m.group("code")
                unit_mm = _UNITS_MM.get(units_code, unit_mm)
            elif m := _JOB_RE.match(line):
                job = m.group("job")
            continue
        if line.startswith("999"):
            break
        if line.startswith("027"):
            continue  # continuation of a 307 record: surface pad of a blind via, not modelled
        m = _RECORD_RE.match(line.ljust(80))
        if not m:
            continue
        net = m.group("net").strip()
        net = aliases.get(net, net)
        x_raw, y_raw = int(m.group("x")) * unit_mm, int(m.group("y")) * unit_mm
        w = _f(m.group("w"))
        h = _f(m.group("h"))
        drill = _f(m.group("drill"))
        plating = m.group("plating")
        points.append(
            TestPoint(
                record=m.group("type"),
                net=net,
                ref=m.group("ref").strip(),
                pin=m.group("pin").strip() if m.group("dash") == "-" else "",
                midpoint=m.group("mid") == "M",
                x_mm=round(x_raw + ox, 6),
                y_mm=round(oy - y_raw, 6),
                x_raw_mm=round(x_raw, 6),
                y_raw_mm=round(y_raw, 6),
                width_mm=round(w * unit_mm, 6) if w is not None else None,
                height_mm=round(h * unit_mm, 6) if h is not None else None,
                rotation_deg=float(m.group("rot") or 0) % 360.0,
                drill_mm=round(drill * unit_mm, 6) if drill is not None else None,
                plated=(plating == "P") if plating else None,
                access=int(m.group("access")),
                soldermask=int(m.group("mask") or 0),
                start_layer=int(m.group("start")) if m.group("start") else None,
                end_layer=int(m.group("end")) if m.group("end") else None,
            )
        )
    return TestPoints(
        source=source,
        job=job,
        units_code=units_code,
        aliases=aliases,
        points=tuple(points),
        aux_origin_mm=(ox, oy),
        raw={"header": header},
    )
