"""Sequential real Kafka → PostgreSQL → AML workflow → browser exercises.

Run only through the owned pipeline launcher, after real search preparation.
No browser fixture/model double from the deterministic step-4 suite is used.
"""
import base64
from datetime import datetime, timedelta
import re
import time

import pytest

from qa.pipeline.support import DLQProbe, Pipeline, coordinates


pytestmark = pytest.mark.live


@pytest.fixture(scope='session')
def pipeline_browser(qa_profile):
    # Windows asyncio creates an internal socket pair. Initialize its driver
    # before the per-test socket guard, just like the step-4 browser fixture.
    # Page HTTP and WebSocket routes remain restricted in _open_linked_job.
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            yield browser
        finally:
            browser.close()


@pytest.fixture
def pipeline(qa_profile):
    from qa.tools.stand import service_ready
    assert service_ready(qa_profile, 'worker'), 'QA_PIPELINE_WORKER_REQUIRED'
    pipeline = Pipeline(qa_profile)
    try:
        yield pipeline
    finally:
        # A failed observation must not leave the owned consumer paused.
        pipeline.start_worker()


def _open_linked_job(pipeline, event_id, fingerprint, browser):
    from playwright.sync_api import expect
    from qa.tools.browser_boundaries import browser_target_allowed
    profile = pipeline.profile
    context = browser.new_context(
        base_url=f'http://127.0.0.1:{profile.ui_port}',
        locale='ru-RU', timezone_id='Europe/Moscow', viewport={'width': 1440, 'height': 1050},
        service_workers='block')
    def http(route):
        if browser_target_allowed(route.request.url, profile):
            route.continue_()
        else:
            route.abort('blockedbyclient')
    def websocket(route):
        if browser_target_allowed(route.url, profile, websocket=True):
            route.connect_to_server()
        else:
            route.close(code=1008, reason='QA address boundary')
    context.route('**/*', http)
    context.route_web_socket('**/*', websocket)
    page = context.new_page()
    page.set_default_timeout(20000)
    page.set_default_navigation_timeout(30000)
    expect.set_options(timeout=20000)
    context.tracing.start(screenshots=True, snapshots=True, sources=True)
    failed = True
    try:
        page.goto('/')
        expect(page.get_by_role('heading', name='AML — пошаговая проверка', exact=True)).to_be_visible()
        page.get_by_text('Запросы AML из Kafka', exact=True).click()
        label = page.get_by_text('Событие: ' + event_id, exact=True)
        expect(label).to_be_visible()
        card = page.get_by_test_id('stVerticalBlock').filter(has=label).last
        expect(card.get_by_text('Задание: ' + fingerprint['job_id'], exact=True)).to_be_visible()
        card.get_by_role('button', name='Открыть задание из Kafka', exact=True).click()
        expect(page.get_by_role('heading', name='INC-000123 · Отчёт готов к проверке', exact=True)).to_be_visible()
        expect(page.get_by_text(re.compile('^Отчёт: ' + re.escape(fingerprint['report_id']) + r' · версия '))).to_be_visible()
        page.get_by_text('Ход обработки и сведения для разбора', exact=True).click()
        expect(page.get_by_text('Идентификатор задания: ' + fingerprint['job_id'], exact=True)).to_be_visible()
        failed = False
        return {'engine': 'chromium', 'version': browser.version,
                'opened_job_id': fingerprint['job_id'], 'opened_report_id': fingerprint['report_id']}
    finally:
        if failed:
            try:
                page.screenshot(path=str(pipeline.output / 'QA-P01-ui-failure.png'), full_page=True, timeout=5000)
            except Exception:
                pass
        context.tracing.stop(path=str(pipeline.output / 'QA-P01-ui-trace.zip') if failed else None)
        context.close()


def test_01_real_chain_opens_exact_kafka_job_in_operator_ui(pipeline, pipeline_browser):
    with pipeline.evidence('QA-P01') as evidence:
        event = pipeline.event()
        before = pipeline.job_ids()
        ack = pipeline.publish(event)
        evidence.record(event_id=event['event_id'], publication=ack['status'], source=coordinates(ack))
        terminal = pipeline.wait_terminal(event['event_id'])
        evidence.record(projected_status=terminal['status'], job_id=terminal['job_id'])
        fingerprint = pipeline.assert_consistent(event['event_id'])
        assert pipeline.job_ids() - before == {fingerprint['job_id']}
        deliveries = pipeline.wait_deliveries(event['event_id'], 1)
        assert coordinates(deliveries[0]) == coordinates(ack)
        assert deliveries[0]['duplicate'] is False
        evidence.record(consistency=fingerprint, deliveries=deliveries)
        evidence.record(ui=_open_linked_job(pipeline, event['event_id'], fingerprint, pipeline_browser))


