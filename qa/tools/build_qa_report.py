"""Build a new offline QA report from an explicit hash-pinned index."""
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from qa.reporting import build_report, checked, strict_json, write_report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--index', required=True)
    parser.add_argument('--output-dir', required=True)
    args = parser.parse_args()
    index = strict_json(checked(ROOT, args.index).read_text(encoding='utf-8-sig'))
    report = build_report(index)
    code = write_report(report, index, args.output_dir)
    print('QA report: ' + report['status'] + '; product acceptance INCOMPLETE')
    return code


if __name__ == '__main__':
    raise SystemExit(main())
