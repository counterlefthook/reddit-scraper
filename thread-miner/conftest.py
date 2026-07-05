"""Make the repo root importable in tests (for the non-installed webapp package)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
