"""Fakes for the core tests: a `Services` whose lower layers return canned objects.

No KiCad is involved. Reports/netlists use the real `kiwrap.formats.reports` dataclasses (the
contract every backend returns); PNGs are generated with zlib/struct so image handling is real.
This module holds no tests itself.
"""

from __future__ import annotations

import struct
import zlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kiwrap.core.services import Services
from kiwrap.formats.reports import (
    Component,
    Net,
    Netlist,
    NetNode,
    Position,
    Report,
    Violation,
    ViolationItem,
)


@dataclass(frozen=True)
class FakeInstall:
    version: str = "10.0.0"
    root: Path = Path("C:/fake/KiCad/10.0")
    source: str = "fake"
    python: Path | None = None

    @property
    def kicad_cli(self) -> Path:
        return self.root / "bin" / "kicad-cli.exe"

    @property
    def share(self) -> Path:
        return self.root / "share" / "kicad"

    @property
    def major_minor(self) -> str:
        return ".".join(self.version.split(".")[:2])

    @property
    def major(self) -> int:
        return int(self.version.split(".")[0])

    def key(self) -> str:
        return f"{self.version}@{self.root}"


class FakeCaps(Mapping[str, Any]):
    def __init__(self, values: dict[str, Any], install: FakeInstall):
        self._values, self.install, self.probed = dict(values), install, True

    def __getitem__(self, key: str) -> Any:
        return self._values[key]

    def __iter__(self):
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def has(self, key: str) -> bool:
        return bool(self._values.get(key))

    def flags(self, subcommand: str) -> list[str]:
        return list(self._values.get(f"cli.flags.{subcommand}", []))


CAPS_10 = {
    "cli.has_erc": True,
    "cli.has_drc": True,
    "cli.has_render": True,
    "cli.has_jobset": True,
    "cli.has_sch_pcb_upgrade": True,
    "cli.drc_refill_flags": True,
    "cli.erc_json_pos_scale": 100,
    "cli.gerbers_creates_outdir": True,
    "cli.writes_prl_on_read": True,
    "cli.sch_stamp_gate_needs_generator_version": True,
    "cli.subcommands": ["sch erc", "pcb drc", "pcb render"],
    "formats.sch": 20260306,
    "formats.pcb": 20260206,
    "formats.sym": 20251024,
    "formats.pro": 3,
    "formats.pcb_layer_ids_v2": True,
    "formats.generator_version_token": True,
    "swig.available": True,
    "ipc.available": True,
    "ipc.headless": False,
    "rules.default_min_track_width_mm": 0.2,
}

CAPS_7 = {
    **CAPS_10,
    "cli.has_erc": False,
    "cli.has_drc": False,
    "cli.has_render": False,
    "cli.drc_refill_flags": False,
    "cli.sch_stamp_gate_needs_generator_version": False,
    "formats.sch": 20230121,
    "formats.pcb": 20221018,
    "formats.pcb_layer_ids_v2": False,
    "formats.generator_version_token": False,
}


class Unsupported(RuntimeError):
    """Same name as kiwrap.capability.Unsupported (the core matches by class name)."""

    def __init__(
        self,
        capability: str,
        install: Any,
        *,
        oldest_kicad_with_it: str | None = None,
        fallback: str | None = None,
    ):
        self.capability, self.install = capability, install
        self.oldest_kicad_with_it, self.fallback = oldest_kicad_with_it, fallback
        super().__init__(f"{capability} is not available on KiCad {install.version}")


class NoKiCadError(RuntimeError):
    def __init__(self, spec: str | None, found: list[Any]):
        self.spec, self.found = spec, found
        super().__init__(f"no KiCad matching {spec!r}")


class CliLoadError(RuntimeError):
    def __init__(self, msg: str):
        super().__init__(msg)
        self.argv, self.returncode, self.stdout, self.stderr = ["kicad-cli"], 3, "", msg


def png_bytes(width: int = 8, height: int = 6) -> bytes:
    raw = b"".join(b"\x00" + bytes([200, 30, 30] * width) for _ in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")
    )


def violation(
    vtype: str, severity: str = "error", x: float = 10.0, y: float = 20.0, *, sheet: str | None = "/"
) -> Violation:
    return Violation(
        type=vtype,
        severity=severity,
        description=f"{vtype} at {x} mm, {y} mm",
        items=(
            ViolationItem(
                uuid="11111111-2222-3333-4444-555555555555", description="item", pos=Position(x, y)
            ),
        ),
        sheet=sheet,
    )


def erc_report(*violations: Violation, version: str = "10.0.0") -> Report:
    return Report(kind="erc", source="root.kicad_sch", kicad_version=version, violations=tuple(violations))


def drc_report(
    *violations: Violation,
    unconnected: tuple[Violation, ...] = (),
    parity: tuple[Violation, ...] = (),
    version: str = "10.0.0",
) -> Report:
    return Report(
        kind="drc",
        source="board.kicad_pcb",
        kicad_version=version,
        violations=tuple(violations),
        unconnected=unconnected,
        schematic_parity=parity,
    )


def netlist(version: str = "10.0.0") -> Netlist:
    return Netlist(
        source="root.kicad_sch",
        kicad_version=version,
        components=(
            Component(
                ref="R1",
                value="10k",
                footprint="Resistor_SMD:R_0603_1608Metric",
                lib_id="Device:R",
                sheet_path="/",
            ),
        ),
        nets=(Net(code=1, name="VCC", nodes=(NetNode("R1", "1"), NetNode("R1", "2"))),),
    )


