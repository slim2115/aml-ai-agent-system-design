"""Explicit hermetic regression with branch coverage; never starts live services.

Uses the existing configured tests unchanged. Child processes are NOT measured;
the report is an in-process/thread diagnostic, not a complete system coverage.
The pinned coverage package lives outside the application's environment.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import runpy
import sys

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / 'var/qa/tooling/coverage-7.16.2'
CRITICAL = {
    'src/workflow/store.py': 'admission, idempotency, report ownership, decisions, revision conflicts',
    'src/workflow/controller.py': 'controller ownership, admission, cancellation, deadlines',
    'src/workflow/delivery.py': 'approval gates, frozen report, local export and retry',
    'src/workflow/sources.py': 'source ownership and report evidence resolution',
    'src/workflow/remote.py': 'fixed-loopback API, protocol checks, shared UI history',
    'src/runtime/generation.py': 'exact factual catalog, valid permutations, publication guards',
    'src/runtime/context.py': 'source snapshot and consistency',
    'src/lab/kafka/analysis_store.py': 'durable intake and monotonic result projection',
    'src/lab/kafka/analysis_consumer.py': 'ACK ordering, DLQ, retry and scheduling',
    'src/evaluation/core.py': 'measurement completeness and evidence integrity',
    'src/evaluation/gates.py': 'PASS/FAIL/INCOMPLETE and mandatory thresholds',
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', required=True)
    args = parser.parse_args()
    output = (ROOT / args.output_dir).resolve()
    relative = output.relative_to(ROOT)
    if not ((len(relative.parts) >= 2 and relative.parts[0] == 'verification'
             and relative.parts[1].startswith('qa-step-')) or output.is_relative_to(ROOT / 'var/qa')):
        raise ValueError('Use verification/qa-step-... or var/qa output')
    if output.exists():
        raise ValueError('Use a new output directory; prior evidence is never overwritten')
    if not (TOOL / 'coverage/__init__.py').is_file() or TOOL.resolve() != TOOL:
        raise ValueError('Install the pinned metrics tool in var/qa/tooling first')
    output.mkdir(parents=True)
    sys.path.insert(0, str(TOOL))
    import coverage
    if coverage.__version__ != '7.16.2' or not Path(coverage.__file__).resolve().is_relative_to(TOOL):
        raise ValueError('Unexpected measurement tool')
    # These values are process-local; no settings or secrets are serialized.
    os.environ.update({'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1', 'PYTHONDONTWRITEBYTECODE': '1',
        'PYTHON_DOTENV_DISABLED': '1', 'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1',
        'HF_HUB_DISABLE_TELEMETRY': '1', 'ANONYMIZED_TELEMETRY': 'FALSE',
        'OTEL_SDK_DISABLED': 'true', 'LANGSMITH_TRACING': 'false', 'LANGCHAIN_TRACING_V2': 'false'})
    os.chdir(ROOT)
    cov = coverage.Coverage(data_file=str(output / '.coverage'), branch=True, config_file=False,
        source=[str(ROOT / 'src' / name) for name in ('workflow', 'runtime', 'lab/kafka', 'evaluation')])
    code = 3
    cov.start()
    try:
        sys.argv = ['run_checks.py', '--output-dir', str(output / 'regression')]
        runpy.run_path(str(ROOT / 'scripts/rebuild/run_checks.py'), run_name='__main__')
        code = 0
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 3
    finally:
        cov.stop()
        cov.save()
        cov.json_report(outfile=str(output / 'coverage.json'))
        cov.xml_report(outfile=str(output / 'coverage.xml'))
        cov.html_report(directory=str(output / 'html'))
        measured = json.loads((output / 'coverage.json').read_text(encoding='utf-8'))
        files = {name.replace('\\', '/'): data for name, data in measured['files'].items()}
        critical = []
        for name, purpose in CRITICAL.items():
            data = files.get(name)
            item = {'path': name, 'purpose': purpose, 'measured': data is not None}
            if data:
                summary = data['summary']
                item.update(summary=summary, missing_lines=data['missing_lines'],
                            missing_branches=data['missing_branches'])
            critical.append(item)
        report = {'schema_version': '1.0', 'checked_at_utc': datetime.now(timezone.utc).isoformat(),
            'measurement_tool': 'coverage.py 7.16.2', 'regression_exit_code': code,
            'status': 'MEASURED' if all(x['measured'] for x in critical) else 'INCOMPLETE',
            'scope': 'In-process and threads in selected modules only; subprocess code is not traced. '
                     'Existing tests may use doubles. Percentages are diagnostic, not acceptance or model quality.',
            'coverage_threshold_imposed': False, 'totals': measured['totals'], 'critical_files': critical}
        (output / 'coverage-summary.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
        print(json.dumps({'measurement': report['status'], 'regression_exit_code': code,
                          'summary': str(output / 'coverage-summary.json')}))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
