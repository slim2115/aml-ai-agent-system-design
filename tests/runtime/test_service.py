from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.agent import AMLAgent
from src.config import get_settings
from src.contracts.v1 import ADAPTERS, ApplicableRule, RuleCitation
from src.runtime.context import ContextBuilder, ContextFailure
from src.runtime.policy import load_demo_policy
from src.runtime.retrieval import RetrievalFailure
from src.runtime.transport import TransportFailure

ROOT = Path(__file__).resolve().parents[2]


class FakeChunk:
    def __init__(self, row):
        self.rule = ApplicableRule(citation=RuleCitation(rule_id=row['rule_id'], rule_version=row['version'],
            source_ref=row['regulation_ref'], chunk_id=row['rule_id'] + '-test',
            content_sha256=sha256(row['text'].encode()).hexdigest()), rule_text=row['text'])
        self.citation = self.rule.citation
    def as_applicable(self):
        return self.rule


class FakeIndex:
    """Only vector ranking is substituted; source/context/render stay real."""
    def __init__(self, path):
        data = path.read_bytes()
        self.provenance = {'source_sha256': sha256(data).hexdigest()}
        self.chunks = [FakeChunk(row) for row in json.loads(data)]
    def active_chunk_count(self, as_of):
        return len(self.chunks)
    def search(self, query, *, as_of, k):
        return self.chunks[:k]


class FakeTransport:
    def __init__(self, invalid_attempts=0):
        self.calls, self.invalid_attempts = [], invalid_attempts
    def complete(self, payload, schema, *, deadline):
        self.calls.append((payload, schema, deadline))
        if len(self.calls) <= self.invalid_attempts:
            return '{"status":"draft","fact":"fabricated amount 999999"}'
        return json.dumps({'schema_version': '1.0', 'fact_order': [r['fact_id'] for r in payload['facts']],
                           'rule_order': payload['rule_keys']})


def agent(**kwargs):
    return AMLAgent(mode='demo', index_factory=FakeIndex, **kwargs)


@pytest.mark.parametrize('value,code', [(None, 'MISSING_INPUT'), ('', 'MISSING_INPUT'), (' ', 'MISSING_INPUT'),
    ('INC-１２３４５６', 'INVALID_INPUT_FORMAT'), (123, 'INVALID_INPUT_FORMAT'),
    (' INC-000123', 'INVALID_INPUT_FORMAT'), ('INC-000123\n', 'INVALID_INPUT_FORMAT')])
def test_invalid_input_never_touches_data_or_model(value, code):
    def forbidden(*args):
        raise AssertionError('must not load')
    result = AMLAgent(index_factory=forbidden).run(value)
    assert result['status'] == 'error' and result['code'] == code
    ADAPTERS['InvestigationResult'].validate_python(result)


def test_production_default_explicitly_refuses_without_claiming_queue():
    result = AMLAgent().run('INC-000123')
    assert result['reason_code'] == 'POLICY_NOT_APPROVED'
    assert result['recommended_action'] == 'manual_review'
    assert 'found_facts' not in result and 'delivery_id' not in result


@pytest.mark.parametrize('incident', ['INC-000123', 'INC-000250', 'INC-000400'])
def test_real_demo_snapshot_to_v1_draft_with_mocked_vectors(incident):
    runtime = agent()
    result = runtime.run(incident)
    assert result['status'] == 'draft', result
    assert result['incident_id'] == incident and result['confidence'] is None
    assert result['suspicious_patterns'] == []
    assert result['policy_version'].lower().startswith('synthetic-demo')
    assert len({e.correlation_id for e in runtime.last_events}) == 1
    assert runtime.last_events[-1].code == 'DRAFT'
    ADAPTERS['InvestigationResult'].validate_python(result)


@pytest.mark.parametrize('incident,expected', [('INC-000999', 'INSUFFICIENT_DATA'), ('INC-999999', 'NOT_FOUND'),
                                             ('INC-000307', 'NO_APPLICABLE_RULE')])
def test_missing_source_and_unknown_incident_are_distinct(incident, expected):
    transport = FakeTransport()
    result = agent(engine='local-model', transport=transport).run(incident)
    assert result.get('reason_code', result.get('code')) == expected, result
    assert 'found_facts' not in result and not transport.calls


