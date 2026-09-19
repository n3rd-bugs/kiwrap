"""Exact unit conversion between the IPC API's integer nanometres and kiwrap's millimetres.

The API carries every length as `int64` nanometres (`Vector2.x_nm`, `Distance.value_nm`;
`api/proto/common/types/base_types.proto`). kiwrap exposes millimetres. Both directions go through
`Decimal` / integer arithmetic so a value is rounded exactly once (decimal -> nearest binary float
on output, decimal -> nearest integer nanometre on input); nothing is ever accumulated in floats.

Verified on the 10.99 nightly: ecc83 `C1` is reported at (141605000, 99695000) nm, orientation
90 degrees, which is (141.605, 99.695) mm in the board file (tests/test_ipc_live.py).
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal

__all__ = ["NM_PER_MM", "mm_to_nm", "nm_to_mm", "nm_to_mm_decimal"]

NM_PER_MM = 1_000_000
_NM_PER_MM_DEC = Decimal(NM_PER_MM)


def nm_to_mm_decimal(nm: int) -> Decimal:
    """Exact decimal millimetres (at most six fractional digits)."""
    return Decimal(int(nm)).scaleb(-6)


def nm_to_mm(nm: int) -> float:
    """Millimetres as the float nearest to the exact decimal value (one rounding, no accumulation)."""
    return float(nm_to_mm_decimal(nm))


def mm_to_nm(mm: float | int | str | Decimal) -> int:
    """Integer nanometres nearest to `mm` (floats are read through `repr`, so 0.1 mm -> 100000 nm)."""
    if isinstance(mm, Decimal):
        value = mm
    elif isinstance(mm, bool):
        raise TypeError("mm must be a number, not bool")
    elif isinstance(mm, int):
        value = Decimal(mm)
    elif isinstance(mm, float):
        value = Decimal(repr(mm))
    else:
        value = Decimal(str(mm))
    return int((value * _NM_PER_MM_DEC).quantize(Decimal(1), rounding=ROUND_HALF_EVEN))
