"""Unit-test-only fixtures. The session-wide hooks and the shared discovery fixtures (`installs`,
`install10`, `newest`, `storage_home`, `config_root`, `cli_installs`, `corpus`, the parametrised
`install`, the `kicad_patch` marker skip and `patched_installs`) live in the repository root
`conftest.py` so they also cover `conformance/tests` and `scripts/ci/tests_ci` (pytest loads this
file only when something under `tests/` is collected); KiCad-dependent tests auto-skip when no
install is discovered.
"""

from __future__ import annotations

from tests._fakes import cli_envs, cli_scratch, user_config_untouched  # noqa: F401 - session fixtures
