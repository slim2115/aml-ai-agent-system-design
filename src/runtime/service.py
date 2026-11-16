"""Read-only v1 service. Explicit synthetic mode; no human approval or delivery.

Budget checks discard late results. Worker cancellation is a later job concern.
"""
from datetime import datetime, timezone
import json
import time
from typing import get_args
from uuid import uuid4

from src.config import get_demo_paths, get_settings
from src.contracts.semantics import classify_incident_id
from src.contracts.v1 import ADAPTERS, ErrorCode, ErrorResult, RefusalCode, RefusalResult
from src.runtime.events import RunEvent
from src.runtime.transport import LocalOllamaTransport, TransportFailure

REFUSAL_MESSAGES = {
    'POLICY_NOT_APPROVED': 'Предметная политика не утверждена. Доступен явно выбранный учебный режим.',
    'INSUFFICIENT_DATA': 'В снимке недостаточно данных для проверяемого черновика.',
    'CONFLICTING_DATA': 'В снимке обнаружены противоречивые данные.',
    'NO_APPLICABLE_RULE': 'Не найден полный набор оснований, заданный учебной политикой.',
    'AMBIGUOUS_RULE': 'Редакции нормативного основания неоднозначны.',
    'UNTRUSTED_INPUT': 'Входные данные не прошли проверку доверия.',
    'VALIDATION_FAILED': 'Не удалось получить результат, удовлетворяющий проверкам.',
}
ERROR_MESSAGES = {
    'MISSING_INPUT': 'Укажите идентификатор инцидента.',
    'INVALID_INPUT_FORMAT': 'Ожидается точный формат INC-000123.',
    'NOT_FOUND': 'Инцидент отсутствует в выбранном снимке.',
    'NOT_READY': 'Локальные зависимости или входной снимок не готовы.',
    'DEADLINE_EXCEEDED': 'Время выполнения истекло; результат не опубликован.',
    'DEPENDENCY_UNAVAILABLE': 'Ошибка локальной зависимости.',
    'INTERNAL_ERROR': 'Внутренняя ошибка обработки.',
}


