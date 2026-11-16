"""Explicit local SQL-lab bootstrap, status and practice reset commands."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.lab.database import LabDatabase, LabDatabaseError, bootstrap, reset_practice


def main() -> int:
    parser = argparse.ArgumentParser(description='Local synthetic AML PostgreSQL lab')
    parser.add_argument('action', choices=('bootstrap', 'status', 'reset-practice'))
    parser.add_argument('--confirm-practice-reset', action='store_true',
                        help='Explicitly authorize erasing practice tables and notes only')
    args = parser.parse_args()
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    try:
        if args.action == 'reset-practice':
            if not args.confirm_practice_reset:
                parser.error('reset-practice requires --confirm-practice-reset')
            result = reset_practice()
        elif args.action == 'bootstrap':
            result = bootstrap()
        else:
            result = LabDatabase().status()
        print(json.dumps({'status': 'ok', **result}, ensure_ascii=False, sort_keys=True))
        return 0
    except LabDatabaseError as exc:
        print(json.dumps({'status': 'error', 'code': exc.code}))
        return 1
    except Exception:
        print(json.dumps({'status': 'error', 'code': 'INTERNAL_ERROR'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
