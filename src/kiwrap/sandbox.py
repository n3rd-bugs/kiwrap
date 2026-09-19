"""Sandboxed KiCad configuration roots.

Every KiCad process kiwrap starts (kicad-cli, the SWIG driver, a GUI for live sessions) runs with
`KICAD_CONFIG_HOME` pointing at a root kiwrap owns under the storage home. The user's real config
(`Platform.user_kicad_config_home`) is read for library tables and environment variables and is
NEVER written; conformance test T6 asserts its hash is unchanged after every run.

Verified facts this module encodes (docs/VERIFICATION.md claim 10, docs/ARCHITECTURE.md §6):
- KiCad appends `<major.minor>/` under KICAD_CONFIG_HOME; seed files go INSIDE that subfolder.
- 10.0 shows the Setup wizard unless `kicad_common.json` has `api.enable_server` (any value),
  `do_not_show_again.update_check_prompt` and `.data_collection_prompt` both true, AND all three
  tables (sym-lib-table, fp-lib-table, design-block-lib-table) exist and parse. 9.0 gates its
  dialogs on the same flags; it auto-creates the design-block table.
- kicad-cli rewrites `kicad_common.json` on exit -> runs are serialised per root and the seed is
  restored afterwards; 8.0.1/9.0.5 segfaulted on the very first ERC against a brand-new root ->
  every root is pre-warmed once with a trivial command.
- Library seed: `<install>/share/kicad/template/*-lib-table` first, then rows merged read-only
  from the user's `<user_kicad_config_home>/<major.minor>/{sym,fp}-lib-table` (decision D7),
  env vars (`environment.vars`) copied from the user's `kicad_common.json` of that version.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from kiwrap.installation import KiCadInstallation


@dataclass(frozen=True)
class ConfigRoot:
    install: KiCadInstallation
    path: Path  # the KICAD_CONFIG_HOME value (root); KiCad uses path/<major.minor>/
    documents_home: Path  # KICAD_DOCUMENTS_HOME (plugins, templates, 3rd-party)
    cache_home: Path  # KICAD_CACHE_HOME

    @property
    def versioned(self) -> Path:
        return self.path / self.install.major_minor

    def env(self, base: dict[str, str] | None = None) -> dict[str, str]:
        """Environment for a KiCad process: base (or os.environ) + KICAD_CONFIG_HOME/DOCUMENTS/CACHE
        + KICAD<major>_* library vars + short TEMP/TMP (Windows) so IPC socket paths stay short."""
        raise NotImplementedError

    @contextmanager
    def lock(self, timeout_s: float = 300) -> Iterator[None]:
        """Serialise KiCad processes against this root (cross-process file lock)."""
        raise NotImplementedError
        yield  # pragma: no cover

    def restore_seed(self) -> None:
        """Put `kicad_common.json` and the tables back to the seeded content (kicad-cli rewrites them)."""
        raise NotImplementedError


def ensure(
    install: KiCadInstallation,
    *,
    storage_home: Path | None = None,
    merge_user_tables: bool = True,
    enable_api_server: bool = False,
    prewarm: bool = True,
) -> ConfigRoot:
    """Create (idempotently) and return the sandbox root for `install`.

    `enable_api_server=True` is only used for live GUI sessions; kicad-cli roots keep it false.
    """
    raise NotImplementedError


def user_config_hash(install: KiCadInstallation) -> str:
    """SHA-256 over the user's real config dir for this major.minor (for the never-modified assertion)."""
    raise NotImplementedError
