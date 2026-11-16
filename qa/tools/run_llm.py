"""Bounded, local-only repeated real-Ollama measurements on frozen synthetic QA.

This collector never edits fixtures or product code. Raw synthetic prompts and
responses belong only in this explicitly selected evidence directory.
"""
from contextlib import ExitStack
from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
from importlib.metadata import version
import argparse
import base64
import http.client
import json
import os
from pathlib import Path
import platform
import socket
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from qa.tools.run_retrieval import CountingEmbedding, environment, sources, write

DRAFT_IDS = ('QA-S01', 'QA-S02', 'QA-S03', 'QA-S04', 'QA-S14')
CONTROL_IDS = tuple(f'QA-S{number:02d}' for number in range(5, 13))
MODEL = 'qwen2.5:14b'
EXPECTED_DIGEST = '7cdf5a0187d5c58cc5d369b255592f7841d1c4696d45a8c8a9489440385b22f6'
ENDPOINT = ('127.0.0.1', 11434)


def output_path(value):
    from src.qa_profile import checked_path
    raw = Path(value)
    path = checked_path(raw if raw.is_absolute() else ROOT / raw, ROOT)
    parts = path.relative_to(ROOT).parts
    allowed = (len(parts) >= 3 and parts[0] == 'verification' and parts[1].startswith('qa-step-07-')) \
        or (len(parts) >= 4 and parts[:3] == ('var', 'qa', 'llm'))
    if not allowed:
        raise ValueError('QA_LLM_NEW_OUTPUT_REQUIRED')
    if path.exists():
        raise FileExistsError('QA_EVIDENCE_ALREADY_EXISTS')
    return path


def require_endpoint(address):
    if not isinstance(address, tuple) or len(address) != 2 or address != ENDPOINT:
        raise RuntimeError('QA_LLM_NETWORK_ENDPOINT_DENIED')


def network_guard():
    """Constrain Python TCP sockets; this is not an operating-system firewall."""
    stack = ExitStack()
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_create = socket.create_connection
    original_resolve = socket.getaddrinfo

    def connect(instance, address):
        require_endpoint(address)
        return original_connect(instance, address)

    def connect_ex(instance, address):
        require_endpoint(address)
        return original_connect_ex(instance, address)

    def create(address, *args, **kwargs):
        require_endpoint(address)
        return original_create(address, *args, **kwargs)

    def resolve(host, port, *args, **kwargs):
        require_endpoint((host, port))
        return original_resolve(host, port, *args, **kwargs)

    def no_datagrams(*_args, **_kwargs):
        raise RuntimeError('QA_LLM_NETWORK_DATAGRAM_DENIED')

    stack.enter_context(patch.object(socket.socket, 'connect', connect))
    stack.enter_context(patch.object(socket.socket, 'connect_ex', connect_ex))
    stack.enter_context(patch.object(socket, 'create_connection', create))
    stack.enter_context(patch.object(socket, 'getaddrinfo', resolve))
    stack.enter_context(patch.object(socket.socket, 'sendto', no_datagrams))
    return stack


def ollama_json(path, *, payload=None):
    """Read local metadata only, without model pull or generation endpoints."""
    if path not in {'/api/version', '/api/tags', '/api/ps', '/api/show'}:
        raise ValueError('QA_METADATA_ENDPOINT_DENIED')
    connection = http.client.HTTPConnection(*ENDPOINT, timeout=15)
    try:
        body = None if payload is None else json.dumps(payload).encode()
        connection.request('GET' if payload is None else 'POST', path, body=body,
                           headers={'Content-Type': 'application/json', 'Accept-Encoding': 'identity'})
        response = connection.getresponse()
        raw = response.read(2 * 1024 * 1024 + 1)
        if response.status != 200 or len(raw) > 2 * 1024 * 1024:
            raise RuntimeError('QA_OLLAMA_METADATA_FAILED')
        return json.loads(raw)
    finally:
        connection.close()


