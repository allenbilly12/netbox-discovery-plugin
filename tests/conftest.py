"""
pytest configuration for the no-Django test harness.

Guarantees the repo root is importable so `from tests._loader import ...`
resolves regardless of the directory pytest was invoked from.
"""

import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
