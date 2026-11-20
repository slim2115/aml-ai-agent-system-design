"""Run a bounded candidate response experiment in its own reviewed worker."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from qa.autonomy.runner import main

if __name__ == '__main__':
    raise SystemExit(main())
