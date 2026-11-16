"""Known defects and missing evidence for the public multi-topic retrieval experiment."""
from copy import deepcopy
import json

import pytest

from qa.dark_retrieval import live_matrix, matrix
from qa.dark_retrieval.matrix_cases import cases, citation


def controlled_rows():
    observations = {}
    for case in cases():
        day = case['as_of'][:10]
        active = [citation(r) for r in case['records']
                  if r['effective_from'] <= day < r.get('effective_until', '9999-12-31')]
        relevant = case['relevant']
        ordered = relevant + [c for c in active if c not in relevant]
        # Targets lie exactly on .50, distractors in negatives exactly on .25.
        first = 0.50 if relevant else 0.25
        observations[case['case_id']] = [{'citation': c, 'distance': first + i / 10}
                                         for i, c in enumerate(ordered[:3])]
    return observations


def test_cutoff_boundary_false_accepts_and_rejects_use_distinct_denominators():
    result = matrix.score(controlled_rows())
    assert result['ranking']['hit_at_1'] == {'numerator': 10, 'denominator': 10}
    strict = result['selectors']['top1-distance-025']['assessment']
    assert strict['false_reject'] == {'numerator': 10, 'denominator': 10}
    assert strict['false_accept'] == {'numerator': 6, 'denominator': 7}
    medium = result['selectors']['top1-distance-050']['assessment']
    assert medium['false_reject'] == {'numerator': 0, 'denominator': 10}
    assert medium['status'] == 'FAIL' and result['measurement_status'] == 'PASS'
    assert result['product_selection_observed'] is False and result['recommended_threshold'] is None


def test_wrong_top1_counts_as_unsupported_selection_not_as_a_refusal():
    rows = controlled_rows()
    key = 'MX-01-old-active'
    rows[key][0]['citation'], rows[key][1]['citation'] = rows[key][1]['citation'], rows[key][0]['citation']
    result = matrix.score(rows)
    selection = result['selectors']['top1-unconditional']['assessment']
    bad = next(r for r in selection['rows'] if r['case_id'] == key)
    assert bad['decision']['unsupported_selected_count'] == bad['decision']['missed_relevant_count'] == 1
    assert not bad['decision']['false_reject']
    assert result['ranking']['hit_at_1']['numerator'] == 9 and result['ranking']['recall_at_3']['numerator'] == 10


def test_missing_observation_is_not_an_observed_refusal_and_does_not_hide_failure():
    rows = controlled_rows()
    del rows['MX-no-active']
    result = matrix.score(rows)
    candidate = result['selectors']['top1-unconditional']['assessment']
    assert result['measurement_status'] == 'INCOMPLETE' and candidate['status'] == 'FAIL'
    assert candidate['false_accept'] == {'numerator': 6, 'denominator': 6}
    assert candidate['incomplete'] == 1


@pytest.mark.parametrize('damage', ['stale-edition', 'truncated', 'invalid-distance', 'unknown-case'])
def test_counterfeit_or_truncated_rankings_are_rejected(damage):
    rows = controlled_rows()
    key = 'MX-01-new-active'
    if damage == 'stale-edition':
        rows[key][0]['citation'] = rows['MX-01-old-active'][0]['citation']
    elif damage == 'truncated':
        rows[key].pop()
    elif damage == 'invalid-distance':
        rows[key][-1]['distance'] = 2.01
    else:
        rows['unplanned-query'] = []
    with pytest.raises(ValueError):
        matrix.score(rows)


def test_replay_recomputes_assessment_and_rejects_row_from_another_attempt(tmp_path, monkeypatch):
    monkeypatch.setattr(live_matrix, 'ROOT', tmp_path)
    monkeypatch.setattr(live_matrix, 'hashes', lambda: {live_matrix.MANIFEST: 'a' * 64})
    output = tmp_path / 'var/qa/dark-retrieval/test-run'
    output.mkdir(parents=True)
    marker = live_matrix.attempt(output, 30)
    (output / 'attempt.json').write_text(json.dumps(marker), encoding='utf-8')
    raw = controlled_rows()
    hashes = {}
    for case_id, observation in raw.items():
        doc = {'run_id': output.name, 'case_id': case_id, 'catalog_sha256': marker['catalog_sha256'],
               'raw': observation}
        hashes[case_id] = matrix.digest(doc)
        (output / f'{case_id}.json').write_text(json.dumps(doc), encoding='utf-8')
    assessment = matrix.score(raw)
    child = {'profile': live_matrix.PROFILE, 'run_id': output.name, 'model_id': live_matrix.MODEL,
             'dependency': 'real_local_embedding_cpu', 'manifest_sha256': 'a' * 64,
             'source_sha256_after': marker['source_sha256_before'], 'rows_sha256': hashes,
             'corpus_sha256': sorted({matrix.digest(c['records']) for c in cases()}),
             'assessment_sha256': matrix.digest(assessment)}
    path = output / 'child-result.json'
    path.write_text(json.dumps(child), encoding='utf-8')
    assert live_matrix.replay(output) == assessment
    forged = deepcopy(child)
    forged['assessment_sha256'] = '0' * 64
    path.write_text(json.dumps(forged), encoding='utf-8')
    with pytest.raises(ValueError, match='ASSESSMENT_MISMATCH'):
        live_matrix.replay(output)
    path.write_text(json.dumps(child), encoding='utf-8')
    row_path = output / 'MX-velocity.json'
    doc = json.loads(row_path.read_bytes())
    doc['run_id'] = 'other-run'
    row_path.write_text(json.dumps(doc), encoding='utf-8')
    with pytest.raises(ValueError, match='ROW_MISMATCH'):
        live_matrix.replay(output)
