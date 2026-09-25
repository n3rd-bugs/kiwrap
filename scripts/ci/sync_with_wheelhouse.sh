#!/usr/bin/env bash
# Sync the project environment WITHOUT building kicad-python from git: install everything else from
# the lock, then the prebuilt, repaired kicad-python wheel from ./wheelhouse (built once by the
# kicad-python-wheel CI job / scripts/ci/build_kicad_python_wheel.py). Building from git clones the
# KiCad source tree as a 3.5 GB submodule in every job (reviewer finding 5, 2026-09-19).
#
# Every later `uv run` must not re-sync the environment (that would put the git build back):
# CI exports UV_NO_SYNC=1 for the whole workflow; locally, use `uv run --no-sync`.
#
# Environment: KIWRAP_WHEELHOUSE (default: wheelhouse, relative to the repository root) - the
# directory holding exactly one kicad_python-*.whl; UV_PROJECT_ENVIRONMENT (uv's own) to sync a venv
# other than .venv (how the S2 CI fix was reproduced on a developer machine without touching .venv).
set -euo pipefail
cd "$(dirname "$0")/../.."
wheelhouse="${KIWRAP_WHEELHOUSE:-wheelhouse}"
shopt -s nullglob
wheels=("$wheelhouse"/kicad_python-*.whl)
shopt -u nullglob
if [ "${#wheels[@]}" -ne 1 ]; then
  echo "expected exactly one $wheelhouse/kicad_python-*.whl, found ${#wheels[@]}: ${wheels[*]:-none}" >&2
  echo "build it with 'python scripts/ci/build_kicad_python_wheel.py --out $wheelhouse'" >&2
  exit 1
fi
uv sync --all-extras --frozen --no-install-package kicad-python
py="$(uv run --no-sync python -c 'import sys; print(sys.executable)')"
uv pip install --python "$py" --no-deps "${wheels[0]}"
uv run --no-sync python -c "import kipy.kicad_api_version as v; from kipy import KiCad; print('kicad-python', v.KICAD_API_VERSION)"