def identity():
    tags = ollama_json('/api/tags')
    matched = [row for row in tags.get('models', []) if row.get('name') == MODEL or row.get('model') == MODEL]
    if len(matched) != 1 or matched[0].get('digest') != EXPECTED_DIGEST:
        raise RuntimeError('QA_OLLAMA_MODEL_IDENTITY_MISMATCH')
    return dict(server=ollama_json('/api/version'), model=matched[0],
                show=ollama_json('/api/show', payload={'model': MODEL}),
                loaded=ollama_json('/api/ps'))


class RecordingResponse:
    def __init__(self, response, record):
        self.response, self.record = response, record
        record.update(http_status=response.status, http_headers=response.getheaders())

    def __getattr__(self, name):
        return getattr(self.response, name)

    def read1(self, amount):
        part = self.response.read1(amount)
        self.record['_raw'].extend(part)
        return part


class RecordingHTTPConnection:
    """Transparent adapter around the real HTTPConnection used by transport."""
    def __init__(self, host, port, *, timeout, records, connection_type=http.client.HTTPConnection):
        require_endpoint((host, port))
        self.connection = connection_type(host, port, timeout=timeout)
        self.record = {'started_at_utc': datetime.now(timezone.utc).isoformat(),
                       'endpoint': f'http://{host}:{port}', '_raw': bytearray()}
        self.started = time.perf_counter()
        records.append(self.record)

    @property
    def sock(self):
        return self.connection.sock

    def request(self, method, path, body, headers):
        self.record.update(method=method, path=path,
                           request_body=body.decode('utf-8'), request_headers=headers)
        return self.connection.request(method, path, body=body, headers=headers)

    def getresponse(self):
        return RecordingResponse(self.connection.getresponse(), self.record)

    def close(self):
        try:
            self.connection.close()
        finally:
            raw = bytes(self.record.pop('_raw', b''))
            self.record.update(elapsed_seconds=time.perf_counter() - self.started,
                               response_body_base64=base64.b64encode(raw).decode('ascii'),
                               response_body_sha256=sha256(raw).hexdigest(),
                               response_body_bytes=len(raw))
            try:
                from src.runtime.transport import strict_json
                self.record['response_body'] = raw.decode('utf-8')
                self.record['response_json'] = strict_json(self.record['response_body'])
            except (UnicodeError, ValueError):
                pass
            self.record['completion'] = completion_metadata(self.record)


def completion_metadata(record):
    """Separate raw-model validity from the application's final report."""
    from src.runtime.transport import strict_json
    result = dict(valid_complete_selection=False, queue_wait_seconds=None,
                  queue_wait_note='Not independently observable in this local HTTP response.')
    reply = record.get('response_json', {})
    if isinstance(reply, dict):
        result.update({key: reply.get(key) for key in ('model', 'done', 'done_reason',
            'prompt_eval_count', 'eval_count')})
        result['timings_seconds'] = {
            key.removesuffix('_duration'): reply[key] / 1_000_000_000
            if type(reply.get(key)) in (int, float) and reply[key] >= 0 else None
            for key in ('total_duration', 'load_duration', 'prompt_eval_duration', 'eval_duration')}
    try:
        request = strict_json(record['request_body'])
        payload = strict_json(request['messages'][1]['content'])
        response = strict_json(reply['message']['content'])
        facts = [fact['fact_id'] for fact in payload['facts']]
        rules = payload['rule_keys']
        result['valid_complete_selection'] = (
            record.get('http_status') == 200 and reply.get('done') is True
            and reply.get('message', {}).get('role') == 'assistant'
            and not reply.get('message', {}).get('tool_calls')
            and isinstance(response, dict) and set(response) == {'schema_version', 'fact_order', 'rule_order'}
            and response['schema_version'] == '1.0'
            and isinstance(response['fact_order'], list) and isinstance(response['rule_order'], list)
            and all(isinstance(value, str) for value in response['fact_order'] + response['rule_order'])
            and len(response['fact_order']) == len(facts) and set(response['fact_order']) == set(facts)
            and len(response['rule_order']) == len(rules) and set(response['rule_order']) == set(rules))
        result['fact_order'] = response.get('fact_order') if isinstance(response, dict) else None
        result['rule_order'] = response.get('rule_order') if isinstance(response, dict) else None
    except (KeyError, ValueError, TypeError, AttributeError):
        result['parse_status'] = 'invalid_or_incomplete'
    else:
        result['parse_status'] = 'parsed'
    return result


