"""Repeat existing UI controls without changing sealed block-05 evidence."""
from datetime import datetime, timezone
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def main():
    from scripts.rebuild import qualify_workflow_ui

    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    qualify_workflow_ui.OUTPUT = ROOT / 'verification/readiness-2026-09-30' / ('ui-controls-' + stamp)
    return qualify_workflow_ui.main()


if __name__ == '__main__':
    raise SystemExit(main())