@dataclass
class FakeBackend:
    """Mutable knobs the tests turn: which report/netlist to return, whether calls raise."""

    installs: list[FakeInstall] = field(default_factory=lambda: [FakeInstall()])
    caps: dict[str, dict[str, Any]] = field(default_factory=lambda: {"10.0.0": CAPS_10})
    erc: Report | Exception = field(default_factory=erc_report)
    drc: Report | Exception = field(default_factory=drc_report)
    net: Netlist | Exception = field(default_factory=netlist)
    render_raises: Exception | None = None
    rasterise_raises: Exception | None = None
    pdf_pages: int = 1
    export_raises: dict[str, Exception] = field(default_factory=dict)
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def _log(self, name: str, **kw: Any) -> None:
        self.calls.append((name, kw))

    def discover(self, **kw: Any) -> list[FakeInstall]:
        self._log("discover")
        return list(self.installs)

    def find(self, spec: str | None, installs: list[FakeInstall] | None = None) -> FakeInstall:
        pool = installs if installs is not None else self.installs
        if not pool:
            raise NoKiCadError(spec, [])
        if spec is None:
            return pool[0]
        for i in pool:
            if i.version == spec or i.major_minor == spec or str(i.major) == spec:
                return i
        raise NoKiCadError(spec, pool)

    def capabilities_for(self, install: FakeInstall, *, refresh: bool = False, **kw: Any) -> FakeCaps:
        self._log("capabilities_for", version=install.version, refresh=refresh)
        return FakeCaps(self.caps.get(install.version, CAPS_10), install)

    def sandbox_ensure(
        self, install: FakeInstall, *, storage_home: Path | None = None, **kw: Any
    ) -> dict[str, Any]:
        self._log("sandbox_ensure", version=install.version)
        return {"install": install.version, "path": str(storage_home)}

    def user_config_hash(self, install: FakeInstall) -> str:
        return "0" * 64

    def run_erc(self, install: FakeInstall, config: Any, schematic: Path, **kw: Any) -> Report:
        self._log("run_erc", schematic=str(schematic), **kw)
        if isinstance(self.erc, Exception):
            raise self.erc
        return self.erc

    def run_drc(self, install: FakeInstall, config: Any, board: Path, **kw: Any) -> Report:
        self._log("run_drc", board=str(board), **kw)
        if isinstance(self.drc, Exception):
            raise self.drc
        return self.drc

    def export_netlist(
        self, install: FakeInstall, config: Any, schematic: Path, out: Path | None = None, **kw: Any
    ) -> Netlist:
        self._log("export_netlist", schematic=str(schematic))
        if isinstance(self.net, Exception):
            raise self.net
        return self.net

    def export_pcb(
        self, install: FakeInstall, config: Any, board: Path, target: str, out_dir: Path, **opts: Any
    ) -> list[Path]:
        self._log("export_pcb", target=target, out_dir=str(out_dir), **opts)
        if target in self.export_raises:
            raise self.export_raises[target]
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        ext = {
            "gerbers": "gbr",
            "drill": "drl",
            "pos": "csv",
            "pdf": "pdf",
            "step": "step",
            "svg": "svg",
        }.get(target, target)
        files = [out_dir / f"{Path(board).stem}-{target}-{i}.{ext}" for i in range(2)]
        for f in files:
            f.write_text(f"fake {target}\n")
        return files

    def export_sch(
        self, install: FakeInstall, config: Any, schematic: Path, target: str, out: Path, **opts: Any
    ) -> list[Path]:
        self._log("export_sch", target=target, out=str(out), **opts)
        if target in self.export_raises:
            raise self.export_raises[target]
        out = Path(out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(f"fake {target}\n")
        return [out]

    def render_pcb(
        self,
        install: FakeInstall,
        config: Any,
        board: Path,
        out_png: Path,
        *,
        width: int,
        height: int,
        **kw: Any,
    ) -> Path:
        self._log("render_pcb", width=width, height=height, **kw)
        if self.render_raises:
            raise self.render_raises
        out_png = Path(out_png)
        out_png.parent.mkdir(parents=True, exist_ok=True)
        out_png.write_bytes(png_bytes(min(width, 64), min(height, 48)))
        return out_png

    def render_pcb_2d(self, install: FakeInstall, config: Any, board: Path, out_png: Path, **kw: Any) -> Path:
        self._log("render_pcb_2d", **kw)
        if self.rasterise_raises:
            raise self.rasterise_raises
        out_png = Path(out_png)
        out_png.parent.mkdir(parents=True, exist_ok=True)
        out_png.write_bytes(png_bytes(32, 24))
        return out_png

    def rasterise_pdf(self, pdf: Path, out_png: Path, *, max_px: int = 1024, page: int = 0) -> Path:
        self._log("rasterise_pdf", pdf=str(pdf), max_px=max_px, page=page)
        if self.rasterise_raises:
            raise self.rasterise_raises
        if page < 0 or page >= self.pdf_pages:  # mirrors backends.cli.render.PdfPageError
            exc = ValueError(f"page {page + 1} does not exist; the PDF has {self.pdf_pages} page(s)")
            exc.page_count = self.pdf_pages  # type: ignore[attr-defined]
            raise exc
        out_png = Path(out_png)
        out_png.parent.mkdir(parents=True, exist_ok=True)
        out_png.write_bytes(png_bytes(40, 30))
        return out_png

    def services(self, storage_home: Path) -> Services:
        return Services(
            storage_home=storage_home,
            discover=self.discover,
            find=self.find,
            capabilities_for=self.capabilities_for,
            sandbox_ensure=self.sandbox_ensure,
            user_config_hash=self.user_config_hash,
            run_erc=self.run_erc,
            run_drc=self.run_drc,
            export_netlist=self.export_netlist,
            export_pcb=self.export_pcb,
            export_sch=self.export_sch,
            render_pcb=self.render_pcb,
            render_pcb_2d=self.render_pcb_2d,
            rasterise_pdf=self.rasterise_pdf,
        )
