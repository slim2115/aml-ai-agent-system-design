"""Hash an explicitly resolved Windows CI wheel set; no install or network."""
import argparse
from email.parser import BytesParser
from hashlib import sha256
from pathlib import Path
import re
import zipfile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--wheel-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    locked = {}
    for wheel in sorted(args.wheel_dir.glob('*.whl')):
        with zipfile.ZipFile(wheel) as archive:
            metadata_paths = [name for name in archive.namelist() if name.endswith('.dist-info/METADATA')]
            if len(metadata_paths) != 1:
                raise ValueError('Wheel must contain exactly one distribution metadata file')
            metadata = BytesParser().parsebytes(archive.read(metadata_paths[0]))
        name = re.sub(r'[-_.]+', '-', metadata['Name']).lower()
        if name in locked:
            raise ValueError('Duplicate distribution: ' + name)
        locked[name] = f"{name}=={metadata['Version']} --hash=sha256:{sha256(wheel.read_bytes()).hexdigest()}"
    if not locked:
        raise ValueError('Empty wheel set')
    header = '# Windows x64 / CPython 3.12 CI only; includes UI and API test dependencies.\n'
    header += '# Generated from resolved wheels with scripts/ci/lock_ci_wheels.py; install --require-hashes.\n'
    args.output.write_text(header + '\n'.join(locked[name] for name in sorted(locked)) + '\n', encoding='utf-8')
    print(f'Locked {len(locked)} distributions in {args.output}')


if __name__ == '__main__':
    main()
