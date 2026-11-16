"""Reuse read-only ordinary-stand fingerprints for Step 5 evidence."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from qa.tools.preserve_learning import require_ordinary_environment, snapshot, compare
from src.qa_profile import checked_path


def artifact(value):
    path = checked_path(ROOT / value)
    relative = path.relative_to(ROOT / 'verification')
    if len(relative.parts) < 2 or not relative.parts[0].startswith('qa-step-05-') or path.suffix != '.json':
        raise ValueError('QA_STEP_05_JSON_REQUIRED')
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    parser.add_argument('--compare')
    args = parser.parse_args()
    require_ordinary_environment()
    output = artifact(args.output)
    if output.exists():
        raise FileExistsError('QA_EVIDENCE_ALREADY_EXISTS')
    previous = json.loads(artifact(args.compare).read_text(encoding='utf-8')) if args.compare else None
    result = snapshot()
    if previous is not None:
        result['comparison'] = compare(previous, result)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    print(json.dumps(dict(fully_observed=result['fully_observed'], comparison=result.get('comparison'))))
    return 0 if result['fully_observed'] and (previous is None or result['comparison']['verdict'] == 'UNCHANGED') else 1


if __name__ == '__main__':
    raise SystemExit(main())
