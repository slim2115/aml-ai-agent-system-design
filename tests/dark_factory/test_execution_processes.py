"""Real process ownership and environment controls, without service startup."""
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from qa.execution import processes


def execute(tmp_path, source, *, timeout=10, name='owned.py', env=None):
    target = tmp_path / name
    target.write_text(source, encoding='utf-8')
    temporary = tmp_path / 'temporary'
    temporary.mkdir(exist_ok=True)
    return processes.run_owned([sys.executable, '-I', '-B', str(target)], tmp_path,
        env if env is not None else processes.child_environment(os.environ, temporary),
        tmp_path / (name + '.log'), timeout)


def test_environment_scrubs_configuration_credentials_and_test_filters(tmp_path):
    parent = {'SystemRoot': 'C:\\Windows', 'PATH': 'tool-path', 'PYTHONPATH': 'injected',
              'PYTHONHOME': 'injected', 'PYTEST_ADDOPTS': '-k nothing', 'PYTEST_PLUGINS': 'evil',
              'AML_QA_RUN_ID': 'other-run', 'DATABASE_URL': 'postgres://secret',
              'HTTP_PROXY': 'secret-proxy', 'OPENAI_API_KEY': 'secret', 'HF_TOKEN': 'secret',
              'LLM_MODEL_NAME': 'wrong', 'TEMP': 'someone-else'}
    original = dict(parent)
    env = processes.child_environment(parent, tmp_path)
    assert parent == original
    assert env['SystemRoot'] == parent['SystemRoot']
    assert env['PATH'] == 'tool-path'
    assert env['TEMP'] == env['TMP'] == str(tmp_path)
    assert env['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] == env['PYTHON_DOTENV_DISABLED'] == '1'
    assert env['HF_HUB_OFFLINE'] == env['TRANSFORMERS_OFFLINE'] == '1'
    for key in parent.keys() - {'SystemRoot', 'PATH', 'TEMP'}:
        assert key not in env
    assert not any('secret' in value for value in env.values())


def test_real_success_preserves_exit_and_output(tmp_path):
    result = execute(tmp_path, 'print("owned execution")\n')
    assert result['returncode'] == 0
    assert not result['timed_out'] and not result['interrupted']
    assert result['cleanup_status'] == 'PASS'
    assert result['elapsed_seconds'] > 0
    assert (tmp_path / 'owned.py.log').read_text('utf-8').strip() == 'owned execution'


def test_owned_environment_preserves_local_account_lookup(tmp_path):
    parent = {key: value for key, value in os.environ.items()
              if key.upper() not in {'USERNAME', 'USER', 'LOGNAME', 'LNAME'}}
    parent['USERNAME'] = 'qa-local-account'
    env = processes.child_environment(parent, tmp_path / 'temporary')
    result = execute(tmp_path, 'import getpass\nprint(getpass.getuser())\n', env=env)
    assert result['returncode'] == 0 and result['cleanup_status'] == 'PASS'
    assert (tmp_path / 'owned.py.log').read_text('utf-8').strip() == 'qa-local-account'


def test_real_nonzero_exit_is_not_reclassified_as_success(tmp_path):
    result = execute(tmp_path, 'raise SystemExit(7)\n')
    assert result['returncode'] == 7
    assert result['cleanup_status'] == 'PASS'


def test_bootstrap_does_not_execute_target_without_start_barrier(tmp_path):
    target = tmp_path / 'target.py'
    marker = tmp_path / 'should-not-exist'
    target.write_text('from pathlib import Path\nPath(' + repr(str(marker)) + ').touch()\n', 'utf-8')
    child = Path(processes.__file__).with_name('child.py')
    result = subprocess.run([sys.executable, '-I', '-B', str(child), str(target)], input=b'',
                            capture_output=True, timeout=10)
    assert result.returncode != 0
    assert not marker.exists()


@pytest.mark.parametrize('timeout', [0, -1, True, float('nan'), float('inf'), 86401])
def test_invalid_timeout_cannot_start_process(tmp_path, monkeypatch, timeout):
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **k: pytest.fail('must not start'))
    with pytest.raises(ValueError, match='TIMEOUT_INVALID'):
        execute(tmp_path, 'pass', timeout=timeout)


@pytest.mark.parametrize('argv', [[], ['powershell', '-c', 'Write-Output x'],
                                 [sys.executable, '-c', 'pass'], [sys.executable, '-I', '-m', 'pytest']])
def test_only_explicit_isolated_python_script_form_is_allowed(tmp_path, argv):
    with pytest.raises(ValueError, match='PYTHON_COMMAND_REQUIRED'):
        processes.run_owned(argv, tmp_path, {}, tmp_path / 'log', 10)


