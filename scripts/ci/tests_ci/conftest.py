"""Make `scripts/ci/*.py` importable as modules for their tests (they are scripts, not a package)."""

from __future__ import annotations

import sys
from pathlib import Path

CI_DIR = Path(__file__).resolve().parents[1]
if str(CI_DIR) not in sys.path:
    sys.path.insert(0, str(CI_DIR))
