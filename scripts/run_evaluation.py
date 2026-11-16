"""Offline replay CLI. No product, model, database or network imports."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if __package__ in (None, ''):
    sys.path.insert(0, str(PROJECT_ROOT))
from src.evaluation import BundleInputError, evaluate_bundle, quality_gates
from src.config import get_settings

EXIT_CODES = {'PASS': 0, 'FAIL': 1, 'INCOMPLETE': 2, 'ERROR': 3}


def generate_report(report):
    """Render safe metadata, never rejected payloads or exceptions."""
    lines = ['# Evaluation report', '', f"Verdict: **{report['verdict']}**", '',
             'Supplied replay evidence only; the product is not invoked.', '']
    if report.get('error_code'):
        lines.extend([f"Error: `{report['error_code']}`", ''])
    lines.extend(['| Gate | Value | Threshold | Status |', '| --- | ---: | ---: | --- |'])
    for item in report.get('gates', []):
        value = 'not_measured' if item['value'] is None else repr(item['value'])
        threshold = '' if item['threshold'] is None else f"{item['op']} {item['threshold']}"
        lines.append(f"| {item['name']} | {value} | {threshold} | {item['status']} |")
    lines.extend(['', '| Metric | Numerator | Denominator | Measurement |', '| --- | ---: | ---: | --- |'])
    for name, metric in report.get('metrics', {}).items():
        lines.append(f"| {name} | {metric.get('numerator')} | {metric['denominator']} | {metric['status']} |")
    lines.extend(['', '| Case | Expected | Actual | Issues / missing measurements |', '| --- | --- | --- | --- |'])
    for row in report.get('cases', []):
        issues = ', '.join([*row['issues'], *row['missing_measurements']]) or 'none'
        lines.append(f"| {row['case_id']} | {row['expected_status']} | {row['actual_status']} | {issues} |")
    lines.extend(['', 'Limitations:', ''])
    lines.extend(f'- {item}' for item in report.get('limitations', []))
    return '\n'.join(lines) + '\n'


def _same_path(first, second):
    return first == second or (first.exists() and second.exists() and first.samefile(second))


def _output_paths(value, input_path=None):
    project = PROJECT_ROOT.resolve()
    report_root = project / 'var' / 'evaluation'
    # A redirected report directory must not broaden the permitted write area.
    if report_root.resolve() != report_root:
        raise BundleInputError('UNSAFE_OUTPUT_PATH')
    path = Path(value)
    path = project / path if not path.is_absolute() else path
    if path.suffix.lower() != '.md':
        raise BundleInputError('OUTPUT_MUST_BE_MARKDOWN')
    destinations = (path.resolve(), path.with_suffix('.json').resolve())
    for destination, suffix in zip(destinations, ('.md', '.json')):
        try:
            relative = destination.relative_to(report_root)
        except ValueError:
            raise BundleInputError('OUTPUT_OUTSIDE_EVALUATION_DIRECTORY') from None
        if (not relative.parts or destination.suffix.lower() != suffix
                or any(part.lower() in {'.git', '.env', '.agents', '.codex'} for part in relative.parts)):
            raise BundleInputError('UNSAFE_OUTPUT_PATH')
        if input_path is not None and _same_path(destination, input_path.resolve()):
            raise BundleInputError('OUTPUT_OVERLAPS_INPUT')
        if destination.exists() and destination.stat().st_nlink > 1:
            raise BundleInputError('UNSAFE_OUTPUT_PATH')
    if _same_path(*destinations):
        raise BundleInputError('UNSAFE_OUTPUT_PATH')
    return destinations


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's default error includes rejected argument values.
        raise BundleInputError('INVALID_ARGUMENTS')


def _error(code):
    print(f'ERROR: {code}', file=sys.stderr)
    return EXIT_CODES['ERROR']


def _reject_constant(_):
    raise ValueError('NONFINITE_JSON')


def _unique_object(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError('DUPLICATE_JSON_KEY')
        obj[key] = value
    return obj


def main(argv=None):
    parser = _SafeArgumentParser(description=__doc__)
    parser.add_argument('--input', help='Strict replay bundle JSON 1.0')
    parser.add_argument('--output', default='var/evaluation/report.md', help='Markdown output inside var/evaluation')
    parser.add_argument('--no-llm', action='store_true', help='Without input produces INCOMPLETE')
    try:
        args = parser.parse_args(argv)
    except BundleInputError:
        return _error('INVALID_ARGUMENTS')
    except SystemExit as exc:
        return 0 if exc.code == 0 else _error('INVALID_ARGUMENTS')
    try:
        input_path = Path(args.input) if args.input else None
        output, json_output = _output_paths(args.output, input_path)
    except BundleInputError as exc:
        return _error(str(exc))
    except (OSError, ValueError, RuntimeError):
        return _error('INVALID_OUTPUT_PATH')
    try:
        if input_path is not None:
            bundle = json.loads(input_path.read_text(encoding='utf-8'),
                                parse_constant=_reject_constant, object_pairs_hook=_unique_object)
        else:
            bundle = {'schema_version': '1.0', 'dataset': {'id': 'no-input', 'kind': 'synthetic_control'},
                      'cases': [], 'outcomes': []}
        report = evaluate_bundle(bundle, settings=get_settings())
    except BundleInputError:
        report = {'schema_version': '1.0', 'verdict': 'ERROR', 'error_code': 'INVALID_REPLAY_BUNDLE'}
    except Exception:
        report = {'schema_version': '1.0', 'verdict': 'ERROR', 'error_code': 'INPUT_READ_OR_EVALUATION_ERROR'}
    try:
        markdown = generate_report(report)
        serialized = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
        output.parent.mkdir(parents=True, exist_ok=True)
        json_output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(markdown, encoding='utf-8')
        json_output.write_text(serialized, encoding='utf-8')
    except Exception:
        return _error('REPORT_WRITE_FAILED')
    print(f"Evaluation: {report['verdict']}")
    return EXIT_CODES[report['verdict']]


if __name__ == '__main__':
    raise SystemExit(main())
