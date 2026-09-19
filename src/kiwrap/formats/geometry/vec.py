"""2-D vector, rotation and bounding-box primitives in KiCad's coordinate convention.

KiCad's board and schematic sheets are Y-down. `RotatePoint` (libs/kimath/src/trigo.cpp, tag
10.0, lines 229-264) rotates by a positive angle *counter-clockwise on screen*, which in Y-down
coordinates is `x' = x·cos + y·sin`, `y' = -x·sin + y·cos`; the 90° shortcut is `(y, -x)`.
`rotate()` encodes exactly that, so a footprint pad at local `(5, 0)` in a footprint placed with
`(at X Y 90)` lands at `(X, Y - 5)` — above the footprint centre on screen. Everything in this
package is millimetres (the CST keeps the file's own text; conversion happens in the models).
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass

__all__ = ["BBox", "Vec2", "rotate"]


@dataclass(frozen=True, slots=True)
class Vec2:
    """An immutable 2-D point/vector in millimetres."""

    x: float
    y: float

    def __add__(self, other: Vec2) -> Vec2:
        return Vec2(self.x + other.x, self.y + other.y)

    def __sub__(self, other: Vec2) -> Vec2:
        return Vec2(self.x - other.x, self.y - other.y)

    def __mul__(self, k: float) -> Vec2:
        return Vec2(self.x * k, self.y * k)

    __rmul__ = __mul__

    def __neg__(self) -> Vec2:
        return Vec2(-self.x, -self.y)

    def dot(self, other: Vec2) -> float:
        return self.x * other.x + self.y * other.y

    def cross(self, other: Vec2) -> float:
        """z-component of the cross product (positive when `other` is clockwise-on-screen of self)."""
        return self.x * other.y - self.y * other.x

    def length(self) -> float:
        return math.hypot(self.x, self.y)

    def distance(self, other: Vec2) -> float:
        return math.hypot(self.x - other.x, self.y - other.y)

    def rotated(self, angle_deg: float) -> Vec2:
        return rotate(self, angle_deg)

    def rounded(self, ndigits: int = 6) -> Vec2:
        return Vec2(round(self.x, ndigits) + 0.0, round(self.y, ndigits) + 0.0)

    def close_to(self, other: Vec2, eps: float = 1e-6) -> bool:
        return abs(self.x - other.x) <= eps and abs(self.y - other.y) <= eps

    def as_tuple(self) -> tuple[float, float]:
        return (self.x, self.y)


def rotate(v: Vec2, angle_deg: float) -> Vec2:
    """Rotate `v` about the origin by `angle_deg` the way KiCad's `RotatePoint` does (see module doc).

    Multiples of 90° are exact (KiCad's own shortcuts) so that footprint geometry at the four
    standard orientations never picks up floating-point noise.
    """
    a = angle_deg % 360.0
    if a == 0.0:
        return v
    if a == 90.0:
        return Vec2(v.y, -v.x)
    if a == 180.0:
        return Vec2(-v.x, -v.y)
    if a == 270.0:
        return Vec2(-v.y, v.x)
    r = math.radians(a)
    s, c = math.sin(r), math.cos(r)
    return Vec2(v.x * c + v.y * s, -v.x * s + v.y * c)


@dataclass(frozen=True, slots=True)
class BBox:
    """Axis-aligned box `[min_x, max_x] × [min_y, max_y]` (inclusive)."""

    min_x: float
    min_y: float
    max_x: float
    max_y: float

    @classmethod
    def of(cls, points: Iterable[Vec2]) -> BBox | None:
        """Bounding box of a point set, or None for an empty one."""
        it = iter(points)
        try:
            first = next(it)
        except StopIteration:
            return None
        min_x = max_x = first.x
        min_y = max_y = first.y
        for p in it:
            min_x = min(min_x, p.x)
            max_x = max(max_x, p.x)
            min_y = min(min_y, p.y)
            max_y = max(max_y, p.y)
        return cls(min_x, min_y, max_x, max_y)

    @property
    def width(self) -> float:
        return self.max_x - self.min_x

    @property
    def height(self) -> float:
        return self.max_y - self.min_y

    @property
    def center(self) -> Vec2:
        return Vec2((self.min_x + self.max_x) / 2, (self.min_y + self.max_y) / 2)

    @property
    def area(self) -> float:
        return self.width * self.height

    def union(self, other: BBox) -> BBox:
        return BBox(
            min(self.min_x, other.min_x),
            min(self.min_y, other.min_y),
            max(self.max_x, other.max_x),
            max(self.max_y, other.max_y),
        )

    def intersection(self, other: BBox) -> BBox | None:
        min_x, max_x = max(self.min_x, other.min_x), min(self.max_x, other.max_x)
        min_y, max_y = max(self.min_y, other.min_y), min(self.max_y, other.max_y)
        if min_x > max_x or min_y > max_y:
            return None
        return BBox(min_x, min_y, max_x, max_y)

    def intersects(self, other: BBox, eps: float = 0.0) -> bool:
        return not (
            self.max_x + eps < other.min_x
            or other.max_x + eps < self.min_x
            or self.max_y + eps < other.min_y
            or other.max_y + eps < self.min_y
        )

    def contains(self, p: Vec2, eps: float = 0.0) -> bool:
        return self.min_x - eps <= p.x <= self.max_x + eps and self.min_y - eps <= p.y <= self.max_y + eps

    def contains_box(self, other: BBox, eps: float = 0.0) -> bool:
        return (
            other.min_x >= self.min_x - eps
            and other.max_x <= self.max_x + eps
            and other.min_y >= self.min_y - eps
            and other.max_y <= self.max_y + eps
        )

    def expanded(self, margin: float) -> BBox:
        return BBox(self.min_x - margin, self.min_y - margin, self.max_x + margin, self.max_y + margin)

    def corners(self) -> tuple[Vec2, Vec2, Vec2, Vec2]:
        return (
            Vec2(self.min_x, self.min_y),
            Vec2(self.max_x, self.min_y),
            Vec2(self.max_x, self.max_y),
            Vec2(self.min_x, self.max_y),
        )
