#!/usr/bin/env bash
# Sync the project environment WITHOUT building kicad-python from git: install everything else from
# the lock, then the prebuilt, repaired kicad-python wheel from ./wheelhouse (built once by the
# kicad-python-wheel CI job / scripts/ci/build_kicad_python_wheel.py). Building from git clones the
# KiCad source tree as a 3.5 GB submodule in every job (reviewer finding 5, 2026-09-19).
#
# Every later `uv run` must not re-sync the environment (that would put the git build back):
# CI exports UV_NO_SYNC=1 for the whole workflow; locally, use `uv run --no-sync`.
set -euo pipefail
cd "$(dirname "$0")/../.."
if ! compgen -G "wheelhouse/kicad_python-*.whl" > /dev/null; then
  echo "no wheelhouse/kicad_python-*.whl: run 'python scripts/ci/build_kicad_python_wheel.py --out wheelhouse'" >&2
  exit 1
fi
uv sync --all-extras --frozen --no-install-package kicad-python
py="$(uv run --no-sync python -c 'import sys; print(sys.executable)')"
uv pip install --python "$py" --no-deps wheelhouse/kicad_python-*.whl
uv run --no-sync python -c "import kipy.kicad_api_version as v; from kipy import KiCad; print('kicad-python', v.KICAD_API_VERSION)"
