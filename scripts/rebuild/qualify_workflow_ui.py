"""Real Streamlit AppTest and local CLI controls with an explicit fake worker.

Writes new qualification evidence and an isolated SQLite database under var.
No embedding/model bootstrap, network service, browser or actual AML assessment.
"""
from contextlib import redirect_stdout
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import io
import json
from pathlib import Path
import socket
import sys
import time
import traceback
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
OUTPUT = ROOT / 'var/checks/ui'


class SyntheticWorker:
    """Only lifecycle/UI test data; cannot invoke an embedding or model."""
    def __init__(self):
        self.is_ready = False
        self.calls = 0

    def start(self, timeout_seconds=180):
        self.is_ready = True
        return {'ready': True, 'code': 'READY'}

    def execute(self, incident_id, engine='template', timeout_seconds=180, cancel_event=None):
        from src.workflow.store import utc_now
        from src.workflow.worker import ExecutionOutcome
        from tests.workflow.test_sources import demo_report
        self.calls += 1
        report = demo_report()
        report.update(incident_id=incident_id, report_id='ui-control-' + uuid4().hex,
                      generated_at=utc_now(), confidence=None)
        return ExecutionOutcome('result', result=report)

    def close(self):
        self.is_ready = False


def until(predicate, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.01)
    raise AssertionError('CONTROL_TIMEOUT')


