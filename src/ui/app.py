"""Guided local workflow; decisions and delivery remain server-validated."""


def main():
    import atexit
    import os
    from pathlib import Path
    import re
    import sys
    from uuid import uuid4

    root = Path(__file__).resolve().parents[2]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    import streamlit as st
    from src.config import get_demo_paths
    from src.qa_profile import get_qa_profile
    from src.contracts.v1 import CreateJobRequest, DecisionRequest, DeliveryRequest
    from src.runtime.policy import load_demo_policy
    from src.ui.presentation import (STAGES, READINESS, JOB_LABELS, DECISIONS, CONSEQUENCES,
                                     EVENT_STAGES, EVENT_CODES, date_label, history_label, render_report)
    from src.workflow.auth import trusted_local_principal
    from src.workflow.controller import WorkflowService
    from src.workflow.remote import RemoteWorkflowService
    from src.workflow.delivery import DeliveryService
    from src.workflow.sources import resolve_report_sources
    from src.workflow.store import WorkflowError

    profile = get_qa_profile()
    st.set_page_config(page_title='AML — пошаговая проверка', layout='wide')
    st.title('AML — пошаговая проверка')
    st.caption('Локальный учебный оператор · синтетические данные · учебная политика без AML-утверждения.')
    if profile is not None:
        st.info('QA-стенд · ' + profile.run_id + ' · отдельные задания, решения и данные проверки.')
    with st.expander('Границы учебного сервиса'):
        st.info('Решения и доставка сохраняются локально. Одобрение отчёта не закрывает банковский кейс. Внешняя CMS, юридическая подпись, реальные получатели и уведомления не подключены.')

    @st.cache_resource
    def workflow(backend, qa_run_id=None):
        if backend not in ('local', 'api'):
            raise WorkflowError('NOT_READY')
        service = RemoteWorkflowService() if backend == 'api' else WorkflowService()
        atexit.register(service.close)
        return service

    def show_error(exc):
        code = exc.code if isinstance(exc, WorkflowError) else 'INVALID_INPUT_FORMAT'
        messages = {
            'NOT_READY': 'Действие сейчас недоступно. Проверьте готовность обработчика и источников.',
            'VERSION_CONFLICT': 'Решение не сохранено: версия изменилась. Перечитайте сохранённое решение перед повтором.',
            'IDEMPOTENCY_CONFLICT': 'Повтор относится к другим данным. Обновите состояние и проверьте выбранное задание.',
            'INVALID_STATE': 'Действие недоступно в текущем состоянии. Обновите состояние; принятое к экспорту решение изменить нельзя.',
            'FORBIDDEN': 'Нет полномочий для этого действия.', 'NOT_FOUND': 'Запись не найдена. Проверьте выбранное задание.',
            'UNAUTHORIZED': 'Ключ учебного API не принят. Проверьте локальную настройку стенда.',
            'INVALID_INPUT_FORMAT': 'Проверьте формат и обязательные поля.',
            'DEADLINE_EXCEEDED': 'Время обработки истекло. Проверьте причину в терминале; затем повторите подготовку.',
            'INTERNAL_ERROR': 'Операция не завершена. Обновите состояние и сохраните код ошибки для разбора.'}
        st.error(messages.get(code, 'Операция не завершена. Сохраните код ошибки для разбора.') + ' Код: ' + code)

    def read_or_stop(operation, *args):
        try:
            return operation(*args)
        except WorkflowError as exc:
            show_error(exc)
            st.stop()

    def go(step):
        st.session_state.step = STAGES[step]

    def clear_view():
        for key in ('active_job', 'view_binding', 'notice', 'delivery_warning'):
            st.session_state.pop(key, None)

    def input_changed(name):
        st.session_state[name] = st.session_state['_' + name]
        clear_view()

    def stage_changed():
        st.session_state.step = st.session_state._stage

    for key, value in {'step': STAGES[0], 'incident': 'INC-000123', 'demo': False,
                       'engine': 'template', 'review_drafts': {}}.items():
        if key not in st.session_state:
            st.session_state[key] = value
    try:
        backend = os.environ.get('AML_WORKFLOW_BACKEND', 'local')
        service = workflow(backend, profile.run_id if profile else None)
        store = service.store
        deliveries = DeliveryService(store)
        principal = trusted_local_principal()
    except WorkflowError as exc:
        show_error(exc)
        st.stop()

    state = service.readiness()
    if backend == 'api':
        st.info('Интегрированный режим: общая история с API и Kafka. Подготовку поиска и запуск анализа выполняет учебный API; проверка отчёта и решение остаются за оператором.')
        if not service.api_available:
            if profile is not None:
                st.warning('QA API сейчас недоступен. Сохранённая QA-история открывается локально. Проверьте состояние этого QA-запуска и обновите страницу.')
            else:
                st.warning('Учебный API сейчас недоступен. Сохранённая история открывается локально. Запустите scripts/lab/Start-Lab.ps1 в отдельном терминале и обновите состояние.')
    else:
        st.caption('Автономный режим: отдельная история сервиса. Для заданий из Kafka запустите scripts/rebuild/Start-IntegratedUI.ps1.')
    if state['state'] in ('ready', 'busy'):
        st.session_state.pop('prepare_requested', None)
    preparation_started = False
    history = read_or_stop(store.list_jobs, principal)[:50]

    def load_history(job_id=None):
        selected = read_or_stop(store.get_job, job_id or st.session_state._history_job, principal)
        clear_view()
        st.session_state.incident = selected['incident_id']
        st.session_state.engine = store.get_engine_internal(selected['job_id'])
        st.session_state.demo = True
        st.session_state.active_job = selected['job_id']
        st.session_state.view_binding = (selected['incident_id'], True, st.session_state.engine)
        go(2)

    with st.sidebar:
        st.subheader('Вернуться к заданию')
        st.caption('Последние 50 заданий. История доступна без подготовки модели.')
        if history:
            jobs_by_id = {job['job_id']: job for job in history}
            st.selectbox('Сохранённые задания', list(jobs_by_id), key='_history_job',
                         format_func=lambda value: history_label(jobs_by_id[value]))

            st.button('Открыть сохранённое задание', on_click=load_history)
        else:
            st.caption('Сохранённых заданий пока нет.')
        st.button('Новая проверка', on_click=lambda: (clear_view(), go(1)))
        if backend == 'api':
            with st.expander('Запросы AML из Kafka'):
                st.caption('Последние 20 запросов. Готовый черновик ещё не означает одобрение оператором.')
                if not service.api_available:
                    st.caption('Для просмотра очереди нужен работающий учебный API.')
                else:
                    try:
                        requests = service.client.analysis_requests()['items']
                        if not requests:
                            st.caption('Запросов анализа из Kafka пока нет.')
                        request_labels = {'queued': 'Ожидает запуска анализа', 'submitted': 'Задание принято обработчиком',
                            'succeeded': 'Черновик готов к проверке', 'refused': 'Анализ завершён отказом',
                            'failed': 'Анализ не завершён', 'timed_out': 'Время обработки истекло',
                            'cancelled': 'Задание отменено', 'rejected': 'Запрос отклонён'}
                        for item in requests:
                            with st.container(border=True):
                                st.text(item.get('incident_id', '') + ' · ' + request_labels.get(item.get('status'), 'Состояние неизвестно'))
                                st.text('Событие: ' + item.get('event_id', ''))
                                if item.get('last_error_code'):
                                    st.text('Код: ' + item['last_error_code'])
                                if item.get('job_id'):
                                    st.text('Задание: ' + item['job_id'])
                                    st.button('Открыть задание из Kafka', key='kafka-' + item['event_id'],
                                              on_click=load_history, args=(item['job_id'],))
                    except WorkflowError as exc:
                        show_error(exc)

    left, right = st.columns([3, 1])
    with left:
        st.text('Обработчик: ' + READINESS.get(state['state'], 'Состояние неизвестно'))
    with right:
        st.button('Обновить состояние')
    if state['state'] == 'preparing':
        st.info('Загружаются поиск и индекс. Состояние обновляется автоматически. На подготовку отведено до 3 минут; процент готовности не измеряется.')
    elif state['state'] == 'busy':
        st.info('Обработчик выполняет задание. Можно открыть сохранённое задание; новый запуск доступен после завершения.')
    elif state['state'] == 'not_ready' and st.session_state.get('prepare_requested'):
        show_error(WorkflowError(state['code']))
        st.caption('Подготовка не завершилась. Проверьте сообщение в терминале сервера, устраните причину и повторите подготовку на первом этапе.')

    st.session_state._stage = st.session_state.step
    st.radio('Этап работы', STAGES, key='_stage', horizontal=True, on_change=stage_changed)
    step = STAGES.index(st.session_state.step)
    notice = st.session_state.pop('notice', None)
    if notice:
        st.success(notice)
    delivery_warning = st.session_state.pop('delivery_warning', None)
    if delivery_warning:
        st.warning(delivery_warning)

    job_id = st.session_state.get('active_job')
    binding = (st.session_state.incident, st.session_state.demo, st.session_state.engine)
    job = read_or_stop(store.get_job, job_id, principal) if job_id and st.session_state.get('view_binding') == binding else None
    reference, latest, sources, report = None, None, None, None
    locked = False
    if job and step >= 2:
        st.subheader(job['incident_id'] + ' · ' + JOB_LABELS.get(job['status'], job['status']))
        st.caption('Принято: ' + date_label(job['accepted_at']) + ' · срок обработки: ' + date_label(job['deadline_at']))
        if job['status'] == 'succeeded':
            report = job['result']
            reference = read_or_stop(store.report_ref, report['report_id'], report['report_version'], principal)
            review = read_or_stop(store.report_review_state, report['report_id'], report['report_version'], principal)
            latest, locked = review['latest_decision'], review['decision_locked']
            try:
                sources = resolve_report_sources(report)
            except WorkflowError:
                st.error('Исходные значения или редакция правила сейчас не подтверждены. Решение и экспорт заблокированы; доступна локальная учебная очередь. Код: NOT_READY')
            st.caption('Отчёт: ' + report['report_id'] + ' · версия ' + str(report['report_version']) + ' · снимок: ' + report['snapshot_id'])

    if step == 0:
        st.subheader('Подготовьте поиск один раз перед созданием задания')
        st.write('Подготовка загружает модель поиска и индекс. Она нужна и для проверяемого шаблона. Сохранённые отчёты можно открыть сразу в истории слева.')
        st.caption('Ollama нужен только для дополнительного режима «Локальная модель: порядок фактов»; сервис сам его не запускает.')
        if st.button('Подготовить поиск', disabled=state['state'] in ('preparing', 'busy', 'ready', 'closed')):
            try:
                service.prepare()
                st.session_state.prepare_requested = True
                preparation_started = True
            except WorkflowError as exc:
                show_error(exc)
        if state['ready']:
            st.success('Поиск готов. Перейдите к выбору инцидента.')
        st.button('Перейти к инциденту', on_click=go, args=(1,))

    elif step == 1:
        st.subheader('Проверьте условия учебного анализа')
        # Canonical values survive the cleanup of hidden Streamlit widgets.
        for name in ('incident', 'demo', 'engine'):
            st.session_state['_' + name] = st.session_state[name]
        incident = st.text_input('Идентификатор инцидента', key='_incident', on_change=input_changed, args=('incident',),
                                help='Формат: INC- и шесть цифр. Учебный пример: INC-000123.')
        valid_incident = bool(re.fullmatch(r'INC-\d{6}', incident, flags=re.ASCII))
        if not valid_incident:
            st.error('Введите идентификатор в формате INC-000123: INC- и шесть цифр.')
        try:
            policy = load_demo_policy(get_demo_paths(root)[0])
        except (ValueError, OSError):
            st.error('Учебная политика недоступна. Проверьте конфигурацию сервиса. Код: NOT_READY')
            st.stop()
        st.text('Период: ' + date_label(policy.window.start_inclusive) + ' — ' + date_label(policy.window.end_exclusive))
        st.caption('Начало включается, конец не включается. Состояние данных на ' + date_label(policy.window.as_of) + '. Это исторический учебный набор.')
        st.checkbox('Использовать учебную политику и синтетический снимок', key='_demo', on_change=input_changed, args=('demo',))
        with st.expander('Способ формирования', expanded=st.session_state.engine != 'template'):
            st.selectbox('Формирование', ['template', 'local-model'], key='_engine', on_change=input_changed, args=('engine',),
                         format_func=lambda value: {'template': 'Проверяемый шаблон (без LLM)', 'local-model': 'Локальная модель: порядок фактов'}[value])
            st.caption('Для первого знакомства выберите шаблон. Локальная модель лишь упорядочивает проверенные факты и требует Ollama с qwen2.5:14b.')
        missing = []
        if not valid_incident:
            missing.append('исправьте идентификатор')
        if not st.session_state.demo:
            missing.append('подтвердите учебный режим')
        if not state['ready']:
            missing.append('дождитесь завершения задания' if state['state'] == 'busy' else 'подготовьте поиск на первом этапе')
        if missing:
            st.info('Для создания задания: ' + '; '.join(missing) + '.')
        if st.button('Создать задание', disabled=bool(missing), type='primary'):
            try:
                request = CreateJobRequest(schema_version='1.0', incident_id=incident, window=policy.window)
                accepted = service.submit(request, principal, 'ui-' + uuid4().hex, engine=st.session_state.engine)
                clear_view()
                st.session_state.active_job = accepted['job_id']
                st.session_state.view_binding = binding
                go(2)
                st.rerun()
            except (WorkflowError, ValueError) as exc:
                show_error(exc)

    elif job is None:
        st.info('Сначала создайте задание на этапе «Инцидент» или откройте сохранённое задание в истории слева.')
        st.button('Выбрать инцидент', on_click=go, args=(1,))

    elif job['status'] in ('queued', 'running'):
        st.info('Обработка продолжается. После завершения появится отчёт или причина отказа. Состояние обновляется автоматически.')
        if st.button('Отменить задание'):
            try:
                service.cancel(job['job_id'], principal, 'cancel-' + uuid4().hex)
                st.rerun()
            except WorkflowError as exc:
                show_error(exc)

    elif step == 2:
        if report:
            st.caption('Проверьте факты ниже, затем переходите к решению. Источники доступны и на следующем этапе.')
            st.button('Перейти к решению', on_click=go, args=(3,), disabled=sources is None, type='primary')
            render_report(st, report, sources)
        else:
            st.warning('Готового отчёта нет. Прочитайте причину; принимать решение по отчёту и экспортировать его нельзя.')
            result = job.get('result') or {}
            st.text(result.get('reason', result.get('message', JOB_LABELS.get(job['status'], job['status']))))
            if result.get('reason_code') or result.get('code'):
                st.caption('Код: ' + result.get('reason_code', result.get('code', '')))
            st.caption('Для нового запуска после отмены или таймаута при необходимости повторите подготовку поиска.')
        st.button('Перейти к сохранению и очереди', on_click=go, args=(4,))

    elif step == 3:
        if not report:
            st.info('Решение недоступно: готового отчёта нет.')
            st.button('Открыть причину завершения', on_click=go, args=(2,))
        else:
            st.subheader('Выберите решение по проверенному отчёту')
            if latest:
                st.text('Сохранённое решение: ' + DECISIONS[latest['action']] + ' · ревизия ' + str(latest['revision']))
                st.text(latest['reason'])
            if locked:
                st.info('Учебный экспорт уже принят. Решение по этой версии доступно только для просмотра; скачивание и повтор доставки находятся на пятом этапе.')
            draft_key = (reference.report_id, reference.report_version, reference.content_sha256)
            draft = st.session_state.review_drafts.setdefault(draft_key, {
                'action': None, 'reason': '', 'expected_revision': latest['revision'] if latest else 0})
            current_revision = latest['revision'] if latest else 0
            conflict = draft['expected_revision'] != current_revision

            def draft_changed(field):
                draft[field] = st.session_state['_decision_' + field]

            def acknowledge_revision():
                draft['expected_revision'] = current_revision

            if conflict and not locked:
                st.warning('Сохранённое решение изменилось. Выше показана текущая ревизия. Ваш текст оставлен как несохранённый ввод; перечитайте решение перед повтором.')
                st.button('Перечитать сохранённое решение', on_click=acknowledge_revision)
            st.session_state._decision_action = latest['action'] if locked and latest else draft['action']
            st.session_state._decision_reason = latest['reason'] if locked and latest else draft['reason']
            blocked = locked or sources is None or conflict
            st.selectbox('Решение', list(DECISIONS), index=None, placeholder='Выберите решение', key='_decision_action',
                         format_func=DECISIONS.get, disabled=blocked, on_change=draft_changed, args=('action',))
            if draft['action'] and not locked:
                st.info(CONSEQUENCES[draft['action']])
            st.text_area('Обоснование решения', key='_decision_reason', max_chars=4000, disabled=blocked,
                         placeholder='Что проверено → что обнаружено → почему выбран этот исход', on_change=draft_changed, args=('reason',))
            if locked:
                st.caption('В полях показано сохранённое решение. Редактирование заблокировано после принятия экспорта.')
            else:
                st.caption('Ввод сохраняется при переходах между этапами в текущем сеансе. До нажатия кнопки он не записан в историю; перезагрузка браузера может его сбросить.')
            incomplete = not draft['action'] or not draft['reason'].strip()
            if incomplete and not blocked:
                st.caption('Для сохранения выберите решение и заполните обоснование.')
            if st.button('Сохранить учебное решение', disabled=blocked or incomplete, type='primary'):
                try:
                    request = DecisionRequest(schema_version='1.0', report=reference,
                        expected_revision=draft['expected_revision'], action=draft['action'], reason=draft['reason'])
                    saved = store.create_decision(reference.report_id, request, principal, 'decision-' + uuid4().hex)
                    draft['expected_revision'] = saved['revision']
                    st.session_state.notice = 'Учебное решение сохранено. Проверьте следующее действие.'
                    go(4)
                    st.rerun()
                except (WorkflowError, ValueError) as exc:
                    show_error(exc)
            st.button('Вернуться к проверке', on_click=go, args=(2,))
            with st.expander('Отчёт и основания рядом с решением', expanded=True):
                render_report(st, report, sources)

    elif step == 4:
        st.subheader('Сохранённый результат и следующее действие')
        if latest:
            st.text('Сохранено: ' + DECISIONS[latest['action']] + ' · ревизия ' + str(latest['revision']))
            st.text(latest['reason'])
        elif report:
            st.info('Решение пока не сохранено. Для экспорта сначала проверьте отчёт и сохраните одобрение.')
        else:
            st.info('Готового отчёта нет. Можно зарегистрировать запись в локальной учебной очереди.')
        existing = read_or_stop(deliveries.list_for_job, job['job_id'], principal)
        exports = [d for d in existing if d.operation == 'export' and latest and d.decision_id == latest['decision_id']]
        if locked:
            st.info('Экспорт принят: изменение решения по этой версии заблокировано. Скачайте файл из подтверждённой доставки или повторите незавершённую доставку ниже.')
        operation = None
        if latest and latest['action'] == 'approve' and not exports and not locked:
            st.text(f"Экспорт: {job['incident_id']} · отчёт {reference.report_id} · версия {reference.report_version} · решение {latest['decision_id']}")
            confirmed = st.checkbox('Понимаю: после принятия экспорта решение по этой версии изменить нельзя', key='export-confirm-' + latest['decision_id'])
            st.caption('Будет сохранён локальный учебный JSON-файл. Во внешнюю систему ничего не отправляется.')
            if st.button('Экспортировать в локальный симулятор', disabled=sources is None or not confirmed, type='primary'):
                operation = 'export'
        elif latest and latest['action'] != 'approve':
            st.info('Для сохранённого решения экспорт недоступен. ' + CONSEQUENCES[latest['action']])
        st.caption('Локальная учебная очередь создаёт запись для ручного разбора. Реальный получатель не назначается; сообщение коллеге не отправляется.')
        if st.button('Зарегистрировать в локальной учебной очереди'):
            operation = 'manual_queue'
        if operation:
            try:
                request = DeliveryRequest(schema_version='1.0', job_id=job['job_id'], report=reference,
                    decision_id=latest['decision_id'] if operation == 'export' else None,
                    destination='simulator-export' if operation == 'export' else 'simulator-manual-queue', operation=operation)
                pending = deliveries.create(request, principal, 'delivery-' + uuid4().hex)
                outcome = deliveries.dispatch(pending.delivery_id, principal, pending.revision)
                if outcome.status == 'delivered':
                    st.session_state.notice = 'Локальный симулятор подтвердил сохранение.'
                else:
                    st.session_state.delivery_warning = 'Доставка не подтверждена. Запись сохранена; проверьте её состояние и повторите доставку ниже.'
                st.rerun()
            except (WorkflowError, ValueError) as exc:
                show_error(exc)
                st.info('Если запись доставки уже создана, обновите состояние и повторите именно её ниже.')
        for delivery in existing:
            label = 'Учебный экспорт' if delivery.operation == 'export' else 'Запись в локальной очереди'
            status = {'pending': 'Ожидает подтверждения', 'failed': 'Попытка не завершена', 'delivered': 'Сохранение подтверждено'}[delivery.status]
            with st.container(border=True):
                st.text(label + ' · ' + status)
                st.caption('Доставка: ' + delivery.delivery_id)
                if delivery.status in ('pending', 'failed'):
                    stale_decision = delivery.operation == 'export' and (not latest or delivery.decision_id != latest['decision_id'])
                    if stale_decision:
                        st.caption('Эта доставка относится к прежнему решению. Повтор недоступен; для нового одобрения создайте новый экспорт выше.')
                    else:
                        st.caption('Повтор использует ту же запись доставки и не создаёт новую.')
                    if st.button('Повторить локальную доставку', key='retry-' + delivery.delivery_id,
                                 disabled=stale_decision or (delivery.operation == 'export' and sources is None)):
                        try:
                            outcome = deliveries.dispatch(delivery.delivery_id, principal, delivery.revision)
                            if outcome.status != 'delivered':
                                st.session_state.delivery_warning = 'Доставка по-прежнему не подтверждена. Проверьте состояние и код ошибки в сведениях доставки.'
                            st.rerun()
                        except WorkflowError as exc:
                            show_error(exc)
                if delivery.status == 'delivered' and delivery.operation == 'export':
                    st.download_button('Скачать сохранённый учебный отчёт', read_or_stop(deliveries.export_artifact, delivery.delivery_id, principal),
                                       file_name=delivery.delivery_id + '.json', mime='application/json', key='download-' + delivery.delivery_id)
                with st.expander('Квитанция и технические сведения'):
                    st.json(delivery.model_dump(mode='json'))
        if report:
            st.button('Открыть решение', on_click=go, args=(3,))

    if job and step >= 2:
        with st.expander('Ход обработки и сведения для разбора'):
            st.caption('Только реально полученные события. Время означает сохранение события; при остановке процесса журнал может быть неполным.')
            events = read_or_stop(store.list_runtime_events, job['job_id'], principal)
            if events:
                st.dataframe([{'№': e['sequence'], 'Время': date_label(e['received_at']),
                               'Этап': EVENT_STAGES.get(e['event']['stage'], e['event']['stage']),
                               'Результат': EVENT_CODES.get(e['event']['code'], e['event']['code'])}
                              for e in events], hide_index=True, use_container_width=True)
            else:
                st.caption('События обработки пока не записаны.')
            st.text('Идентификатор задания: ' + job['job_id'])
            st.json(job)

    # Observe job publication too, independently of worker cleanup/readiness.
    polling = state['state'] in ('preparing', 'busy') or (job and job['status'] in ('queued', 'running'))
    @st.fragment(run_every=1.0 if polling else None)
    def watch_progress():
        changed = service.readiness() != state
        if job and job['status'] in ('queued', 'running'):
            current = read_or_stop(store.get_job, job['job_id'], principal)
            changed = changed or current['revision'] != job['revision']
        if changed:
            st.rerun()
    if preparation_started:
        st.rerun()
    watch_progress()


# Windows spawn must not render UI or acquire another controller lease.
if __name__ == '__main__':
    main()
