"""Create platform-specific hash locks from resolved wheel metadata (no installs)."""
from email.parser import BytesParser
from pathlib import Path
import hashlib
import json
import sys
import zipfile
from datetime import datetime, timezone
from pip._vendor.packaging.requirements import Requirement
from pip._vendor.packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parents[2]
WHEELS = ROOT / 'var/wheelhouse/windows-py312'


def main():
    if sys.version_info[:2] != (3, 12) or sys.platform != 'win32':
        raise SystemExit('This resolver profile requires Windows Python 3.12')
    packages = {}
    for path in sorted(WHEELS.glob('*.whl')):
        with zipfile.ZipFile(path) as archive:
            metadata_files = [name for name in archive.namelist() if name.endswith('.dist-info/METADATA') and len(name.split('/')) == 2]
            if len(metadata_files) != 1:
                raise ValueError(f'Invalid wheel metadata: {path.name}')
            metadata = BytesParser().parsebytes(archive.read(metadata_files[0]))
        name = canonicalize_name(metadata['Name'])
        if name in packages:
            raise ValueError(f'Multiple resolved wheels for {name}')
        with path.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        packages[name] = {'name': name, 'version': metadata['Version'], 'file': path.name,
                          'sha256': digest,
                          'requires': metadata.get_all('Requires-Dist', [])}

    def closure(input_files):
        pending = []
        for file in input_files:
            for line in file.read_text(encoding='utf-8').splitlines():
                if line.strip() and not line.startswith('#'):
                    pending.append(Requirement(line))
        selected = set()
        visited = set()
        while pending:
            requirement = pending.pop()
            name = canonicalize_name(requirement.name)
            extras = tuple(sorted(requirement.extras))
            if (name, extras) in visited:
                continue
            visited.add((name, extras))
            package = packages[name]
            if not requirement.specifier.contains(package['version']):
                raise ValueError(f'Resolved version mismatch for {name}')
            selected.add(name)
            for raw in package['requires']:
                dependency = Requirement(raw)
                if dependency.marker is None or any(dependency.marker.evaluate({'extra': extra}) for extra in ('', *extras)):
                    pending.append(dependency)
        return selected

    unit = closure([ROOT / 'requirements/unit.in'])
    all_packages = closure([ROOT / 'requirements/runtime.in', ROOT / 'requirements/unit.in'])
    for filename, names in [('windows-py312.lock', all_packages), ('unit-windows-py312.lock', unit)]:
        lines = ['# Generated for Windows x64 / CPython 3.12 from resolved binary wheels.',
                 '# Install with pip --require-hashes; this file is not a Linux lock.']
        for name in sorted(names):
            record = packages[name]
            marker = '; sys_platform == "win32"' if name in ('pywin32', 'colorama') else ''
            lines.append(f"{name}=={record['version']}{marker} --hash=sha256:{record['sha256']}")
        (ROOT / 'requirements' / filename).write_text('\n'.join(lines) + '\n', encoding='utf-8')
    manifest = {'generated_at_utc': datetime.now(timezone.utc).isoformat(), 'profile': 'windows-x64-cpython312',
                'full_packages': len(all_packages), 'unit_packages': len(unit),
                'files': [{key: value for key, value in packages[name].items() if key != 'requires'} for name in sorted(all_packages)]}
    (ROOT / 'verification/block-03/wheel-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    print(f'Locked {len(all_packages)} full / {len(unit)} unit packages')


if __name__ == '__main__':
    main()