class RecordingTransport:
    def __init__(self, settings):
        from src.runtime.transport import LocalOllamaTransport
        self.attempts, self.prompts = [], []
        self.transport = LocalOllamaTransport(settings, connection_factory=lambda host, port, **kwargs:
            RecordingHTTPConnection(host, port, records=self.attempts, **kwargs))
        self.outcome = 'completed'

    def complete(self, payload, schema, *, deadline):
        from src.runtime.transport import TransportFailure
        self.prompts.append(json.loads(json.dumps(payload, ensure_ascii=False)))
        try:
            return self.transport.complete(payload, schema, deadline=deadline)
        except TransportFailure as exc:
            self.outcome = exc.code
            raise


class RecordedBuilder:
    def __init__(self, folder, policy):
        from src.runtime.context import ContextBuilder
        self.builder, self.context = ContextBuilder(folder, policy), None

    def build(self, incident_id):
        prepared = self.builder.build(incident_id)
        self.context = prepared.context.model_dump(mode='json')
        return prepared


class RecordedChunk:
    def __init__(self, chunk, selected):
        self.chunk, self.selected, self.citation = chunk, selected, chunk.citation

    def as_applicable(self):
        self.selected.append(self.citation.rule_id)
        return self.chunk.as_applicable()


class RecordedIndex:
    def __init__(self, index, selected, rankings):
        self.index, self.selected, self.rankings = index, selected, rankings
        self.provenance = index.provenance

    def active_chunk_count(self, as_of):
        return self.index.active_chunk_count(as_of)

    def search(self, query, *, as_of, k):
        chunks = self.index.search(query, as_of=as_of, k=k)
        self.rankings.append(dict(query=query, as_of=as_of, k=k,
            candidates=[dict(citation=c.citation.model_dump(mode='json'), distance=c.distance,
                             text=c.text) for c in chunks]))
        return [RecordedChunk(chunk, self.selected) for chunk in chunks]


def observe(scenario, folder, settings, embedding, manifest_digest, *, engine):
    from qa.scenario_checks import Observation
    from src.runtime.policy import DemoPolicy
    from src.runtime.retrieval import RuleIndex
    from src.runtime.service import AMLAgent
    policy = DemoPolicy.model_validate_json((folder / 'policy.json').read_bytes())
    builder = RecordedBuilder(folder, policy)
    selected, rankings = [], []
    transport = RecordingTransport(settings)

    def index_factory(path):
        raw = path.read_bytes()
        index = RuleIndex.from_records(json.loads(raw), embedding=embedding,
            embedding_manifest_digest=manifest_digest, source_sha256=sha256(raw).hexdigest())
        return RecordedIndex(index, selected, rankings)

    agent = AMLAgent(mode=scenario.mode, engine=engine, builder=builder, policy=policy,
        settings=settings, index_factory=index_factory, transport=transport)
    started = time.perf_counter()
    result = agent.run(scenario.case.incident_id)
    observation = Observation(result, builder.context, selected, transport.prompts,
        time.perf_counter() - started, [event.to_dict() for event in agent.last_events])
    return observation, transport, rankings


def selected_cases(catalog, case_id):
    allowed = DRAFT_IDS + CONTROL_IDS
    if case_id is not None and case_id not in allowed:
        raise ValueError('QA_LLM_UNSUPPORTED_CASE')
    mapping = {scenario.case.id: scenario for scenario in catalog.cases}
    return [mapping[key] for key in ((case_id,) if case_id else allowed)]


