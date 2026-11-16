"""Fallback for a broken Node download path: curl official archives, then original installer.

This does not extract archives or mark browsers installed. Playwright still installs and
verifies its own pinned browsers, served from a temporary loopback-only archive cache.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from qa.tools import browser_tooling as tooling


def reuse_downloads(manifest: Path, sources: list[tuple[str, str]]) -> list[dict]:
    """Reuse only the recorded official archives after checking every byte again."""
    manifest = tooling._checked(manifest)
    allowed = tooling.ROOT / 'verification/qa-step-04-2026-10-14/tooling'
    if not manifest.is_relative_to(allowed) or manifest.name != 'downloads.json':
        raise ValueError('Reuse requires a prior step-04 downloads.json')
    record = json.loads(manifest.read_text(encoding='utf-8'))
    if record.get('schema_version') != '1.0' or record.get('packages') != tooling._pins():
        raise ValueError('Download manifest does not match the installed package lock')
    rows = record.get('downloads', [])
    if not isinstance(rows, list) or len(rows) != len(sources):
        raise ValueError('Download manifest must contain each expected archive exactly once')
    expected = dict(sources)
    checked = []
    for row in rows:
        remote_path = row['installer_path']
        if remote_path not in expected or row['source_url'] != expected.pop(remote_path):
            raise ValueError('Unexpected or duplicate archive source')
        effective = urlsplit(row['effective_url'])
        if (effective.scheme != 'https' or effective.hostname not in
                ('storage.googleapis.com', 'cdn.playwright.dev', 'playwright.download.prss.microsoft.com')
                or effective.username or effective.password or effective.port not in (None, 443)):
            raise ValueError('Archive redirect was not an official HTTPS source')
        archive = tooling._checked(tooling.ROOT / row['local_file'])
        if archive.parent != manifest.parent / 'archives' or archive.name != Path(remote_path).name:
            raise ValueError('Archive must belong to the supplied manifest')
        if not archive.is_file() or archive.stat().st_size != row['size_bytes']:
            raise ValueError('Archive is missing or changed size')
        with archive.open('rb') as handle:
            digest = hashlib.file_digest(handle, 'sha256').hexdigest()
        if digest != row['sha256']:
            raise ValueError('Archive checksum mismatch')
        checked.append(dict(row))
    return checked


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', required=True)
    parser.add_argument('--reuse-downloads', help='Prior local downloads.json; all hashes are checked again')
    args = parser.parse_args()
    tooling.activate()
    output = tooling._checked(tooling.ROOT / args.output_dir)
    if not output.is_relative_to(tooling.ROOT / 'verification/qa-step-04-2026-10-14/tooling'):
        raise ValueError('Use a new step-04 tooling evidence directory')
    output.mkdir(parents=True, exist_ok=False)
    metadata = json.loads((tooling.TOOL / 'playwright/driver/package/browsers.json').read_text())
    browsers = {row['name']: row for row in metadata['browsers']}
    version = browsers['chromium']['browserVersion']
    if browsers['chromium-headless-shell']['browserVersion'] != version:
        raise ValueError('Browser version mismatch')
    sources = [
        (f'builds/cft/{version}/win64/chrome-win64.zip',
         f'https://storage.googleapis.com/chrome-for-testing-public/{version}/win64/chrome-win64.zip'),
        (f'builds/cft/{version}/win64/chrome-headless-shell-win64.zip',
         f'https://storage.googleapis.com/chrome-for-testing-public/{version}/win64/chrome-headless-shell-win64.zip'),
    ]
    for name in ('ffmpeg', 'winldd'):
        path = f"builds/{name}/{browsers[name]['revision']}/{name}-win64.zip"
        sources.append((path, f'https://cdn.playwright.dev/dbazure/download/playwright/{path}'))
    cache = output / 'archives'
    cache.mkdir()

    def download(source: tuple[str, str]) -> dict:
        remote_path, url = source
        destination = cache / Path(remote_path).name
        headers = destination.with_suffix('.headers.txt')
        result = subprocess.run([
            'curl.exe', '--fail', '--location', '--silent', '--show-error',
            '--proto', '=https', '--proto-redir', '=https',
            '--connect-timeout', '10', '--max-time', '300',
            '--dump-header', str(headers), '--output', str(destination),
            '--write-out', '%{url_effective}', url,
        ], check=True, capture_output=True, text=True, timeout=320)
        return {'installer_path': remote_path, 'source_url': url,
                'effective_url': result.stdout.strip(), 'size_bytes': destination.stat().st_size,
                'sha256': hashlib.sha256(destination.read_bytes()).hexdigest(),
                'local_file': str(destination.relative_to(tooling.ROOT))}

    if args.reuse_downloads:
        downloads = reuse_downloads(tooling.ROOT / args.reuse_downloads, sources)
    else:
        with ThreadPoolExecutor(max_workers=3) as pool:
            downloads = list(pool.map(download, sources))
    tooling._write_new(output / 'downloads.json', {
        'schema_version': '1.0', 'recorded_at_utc': datetime.now(timezone.utc).isoformat(),
        'mechanism': 'curl HTTPS official archives; Playwright original installer reads exact bytes via temporary loopback cache',
        'packages': tooling._pins(), 'downloads': downloads,
        'checksum_scope': 'Locally computed SHA256 for reproducibility; HTTPS provides source authentication',
        'reused_manifest': args.reuse_downloads,
    })
    routes = {'/' + item['installer_path']: tooling.ROOT / item['local_file'] for item in downloads}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = routes.get(self.path)
            if path is None:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header('Content-Type', 'application/zip')
            self.send_header('Content-Length', str(path.stat().st_size))
            self.end_headers()
            with path.open('rb') as file:
                while block := file.read(1024 * 1024):
                    self.wfile.write(block)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    env = tooling._child_env()
    env['PLAYWRIGHT_DOWNLOAD_HOST'] = f'http://127.0.0.1:{server.server_port}'
    env.pop('PLAYWRIGHT_CHROMIUM_DOWNLOAD_HOST', None)
    env['PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT'] = '30000'
    try:
        with (output / 'installer.log').open('w', encoding='utf-8') as log:
            result = subprocess.run([
                sys.executable, '-I', '-B', str(Path(tooling.__file__)), 'install',
                '--output-dir', str(output / 'installation'),
            ], env=env, cwd=tooling.ROOT, stdout=log, stderr=subprocess.STDOUT, timeout=600)
        print(json.dumps({'exit_code': result.returncode, 'output_dir': str(output)}))
        return result.returncode
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


if __name__ == '__main__':
    raise SystemExit(main())