class AMLAgent:
    """No I/O in constructor. Dependencies may be injected by trusted local tests."""
    def __init__(self, *, mode='production', engine='template', builder=None,
                 policy=None, index_factory=None, transport=None, settings=None,
                 clock=time.monotonic, utcnow=None, event_sink=None):
        self.mode, self.engine = mode, engine
        self._builder, self._policy = builder, policy
        self._index_factory, self._transport = index_factory, transport
        self._settings, self._clock = settings, clock
        self._utcnow = utcnow or (lambda: datetime.now(timezone.utc).isoformat())
        self._event_sink = event_sink
        self.last_events: tuple[RunEvent, ...] = ()

    def run(self, incident_id) -> dict:
        from src.runtime.context import ContextBuilder, ContextFailure
        from src.runtime.generation import GenerationViolation, prepare_generation, render_generation
        from src.runtime.policy import PolicyFailure, load_demo_policy
        from src.runtime.retrieval import RetrievalFailure, RuleIndex

        correlation = 'run-' + uuid4().hex
        events = []
        active_stage = 'input'
        completeness = 'unassessed'
        deadline = None

        def event(stage, code, attempt=0):
            nonlocal active_stage
            if code == 'STARTED':
                active_stage = stage
            observed = RunEvent(correlation, stage, code, attempt)
            events.append(observed)
            if self._event_sink is not None:
                # Trusted operational adapter only. Payloads, prompts and raw
                # exceptions never cross this boundary.
                self._event_sink(observed.to_dict())

        def check_budget():
            if deadline is not None and self._clock() >= deadline:
                raise TransportFailure('DEADLINE_EXCEEDED')

        def error(code):
            if code not in get_args(ErrorCode):
                code = 'INTERNAL_ERROR'
            return ErrorResult(schema_version='1.0', status='error', generated_at=self._utcnow(),
                correlation_id=correlation, code=code,
                message=ERROR_MESSAGES.get(code, ERROR_MESSAGES['INTERNAL_ERROR']),
                retryable=code in {'NOT_READY', 'DEPENDENCY_UNAVAILABLE'}).model_dump(mode='json')

        def refuse(code):
            return RefusalResult(schema_version='1.0', status='refusal', incident_id=incident_id,
                generated_at=self._utcnow(), data_completeness=completeness,
                reason_code=code, reason=REFUSAL_MESSAGES[code],
                recommended_action='manual_review').model_dump(mode='json')

        def work():
            nonlocal completeness, deadline
            event('input', 'STARTED')
            code = classify_incident_id(incident_id)
            if code:
                return error(code)
            event('input', 'PASSED')
            if self.mode != 'demo':
                event('policy', 'POLICY_NOT_APPROVED')
                return refuse('POLICY_NOT_APPROVED')
            if self.engine not in {'template', 'local-model'}:
                return error('NOT_READY')
            event('policy', 'STARTED')
            settings = self._settings or get_settings()
            deadline = self._clock() + settings.performance.generation_timeout_sec
            root = settings.project_root
            policy_path, snapshot_dir = get_demo_paths(root)
            policy = self._policy or load_demo_policy(policy_path)
            if policy.purpose != 'synthetic_learning_only' or policy.expert_approved is not False:
                return refuse('POLICY_NOT_APPROVED')
            event('policy', 'PASSED')
            event('context', 'STARTED')
            prepared = (self._builder or ContextBuilder(snapshot_dir, policy)).build(incident_id)
            context = ADAPTERS['InvestigationContext'].validate_python(prepared.context.model_dump(mode='json'))
            if context.incident_id != incident_id or context.policy_version != policy.policy_id:
                return refuse('CONFLICTING_DATA')
            completeness = context.data_completeness
            event('context', 'PASSED')
            check_budget()
            if context.consistency != 'consistent':
                return refuse('CONFLICTING_DATA')
            if completeness != 'full':
                return refuse('INSUFFICIENT_DATA')
            allowed = policy.applicability.get(prepared.alert_type, [])
            if not allowed:
                return refuse('NO_APPLICABLE_RULE')
            event('retrieval', 'STARTED')
            index = (self._index_factory(prepared.rule_corpus_path) if self._index_factory is not None
                     else RuleIndex.from_file(prepared.rule_corpus_path, settings=settings))
            check_budget()
            if index.provenance.get('source_sha256') != prepared.rule_corpus_sha256:
                return error('NOT_READY')
            count = index.active_chunk_count(context.window.as_of)
            if count == 0:
                return refuse('NO_APPLICABLE_RULE')
            candidates = index.search(prepared.alert_type, as_of=context.window.as_of, k=count)
            selected = [chunk for chunk in candidates if chunk.citation.rule_id in allowed]
            if {chunk.citation.rule_id for chunk in selected} != set(allowed):
                return refuse('NO_APPLICABLE_RULE')
            check_budget()
            event('retrieval', 'PASSED')
            request = prepare_generation(context, [chunk.as_applicable() for chunk in selected])
            report_id = 'report-' + uuid4().hex
            for attempt in range(1, 3):
                check_budget()
                event('generation', 'STARTED', attempt)
                if self.engine == 'template':
                    raw = json.dumps({'schema_version': '1.0', 'fact_order': list(request.fact_ids),
                                      'rule_order': list(request.rule_keys)})
                else:
                    payload = request.model_prompt_payload
                    if attempt > 1:
                        payload['validation_feedback'] = 'INVALID_SELECTION_RETURN_COMPLETE_CATALOG'
                    raw = (self._transport or LocalOllamaTransport(settings)).complete(
                        payload, request.response_schema, deadline=deadline)
                check_budget()
                event('validation', 'STARTED', attempt)
                try:
                    report = render_generation(request, raw, report_id=report_id, generated_at=self._utcnow())
                except GenerationViolation:
                    event('validation', 'RETRY' if attempt < 2 else 'VALIDATION_FAILED', attempt)
                    continue
                check_budget()
                event('validation', 'PASSED', attempt)
                return report.model_dump(mode='json')
            return refuse('VALIDATION_FAILED')

        try:
            result = work()
        except ContextFailure as exc:
            result = refuse(exc.code) if exc.code in get_args(RefusalCode) else error(exc.code)
        except PolicyFailure:
            result = refuse('POLICY_NOT_APPROVED')
        except RetrievalFailure as exc:
            result = refuse('AMBIGUOUS_RULE') if exc.code == 'AMBIGUOUS_RULE' else error('NOT_READY')
        except TransportFailure as exc:
            result = error(exc.code)
        except GenerationViolation:
            result = refuse('VALIDATION_FAILED')
        except Exception:
            result = error('INTERNAL_ERROR')
        result = ADAPTERS['InvestigationResult'].validate_python(result).model_dump(mode='json')
        # Late failures/refusals and final contract validation consume the same
        # budget as successful stages. No late result is published as on-time.
        if deadline is not None and self._clock() >= deadline:
            result = error('DEADLINE_EXCEEDED')
        if result['status'] != 'draft':
            failure_code = result.get('code') or result.get('reason_code')
            if not events or events[-1].stage != active_stage or events[-1].code != failure_code:
                event(active_stage, failure_code, events[-1].attempt if events else 0)
        event('complete', result.get('code') or result.get('reason_code') or 'DRAFT')
        self.last_events = tuple(events)
        return result
