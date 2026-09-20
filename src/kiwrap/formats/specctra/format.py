"""Text conventions of KiCad's Specctra writer (`pcbnew/specctra_import_export/specctra.cpp`).

Everything here reproduces how KiCad 10.0.6 and the 10.99 nightly print a DSN so that the codec's
files are token-for-token compatible with what Freerouting has always been fed (docs/VERIFICATION.md
claim 7: the exported files of 7.0.6 – 10.0.x are what Freerouting 2.4.1 was verified against):

* numbers are printed with C's `%.6g` (`POINT::Format`, `PATH::Format`, the rule strings
  `(width %.6g)` …): six significant digits, no trailing zeros, exponent form for magnitudes at
  or above 1e6. Coordinates are micrometres (`(unit um)`), so a 400 mm board keeps 1 µm and a
  40 mm board 0.1 µm; `(resolution um 10)` tells the router the grid is 0.1 µm. `PLACE::Format`
  alone prints its x, y and rotation through `FormatDouble2Str` (common/string_utils.cpp:
  `{:.10g}`, ten significant digits, so `142480.532` stays as written);
* every millimetre value goes through KiCad's integer nanometres first: the file's `128.0825`
  is `KiROUND(128.0825 * 1e6) = 128082500` nm and `scale()` divides that by 1000.0, giving
  exactly 128082.5 which `%.6g` rounds to `128082`; a plain `mm * 1000.0` yields
  128082.50000000001 and prints `128083`, 1 µm off KiCad on every half-micrometre coordinate at
  or above 100 mm (`um` reproduces the integer path, `kiround` is KiROUND: half away from zero);
* `-0` never appears (`POINT::FixNegativeZero`);
* a token is quoted (`OUTPUTFORMATTER::GetQuoteChar`, common/richio.cpp) when it is empty, starts
  with `#`, contains any of tab, space, `(`, `)`, `%`, `{`, `}` or contains a `-` anywhere but
  in the first position. Specctra strings have no escape mechanism, so a payload holding the
  quote character is folded to `''` first (`sanitizeForDSNString`, values and net names);
* the DSN y axis points up: `mapPt` negates KiCad's y (`static POINT mapPt( const VECTOR2I& )`),
  and every millimetre value is scaled to micrometres.
"""

from __future__ import annotations

import math
from collections.abc import Iterable

__all__ = [
    "MM_TO_DSN",
    "dsn_string",
    "g6",
    "g10",
    "join_ids",
    "kiround",
    "place_pt",
    "pt",
    "quote_id",
    "um",
]

#: DSN unit is micrometres; KiCad scales internal nanometres by 1/1000.
MM_TO_DSN = 1000.0
_QUOTE_THESE = "\t ()%{}"


_NM_PER_MM = 1_000_000.0


def kiround(v: float) -> int:
    """`KiROUND`: round half away from zero (libs/kimath/include/math/util.h)."""
    return int(math.floor(v + 0.5)) if v >= 0 else -int(math.floor(-v + 0.5))


def um(mm: float) -> float:
    """Millimetres -> DSN micrometres through KiCad's integer nanometres (`scale( int )`:
    `KiROUND(mm * 1e6) / 1000.0`, module docstring), `-0.0` folded to `0.0`."""
    v = kiround(mm * _NM_PER_MM) / (_NM_PER_MM / MM_TO_DSN)
    return 0.0 if v == 0 else v


def g6(v: float) -> str:
    """C `%.6g` (Python's `format(v, '.6g')` is the same algorithm); `-0` becomes `0`."""
    if v == 0:
        return "0"
    text = format(float(v), ".6g")
    if text == "-0":
        return "0"
    return text


def g10(v: float) -> str:
    """`FormatDouble2Str` for the magnitudes a placement holds: `{:.10g}` (the `<= 0.0001`
    branch prints 16 decimals, which no placement coordinate or angle reaches)."""
    if v == 0:
        return "0"
    if abs(v) <= 0.0001:
        text = format(float(v), ".16f").rstrip("0").rstrip(".")
        return text if text not in ("", "-0", "-") else "0"
    text = format(float(v), ".10g")
    return "0" if text == "-0" else text


def pt(x_mm: float, y_mm: float) -> str:
    """A point in DSN space: micrometres with the y axis flipped (`mapPt`)."""
    return f"{g6(um(x_mm))} {g6(-um(y_mm))}"


def place_pt(x_mm: float, y_mm: float) -> str:
    """A `(place …)` vertex: `mapPt` then `FormatDouble2Str` (ten significant digits)."""
    return f"{g10(um(x_mm))} {g10(-um(y_mm))}"


def dsn_string(value: str) -> str:
    """`sanitizeForDSNString`: fold the quote character, which Specctra cannot escape."""
    return value.replace('"', "''")


def quote_id(token: str) -> str:
    """Wrap `token` in quotes exactly when KiCad's `GetQuoteChar` would (module docstring)."""
    if token == "" or token[0] == "#":
        return f'"{token}"'
    for i, ch in enumerate(token):
        if ch in _QUOTE_THESE or (ch == "-" and i > 0):
            return f'"{token}"'
    return token


def join_ids(tokens: Iterable[str]) -> str:
    return " ".join(quote_id(t) for t in tokens)
