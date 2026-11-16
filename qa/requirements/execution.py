"""Project first-run pytest lifecycle observations onto mapped requirements.

The runner owns these observations. No external evidence/JUnit import, candidate
admission, human acceptance or independent attestation is implemented here.
"""
from __future__ import annotations

from qa.requirements.catalog import canonical_digest, selectors


def _node_identity(nodeid):
    # Only the file portion is a filesystem path. Parameters can deliberately
    # contain backslashes/slashes; normalizing them would merge distinct cases.
    filename, separator, test_identity = nodeid.partition('::')
    return filename.replace('\\', '/') + separator + test_identity


class Recorder:
    def __init__(self):
        self.collected = []
        self.reports = []
        self.collection_complete = False
        self.collection_failed = False

    def pytest_deselected(self, items):
        if items:
            self.collection_failed = True

    def pytest_collectreport(self, report):
        if report.failed or report.skipped:
            self.collection_failed = True

    def pytest_collection_finish(self, session):
        self.collected = [_node_identity(item.nodeid) for item in session.items]
        self.collection_complete = not self.collection_failed

    def pytest_runtest_logreport(self, report):
        self.reports.append({'nodeid': _node_identity(report.nodeid), 'when': report.when,
                             'outcome': report.outcome, 'wasxfail': hasattr(report, 'wasxfail')})

    def result(self, exit_code, source_unchanged):
        return {'exit_code': int(exit_code), 'collection_complete': self.collection_complete,
                'collected': self.collected, 'reports': self.reports, 'source_unchanged': source_unchanged}


def _matches(node, selector):
    return node == selector or node.startswith(selector + '[')


def _rollup(values):
    if 'FAIL' in values:
        return 'FAIL'
    return 'PASS' if values and all(value == 'PASS' for value in values) else 'INCOMPLETE'


def _valid_run(run):
    if type(run) is not dict or set(run) != {'exit_code', 'collection_complete', 'collected', 'reports', 'source_unchanged'}:
        return False
    if (type(run['exit_code']) is not int or run['exit_code'] not in range(6)
            or type(run['collection_complete']) is not bool or type(run['source_unchanged']) is not bool
            or type(run['collected']) is not list or not all(type(n) is str for n in run['collected'])
            or len(set(run['collected'])) != len(run['collected']) or type(run['reports']) is not list):
        return False
    return all(type(row) is dict and set(row) == {'nodeid', 'when', 'outcome', 'wasxfail'}
               and type(row['nodeid']) is str and row['when'] in ('setup', 'call', 'teardown')
               and row['outcome'] in ('passed', 'failed', 'skipped') and type(row['wasxfail']) is bool
               for row in run['reports'])


def _node_status(rows):
    if any(row['outcome'] == 'failed' for row in rows):
        return 'FAIL'
    if any(row['outcome'] == 'skipped' or row['wasxfail'] for row in rows):
        return 'INCOMPLETE'
    # Multiple calls may be pytest subtests; all observations above are retained.
    if (sum(row['when'] == 'setup' for row in rows) != 1
            or sum(row['when'] == 'teardown' for row in rows) != 1
            or not any(row['when'] == 'call' for row in rows)):
        return 'INCOMPLETE'
    return 'PASS'


def build_report(catalog, validation, run=None):
    identity_ok = (validation.get('status') == 'PASS'
                   and validation.get('catalog_sha256') == canonical_digest(catalog))
    selected = selectors(catalog)
    valid = run is not None and _valid_run(run)
    sound = valid and identity_ok and run['source_unchanged']
    execution_status = 'NOT_RUN' if run is None else 'ERROR'
    checks = {selector: {'selector': selector, 'status': 'INCOMPLETE', 'collected_cases': 0,
                         'reason': 'NOT_RUN' if run is None else 'UNVERIFIED_EXECUTION'} for selector in selected}
    if sound:
        known = set(run['collected'])
        unexpected = (any(not any(_matches(node, selector) for selector in selected) for node in known)
                      or any(row['nodeid'] not in known for row in run['reports']))
        for selector in selected:
            nodes = [node for node in run['collected'] if _matches(node, selector)]
            status = _rollup([_node_status([row for row in run['reports'] if row['nodeid'] == node]) for node in nodes])
            reason = 'OBSERVED_TEST_RESULTS'
            if (not run['collection_complete'] or unexpected or run['exit_code'] not in (0, 1)) and status != 'FAIL':
                status, reason = 'INCOMPLETE', 'EXECUTION_NOT_COMPLETE'
            checks[selector] = {'selector': selector, 'status': status, 'collected_cases': len(nodes), 'reason': reason}
        execution_status = _rollup([value['status'] for value in checks.values()])
        if unexpected or run['exit_code'] not in (0, 1):
            execution_status = 'ERROR'
        elif run['exit_code'] == 1:
            execution_status = 'FAIL'
            # A nonzero runner cannot qualify a collection claiming only passes.
            if not any(value['status'] == 'FAIL' for value in checks.values()):
                for value in checks.values():
                    value.update(status='INCOMPLETE', reason='EXIT_CODE_DISAGREES')
        elif execution_status == 'FAIL':
            execution_status = 'ERROR'  # Contradictory exit 0 must not erase the failure.
    rows = []
    for requirement in catalog['requirements']:
        cases = []
        for scenario in requirement['scenarios']:
            observed = [dict(checks[selector]) for selector in scenario['test_selectors']]
            cases.append({'id': scenario['id'], 'kind': scenario['kind'],
                          'expected_outcome': scenario['expected_outcome'],
                          'status': _rollup([item['status'] for item in observed]), 'checks': observed})
        rows.append({'id': requirement['id'], 'version': requirement['version'], 'title': requirement['title'],
                     'priority': requirement['priority'], 'scope': requirement['scope'],
                     'limitations': requirement['limitations'],
                     'status': _rollup([case['status'] for case in cases]), 'scenarios': cases})
    return {'schema_version': '1.0', 'catalog_id': catalog['catalog_id'], 'catalog_version': catalog['version'],
            'catalog_sha256': validation.get('catalog_sha256'),
            'catalog_validation': 'PASS' if identity_ok else 'ERROR',
            'execution_status': execution_status, 'requirements_status': _rollup([r['status'] for r in rows]),
            'requirements': rows, 'requirement_count': len(rows), 'selector_count': len(selected),
            'confirmed_requirements': sum(r['status'] == 'PASS' for r in rows),
            'unconfirmed_requirements': [r['id'] for r in rows if r['status'] != 'PASS'],
            'product_acceptance': 'INCOMPLETE', 'candidate_admission': 'NOT_EVALUATED',
            'human_calibration': 'NOT_MEASURED', 'historical_judge': 'NOT_REASSESSED',
            'evidence_scope': 'Current owned pytest invocation and mapped mechanical checks only. '
                'No full runtime/model/environment attestation, independent holdout or release decision.'}
