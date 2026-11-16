"""Open, synthetic topic-presence and edition pairs, authored before measurement.

These short learning rules are not laws or expert-approved AML instructions.
Query names use the application's existing fixed query contract.
"""
from copy import deepcopy

from qa.dark_retrieval.scenarios import _citation

AS_OF = '2026-07-20T00:00:00Z'
TOPICS = (
    ('structuring', 'Разбиение суммы', 'Несколько связанных переводов дробят одну сумму на небольшие операции.'),
    ('threshold', 'Порог суммы', 'Сумма одной операции сравнивается с установленным денежным порогом.'),
    ('kyc', 'Сведения о клиенте', 'Идентификация клиента требует проверки и обновления его анкетных сведений.'),
    ('high_risk', 'Риск юрисдикции', 'Страна получателя относится к юрисдикциям с повышенным уровнем риска.'),
    ('reporting', 'Передача сведений', 'Сведения об операции передаются уполномоченному органу в форме отчёта.'),
)


def citation(record):
    return _citation(record['rule_id'], record['version'], record['regulation_ref'], record['text'])


def cases():
    records = []
    for position, (topic, title, text) in enumerate(TOPICS, 1):
        for version, start, end in (('v1', '2024-01-01', '2026-07-20'),
                                    ('v2', '2026-07-20', None)):
            record = {'rule_id': f'MATRIX-{position:02d}', 'title': title,
                      'regulation_ref': f'Synthetic learning rule {position}', 'category': topic,
                      'text': text + (' Учебная редакция один.' if version == 'v1' else ' Учебная редакция два.'),
                      'effective_from': start, 'version': version}
            if end is not None:
                record['effective_until'] = end
            records.append(record)
    result = []
    for position, (topic, _, _) in enumerate(TOPICS, 1):
        rule_id = f'MATRIX-{position:02d}'
        for slice_name, timestamp, version in (
                ('old-active', '2026-07-19T23:59:59Z', 'v1'), ('new-active', AS_OF, 'v2'),
                ('topic-absent', AS_OF, None)):
            corpus = records if version else [r for r in records if r['rule_id'] != rule_id]
            relevant = [citation(r) for r in corpus if r['rule_id'] == rule_id and r['version'] == version]
            result.append({'case_id': f'MX-{position:02d}-{slice_name}', 'family': topic,
                           'slice': slice_name, 'query': topic, 'as_of': timestamp,
                           'records': corpus, 'relevant': relevant})
    result.extend([
        {'case_id': 'MX-velocity', 'family': 'unsupported-velocity', 'slice': 'unsupported-topic',
         'query': 'velocity', 'as_of': AS_OF, 'records': records, 'relevant': []},
        {'case_id': 'MX-no-active', 'family': 'threshold', 'slice': 'no-active-edition',
         'query': 'threshold', 'as_of': '2023-12-31T23:59:59Z', 'records': records, 'relevant': []},
    ])
    return deepcopy(result)
