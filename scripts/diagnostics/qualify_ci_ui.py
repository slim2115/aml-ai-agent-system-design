"""Run UI/SQLite/CLI qualification into CI output, preserving historical evidence."""
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output-dir', default='var/checks/ci/ui')
    args = parser.parse_args()
    directory = (ROOT / args.output_dir).resolve()
    directory.relative_to(ROOT)
    if not directory.is_relative_to(ROOT / 'var'):
        raise ValueError('CI UI output must stay under var')
    from scripts.rebuild import qualify_workflow_ui
    qualify_workflow_ui.OUTPUT = directory
    return qualify_workflow_ui.main()


if __name__ == '__main__':
    raise SystemExit(main())
