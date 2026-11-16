"""Deterministic JSON Schema/OpenAPI generation; does not start an API server."""
import json
from pathlib import Path

from pydantic import TypeAdapter

from .v1 import ADAPTERS, Identifier

SCHEMA_DIALECT = 'https://json-schema.org/draft/2020-12/schema'


def replace_refs(value):
    if isinstance(value, dict):
        return {key: replace_refs(item) for key, item in value.items()}
    if isinstance(value, list):
        return [replace_refs(item) for item in value]
    if isinstance(value, str) and value.startswith('#/$defs/'):
        return value.replace('#/$defs/', '#/components/schemas/', 1)
    return value


def build_artifacts() -> dict:
    definitions = {}

    def register(name, schema):
        if name in definitions and definitions[name] != schema:
            raise ValueError(f'Conflicting schema name: {name}')
        definitions[name] = schema

    for name, adapter in ADAPTERS.items():
        schema = adapter.json_schema(mode='validation')
        for nested_name, nested_schema in schema.pop('$defs', {}).items():
            register(nested_name, nested_schema)
        # A model that also occurs under another adapter may already be present.
        if schema != {'$ref': f'#/$defs/{name}'}:
            register(name, schema)

    bundle = {
        '$schema': SCHEMA_DIALECT,
        'title': 'AML rebuild v1 public contract catalog',
        'description': 'Shape only. Apply semantic/service invariants documented in contracts-v1.md.',
        '$defs': definitions,
        'anyOf': [{'$ref': f'#/$defs/{name}'} for name in ADAPTERS],
    }
    reference = lambda name: {'$ref': f'#/components/schemas/{name}'}

    def response(name, description):
        return {'description': description, 'content': {'application/json': {'schema': reference(name)}}}

    def request(name):
        return {'required': True, 'content': {'application/json': {'schema': reference(name)}}}

    def path_id(name):
        return {'name': name, 'in': 'path', 'required': True, 'schema': TypeAdapter(Identifier).json_schema()}

    idempotency = {'name': 'Idempotency-Key', 'in': 'header', 'required': True,
                   'description': 'Scope: authenticated principal + operation. Same key/body returns saved response; changed body -> 409.',
                   'schema': TypeAdapter(Identifier).json_schema()}

    def operation(operation_id, success_status, success_schema, description, *, body=None, params=(), mutate=False):
        errors = {'400': 'MISSING_INPUT or INVALID_INPUT_FORMAT; no source lookup or generation.',
                  '401': 'UNAUTHORIZED', '403': 'FORBIDDEN', '404': 'NOT_FOUND',
                  '409': 'VERSION_CONFLICT, IDEMPOTENCY_CONFLICT or INVALID_STATE', '503': 'NOT_READY'}
        result = {'operationId': operation_id, 'description': description,
                  'responses': {success_status: response(success_schema, 'Contract response; runtime implementation pending.'),
                                **{status: response('ErrorResult', message) for status, message in errors.items()},
                                '500': response('ErrorResult', 'INTERNAL_ERROR; safe message and correlation ID only.')}}
        parameters = [*params, *([idempotency] if mutate else [])]
        if parameters:
            result['parameters'] = parameters
        if body:
            result['requestBody'] = request(body)
        return result

    paths = {
        '/v1/jobs': {'post': operation('createJob', '202', 'ActiveJob',
            'Persist job and deadline before acceptance; return Location header. Unknown incident may become failed/NOT_FOUND asynchronously. Admission requires readiness.',
            body='CreateJobRequest', mutate=True)},
        '/v1/jobs/{job_id}': {'get': operation('getJob', '200', 'Job', 'Return persisted state for authorized user.', params=[path_id('job_id')])},
        '/v1/jobs/{job_id}/cancel': {'post': operation('cancelJob', '200', 'CancelledJob',
            'Atomically persist cancelled before acknowledgement. Completed jobs -> 409. Late worker cannot publish. Same idempotency key replays the prior response.',
            params=[path_id('job_id')], mutate=True)},
        '/v1/reports/{report_id}/decisions': {'post': operation('createDecision', '201', 'DecisionRecord',
            'Body report_id must match path and stored report version/hash. Compare expected_revision atomically. Derive actor/time/audit IDs on server; approval does not close a bank case.',
            body='DecisionRequest', params=[path_id('report_id')], mutate=True)},
        '/v1/decisions/{decision_id}': {'get': operation('getDecision', '200', 'DecisionRecord', 'Read authorized decision record.', params=[path_id('decision_id')])},
        '/v1/deliveries': {'post': operation('createDelivery', '202', 'PendingDelivery',
            'Persist delivery intent. manual_queue may target a refused job with null report/decision. export requires approved decision bound to the same report/job. Status delivered requires a verified matching receipt.',
            body='DeliveryRequest', mutate=True)},
        '/v1/deliveries/{delivery_id}': {'get': operation('getDelivery', '200', 'Delivery',
            'Return state, attempt count and receipt when delivered; local_simulator does not prove external integration.', params=[path_id('delivery_id')])},
    }
    paths['/v1/jobs']['post']['responses']['202']['headers'] = {
        'Location': {'description': 'Relative URL /v1/jobs/{job_id}', 'schema': {'type': 'string'}}}
    api = {
        'openapi': '3.1.0', 'jsonSchemaDialect': SCHEMA_DIALECT,
        'info': {'title': 'AML rebuild API contract', 'version': '1.0.0',
                 'description': 'Design artifact only. No HTTP handlers, persistence, authentication or Swagger server implemented in block 2.'},
        'servers': [{'url': 'http://127.0.0.1:8000', 'description': 'Planned loopback training server; not started.'}],
        'security': [{'localBearer': []}], 'paths': paths,
        'components': {'schemas': replace_refs(definitions), 'securitySchemes': {
            'localBearer': {'type': 'http', 'scheme': 'bearer', 'description': 'Planned local authentication; credentials and roles must be verified by the service.'}}},
    }
    return {'schema.json': bundle, 'openapi.json': api}


def export_artifacts(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name, payload in build_artifacts().items():
        (directory / name).write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + '\n', encoding='utf-8')
