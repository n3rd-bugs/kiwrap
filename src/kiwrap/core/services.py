"""The seam between the kw_* tools and the lower layers (installation, capability, sandbox, backends).

Tools never import `kiwrap.backends.*` directly; they call `Services`, which is a bag of
callables. `Services.real()` resolves each callable lazily from the real modules (so a
component that is still a `NotImplementedError` stub, or absent, turns into a teaching
`E_UNSUPPORTED` at call time rather than an import failure at server start), and tests build a
`Services` with fakes. This is also where KiCad installs and capability records are cached
for the lifetime of the server (`kw_env(refresh=True)` clears them).

Data-first backends (docs/DECISIONS.md D16/D17; selected per operation by `kiwrap.core.planner`):
the IPC headless session + readers, the lossless file readers + analysis and the structured
kicad-cli exports. They are optional fields (default: a "missing" callable that raises
E_UNSUPPORTED, which the planner treats as "provider unavailable, try the next one"), so a
`Services` built by hand for tests keeps working, and `Services.real()` binds them lazily to the
contract modules named below (the integrator reconciles names if the real modules differ):

    ipc_session(install, file, config_root=...) -> kiwrap.backends.ipc.sessions.HeadlessSession
    ipc_read_board(session) / ipc_read_schematic(session) -> kiwrap.backends.ipc.readers (mm units)
    ipc_ratsnest(session) -> kiwrap.backends.ipc.queries.ratsnest (KiCad's connectivity clusters
    per net via GetConnectedItems; unrouted = clusters - 1, airwires between nearest anchors)
    file_read_board(path) -> kiwrap.formats.board.BoardFile.load
    file_read_schematic(path) -> kiwrap.formats.schematic.Hierarchy.load (the sheet tree)
    courtyard_overlaps / off_board / ratsnest_estimate (board) and pin_connectivity /
    netlist_from_file (schematic) -> kiwrap.formats.analysis
    export_stats / export_stackup (install, config, board) and sch_diff (install, config, a, b)
    -> kiwrap.backends.cli.structured (parsed JSON dicts)
"""

from __future__ import annotations

import importlib
import inspect
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kiwrap.core.state import KiwrapError

Install = Any  # kiwrap.installation.KiCadInstallation (kept loose so fakes work)
Caps = Any  # kiwrap.capability.Capabilities
ConfigRoot = Any  # kiwrap.sandbox.ConfigRoot


def _lazy(
    module: str, name: str, *, capability_hint: str, extra: dict[str, Any] | None = None
) -> Callable[..., Any]:
    """Return a callable that imports `module.name` on first use.

    ImportError / AttributeError / NotImplementedError become E_UNSUPPORTED with a `fix` that
    names the missing component (the working tree is built by several components at once).
    `extra` keyword arguments (e.g. `storage_home`) are added only when the resolved function
    accepts them, so the core keeps working against the bare contract signatures.
    """

    def call(*args: Any, **kwargs: Any) -> Any:
        try:
            mod = importlib.import_module(module)
            fn: Any = mod
            for part in name.split("."):  # "BoardFile.load" -> the classmethod
                fn = getattr(fn, part)
            if extra:
                accepted = inspect.signature(fn).parameters
                for key, value in extra.items():
                    if key in accepted and key not in kwargs:
                        kwargs[key] = value
        except (ImportError, AttributeError) as exc:
            raise KiwrapError(
                "E_UNSUPPORTED",
                f"{capability_hint} is not available in this build ({module}.{name}: {exc})",
                why="the component implementing it is not present in this kiwrap installation",
                fix="update kiwrap; this tool needs the " + module + " component",
                data={"module": module, "function": name},
            ) from exc
        try:
            return fn(*args, **kwargs)
        except NotImplementedError as exc:
            raise KiwrapError(
                "E_UNSUPPORTED",
                f"{capability_hint} is not implemented yet ({module}.{name})",
                why="the component implementing it is still a stub in this build",
                fix="update kiwrap; this tool needs the " + module + " component",
                data={"module": module, "function": name},
            ) from exc

    call.__name__ = name.replace(".", "_")
    return call


def _missing(name: str, capability_hint: str) -> Callable[..., Any]:
    """The default for an optional backend: E_UNSUPPORTED with a fix naming the component."""

    def call(*args: Any, **kwargs: Any) -> Any:
        raise KiwrapError(
            "E_UNSUPPORTED",
            f"{capability_hint} is not wired into this server ({name})",
            why="the Services instance was built without this backend (a test double or a partial build)",
            fix="update kiwrap; this tool needs the component providing " + name,
            data={"service": name},
        )

    call.__name__ = name
    return call


