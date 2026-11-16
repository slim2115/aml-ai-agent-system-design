"""HTTP and broker acknowledgement boundaries without a client/broker install."""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from pydantic import ValidationError

from src.lab.api import create_app
from src.lab.database import LabDatabaseError
from src.lab.kafka.broker import BrokerUnavailable, KafkaBroker
from src.lab.kafka.contracts import DLQ_TOPIC, TOPIC, ReviewEvent, Record
from tests.lab.test_api import event_loop_socketpair_only  # Windows event-loop fixture

TOKEN = 'a' * 64
AUTH = {'X-Lab-Key': TOKEN}


def event_body():
    return {'event_id': str(uuid4()), 'occurred_at': '2026-10-01T12:00:00Z', 'incident_id': 'INC-000123'}


@pytest.fixture
def stand():
    broker = Mock()
    broker.publish.return_value = {'topic': TOPIC, 'partition': 1, 'offset': 7}
    broker.status.return_value = {'status': 'ready'}
    store = Mock()
    store.get.return_value = None
    store.list_results.return_value = []
    store.status.return_value = {'schema': 'kafka_lab'}
    app = create_app({'api_token': TOKEN}, database=Mock(), kafka_broker=broker, kafka_store=store)
    with TestClient(app) as client:
        yield client, broker, store


@pytest.mark.parametrize('method,path,body', [
    ('post', '/events', {}), ('get', '/status', None), ('get', '/results', None),
    ('get', '/results/11111111-1111-4111-8111-111111111111', None),
])
def test_every_route_authenticates_before_io(stand, method, path, body):
    client, broker, store = stand
    args = {'json': body} if body is not None else {}
    assert client.request(method, '/lab/kafka' + path, **args).status_code == 401
    assert not broker.mock_calls and not store.mock_calls


def test_liveness_docs_and_openapi_do_not_connect(stand):
    client, broker, store = stand
    for path in ('/health', '/docs', '/openapi.json'):
        assert client.get(path).status_code == 200
    assert not broker.mock_calls and not store.mock_calls
    schema = client.get('/openapi.json').json()
    operation = schema['paths']['/lab/kafka/events']['post']
    assert operation['security'] == [{'APIKeyHeader': []}]
    assert operation['responses']['503']['content']['application/json']['schema']['$ref'].endswith('/KafkaErrorBody')
    assert operation['responses']['503']['content']['application/json']['example']['publication'] == 'unknown'


def test_ack_is_not_a_processing_result(stand):
    client, broker, store = stand
    body = event_body()
    response = client.post('/lab/kafka/events', json=body, headers=AUTH)
    assert response.status_code == 202
    assert response.json() == {'status': 'broker_acknowledged', 'event_id': body['event_id'],
        'topic': TOPIC, 'partition': 1, 'offset': 7, 'processing': 'check_result_separately'}
    broker.publish.assert_called_once_with(TOPIC, key=b'INC-000123', value=ReviewEvent(**body).canonical_bytes())
    assert not store.mock_calls


def test_missing_result_does_not_claim_event_was_not_sent(stand):
    client, _, _ = stand
    response = client.get('/lab/kafka/results/' + str(uuid4()), headers=AUTH)
    assert response.status_code == 404
    assert response.json()['publication'] == 'not_applicable'


def test_timeout_returns_uncertain_outcome_and_no_success(stand):
    client, broker, store = stand
    broker.publish.side_effect = BrokerUnavailable('KAFKA_DELIVERY_UNCONFIRMED', publication='unknown')
    response = client.post('/lab/kafka/events', json=event_body(), headers=AUTH)
    assert response.status_code == 503
    assert response.json()['publication'] == 'unknown'
    assert not store.mock_calls


def test_independent_component_status(stand):
    client, broker, store = stand
    broker.status.side_effect = BrokerUnavailable('KAFKA_CLIENT_NOT_INSTALLED')
    response = client.get('/lab/kafka/status', headers=AUTH).json()
    assert response['broker']['status'] == 'unavailable'
    assert response['database']['status'] == 'ready'
    store.status.side_effect = LabDatabaseError('INVALID_STATE')
    assert client.get('/lab/kafka/status', headers=AUTH).json()['database']['code'] == 'INVALID_STATE'


@pytest.mark.parametrize('change', [
    {'synthetic': False}, {'synthetic': 1}, {'schema_version': True}, {'schema_version': 2},
    {'event_type': 'other'}, {'occurred_at': '2026-10-01T12:00:00'}, {'occurred_at': 1780000000},
    {'incident_id': 'INC-1'}, {'unknown': 'field'}, {'event_id': 'invalid'},
])
def test_invalid_event_rejected_before_publish(stand, change):
    client, broker, _ = stand
    response = client.post('/lab/kafka/events', json={**event_body(), **change}, headers=AUTH)
    assert response.status_code == 400
    assert not broker.mock_calls


def test_equivalent_timezones_and_field_order_have_one_canonical_body():
    body = event_body()
    one = ReviewEvent(**body)
    two = ReviewEvent(**{**body, 'occurred_at': '2026-10-01T15:00:00+03:00'})
    assert one.canonical_bytes() == two.canonical_bytes()
    with pytest.raises(ValidationError):
        one.incident_id = 'INC-000124'


