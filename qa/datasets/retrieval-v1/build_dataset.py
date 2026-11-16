"""Author retrieval v1 once, using source text and explicit labels, never a model.

This authoring command refuses to replace an existing catalogue or manifest.
The source corpus is synthetic and carries no claim of legal approval.
"""
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
QUERY_TEXT = {
    'structuring': 'Признаки разбиения суммы на несколько операций',
    'threshold': 'Порог суммы операции обязательного контроля',
    'threshold_breach': 'Порог суммы операции обязательного контроля',
    'kyc': 'Идентификация клиента и обновление сведений',
    'kyc_mismatch': 'Идентификация клиента и обновление сведений',
    'high_risk': 'Юрисдикции с повышенным уровнем риска',
    'high_risk_country': 'Юрисдикции с повышенным уровнем риска',
    'reporting': 'Порядок представления сведений об операциях',
    'velocity': 'Частота операций клиента за период',
}
TARGETS = {'structuring': 'R-115-002', 'threshold': 'R-115-006',
           'kyc': 'R-115-007', 'high_risk': 'R-AML-010', 'reporting': 'R-AML-015'}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8')


def citation(row):
    digest = sha256(row['text'].encode('utf-8')).hexdigest()
    identity = {'rule_id': row['rule_id'], 'version': row['version'],
                'source_ref': row['regulation_ref'], 'char_start': 0,
                'char_end': len(row['text']), 'text_sha256': digest}
    return {'rule_id': row['rule_id'], 'rule_version': row['version'],
            'source_ref': row['regulation_ref'],
            'chunk_id': 'chunk-' + sha256(canonical(identity)).hexdigest(),
            'content_sha256': digest}


def active(rows, timestamp):
    day = datetime.fromisoformat(timestamp.replace('Z', '+00:00')).astimezone(timezone.utc).date().isoformat()
    return [row for row in rows if row['effective_from'] <= day
            and (row.get('effective_until') is None or day < row['effective_until'])]


def main():
    outputs = ['cases.json', 'manifest.json', 'corpora/base.json', 'corpora/editions.json']
    if any((HERE / name).exists() for name in outputs):
        raise RuntimeError('DATASET_ALREADY_AUTHORED')
    corpora, source_bytes, rows = {}, {}, {}
    for corpus, fixture in [('base', 'S01'), ('editions', 'S04')]:
        source = f'qa/datasets/aml-v1/fixtures/{fixture}/rules.json'
        raw = (ROOT / source).read_bytes()
        source_bytes[corpus] = raw
        rows[corpus] = json.loads(raw.decode('utf-8'))
        corpora[corpus] = {'path': f'corpora/{corpus}.json', 'source_path': source,
                          'source_sha256': sha256(raw).hexdigest()}
    moment = '2026-07-20T00:00:00Z'
    cases = []

    def add(query, group, *, corpus='base', as_of=moment, kind='primary'):
        eligible = active(rows[corpus], as_of)
        target = TARGETS.get(group)
        relevant = [citation(row) for row in eligible if row['rule_id'] == target]
        cases.append({'id': f'R{len(cases) + 1:02d}', 'query': query, 'corpus': corpus,
                      'as_of': as_of, 'group': group, 'primary': kind == 'primary',
                      'kind': kind, 'relevant': relevant,
                      'expected_active': sorted([citation(row) for row in eligible], key=lambda row: row['chunk_id'])})

    for query in TARGETS:
        add(query, query)
    for query, group in [('threshold_breach', 'threshold'), ('kyc_mismatch', 'kyc'),
                         ('high_risk_country', 'high_risk')]:
        add(query, group, kind='alias')
    for moment in ['2026-07-19T23:59:59Z', '2026-07-20T00:00:00Z', '2026-07-20T03:00:00+03:00']:
        add('threshold', 'threshold', corpus='editions', as_of=moment, kind='edition')
    add('velocity', 'velocity', kind='no_relevance')
    add('threshold', 'threshold', as_of='2023-12-31T23:59:59Z', kind='no_active')
    catalog = {'schema_version': '1.0', 'dataset_version': '1.0.0',
               'purpose': 'synthetic_learning_only', 'expert_approved': False,
               'k_values': [1, 3], 'metric_mode': 'descriptive_no_threshold',
               'query_text': QUERY_TEXT,
               'query_source': {'path': 'src/runtime/retrieval.py',
                                'sha256': sha256((ROOT / 'src/runtime/retrieval.py').read_bytes()).hexdigest()},
               'parent_manifest': {'path': 'qa/datasets/aml-v1/manifest.json',
                                   'sha256': sha256((ROOT / 'qa/datasets/aml-v1/manifest.json').read_bytes()).hexdigest()},
               'label_method': 'Independent direct-topic labels and exact full-text spans frozen before model execution.',
               'corpora': corpora, 'cases': cases}
    (HERE / 'corpora').mkdir(exist_ok=True)
    for corpus, raw in source_bytes.items():
        with (HERE / f'corpora/{corpus}.json').open('xb') as handle:
            handle.write(raw)
    with (HERE / 'cases.json').open('xb') as handle:
        handle.write(encoded(catalog))
    files = []
    for name in ['README.md', 'build_dataset.py', 'cases.json', 'corpora/base.json', 'corpora/editions.json']:
        raw = (HERE / name).read_bytes()
        files.append({'path': name, 'bytes': len(raw), 'sha256': sha256(raw).hexdigest()})
    with (HERE / 'manifest.json').open('xb') as handle:
        handle.write(encoded({'schema_version': '1.0', 'dataset_version': '1.0.0', 'files': files}))
    print(json.dumps({'status': 'AUTHORED_BEFORE_MODEL_RUN', 'cases': len(cases),
                      'primary_cases': sum(row['primary'] for row in cases),
                      'manifest_sha256': sha256((HERE / 'manifest.json').read_bytes()).hexdigest()}))


if __name__ == '__main__':
    main()
