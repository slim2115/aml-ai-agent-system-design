"""Open-rubric practice. Answers stay in the browser session until downloaded."""
import json

from qa.investigations import storage
from qa.investigations.core import assess_answer


def render():
    import streamlit as st
    st.subheader('Учебные расследования')
    st.write('Прочитайте факты, выберите вывод, причину и следующий шаг. Укажите факты, '
             'которые непосредственно подтверждают вывод. Рубрика открытая: это практика разбора свидетельств.')
    st.caption('PASS означает совпадение с учебной рубрикой. Компетентность оператора: NOT_MEASURED. '
               'Свободное обоснование не оценивается автоматически. Живые Kafka, PostgreSQL и LLM здесь не запускаются.')
    try:
        packs = storage.list_packs()
        if not packs:
            st.warning('Учебных пакетов пока нет. Создайте пакет командой из инструкции, раздел 10.14.')
            return
        run_id = st.sidebar.selectbox('Учебный пакет', packs)
        with st.spinner('Проверяю свидетельства и текущие исходники…'):
            view = storage.load_pack(run_id)
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        st.error('Учебный пакет не прошёл проверку целостности или структуры. Код: LEARNING_PACK_ERROR. '
                 'Сохраните его для разбора и создайте новый пакет с другим именем.')
        return
    st.text('Целостность пакета: ' + view['integrity'] + ' · Текущий код: ' + view['source_check'])
    st.caption('Локальные хэши обнаруживают изменения, но не являются независимой подписью. '
               'Данные получены из синтетических контролей; это не замер поведения реальной модели.')
    lessons = {item['id']: item for item in view['catalog']['lessons']}
    lesson_id = st.selectbox('Расследование', list(lessons),
                            format_func=lambda key: key + ' — ' + lessons[key]['title'])
    lesson = lessons[lesson_id]
    st.text(lesson['claim'])
    st.text('Область: ' + lesson['scope'])
    st.text('Требования: ' + ', '.join(lesson['requirements']))
    facts = next(item['facts'] for item in view['lessons'] if item['id'] == lesson_id)
    for fact in facts:
        with st.expander(fact['id'] + ' — ' + fact['label'], expanded=True):
            st.code(json.dumps(fact['value'], ensure_ascii=False, indent=2), language='json')
            st.text('Источник: ' + fact['source'])
    if not view['assessment_enabled']:
        st.warning('Исходники изменились после подготовки пакета. Просмотр доступен; для оценки создайте новый пакет.')
        return
    binding = view['fingerprint'] + ':' + lesson_id
    with st.form('answer-' + binding):
        answer = {'schema_version': '1.0', 'lesson_id': lesson_id}
        for name in ('conclusion', 'root_cause', 'next_action'):
            question = lesson['questions'][name]
            labels = {option['id']: option['label'] for option in question['options']}
            answer[name] = st.selectbox(question['prompt'], list(labels), format_func=labels.get,
                                        index=None, key=binding + ':' + name,
                                        placeholder='Выберите вариант')
        answer['evidence_ids'] = st.multiselect('Подтверждающие факты', ['F01', 'F02', 'F03', 'F04'],
                                                key=binding + ':evidence')
        answer['rationale'] = st.text_area('Обоснование своими словами (без автоматической оценки)',
                                         max_chars=2000, key=binding + ':rationale')
        submitted = st.form_submit_button('Проверить разбор')
    if submitted:
        # A form may have remained open while files or source changed.
        try:
            with st.spinner('Повторно проверяю пакет перед оценкой…'):
                fresh = storage.load_pack(run_id)
            if not fresh['assessment_enabled'] or fresh['fingerprint'] != view['fingerprint']:
                raise ValueError('LEARNING_PACK_CHANGED')
            evaluation = assess_answer(fresh['catalog'], lesson_id, answer)
        except (OSError, ValueError, KeyError, TypeError, IndexError):
            st.error('Пакет или исходники изменились. Обновите страницу и подготовьте новый пакет.')
            return
        st.text('Результат разбора: ' + evaluation['status'])
        for criterion in evaluation['criteria']:
            st.text(criterion['id'] + ': ' + criterion['status'])
        st.write(evaluation['explanation'])
        st.write('Проверка регрессии: ' + evaluation['regression_hint'])
        st.caption('Обоснование: NOT_SCORED. Практика человека: NOT_MEASURED. Продуктовая приёмка: INCOMPLETE.')
        st.download_button('Скачать учебный разбор JSON', json.dumps({
            'schema_version': '1.0', 'run_id': run_id, 'pack_fingerprint': view['fingerprint'],
            'answer': answer, 'evaluation': evaluation}, ensure_ascii=False, indent=2),
            file_name=lesson_id + '-learning-answer.json', mime='application/json')
    with st.expander('Открытая рубрика и ожидаемый разбор'):
        st.json(lesson['rubric'])
        st.write(lesson['explanation'])
        st.caption('Открытая рубрика помогает учиться; результат не является независимой оценкой знаний.')
