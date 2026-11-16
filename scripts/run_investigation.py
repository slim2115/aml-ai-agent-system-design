"""Print one v1 result. Explicit demo runs persist a managed local workflow."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    # Stable JSON encoding even when PowerShell redirects a Windows code page.
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('incident_id', nargs='?')
    parser.add_argument('--demo', action='store_true', help='Synthetic historical replay; not an AML legal conclusion')
    parser.add_argument('--engine', choices=['template', 'local-model'], default='template')
    args = parser.parse_args()
    if args.demo:
        from scripts.run_workflow import run_one, safe_error
        from src.workflow.store import WorkflowError
        try:
            job = run_one(args.incident_id, engine=args.engine)
            result = job.get('result') or safe_error('DEADLINE_EXCEEDED' if job['status'] == 'timed_out' else 'INVALID_STATE')
        except WorkflowError as exc:
            result = safe_error(exc.code)
        except Exception:
            result = safe_error('INTERNAL_ERROR')
    else:
        from src.runtime.service import AMLAgent
        result = AMLAgent().run(args.incident_id)
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return {'draft': 0, 'refusal': 2, 'error': 3}[result['status']]


if __name__ == '__main__':
    raise SystemExit(main())
