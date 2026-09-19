"""The seam between the kw_* tools and the lower layers (installation, capability, sandbox, backends).

Tools never import `kiwrap.backends.*` directly; they call `Services`, which is a bag of
callables. `Services.real()` resolves each callable lazily from the real modules (so a
component that is still a `NotImplementedError` stub, or absent, turns into a teaching
`E_UNSUPPORTED` at call time rather than an import failure at server start), and tests build a
`Services` with fakes. This is also where KiCad installs and capability records are cached
for the lifetime of the server (`kw_env(refresh=True)` clears them).
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
            fn = getattr(mod, name)
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

    call.__name__ = name
    return call


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
    # caches (per server process)
    _installs: list[Install] | None = field(default=None, repr=False)
    _caps: dict[str, Caps] = field(default_factory=dict, repr=False)
    _roots: dict[str, ConfigRoot] = field(default_factory=dict, repr=False)

    @classmethod
    def real(cls, storage_home: Path) -> Services:
        home = {"storage_home": storage_home}
        return cls(
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
