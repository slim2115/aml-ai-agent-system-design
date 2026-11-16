"""Run the fixed synthetic environment schedules with owned local evidence."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from qa.environments.runner import main

if __name__ == '__main__':
    raise SystemExit(main())