#: optional data-first backends: field name -> (module, attribute, capability hint)
DATA_BACKENDS: dict[str, tuple[str, str, str]] = {
    # pooled: one api-server per (install, file) survives between tool calls (sessions.SessionPool)
    "ipc_session": ("kiwrap.backends.ipc.sessions", "pooled_session", "the headless IPC session"),
    # the mutating tools' prologue: close kiwrap's own pooled servers on the project (they hold
    # KiCad's ~<name>.kicad_pro.lck) and learn about reads still in flight
    "ipc_release_project": ("kiwrap.backends.ipc.sessions", "release_project", "the headless IPC session pool"),
    "ipc_read_board": ("kiwrap.backends.ipc.readers", "read_board", "the IPC board reader"),
    "ipc_read_schematic": ("kiwrap.backends.ipc.readers", "read_schematic", "the IPC schematic reader"),
    "ipc_ratsnest": ("kiwrap.backends.ipc.queries", "ratsnest", "the IPC connectivity / ratsnest query"),
    "file_read_board": ("kiwrap.formats.board", "BoardFile.load", "the board file reader"),
    # the whole sheet tree (root + sub-sheets, per-instance references), not one file
    "file_read_schematic": ("kiwrap.formats.schematic", "Hierarchy.load", "the schematic file reader"),
    "courtyard_overlaps": ("kiwrap.formats.analysis", "courtyard_overlaps", "courtyard overlap analysis"),
    "off_board": ("kiwrap.formats.analysis", "off_board", "off-board analysis"),
    "ratsnest_estimate": ("kiwrap.formats.analysis", "ratsnest_estimate", "the ratsnest estimate"),
    "pin_connectivity": ("kiwrap.formats.analysis", "pin_connectivity", "schematic pin connectivity"),
    "netlist_from_file": ("kiwrap.formats.analysis", "netlist_from_file", "the file netlist"),
    "export_stats": ("kiwrap.backends.cli.structured", "export_stats", "kicad-cli pcb export stats"),
    "export_stackup": ("kiwrap.backends.cli.structured", "export_stackup", "kicad-cli pcb export stackup"),
    "sch_diff": ("kiwrap.backends.cli.structured", "sch_diff", "kicad-cli sch diff"),
}