def test_invalid_selection_retries_once_with_safe_feedback_and_shared_deadline():
    transport = FakeTransport(invalid_attempts=1)
    runtime = agent(engine='local-model', transport=transport)
    result = runtime.run('INC-000123')
    assert result['status'] == 'draft', result
    assert len(transport.calls) == 2
    assert transport.calls[0][2] == transport.calls[1][2]
    assert 'validation_feedback' not in transport.calls[0][0]
    assert transport.calls[1][0]['validation_feedback'] == 'INVALID_SELECTION_RETURN_COMPLETE_CATALOG'
    assert 'fabricated' not in json.dumps([e.to_dict() for e in runtime.last_events])


def test_exhausted_retries_refuse_without_partial_draft():
    transport = FakeTransport(invalid_attempts=10)
    result = agent(engine='local-model', transport=transport).run('INC-000123')
    assert result['reason_code'] == 'VALIDATION_FAILED'
    assert len(transport.calls) == 2 and 'found_facts' not in result


@pytest.mark.parametrize('exception,expected', [(ContextFailure('CONFLICTING_DATA'), 'CONFLICTING_DATA'),
    (ContextFailure('NOT_READY'), 'NOT_READY'), (RetrievalFailure('AMBIGUOUS_RULE'), 'AMBIGUOUS_RULE'),
    (TransportFailure(), 'DEPENDENCY_UNAVAILABLE'), (RuntimeError('SECRET PAN 4111111111111111'), 'INTERNAL_ERROR')])
def test_safe_terminal_errors_no_exception_text_or_false_success(exception, expected):
    def fail(_):
        raise exception
    runtime = AMLAgent(mode='demo', index_factory=fail)
    result = runtime.run('INC-000123')
    assert result.get('reason_code', result.get('code')) == expected
    text = json.dumps(result) + json.dumps([e.to_dict() for e in runtime.last_events])
    assert 'SECRET' not in text and '4111111111111111' not in text
    assert 'found_facts' not in result


def test_corpus_changed_between_context_and_retrieval_cannot_publish():
    def tampered(path):
        index = FakeIndex(path)
        index.provenance['source_sha256'] = 'a' * 64
        return index
    result = AMLAgent(mode='demo', index_factory=tampered).run('INC-000123')
    assert result['code'] == 'NOT_READY'


def test_return_from_slow_stage_is_rejected_without_model_call():
    ticks = [0.]
    def slow_index(path):
        index = FakeIndex(path)
        ticks[0] = 181.
        return index
    transport = FakeTransport()
    result = AMLAgent(mode='demo', engine='local-model', transport=transport, index_factory=slow_index,
                      clock=lambda: ticks[0]).run('INC-000123')
    assert result['code'] == 'DEADLINE_EXCEEDED' and not transport.calls


def test_constructor_is_lazy_and_legacy_entrypoint_uses_v1():
    from src.agent.graph import AMLAgent as GraphAgent
    from src.runtime.service import AMLAgent as RuntimeAgent
    assert GraphAgent is RuntimeAgent is AMLAgent
    assert AMLAgent().last_events == ()


@pytest.mark.parametrize('outcome', ['empty', 'checksum', 'exception'])
def test_late_failure_or_refusal_does_not_bypass_budget(outcome):
    ticks = [0.]
    def late(path):
        index = FakeIndex(path)
        ticks[0] = 181.
        if outcome == 'exception':
            raise RetrievalFailure('INVALID_CORPUS')
        if outcome == 'empty':
            index.chunks = []
        else:
            index.provenance['source_sha256'] = '0' * 64
        return index
    result = AMLAgent(mode='demo', index_factory=late, clock=lambda: ticks[0]).run('INC-000123')
    assert result['code'] == 'DEADLINE_EXCEEDED'


def test_null_confidence_accepted_but_required():
    result = agent().run('INC-000123')
    assert result['status'] == 'draft'
    ADAPTERS['InvestigationResult'].validate_python(result)
    del result['confidence']
    with pytest.raises(ValueError):
        ADAPTERS['InvestigationResult'].validate_python(result)
