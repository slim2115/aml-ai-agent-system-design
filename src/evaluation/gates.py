"""Shared metric thresholds for replay API, CLI and pytest."""
from math import isfinite

SCHEMA = 'M-01 Schema Match'
FAITHFULNESS = 'M-02 RAG Faithfulness'
RELEVANCE = 'M-03 Answer Relevance'
TRACEABILITY = 'M-04 Traceability Ratio'
REFUSAL = 'M-05 Refusal Correctness'
EVIDENCE = 'M-06 Evidence Grounding Correctness'
PRECISION = 'M-07 RAG Precision'
RECALL = 'M-07 RAG Recall'
ACCEPTANCE = 'M-08 Draft Acceptance Rate'
LATENCY_MAX = 'M-09 Max Latency (s)'
LATENCY_P95 = 'M-09 P95 Latency (s)'


def _setting(settings, section, key, default):
    if settings is None:
        return default
    group = settings.get(section, {}) if isinstance(settings, dict) else getattr(settings, section, None)
    value = group.get(key, default) if isinstance(group, dict) else getattr(group, key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
        raise ValueError('INVALID_EVALUATION_SETTINGS')
    if (section == 'thresholds' and not default <= value <= 1
            or section == 'performance' and not 0 < value <= default):
        raise ValueError('INVALID_EVALUATION_SETTINGS')
    return value


def gate(name, status, *, value=None, threshold=None, op=None, reason=None):
    return {'name': name, 'status': status, 'passed': {'PASS': True, 'FAIL': False}.get(status),
            'value': value, 'threshold': threshold, 'op': op, 'reason': reason}


def quality_gates(metrics, settings=None):
    """Return mandatory metric gates; absent measurements are INCOMPLETE, never 1.0."""
    threshold = lambda name, default: _setting(settings, 'thresholds', name, default)
    latency = _setting(settings, 'performance', 'generation_timeout_sec', 180)
    specs = [(SCHEMA, threshold('schema_match', 1.0), '>='),
             (FAITHFULNESS, threshold('faithfulness', .9), '>='),
             (RELEVANCE, .9, '>='), (TRACEABILITY, threshold('traceability', 1.0), '>='),
             (REFUSAL, threshold('refusal_correctness', .95), '>='), (EVIDENCE, 1.0, '>='),
             (PRECISION, threshold('rag_precision', 1.0), '>='),
             (RECALL, threshold('rag_recall', 1.0), '>='), (ACCEPTANCE, .8, '>='),
             (LATENCY_MAX, latency, '<='), (LATENCY_P95, latency, '<=')]
    result = []
    for name, limit, op in specs:
        measured = metrics.get(name, {})
        value = measured.get('value')
        denominator = measured.get('denominator')
        valid = (measured.get('status') == 'measured'
                 and not isinstance(denominator, bool) and isinstance(denominator, int) and denominator > 0
                 and not isinstance(value, bool) and isinstance(value, (int, float)) and isfinite(value)
                 and value >= 0 and (op == '<=' or value <= 1))
        if not valid:
            result.append(gate(name, 'INCOMPLETE', threshold=limit, op=op, reason='NOT_MEASURED'))
        else:
            passed = value <= limit if op == '<=' else value >= limit
            result.append(gate(name, 'PASS' if passed else 'FAIL', value=value, threshold=limit, op=op))
    return result
