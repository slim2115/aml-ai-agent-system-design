"""Stage the exact official Swagger UI distribution assets, with npm integrity."""
import base64
from datetime import datetime, timezone
from hashlib import sha256, sha512
import io
import json
from pathlib import Path
import tarfile
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[2]
URL = 'https://registry.npmjs.org/swagger-ui-dist/-/swagger-ui-dist-5.33.0.tgz'
INTEGRITY = 'wpdK+m6BU5yj6pmUdMskZVTSWYG4DLglAx3sIhylloY37i8O37IrH+YEpqdXNfpaTGxILRBFzUqLF2jKqbfI7A=='
NAMES = ('swagger-ui-bundle.js', 'swagger-ui.css', 'favicon-32x32.png', 'LICENSE')


def main():
    archive = ROOT / 'var/lab-downloads/swagger-ui-dist-5.33.0.tgz'
    if not archive.exists():
        with urlopen(URL, timeout=60) as response:
            raw = response.read(20 * 1024 * 1024 + 1)
        if len(raw) > 20 * 1024 * 1024:
            raise ValueError('SWAGGER_DOWNLOAD_TOO_LARGE')
    else:
        raw = archive.read_bytes()
    if base64.b64encode(sha512(raw).digest()).decode() != INTEGRITY:
        raise ValueError('SWAGGER_INTEGRITY_MISMATCH')
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.write_bytes(raw)
    output = ROOT / 'lab/api/static'
    output.mkdir(parents=True, exist_ok=True)
    records = []
    with tarfile.open(fileobj=io.BytesIO(raw), mode='r:gz') as package:
        for name in NAMES:
            member = package.getmember('package/' + name)
            if not member.isfile() or member.size > 8 * 1024 * 1024:
                raise ValueError('INVALID_SWAGGER_ASSET')
            data = package.extractfile(member).read()
            (output / name).write_bytes(data)
            records.append({'path': 'lab/api/static/' + name, 'bytes': len(data), 'sha256': sha256(data).hexdigest()})
    report = {'checked_at_utc': datetime.now(timezone.utc).isoformat(), 'status': 'PASS', 'version': '5.33.0',
              'source': URL, 'npm_integrity': 'sha512-' + INTEGRITY, 'files': records,
              'scope': 'SHA512 compared with published npm distribution metadata; no claim of signature verification.'}
    evidence = ROOT / 'var/checks/lab'
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / 'swagger-assets.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print('Swagger UI 5.33.0: local assets verified.')


if __name__ == '__main__':
    main()
