"""Build a fresh synthetic investigation pack using existing executable controls."""
import argparse
from datetime import datetime, timezone
from pathlib import Path

from qa.execution.provenance import compare_snapshots, environment, snapshot
from qa.execution.runner import pin_file, verify_files, write_new
from qa.investigations.core import load_catalog
from qa.investigations.storage import FILES, pack_path, require
from qa.requirements.catalog import ROOT
from src.qa_profile import checked_path


def build(run_id, root=ROOT):
    from qa.investigations.observations import generate, project, qualify
    root = Path(root).absolute()
    directory = pack_path(run_id, root)
    workspace = checked_path(root / 'var/workflow/investigations' / run_id / 'http-ack-replay', root)
    require(not directory.exists() and not workspace.parent.exists(), 'LEARNING_OUTPUT_EXISTS')
    catalog = load_catalog(root)
    before, env = snapshot(root), environment()
    directory.mkdir(parents=True)
    workspace.mkdir(parents=True)
    write_new(directory, 'started.json', {'status': 'INCOMPLETE', 'run_id': run_id}, root)
    save = lambda name, value: write_new(directory, name, value, root)
    save('source-before.json', before)
    save('environment-before.json', env)
    save('catalog.json', catalog)
    try:
        observations = generate(root, directory, workspace)
        save('observations.json', observations)
        save('bundle.json', project(observations))
        qualification = qualify(observations)
        save('qualification.json', qualification)
        after, env_after = snapshot(root), environment()
        save('source-after.json', after)
        save('environment-after.json', env_after)
        require(qualification['status'] == 'PASS', 'LEARNING_CONTROL_FAILURE')
        require(compare_snapshots(before, after)['status'] == 'PASS' and env == env_after,
                'LEARNING_VERSION_CHANGED')
        result = {'schema_version': '1.0', 'run_id': run_id, 'status': 'PASS',
                  'lesson_count': 6, 'new_model_calls': 0, 'source_digest': before['digest'],
                  'finished_at_utc': datetime.now(timezone.utc).isoformat(),
                  'operator_practice': 'NOT_MEASURED', 'product_acceptance': 'INCOMPLETE'}
        save('result.json', result)
        pins = [pin_file(directory / name, root) for name in FILES]
        pins.append(pin_file(workspace / 'workflow.sqlite3', root))
        require(verify_files(pins, root)['status'] == 'PASS', 'LEARNING_BUILD_INTEGRITY')
        save('evidence.json', {'files': pins})
    except Exception:
        # Retain the incomplete pack; never overwrite it or silently retry in place.
        save('error.json', {'status': 'ERROR', 'error_code': 'LEARNING_BUILD_ERROR',
                            'product_acceptance': 'INCOMPLETE'})
        raise
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-id', required=True)
    args = parser.parse_args()
    try:
        import json
        print(json.dumps(build(args.run_id), ensure_ascii=False))
        return 0
    except Exception:
        print('{"status":"ERROR","error_code":"LEARNING_BUILD_ERROR","product_acceptance":"INCOMPLETE"}')
        return 2