def test_existing_log_is_preserved_without_process_start(tmp_path, monkeypatch):
    log = tmp_path / 'owned.py.log'
    log.write_bytes(b'earlier evidence')
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **k: pytest.fail('must not start'))
    with pytest.raises(FileExistsError):
        execute(tmp_path, 'pass')
    assert log.read_bytes() == b'earlier evidence'


def test_outside_target_is_rejected(tmp_path):
    with pytest.raises(ValueError, match='INVALID_QA_PATH'):
        processes.run_owned([sys.executable, '-I', '-B', str(tmp_path.parent / 'outside.py')],
                            tmp_path, {}, tmp_path / 'log', 10)


def test_real_timeout_kills_child_and_grandchild(tmp_path):
    marker = tmp_path / 'late-grandchild-output'
    grandchild = 'import time; from pathlib import Path; time.sleep(4); Path(' + repr(str(marker)) + ').touch()'
    source = ('import subprocess, sys, time\n'
              'subprocess.Popen([sys.executable, "-c", ' + repr(grandchild) + '])\n'
              'print("grandchild started", flush=True)\n'
              'time.sleep(30)\n')
    result = execute(tmp_path, source, timeout=1.5)
    assert result['timed_out'] and result['returncode'] is None
    assert result['cleanup_status'] == 'PASS'
    assert 'grandchild started' in (tmp_path / 'owned.py.log').read_text('utf-8')
    time.sleep(3)
    assert not marker.exists()


def test_normal_exit_also_cleans_orphan_grandchild(tmp_path):
    marker = tmp_path / 'late-orphan-output'
    grandchild = 'import time; from pathlib import Path; time.sleep(2); Path(' + repr(str(marker)) + ').touch()'
    result = execute(tmp_path, 'import subprocess, sys\nsubprocess.Popen([sys.executable, "-c", ' + repr(grandchild) + '])\n')
    assert result['returncode'] == 0 and result['cleanup_status'] == 'PASS'
    time.sleep(2.2)
    assert not marker.exists()


def test_multiprocessing_spawn_target_runs_under_bootstrap(tmp_path):
    source = ('import multiprocessing\n'
              'def child(queue):\n    queue.put("spawned")\n'
              'if __name__ == "__main__":\n'
              '    context = multiprocessing.get_context("spawn")\n'
              '    queue = context.Queue()\n'
              '    process = context.Process(target=child, args=(queue,))\n'
              '    process.start()\n'
              '    print(queue.get(timeout=5), flush=True)\n'
              '    process.join(5)\n'
              '    assert process.exitcode == 0\n')
    result = execute(tmp_path, source)
    assert result['returncode'] == 0 and result['cleanup_status'] == 'PASS'
    assert 'spawned' in (tmp_path / 'owned.py.log').read_text('utf-8')


@pytest.mark.skipif(os.name != 'nt', reason='Windows Job Object failure control')
def test_job_assignment_failure_never_runs_target(tmp_path, monkeypatch):
    class RejectedJob:
        def assign(self, process):
            raise OSError('assignment rejected')
        def close(self):
            return True
    monkeypatch.setattr(processes, '_WindowsJob', RejectedJob)
    marker = tmp_path / 'forbidden'
    result = execute(tmp_path, 'from pathlib import Path\nPath(' + repr(str(marker)) + ').touch()')
    assert result['returncode'] is None and result['error_type'] == 'OSError'
    assert result['cleanup_status'] == 'PASS'
    assert not marker.exists()


@pytest.mark.skipif(os.name != 'nt', reason='Windows Job Object startup control')
def test_job_creation_failure_cannot_launch_process(tmp_path, monkeypatch):
    def reject():
        raise OSError('job unavailable')
    monkeypatch.setattr(processes, '_WindowsJob', reject)
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **k: pytest.fail('must not start'))
    result = execute(tmp_path, 'pass')
    assert result['returncode'] is None and result['error_type'] == 'OSError'


def test_interruption_stops_owned_process_and_is_reported(tmp_path, monkeypatch):
    original_wait = subprocess.Popen.wait
    calls = []
    def interrupt_once(self, timeout=None):
        if not calls:
            calls.append(True)
            raise KeyboardInterrupt
        return original_wait(self, timeout=timeout)
    monkeypatch.setattr(subprocess.Popen, 'wait', interrupt_once)
    result = execute(tmp_path, 'import time\ntime.sleep(30)')
    assert result['interrupted'] and result['returncode'] is None
    assert result['cleanup_status'] == 'PASS'
