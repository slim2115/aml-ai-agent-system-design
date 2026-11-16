"""AML publication, separate broker flow and inbox reads; no live services."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from src.config import get_settings
from src.lab.api import create_app
from src.lab.database import LabDatabaseError
from src.lab.kafka.analysis_contracts import AnalysisEvent, TOPIC, DLQ_TOPIC, GROUP
from src.lab.kafka.broker import BrokerUnavailable, KafkaBroker
from src.lab.kafka.contracts import TOPIC as REVIEW_TOPIC, Record
from src.runtime.policy import load_demo_policy
from tests.lab.test_api import event_loop_socketpair_only
from tests.lab.test_kafka_api import FakeProducer

AUTH = {'X-Lab-Key': 'a' * 64}


def body():
    return {'event_id': str(uuid4()), 'occurred_at': '2026-10-04T12:00:00Z',
            'incident_id': 'INC-000123', 'schema_version': 1, 'synthetic': True,
            'event_type': 'aml.analysis.requested.v1', 'engine': 'template',
            'window': load_demo_policy(get_settings().project_root / 'config/demo-policy.json').window.model_dump(mode='json')}


@pytest.fixture
def stand():
    broker, store, workflow = Mock(), Mock(), Mock()
    broker.publish.return_value = {'topic': TOPIC, 'partition': 1, 'offset': 7}
    broker.status.return_value = {'status': 'ready'}
    store.list_results.return_value = []
    store.get.return_value = None
    app = create_app({'api_token': 'a' * 64}, database=Mock(), workflow=workflow,
                     kafka_broker=Mock(), kafka_store=Mock(), analysis_broker=broker, analysis_store=store)
    with TestClient(app) as client:
        yield client, broker, store, workflow


@pytest.mark.parametrize('method,path', [('post', ''), ('get', ''), ('get', '/status'),
                                       ('get', '/11111111-1111-4111-8111-111111111111')])
def test_aml_routes_authenticate_before_any_io(stand, method, path):
    client, broker, store, workflow = stand
    response = client.request(method, '/lab/kafka/analyses' + path, json=body() if method == 'post' else None)
    assert response.status_code == 401
    assert not broker.mock_calls and not store.mock_calls and not workflow.mock_calls


def test_docs_and_openapi_do_not_prepare_or_connect(stand):
    client, broker, store, workflow = stand
    assert client.get('/docs').status_code == 200
    schema = client.get('/openapi.json').json()
    sample = schema['components']['schemas']['AnalysisEvent']['examples'][0]
    assert AnalysisEvent.model_validate(sample).window == AnalysisEvent(**body()).window
    assert not broker.mock_calls and not store.mock_calls and not workflow.mock_calls


def test_publication_is_ack_only_and_has_no_aml_side_effects(stand):
    client, broker, store, workflow = stand
    event = body()
    response = client.post('/lab/kafka/analyses', headers=AUTH, json=event)
    assert response.status_code == 202
    assert response.json()['processing'] == 'check_result_separately'
    broker.publish.assert_called_once_with(TOPIC, key=b'INC-000123', value=AnalysisEvent(**event).canonical_bytes())
    assert not store.mock_calls and not workflow.mock_calls


@pytest.mark.parametrize('change', [{'synthetic': False}, {'engine': 'unsupported'}, {'window': None},
                                  {'actor': 'admin'}, {'event_type': 'incident.review.requested.v1'},
                                  {'occurred_at': '2026-10-04T12:00:00'}])
def test_invalid_aml_event_never_reaches_broker(stand, change):
    client, broker, store, workflow = stand
    response = client.post('/lab/kafka/analyses', headers=AUTH, json={**body(), **change})
    assert response.status_code == 400
    assert not broker.mock_calls and not store.mock_calls and not workflow.mock_calls


def test_window_must_match_explicit_demo_policy(stand):
    client, broker, _, _ = stand
    event = body()
    event['window']['start_inclusive'] = '2026-07-02T00:00:00Z'
    assert client.post('/lab/kafka/analyses', headers=AUTH, json=event).status_code == 400
    broker.publish.assert_not_called()


def test_uncertain_publish_is_not_reported_as_queued(stand):
    client, broker, store, _ = stand
    broker.publish.side_effect = BrokerUnavailable('KAFKA_DELIVERY_UNCONFIRMED', publication='unknown')
    response = client.post('/lab/kafka/analyses', headers=AUTH, json=body())
    assert response.status_code == 503 and response.json()['publication'] == 'unknown'
    store.enqueue.assert_not_called()


def test_projection_reads_do_not_create_workflow(stand):
    client, broker, store, workflow = stand
    event_id = str(uuid4())
    row = {'event_id': event_id, 'status': 'submitted', 'job_id': 'job-example'}
    store.get.return_value = row
    store.list_results.return_value = [row]
    assert client.get('/lab/kafka/analyses/' + event_id, headers=AUTH).json() == row
    assert client.get('/lab/kafka/analyses?limit=1', headers=AUTH).json() == {'items': [row], 'limit': 1}
    assert not broker.mock_calls and not workflow.mock_calls


def test_missing_inbox_does_not_claim_missing_publication(stand):
    client, _, _, _ = stand
    response = client.get('/lab/kafka/analyses/' + str(uuid4()), headers=AUTH)
    assert response.status_code == 404
    assert response.json()['code'] == 'AML_REQUEST_NOT_FOUND'


def test_status_checks_separate_dependencies(stand):
    client, broker, store, workflow = stand
    broker.status.side_effect = BrokerUnavailable()
    store.list_results.side_effect = LabDatabaseError('UNAVAILABLE')
    response = client.get('/lab/kafka/analyses/status', headers=AUTH)
    assert response.status_code == 200
    assert response.json()['broker']['status'] == response.json()['database']['status'] == 'unavailable'
    assert response.json()['consumer_group'] == GROUP
    assert not workflow.mock_calls


def test_broker_profile_isolates_topic_and_group():
    consumer = Mock()
    consumer.poll.return_value = None
    factory = Mock(return_value=consumer)
    partition = lambda topic, part, offset: SimpleNamespace(topic=topic, partition=part, offset=offset, error=None)
    broker = KafkaBroker(flow='aml', worker=True, client=SimpleNamespace(
        Producer=FakeProducer, Consumer=factory, TopicPartition=partition))
    assert broker.publish(TOPIC, key=b'k', value=b'{}')['topic'] == TOPIC
    with pytest.raises(BrokerUnavailable):
        broker.publish(REVIEW_TOPIC, key=b'k', value=b'{}')
    assert broker.poll() is None
    assert factory.call_args.args[0]['group.id'] == GROUP
    consumer.subscribe.assert_called_once_with([TOPIC])
    consumer.commit.side_effect = lambda offsets, asynchronous: offsets
    broker.commit(Record(TOPIC, 0, 7, b'k', b'{}'))
    assert consumer.commit.call_args.kwargs['offsets'][0].offset == 8
    with pytest.raises(BrokerUnavailable):
        broker.commit(Record(REVIEW_TOPIC, 0, 7, b'k', b'{}'))
    broker.close()


def test_bookkeeping_profile_cannot_publish_aml_event():
    with pytest.raises(BrokerUnavailable):
        KafkaBroker(client=SimpleNamespace(Producer=FakeProducer)).publish(TOPIC, key=b'k', value=b'{}')
