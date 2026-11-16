"""One controller per database, released by the OS even after a process crash."""
import os
from pathlib import Path

from src.config import PROJECT_ROOT, resolve_project_path


class ControllerLease:
    def __init__(self, database: Path):
        path = Path(resolve_project_path(database, root=PROJECT_ROOT, key='workflow database'))
        path.relative_to(PROJECT_ROOT / 'var/workflow')
        path = path.with_name(path.name + '.controller.lock')
        if path.is_symlink() or (path.exists() and path.stat().st_nlink != 1):
            raise ValueError('INVALID_CONTROLLER_PATH')
        path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = path.open('a+b')
        try:
            if path.stat().st_size == 0:
                self.stream.write(b'0')
                self.stream.flush()
            self.stream.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except Exception:
            self.stream.close()
            raise ValueError('CONTROLLER_ALREADY_ACTIVE') from None

    def close(self):
        if not self.stream.closed:
            self.stream.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
            self.stream.close()
