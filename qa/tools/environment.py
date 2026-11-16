"""Process-local environment and bounded helpers shared by QA launchers."""
import json
import os
from pathlib import Path
import socket
import time
from urllib.request import build_opener, ProxyHandler, HTTPRedirectHandler, Request

from src.qa_profile import checked_path


def child_environment(profile):
    profile.validate()
    env = dict(os.environ)
    for key in ('PYTHONPATH', 'PYTHONHOME', 'DATABASE_URL', 'SQLALCHEMY_DATABASE_URI',
                'CHROMA_PERSIST_DIR', 'SYNTHETIC_DATA_DIR', 'KNOWLEDGE_BASE_COLLECTION',
                'TRANSFORMERS_CACHE', 'PYTORCH_TRANSFORMERS_CACHE', 'PYTORCH_PRETRAINED_BERT_CACHE',
                'HF_ASSETS_CACHE', 'HF_MODULES_CACHE'):
        env.pop(key, None)
    env.update({
        'AML_QA_RUN_ID': profile.run_id, 'AML_WORKFLOW_BACKEND': 'api',
        'PYTHON_DOTENV_DISABLED': '1', 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONNOUSERSITE': '1',
        'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1', 'HF_HUB_DISABLE_TELEMETRY': '1',
        'ANONYMIZED_TELEMETRY': 'FALSE', 'OTEL_SDK_DISABLED': 'true',
        'LANGSMITH_TRACING': 'false', 'LANGCHAIN_TRACING_V2': 'false', 'DO_NOT_TRACK': '1',
        'TEMP': str(profile.base / 'tmp'), 'TMP': str(profile.base / 'tmp'),
        'HF_HOME': str(profile.base / 'cache/hf'), 'HF_HUB_CACHE': str(profile.base / 'cache/hf/hub'),
        'HF_ASSETS_CACHE': str(profile.base / 'cache/hf/assets'),
        'HF_MODULES_CACHE': str(profile.base / 'cache/hf/modules'),
        'SENTENCE_TRANSFORMERS_HOME': str(profile.base / 'cache/hf/hub'),
        'TORCH_HOME': str(profile.base / 'cache/torch'), 'XDG_CACHE_HOME': str(profile.base / 'cache/xdg'),
        'MPLCONFIGDIR': str(profile.base / 'cache/matplotlib'),
        'CHROMA_PERSIST_DIR': str(profile.chroma_dir), 'SYNTHETIC_DATA_DIR': str(profile.seed_dir),
        'KNOWLEDGE_BASE_COLLECTION': 'aml_qa_' + profile.suffix,
        'OLLAMA_BASE_URL': 'http://127.0.0.1:11434', 'LLM_MODEL_NAME': 'qwen2.5:14b',
        'EMBEDDING_MODEL_NAME': 'sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2',
        'EMBEDDING_MODEL_PATH': str(profile.root / 'var/models/paraphrase-multilingual-MiniLM-L12-v2'),
        'EMBEDDING_MANIFEST_PATH': str(profile.root / 'var/models/paraphrase-multilingual-MiniLM-L12-v2.manifest.json'),
    })
    return env


def process_creation(pid):
    """Return the creation identity of a live Windows process, never signal it."""
    if type(pid) is not int or pid <= 0:
        return None
    if os.name != 'nt':
        raise RuntimeError('QA_LIFECYCLE_REQUIRES_WINDOWS')
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel.GetProcessTimes.argtypes = (wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4))
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return None
    try:
        status = wintypes.DWORD()
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(status)) or status.value != 259:
            return None
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
            return None
        return (times[0].dwHighDateTime << 32) + times[0].dwLowDateTime
    finally:
        kernel.CloseHandle(handle)


def same_process(record):
    observed = process_creation(record.get('pid'))
    return observed is not None and observed == record.get('process_creation')


def write_json(profile, path, value, *, exclusive=False):
    profile.validate()
    path = checked_path(Path(path), profile.root)
    if not (path.is_relative_to(profile.base) or path.is_relative_to(profile.workflow_dir)):
        raise ValueError('INVALID_QA_OUTPUT')
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(value, ensure_ascii=False, indent=2) + '\n'
    if exclusive:
        with path.open('x', encoding='utf-8') as stream:
            stream.write(content)
    else:
        temporary = checked_path(path.with_suffix('.tmp'), profile.root)
        temporary.write_text(content, encoding='utf-8')
        # Windows readers/antivirus may briefly hold a handle without
        # FILE_SHARE_DELETE. Keep the old complete JSON visible and bound the
        # retry to this one atomic replacement; never retry application work.
        deadline = time.monotonic() + 1
        while True:
            try:
                temporary.replace(path)
                break
            except PermissionError as exc:
                if getattr(exc, 'winerror', None) not in (5, 32) or time.monotonic() >= deadline:
                    raise
                time.sleep(.01)


def read_state(profile, service):
    if service not in ('api', 'ui', 'worker', 'review-worker'):
        raise ValueError('INVALID_QA_SERVICE')
    path = checked_path(profile.base / 'lab' / (service + '-process.json'), profile.root)
    if not path.exists():
        return None
    value = json.loads(path.read_text(encoding='utf-8-sig'))
    if (value.get('root') != str(profile.root) or value.get('qa_run_id') != profile.run_id
            or value.get('qa_owner') != profile.marker):
        raise ValueError('QA_PROCESS_OWNER_MISMATCH')
    return value


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, url):
        return None


def local_http(profile, port, path, *, method='GET', payload=None, token=None):
    if port not in (profile.api_port, profile.ui_port) or not path.startswith('/') or path.startswith('//'):
        raise ValueError('INVALID_QA_HTTP_TARGET')
    headers = {'Accept': 'application/json'}
    if token:
        headers['X-Lab-Key'] = token
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers['Content-Type'] = 'application/json'
    request = Request(f'http://127.0.0.1:{port}{path}', data=data, headers=headers, method=method)
    with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=2) as response:
        raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError('QA_RESPONSE_TOO_LARGE')
        return raw


def port_free(port):
    with socket.socket() as sock:
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            sock.bind(('127.0.0.1', port))
            return True
        except OSError:
            return False