@pytest.mark.parametrize('limit', [0, 101])
def test_result_list_limits(stand, limit):
    client, _, store = stand
    assert client.get('/lab/kafka/results', params={'limit': limit}, headers=AUTH).status_code == 400
    assert not store.mock_calls


class FakeProducer:
    def __init__(self, config, **kwargs):
        self.config = config
        self.outcome = 'success'
        self.callback = None
    def produce(self, topic, key, value, on_delivery):
        if self.outcome == 'full':
            raise BufferError('secret')
        self.callback = on_delivery
        self.topic = topic
    def flush(self, timeout):
        if self.callback and self.outcome != 'timeout':
            callback, self.callback = self.callback, None
            callback(None if self.outcome == 'success' else Exception('secret'),
                     SimpleNamespace(topic=lambda: self.topic, partition=lambda: 1, offset=lambda: 3))
        return 0 if self.callback is None else 1


def fake_broker():
    library = SimpleNamespace(Producer=FakeProducer)
    return KafkaBroker(client=library)


def test_native_producer_settings_and_ack():
    broker = fake_broker()
    assert broker.publish(TOPIC, key=b'k', value=b'{}') == {'topic': TOPIC, 'partition': 1, 'offset': 3}
    assert broker._producer.config['enable.idempotence'] is True
    assert broker._producer.config['acks'] == 'all'
    assert broker._producer.config['delivery.timeout.ms'] <= 10000
    broker.close()


@pytest.mark.parametrize('outcome,publication', [('timeout', 'unknown'), ('error', 'unknown'), ('full', 'not_attempted')])
def test_native_delivery_failures_are_not_acks(outcome, publication):
    broker = fake_broker()
    broker._get_producer().outcome = outcome
    with pytest.raises(BrokerUnavailable) as raised:
        broker.publish(TOPIC, key=b'k', value=b'{}')
    assert raised.value.publication == publication
    assert 'secret' not in str(raised.value)


def test_missing_optional_client_is_explicit(monkeypatch):
    def missing(_):
        raise ImportError('private path')
    monkeypatch.setattr('src.lab.kafka.broker.importlib.import_module', missing)
    with pytest.raises(BrokerUnavailable, match='KAFKA_CLIENT_NOT_INSTALLED') as raised:
        KafkaBroker().publish(TOPIC, key=b'k', value=b'{}')
    assert raised.value.publication == 'not_attempted'


def test_native_client_initialization_failure_has_no_publication():
    client = SimpleNamespace(Producer=Mock(side_effect=RuntimeError('private diagnostic')))
    with pytest.raises(BrokerUnavailable, match='KAFKA_CLIENT_INIT_FAILED') as raised:
        KafkaBroker(client=client).publish(TOPIC, key=b'k', value=b'{}')
    assert raised.value.publication == 'not_attempted'


def test_kafka_close_error_does_not_skip_aml_cleanup(monkeypatch):
    broker = Mock()
    broker.close.side_effect = RuntimeError('shutdown failed')
    workflow = Mock()
    workflow.readiness.return_value = {'ready': False, 'code': 'NOT_READY', 'state': 'not_ready'}
    monkeypatch.setattr('src.lab.kafka.broker.KafkaBroker', lambda **kwargs: broker)
    monkeypatch.setattr('src.lab.api.WorkflowService', lambda *args, **kwargs: workflow)
    monkeypatch.setattr('src.lab.api.SQLiteStore', lambda *args, **kwargs: None)
    app = create_app({'api_token': TOKEN}, database=Mock())
    with pytest.raises(RuntimeError, match='shutdown failed'):
        with TestClient(app) as client:
            assert client.get('/v1/readiness', headers=AUTH).status_code == 200
    workflow.close.assert_called_once()


def test_commit_is_synchronous_and_points_to_next_offset():
    factory = lambda topic, partition, offset: SimpleNamespace(topic=topic, partition=partition, offset=offset, error=None)
    broker = KafkaBroker(worker=True, client=SimpleNamespace(TopicPartition=factory))
    broker._consumer = Mock()
    broker._consumer.commit.side_effect = lambda offsets, asynchronous: offsets
    record = Record(TOPIC, 2, 40, b'k', b'{}')
    broker.commit(record)
    args = broker._consumer.commit.call_args.kwargs
    assert args['asynchronous'] is False and args['offsets'][0].offset == 41
    broker._consumer.commit.return_value = []
    broker._consumer.commit.side_effect = None
    with pytest.raises(BrokerUnavailable, match='KAFKA_COMMIT_FAILED'):
        broker.commit(record)


def test_consumer_disables_both_automatic_offset_mechanisms():
    consumer = Mock()
    consumer.poll.return_value = None
    factory = Mock(return_value=consumer)
    broker = KafkaBroker(worker=True, client=SimpleNamespace(Consumer=factory))
    assert broker.poll() is None
    cfg = factory.call_args.args[0]
    assert cfg['enable.auto.commit'] is False and cfg['enable.auto.offset.store'] is False
    consumer.subscribe.assert_called_once_with([TOPIC])
    broker.close()
    consumer.commit.assert_not_called()
