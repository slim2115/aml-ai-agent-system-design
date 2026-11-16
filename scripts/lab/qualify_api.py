"""Finite HTTP + PostgreSQL checks; deletes only note UUIDs created by this run.

Start the stand first. Never bootstraps, resets practice, starts models, or reads
workflow reports. Credentials are read locally and never written to evidence.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import sys
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, build_opener, ProxyHandler
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.lab.config import load_lab_settings
from src.lab.database import LabDatabase

BASE = 'http://127.0.0.1:8765'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'var/checks/lab')
    args = parser.parse_args()
    output_dir = args.output_dir.resolve()
    allowed = [ROOT / 'var/checks/lab', ROOT / 'verification/block-06']
    if not any(output_dir.is_relative_to(path.resolve()) for path in allowed):
        parser.error('Output must be inside var/checks/lab or verification/block-06.')
    output_dir.mkdir(parents=True, exist_ok=True)
    out = output_dir / 'api-qualification.json'
    started = time.monotonic()
    checks, owned = [], []
    settings = load_lab_settings()
    db = LabDatabase(settings)
    token = settings['api_token']
    secrets = [token, *(r['password'] for r in settings['roles'].values())]
    marker = 'API qualification ' + uuid4().hex

    def check(name, predicate):
        checks.append({'name': name, 'status': 'PASS' if predicate else 'FAIL'})
        if not predicate:
            raise AssertionError(name)

    def request(method, path, *, query=None, body=None, authorized=True, raw=None):
        if query:
            path += '?' + urlencode(query)
        headers = {'X-Lab-Key': token} if authorized else {}
        data = json.dumps(body).encode('utf-8') if body is not None else raw
        if data is not None:
            headers['Content-Type'] = 'application/json'
        req = Request(BASE + path, method=method, headers=headers, data=data)
        # Ignore machine proxy configuration: credentials stay on loopback.
        try:
            response = build_opener(ProxyHandler({})).open(req, timeout=15)
        except HTTPError as exc:
            response = exc
        with response:
            content = response.read(8 * 1024 * 1024)
            code = response.status
            response_headers = response.headers
        if any(secret.encode() in content for secret in secrets):
            raise AssertionError('SECRET_DISCLOSURE')
        decoded = json.loads(content) if 'application/json' in response_headers.get('Content-Type', '') and content else content
        return code, decoded, response_headers

    def error(name, response, status, code):
        actual, payload, _ = response
        check(name, actual == status and isinstance(payload, dict) and payload.get('status') == 'error'
              and payload.get('code') == code and set(payload) == {
                  'schema_version', 'status', 'generated_at', 'correlation_id', 'code', 'message', 'retryable'})

    def page(path, **query):
        status, payload, _ = request('GET', path, query={'limit': 100, **query})
        if status != 200:
            raise AssertionError('READ_FAILURE')
        return payload['items']

    def transaction_ids(rows):
        return [r['tx_id'] for r in rows]

    try:
        check('http_liveness', request('GET', '/health', authorized=False)[1] == {
            'status': 'ok', 'mode': 'local_learning', 'check': 'http_liveness_only'})
        status, docs, _ = request('GET', '/docs', authorized=False)
        check('swagger_uses_local_assets', status == 200 and b'/static/swagger-ui-bundle.js' in docs
              and b'/static/swagger-ui.css' in docs and b'cdn.jsdelivr' not in docs and b'unpkg.com' not in docs)
        for filename in ('swagger-ui-bundle.js', 'swagger-ui.css', 'favicon.svg'):
            status, content, _ = request('GET', '/static/' + filename, authorized=False)
            check('asset_' + filename, status == 200 and len(content) > 20)
        status, spec, _ = request('GET', '/openapi.json', authorized=False)
        protected = [operation for path, methods in spec['paths'].items()
                     if path.startswith(('/lab/', '/v1/')) for operation in methods.values()]
        check('runtime_openapi_auth_and_safe_errors', status == 200 and len(protected) >= 25
              and all(o.get('security') == [{'APIKeyHeader': []}] and '400' in o['responses']
                      and '422' not in o['responses'] for o in protected))
        (out.parent / 'runtime-openapi.json').write_text(json.dumps(spec, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        error('missing_key', request('GET', '/lab/clients', authorized=False), 401, 'UNAUTHORIZED')
        check('postgres_available', request('GET', '/lab/status')[0] == 200)
        with db.connect('reader') as conn:
            clients = conn.execute('SELECT client_id FROM practice.clients ORDER BY client_id').fetchall()
            cards = conn.execute('SELECT card_id, client_id FROM practice.cards ORDER BY card_id').fetchall()
            rules = conn.execute('SELECT rule_id, version, category FROM practice.rules ORDER BY rule_id, version').fetchall()
            incidents = conn.execute('SELECT incident_id, client_id FROM practice.incidents ORDER BY incident_id').fetchall()
            transactions = conn.execute('SELECT tx_id, client_id, currency, amount, timestamp FROM practice.transactions ORDER BY timestamp, tx_id').fetchall()
        check('fixture_size_precondition', 0 < len(transactions) <= 100 and 0 < len(clients) <= 100 and bool(incidents))
        check('clients_match_sql', [x['client_id'] for x in page('/lab/clients')] == [x['client_id'] for x in clients])
        check('cards_match_sql', [x['card_id'] for x in page('/lab/cards')] == [x['card_id'] for x in cards])
        category = rules[0]['category']
        check('rule_category_filter', [(x['rule_id'], x['version']) for x in page('/lab/rules', category=category)]
              == [(x['rule_id'], x['version']) for x in rules if x['category'] == category])
        client_id = clients[0]['client_id']
        check('card_client_filter', [x['card_id'] for x in page('/lab/cards', client_id=client_id)]
              == [x['card_id'] for x in cards if x['client_id'] == client_id])
        check('incident_client_filter', [x['incident_id'] for x in page('/lab/incidents', client_id=client_id)]
              == [x['incident_id'] for x in incidents if x['client_id'] == client_id])
        actual = page('/lab/transactions')
        check('transaction_order_and_exact_money', transaction_ids(actual) == transaction_ids(transactions)
              and all(isinstance(a['amount'], str) and Decimal(a['amount']) == e['amount']
                      for a, e in zip(actual, transactions)))
        check('timestamps_are_utc_and_equal_sql', all(a['timestamp'].endswith('Z')
              and datetime.fromisoformat(a['timestamp']) == e['timestamp'] for a, e in zip(actual, transactions)))
        first = transactions[0]
        boundary = first['timestamp']
        upper = boundary + timedelta(days=1)
        chosen = [x for x in transactions if boundary <= x['timestamp'] < upper]
        check('time_filter_inclusive_exclusive', transaction_ids(page('/lab/transactions',
              start_inclusive=boundary.isoformat(), end_exclusive=upper.isoformat())) == transaction_ids(chosen))
        check('time_filter_timezone_equivalence', transaction_ids(page('/lab/transactions',
              start_inclusive=boundary.astimezone(timezone(timedelta(hours=3))).isoformat(),
              end_exclusive=upper.astimezone(timezone(timedelta(hours=3))).isoformat())) == transaction_ids(chosen))
        check('exclusive_end_excludes_boundary', page('/lab/transactions', end_exclusive=boundary.isoformat()) == [])
        amount = first['amount']
        check('money_equality_filter', transaction_ids(page('/lab/transactions', min_amount=str(amount), max_amount=str(amount)))
              == transaction_ids([x for x in transactions if x['amount'] == amount]))
        incident = incidents[0]
        check('incident_filter_selects_incident_client', transaction_ids(page('/lab/transactions', incident_id=incident['incident_id']))
              == transaction_ids([x for x in transactions if x['client_id'] == incident['client_id']]))
        currency = first['currency']
        check('currency_filter', transaction_ids(page('/lab/transactions', currency=currency))
              == transaction_ids([x for x in transactions if x['currency'] == currency]))
        check('pagination_has_stable_order', transaction_ids(page('/lab/transactions', limit=2, offset=1)) == transaction_ids(transactions[1:3]))
        check('query_injection_is_literal', page('/lab/transactions', client_id="x' OR true --") == [])
        error('naive_datetime_rejected', request('GET', '/lab/transactions', query={'start_inclusive': '2026-07-01T00:00:00'}), 400, 'INVALID_INPUT_FORMAT')
        error('excess_money_precision_rejected', request('GET', '/lab/transactions', query={'min_amount': '0.001'}), 400, 'INVALID_INPUT_FORMAT')
        error('invalid_json_is_safe', request('POST', '/lab/notes', raw=b'{'), 400, 'INVALID_INPUT_FORMAT')
        used = {row['incident_id'] for row in incidents}
        missing = next('INC-' + str(n).zfill(6) for n in range(999999, 900000, -1) if 'INC-' + str(n).zfill(6) not in used)
        error('foreign_key_failure_is_safe', request('POST', '/lab/notes', body={'incident_id': missing, 'body': marker}), 400, 'INVALID_INPUT_FORMAT')
        error('nul_text_failure_is_safe', request('POST', '/lab/notes', body={'incident_id': incident['incident_id'], 'body': marker + '\x00'}), 400, 'INVALID_INPUT_FORMAT')
        body = marker + " x'); DELETE FROM practice.case_notes; --"
        status, note, headers = request('POST', '/lab/notes', body={'incident_id': incident['incident_id'], 'body': body})
        if status == 201 and isinstance(note, dict) and 'id' in note:
            owned.append(UUID(note['id']))
        check('note_create_location_revision', status == 201 and note['revision'] == 1
              and headers.get('Location') == '/lab/notes/' + note['id'] and note['body'] == body)
        path = headers['Location']
        with db.connect('reader') as conn:
            stored = conn.execute('SELECT body, revision FROM practice.case_notes WHERE id = %s', (owned[-1],)).fetchone()
        check('create_committed_before_ack', stored == {'body': body, 'revision': 1})
        check('note_get_exact', request('GET', path)[1] == note)
        check('notes_incident_filter', all(row['incident_id'] == incident['incident_id'] for row in page('/lab/notes', incident_id=incident['incident_id'])))
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(request, 'PATCH', path, body={'body': marker + ' concurrent ' + str(i), 'expected_revision': 1}) for i in range(2)]
            results = [f.result() for f in futures]
        check('concurrent_cas_one_winner', sorted(r[0] for r in results) == [200, 409])
        winner = next(r[1] for r in results if r[0] == 200)
        check('concurrent_cas_preserves_winner', winner['revision'] == 2 and request('GET', path)[1] == winner)
        error('stale_delete_cannot_remove_note', request('DELETE', path, query={'expected_revision': 1}), 409, 'VERSION_CONFLICT')
        check('stale_delete_preserves_note', request('GET', path)[1] == winner)
        check('delete_current_revision', request('DELETE', path, query={'expected_revision': 2})[:2] == (204, b''))
        error('deleted_note_is_missing', request('GET', path), 404, 'NOT_FOUND')
        error('missing_note_update', request('PATCH', path, body={'body': marker, 'expected_revision': 2}), 404, 'NOT_FOUND')
        check('read_queries_preserve_transaction_rows', transaction_ids(page('/lab/transactions')) == transaction_ids(transactions))
    except Exception as exc:
        # No exception text: driver/HTTP diagnostics could include input or keys.
        checks.append({'name': 'execution', 'status': 'FAIL', 'exception_type': type(exc).__name__})
    finally:
        try:
            with db.connect('api') as conn:
                for note_id in owned:
                    conn.execute('DELETE FROM practice.case_notes WHERE id = %s', (note_id,))
            checks.append({'name': 'own_note_cleanup', 'status': 'PASS'})
        except Exception as exc:
            checks.append({'name': 'own_note_cleanup', 'status': 'FAIL', 'exception_type': type(exc).__name__})
    result = {'status': 'PASS' if checks and all(c['status'] == 'PASS' for c in checks) else 'FAIL',
              'checked_at': datetime.now(timezone.utc).isoformat(), 'transport': BASE,
              'database': 'PostgreSQL on 127.0.0.1:55432/aml_lab',
              'scope': 'real HTTP, real PostgreSQL, synthetic practice; no model or workflow job executed',
              'cleanup': 'only note UUIDs created by this run; no reset',
              'elapsed_seconds': round(time.monotonic() - started, 3),
              'check_count': len(checks), 'checks': checks}
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'status': result['status'], 'checks': len(checks), 'evidence': str(out)}))
    return 0 if result['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
