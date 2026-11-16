"""Always-run CI summary using only stdlib; missing stages cannot become PASS."""
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from qa.reporting import build_report, checked, pin, strict_json, write_report


def ci_index(profile, input_dir, root=ROOT):
    checked(root, input_dir)
    def spec(identifier, kind, paths):
        return dict(id=identifier, kind=kind, files={role: pin(root, name) for role, name in paths.items()})
    if profile == 'hermetic':
        profiles = [spec('ci-inputs', 'checks', {'result': input_dir + '/ci-inputs.json'}),
            spec('regression', 'unit', {'result': input_dir + '/unit-verification.json', 'junit': input_dir + '/unit-tests.xml'}),
            spec('operator-apptest', 'checks', {'result': input_dir + '/ui/ui-cli-qualification.json'})]
    elif profile == 'browser':
        profiles = [spec('browser-lifecycle', 'browser_ci', {'result': input_dir + '/result.json'})]
        path = checked(root, input_dir + '/result.json')
        candidate = None
        try:
            candidate = strict_json(path.read_text(encoding='utf-8-sig')).get('browser_result_path')
        except (OSError, ValueError, AttributeError):
            pass
        if not isinstance(candidate, str) or not candidate.startswith('var/qa/runs/') or not candidate.endswith('/result.json'):
            candidate = input_dir + '/missing-browser/result.json'
        base = candidate.rsplit('/', 1)[0]
        profiles.append(spec('chromium-smoke', 'browser', {'result': candidate, 'junit': base + '/tests.xml', 'browser': base + '/browser.json'}))
    else:
        raise ValueError('REPORT_CI_PROFILE_INVALID')
    return dict(schema_version='1.0', scope='ci-' + profile, profiles=profiles)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', choices=('hermetic', 'browser'), required=True)
    parser.add_argument('--input-dir', required=True)
    parser.add_argument('--output-dir', required=True)
    args = parser.parse_args()
    index = ci_index(args.profile, args.input_dir)
    report = build_report(index)
    code = write_report(report, index, args.output_dir)
    print('CI profile: ' + report['status'] + '; remote execution is separately verified in GitHub Actions')
    return code


if __name__ == '__main__':
    raise SystemExit(main())
