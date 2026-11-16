"""Operator labels and exact, read-only source presentation."""
from datetime import datetime, timezone
import json

STAGES = ('1. Подготовка', '2. Инцидент', '3. Проверка отчёта', '4. Решение', '5. Сохранение и экспорт')
JOB_LABELS = {'queued': 'Задание принято', 'running': 'Идёт обработка', 'succeeded': 'Отчёт готов к проверке',
              'refused': 'Отчёт не сформирован', 'failed': 'Обработка завершилась ошибкой',
              'timed_out': 'Время обработки истекло', 'cancelled': 'Задание отменено'}
DECISIONS = {'approve': 'Одобрить учебный отчёт', 'reject': 'Отклонить отчёт', 'request_changes': 'Запросить изменения'}
CONSEQUENCES = {'approve': 'После сохранения станет доступен локальный учебный экспорт.',
                'reject': 'Отчёт будет отклонён. Экспорт недоступен; запись останется в истории.',
                'request_changes': 'Замечание сохранится. Автоматическое исправление отчёта не запускается.'}
READINESS = {'not_ready': 'Поиск не подготовлен', 'preparing': 'Поиск подготавливается',
             'ready': 'Можно создать задание', 'busy': 'Обработчик занят заданием', 'closed': 'Обработчик остановлен'}
FIELDS = {'tx_id': 'Операция', 'amount': 'Сумма', 'currency': 'Валюта', 'timestamp': 'Дата операции',
          'status': 'Статус', 'client_id': 'Клиент', 'risk_level': 'Уровень риска в источнике',
          'country': 'Страна', 'kyc_status': 'Статус KYC', 'declared_income': 'Заявленный доход'}
EVENT_STAGES = {'input': 'Проверка входных данных', 'policy': 'Учебная политика', 'context': 'Сбор фактов',
                'retrieval': 'Поиск правил', 'generation': 'Формирование отчёта', 'validation': 'Проверка результата', 'complete': 'Завершение'}
EVENT_CODES = {'STARTED': 'Начато', 'PASSED': 'Проверка пройдена', 'RETRY': 'Повтор', 'DRAFT': 'Черновик готов',
               'REFUSAL': 'Отказ', 'ERROR': 'Ошибка'}


def date_label(value):
    if not value:
        return '—'
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(timezone.utc).strftime('%d.%m.%Y %H:%M UTC')
    except (ValueError, TypeError, AttributeError):
        return str(value)


def history_label(job):
    return (f"{job['incident_id']} · {date_label(job['accepted_at'])} · "
            f"{JOB_LABELS.get(job['status'], job['status'])} · {job['job_id']}")


def cell_value(value):
    return json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)


def render_report(st, report, sources):
    st.caption('Сверьте факты с исходными значениями. Подтверждение источника не означает одобрение отчёта.')
    st.caption('Уверенность не измерена. Этот отчёт не содержит утверждённой оценки риска.')
    st.subheader('Факты и исходные значения')
    for number, fact in enumerate(report['found_facts'], 1):
        with st.container(border=True):
            st.markdown(f'**Факт {number}**')
            st.text(fact['fact'])
            with st.expander(f'Сверить источники факта {number}', expanded=number == 1):
                st.caption('Проверьте идентификатор, значение, валюту, дату и статус. Для итога — также период и состав операций.')
                if sources is None:
                    st.warning('Исходные значения не подтверждены.')
                else:
                    rows = []
                    for evidence in sources['facts'][number - 1]:
                        if evidence['kind'] in ('sum', 'count'):
                            label = 'Сумма по указанным операциям' if evidence['kind'] == 'sum' else 'Количество операций'
                            st.text(label + ': ' + str(evidence['value']))
                            window = evidence['window']
                            st.caption(f"Период: {date_label(window['start_inclusive'])} — {date_label(window['end_exclusive'])}; конец не включается.")
                            st.caption('Входят все статусы операций. Валюты сверяйте по исходным строкам; значения здесь не пересчитываются.')
                        rows.extend({'Запись': c['record_id'], 'Поле': FIELDS.get(c['field'], c['field']),
                                     'Значение': cell_value(c['value']), 'Источник': c['source_file']} for c in evidence['cells'])
                    st.dataframe(rows, hide_index=True, use_container_width=True)
                with st.expander(f'Технические сведения факта {number}'):
                    st.json({'evidence': fact['evidence'], 'resolved_sources': sources['facts'][number - 1] if sources else None,
                             'references': fact.get('sources', [])})
    st.subheader('Учебные правила')
    if not report['applicable_rules']:
        st.info('Для этого отчёта учебные правила не приведены. Это не вывод об отсутствии риска.')
    for index, rule in enumerate(report['applicable_rules']):
        with st.container(border=True):
            citation = rule['citation']
            st.text(f"{citation['rule_id']} · редакция {citation['rule_version']} · {citation['source_ref']}")
            st.text(rule['rule_text'])
            if sources:
                edition = sources['rules'][index]
                st.caption(f"Действует с {edition['effective_from']}; окончание: {edition['effective_until'] or 'не указано'} (конечная дата не включается).")
                with st.expander(f'Полная редакция правила {index + 1}'):
                    st.text(edition['edition_text'])
            with st.expander(f'Технические сведения правила {index + 1}'):
                st.json(sources['rules'][index] if sources else citation)
