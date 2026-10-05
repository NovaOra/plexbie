# path: tests/conftest.py
"""Make the project importable and give Config a usable token.

Doubles as plain-import setup so `python tests/run_all.py` works without pytest.
"""
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Config validates that a Discord token is present, so supply a dummy one
# before anything constructs a Config(). Never read a real config/.env here.
os.environ.setdefault("DISCORD_BOT_TOKEN", "x" * 60)
