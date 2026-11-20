"""Reviewed worker; all model output remains data and no candidate code runs."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time

from qa.autonomy.candidates import CASE_IDS, prepare_cases
from qa.autonomy.cycle import run_cycle
from qa.autonomy.demo import run_demo
from qa.autonomy.providers import OllamaProposer
from qa.execution.provenance import snapshot
from qa.execution.runner import write_new
from qa.reporting import strict_json
from qa.requirements.catalog import ROOT
from qa.stability.budget import validate_limits
from qa.tools.run_llm import network_guard
from src.qa_profile import checked_path

EXIT = {'PASS': 0, 'FAIL': 1, 'ERROR': 2, 'INCOMPLETE': 3}


def validate_config(value):
    if (type(value) is not dict or set(value) != {'schema_version', 'mode', 'case_ids', 'limits', 'source_digest'}
            or value['schema_version'] != '1.0' or value['mode'] not in ('demo', 'ollama')
            or type(value['case_ids']) is not list or not 1 <= len(value['case_ids']) <= 5
            or any(type(x) is not str or x not in CASE_IDS for x in value['case_ids'])
            or len(set(value['case_ids'])) != len(value['case_ids'])
            or type(value['source_digest']) is not str or len(value['source_digest']) != 64
            or any(c not in '0123456789abcdef' for c in value['source_digest'])):
        raise ValueError('AUTONOMY_WORKER_CONFIG')
    limits = validate_limits(value['limits'])
    if limits['max_attempts'] > 15:
        raise ValueError('AUTONOMY_WORKER_CONFIG')
    if value['mode'] == 'demo' and (value['case_ids'] != list(CASE_IDS)
            or limits['max_attempts'] != 6 or limits['max_tokens'] != 98304):
        raise ValueError('AUTONOMY_DEMO_FIXED_CONTROLS')
    return value


def run(directory, root=ROOT):
    root = Path(root).absolute()
    directory = checked_path(Path(directory).absolute(), root)
    if directory.parent != root / 'var/qa/autonomy' or not directory.is_dir():
        raise ValueError('AUTONOMY_WORKER_SCOPE')
    config_path = checked_path(directory / 'worker-config.json', root)
    if config_path.stat().st_size > 8192:
        raise ValueError('AUTONOMY_WORKER_CONFIG')
    config = validate_config(strict_json(config_path.read_text('utf-8')))
    result = {'status': 'ERROR', 'mode': config['mode'], 'product_acceptance': 'INCOMPLETE',
              'candidate_admission': 'NOT_EVALUATED', 'new_model_calls': 0}
    proposer = None
    proposal_calls = 0
    started = time.monotonic()

    def save(name, value):
        write_new(directory, name, value, root)

    def guard():
        if snapshot(root)['digest'] != config['source_digest']:
            raise ValueError('AUTONOMY_SOURCE_CHANGED')

    journal = checked_path(directory / 'journal.jsonl', root)
    with journal.open('x', encoding='utf-8', newline='\n') as stream:
        sequence = 0

        def emit(event):
            nonlocal sequence
            sequence += 1
            row = {'journal_sequence': sequence, 'recorded_at_utc': datetime.now(timezone.utc).isoformat(),
                   'data': event}
            stream.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + '\n')
            stream.flush()
            os.fsync(stream.fileno())

        try:
            guard()
            if config['mode'] == 'demo':
                demo = run_demo(root=root, emit=emit, guard=guard)
                save('demo.json', demo)
                result.update(status=demo['status'], qualification_status=demo['status'],
                              control_count=demo['control_count'])
            else:
                cases = prepare_cases(config['case_ids'], root)
                baseline = cases.baseline()
                save('baseline.json', baseline)
                if baseline['status'] != 'PASS':
                    raise ValueError('AUTONOMY_BASELINE_REJECTED')
                proposer = OllamaProposer(cases)
                with network_guard():
                    before = proposer.identity()
                    save('model-before.json', before)
                    remaining = config['limits']['wall_seconds'] - (time.monotonic() - started)
                    if remaining <= 0:
                        result.update(status='INCOMPLETE', error_code='AUTONOMY_SETUP_DEADLINE')
                    else:
                        limits = {**config['limits'], 'wall_seconds': remaining}
                        plan = {'schema_version': '1.0', 'plan_id': 'local-ollama-repair-v1',
                                'limits': limits, 'cases': cases.plan_cases(2, proposer.reserve_tokens)}
                        save('cycle-plan.json', plan)

                        def propose(*args):
                            nonlocal proposal_calls
                            guard()
                            proposal_calls += 1
                            return proposer(*args)

                        def assess(*args):
                            guard()
                            return cases.assess(*args)

                        cycle = run_cycle(plan, propose, assess, emit=emit)
                        save('cycle.json', cycle)
                        save('candidates.json', cases.reports)
                        result.update(status=cycle['status'], new_model_calls=cycle['provider_calls'],
                                      qualification_status='NOT_RUN', termination_reason=cycle['termination_reason'])
                    after = proposer.identity()
                    save('model-after.json', after)
                    stable = all(before[key] == after[key] for key in ('model', 'server', 'show'))
                    result['model_identity_stability'] = 'PASS' if stable else 'ERROR'
                    if not stable:
                        result.update(status='ERROR', error_code='AUTONOMY_MODEL_CHANGED')
            guard()
        except KeyboardInterrupt:
            result.update(status='INCOMPLETE', error_code='INTERRUPTED')
        except Exception as exc:
            result.update(status='ERROR', error_code=str(exc) if type(exc) is ValueError and
                          str(exc).startswith('AUTONOMY_') else 'AUTONOMY_WORKER_ERROR')
        finally:
            if proposer is not None:
                save('http-attempts.json', proposer.attempts)
                result['new_model_calls'] = proposal_calls
            result['elapsed_seconds'] = time.monotonic() - started
            save('worker-result.json', result)
    return EXIT[result['status']]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', required=True)
    return run(parser.parse_args().run_dir)
