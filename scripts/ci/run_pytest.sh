#!/usr/bin/env bash
# CI wrapper around `uv run pytest`: pytest exits 5 when it collected no tests, which is the expected state
# of a component whose tests have not landed yet, so 5 is mapped to 0 here; every other exit code passes
# through (1 = failures, 2 = interrupted, 3 = internal error, 4 = usage error).
# Usage: bash scripts/ci/run_pytest.sh <pytest args...>
set -uo pipefail
uv run --no-sync pytest "$@"
rc=$?
if [ "$rc" -eq 5 ]; then
  echo "run_pytest.sh: no tests collected for '$*' (pytest exit 5) - treated as success"
  rc=0
fi
exit "$rc"