def test_02_identical_republication_has_two_deliveries_and_one_job(pipeline):
    with pipeline.evidence('QA-P02') as evidence:
        event = pipeline.event()
        before = pipeline.job_ids()
        first = pipeline.publish(event)
        pipeline.wait_terminal(event['event_id'])
        original = pipeline.assert_consistent(event['event_id'])
        second = pipeline.publish(event)
        assert coordinates(first) != coordinates(second)
        deliveries = pipeline.wait_deliveries(event['event_id'], 2)
        assert {tuple(coordinates(row).values()) for row in deliveries} == {
            tuple(coordinates(first).values()), tuple(coordinates(second).values())}
        assert [row['duplicate'] for row in deliveries] == [False, True]
        assert pipeline.assert_consistent(event['event_id']) == original
        assert pipeline.job_ids() - before == {original['job_id']}
        evidence.record(event_id=event['event_id'], first_source=coordinates(first), second_source=coordinates(second),
                        consistency=original, deliveries=deliveries, new_job_count=1)


def test_03_conflicting_uuid_goes_to_dlq_without_changing_original(pipeline):
    with pipeline.evidence('QA-P03') as evidence:
        event = pipeline.event()
        first = pipeline.publish(event)
        pipeline.wait_terminal(event['event_id'])
        original = pipeline.assert_consistent(event['event_id'])
        original_row = pipeline.store.get(event['event_id'])
        before_jobs = pipeline.job_ids()
        conflicting = dict(event, occurred_at=(datetime.fromisoformat(
            event['occurred_at'].replace('Z', '+00:00')) + timedelta(seconds=1)).isoformat())
        with DLQProbe(pipeline.profile) as dlq:
            rejected = pipeline.publish(conflicting)
            envelope = dlq.wait(rejected, 'EVENT_CONFLICT')
            evidence.record(event_id=event['event_id'], original_source=coordinates(first),
                            rejected_source=coordinates(rejected), dlq_records=dlq.records,
                            reason_code=envelope['reason_code'])
        assert pipeline.source_deliveries(rejected) == []
        assert pipeline.store.get(event['event_id']) == original_row
        assert pipeline.assert_consistent(event['event_id']) == original
        assert len(pipeline.deliveries(event['event_id'])) == 1
        assert pipeline.job_ids() == before_jobs
        evidence.record(consistency=original, original_unchanged=True)


def test_04_malformed_json_has_exact_dlq_source_and_no_inbox_entry(pipeline):
    with pipeline.evidence('QA-P04') as evidence:
        probe_event = pipeline.event()  # Correlates the exercise, not the invalid JSON bytes.
        before_requests, before_jobs = pipeline.request_ids(), pipeline.job_ids()
        with DLQProbe(pipeline.profile) as dlq:
            ack = pipeline.raw_publish(probe_event['incident_id'].encode('utf-8'), b'{')
            envelope = dlq.wait(ack, 'INVALID_EVENT')
            assert base64.b64decode(envelope['value_base64'], validate=True) == b'{'
            assert envelope['value_bytes'] == 1 and envelope['value_truncated'] is False
            evidence.record(probe_id=probe_event['event_id'], source=coordinates(ack),
                            reason_code=envelope['reason_code'], value_bytes=1, dlq_records=dlq.records)
        assert pipeline.source_deliveries(ack) == []
        assert pipeline.request_ids() == before_requests
        assert pipeline.job_ids() == before_jobs
        evidence.record(inbox_unchanged=True, jobs_unchanged=True)


def test_05_broker_ack_while_worker_stopped_then_resumes_single_job(pipeline):
    with pipeline.evidence('QA-P05') as evidence:
        event = pipeline.event()
        before_jobs = pipeline.job_ids()
        try:
            pipeline.stop_worker()
            ack = pipeline.publish(event)
            evidence.record(event_id=event['event_id'], worker='stopped', publication=ack['status'], source=coordinates(ack))
            deadline = time.monotonic() + 2
            observations = 0
            while True:
                assert pipeline.store.get(event['event_id']) is None
                missing = pipeline.request('GET', '/lab/kafka/analyses/' + event['event_id'], expected=404)
                assert missing['code'] == 'AML_REQUEST_NOT_FOUND'
                assert pipeline.deliveries(event['event_id']) == []
                observations += 1
                if time.monotonic() >= deadline:
                    break
                time.sleep(.2)
            assert pipeline.job_ids() == before_jobs
            evidence.record(no_inbox_observations=observations, result_code='AML_REQUEST_NOT_FOUND')
        finally:
            pipeline.start_worker()
            evidence.record(worker='restored')
        pipeline.wait_terminal(event['event_id'])
        fingerprint = pipeline.assert_consistent(event['event_id'])
        deliveries = pipeline.wait_deliveries(event['event_id'], 1)
        assert coordinates(deliveries[0]) == coordinates(ack)
        assert pipeline.job_ids() - before_jobs == {fingerprint['job_id']}
        evidence.record(consistency=fingerprint, deliveries=deliveries, new_job_count=1)
