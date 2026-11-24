"""Create a new local investigation pack; no network or models are required."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from qa.investigations.runner import main

if __name__ == '__main__':
    raise SystemExit(main())
