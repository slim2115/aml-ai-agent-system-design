"""Run the statistics/budget lesson or reanalyse saved synthetic LLM outputs."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from qa.stability.runner import main

if __name__ == '__main__':
    raise SystemExit(main())
