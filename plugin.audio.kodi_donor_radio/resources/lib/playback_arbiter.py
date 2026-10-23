# SPDX-License-Identifier: GPL-2.0-or-later
"""Serialize this add-on's Kodi Player.play starts across addon invocations."""
from contextlib import contextmanager
import os
from pathlib import Path
import threading


_registry_lock = threading.Lock()
_path_locks = {}


def _process_lock(path):
    key = str(Path(path).resolve()) if path is not None else '<process-local>'
    with _registry_lock:
        return _path_locks.setdefault(key, threading.RLock())


@contextmanager
def _file_lock(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.chmod(str(path), 0o600)
        if os.name == 'nt':
            import msvcrt
            if os.fstat(fd).st_size == 0:
                os.write(fd, b'\0')
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == 'nt':
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


class PlaybackStartArbiter:
    """Hold one shared lock across Kodi state checks and addon Player.play calls.

    A profile lock file coordinates Kodi's separate Python addon invocations;
    the process-local lock also serializes threads that share one interpreter.
    Kodi UI actions and other addons do not participate in this lock.
    """

    def __init__(self, lock_path=None):
        self.lock_path = Path(lock_path) if lock_path is not None else None
        self._thread_lock = _process_lock(self.lock_path)

    @contextmanager
    def _locked(self):
        with self._thread_lock:
            if self.lock_path is None:
                yield
            else:
                with _file_lock(self.lock_path):
                    yield

    def start(self, callback):
        """Run a deliberate add-on playback start under the shared lock."""
        with self._locked():
            return callback()

    def start_if_available(self, can_start, callback):
        """Check Kodi state and start only while the shared lock is held."""
        with self._locked():
            if not can_start():
                return False
            callback()
            return True
