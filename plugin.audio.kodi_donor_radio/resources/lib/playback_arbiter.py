# SPDX-License-Identifier: GPL-2.0-or-later
"""Serialize this add-on's Kodi Player.play starts across addon invocations."""
from contextlib import contextmanager
import os
from pathlib import Path
import threading
import uuid


_registry_lock = threading.Lock()
_path_locks = {}
_path_intents = {}
_lock_depths = threading.local()


def _path_key(path):
    return str(Path(path).resolve()) if path is not None else '<process-local>'


def _process_lock(path):
    key = _path_key(path)
    with _registry_lock:
        return _path_locks.setdefault(key, threading.RLock())


@contextmanager
def profile_file_lock(path):
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
        self._key = _path_key(self.lock_path)
        self._thread_lock = _process_lock(self.lock_path)

    @contextmanager
    def _locked(self):
        with self._thread_lock:
            depths = getattr(_lock_depths, 'depths', None)
            if depths is None:
                depths = _lock_depths.depths = {}
            depth = depths.get(self._key, 0)
            depths[self._key] = depth + 1
            try:
                if self.lock_path is None or depth:
                    yield
                else:
                    with profile_file_lock(self.lock_path):
                        yield
            finally:
                if depth:
                    depths[self._key] = depth
                else:
                    del depths[self._key]

    def _record_intent(self):
        token = uuid.uuid4().hex
        if self.lock_path is None:
            with _registry_lock:
                _path_intents[self._key] = token
            return token
        intent_path = Path(str(self.lock_path) + '.intent')
        fd = os.open(str(intent_path), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        try:
            os.chmod(str(intent_path), 0o600)
            os.write(fd, token.encode('ascii'))
            os.fsync(fd)
        finally:
            os.close(fd)
        return token

    def _current_intent(self):
        if self.lock_path is None:
            with _registry_lock:
                return _path_intents.get(self._key)
        try:
            with open(str(self.lock_path) + '.intent', 'rb') as source:
                token = source.read(64)
        except OSError:
            return None
        try:
            value = token.decode('ascii')
        except UnicodeDecodeError:
            return None
        return value if len(value) == 32 else None

    def start(self, callback):
        """Record and run a deliberate add-on playback start under the lock."""
        with self._locked():
            token = self._record_intent()
            callback()
            return token

    def start_if_available(self, can_start, callback):
        """Check Kodi state and start only while the shared lock is held."""
        with self._locked():
            if not can_start():
                return None
            token = self._record_intent()
            callback()
            return token

    def stop_if_current(self, token, can_stop, callback):
        """Stop playback only if no newer add-on start intent was recorded."""
        with self._locked():
            if token is not None and self._current_intent() != token:
                return False
            if not can_stop():
                return False
            callback()
            return True

    def is_current_intent(self, token):
        """Return whether this start remains the newest add-on playback intent."""
        with self._locked():
            return token is None or self._current_intent() == token
