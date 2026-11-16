"""Bounded loopback adapter. confluent_kafka is imported only on explicit use."""
from __future__ import annotations

import importlib
import logging
import threading

from .contracts import BOOTSTRAP, DLQ_TOPIC, GROUP, PARTITIONS, TOPIC, Record


class BrokerUnavailable(RuntimeError):
    def __init__(self, code='KAFKA_UNAVAILABLE', *, publication='not_applicable'):
        self.code, self.publication = code, publication
        super().__init__(code)


class KafkaBroker:
    def __init__(self, *, worker=False, client=None, flow='bookkeeping'):
        if flow not in ('bookkeeping', 'aml'):
            raise ValueError('INVALID_KAFKA_FLOW')
        if flow == 'aml':
            from .analysis_contracts import TOPIC as ANALYSIS_TOPIC, DLQ_TOPIC as ANALYSIS_DLQ, GROUP as ANALYSIS_GROUP
            self.topic, self.dlq_topic, self.group = ANALYSIS_TOPIC, ANALYSIS_DLQ, ANALYSIS_GROUP
        else:
            self.topic, self.dlq_topic, self.group = TOPIC, DLQ_TOPIC, GROUP
        self._client = client
        self._worker = worker
        self._producer = None
        self._consumer = None
        self._lock = threading.Lock()
        self._logger = logging.getLogger('aml.kafka.client')
        self._logger.addHandler(logging.NullHandler())
        self._logger.propagate = False

    def _library(self):
        if self._client is None:
            try:
                self._client = importlib.import_module('confluent_kafka')
            except ImportError:
                raise BrokerUnavailable('KAFKA_CLIENT_NOT_INSTALLED', publication='not_attempted') from None
        return self._client

    def _get_producer(self):
        if self._producer is None:
            self._producer = self._library().Producer({
                'bootstrap.servers': BOOTSTRAP, 'client.id': 'aml-lab-publisher',
                'enable.idempotence': True, 'acks': 'all', 'delivery.timeout.ms': 8000,
                'request.timeout.ms': 3000, 'socket.timeout.ms': 3000,
                'queue.buffering.max.messages': 100, 'message.max.bytes': 131072,
            }, logger=self._logger)
        return self._producer

    def publish(self, topic: str, *, key: bytes, value: bytes) -> dict:
        if topic not in (self.topic, self.dlq_topic) or len(value) > 65536 or len(key) > 1024:
            raise BrokerUnavailable('KAFKA_INVALID_MESSAGE', publication='not_attempted')
        with self._lock:
            try:
                producer = self._get_producer()
            except BrokerUnavailable:
                raise
            except Exception:
                raise BrokerUnavailable('KAFKA_CLIENT_INIT_FAILED', publication='not_attempted') from None
            outcome = []
            def delivered(error, message):
                outcome.append((error, message))
            try:
                producer.produce(topic, key=key, value=value, on_delivery=delivered)
            except BufferError:
                raise BrokerUnavailable('KAFKA_QUEUE_FULL', publication='not_attempted') from None
            except Exception:
                raise BrokerUnavailable('KAFKA_PUBLISH_FAILED', publication='unknown') from None
            try:
                producer.flush(timeout=9)
            except Exception:
                raise BrokerUnavailable('KAFKA_PUBLISH_FAILED', publication='unknown') from None
            if not outcome or outcome[0][0] is not None:
                # A timeout does not prove absence from the broker. Retry the
                # same event UUID and body; database deduplication handles it.
                raise BrokerUnavailable('KAFKA_DELIVERY_UNCONFIRMED', publication='unknown')
            message = outcome[0][1]
            if message.topic() != topic or message.partition() < 0 or message.offset() < 0:
                raise BrokerUnavailable('KAFKA_INVALID_ACK', publication='unknown')
            return {'topic': topic, 'partition': message.partition(), 'offset': message.offset()}

    def status(self) -> dict:
        with self._lock:
            try:
                metadata = self._get_producer().list_topics(timeout=3)
                topics = {}
                for name in (self.topic, self.dlq_topic):
                    topic = metadata.topics.get(name)
                    if (topic is None or topic.error is not None or len(topic.partitions) != PARTITIONS
                            or any(p.error is not None or p.leader < 0 or len(p.replicas) != 1
                                   for p in topic.partitions.values())):
                        raise BrokerUnavailable('KAFKA_TOPICS_NOT_READY')
                    topics[name] = len(topic.partitions)
                return {'status': 'ready', 'bootstrap': BOOTSTRAP, 'topics': topics}
            except BrokerUnavailable:
                raise
            except Exception:
                raise BrokerUnavailable() from None

    def poll(self, timeout=1) -> Record | None:
        if not self._worker:
            raise BrokerUnavailable('KAFKA_NOT_A_WORKER')
        client = self._library()
        if self._consumer is None:
            self._consumer = client.Consumer({
                'bootstrap.servers': BOOTSTRAP, 'client.id': 'aml-lab-worker', 'group.id': self.group,
                'enable.auto.commit': False, 'enable.auto.offset.store': False,
                'auto.offset.reset': 'earliest', 'allow.auto.create.topics': False,
                'session.timeout.ms': 10000, 'max.poll.interval.ms': 60000,
                'socket.timeout.ms': 5000,
            }, logger=self._logger)
            self._consumer.subscribe([self.topic])
        message = self._consumer.poll(timeout)
        if message is None:
            return None
        if message.error() is not None:
            raise BrokerUnavailable('KAFKA_CONSUME_FAILED')
        return Record(message.topic(), message.partition(), message.offset(), message.key(), message.value())

    def commit(self, record: Record) -> None:
        if self._consumer is None or record.topic != self.topic:
            raise BrokerUnavailable('KAFKA_COMMIT_FAILED')
        results = self._consumer.commit(offsets=[self._library().TopicPartition(
            record.topic, record.partition, record.offset + 1)], asynchronous=False)
        if (not results or len(results) != 1 or results[0].error is not None
                or results[0].topic != record.topic or results[0].partition != record.partition
                or results[0].offset != record.offset + 1):
            raise BrokerUnavailable('KAFKA_COMMIT_FAILED')

    def close(self):
        try:
            if self._consumer is not None:
                self._consumer.close()  # auto commit is disabled
        finally:
            with self._lock:
                if self._producer is not None:
                    self._producer.flush(timeout=3)
                self._consumer = self._producer = None
