"""Versioned scenario expectations and negative controls for environment models."""
from copy import deepcopy
import json
from pathlib import Path
import socket
import sqlite3
import subprocess

import pytest

from qa.environments import models
from src.workflow import store as workflow_store


CATALOG_PATH = Path(__file__).resolve().parents[2] / 'qa/environments/catalog-v1.json'


@pytest.fixture
def catalog():
    return json.loads(CATALOG_PATH.read_text(encoding='utf-8'))


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    root = tmp_path / 'workflow'
    monkeypatch.setattr(workflow_store, 'WORKFLOW_ROOT', root)
    folder = root / 'environment-models' / 'test-run' / 'case'
    folder.mkdir(parents=True)
    return folder


def scenario(catalog, name):
    return next(item for item in catalog['scenarios'] if item['id'] == name)


def test_catalog_is_exact_versioned_bounded_and_not_rewritten(catalog):
    before = deepcopy(catalog)
    assert models.validate_catalog(catalog) is catalog
    assert catalog == before
    assert tuple(item['id'] for item in catalog['scenarios']) == models.SCENARIO_IDS
    assert len(catalog['scenarios']) == 9


@pytest.mark.parametrize('name', models.SCENARIO_IDS)
def test_real_boundaries_match_independent_expected_steps(name, catalog, workspace, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('LIVE_DEPENDENCY_FORBIDDEN')

    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    monkeypatch.setattr('psycopg.connect', forbidden)
    spec = scenario(catalog, name)
    expected_before = deepcopy(spec['expected'])
    result = models.execute(spec, workspace)
    assert result['steps'] == expected_before
    assert spec['expected'] == expected_before
    assert set(result) == {'steps', 'trace', 'invariants', 'dependencies'}
    assert result['invariants'] == [{'id': item, 'passed': True} for item in models.REQUIRED_INVARIANTS]
    assert result['dependencies'] == {'real': list(models.REAL_DEPENDENCIES),
                                      'modeled': list(models.MODELED_DEPENDENCIES)}
    assert 1 <= len(result['trace']) <= 512
    assert all(set(item) == {'step', 'call'} for item in result['trace'])
    assert 'a' * 64 not in json.dumps(result)
    assert str(workspace) not in json.dumps(result)
    connection = sqlite3.connect(workspace / 'workflow.sqlite3')
    try:
        assert connection.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == result['steps'][-1]['job_count']
    finally:
        connection.close()


def test_repeated_schedules_have_identical_normalized_evidence(catalog, workspace):
    spec = scenario(catalog, 'http-ack-replay')
    first = models.execute(spec, workspace)
    other = workspace.parent / 'repeat'
    other.mkdir()
    second = models.execute(spec, other)
    assert first == second
    assert first['steps'][-1]['job_count'] == 1
    assert first['steps'][-1]['submit_calls'] == 2


@pytest.mark.parametrize('change', [
    lambda c: c.update(unknown=True),
    lambda c: c.update(schema_version=1),
    lambda c: c.update(catalog_id='other'),
    lambda c: c['scenarios'].pop(),
    lambda c: c['scenarios'].reverse(),
    lambda c: c['scenarios'].__setitem__(1, deepcopy(c['scenarios'][0])),
    lambda c: c['scenarios'][0].update(id='unknown'),
    lambda c: c['scenarios'][0].update(requirements=['not-a-requirement']),
    lambda c: c['scenarios'][0].update(limitations=[]),
    lambda c: c['scenarios'][0]['steps'][0].update(action='exec'),
    lambda c: c['scenarios'][0]['steps'][0].update(path='../../workflow.sqlite3'),
    lambda c: c['scenarios'][0]['steps'][0].update(fault='http_ack_lost'),
    lambda c: c['scenarios'][0]['steps'][0].update(fault='unknown'),
    lambda c: c['scenarios'][0]['steps'][0].update(invalid=1),
    lambda c: c['scenarios'][0]['steps'][1].update(invalid=True),
    lambda c: c['scenarios'][0]['steps'].extend([{'action': 'receive'}] * 13),
    lambda c: c['scenarios'][0]['expected'].pop(),
    lambda c: c['scenarios'][0]['expected'][0].update(job_count=True),
    lambda c: c['scenarios'][0]['expected'][0].update(job_count=13),
    lambda c: c['scenarios'][0]['expected'][0].update(queue_status=[]),
    lambda c: c['scenarios'][0]['expected'][0].update(queue_error={}),
    lambda c: c['scenarios'][0]['expected'][0].update(outcome=['unknown']),
    lambda c: c['scenarios'][0]['expected'][0].update(outcome=[]),
    lambda c: c['scenarios'][0]['expected'][0].update(action='restart'),
])
def test_catalog_rejects_unbounded_unknown_or_ill_typed_inputs(catalog, change):
    change(catalog)
    with pytest.raises(ValueError):
        models.validate_catalog(catalog)


def test_unreached_fault_is_not_a_passing_scenario(catalog, workspace):
    spec = scenario(catalog, 'baseline')
    spec['steps'][0]['fault'] = 'dlq_unavailable'
    with pytest.raises(ValueError, match='FAULT_NOT_REACHED'):
        models.execute(spec, workspace)


def test_existing_workspace_is_never_overwritten(catalog, workspace):
    marker = workspace / 'operator-file.txt'
    marker.write_text('retain', encoding='utf-8')
    with pytest.raises(ValueError, match='WORKSPACE_NOT_EMPTY'):
        models.execute(scenario(catalog, 'baseline'), workspace)
    assert marker.read_text(encoding='utf-8') == 'retain'
    assert not (workspace / 'workflow.sqlite3').exists()


def test_outside_workflow_workspace_cannot_create_database(catalog, workspace, tmp_path):
    other = tmp_path / 'ordinary-directory'
    other.mkdir()
    with pytest.raises(workflow_store.WorkflowError, match='INVALID_INPUT_FORMAT'):
        models.execute(scenario(catalog, 'baseline'), other)
    assert list(other.iterdir()) == []


def test_offset_before_receipt_is_detected(catalog, workspace, monkeypatch):
    real = models.process_one

    def unsafe(record, queue, broker):
        broker.commit(record)
        return real(record, queue, broker)

    monkeypatch.setattr(models, 'process_one', unsafe)
    result = models.execute(scenario(catalog, 'baseline'), workspace)
    assert result['invariants'][0] == {'id': 'durable_receipt_before_offset', 'passed': False}
    assert result['steps'] != scenario(catalog, 'baseline')['expected']


def test_changed_retry_key_is_detected_by_real_sqlite_admissions(catalog, workspace, monkeypatch):
    real = models.WorkflowClient.submit
    calls = 0

    def unsafe(self, request, principal, idempotency_key, **kwargs):
        nonlocal calls
        calls += 1
        key = idempotency_key if calls == 1 else idempotency_key + '-changed'
        return real(self, request, principal, key, **kwargs)

    monkeypatch.setattr(models.WorkflowClient, 'submit', unsafe)
    result = models.execute(scenario(catalog, 'http-ack-replay'), workspace)
    assert result['invariants'][1] == {'id': 'stable_idempotency', 'passed': False}
    assert result['steps'][-1]['job_count'] == 2
    assert result['steps'][-1]['key_count'] == 2


def test_fresh_queue_operation_after_budget_is_detected(catalog, workspace, monkeypatch):
    real = models.scheduler_tick

    def unsafe(queue, client, principal, **kwargs):
        result = real(queue, client, principal, **kwargs)
        if any(item['status'] == 'budget_exhausted' for item in result):
            queue.pending(limit=1)
        return result

    monkeypatch.setattr(models, 'scheduler_tick', unsafe)
    result = models.execute(scenario(catalog, 'budget-reconcile'), workspace)
    assert result['invariants'][2] == {'id': 'no_admission_after_budget', 'passed': False}
