"""Scoped contract verification. No legacy app imports, LLM, DB or network."""
import hashlib
import importlib.metadata
import json
from pathlib import Path
import sys
import unittest
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[2]
if ROOT != Path('D:/work/aml-ai-agent-system-rebuild'):
    raise SystemExit('Unexpected rebuild location')
sys.path.insert(0, str(ROOT))

from jsonschema import Draft202012Validator
from src.contracts.export import build_artifacts, export_artifacts


def verify_references(document):
    def walk(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key == '$ref':
                    if not item.startswith('#/'):
                        raise ValueError('External schema references are not permitted')
                    target = document
                    for component in item[2:].split('/'):
                        target = target[component.replace('~1', '/').replace('~0', '~')]
                else:
                    walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
    walk(document)


def main():
    evidence = ROOT / 'verification/block-02'
    evidence.mkdir(parents=True, exist_ok=True)
    export_artifacts(ROOT / 'contracts/v1')
    artifacts = build_artifacts()
    Draft202012Validator.check_schema(artifacts['schema.json'])
    for artifact in artifacts.values():
        verify_references(artifact)
    for schema in artifacts['openapi.json']['components']['schemas'].values():
        Draft202012Validator.check_schema(schema)
    suite = unittest.TestLoader().discover(str(ROOT / 'tests/contracts'), pattern='test_*.py')
    with (evidence / 'test-output.txt').open('w', encoding='utf-8') as stream:
        result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    passed = result.wasSuccessful() and result.testsRun > 0 and not result.skipped
    report = {
        'checked_at_utc': datetime.now(timezone.utc).isoformat(), 'status': 'PASS' if passed else 'FAIL',
        'tests_run': result.testsRun, 'failures': len(result.failures), 'errors': len(result.errors), 'skipped': len(result.skipped),
        'schema_definitions': len(artifacts['schema.json']['$defs']),
        'api_operations': sum(len(item) for item in artifacts['openapi.json']['paths'].values()),
        'schema_metaschema_and_local_references': 'PASS',
        'openapi_validation_scope': 'Generated component schemas and local refs only; HTTP behavior not tested; not a complete OpenAPI conformance validator.',
        'python': sys.version.split()[0], 'executable': sys.executable,
        'packages': {name: importlib.metadata.version(name) for name in ('pydantic', 'jsonschema', 'rfc3339-validator')},
        'scope': 'Offline contracts only; no runtime wiring, source authenticity, AML prose accuracy, real authentication, durable jobs, database, LLM or SME acceptance.',
        'artifacts': {name: hashlib.sha256((ROOT / 'contracts/v1' / name).read_bytes()).hexdigest() for name in artifacts},
    }
    (evidence / 'verification.json').write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    print(json.dumps(report, indent=2, ensure_ascii=False))
    if not passed:
        print((evidence / 'test-output.txt').read_text(encoding='utf-8'))
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
