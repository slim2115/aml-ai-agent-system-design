"""Evaluate bounded ordering artifacts with the product renderer and fixed oracles.

The frozen catalog independently binds its context and preselected rule chunks
to source files, policy and manually calculated totals. This adapter does not
measure retrieval, open-ended reasoning, usefulness of order, or AML acceptance.
The proposer receives only the product's factual catalog and response schema.
"""
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from qa.scenario_checks import Observation, canonical, check_observation
from qa.scenarios import Scenario, load_catalog
from src.qa_profile import checked_path
from src.runtime.generation import (
    GenerationRequest, GenerationViolation, prepare_generation, render_generation,
)


ROOT = Path(__file__).resolve().parents[2]
CASE_IDS = ('QA-S01', 'QA-S02', 'QA-S03', 'QA-S04', 'QA-S14')
_CANDIDATE_CODES = frozenset({
    'INVALID_MODEL_JSON', 'INVALID_MODEL_RESPONSE', 'DUPLICATE_RESPONSE_KEY',
    'NONFINITE_RESPONSE_VALUE', 'MODEL_RESPONSE_TOO_LARGE',
    'INCOMPLETE_OR_UNKNOWN_SELECTION',
})


class CandidateError(ValueError):
    """A fixed code; no raw candidate or caller path is exposed."""


@dataclass(frozen=True)
class _Case:
    scenario_json: str
    request: GenerationRequest
    generated_at: str

    def scenario(self):
        return Scenario.model_validate_json(self.scenario_json)


class CandidateCases:
    """Prepared immutable inputs; returned payloads, reports and plans are copies.

    A verified identity-order baseline is required before a provider can obtain
    input. Baseline reports and assessed candidate reports have separate stores.
    Report timestamps are fixed synthetic snapshot times, not measured latency.
    """

    def __init__(self, items):
        self._items = MappingProxyType(dict(items))
        self.case_ids = tuple(self._items)
        self._reports = {}
        rows, reports = [], {}
        for case_id, item in self._items.items():
            raw = canonical({'schema_version': '1.0',
                             'fact_order': list(item.request.fact_ids),
                             'rule_order': list(item.request.rule_keys)})
            assessment, report = self._evaluate(case_id, raw, kind='baseline')
            rows.append({'case_id': case_id, **assessment})
            if report is not None:
                reports[case_id] = report
        states = [row['status'] for row in rows]
        status = next((state for state in ('ERROR', 'FAIL', 'INCOMPLETE') if state in states), 'PASS')
        self._baseline = {'status': status, 'cases': rows, 'reports': reports,
                          'scope': 'Identity-order product rendering against frozen facts; no model or retrieval measurement'}

    def _item(self, case_id):
        if type(case_id) is not str or case_id not in self._items:
            raise CandidateError('AUTONOMY_UNKNOWN_CASE')
        return self._items[case_id]

    def _require_baseline(self):
        if self._baseline['status'] != 'PASS':
            raise CandidateError('AUTONOMY_BASELINE_REJECTED')

    @property
    def reports(self):
        """Only the latest successfully assessed candidate for each case."""
        return deepcopy(self._reports)

    def baseline(self):
        return deepcopy(self._baseline)

    def plan_cases(self, max_attempts, reservation_tokens):
        if (type(max_attempts) is not int or not 1 <= max_attempts <= 3
                or type(reservation_tokens) is not int or not 1 <= reservation_tokens <= 10_000_000):
            raise CandidateError('AUTONOMY_CASE_LIMITS')
        return [{'case_id': case_id, 'max_attempts': max_attempts,
                 'reservation_tokens': reservation_tokens} for case_id in self.case_ids]

    def payload(self, case_id):
        item = self._item(case_id)
        self._require_baseline()
        return item.request.model_prompt_payload

    def schema(self, case_id):
        item = self._item(case_id)
        self._require_baseline()
        return item.request.response_schema

    def _evaluate(self, case_id, raw, *, kind):
        item = self._item(case_id)
        try:
            report = render_generation(item.request, raw,
                report_id=f'autonomy-{case_id}-{kind}', generated_at=item.generated_at).model_dump(mode='json')
            scenario = item.scenario().model_copy(update={'engine': 'template', 'expected_model_calls': 0})
            observed = Observation(result=report, context=scenario.case.context.model_dump(mode='json'),
                selected_rule_ids=[rule['citation']['rule_id'] for rule in report['applicable_rules']],
                model_prompts=[], latency_seconds=0.0, events=[])
            issues = check_observation(scenario, observed)
            assessment = {'status': 'FAIL' if issues else 'PASS', 'issues': issues}
            return assessment, report if not issues else None
        except GenerationViolation as exc:
            if exc.code in _CANDIDATE_CODES:
                return {'status': 'FAIL', 'issues': [exc.code]}, None
            return {'status': 'ERROR', 'issues': ['AUTONOMY_RENDERER_ERROR']}, None
        except Exception:
            return {'status': 'ERROR', 'issues': ['AUTONOMY_EVALUATOR_ERROR']}, None

    def assess(self, case_id, raw):
        self._item(case_id)
        self._reports.pop(case_id, None)
        if self._baseline['status'] != 'PASS':
            return {'status': 'ERROR', 'issues': ['AUTONOMY_BASELINE_REJECTED']}
        assessment, report = self._evaluate(case_id, raw, kind='candidate')
        if report is not None:
            self._reports[case_id] = deepcopy(report)
        return deepcopy(assessment)


def prepare_cases(case_ids, root=ROOT):
    """Read the complete frozen package before preparing closed, positive cases.

    ``load_catalog`` checks source bytes, contexts, policy membership, rule
    editions and golden arithmetic before returning these inputs. Neither the
    actual vector search nor a live data-source builder is measured here.
    """
    if (type(case_ids) is not list or not 1 <= len(case_ids) <= len(CASE_IDS)
            or any(type(case_id) is not str or case_id not in CASE_IDS for case_id in case_ids)
            or len(set(case_ids)) != len(case_ids)):
        raise CandidateError('AUTONOMY_CASE_SELECTION')
    try:
        root = Path(root).absolute()
        folder = checked_path(root / 'qa/datasets/aml-v1', root)
        catalog = load_catalog(folder)
        scenarios = {scenario.case.id: scenario for scenario in catalog.cases}
        items = []
        for case_id in case_ids:
            scenario = scenarios[case_id]
            if scenario.case.expected_status != 'draft' or scenario.golden is None or scenario.case.context is None:
                raise CandidateError('AUTONOMY_CASE_NOT_APPLICABLE')
            request = prepare_generation(scenario.case.context, scenario.golden.rules)
            items.append((case_id, _Case(scenario.model_dump_json(), request,
                                         scenario.case.context.snapshot.captured_at)))
        return CandidateCases(items)
    except CandidateError:
        raise
    except Exception:
        raise CandidateError('AUTONOMY_CASE_PREPARATION_ERROR') from None
