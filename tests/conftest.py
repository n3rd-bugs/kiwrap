"""Unit-test-only fixtures. The session-wide hooks and the shared discovery fixtures (`installs`,
`install10`, `newest`, `storage_home`, `config_root`, `cli_installs`, `corpus`, the parametrised
`install`) live in the repository root `conftest.py` so they also cover `conformance/tests` and
`scripts/ci/tests_ci`; KiCad-dependent tests auto-skip when no install is discovered.
"""

from __future__ import annotations

from tests._fakes import cli_envs, cli_scratch, user_config_untouched  # noqa: F401 - session fixtures
