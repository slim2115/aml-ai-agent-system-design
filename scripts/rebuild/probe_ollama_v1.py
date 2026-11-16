"""One finite synthetic investigation using the already-running local Ollama."""
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
from pathlib import Path
import socket
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
OUTPUT = ROOT / 'verification/block-04'


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    started, attempts, blocked = time.monotonic(), [], []
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    def permit(address):
        try:
            allowed = ipaddress.ip_address(address[0]).is_loopback and address[1] == 11434
        except (TypeError, ValueError, IndexError):
            allowed = False
        if not allowed:
            blocked.append('NON_OLLAMA_NETWORK_ATTEMPT')
            raise OSError('LOOPBACK_OLLAMA_ONLY')
    def connect(sock, address):
        permit(address)
        return original_connect(sock, address)
    def connect_ex(sock, address):
        permit(address)
        return original_connect_ex(sock, address)
    socket.socket.connect, socket.socket.connect_ex = connect, connect_ex
    from src.agent import AMLAgent
    from src.runtime.transport import LocalOllamaTransport
    from src.runtime.retrieval import RuleIndex
    class ObservedTransport(LocalOllamaTransport):
        def complete(self, payload, schema, *, deadline):
            # Record no prompt, facts, provider output or exception strings.
            item = {'request_bytes': len(json.dumps(payload).encode()),
                    'fact_count': len(payload.get('facts', [])),
                    'rule_count': len(payload.get('rule_keys', [])),
                    'remaining_seconds_at_request': deadline - time.monotonic()}
            attempts.append(item)
            try:
                value = super().complete(payload, schema, deadline=deadline)
                item['response_bytes'] = len(value.encode())
                return value
            finally:
                item['remaining_seconds_after_request'] = deadline - time.monotonic()
    tracked = [*sorted((ROOT / 'src/runtime').glob('*.py')), ROOT / 'src/contracts/v1.py',
               ROOT / 'src/contracts/semantics.py', ROOT / 'config/demo-policy.json',
               ROOT / 'data/demo-v1/manifest.json']
    before = {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in tracked}
    readiness_started = time.monotonic()
    print('Preparing verified local index before functional qualification', flush=True)
    index = RuleIndex.from_file(ROOT / 'data/demo-v1/rules.json')
    readiness_seconds = time.monotonic() - readiness_started
    # Reuse one loaded artifact for both finite probes. The nested offline probe
    # restores its socket hooks before the loopback-only model call below.
    from scripts.rebuild import probe_runtime_v1
    offline_exit = probe_runtime_v1.main(prepared_index=index)
    runtime = AMLAgent(mode='demo', engine='local-model', transport=ObservedTransport(),
                       index_factory=lambda path: index)
    investigation_started = time.monotonic()
    result = runtime.run('INC-000123')
    after = {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in tracked}
    passed = result['status'] == 'draft' and result['confidence'] is None and bool(attempts) and not blocked and before == after and offline_exit == 0
    record = {'checked_at_utc': datetime.now(timezone.utc).isoformat(), 'status': 'PASS' if passed else 'FAIL',
              'elapsed_seconds': time.monotonic() - started, 'attempts': attempts,
              'readiness_seconds': readiness_seconds, 'investigation_seconds': time.monotonic() - investigation_started,
              'network_attempts_blocked': len(blocked), 'inputs_unchanged_during_probe': before == after,
              'input_hashes': before, 'result': result, 'events': [event.to_dict() for event in runtime.last_events],
              'scope': 'One real qwen2.5:14b loopback call after explicit readiness, plus offline functional probe using the same immutable index. Synthetic constrained ordering only. Cold-start failure retained separately; no AML/SLA acceptance.'}
    (OUTPUT / 'ollama-probe.json').write_text(json.dumps(record, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'status': record['status'], 'result_status': result['status'],
                      'reason_or_code': result.get('code', result.get('reason_code')),
                      'elapsed_seconds': record['elapsed_seconds'], 'attempts': len(attempts)}), flush=True)
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