@dataclass
class Services:
    """Everything a tool needs from below the core layer. Replace fields with fakes in tests."""

    storage_home: Path
    discover: Callable[..., list[Install]]
    find: Callable[..., Install]
    capabilities_for: Callable[..., Caps]
    sandbox_ensure: Callable[..., ConfigRoot]
    user_config_hash: Callable[..., str]
    run_erc: Callable[..., Any]
    run_drc: Callable[..., Any]
    export_netlist: Callable[..., Any]
    export_pcb: Callable[..., list[Path]]
    export_sch: Callable[..., list[Path]]
    render_pcb: Callable[..., Path]
    render_pcb_2d: Callable[..., Path]
    rasterise_pdf: Callable[..., Path]
    # data-first backends (optional; see DATA_BACKENDS and the module docstring)
    ipc_session: Callable[..., Any] = None  # type: ignore[assignment]  # __post_init__ fills it
    ipc_release_project: Callable[..., Any] = None  # type: ignore[assignment]  # __post_init__ fills it
    ipc_read_board: Callable[..., Any] = None  # type: ignore[assignment]  # __post_init__ fills it
    ipc_read_schematic: Callable[..., Any] = None  # type: ignore[assignment]  # __post_init__ fills it
    ipc_ratsnest: Callable[..., Any] = None  # type: ignore[assignment]  # __post_init__ fills it
    file_read_board: Callable[..., Any] = None  # type: ignore[assignment]  # __post_init__ fills it
    file_read_schematic: Callable[..., Any] = None  # type: ignore[assignment]  # __post_init__ fills it
    courtyard_overlaps: Callable[..., Any] = None  # type: ignore[assignment]  # __post_init__ fills it
    off_board: Callable[..., Any] = None  # type: ignore[assignment]  # __post_init__ fills it
    ratsnest_estimate: Callable[..., Any] = None  # type: ignore[assignment]  # __post_init__ fills it
    pin_connectivity: Callable[..., Any] = None  # type: ignore[assignment]  # __post_init__ fills it
    netlist_from_file: Callable[..., Any] = None  # type: ignore[assignment]  # __post_init__ fills it
    export_stats: Callable[..., Any] = None  # type: ignore[assignment]  # __post_init__ fills it
    export_stackup: Callable[..., Any] = None  # type: ignore[assignment]  # __post_init__ fills it
    sch_diff: Callable[..., Any] = None  # type: ignore[assignment]  # __post_init__ fills it
    # caches (per server process)
    _installs: list[Install] | None = field(default=None, repr=False)
    _caps: dict[str, Caps] = field(default_factory=dict, repr=False)
    _roots: dict[str, ConfigRoot] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        """Optional backends left at None (a hand-built `Services`) become `_missing` callables."""
        for name, (module, attr, hint) in DATA_BACKENDS.items():
            if getattr(self, name) is None:
                setattr(self, name, _missing(f"{module}.{attr}", hint))

    @classmethod
    def real(cls, storage_home: Path) -> Services:
        home = {"storage_home": storage_home}
        data: dict[str, Any] = {
            name: _lazy(module, attr, capability_hint=hint)
            for name, (module, attr, hint) in DATA_BACKENDS.items()
        }
        return cls(
            **data,
            storage_home=storage_home,
            discover=_lazy("kiwrap.installation", "discover", capability_hint="KiCad discovery", extra=home),
            # probe_paths=False: a path in a tool argument only ever selects an install discovery
            # already found; it is never executed to find out whether it is a KiCad.
            find=_lazy(
                "kiwrap.installation",
                "find",
                capability_hint="KiCad discovery",
                extra={"probe_paths": False},
            ),
            capabilities_for=_lazy(
                "kiwrap.capability", "capabilities_for", capability_hint="capability probing", extra=home
            ),
            sandbox_ensure=_lazy("kiwrap.sandbox", "ensure", capability_hint="the config sandbox"),
            user_config_hash=_lazy(
                "kiwrap.sandbox", "user_config_hash", capability_hint="the config sandbox"
            ),
            run_erc=_lazy("kiwrap.backends.cli.erc", "run_erc", capability_hint="kicad-cli ERC"),
            run_drc=_lazy("kiwrap.backends.cli.drc", "run_drc", capability_hint="kicad-cli DRC"),
            export_netlist=_lazy(
                "kiwrap.backends.cli.netlist", "export_netlist", capability_hint="kicad-cli netlist export"
            ),
            export_pcb=_lazy(
                "kiwrap.backends.cli.export", "export_pcb", capability_hint="kicad-cli pcb export"
            ),
            export_sch=_lazy(
                "kiwrap.backends.cli.export", "export_sch", capability_hint="kicad-cli sch export"
            ),
            render_pcb=_lazy(
                "kiwrap.backends.cli.render", "render_pcb", capability_hint="kicad-cli pcb render"
            ),
            render_pcb_2d=_lazy(
                "kiwrap.backends.cli.render", "render_pcb_2d", capability_hint="2D board preview"
            ),
            rasterise_pdf=_lazy(
                "kiwrap.backends.cli.render", "rasterise_pdf", capability_hint="PDF rasterisation"
            ),
        )

    # ---- cached lookups -----------------------------------------------------------------------

    def installs(self, *, refresh: bool = False) -> list[Install]:
        if refresh or self._installs is None:
            self._installs = list(self.discover())
            if refresh:
                self._caps.clear()
                self._roots.clear()
        return self._installs

    def install(self, spec: str | None) -> Install:
        """`find()` over the cached installs; NoKiCadError -> E_NO_KICAD."""
        installs = self.installs()
        try:
            return self.find(spec, installs)
        except KiwrapError:
            raise
        except Exception as exc:  # NoKiCadError (or a stub) - keep the dependency loose
            found = [getattr(i, "version", str(i)) for i in installs]
            raise KiwrapError(
                "E_NO_KICAD",
                f"no KiCad matching {spec!r}" if spec else "no KiCad installation found",
                why=str(exc),
                fix=(
                    "install KiCad (the newest release is recommended) or pass target_kicad as a major "
                    "('N'), major.minor ('N.M'), a full semver, or the root/kicad-cli path of an install "
                    "kw_env lists"
                ),
                candidates=found,
                example='kw_project(action="create", dir=..., name=..., target_kicad="<major>")',
            ) from exc

    def caps(self, install: Install, *, refresh: bool = False) -> Caps:
        key = install.key()
        if refresh or key not in self._caps:
            self._caps[key] = self.capabilities_for(install, refresh=refresh)
        return self._caps[key]

    def config_root(self, install: Install) -> ConfigRoot:
        key = install.key()
        if key not in self._roots:
            self._roots[key] = self.sandbox_ensure(install, storage_home=self.storage_home)
        return self._roots[key]

    def provider(self, install: Install) -> str:
        return f"cli:{install.version}"

    def data_backends(self) -> dict[str, bool]:
        """Which optional backends are wired (not the `_missing` default) - for kw_env / tests."""
        return {
            name: getattr(getattr(self, name), "__name__", "") != f"{m}.{a}"
            for name, (m, a, _) in DATA_BACKENDS.items()
        }
