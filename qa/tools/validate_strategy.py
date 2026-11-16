"""Validate QA-01 references against source, fresh JUnit and the saved baseline.

Read-only checks; does not import application modules, start services or run tests.
Writes one new report inside the specified existing evidence directory.
"""
import argparse
import ast
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence-dir', required=True)
    args = parser.parse_args()
    evidence = (ROOT / args.evidence_dir).resolve()
    if not evidence.is_relative_to(ROOT / 'verification') or not evidence.is_dir():
        raise ValueError('Use an existing verification directory')
    report_path = evidence / 'strategy-validation.json'
    if report_path.exists():
        raise ValueError('Previous validation is not overwritten')
    errors = []

    def check(condition, label):
        if not condition:
            errors.append(label)

    audit = read_json(ROOT / 'qa/strategy/audit-traceability.json')
    operator = read_json(ROOT / 'qa/strategy/operator-matrix.json')
    policy = read_json(ROOT / 'qa/strategy/quality-policy.json')
    baseline = read_json(evidence / 'baseline.json')
    manifest = read_json(evidence / 'file-manifest.json')
    regression = read_json(evidence / 'coverage/regression/unit-verification.json')
    junit = ET.parse(evidence / 'coverage/regression/unit-tests.xml')
    families = {}
    for case in junit.iter('testcase'):
        name = re.split(r'[\[(]', case.attrib['name'])[0].strip()
        key = (case.attrib['classname'], name)
        families.setdefault(key, []).append(case)

    selectors = set()
    source_trees = {}

    def validate_selector(selector):
        selectors.add(selector)
        parts = selector.split('::')
        path = (ROOT / parts[0]).resolve()
        if not path.is_relative_to(ROOT) or not path.is_file():
            check(False, 'Missing test: ' + selector)
            return
        if path not in source_trees:
            source_trees[path] = ast.parse(path.read_text(encoding='utf-8-sig'))
        node = source_trees[path]
        for name in parts[1:]:
            found = [item for item in getattr(node, 'body', [])
                     if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                     and item.name == name]
            if len(found) != 1:
                check(False, 'Missing definition: ' + selector)
                return
            node = found[0]
        classname = '.'.join(Path(parts[0]).with_suffix('').parts) + ''.join('.' + p for p in parts[1:-1])
        cases = families.get((classname, parts[-1]), [])
        check(bool(cases), 'Absent from fresh JUnit: ' + selector)
        check(all(not any(c.tag in ('failure', 'error', 'skipped') for c in case) for case in cases),
              'Nonpassing JUnit member: ' + selector)

    def walk(value):
        if isinstance(value, dict):
            if 'node_id' in value:
                validate_selector(value['node_id'])
                if 'source_sha256' in value:
                    check(digest(ROOT / value['path']) == value['source_sha256'],
                          'Reference source hash: ' + value['node_id'])
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(audit)
    check(sorted(row['id'] for row in audit['fixes']) == [f'FIX-{n:02}' for n in range(1, 23)], '22 unique audit FIX required')
    check(sorted(row['id'] for row in operator['rows']) == [f'QA-{n:03}' for n in range(1, 25)], '24 unique QA scenarios required')
    for row in audit['fixes']:
        for field in ('risk', 'expected_behavior_from_audit', 'gap', 'owner_role', 'next_step'):
            check(bool(row.get(field)), row['id'] + ': ' + field)
        for ref in row['evidence_refs']:
            check(ref in audit['evidence_catalog'], row['id'] + ': unknown evidence ' + ref)
    evidence_paths = set()
    for item in list(audit['evidence_catalog'].values()) + audit['sources']:
        path = ROOT / item['path']
        evidence_paths.add(str(path))
        check(path.is_file(), 'Missing evidence: ' + str(path))
        if path.is_file() and 'sha256' in item:
            check(digest(path) == item['sha256'], 'Evidence hash: ' + str(path))
    for row in operator['rows']:
        for field in ('expected', 'gap', 'owner_role', 'next_step', 'dependencies'):
            check(bool(row.get(field)), row['id'] + ': ' + field)
        for selector in row['test_refs']:
            validate_selector(selector)
        for ref in row['evidence_paths']:
            evidence_paths.add(str(ROOT / ref))
            check((ROOT / ref).is_file(), 'Missing evidence: ' + ref)
    unchanged = 0
    for entry in manifest['files']:
        path = ROOT / entry['path']
        matched = path.is_file() and digest(path) == entry['sha256']
        check(matched, 'Changed baseline file: ' + entry['path'])
        unchanged += int(matched)
    check(digest(evidence / 'file-manifest.json') == baseline['source_snapshot']['manifest_file_sha256'], 'Baseline manifest hash')
    check(regression['status'] == 'PASS' and regression['pytest_exit_code'] == 0, 'Fresh regression failed')
    check(all(regression[name] == 0 for name in ('failures', 'errors', 'skipped')), 'Regression failures/errors/skips')
    check(not policy['scopes']['judge']['may_supply_M08'], 'Judge must not supply M08')
    check(not audit['fixes_closed'], 'No formal audit acceptance supplied')
    report = {
        'checked_at_utc': datetime.now(timezone.utc).isoformat(),
        'status': 'PASS' if not errors else 'FAIL',
        'scope': 'Static references, fresh JUnit membership, evidence hashes and baseline preservation; no tests or services started.',
        'audit_fixes': len(audit['fixes']), 'operator_scenarios': len(operator['rows']),
        'unique_test_selectors': len(selectors), 'unique_evidence_paths': len(evidence_paths),
        'baseline_files_checked': len(manifest['files']), 'baseline_files_unchanged': unchanged,
        'errors': errors,
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))
    return 0 if not errors else 1


if __name__ == '__main__':
    raise SystemExit(main())