def widget(ui, kind, label):
    found = [item for item in ui.get(kind) if item.label == label]
    if len(found) != 1:
        raise AssertionError(f'CONTROL_WIDGET_NOT_UNIQUE: {kind}: {label}')
    return found[0]


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    checks, network_attempts = [], []
    started = time.monotonic()
    code_paths = ('src/ui/app.py', 'src/ui/presentation.py', 'scripts/run_workflow.py', 'scripts/run_investigation.py',
                  'src/workflow/controller.py', 'src/workflow/store.py', 'src/workflow/delivery.py', 'src/workflow/sources.py',
                  'scripts/rebuild/qualify_workflow_ui.py')
    hashes = {path: sha256((ROOT / path).read_bytes()).hexdigest() for path in code_paths}
    service = None
    store_path = None

    def check(name, passed, **details):
        checks.append({'name': name, 'passed': bool(passed), **details})
        if not passed:
            raise AssertionError(name)

    def no_network(*_args, **_kwargs):
        network_attempts.append('blocked')
        raise AssertionError('NETWORK_FORBIDDEN_IN_UI_CONTROL')

    try:
        with patch.object(socket.socket, 'connect', no_network), \
                patch.object(socket.socket, 'connect_ex', no_network), \
                patch.object(socket, 'create_connection', no_network):
            import streamlit as st
            from streamlit.testing.v1 import AppTest
            from src.contracts.v1 import ADAPTERS, DeliveryRequest
            from src.workflow.auth import trusted_local_principal
            from src.workflow.controller import WorkflowService
            from src.workflow.delivery import DeliveryService
            from src.workflow.store import SQLiteStore
            from src.ui.presentation import STAGES
            from scripts import run_investigation, run_workflow

            principal = trusted_local_principal()
            directory = ROOT / 'var/workflow' / ('ui-qualification-' + uuid4().hex)
            store_path = directory / 'workflow.sqlite3'
            store = SQLiteStore(store_path)
            fake = SyntheticWorker()
            service = WorkflowService(store, worker=fake)

            def cli(arguments):
                output = io.StringIO()
                with redirect_stdout(output):
                    exit_code = run_workflow.main(['--database', str(store_path), *arguments])
                return exit_code, json.loads(output.getvalue())

            st.cache_resource.clear()
            # The application imports the patched service constructor. Everything
            # below that boundary (database, decisions, delivery) is real code.
            with patch('src.workflow.controller.WorkflowService', return_value=service):
                ui = AppTest.from_file(str(ROOT / 'src/ui/app.py'), default_timeout=15).run()

                def stage(number):
                    widget(ui, 'radio', 'Этап работы').set_value(STAGES[number - 1]).run()
                    if ui.exception:
                        raise AssertionError('CONTROL_STAGE_RENDER_FAILED')

                stage(2)
                check('ui_initial_consent_and_readiness_required', not ui.exception and
                      widget(ui, 'button', 'Создать задание').disabled and not store.list_jobs(principal))
                widget(ui, 'checkbox', 'Использовать учебную политику и синтетический снимок').set_value(True).run()
                stage(1)
                widget(ui, 'button', 'Подготовить поиск').click().run()
                until(lambda: service.readiness()['ready'])
                widget(ui, 'button', 'Обновить состояние').click().run()
                stage(2)
                check('ui_preparation_and_refresh_preserve_consent', ui.session_state['demo'] is True and
                      not widget(ui, 'button', 'Создать задание').disabled)
                widget(ui, 'button', 'Создать задание').click().run()
                job = until(lambda: next((row for row in store.list_jobs(principal)
                                         if row['status'] not in ('queued', 'running')), None))
                ui.run()
                report = job['result']
                reference = store.report_ref(report['report_id'], report['report_version'], principal)
                check('ui_job_and_report_persisted', not ui.exception and job['status'] == 'succeeded' and
                      fake.calls == 1 and SQLiteStore(store_path).get_job(job['job_id'], principal) == job)
                check('ui_fact_uses_plain_text', any(
                      item.value == report['found_facts'][0]['fact'] for item in ui.text) and
                      not any(item.proto.allow_html for item in ui.markdown))
                check('ui_exact_source_cells_and_rule_edition_available', any(
                    item.label == 'Полная редакция правила 1' for item in ui.expander) and
                    any('json_pointer' in item.value for item in ui.json) and
                    any('95000' in frame.value.astype(str).to_string() for frame in ui.dataframe))

                stage(4)
                check('ui_decision_requires_explicit_choice_and_reason',
                      widget(ui, 'selectbox', 'Решение').value is None and
                      widget(ui, 'text_area', 'Обоснование решения').value == '' and
                      widget(ui, 'button', 'Сохранить учебное решение').disabled)
                widget(ui, 'selectbox', 'Решение').set_value('approve').run()
                widget(ui, 'text_area', 'Обоснование решения').set_value('Synthetic stale approval control.').run()
                stage(3)
                stage(4)
                check('ui_unsaved_review_survives_stage_change',
                      widget(ui, 'selectbox', 'Решение').value == 'approve' and
                      widget(ui, 'text_area', 'Обоснование решения').value == 'Synthetic stale approval control.' and
                      store.latest_decision(reference.report_id, reference.report_version, principal) is None)

                # Emulate a second local session after the UI bound revision 0.
                request_path = directory / 'decision-request.json'
                request_path.write_text(json.dumps({'schema_version': '1.0',
                    'report': reference.model_dump(mode='json'), 'expected_revision': 0,
                    'action': 'reject', 'reason': 'Synthetic concurrent local session control.'}), encoding='utf-8')
                decision_code, decision = cli(['decide', '--request', str(request_path), '--key', 'ui-race'])
                check('cli_persists_concurrent_decision', decision_code == 0 and decision['revision'] == 1)
                ui.run()
                check('ui_stale_decision_rejected', not ui.exception and
                      widget(ui, 'button', 'Сохранить учебное решение').disabled and
                      widget(ui, 'text_area', 'Обоснование решения').value == 'Synthetic stale approval control.' and
                      store.latest_decision(reference.report_id, reference.report_version, principal) == decision)

                widget(ui, 'button', 'Обновить состояние').click().run()
                decision_widgets = [item for item in ui.get('selectbox') if item.label == 'Решение']
                check('ui_refresh_retains_bound_report', not ui.exception and len(decision_widgets) == 1,
                      decision_widget_count=len(decision_widgets),
                      incident=ui.session_state['incident'], demo=ui.session_state['demo'],
                      engine=ui.session_state['engine'],
                      active_job_retained='active_job' in ui.session_state and ui.session_state['active_job'] == job['job_id'],
                      binding=ui.session_state['view_binding'] if 'view_binding' in ui.session_state else None,
                      selectbox_labels=[item.label for item in ui.get('selectbox')])
                check('ui_refresh_does_not_silently_accept_new_revision',
                      widget(ui, 'button', 'Сохранить учебное решение').disabled)
                widget(ui, 'button', 'Перечитать сохранённое решение').click().run()
                widget(ui, 'text_area', 'Обоснование решения').set_value('Synthetic reviewed local approval.').run()
                widget(ui, 'button', 'Сохранить учебное решение').click().run()
                approved = store.latest_decision(reference.report_id, reference.report_version, principal)
                check('ui_decision_saved_with_bound_report', not ui.exception and approved['action'] == 'approve' and
                      approved['revision'] == 2 and approved['report'] == reference.model_dump(mode='json'))

                from src.workflow.store import WorkflowError
                with patch('src.workflow.sources.resolve_report_sources', side_effect=WorkflowError('NOT_READY')):
                    stage(4)
                    decision_disabled = widget(ui, 'button', 'Сохранить учебное решение').disabled
                    stage(5)
                    widget(ui, 'checkbox', 'Понимаю: после принятия экспорта решение по этой версии изменить нельзя').set_value(True).run()
                    check('ui_unavailable_sources_block_decision_and_new_export', not ui.exception and
                          decision_disabled and widget(ui, 'button', 'Экспортировать в локальный симулятор').disabled and
                          not widget(ui, 'button', 'Зарегистрировать в локальной учебной очереди').disabled)
                ui.run()
                widget(ui, 'checkbox', 'Понимаю: после принятия экспорта решение по этой версии изменить нельзя').set_value(False).run()
                check('ui_export_requires_explicit_consequence_confirmation',
                      widget(ui, 'button', 'Экспортировать в локальный симулятор').disabled)
                widget(ui, 'checkbox', 'Понимаю: после принятия экспорта решение по этой версии изменить нельзя').set_value(True).run()
                widget(ui, 'button', 'Экспортировать в локальный симулятор').click().run()
                delivery_service = DeliveryService(SQLiteStore(store_path))
                deliveries = delivery_service.list_for_job(job['job_id'], principal)
                check('ui_local_export_receipt_and_download', not ui.exception and len(deliveries) == 1 and
                      deliveries[0].status == 'delivered' and deliveries[0].mode == 'local_simulator' and
                      deliveries[0].receipt.report == reference and len(ui.get('download_button')) == 1)
                artifact = delivery_service.export_artifact(deliveries[0].delivery_id, principal)
                check('export_reopens_exact_saved_bytes', json.loads(artifact) == report and
                      sha256(artifact).hexdigest() == reference.content_sha256)
                stage(4)
                check('ui_export_freezes_decision_and_reason',
                      widget(ui, 'selectbox', 'Решение').disabled and
                      widget(ui, 'text_area', 'Обоснование решения').disabled and
                      widget(ui, 'button', 'Сохранить учебное решение').disabled and
                      store.report_review_state(reference.report_id, reference.report_version, principal)['decision_locked'])
                stage(5)

                read_code, read_job = cli(['get', job['job_id']])
                list_code, listed = cli(['list'])
                dispatch_code, replayed = cli(['dispatch', deliveries[0].delivery_id, '--revision', '0'])
                check('cli_reads_saved_job_and_replays_receipt', read_code == list_code == dispatch_code == 0 and
                      read_job == job and listed == [job] and replayed == deliveries[0].model_dump(mode='json'))

                pending = delivery_service.create(DeliveryRequest(schema_version='1.0', job_id=job['job_id'],
                    report=reference, decision_id=None, destination='simulator-manual-queue', operation='manual_queue'),
                    principal, 'ui-retry-control')
                ui.run()
                widget(ui, 'button', 'Повторить локальную доставку').click().run()
                recovered = delivery_service.get(pending.delivery_id, principal)
                check('ui_retries_existing_pending_delivery', not ui.exception and recovered.status == 'delivered'
                      and recovered.delivery_id == pending.delivery_id and recovered.attempt_count == 1 and
                      len(delivery_service.list_for_job(job['job_id'], principal)) == 2)

                stage(2)
                widget(ui, 'text_input', 'Идентификатор инцидента').set_value('INC-000250').run()
                stage(4)
                check('ui_incident_change_hides_previous_report_actions', not ui.exception and
                      'active_job' not in ui.session_state and 'decision_binding' not in ui.session_state and
                      not any(report['report_id'] in item.value for item in ui.text) and
                      not ui.get('download_button') and not any(
                          item.label == 'Сохранить учебное решение' for item in ui.button))
                widget(ui, 'button', 'Открыть сохранённое задание').click().run()
                stage(5)
                check('ui_history_restores_saved_report_and_delivery', not ui.exception and
                      ui.session_state['active_job'] == job['job_id'] and
                      ui.session_state['incident'] == job['incident_id'] and
                      len(ui.get('download_button')) == 1 and fake.calls == 1)
                stage(2)
                widget(ui, 'selectbox', 'Формирование').set_value('local-model').run()
                stage(3)
                check('ui_engine_change_hides_previous_report', not ui.exception and
                      'active_job' not in ui.session_state and not ui.get('download_button'))
                widget(ui, 'button', 'Открыть сохранённое задание').click().run()
                stage(2)
                widget(ui, 'checkbox', 'Использовать учебную политику и синтетический снимок').set_value(False).run()
                check('ui_policy_consent_change_hides_previous_report', not ui.exception and
                      'active_job' not in ui.session_state and not ui.get('download_button') and
                      widget(ui, 'button', 'Создать задание').disabled)
                widget(ui, 'button', 'Открыть сохранённое задание').click().run()
                retry_export = delivery_service.create(DeliveryRequest(schema_version='1.0', job_id=job['job_id'],
                    report=reference, decision_id=approved['decision_id'], destination='simulator-export', operation='export'),
                    principal, 'ui-unavailable-export-retry')
                with patch('src.workflow.sources.resolve_report_sources', side_effect=WorkflowError('NOT_READY')):
                    stage(5)
                    check('ui_unavailable_sources_block_pending_export_retry',
                          widget(ui, 'button', 'Повторить локальную доставку').disabled and
                          delivery_service.get(retry_export.delivery_id, principal).attempt_count == 0)
                tampered = deepcopy(job)
                attack = '<script>alert("synthetic-control")</script>'
                tampered['result']['found_facts'][0]['fact'] = attack
                # Defense-in-depth render control only: normal storage rejects
                # altered reports before they reach this UI boundary.
                with patch.object(store, 'get_job', return_value=tampered):
                    stage(4)
                    check('ui_untrusted_html_is_text_and_cannot_authorize', not ui.exception and
                          any(item.value == attack for item in ui.text) and
                          not any(item.proto.allow_html for item in ui.markdown) and
                          widget(ui, 'button', 'Сохранить учебное решение').disabled)

            # Compatibility adapter formatting is tested with a previously saved
            # job. This is not a second model run or subprocess CLI qualification.
            output = io.StringIO()
            with patch('scripts.run_workflow.run_one', return_value=job) as adapted, \
                    patch.object(sys, 'argv', ['run_investigation.py', job['incident_id'], '--demo']), \
                    redirect_stdout(output):
                exit_code = run_investigation.main()
            adapted.assert_called_once_with(job['incident_id'], engine='template')
            result = ADAPTERS['InvestigationResult'].validate_python(json.loads(output.getvalue()))
            check('compatibility_cli_emits_v1_result', exit_code == 0 and result.model_dump(mode='json') == report)
            check('no_embedding_or_model_import', not any(name in sys.modules for name in
                  ('torch', 'sentence_transformers', 'chromadb')))
    except Exception as exc:
        checks.append({'name': 'qualification_completed', 'passed': False,
                       'safe_exception_type': type(exc).__name__,
                       'safe_frames': [{'function': frame.name, 'line': frame.lineno}
                                       for frame in traceback.extract_tb(exc.__traceback__)]})
    finally:
        if service is not None:
            service.close()
        if 'streamlit' in sys.modules:
            sys.modules['streamlit'].cache_resource.clear()
    checks.append({'name': 'no_python_network_attempts', 'passed': not network_attempts,
                   'attempt_count': len(network_attempts)})
    checks.append({'name': 'code_stable_during_qualification', 'passed': all(
        sha256((ROOT / path).read_bytes()).hexdigest() == digest for path, digest in hashes.items())})
    passed = bool(checks) and all(item['passed'] for item in checks)
    report = {'checked_at_utc': datetime.now(timezone.utc).isoformat(),
              'status': 'PASS' if passed else 'FAIL', 'elapsed_seconds': time.monotonic() - started,
              'scope': 'Real Streamlit AppTest, SQLite persistence, decisions and local simulated delivery; '
                       'CLI parser/function controls with captured stdout. Fake investigation worker. '
                       'No browser execution, HTTP server, real model, actual download, SME or external delivery acceptance.',
              'database': str(store_path) if store_path else None, 'source_sha256': hashes, 'checks': checks}
    (OUTPUT / 'ui-cli-qualification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'status': report['status'], 'checks': len(checks),
                      'artifact': str(OUTPUT / 'ui-cli-qualification.json')}, ensure_ascii=False))
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
