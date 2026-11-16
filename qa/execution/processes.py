"""Bounded child execution with an owned process tree; no global PID killing.

On Windows the child waits for a private stdin barrier before target code can
run, allowing assignment to a kill-on-close Job Object first. This is process
lifecycle ownership, not a security sandbox for untrusted test code.
"""
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from src.qa_profile import checked_path


def child_environment(parent, temporary):
    """Allow only OS discovery variables, and use owned temporary/cache paths."""
    allowed = {'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATH', 'PATHEXT', 'SYSTEMDRIVE',
               'USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'PROGRAMDATA',
               'PROGRAMFILES', 'PROGRAMFILES(X86)', 'COMMONPROGRAMFILES',
               'COMMONPROGRAMFILES(X86)', 'PROCESSOR_ARCHITECTURE',
               'PROCESSOR_IDENTIFIER', 'NUMBER_OF_PROCESSORS', 'HOME', 'LANG', 'LC_ALL',
               'USERNAME', 'USER', 'LOGNAME', 'LNAME'}
    result = {key: value for key, value in parent.items() if key.upper() in allowed}
    temporary = Path(temporary).absolute()
    result.update({
        'TEMP': str(temporary), 'TMP': str(temporary), 'TMPDIR': str(temporary),
        'PYTHON_DOTENV_DISABLED': '1', 'PYTHONDONTWRITEBYTECODE': '1',
        'PYTHONNOUSERSITE': '1', 'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8',
        'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1',
        'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1',
        'HF_HUB_DISABLE_TELEMETRY': '1', 'ANONYMIZED_TELEMETRY': 'FALSE',
        'OTEL_SDK_DISABLED': 'true', 'LANGSMITH_TRACING': 'false',
        'LANGCHAIN_TRACING_V2': 'false', 'DO_NOT_TRACK': '1',
        'HF_HOME': str(temporary / 'cache/hf'),
        'HF_HUB_CACHE': str(temporary / 'cache/hf/hub'),
        'SENTENCE_TRANSFORMERS_HOME': str(temporary / 'cache/hf/hub'),
        'TORCH_HOME': str(temporary / 'cache/torch'),
        'XDG_CACHE_HOME': str(temporary / 'cache/xdg'),
        'MPLCONFIGDIR': str(temporary / 'cache/matplotlib'),
    })
    return result


class _WindowsJob:
    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self.ctypes = ctypes
        self.types = wintypes
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel = self.kernel
        kernel.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel.QueryInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                     wintypes.DWORD, ctypes.c_void_p)
        kernel.QueryInformationJobObject.restype = wintypes.BOOL
        kernel.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
        kernel.TerminateJobObject.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel.CloseHandle.restype = wintypes.BOOL

        class Limits(ctypes.Structure):
            _fields_ = [('process_time', ctypes.c_longlong), ('job_time', ctypes.c_longlong),
                        ('flags', wintypes.DWORD), ('minimum_working_set', ctypes.c_size_t),
                        ('maximum_working_set', ctypes.c_size_t), ('active_process_limit', wintypes.DWORD),
                        ('affinity', ctypes.c_size_t), ('priority', wintypes.DWORD),
                        ('scheduling_class', wintypes.DWORD)]

        class Io(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in
                        ('read_operations', 'write_operations', 'other_operations',
                         'read_bytes', 'write_bytes', 'other_bytes')]

        class Extended(ctypes.Structure):
            _fields_ = [('basic', Limits), ('io', Io), ('process_memory', ctypes.c_size_t),
                        ('job_memory', ctypes.c_size_t), ('peak_process_memory', ctypes.c_size_t),
                        ('peak_job_memory', ctypes.c_size_t)]

        class Accounting(ctypes.Structure):
            _fields_ = [('user_time', ctypes.c_longlong), ('kernel_time', ctypes.c_longlong),
                        ('period_user_time', ctypes.c_longlong), ('period_kernel_time', ctypes.c_longlong),
                        ('page_faults', wintypes.DWORD), ('total_processes', wintypes.DWORD),
                        ('active_processes', wintypes.DWORD), ('terminated_processes', wintypes.DWORD)]

        self.Accounting = Accounting
        self.handle = kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise OSError('EXECUTION_JOB_CREATE_FAILED')
        limits = Extended()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            self.close()
            raise OSError('EXECUTION_JOB_CONFIGURATION_FAILED')

    def assign(self, process):
        # Popen owns this exact process handle; no PID lookup or reuse window.
        if not self.kernel.AssignProcessToJobObject(self.handle, self.types.HANDLE(int(process._handle))):
            raise OSError('EXECUTION_JOB_ASSIGNMENT_FAILED')

    def active(self):
        value = self.Accounting()
        if not self.kernel.QueryInformationJobObject(self.handle, 1, self.ctypes.byref(value),
                                                     self.ctypes.sizeof(value), None):
            raise OSError('EXECUTION_JOB_QUERY_FAILED')
        return value.active_processes

    def terminate_and_wait(self, timeout=10):
        if not self.active():
            return True
        if not self.kernel.TerminateJobObject(self.handle, 1):
            return False
        deadline = time.monotonic() + timeout
        while self.active():
            if time.monotonic() >= deadline:
                return False
            time.sleep(.02)
        return True

    def close(self):
        handle, self.handle = self.handle, None
        return not handle or bool(self.kernel.CloseHandle(handle))


