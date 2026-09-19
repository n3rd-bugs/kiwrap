"""kicad-cli `--help` parsing and the per-install help tree (contract: backends/cli/__init__.py).

The parser and the recursive probe are implemented in `kiwrap.capability` (the capability layer
needs them for its `cli.*` probes and import-linter forbids capability -> backends imports); this
module is the backend-facing entry point and adds the sandbox-aware `help_tree` signature.

Verified behaviour the parser encodes (spikes/versioning-strategy/clihelp, live 2026-09-19):
- 7.0 prints `Usage: gerbers [-h] [--output VAR] ...` and separates option names from help with a
  TAB; 8+ prints `[--output OUTPUT_FILE]` and uses column alignment; 10.0 prefixes top-level
  usage with `kicad-cli` (`Usage: kicad-cli version [--help] ...`).
- On 7/8 a leaf `--help` exits 1 and prints `input: 1 argument(s) expected. 0 provided.` on
  stderr before the usage on stdout; 9/10 exit 0. Asking for a subcommand the version does not
  have prints the parent's usage (7.0: `Maximum number of positional arguments exceeded`), so a
  node counts only when its usage line names it.
- `pcb export hpgl` still parses on 10.0 although its description says "No longer supported".
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from kiwrap.capability import flatten_help_tree, help_tree_for, parse_help, probe_help_tree
from kiwrap.installation import KiCadInstallation
from kiwrap.sandbox import ConfigRoot

__all__ = ["flatten_help_tree", "help_tree", "parse_help", "probe_help_tree"]


def help_tree(
    install: KiCadInstallation,
    config: ConfigRoot | None = None,
    *,
    refresh: bool = False,
    storage_home: Path | None = None,
) -> dict[str, Any]:
    """Nested `--help` tree for `install`, cached per install key under the storage home.

    With a `ConfigRoot` the probe runs with `config.env()` inside `config.lock()` and the seed is
    restored afterwards (every `--help` process rewrites `kicad_common.json` on exit, claim 10;
    the CLAUDE.md rule is env()/lock()/restore_seed() for EVERY KiCad process). Otherwise it runs
    with the throwaway probe environment from `kiwrap.installation.probe_env`. Either way the
    user's real KiCad config is never touched.
    """
    if config is None:
        return help_tree_for(install, refresh=refresh, storage_home=storage_home)
    env: Mapping[str, str] = config.env()
    with config.lock():
        try:
            return help_tree_for(install, refresh=refresh, storage_home=storage_home, env=env)
        finally:
            config.restore_seed()
