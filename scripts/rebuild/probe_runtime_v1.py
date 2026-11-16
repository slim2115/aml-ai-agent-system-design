"""Finite offline integration: actual embedding/tokenizer, v1 service, UI smoke.

The template engine executes no Ollama request. Python sockets are blocked;
this is not a proof of machine-level egress controls or production acceptance.
"""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import socket
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
OUTPUT = ROOT / 'verification/block-04'


def main(prepared_index=None):
    OUTPUT.mkdir(parents=True, exist_ok=True)
    checks, network_attempts, results = [], [], []
    started = time.monotonic()
    original_network = (socket.socket.connect, socket.socket.connect_ex, socket.create_connection)
    def deny(*args, **kwargs):
        network_attempts.append('PYTHON_SOCKET_ATTEMPT')
        raise RuntimeError('OFFLINE_PROBE_NETWORK_DISABLED')
    socket.socket.connect = deny
    socket.socket.connect_ex = deny
    socket.create_connection = deny
    def check(name, passed, **detail):
        checks.append({'name': name, 'passed': bool(passed), **detail})
        print(name + ': ' + ('PASS' if passed else 'FAIL'), flush=True)
    try:
        from src.agent import AMLAgent
        from src.contracts.v1 import ADAPTERS
        from src.runtime.policy import load_demo_policy
        from src.runtime.retrieval import RuleIndex
        indexes = {}
        readiness_started = time.monotonic()
        corpus_path = ROOT / 'data/demo-v1/rules.json'
        warmed_index = prepared_index or RuleIndex.from_file(corpus_path)
        indexes[hashlib.sha256(corpus_path.read_bytes()).hexdigest()] = warmed_index
        readiness_seconds = time.monotonic() - readiness_started
        def verified_index(path):
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest not in indexes:
                indexes[digest] = RuleIndex.from_file(path)
            return indexes[digest]
        runtime = AMLAgent(mode='demo', engine='template', index_factory=verified_index)
        cases = [('INC-000123', 'draft', None), ('INC-000250', 'draft', None),
                 ('INC-000400', 'draft', None), ('INC-000307', 'refusal', 'NO_APPLICABLE_RULE'),
                 ('INC-000999', 'refusal', 'INSUFFICIENT_DATA'), ('INC-999999', 'error', 'NOT_FOUND')]
        for incident, status, code in cases:
            case_start = time.monotonic()
            result = runtime.run(incident)
            duration = time.monotonic() - case_start
            ADAPTERS['InvestigationResult'].validate_python(result)
            check('runtime_' + incident, result['status'] == status and
                  (code is None or result.get('code', result.get('reason_code')) == code),
                  actual_status=result['status'], actual_code=result.get('code', result.get('reason_code')),
                  elapsed_seconds=duration)
            results.append({'incident_id': incident, 'elapsed_seconds': duration, 'result': result,
                            'events': [e.to_dict() for e in runtime.last_events]})
        index = next(iter(indexes.values()))
        policy = load_demo_policy(ROOT / 'config/demo-policy.json')
        chunks = index.search('structuring', as_of=policy.window.as_of, k=index.active_chunk_count(policy.window.as_of))
        maximum = index.provenance['chunk_recipe']['max_seq_length']
        check('real_tokenizer_bounded_chunks', len(chunks) > 0 and all(c.token_count <= maximum for c in chunks),
              chunks=len(chunks), max_seq_length=maximum)
        check('exact_chunk_text_hashes', all(hashlib.sha256(c.text.encode()).hexdigest() == c.citation.content_sha256 for c in chunks))
        (OUTPUT / 'retrieval-provenance.json').write_text(json.dumps(index.provenance, indent=2) + '\n', encoding='utf-8')
        # Test Streamlit's real rendering harness without starting a web server.
        from streamlit.testing.v1 import AppTest
        ui = AppTest.from_file(str(ROOT / 'src/ui/app.py'), default_timeout=20).run()
        check('ui_initial_plain_text_page', not ui.exception and len(ui.button) == 1)
        ui.button[0].click().run()
        check('ui_default_policy_refusal', not ui.exception and any('POLICY_NOT_APPROVED' in item.value for item in ui.text))
        ui.text_input[0].set_value('INC-000123<script>').run()
        check('ui_input_change_invalidates_result', not ui.exception and 'result_v1' not in ui.session_state)
        ui.button[0].click().run()
        check('ui_invalid_input_error', not ui.exception and any('INVALID_INPUT_FORMAT' in item.value for item in ui.text))
    except Exception as exc:
        # Exception type only; provider errors may contain source/prompt data.
        check('probe_completed', False, safe_exception_type=type(exc).__name__)
    check('no_python_network_attempts', not network_attempts, count=len(network_attempts))
    socket.socket.connect, socket.socket.connect_ex, socket.create_connection = original_network
    status = 'PASS' if checks and all(c['passed'] for c in checks) else 'FAIL'
    (OUTPUT / 'runtime-results.json').write_text(json.dumps(results, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    report = {'checked_at_utc': datetime.now(timezone.utc).isoformat(), 'status': status,
              'elapsed_seconds': time.monotonic() - started, 'checks': checks,
              'readiness_seconds': locals().get('readiness_seconds'),
              'scope': 'Real local embedding/tokenizer and six synthetic template cases plus Streamlit AppTest, after explicit readiness. Immutable index reused. Cold-start failure retained in runtime-probe-cold.json. No cold-start, SME, law or SLA acceptance.'}
    (OUTPUT / 'runtime-probe.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    return 0 if status == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