def _cleanup(process, job, assigned):
    """Return a bounded ownership check; no unrelated process is inspected."""
    success = True
    try:
        if job is not None and assigned:
            success = job.terminate_and_wait()
        elif process is not None and os.name != 'nt':
            # start_new_session created this group before the bootstrap ran.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        elif process is not None and process.poll() is None:
            # Assignment failed while bootstrap was still blocked on stdin:
            # target code has never run and cannot have created descendants.
            process.kill()
    except OSError:
        success = False
    finally:
        if job is not None:
            try:
                success = job.close() and success
            except OSError:
                success = False
        if process is not None:
            try:
                process.wait(timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                success = False
            if process.stdin is not None and not process.stdin.closed:
                try:
                    process.stdin.close()
                except OSError:
                    success = False
    return 'PASS' if success else 'ERROR'


def run_owned(argv, cwd, environment, log_path, timeout_seconds):
    """Execute one Python script after lifecycle ownership has been established."""
    if (not isinstance(argv, list) or len(argv) < 4 or not all(isinstance(arg, str) for arg in argv)
            or argv[1:3] != ['-I', '-B'] or Path(argv[0]).resolve() != Path(sys.executable).resolve()):
        raise ValueError('EXECUTION_PYTHON_COMMAND_REQUIRED')
    if (type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= 86400):
        raise ValueError('EXECUTION_TIMEOUT_INVALID')
    cwd = checked_path(Path(cwd).absolute(), Path(cwd).absolute())
    target = checked_path(cwd / argv[3], cwd)
    if not target.is_file() or target.suffix != '.py':
        raise ValueError('EXECUTION_SCRIPT_REQUIRED')
    log_path = checked_path(Path(log_path).absolute(), cwd)
    bootstrap = Path(__file__).with_name('child.py').absolute()
    command = [sys.executable, '-I', '-B', str(bootstrap), str(target), *argv[4:]]
    log_path.parent.mkdir(parents=True, exist_ok=True)
    process = job = None
    assigned = False
    result = {'returncode': None, 'timed_out': False, 'interrupted': False, 'cleanup_status': 'ERROR'}
    started = time.monotonic()
    # Exclusive creation occurs before launching any process.
    with log_path.open('xb') as log:
        try:
            if os.name == 'nt':
                job = _WindowsJob()
            process = subprocess.Popen(command, cwd=cwd, env=dict(environment), stdin=subprocess.PIPE,
                stdout=log, stderr=subprocess.STDOUT, shell=False, close_fds=True,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
                start_new_session=os.name != 'nt')
            if job is not None:
                job.assign(process)
                assigned = True
            process.stdin.write(b'RUN\n')
            process.stdin.flush()
            process.stdin.close()
            result['returncode'] = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            result['timed_out'] = True
        except KeyboardInterrupt:
            result['interrupted'] = True
        except (OSError, ValueError) as exc:
            result['error_type'] = type(exc).__name__
        finally:
            result['cleanup_status'] = _cleanup(process, job, assigned)
            result['elapsed_seconds'] = time.monotonic() - started
    return result
