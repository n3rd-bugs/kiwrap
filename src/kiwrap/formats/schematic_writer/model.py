"""Result dataclasses and the error type of the schematic authoring engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "PinEndpoint",
    "SheetPinEndpoint",
    "SheetPlacement",
    "SymbolPlacement",
    "WriterError",
]


class WriterError(ValueError):
    """A request the writer cannot honour (unknown lib_id, duplicate reference, off-grid sheet
    pin, unknown uuid, …). `code` is stable for teaching errors upstream; `fix` says what to do."""

    def __init__(
        self, code: str, message: str, *, fix: str = "", candidates: list[str] | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.fix = fix
        self.candidates = candidates or []


@dataclass(frozen=True)
class PinEndpoint:
    """A pin of a placed symbol with its ABSOLUTE sheet position (mm, Y down), from the verified
    transform (docs/VERIFICATION.md claim 3). Wires must END here to connect."""

    number: str
    name: str
    x_mm: float
    y_mm: float
    electrical_type: str
    unit: int = 0  # library unit the pin belongs to (0 = shared by all units)
    hidden: bool = False
    uuid: str = ""

    @property
    def at_mm(self) -> tuple[float, float]:
        return (self.x_mm, self.y_mm)


@dataclass(frozen=True)
class SymbolPlacement:
    uuid: str
    ref: str
    pins: list[PinEndpoint]
    lib_id: str = ""
    unit: int = 1
    sheet: str = ""  # the sheet key the symbol was placed on ("" = root)
    #: reference per instance path when the sheet file is instantiated more than once
    references: dict[str, str] = field(default_factory=dict)

    def pin(self, number_or_name: str) -> PinEndpoint:
        for p in self.pins:
            if p.number == number_or_name:
                return p
        for p in self.pins:
            if p.name == number_or_name:
                return p
        raise KeyError(f"{self.ref} has no pin {number_or_name!r}; pins: {[p.number for p in self.pins]}")


@dataclass(frozen=True)
class SheetPinEndpoint:
    name: str
    kind: str  # input | output | bidirectional | tri_state | passive
    side: str  # left | right | top | bottom
    x_mm: float
    y_mm: float
    uuid: str = ""

    @property
    def at_mm(self) -> tuple[float, float]:
        return (self.x_mm, self.y_mm)


@dataclass(frozen=True)
class SheetPlacement:
    uuid: str
    name: str
    file: Path
    pins: list[SheetPinEndpoint]
    instance_path: str  # "/ROOT_UUID/SHEET_UUID" — the path symbols inside the sheet use
    page: str = ""

    def pin(self, name: str) -> SheetPinEndpoint:
        for p in self.pins:
            if p.name == name:
                return p
        raise KeyError(f"sheet {self.name!r} has no pin {name!r}")