def run(output, *, repeats=3, case_id=None):
    """A complete batch is five draft scenarios plus eight pre-generation controls."""
    from qa.scenarios import DEFAULT_FOLDER, load_catalog
    from qa.scenario_checks import check_observation
    from qa.model_eval.checks import check_repeat, summarize_repeats
    if not 1 <= repeats <= 5:
        raise ValueError('QA_LLM_REPEAT_LIMIT')
    catalog = load_catalog()
    scenarios = selected_cases(catalog, case_id)
    before = sources()
    output.mkdir(parents=True, exist_ok=False)
    environment(output)
    write(output, 'source-hashes.json', before)
    write(output, 'run.json', dict(status='INCOMPLETE', started_at_utc=datetime.now(timezone.utc).isoformat(),
        model=MODEL, model_digest=EXPECTED_DIGEST, repeats=repeats, selected_case_ids=[s.case.id for s in scenarios],
        partial=case_id is not None or repeats < 3, temperature=0, seed='not_explicitly_set',
        excluded_case_ids={'QA-S13': 'synthetic unavailable dependency double',
                           'QA-S15': 'synthetic invalid selection double'},
        network='only literal 127.0.0.1:11434 via Python socket guard; offline embeddings',
        product_acceptance='INCOMPLETE', sme_acceptance='pending',
        scope='Constrained fact/rule ordering; no free-form report generation or LLM judge.'))
    result = dict(status='INCOMPLETE', product_acceptance='INCOMPLETE', sme_acceptance='pending')
    rows, controls, template_failures, attempts_by_run = [], [], [], []
    started = time.perf_counter()
    try:
        with network_guard():
            from src.config import get_settings
            from src.retrieval.embedding import build_embedding_function, validate_embedding_artifact
            get_settings.cache_clear()
            settings = get_settings()
            initial_identity = identity()
            write(output, 'ollama-before.json', initial_identity)
            manifest_digest = sha256(Path(settings.llm.embedding_manifest_path).read_bytes()).hexdigest()
            loaded = time.perf_counter()
            embedding = CountingEmbedding(build_embedding_function(settings))
            write(output, 'runtime.json', dict(python=sys.version, platform=platform.platform(),
                cpu=platform.processor(), logical_cpus=os.cpu_count(), embedding_device='cpu',
                packages={name: version(name) for name in ('torch', 'transformers', 'sentence-transformers', 'numpy')},
                embedding_model=settings.llm.embedding_model_name, embedding_manifest_sha256=manifest_digest,
                embedding_load_seconds=time.perf_counter() - loaded,
                generation_timeout_seconds=settings.performance.generation_timeout_sec))
            cases_dir = output / 'cases'
            cases_dir.mkdir()
            for scenario in scenarios:
                folder = DEFAULT_FOLDER / scenario.fixture
                case_dir = cases_dir / scenario.case.id
                case_dir.mkdir()
                template, _, template_rankings = observe(scenario, folder, settings, embedding, manifest_digest,
                                                         engine='template')
                template_issues = check_observation(scenario.model_copy(
                    update={'engine': 'template', 'expected_model_calls': 0}), template)
                if template_issues:
                    template_failures.append(dict(case_id=scenario.case.id, issues=template_issues))
                write(case_dir, 'template.json', dict(observation=asdict(template),
                    rankings=template_rankings, issues=template_issues))
                count = repeats if scenario.case.id in DRAFT_IDS else 1
                for repetition in range(1, count + 1):
                    loaded_before = ollama_json('/api/ps')
                    present = any(item.get('digest') == EXPECTED_DIGEST for item in loaded_before.get('models', []))
                    observed, transport, rankings = observe(scenario, folder, settings, embedding, manifest_digest,
                                                            engine='local-model')
                    if scenario.case.id in DRAFT_IDS:
                        runtime_scenario = scenario.model_copy(update={'engine': 'local-model', 'expected_model_calls': 1})
                        row = check_repeat(runtime_scenario, observed, repeat_index=repetition,
                                           transport_outcome='completed')
                        rows.append(row)
                    else:
                        issues = check_observation(scenario, observed)
                        row = dict(case_id=scenario.case.id, repeat_index=1,
                            status='PASS' if not issues else 'FAIL', issues=issues,
                            model_calls=len(observed.model_prompts))
                        controls.append(row)
                    record = dict(evaluation=row, observation=asdict(observed), http_attempts=transport.attempts,
                        rankings=rankings, loaded_before=loaded_before,
                        model_load_state='warm' if present else 'cold',
                        transport_outcome=transport.outcome,
                        template_issues=template_issues, template_report=template.result)
                    if scenario.case.id in DRAFT_IDS:
                        attempts_by_run.append(dict(case_id=scenario.case.id, repeat_index=repetition,
                            http_attempts=len(transport.attempts),
                            first_attempt_valid=bool(transport.attempts and
                                transport.attempts[0]['completion']['valid_complete_selection']),
                            repair_attempted=len(transport.attempts) > 1,
                            final_status=observed.result['status']))
                    write(case_dir, f'repeat-{repetition:02d}.json', record)
                    print(json.dumps(dict(case_id=scenario.case.id, repetition=repetition,
                        status=row['status'], report_status=observed.result['status'],
                        seconds=round(observed.latency_seconds, 3)), ensure_ascii=False), flush=True)
            planned_drafts = [s.case.id for s in scenarios if s.case.id in DRAFT_IDS]
            summary = summarize_repeats(rows, planned_case_ids=planned_drafts,
                repeats_per_case=repeats) if planned_drafts else dict(status='PASS',
                    applicability='not_applicable_control_only', planned_case_count=0, observed_repeat_count=0)
            final_identity = identity()
            write(output, 'ollama-after.json', final_identity)
            validate_embedding_artifact(settings)
            load_catalog()
            unchanged = sources() == before
            write(output, 'embedding-calls.json', embedding.calls)
            full = case_id is None and repeats >= 3
            all_pass = all(row['status'] == 'PASS' for row in rows + controls)
            collection_complete = (len(rows) == sum(s.case.id in DRAFT_IDS for s in scenarios) * repeats
                and len(controls) == sum(s.case.id in CONTROL_IDS for s in scenarios))
            stable = (initial_identity['model']['digest'] == final_identity['model']['digest']
                and initial_identity['server'] == final_identity['server']
                and initial_identity['show'] == final_identity['show'])
            status = ('PASS' if full else 'INCOMPLETE') if all_pass else (
                'INCOMPLETE' if any(row['status'] == 'INCOMPLETE' for row in rows + controls) else 'FAIL')
            if not unchanged or not stable or template_failures:
                status = 'FAIL'
            if summary['status'] == 'FAIL' or any(row['status'] == 'FAIL' for row in rows + controls):
                status = 'FAIL'
            elif not collection_complete or summary['status'] != 'PASS':
                status = 'INCOMPLETE' if status != 'FAIL' else status
            result.update(status=status,
                summary=summary, controls=controls, draft_observation_count=len(rows),
                control_observation_count=len(controls), source_files_unchanged_during_run=unchanged,
                model_identity_stable=stable, template_failures=template_failures,
                full_planned_matrix=full, collection_complete=collection_complete, repeats_per_draft_case=repeats,
                raw_attempt_summary=dict(observations=len(attempts_by_run),
                    first_attempt_valid_count=sum(row['first_attempt_valid'] for row in attempts_by_run),
                    repair_attempted_count=sum(row['repair_attempted'] for row in attempts_by_run),
                    total_http_attempts=sum(row['http_attempts'] for row in attempts_by_run),
                    rows=attempts_by_run))
    except Exception as exc:
        result.update(error_type=type(exc).__name__,
            error_code=str(exc) if str(exc).startswith('QA_') else 'QA_LLM_COLLECTION_ERROR',
            completed_draft_observations=len(rows), completed_controls=len(controls))
    finally:
        after = sources()
        write(output, 'source-hashes-after.json', after)
        result['source_files_unchanged_during_run'] = after == before
        if after != before:
            result['status'] = 'FAIL'
        result['elapsed_seconds'] = time.perf_counter() - started
        result['finished_at_utc'] = datetime.now(timezone.utc).isoformat()
        write(output, 'result.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', required=True)
    parser.add_argument('--repeats', type=int, default=3, choices=range(1, 6))
    parser.add_argument('--case-id', choices=DRAFT_IDS + CONTROL_IDS)
    args = parser.parse_args()
    result = run(output_path(args.output_dir), repeats=args.repeats, case_id=args.case_id)
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    return 0 if result['status'] == 'PASS' else 3 if result['status'] == 'INCOMPLETE' else 1


if __name__ == '__main__':
    raise SystemExit(main())
