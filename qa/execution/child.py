"""Private startup barrier for the owned QA process runner."""
from pathlib import Path
import runpy
import sys


def main():
    if sys.stdin.buffer.readline(16) != b'RUN\n':
        raise SystemExit('EXECUTION_START_BARRIER_REQUIRED')
    if len(sys.argv) < 2:
        raise SystemExit('EXECUTION_TARGET_REQUIRED')
    target = Path(sys.argv[1]).absolute()
    sys.argv = [str(target), *sys.argv[2:]]
    # Match normal script execution; the explicit -I parent excludes ambient
    # Python paths. Target wrappers insert their reviewed project root.
    runpy.run_path(str(target), run_name='__main__')


if __name__ == '__main__':
    main()
