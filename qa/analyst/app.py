"""Local QA analyst view. Launch separately from the AML operator service."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qa.analyst import reader

LABELS = {
    'execution': 'Общий прогон и требования',
    'evaluator-qualification': 'Квалификация оценщиков',
    'environments': 'Сценарии окружения',
    'stability': 'Повторы и статистика',
    'autonomy': 'Исправление ответа и бюджет',
}
STATE = {'PASS': 'Проверки пройдены', 'FAIL': 'Есть провал', 'ERROR': 'Ошибка проверки',
         'INCOMPLETE': 'Проверка не завершена', 'UNAVAILABLE': 'Результат недоступен'}
FIELD_LABELS = {
    'mode': 'Режим', 'id': 'Проверка', 'status': 'Статус', 'case_id': 'Случай', 'control': 'Контроль',
    'first_attempt_status': 'Первый ответ', 'final_candidate_status': 'Итоговый кандидат',
    'termination_reason': 'Причина остановки', 'attempt_index': 'Попытка', 'issues': 'Причины',
    'execution_status': 'Завершение запуска', 'deterministic_status': 'Детерминированные проверки',
    'plan_fulfillment': 'Выполнение всего плана', 'version_binding': 'Привязка версии при запуске',
    'pending_profiles': 'Невыполненные профили', 'error_code': 'Код ошибки',
    'observed_failure': 'Сохранённый провал', 'known_tokens': 'Известные токены',
    'charged_tokens': 'Учтено с резервами', 'total_tokens': 'Полный расход',
    'unknown_usage_attempts': 'Попытки с неизвестным расходом', 'attempted_calls': 'Попыток учтено',
    'elapsed_seconds': 'Длительность, с', 'max_attempts': 'Лимит попыток', 'max_tokens': 'Лимит токенов',
    'wall_seconds': 'Лимит времени, с', 'tokens': 'Токены попытки', 'stop_reason': 'Остановка бюджета',
    'planned_slots': 'Запланировано', 'observed_slots': 'Наблюдалось', 'missing_slots': 'Нет записи',
    'not_run_slots': 'Явно не выполнено', 'family_id': 'Семейство', 'arm': 'Вариант',
    'known_sum': 'Известный расход', 'unknown_attempts': 'Неизвестных попыток', 'recorded_total': 'Полный расход',
    'collected_tests': 'Собрано тестов', 'additional_subtests': 'Подслучаи',
    'sequence': 'Порядок', 'event': 'Событие', 'reason': 'Обоснование', 'path': 'Файл', 'size': 'Байт',
}
GUIDANCE = {
    'execution': 'Начните с профилей: NOT_RUN означает, что проверка не выполнялась. Затем откройте требования и причины выбора проверок.',
    'evaluator-qualification': 'PASS означает обнаружение заранее заданных дефектов на учебных контролях. Качество настоящего LLM Judge оценивается отдельно.',
    'environments': 'Здесь моделируются сбои зависимостей. Успех сценария не подтверждает работу живой Kafka или PostgreSQL.',
    'stability': 'Сравните план и наблюдения. Повторы одного случая не являются независимыми случаями. Неизвестный расход не равен нулю.',
    'autonomy': 'Сопоставьте первый ответ и итогового кандидата. Исправление не отменяет первый FAIL. PASS учебного контроля может означать ожидаемую остановку ошибочного цикла.',
}


def main():
    import streamlit as st
    st.set_page_config(page_title='AML — аналитик QA', layout='wide')
    st.title('AML — аналитик QA')
    st.caption('Локальные свидетельства Dark Factory · просмотр без запуска тестов и моделей')
    st.info('Продуктовая приёмка: INCOMPLETE. Допуск кандидата: NOT_EVALUATED. '
            'Этот экран не одобряет AML-решения и релизы.')
    with st.sidebar:
        st.header('Выбор проверки')
        kind = st.selectbox('Тип проверки', list(LABELS), format_func=LABELS.get)
        st.button('Обновить список')
        st.caption('Имена в обратном алфавитном порядке. Старые запуски сохраняются.')
    try:
        runs = reader.list_runs(kind)
    except (OSError, ValueError):
        st.error('Каталог результатов недоступен или нарушает ограничения чтения. Проверьте локальные файлы. Код: ANALYST_LIST_ERROR')
        return
    if not runs:
        st.warning('Сохранённых запусков этого типа пока нет.')
        st.write('Выполните соответствующую команду QA из разделов 10.8–10.12 инструкции, затем обновите список.')
        return
    with st.sidebar:
        run_id = st.selectbox('Сохранённый запуск', runs, key='run-' + kind)
    st.subheader(LABELS[kind])
    st.text('Запуск: ' + run_id)
    st.write(GUIDANCE[kind])
    verify = st.button('Проверить файлы и текущий код')
    st.caption('Проверка читает локальные хэши. Она не повторяет тесты и не измеряет сегодняшнюю модель или окружение. '
               'После любого следующего действия проверку нужно выполнить заново.')
    view = reader.load_run(kind, run_id, verify=verify)
    st.text('Записанный итог: ' + view['recorded_status'] + ' · ' + STATE.get(view['recorded_status'], 'Неизвестно'))
    st.text('Чтение отчёта: ' + view['read_status'])
    if view['read_status'] == 'ERROR':
        st.error('Свидетельства не удалось прочитать согласованно. Записанный итог нельзя использовать как подтверждение.')
    elif view['read_status'] == 'INCOMPLETE':
        st.warning('Итогового result.json ещё нет. Запуск мог прерваться или продолжаться; работающий процесс здесь не проверяется.')
    elif view['recorded_status'] in ('FAIL', 'ERROR'):
        st.warning('В сохранённом запуске есть провал или ошибка. Начните с причины и невыполненных проверок ниже.')
    for issue in view['issues']:
        st.text('Код: ' + issue)
    st.subheader('Достоверность и актуальность')
    st.text('Свидетельства по локальному перечню: ' + view['evidence_check'])
    st.text('Совпадение с текущими исходниками: ' + view['source_check'])
    if view['source_check'] == 'DIFFERENT':
        st.warning('Код изменился после запуска. Сохранённый результат относится к прежней версии; для текущей нужен новый прогон.')
    elif view['source_check'] == 'MATCH' and view['evidence_check'] == 'MATCH':
        st.success('На момент проверки байты свидетельств и снимок исходников совпадают. Это не новая приёмка продукта.')
    elif view['evidence_check'] == 'ERROR':
        st.error('Часть свидетельств отсутствует, изменена или недоступна. Не используйте этот запуск для подтверждения качества.')
    if view['checked_at_utc']:
        st.text('Проверено UTC: ' + view['checked_at_utc'])
    st.caption('Хэши не являются независимой подписью. Записанный итог не пересчитывается оценщиками. '
               'Произвольные старые отчёты, секреты, ответы модели и журналы с payload здесь не выводятся.')
    with st.expander('Как читать статусы и числа'):
        st.write('PASS — ожидание выполнено в области проверки; FAIL — обнаружен провал; ERROR — ошибка выполнения или чтения; '
                 'INCOMPLETE — данных недостаточно; NOT_RUN — проверка не выполнялась. '
                 '«Не измерено» означает отсутствие значения, а не нулевой расход. В demo время и поставщик ответов моделируются.')
    if view['summary']:
        with st.expander('Паспорт и незавершённые проверки', expanded=True):
            for key, value in view['summary'].items():
                label = FIELD_LABELS.get(key, key)
                st.text(label + ': ' + ('не измерено' if value is None else str(value)))
    for title, values in view['tables'].items():
        if values:
            with st.expander(title, expanded=title in ('Профили проверок', 'Случаи и исправления', 'Бюджет')):
                # All values are selected scalars, never HTML or dynamic link columns.
                displayed = [{FIELD_LABELS.get(key, key): 'не измерено' if value is None else str(value)
                              for key, value in row.items()} for row in values]
                st.dataframe(displayed, hide_index=True, width='stretch')
    if view['changed_sources']:
        with st.expander('Изменённые исходники'):
            st.dataframe(view['changed_sources'], hide_index=True, width='stretch')
    if view['pins']:
        with st.expander('Перечень свидетельств'):
            st.dataframe(view['pins'], hide_index=True, width='stretch')
    st.subheader('Следующее действие аналитика')
    st.write('Зафиксируйте имя запуска и код причины. При изменении файлов сохраните старые свидетельства и выполните новый прогон '
             'с новым именем по инструкции. Для NOT_RUN выберите отдельный профиль проверки. Человеческую калибровку выполняют отдельно.')


if __name__ == '__main__':
    main()
